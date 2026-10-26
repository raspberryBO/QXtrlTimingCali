# -*- coding: utf-8 -*-
"""
ledger.py — 测量台账（只记录，不写回）+ 测量日志（断点续跑）

批量校准的第一阶段：把每一对之间的相对时序原样攒成一堆**观测式**，
交给 solver 之后统一解。这一阶段有一条铁律：

    **不修改任何状态** —— 不改待测文件、不动会话里的 d、也不把偏移量下发给执行方。

正因为测量期间世界是静止的，误差才不会像旧流程那样"测一对写一对"逐级累加；
每条 skew 描述的都是同一个时刻的物理状态，全局求解才有意义。

测量日志（journal）：
  真机上一条边几分钟、一整批几小时，而批量流程把全部观测攒到末尾才写回——
  崩溃的爆炸半径恰好最大。于是每测完一条边就往 pending 旁边的
  measurements-audit.jsonl **追加一行**（含 config_hash）。恢复判据由既有的
  版本纪律直接给出：**日志里 config_hash 与当前待测文件一致的合格观测，
  描述的正是眼前这个世界**，可直接跳过不重测；hash 不一致的记录属于已随
  写回生效的旧世界，作废（阶段级的跳过由 stages 按 stage_done 事件判定）。
  日志只承载未生效的观测与阶段事件；已生效状态仍归 pending 文件——
  就是 WAL 的"日志与状态分离"。

职责边界：
  * 质量判据借用 policies.quality_problems（与策略链共用一份定义），不达标就地重试；
  * 重试用尽仍不达标 → 记为坏边 reason="quarantine"，由上层决定替补还是隔离；
  * 测量途中若发现待测文件被别人改了（config_hash 漂移），立刻停手：
    再测下去得到的是混合两个版本的垃圾数据。这种情况不可重试，直接 aborted。
"""
from __future__ import annotations

import json
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import ExperimentRequest, ExperimentResult
from .graph import GROUP_QUBIT, Pair, bridging_pairs, components
from .policies import Thresholds, quality_problems
from .queue_io import ExecutorAdapter
from .session import CalibrationSession
from .solver import RelativeEdge

JOURNAL_NAME = "measurements-audit.jsonl"


def journal_path_for(pending_path) -> Path:
    """pending 的伴生测量日志路径：<stem>.measurements-audit.jsonl。

    按 pending 文件界定作用域——同一目录放多份待测文件时互不串台；
    重新生成 pending 视为开启新一轮校准（wiring_gen 会顺手清掉旧日志）。
    """
    p = Path(pending_path)
    return p.with_name(p.stem + "." + JOURNAL_NAME)


def load_journal(path) -> list[dict]:
    """读取测量日志（逐行 JSON）。文件缺失返回空；损坏行（崩溃写到一半）跳过。"""
    p = Path(path) if path else None
    if p is None or not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def append_journal(path, record: dict) -> None:
    """追加一行日志（小行 + 追加写，接近原子；半截行由 load_journal 跳过）。"""
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    broken_tail = False
    if p.exists() and p.stat().st_size > 0:
        with p.open("rb") as fh:
            fh.seek(-1, 2)
            broken_tail = fh.read(1) != b"\n"
    with p.open("a", encoding="utf-8") as fh:
        if broken_tail:
            fh.write("\n")       # 崩溃残留的半截行补上边界，别吞掉下一行
        fh.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), **record},
                            ensure_ascii=False) + "\n")


@dataclass
class Measurement:
    """一条边的测量留痕：成功时带有效 skew，失败时带 reason。"""

    pair: Pair
    ok: bool = False
    skew_ns: float = 0.0
    sigma_ns: float = 0.0
    quality: float = 0.0
    request_id: str = ""
    config_hash: str = ""
    attempts: int = 0
    note: str = ""
    reason: str = ""
    wave: int = 0                         # 所属并行波（0 = 未参与波划分）

    @property
    def a(self) -> str:
        return self.pair.a

    @property
    def b(self) -> str:
        return self.pair.b

    def as_edge(self) -> RelativeEdge:
        return RelativeEdge(a=self.pair.a, b=self.pair.b, skew_ns=self.skew_ns,
                            sigma_ns=self.sigma_ns, request_id=self.request_id)

    def to_dict(self) -> dict:
        return {"a": self.pair.a, "b": self.pair.b, "ok": self.ok, "note": self.pair.note,
                "skew_ns": round(self.skew_ns, 6), "sigma_ns": round(self.sigma_ns, 6),
                "quality": round(self.quality, 6), "attempts": self.attempts,
                "request_id": self.request_id, "config_hash": self.config_hash,
                "reason": self.reason, "wave": self.wave}


class MeasurementLedger:
    """串起「申请 → 取结果 → 质量筛选 → 重试」这段重复劳动。

    journal_path 缺省取 pending 旁边的 measurements-audit.jsonl：每条边测完追加
    一行，崩溃后 preload() 按 config_hash 恢复同世界的观测、跳过已测边。
    """

    def __init__(self, session: CalibrationSession, executor: ExecutorAdapter, *,
                 th: Thresholds | None = None, params: dict | None = None,
                 max_attempts: int | None = None, journal_path: str | None = None):
        self.session = session
        self.executor = executor
        self.th = th or Thresholds()
        self.params = dict(params or {})
        self.max_attempts = int(max_attempts or self.th.max_attempts)
        self.records: list[Measurement] = []
        self.requests: list[ExperimentRequest] = []
        self.aborted: str | None = None        # 不可恢复：测量期间文件被外部改动
        if journal_path is None and session.pending_path:
            journal_path = str(journal_path_for(session.pending_path))
        self.journal_path = journal_path
        self._accepted: dict[frozenset[str], Measurement] = {}
        # 提前到达、暂时对不上号的结果先存这里（异步执行方下重试期间别的边会回国），
        # 之后的 _take / _collect 按 request_id 取走；容量有限，防止长跑无限增长。
        self._stash: dict[str, ExperimentResult] = {}

    # ---------------- 断点续跑 ----------------

    def preload(self, records: list[dict] | None = None, *, groups=None,
                require_hash: bool = True) -> int:
        """从测量日志恢复观测，返回新恢复的边数。

        * 默认只收「config_hash 与当前待测文件一致」的合格边——它们描述的正是
          眼前这个世界，可直接当作已测完跳过；hash 不一致的是旧世界观测，作废；
        * groups 给定时按实验分组过滤（替补桥接边同样带组名，一并恢复）；
        * require_hash=False 只用于展示旧世界留痕（阶段跳过时的报告）——这些
          观测的基线已随写回失效，绝不能进入后续求解；
        * 同时把申请序号顶过日志里已用过的编号，避免恢复后重发撞号。
        """
        if records is None:
            records = load_journal(self.journal_path)
        ids = [str(d.get("request_id")) for d in records if d.get("request_id")]
        if ids:
            self.session.advance_seq_past(ids)
        allow = set(groups) if groups is not None else None
        by_key: dict[frozenset[str], Measurement] = dict(self._accepted)
        for d in records:
            if d.get("event") or not d.get("ok"):
                continue
            if allow is not None and d.get("group") not in allow:
                continue
            if require_hash and d.get("config_hash") != self.session.config_hash:
                continue
            pair = Pair(a=str(d["a"]), b=str(d["b"]),
                        kind=str(d.get("kind", "timing.align")),
                        note=str(d.get("note", "")),
                        group=str(d.get("group", GROUP_QUBIT)))
            by_key[pair.key] = Measurement(
                pair=pair, ok=True, skew_ns=float(d.get("skew_ns", 0.0)),
                sigma_ns=float(d.get("sigma_ns", 0.0)),
                quality=float(d.get("quality", 0.0)),
                request_id=str(d.get("request_id", "")),
                config_hash=str(d.get("config_hash", "")),
                attempts=int(d.get("attempts", 1)), reason="日志恢复（断点续跑）")
        before = len(self._accepted)
        self._accepted = by_key
        self.records = list(by_key.values()) + [m for m in self.records if not m.ok]
        return len(by_key) - before

    # ---------------- 单条边 ----------------

    def measure(self, pair: Pair, *, wave: int = 0) -> Measurement:
        """测一对；质量不达标记就重试，用尽则标坏。全程不触碰任何已有位移。

        已在本基线上测过的边（日志恢复进来的）直接返回缓存，不重复消耗实验时间。
        """
        self._ensure_lines(pair)
        cached = self._accepted.get(pair.key)
        if cached is not None:
            return cached
        record = Measurement(pair=pair, note=pair.note, wave=wave)

        for attempt in range(1, self.max_attempts + 1):
            record.attempts = attempt
            req = self._submit(pair, record)
            result = self._take(req)
            if result is None:
                record.reason = "执行方未返回结果"
                break
            if self._absorb(record, result):
                break
        return self._settle(record)

    def run_waves(self, plan, *, wait_s: float = 0.0) -> list[Measurement]:
        """按并行策略测量：同一提交束内的边**先一起提交，再统一收结果**。

        与 `run()` 的差别只在提交方式——真实异步执行方此刻才可能让一束实验重叠；
        同步执行方（SimExecutor / DirectExecutor）逐条 submit，行为与 `run()` 一致。
        质量不达标仍按边重试，hash 漂移仍整批作废，语义一条不变。
        """
        out: list[Measurement] = []
        for wave_no, bundle in plan.bundles():
            if self.aborted:
                break
            entries = []
            requests = []
            for pair in bundle:
                if self.aborted:
                    break
                self._ensure_lines(pair)
                cached = self._accepted.get(pair.key)
                if cached is not None:
                    out.append(cached)
                    continue
                record = Measurement(pair=pair, note=pair.note, wave=wave_no)
                record.attempts = 1            # 与串行路径同款：首试即第 1 次
                entries.append((pair, record))
                requests.append(self._new_request(pair, record))
            if not entries:
                continue
            self.executor.submit_many(requests)    # 执行方可覆盖批量提交入口
            # 收结果：按 request_id 归位；执行方还没给就按 wait_s 继续等
            results = self._collect([r.request_id for _, r in entries], wait_s=wait_s)
            for pair, record in entries:
                if self.aborted:
                    self._settle(record)
                    out.append(record)
                    continue
                result = results.get(record.request_id)
                if result is None:
                    record.reason = "执行方未返回结果（本束结束后仍未回国）"
                    self._settle(record)
                    out.append(record)
                    continue
                if self._absorb(record, result) or self.aborted:
                    self._settle(record)
                    out.append(record)
                    continue
                # 不合格：退回逐条重试（与串行路径同一套逻辑）
                for attempt in range(2, self.max_attempts + 1):
                    if self.aborted:
                        break
                    record.attempts = attempt
                    result = self._take(self._submit(pair, record))
                    if result is None:
                        record.reason = "执行方未返回结果"
                        break
                    if self._absorb(record, result):
                        break
                self._settle(record)
                out.append(record)
        return out

    def _submit(self, pair: Pair, record: Measurement):
        """发一条申请（不取结果），返回 request；波内整束先提交用。"""
        req = self._new_request(pair, record)
        self.executor.submit(req)
        return req

    def _new_request(self, pair: Pair, record: Measurement):
        """只登记申请；串行与整束提交共用编号及溯源逻辑。"""
        req = self.session.new_request(pair.kind, pair.a, pair.b,
                                       params=dict(self.params), note=pair.note)
        record.request_id = req.id
        self.requests.append(req)
        return req

    def reconnect(self, nodes, candidates, *, expected_components: int = 1,
                  max_replacements: int = 8) -> list[dict]:
        """用替补边补足连通性；返回失败留痕，两套编排器共用。"""
        failures = []
        for _ in range(max(0, max_replacements)):
            if self.aborted:
                break
            parts = components(nodes, [m.pair for m in self.accepted()])
            if len(parts) <= expected_components:
                break
            bridges = bridging_pairs(nodes, parts, candidates, self.records)
            if not bridges:
                break
            rec = self.measure(bridges[0])
            if not rec.ok:
                failures.append(rec.to_dict())
        return failures

    def _absorb(self, record: Measurement, result: ExperimentResult) -> bool:
        """把结果记进留痕并做质量判定；返回 True 表示合格（可停止重试）。"""
        record.skew_ns = float(result.skew_ns)
        record.sigma_ns = float(result.sigma_ns)
        record.quality = float(result.quality)
        record.config_hash = result.config_hash
        if result.config_hash != self.session.config_hash:
            # 版本漂移：已测的边全部作废，停手交给上层
            record.ok = False
            record.reason = "待测文件在测量期间被改动，整批观测作废"
            self.aborted = record.reason
            return True
        problems = quality_problems(result, self.th)
        if not problems:
            record.ok = True
            record.reason = "合格"
            return True
        record.reason = (f"质量不达标（第 {record.attempts}/{self.max_attempts} 次）："
                         + "；".join(problems))
        return False

    def _settle(self, record: Measurement) -> Measurement:
        """收尾：进台账、写日志、入缓存（合格边重启后可直接跳过）。"""
        self.records.append(record)
        if record.ok:
            self._accepted[record.pair.key] = record
        self._journal(record)
        return record

    def _stash_put(self, res: ExperimentResult) -> None:
        self._stash[res.request_id] = res
        while len(self._stash) > 1024:      # FIFO 上限：被放弃申请的残留结果别占着内存
            self._stash.pop(next(iter(self._stash)))

    def _collect(self, request_ids: list[str], *, wait_s: float = 0.0) -> dict:
        """按 request_id 收一束结果；必要时按 wait_s 轮询等待执行方补齐。

        与本束无关的结果**缓存不丢弃**——异步执行方下，某条边重试的间隙里
        别的边可能先回国，扔掉它们会造成假性"执行方未返回结果"。
        """
        import time
        want = set(request_ids)
        got = {rid: self._stash.pop(rid) for rid in list(self._stash) if rid in want}
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            for res in self.executor.poll():
                if res.request_id in want:
                    got[res.request_id] = res
                else:
                    self._stash_put(res)
            if len(got) >= len(want) or wait_s <= 0:
                break
            if not (self._inflight() - set(got)):
                break                       # 执行方说没有在途了：不再干等
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        return got

    def _inflight(self) -> set[str]:
        try:
            return set(self.executor.inflight())
        except Exception:
            return set()

    def run(self, pairs) -> list[Measurement]:
        """按给定顺序测完全部边；遇到不可恢复错误就停。"""
        out = []
        for pair in pairs:
            if self.aborted:
                break
            out.append(self.measure(pair))
        return out

    # ---------------- 视图 ----------------

    def accepted(self) -> list[Measurement]:
        return [m for m in self.records if m.ok]

    def failed(self) -> list[Measurement]:
        return [m for m in self.records if not m.ok]

    def stats(self) -> dict:
        return {"records": len(self.records), "accepted": len(self.accepted()),
                "failed": len(self.failed()),
                "requests": len(self.requests), "aborted": self.aborted}

    # ---------------- 内部 ----------------

    def _journal(self, record: Measurement) -> None:
        """一条边测完（无论好坏）就落一行日志：恢复只用合格行，坏行是审计留痕。"""
        if not self.journal_path:
            return
        append_journal(self.journal_path,
                       {"a": record.pair.a, "b": record.pair.b,
                        "group": record.pair.group, "kind": record.pair.kind,
                        "ok": record.ok, "skew_ns": record.skew_ns,
                        "sigma_ns": record.sigma_ns, "quality": record.quality,
                        "attempts": record.attempts, "request_id": record.request_id,
                        "config_hash": record.config_hash or self.session.config_hash,
                        "note": record.pair.note, "reason": record.reason})

    def _ensure_lines(self, pair: Pair) -> None:
        """每条线注册成一个单例块：会话的 new_request 认块名，这里块名就是线名。"""
        for ln in (pair.a, pair.b):
            self.session.ensure_block(ln, [ln])

    def _take(self, req: ExperimentRequest) -> ExperimentResult | None:
        cached = self._stash.pop(req.id, None)
        if cached is not None:
            return cached
        for res in self.executor.poll():
            self._stash_put(res)      # 整批先收完，避免命中后提前返回丢掉后续结果
        return self._stash.pop(req.id, None)

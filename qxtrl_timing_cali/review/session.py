# -*- coding: utf-8 -*-
"""
session.py — 校准会话（CalibrationSession）

职责：审查中心与（后续）编排器**共读**的状态对象。
  * 逐线累计右移 d（读自待测文件）；
  * 同步块簿记 block_id → [线名]，块内共享对齐关系；
  * 生成纯回写计划 plan_align（只算不写，供策略先行检查）；
  * 记账已发出的实验申请。

约定：
  * 时序位置 q = L + d，L 是设备隐藏真值（truth 文件），会话不持有 L；
  * 会话只做「线 + 块」簿记，阈值（tol / d_max）归审查中心所有。
"""
from __future__ import annotations

import itertools
import json
import math
import re
from pathlib import Path

from .contracts import ExperimentRequest, WritePlan
from .writeback import content_hash, load_pending

_REQ_SEQ = re.compile(r"^req-(\d+)-")


class CalibrationSession:
    def __init__(self, delays: dict[str, float], *, chip_id: str = "",
                 pending_path=None, config_hash: str = "", blocks: dict | None = None):
        self.delays: dict[str, float] = {k: float(v) for k, v in delays.items()}
        self.blocks: dict[str, list[str]] = {k: list(v) for k, v in (blocks or {}).items()}
        self.requests: dict[str, ExperimentRequest] = {}
        self.chip_id = chip_id
        self.pending_path = str(pending_path) if pending_path else None
        self.config_hash = config_hash
        # headroom 台账：线名 → 本流水线授予、尚未收回的临时余量（≥ 0）。
        # 校准位移 δ 恒 ≥ 0；唯一允许的负向移动 = 收回这里记着的 headroom。
        self.headroom: dict[str, float] = {}
        self._seq = itertools.count(1)
        self._seq_floor = 0                # 申请序号已用到的最大编号（日志恢复续号用）

    # ---------------- 构造 ----------------

    @classmethod
    def from_pending(cls, pending_path, *, blocks: dict | None = None) -> "CalibrationSession":
        """从待测文件装载逐线 d 与指纹。"""
        data, digest = load_pending(pending_path)
        return cls({e["line"]: float(e["delay"]) for e in data["lines"]},
                   chip_id=str((data.get("meta") or {}).get("chip", "")),
                   pending_path=pending_path, config_hash=digest, blocks=blocks)

    # ---------------- 线 / 块视图 ----------------

    def lines(self) -> list[str]:
        return list(self.delays)

    def delay(self, line: str) -> float:
        return self.delays[line]

    def ensure_block(self, bid: str, lines: list[str]) -> str:
        """登记块（已存在则原样返回，不覆盖既有合并结果）。"""
        if bid not in self.blocks:
            for ln in lines:
                if ln not in self.delays:
                    raise KeyError(f"未知线路：{ln}（不在待测文件中）")
            self.blocks[bid] = list(lines)
        return bid

    def block_repr(self, bid: str) -> str:
        """块代表线 = 首线。"""
        return self.blocks[bid][0]

    def block_of(self, line: str) -> str | None:
        for bid, lines in self.blocks.items():
            if line in lines:
                return bid
        return None

    def unite(self, new_id: str, ref_id: str, into: str | None = None) -> str:
        """块合并：ref 的线在前、new 的线在后（保持代表线为 ref 首线）。"""
        merged = self.blocks.pop(ref_id) + self.blocks.pop(new_id)
        target = into or ref_id
        self.blocks[target] = merged
        return target

    # ---------------- 计划（纯函数，无副作用） ----------------

    def plan_align(self, new_id: str, ref_id: str, skew_ns: float, *,
                   into: str | None = None, tol_ns: float = 0.0,
                   request_id: str = "") -> WritePlan:
        """只右移合并计划：skew = q(new) − q(ref)。

        skew < −tol → new 在左 → 右移 new |skew|
        skew > +tol → ref 在左 → 右移 ref  skew
        |skew| ≤ tol → 零位移，仅合并块
        """
        skew = float(skew_ns)
        move, delta = (new_id, -skew) if skew < -tol_ns else (ref_id, skew)
        if abs(skew) <= tol_ns:
            move, delta = None, 0.0
        shifts = {} if move is None else {ln: round(delta, 6) for ln in self.blocks[move]}
        reason = (f"skew={skew:+.3f}ns |skew|≤tol={tol_ns:g} → 仅合并" if move is None
                  else f"skew={skew:+.3f}ns → 右移块 {move} +{delta:.3f}ns")
        return WritePlan(request_id=request_id, shifts=shifts,
                         merge={"new": new_id, "ref": ref_id, "into": into or ref_id},
                         reason=reason)

    def budget_ok(self, plan: WritePlan, d_max_ns: float | None) -> tuple[bool, str]:
        """预算检查：plan 落盘后每线累计 d 是否仍在 d_max 内。"""
        if d_max_ns is None:
            return True, "未设预算上限"
        for line, delta in plan.shifts.items():
            if self.delays[line] + delta > d_max_ns + 1e-9:
                return False, (f"{line} 累计 d 将达 {self.delays[line] + delta:.1f}ns"
                               f" > d_max={d_max_ns:g}ns")
        return True, f"全部线累计 d ≤ {d_max_ns:g}ns"

    # ---------------- 提交（有副作用） ----------------

    def grant_headroom(self, lines: list[str], amount_ns: float) -> None:
        """登记本流水线给一批线授予的临时余量（步 2 右移 headroom 后调用）。

        只有记在这里的量，才允许在步 6 被收回（headroom_return）；d 本身
        永远不会低于"未加 headroom 的校准基线"。
        """
        h = float(amount_ns)
        if not math.isfinite(h) or h < 0:
            raise ValueError(f"headroom 不可为负：{h}")
        for ln in lines:
            self.headroom[ln] = round(self.headroom.get(ln, 0.0) + h, 6)

    def apply_plan(self, plan: WritePlan) -> dict:
        """把已批准的写回计划落到会话状态：d 增加 + 块合并。

        逐线净位移 = shifts − headroom_return。shifts 部分 δ 恒 ≥ 0；
        headroom_return 是唯一允许的负向移动，且不得超过该线的授予余额。
        """
        plan = WritePlan.from_dict(plan.to_dict())
        updated = {}
        for line in set(plan.shifts) | set(plan.headroom_return):
            if line not in self.delays:
                raise ValueError(f"未知线路：{line}")
            value = round(self.delays[line] + plan.shifts.get(line, 0.0)
                          - plan.headroom_return.get(line, 0.0), 6)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"应用后延时必须有限且非负：{line}")
            updated[line] = value
        if plan.merge:
            new, ref, into = (plan.merge.get(k) for k in ("new", "ref", "into"))
            if not isinstance(new, str) or not isinstance(ref, str) \
                    or new not in self.blocks or ref not in self.blocks or new == ref:
                raise ValueError("合并计划必须引用两个不同的已知同步块")
            if into is not None and (not isinstance(into, str)
                                     or into in self.blocks and into not in (new, ref)):
                raise ValueError("合并目标必须为字符串，且不可覆盖无关同步块")
        for line, ret in plan.headroom_return.items():
            granted = self.headroom.get(line, 0.0)
            if not math.isfinite(granted) or granted < 0 or ret > granted + 1e-9:
                raise ValueError(f"{line} 收回 headroom {ret:.3f}ns 超过授予余额 {granted:.3f}ns")
        self.delays.update(updated)
        for line, ret in plan.headroom_return.items():
            if ret <= 0:
                continue
            self.headroom[line] = round(self.headroom.get(line, 0.0) - ret, 6)
        merged = None
        if plan.merge:
            merged = self.unite(plan.merge["new"], plan.merge["ref"], plan.merge.get("into"))
        return {"shifts": dict(plan.shifts),
                "headroom_return": dict(plan.headroom_return), "merged_block": merged}

    def refresh_hash(self) -> str:
        """回写后重新取待测文件指纹（CAS 基线推进）。"""
        if self.pending_path:
            _, digest = load_pending(self.pending_path)
            self.config_hash = digest
        return self.config_hash

    # ---------------- 实验申请 ----------------

    def advance_seq_past(self, request_ids) -> int:
        """把申请序号顶到已用编号之后（测量日志恢复时防撞号）。

        幂等：_seq_floor 记的是计数器已消耗到的编号（含 new_request 实际用掉的），
        只升不降，重复传入同一批编号不会二次跳号。
        """
        top = 0
        for rid in request_ids:
            m = _REQ_SEQ.match(str(rid))
            if m:
                top = max(top, int(m.group(1)))
        while self._seq_floor < top:
            next(self._seq)
            self._seq_floor += 1
        return self._seq_floor

    def new_request(self, kind: str, new_id: str, ref_id: str, *,
                    into: str | None = None, params: dict | None = None,
                    note: str = "") -> ExperimentRequest:
        n = next(self._seq)
        self._seq_floor = max(self._seq_floor, n)
        req = ExperimentRequest(
            id=f"req-{n:03d}-{kind.split('.')[-1]}",
            kind=kind, targets=[self.block_repr(new_id), self.block_repr(ref_id)],
            config_hash=self.config_hash,
            intent={"op": "align", "new": new_id, "ref": ref_id, "into": into or ref_id},
            params=dict(params or {}), note=note)
        self.requests[req.id] = req
        return req

    # ---------------- 快照 / 报告 ----------------

    def to_dict(self) -> dict:
        return {"chip": self.chip_id, "pending": self.pending_path,
                "config_hash": self.config_hash,
                "delays": dict(self.delays), "blocks": {k: list(v) for k, v in self.blocks.items()},
                "headroom": dict(self.headroom),
                "requests": [r.to_dict() for r in self.requests.values()]}

    def save(self, path="runs/session.json") -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
        return p


def position_residual(truth: dict[str, float], delays: dict[str, float]) -> dict:
    """离线核对指标：q = L + d 的极差（残差）。仅模拟端可算（需要 truth 文件）。"""
    q = {ln: truth[ln] + delays.get(ln, 0.0) for ln in truth}
    if not q:
        return {"count": 0, "residual_ns": 0.0, "max_ns": None, "min_ns": None, "q": {}}
    return {"count": len(q), "residual_ns": round(max(q.values()) - min(q.values()), 6),
            "max_ns": round(max(q.values()), 6), "min_ns": round(min(q.values()), 6),
            "q": {k: round(v, 6) for k, v in q.items()}}

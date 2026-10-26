# -*- coding: utf-8 -*-
"""
stages.py — 六步顺序执行的校准实验

真实机台上不能"所有线一起做优化"，实验之间有硬性的先后依赖：

  步 1  读取线校准（RL.ri ↔ RL.ro 对齐）     → **测完立刻写回**
        读取脉冲的发射(ro)与采样窗口(ri)没对准，后面任何实验都读不出信号。

  步 2  读取线整体右移 headroom             → **立刻写回**
        给后续 qubit / coupler 对齐预留空间；数值可按经验设定（默认 0 ns，
        典型值 1000 ns）。headroom 是流水线**自己授予的临时余量**，记入会话
        台账，最终会在步 6 被收回（读取线往左拉回对齐）。

  步 3  单比特 XY-Z 校准（Q.xy ↔ Q.z）       → 只记录，先不写
        读取线可用后才能逐比特把 xy 与 z 对准。

  步 4  比特间 Z-Z / 耦合器 Z 校准           → 只记录，先不写
        单比特门能跑通之后，做相邻比特 z-z 以及耦合器 CP.z 挂到相邻比特。
        可调比特用 Q.z，不可调比特退化为 Q.xy。

  步 5  统一求解 qubit + coupler            → 求解并写回
        步 3、步 4 的观测同基线，合并做一次全局加权最小二乘，把所有 xy/z
        对齐；readout 暂时不动。

  步 6  收尾求解：读取线收回 headroom 拉回对齐 → **立刻写回**
        readout ↔ 首腔 qubit.xy 的实测 skew 把"读取线世界"与"qubit/coupler
        世界"连起来，qubit/coupler 内部以步 5 结果为"已对齐"软约束，做一次
        全局最小二乘。锚 = qubit/coupler 最右的线（步 5 的目标）：
        加了 headroom 时读取线在其右侧，解出的负向位移以**收回 headroom**
        的方式落盘——单调守卫对校准位移仍是 δ≥0，收回量受授予台账约束，
        d 永远不会低于加 headroom 之前的校准基线；没加 headroom 时读取线
        本来就在左边，照旧只向右拉。

三条纪律：
  * 组间不混解 —— 步 1/2/6 各自写回后，旧观测失效；
  * 步 3、步 4 的观测同基线，可以合并；
  * 前一步没做完，后一步不该开始 —— 依赖关系写死在 `StagePlan.depends_on`
    里，编排器按 STAGE_GROUPS 顺序执行。

分阶段驱动（供上层调度器）：真机上校准只是整条实验流程的一环——读完 ro/ri 还要插
S21 / spectrum / scatter / power Rabi 等实验，剩下的组什么时候跑不该由本仓库替
上层决定。因此除 `run()`（一次跑完，给 demo）外另提供三个**每次只推进一步**的入口：

  * `stage_status()`  只看不跑：各组 state / applied / ready / blocked_by + 下一步建议；
  * `run_group(name)` 只跑一组；依赖不满足就拒绝，绝不顺带跑后面的组；
  * `run_final()`     只跑步 5（延后组合并求解 + 整批一次写回）。

三者跨进程可用：状态与观测全部来自待测文件指纹和测量日志，步 5 的全局解也随
`final_solve` 事件带走，所以步 6 在新进程里单独驱动仍能正确收回 headroom。
CLI：`python -m qxtrl_timing_cali.stages --status [--json]` /
`--stage readout|qubit|coupler|readout_final` / `--final`。
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from .review.center import ReviewCenter
from .review.graph import (GROUP_COUPLER, GROUP_QUBIT, GROUP_READOUT, GROUP_READOUT_FINAL,
                           STAGE_GROUPS, Pair, candidate_pairs, components,
                           minimum_spanning_forest, pairs_of_group)
from .review.ledger import (MeasurementLedger, append_journal, journal_path_for,
                            load_journal)
from .review.parallel import ParallelPlan, plan_waves, summarize
from .review.session import CalibrationSession, position_residual
from .review.solver import RelativeEdge, SolveResult, solve, solve_components, solve_main_component
from .review.writeback import WriteGuardError, read_delays

IMMEDIATE = "immediate"      # 测完就地写回（组 1）
DEFERRED = "deferred"        # 只记录，末尾统一写回（组 2 / 组 3）

_EMPTY_SOLVE = SolveResult(offsets={}, shifts={}, request_id="staged-none", reason="无合格观测")

_STAGE_META = {
    GROUP_READOUT: ("读取线校准（ro ↔ ri 对齐）", IMMEDIATE, (),
                    "后续一切实验的读出前提：测完立刻写回，后面的实验才读得出信号"),
    GROUP_QUBIT: ("单比特 XY-Z 校准（xy ↔ z）", DEFERRED, (GROUP_READOUT,),
                  "依赖已对齐的读取线：先记录，末尾与耦合器组一起解"),
    GROUP_COUPLER: ("比特间 Z-Z / 耦合器 Z 校准", DEFERRED, (GROUP_QUBIT,),
                    "依赖可用的单比特门：先记录，末尾一起解"),
    GROUP_READOUT_FINAL: ("读取线最终对齐（收回 headroom）", IMMEDIATE, (GROUP_COUPLER,),
                         "依赖 qubit/coupler 已全局对齐：以 readout↔首腔 qubit.xy 的实测"
                         " skew 收尾，读取线把 headroom 收回、往左拉回对齐"),
}


@dataclass
class StagePlan:
    """一组实验：测哪些线、哪些边、测完是否立刻写回、依赖谁。"""

    name: str
    title: str
    nodes: list[str]
    pairs: list[Pair]
    writeback: str = DEFERRED
    depends_on: tuple[str, ...] = ()
    why: str = ""
    expected_components: int = 1     # 组内连通后应有的分量数（达不到才需要替补边）
    parallel: ParallelPlan | None = None   # 并行策略：这组的边能分成几波（纯提示，见 parallel.py）

    @property
    def immediate(self) -> bool:
        return self.writeback == IMMEDIATE

    @property
    def wave_summary(self) -> str:
        return summarize(self.parallel) if self.parallel else "未划分"

    def to_dict(self) -> dict:
        return {"name": self.name, "title": self.title, "nodes": list(self.nodes),
                "writeback": self.writeback, "depends_on": list(self.depends_on),
                "why": self.why, "expected_components": self.expected_components,
                "waves": self.parallel.to_dict() if self.parallel else None,
                "pairs": [_pair_dict(p, self.name) for p in self.pairs]}


def _pair_dict(pair: Pair, group: str) -> dict:
    """边的序列化 + 它所属的实验分组（分组只有在这份报告里才有意义）。"""
    return {**pair.to_dict(), "group": group}


@dataclass
class StageOutcome:
    """一组实验的留痕：台账 +（即时组的）求解与回执。

    resumed 非空 = 该阶段已随写回生效，本轮按测量日志直接跳过（不重测、不再写）。
    """

    plan: StagePlan
    ledger: MeasurementLedger
    solve: SolveResult | None = None
    receipt: dict | None = None
    written: bool = False
    aborted: str | None = None
    resumed: str = ""

    @property
    def ok(self) -> bool:
        return self.aborted is None and bool(self.ledger.accepted())

    def to_dict(self) -> dict:
        return {"name": self.plan.name, "title": self.plan.title,
                "writeback": self.plan.writeback, "depends_on": list(self.plan.depends_on),
                "why": self.plan.why, "nodes": list(self.plan.nodes),
                "waves": self.plan.parallel.to_dict() if self.plan.parallel else None,
                "pairs": [_pair_dict(p, self.plan.name) for p in self.plan.pairs],
                "measurements": [m.to_dict() for m in self.ledger.records],
                "failed": [m.to_dict() for m in self.ledger.failed()],
                "solve": self.solve.to_dict() if self.solve else None,
                "receipt": self.receipt, "written": self.written,
                "aborted": self.aborted, "resumed": self.resumed,
                "ledger": self.ledger.stats()}


# ===================== 分组选边 =====================

def plan_stages(chip: dict, lines: list[str] | None = None, *,
                max_parallel: int = 0) -> list[StagePlan]:
    """按 STAGE_GROUPS 顺序，给出每组实际涉及的节点与最少测量边。

    `max_parallel` 只影响**展示与提交粒度**：>0 时每波再切成不超过该长度的提交束；
    0 表示"一波就是一个提交束，能开多少由执行方定"。波的划分本身与它无关。
    """
    known = set(lines) if lines is not None else None
    cands = candidate_pairs(chip, lines)
    out: list[StagePlan] = []

    for name in STAGE_GROUPS:
        title, writeback, depends, why = _STAGE_META[name]
        group_pairs = [p for p in pairs_of_group(cands, name)
                       if known is None or (p.a in known and p.b in known)]
        # 节点由本组候选边实际产生，避免节点与边不一致（如 tunable 开关影响 z 线存在性）
        nodes = sorted({n for p in group_pairs for n in (p.a, p.b)})
        pairs = minimum_spanning_forest(nodes, group_pairs)
        out.append(StagePlan(name=name, title=title, nodes=nodes, pairs=pairs,
                             writeback=writeback, depends_on=depends, why=why,
                             expected_components=len(components(nodes, pairs)),
                             parallel=plan_waves(pairs, bundle_size=max(0, int(max_parallel)))))
    return out


# ===================== 编排器 =====================

class StagedCalibrationPipeline:
    """四组（六步）顺序执行、数据依赖的校准编排器。

    - 组 1（`immediate`）：测完立刻求解并落盘，之后刷新指纹、把位移下发给设备；
    - 组 2 / 组 3（`deferred`）：只测量、只记录，一个字节都不写；
    - `finish()`：把组 2 + 组 3 的观测合起来做一次全局最小二乘，整批一次写回；
    - 步 6（readout_final）：整芯片收尾求解——读取线把步 2 授予的 headroom
      收回，往左拉回对齐到 qubit.xy。
    """

    def __init__(self, chip: dict, center: ReviewCenter, *, max_replacements: int = 8,
                 max_iter: int | None = None, id_prefix: str = "stage",
                 readout_headroom_ns: float | None = None,
                 max_parallel: int = 1, wait_s: float = 0.0):
        self.chip = chip
        self.center = center
        self.session = center.session
        self.max_replacements = int(max_replacements)
        self.max_iter = max_iter
        self.id_prefix = id_prefix
        # 并行：1 = 逐条提交（默认，行为与历史版本逐位一致）；>1 = 按波的提交束一起提交。
        # 波只是提交提示，执行方可以按顺序跑完一束（hardware_hold 独占时本就如此）。
        self.max_parallel = max(1, int(max_parallel))
        self.wait_s = max(0.0, float(wait_s))
        self.aborted: str | None = None
        self.bridge_failures: list[dict] = []
        # 步 5 的全局解（供步 6 作"qubit/coupler 已对齐"软约束）；直接调
        # run_stage(readout_final) 而没跑过 finish() 时为 None → 步 6 退化为旧行为
        self._final_solve: SolveResult | None = None
        self._deferred_nodes: list[str] = []
        # headroom 默认优先来自 center 参数，其次 0（保持旧行为）
        if readout_headroom_ns is None:
            readout_headroom_ns = float(center.default_params.get("readout_headroom_ns", 0.0))
        self.readout_headroom_ns = max(0.0, float(readout_headroom_ns))

    # ---------------- 计划 ----------------

    def lines(self) -> list[str]:
        return self.session.lines()

    def candidates(self) -> list[Pair]:
        return candidate_pairs(self.chip, self.lines())

    def stage_plans(self) -> list[StagePlan]:
        return plan_stages(self.chip, self.lines(), max_parallel=self.max_parallel)

    # ---------------- 单组执行 ----------------

    def run_stage(self, plan: StagePlan) -> StageOutcome:
        """测完一组；即时组顺便求解并落盘。"""
        hash_before = self.session.config_hash
        ledger = self._measure(plan)
        outcome = StageOutcome(plan=plan, ledger=ledger)

        if ledger.aborted:
            outcome.aborted = f"测量中止：{ledger.aborted}"
            return outcome
        if not ledger.accepted():
            outcome.aborted = f"{plan.title} 无合格观测"
            return outcome
        if not plan.immediate:
            self._journal_event(stage=plan.name, hash_before=hash_before,
                                hash_after=hash_before)
            return outcome                      # 延后组：只留台账，末尾统一用

        if plan.name == GROUP_READOUT_FINAL:
            self.replay_headroom_grants()          # 授予台账可能在上一个进程里
            if self._final_solve is None:
                # 调度器在另一个进程里单独驱动步 6：从测量日志取回步 5 的全局解
                self._final_solve = self._final_solve_from_journal()
            if not self._deferred_nodes:
                self._deferred_nodes = self._deferred_nodes_from_plans()
            result = self._solve_readout_final(plan, ledger)
        else:
            result = solve_components(plan.nodes, [m.as_edge() for m in ledger.accepted()],
                                      request_id=f"{self.id_prefix}-{plan.name}-solve",
                                      max_iter=self.max_iter)
        if plan.name == GROUP_READOUT and self.readout_headroom_ns > 0:
            result = self._with_headroom(result)
        outcome.solve = result
        if not result.shifts and not result.headroom_return:
            outcome.aborted = f"{plan.title} 求解为空，未写回"
            return outcome
        receipt = self._write(result)
        outcome.receipt = receipt
        outcome.written = bool(receipt and receipt.get("written"))
        if receipt is None and self.center.write and self.session.pending_path:
            outcome.aborted = self.aborted or "回写未落盘"
            return outcome
        if self.aborted:
            outcome.aborted = self.aborted
            return outcome
        self._apply(result, receipt)            # 推进会话指纹 + 下发设备
        grant = None
        if plan.name == GROUP_READOUT and self.readout_headroom_ns > 0:
            # headroom 是流水线自己授予的临时余量：记入会话台账，步 6 才能收回
            self.session.grant_headroom(list(result.shifts), self.readout_headroom_ns)
            grant = {"lines": sorted(result.shifts), "amount_ns": self.readout_headroom_ns}
        # 收回量同样入日志：新进程按事件重放时"只放不收"会把已收回的余额又加回来
        returned = {ln: round(v, 6) for ln, v in result.headroom_return.items() if v > 0}
        self._journal_event(stage=plan.name, hash_before=hash_before,
                            hash_after=self.session.config_hash, headroom=grant,
                            headroom_return=returned or None)
        return outcome

    def measure(self, *, include_final: bool = False) -> list[StageOutcome]:
        """按依赖顺序跑主阶段；final 阶段默认由 run() 在 finish 之后单独调度。

        断点续跑：日志里 stage_done 事件的测量基线（hash_before）与当前待测文件
        不一致，说明该阶段已随写回生效——直接跳过，不重测不重写。
        """
        outcomes: list[StageOutcome] = []
        done: dict[str, StageOutcome] = {}
        events = self._stage_events()
        for plan in self.stage_plans():
            if not include_final and plan.name == GROUP_READOUT_FINAL:
                continue
            ev = events.get(plan.name)
            if ev is not None and ev.get("hash_before") != self.session.config_hash:
                outcome = self._resumed_outcome(plan, ev)
                outcomes.append(outcome)
                done[plan.name] = outcome
                continue                       # 已随写回生效：本轮跳过
            for dep in plan.depends_on:
                prev = done.get(dep)
                if prev is None or prev.aborted:
                    self.aborted = (self.aborted
                                    or f"前置实验「{dep}」未就绪，"
                                       f"「{plan.name}」及后续实验不执行")
                    return outcomes
            outcome = self.run_stage(plan)
            outcomes.append(outcome)
            done[plan.name] = outcome
            if outcome.aborted and plan.immediate:
                self.aborted = self.aborted or outcome.aborted
                return outcomes                # 读取基准缺失：后面全停
        return outcomes

    # ---------------- 末尾：统一求解 + 一次写回 ----------------

    def finish(self, outcomes: list[StageOutcome],
               truth: dict[str, float] | None = None) -> dict:
        """把延后组（qubit + coupler）的观测合并成一次全局求解并写回。"""
        edges = self._final_edges(outcomes)
        if not edges:
            self.aborted = self.aborted or "组 2 / 组 3 无合格观测：不求解、不写回"
        nodes = self._final_nodes(outcomes)
        solve_nodes, result = nodes, _EMPTY_SOLVE
        if not self.aborted:
            # solve() 只吃单个连通分量（各分量零点不可比）：坏边把延后图劈开时
            # 只解最大分量，其余线划进隔离区——绝不给从未测过的线凭空造位移。
            # 与 CalibrationPipeline.solve() 同一条纪律。
            result = solve_main_component(nodes, edges, request_id=f"{self.id_prefix}-final",
                                          max_iter=self.max_iter)
            solve_nodes = list(result.offsets)
        self._deferred_nodes = list(solve_nodes)   # 步 6 的"已对齐"软约束来源
        self._final_solve = result
        hash_before = self.session.config_hash   # 写回前的指纹（随 final_solve 事件带走）
        receipt = None if self.aborted else self._write(result)
        if not self.aborted:
            self._apply(result, receipt)
            if receipt is not None and receipt.get("written"):
                # 步 6 可能由上层调度器在另一个进程里单独驱动，把全局解随日志带走
                self._journal_final_solve(result, hash_before)
        return self.report(outcomes, result, receipt, truth, nodes=nodes)

    # ---------------- 供上层调度器：按组单独驱动 ----------------

    def replay_headroom_grants(self) -> dict[str, float]:
        """从测量日志重建 headroom 台账（授予与收回都重放），返回当前未收回的余额。

        步 2 的授予与步 6 的收回发生在**某些进程**里，而各组常由调度器在不同
        进程单独驱动——新进程的会话台账是空的，守卫会以为"没授予过"从而拒绝
        收回。好在授予记在 `stage_done.headroom`、收回记在 `stage_done.headroom_return`，
        按事件顺序**整账重算**即可。

        整账重算天然幂等：同一份日志永远重放出同一份台账，调用多少次都不叠加
        ——"只放不收把已收回的余额加回来"与"resumed 阶段重复入账把余额翻倍"
        两类事故都源于增量补账，这里一并杜绝。日志里没有事件时不动现有台账
        （保留不经日志的构造场景）。
        """
        events = [d for d in load_journal(self._journal_file())
                  if d.get("event") == "stage_done"]
        if not events:
            return {ln: v for ln, v in self.session.headroom.items() if v > 0}
        ledger: dict[str, float] = {}
        for d in events:
            grant = d.get("headroom") or {}
            lines = [str(x) for x in grant.get("lines", [])]
            amount = float(grant.get("amount_ns", 0.0) or 0.0)
            if lines and amount > 0:
                for ln in lines:
                    ledger[ln] = round(ledger.get(ln, 0.0) + amount, 6)
            for ln, v in (d.get("headroom_return") or {}).items():
                ledger[str(ln)] = round(ledger.get(str(ln), 0.0) - float(v), 6)
        self.session.headroom = {ln: v for ln, v in ledger.items() if v > 1e-9}
        return dict(self.session.headroom)

    def stage_status(self) -> dict:
        """体检报告：每组什么状态、现在能跑哪组、还差什么（不测量、不写回文件）。

        唯一的状态动作是把测量日志里的 headroom 台账重放进会话（授予 − 收回，
        整账重算、幂等）——不重放的话，新进程会以为"从没授予过"，步 6 的收回
        会被守卫拒绝。

        真机上校准只是整条实验流程的一环：读完 `ro/ri` 还要插 S21 / spectrum /
        scatter / power Rabi 等实验，剩下的组什么时候跑由上层调度器决定。
        本方法把"跑没跑过、写没写回、前置条件够不够"问清楚，调度器每次调用前
        先看它，再决定跑哪一组——**绝不自动往后跑**。

        跨进程可用：状态全部来自待测文件指纹与测量日志，不依赖内存态。
        """
        plans = {p.name: p for p in self.stage_plans()}
        events = self._stage_events()
        now = self.session.config_hash
        journal = load_journal(self._journal_file())
        # 与本轮同版本（config_hash 一致）的合格观测条数：延后组测过就知道
        measured: dict[str, int] = {}
        for d in journal:
            if d.get("event") or not d.get("ok") or d.get("config_hash") != now:
                continue
            g = str(d.get("group") or "")
            measured[g] = measured.get(g, 0) + 1
        groups: dict[str, dict] = {}

        for name, plan in plans.items():
            ev = events.get(name)
            # 基线 hash 被后来的写回推走 ⇒ 该组已随写回生效（重启后应跳过，不重测）
            applied = ev is not None and str(ev.get("hash_before")) != now
            edges = measured.get(name, 0)
            if applied:
                state = "applied"
            elif plan.writeback == IMMEDIATE:
                state = "pending"
            else:
                state = "measured" if edges else "pending"
            groups[name] = {"title": plan.title, "writeback": plan.writeback,
                            "depends_on": list(plan.depends_on),
                            "why": plan.why,
                            "pairs": len(plan.pairs), "nodes": len(plan.nodes),
                            "journaled": ev is not None, "applied": applied,
                            "measured_edges": edges, "state": state,
                            "waves": plan.parallel.to_dict() if plan.parallel else None}

        # 步 5（延后组整批写回）不是一个 StagePlan，但同样是可单独驱动的动作
        deferred = [groups.get(GROUP_QUBIT), groups.get(GROUP_COUPLER)]
        final_applied = bool(deferred) and all(g is not None and g["applied"] for g in deferred)

        def _dep_ok(dep: str) -> bool:
            g = groups.get(dep)
            if g is None:
                return False
            return g["applied"] if g["writeback"] == IMMEDIATE else g["journaled"]

        for name, plan in plans.items():
            blocked = [d for d in plan.depends_on if not _dep_ok(d)]
            if name == GROUP_READOUT_FINAL and not final_applied:
                blocked.append("final（步 5 整批写回未生效）")
            groups[name]["blocked_by"] = blocked
            groups[name]["ready"] = not blocked

        todo: list[str] = []
        deferred_measured = {n: (groups[n]["measured_edges"] or groups[n]["applied"])
                             for n in (GROUP_QUBIT, GROUP_COUPLER) if n in groups}
        for name in STAGE_GROUPS:
            if groups[name]["ready"] and not groups[name]["applied"]:
                # 延后组在本版本下已经测过 ⇒ 没有新边可测，不再挂进 todo
                if not (groups[name]["writeback"] == DEFERRED and groups[name]["measured_edges"]):
                    todo.append(name)
            if (name == GROUP_COUPLER and not final_applied
                    and all(deferred_measured.get(n) for n in (GROUP_QUBIT, GROUP_COUPLER))):
                todo.append("final")        # 两组都测完了，就差步 5 的一次整批写回
        granted = self.replay_headroom_grants()   # 跨进程时把日志里的授予补回台账
        return {"chip": self.session.chip_id, "config_hash": now,
                "max_parallel": self.max_parallel,
                "readout_headroom_ns": self.readout_headroom_ns,
                "granted_headroom": granted,
                "groups": groups,
                "final": {"applied": final_applied,
                          "reason": "延后组观测合成一次全局最小二乘 → 整批一次写回"},
                "todo": todo}

    def run_group(self, name: str) -> StageOutcome:
        """只跑一组实验（上层调度器的入口；绝不顺带把后面的组跑起来）。

        * 没跑过  → 正常测量：即时组测完立刻求解写回，延后组只记录不写；
        * 已生效  → 直接跳过，返回带 `resumed` 的留痕（不重测、不再写）；
        * 依赖不够 → 拒绝执行，`aborted` 说清缺什么（文件一个字节不动）。
        """
        plans = {p.name: p for p in self.stage_plans()}
        if name not in plans:
            raise KeyError(f"未知实验组 {name!r}（应为 {list(plans)}）")
        plan = plans[name]
        info = self.stage_status()["groups"][name]
        if not info["ready"]:
            return StageOutcome(plan=plan, ledger=self._fresh_ledger(),
                                aborted=f"前置条件不满足（{info['blocked_by']}）："
                                        f"先完成它们再跑 {name}")
        if info["applied"]:
            return self._resumed_outcome(plan, self._stage_events()[name])
        return self.run_stage(plan)

    def run_final(self, truth: dict[str, float] | None = None) -> dict:
        """只执行步 5：延后组观测合成一次全局最小二乘 → 整批一次写回。

        给"每次只驱动一步"的调度器用：观测全部从测量日志重建，不依赖内存里的
        outcomes（进程重启过也能跑）。已经写回过就只重算求解、不再落盘——
        末尾求解是确定性纯计算，重算即可。
        """
        outcomes = self.deferred_outcomes()
        if self._finish_already_written(outcomes):
            result = self._reconstruct_final_solve(outcomes)
            self._final_solve = result
            return self.report(outcomes, result, None, truth, nodes=self._deferred_nodes)
        return self.finish(outcomes, truth)

    def deferred_outcomes(self) -> list[StageOutcome]:
        """从测量日志重建延后组（qubit + coupler）的留痕，供步 5 单独续跑。

        整批还没写回 → 只收"与本轮同版本"的合格观测（require_hash=True）；
        已经写回过   → 收旧世界的观测（require_hash=False），仅用于确定性重算。
        """
        plans = {p.name: p for p in self.stage_plans()}
        events = self._stage_events()
        applied = any(str(events.get(n, {}).get("hash_before", "")) != self.session.config_hash
                      for n in (GROUP_QUBIT, GROUP_COUPLER) if n in events)
        out: list[StageOutcome] = []
        for name in (GROUP_QUBIT, GROUP_COUPLER):
            plan, ev = plans.get(name), events.get(name)
            if plan is None or ev is None:
                continue                        # 这组还没测过：步 5 无从解起
            ledger = self._fresh_ledger()
            ledger.preload(groups={name}, require_hash=not applied)
            out.append(StageOutcome(
                plan=plan, ledger=ledger,
                resumed="从测量日志恢复（步 5 由调度器单独驱动）" if applied else ""))
        return out

    def _fresh_ledger(self) -> MeasurementLedger:
        """空台账（拒绝执行 / 纯查询时也要有对象可序列化）。"""
        return MeasurementLedger(self.session, self.center.executor, th=self.center.th,
                                 params=self.center.default_params)

    def _deferred_nodes_from_plans(self) -> list[str]:
        """延后组涉及的节点（等价于 `_final_nodes(outcomes)`，但不依赖内存态）。"""
        nodes: list[str] = []
        for name in (GROUP_QUBIT, GROUP_COUPLER):
            plan = next((p for p in self.stage_plans() if p.name == name), None)
            if plan is None:
                continue
            for n in plan.nodes:
                if n not in nodes:
                    nodes.append(n)
        return nodes

    def _final_solve_from_journal(self) -> SolveResult | None:
        """步 5 的全局解（步 6 的"qubit/coupler 已对齐"软约束来源）。

        步 6 常由调度器在**另一个进程**里单独驱动，那时内存里没有 `_final_solve`；
        好在求解是确定性纯计算，finish() 已把它随 final_solve 事件记进测量日志，
        这里按"写回后的指纹"取回同一份。
        """
        for d in reversed(load_journal(self._journal_file())):
            if d.get("event") != "final_solve":
                continue
            if d.get("hash_after") == self.session.config_hash:
                result = SolveResult.from_dict(d.get("solve") or {})
                return result if result.offsets else None
        return None

    def _journal_final_solve(self, result: SolveResult, hash_before: str) -> None:
        p = self._journal_file()
        if p is not None:
            append_journal(p, {"event": "final_solve", "solve": result.to_dict(),
                               "hash_before": hash_before,
                               "hash_after": self.session.config_hash})
    def run(self, truth: dict[str, float] | None = None) -> dict:
        """六步一次跑完：readout → headroom → qubit → coupler → 全局求解 → readout_final。"""
        plans = {p.name: p for p in self.stage_plans()}
        final_plan = plans.get(GROUP_READOUT_FINAL)

        # 前 3 个主阶段
        outcomes = self.measure()
        if self._finish_already_written(outcomes):
            # 组 2+3 的整批写回已随上一次运行落盘（日志判定）：不重测不重写，
            # 只用旧世界的观测重建步 6 所需的全局解（求解是确定性纯计算）。
            result = self._reconstruct_final_solve(outcomes)
            self._final_solve = result
            report = self.report(outcomes, result, None, truth, nodes=self._deferred_nodes)
        else:
            report = self.finish(outcomes, truth)
        if report["aborted"] or final_plan is None:
            return report

        # 步 6：读取线最终对齐到 qubit.xy（已生效则按日志跳过）
        ev = self._stage_events().get(GROUP_READOUT_FINAL)
        if ev is not None and ev.get("hash_before") != self.session.config_hash:
            final_outcome = self._resumed_outcome(final_plan, ev)
        else:
            final_outcome = self.run_stage(final_plan)
        outcomes.append(final_outcome)
        report["stages"].append(final_outcome.to_dict())
        report["tree"] = [_pair_dict(p, o.plan.name) for o in outcomes for p in o.plan.pairs]
        report["measurements"] = [m.to_dict() for o in outcomes for m in o.ledger.records]
        report["failed"] = [m.to_dict() for o in outcomes for m in o.ledger.failed()]
        report["ledger"] = {
            "records": sum(len(o.ledger.records) for o in outcomes),
            "accepted": sum(len(o.ledger.accepted()) for o in outcomes),
            "failed": sum(len(o.ledger.failed()) for o in outcomes),
            "requests": sum(len(o.ledger.requests) for o in outcomes),
            "aborted": next((o.ledger.aborted for o in outcomes if o.ledger.aborted), None),
        }
        if final_outcome.aborted:
            self.aborted = self.aborted or final_outcome.aborted
            report["aborted"] = self.aborted
            report["aligned"] = False
            report["converged"] = False
        else:
            # 步 6 的解（真正最后落盘、含 headroom_return 的那份）附加在 "final_solve"
            # 键下供消费者取用；顶层 solve/anchor/plan_ns 仍保持步 5 全局解视角——
            # 动画数据管线靠它还原 d2 段（qubit/coupler 整批写回），不能换视角。
            if final_outcome.solve is not None:
                report["final_solve"] = final_outcome.solve.to_dict()
            # 全部完成后重新评估残差：所有线都已参与（qubit/coupler + readout）
            residual = position_residual(truth, self.session.delays) if truth else None
            tolerable = residual is None or residual["residual_ns"] <= self.center.th.tol_ns
            covered = set(report["solve"]["offsets"])
            if final_outcome.solve is not None:
                covered.update(final_outcome.solve.offsets)
            else:
                covered.update(m.a for m in final_outcome.ledger.accepted())
                covered.update(m.b for m in final_outcome.ledger.accepted())
                covered.update(ln[:-3] + ".ri" for ln in list(covered) if ln.endswith(".ro"))
            report["isolated"] = sorted(set(self.lines()) - covered)
            report["residual"] = residual
            report["converged"] = bool(report["solve"]["offsets"]) and not report["isolated"] \
                and report["solve"]["solver_converged"] and not self.aborted
            report["aligned"] = bool(report["converged"] and tolerable and not self.aborted)
        report["written"] = bool(report["written"] or final_outcome.written)
        return report

    # ---------------- 内部 ----------------

    # ---- 测量日志（断点续跑）----

    def _journal_file(self) -> Path | None:
        if not self.session.pending_path:
            return None
        return journal_path_for(self.session.pending_path)

    def _stage_events(self) -> dict[str, dict]:
        """日志里的 stage_done 事件（stage → 最后一条）。"""
        out: dict[str, dict] = {}
        for d in load_journal(self._journal_file()):
            if d.get("event") == "stage_done" and d.get("stage"):
                out[str(d["stage"])] = d
        return out

    def _journal_event(self, **fields) -> None:
        p = self._journal_file()
        if p is not None:
            append_journal(p, {"event": "stage_done", **fields})

    def _resumed_outcome(self, plan: StagePlan, ev: dict) -> StageOutcome:
        """从日志合成「已生效阶段」的结果：不重测、不再写，只还原留痕。

        headroom 台账**不在这里补**——统一走 replay_headroom_grants() 按日志
        整账重算；此处再授一次会把余额翻倍（授予上限被虚击穿，步 6 可能
        把 d 拉破"加 headroom 前的校准基线"）。
        """
        ledger = MeasurementLedger(self.session, self.center.executor, th=self.center.th,
                                   params=self.center.default_params)
        ledger.preload(groups={plan.name}, require_hash=False)   # 仅展示旧世界留痕
        return StageOutcome(
            plan=plan, ledger=ledger, resumed=(
                f"测量基线 {str(ev.get('hash_before'))[:8]} ≠ 当前 "
                f"{self.session.config_hash[:8]}：该阶段已随写回生效，本轮跳过重测"))

    def _finish_already_written(self, outcomes: list[StageOutcome]) -> bool:
        """组 2+3 是否都已随写回生效（其测量基线 hash 已被整批写回推过去）。"""
        deferred = [o for o in outcomes if not o.plan.immediate]
        return bool(deferred) and all(o.resumed for o in deferred)

    def _reconstruct_final_solve(self, outcomes: list[StageOutcome]) -> SolveResult:
        """用旧世界的观测重建步 5 的全局解（确定性纯计算，不需要重测）。"""
        edges: list[RelativeEdge] = []
        nodes: list[str] = []
        for out in outcomes:
            if out.plan.immediate or not out.resumed:
                continue
            edges += [m.as_edge() for m in out.ledger.accepted()]
            nodes += out.plan.nodes
        nodes = list(dict.fromkeys(nodes))
        # 与 finish() 同一条纪律：只解最大连通分量，劈在外面的线进隔离区
        result = solve_main_component(nodes, edges,
                                      request_id=f"{self.id_prefix}-final-resumed", max_iter=self.max_iter)
        self._deferred_nodes = list(result.offsets)
        return result

    # ---- 测量与写回 ----

    def _measure(self, plan: StagePlan) -> MeasurementLedger:
        ledger = MeasurementLedger(self.session, self.center.executor, th=self.center.th,
                                   params=self.center.default_params)
        ledger.preload(groups={plan.name})     # 断点续跑：同基线已测过的边不再测
        plan_parallel = plan.parallel or plan_waves(plan.pairs, bundle_size=self.max_parallel)
        # 注意：单波组（如 qubit 16 条全并行）恰恰最该走 run_waves，不能要求 wave_count > 1
        if self.max_parallel > 1 and plan_parallel.wave_count >= 1:
            ledger.run_waves(plan_parallel, wait_s=self.wait_s)
        else:
            ledger.run(plan.pairs)             # 串行默认路径：与历史版本逐位一致
        if ledger.aborted:
            return ledger
        self._reconnect(ledger, plan)
        return ledger

    def _reconnect(self, ledger: MeasurementLedger, plan: StagePlan) -> None:
        """坏边把组内图劈得更碎时，用组内最便宜的替补边重新搭桥。"""
        cands = [p for p in pairs_of_group(self.candidates(), plan.name)
                 if p.a in plan.nodes and p.b in plan.nodes]
        self.bridge_failures.extend(ledger.reconnect(
            plan.nodes, cands, expected_components=plan.expected_components,
            max_replacements=self.max_replacements))

    def _final_nodes(self, outcomes: list[StageOutcome]) -> list[str]:
        """末尾（qubit + coupler）全局求解涉及的节点。"""
        nodes: list[str] = []
        seen: set[str] = set()
        for out in outcomes:
            if out.plan.immediate:
                continue
            for n in out.plan.nodes:
                if n not in seen:
                    seen.add(n)
                    nodes.append(n)
        return nodes

    def _final_edges(self, outcomes: list[StageOutcome]) -> list[RelativeEdge]:
        """末尾求解的输入：只有延后组（qubit + coupler）的合格观测。"""
        edges: list[RelativeEdge] = []
        for out in outcomes:
            if out.plan.immediate:
                continue                        # 已写回：旧观测随线条移动而失效
            edges += [m.as_edge() for m in out.ledger.accepted()]
        return edges

    def _write(self, result: SolveResult) -> dict | None:
        try:
            return self.center.commit_solve(result)
        except (WriteGuardError, OSError, ValueError) as exc:
            self.aborted = f"回写守卫拒绝（整批未写）：{exc}"
            return None

    def _with_headroom(self, result: SolveResult) -> SolveResult:
        """读取线组求解后整体再右移 headroom，给后续 qubit/coupler 留出空间。

        右移量同时记入会话 headroom 台账（见 run_stage），步 6 允许原额收回。
        """
        h = self.readout_headroom_ns
        if h <= 0:
            return result
        new_offsets = {ln: round(o + h, 6) for ln, o in result.offsets.items()}
        new_shifts = {ln: round(s + h, 6) for ln, s in result.shifts.items()}
        return SolveResult(
            offsets=new_offsets, shifts=new_shifts,
            residuals=list(result.residuals), target_ns=round(result.target_ns + h, 6),
            max_resid_ns=result.max_resid_ns, rms_resid_ns=result.rms_resid_ns,
            iterations=result.iterations, solver_converged=result.solver_converged,
            request_id=result.request_id,
            reason=f"{result.reason} · 加 readout headroom {h} ns")

    def _solve_readout_final(self, plan: StagePlan, ledger: MeasurementLedger) -> SolveResult:
        """步 6 的收尾求解（整芯片级），并把结果补成 ro/ri 成对。

        步 1 之后一条读取线的 `ro`/`ri` 同步增减，所以本组**每线只测一条边**
        （代表线 `ro`）；这里解完之后再把 `ri` 按同量补进 shifts / headroom_return /
        offsets，两条 delay 条目才能同进同退，读出链不会被写回拆散。
        """
        return self._with_readout_twins(self._readout_final_solve(plan, ledger))

    def _with_readout_twins(self, result: SolveResult) -> SolveResult:
        """把每条读取线代表线（ro）的位移/收回量按同量补给它配对的 ri。"""
        twins = {}
        for ln in list(result.shifts) + list(result.headroom_return) + list(result.offsets):
            if ln.endswith(".ro"):
                ri = ln[:-3] + ".ri"
                if ri in self.session.delays:
                    twins[ri] = ln
        if not twins:
            return result
        offsets, shifts, ret = (dict(result.offsets), dict(result.shifts),
                                dict(result.headroom_return))
        for ri, ro in twins.items():
            offsets.setdefault(ri, offsets.get(ro, 0.0))
            if ro in shifts:
                shifts[ri] = shifts[ro]
            if ro in ret:
                ret[ri] = ret[ro]
        return SolveResult(
            offsets=offsets, shifts=shifts, residuals=list(result.residuals),
            target_ns=result.target_ns, max_resid_ns=result.max_resid_ns,
            rms_resid_ns=result.rms_resid_ns, iterations=result.iterations,
            solver_converged=result.solver_converged, request_id=result.request_id,
            reason=f"{result.reason} · 读取线 ro/ri 同步增减（{len(twins)} 条 ri 随动）",
            headroom_return=ret)

    def _readout_final_solve(self, plan: StagePlan, ledger: MeasurementLedger) -> SolveResult:
        """步 6 收尾求解的原始解：只动每条读取线的代表线，见 _solve_readout_final。

        观测来自 readout ↔ 首腔 qubit.xy 的实测 skew——全流程里唯一把"读取线世界"
        和"qubit/coupler 世界"连起来的桥；qubit/coupler 内部用步 5 的结果作"已对齐"
        软约束（skew=0，σ 取步 5 残差）。锚 = qubit/coupler 最右的线（步 5 的目标）：

        * 加了 headroom：读取线被步 2 主动推到锚线右侧 → 解出的负向位移以
          **收回 headroom** 的方式落盘，读取线往左拉回对齐（收回量受会话
          授予台账约束，d 不会低于加 headroom 之前的校准基线）；
        * 没加 headroom：读取线本来就在锚线左边 → 照旧只向右拉。

        没跑过 finish()（直接调 run_stage 的场景）或整图不连通时，退化为只把
        readout 向右拉。
        """
        edges = [m.as_edge() for m in ledger.accepted()]
        fin = self._final_solve
        if fin is not None and fin.shifts and self._deferred_nodes:
            sigma = max(fin.rms_resid_ns, 1e-3)
            hub = fin.anchor
            for ln in self._deferred_nodes:
                if ln != hub:
                    edges.append(RelativeEdge(a=hub, b=ln, skew_ns=0.0, sigma_ns=sigma,
                                              request_id=f"{self.id_prefix}-final-soft"))
            nodes = list(dict.fromkeys(list(plan.nodes) + list(self._deferred_nodes)))
            if len(components(nodes, [(e.a, e.b) for e in edges])) == 1:
                result = solve(nodes, edges, request_id=f"{self.id_prefix}-{plan.name}-solve",
                               max_iter=self.max_iter)
                return self._pull_back_headroom(result, plan)
        # 退路（旧行为）：不移动 qubit，只把 readout 向右拉到其首个腔 qubit.xy 的位置。
        shifts: dict[str, float] = {}
        residuals: list[dict] = []
        for m in ledger.accepted():
            # 候选边 a=readout, b=qubit.xy；skew = q(readout) - q(qubit.xy)
            shift = max(0.0, -m.skew_ns)          # readout 在 qubit 左侧时才右移
            shifts[m.a] = round(max(shifts.get(m.a, 0.0), shift), 6)
            residuals.append({"a": m.a, "b": m.b, "skew_ns": round(float(m.skew_ns), 6),
                              "resid_ns": 0.0, "request_id": m.request_id})
        return SolveResult(
            offsets={ln: 0.0 for m in ledger.accepted() for ln in (m.a, m.b)},
            shifts=shifts, residuals=residuals,
            target_ns=0.0, max_resid_ns=0.0, rms_resid_ns=0.0,
            iterations=0, solver_converged=True,
            request_id=f"{self.id_prefix}-{plan.name}-solve",
            reason="readout_final 直接按首个腔 qubit.xy 对齐（不移动 qubit）")

    def _pull_back_headroom(self, result: SolveResult, plan: StagePlan) -> SolveResult:
        """把收尾解的锚从"全局最右"改到"qubit/coupler 最右"，读取线往左拉回。

        solve() 只会产出 δ≥0 的 shifts（锚 = 全局最右的线，headroom 场景即
        读取线自己）。步 6 的语义是读取线**退回**步 2 主动右移的 headroom：
        把所有位移整体平移 −C（C = 读取线锚与 qubit/coupler 锚之间的距离），
        读取线的负向部分改记为 headroom_return——逐线受会话授予余额约束
        （ro/ri 的同量关系由 _with_readout_twins 保证，这里不必再配对）。
        """
        deferred_off = [result.offsets[n] for n in self._deferred_nodes if n in result.offsets]
        if not self.session.headroom or not deferred_off:
            return result
        c = result.target_ns - max(deferred_off)      # 新锚 = deferred 最右线
        if c <= 1e-9:
            return result                             # 读取线并不在最右：无须收回

        # 期望净位移 = 旧 shifts − C；负的部分走 headroom_return，但不超过授予余额
        want = {ln: result.shifts.get(ln, 0.0) - c for ln in result.shifts}
        ret: dict[str, float] = {}
        for ln, net in want.items():
            give = round(min(max(0.0, -net), self.session.headroom.get(ln, 0.0)), 6)
            if give > 0:
                ret[ln] = give
        shifts = {ln: round(v, 6) for ln, v in want.items() if v > 0 and ln not in ret}
        if not ret:
            return result                             # 没有可收回的量：维持 δ≥0 解
        total = round(sum(ret.values()), 6)
        return SolveResult(
            offsets=result.offsets, shifts=shifts, residuals=list(result.residuals),
            target_ns=round(result.target_ns - c, 6),
            max_resid_ns=result.max_resid_ns, rms_resid_ns=result.rms_resid_ns,
            iterations=result.iterations, solver_converged=result.solver_converged,
            request_id=result.request_id,
            reason=f"{result.reason} · 锚改取 qubit/coupler 最右线，"
                   f"读取线收回 headroom {total:.1f} ns 往左拉回对齐",
            headroom_return=ret)

    def _apply(self, result: SolveResult, receipt: dict | None) -> None:
        """推进会话，并让设备跟随。

        只有真的落了盘才刷新指纹（CAS 基线）；但设备侧必须**始终**跟着走——
        dry-run 也不例外：组 2 / 组 3 的实验站在组 1 的结果上，设备若停在旧状态，
        后面测到的 skew 就是另一个世界的数，末尾统一求解会把它们解歪。
        """
        self.session.apply_plan(result.plan())          # δ 恒 ≥ 0（headroom 收回除外）
        if receipt is not None:
            self.session.refresh_hash()
        self.center.executor.apply(result.plan())

    # ---------------- 报告 ----------------

    def report(self, outcomes: list[StageOutcome], result: SolveResult,
               receipt: dict | None, truth: dict[str, float] | None = None,
               nodes: list[str] | None = None) -> dict:
        nodes = nodes if nodes is not None else self.lines()
        isolated = sorted(set(nodes) - set(result.offsets))
        residual = position_residual(truth, self.session.delays) if truth else None
        tolerable = residual is None or residual["residual_ns"] <= self.center.th.tol_ns
        converged = not isolated and bool(result.offsets) and result.solver_converged
        return {
            "chip": self.session.chip_id,
            "lines": len(self.lines()),
            "stages": [o.to_dict() for o in outcomes],
            "tree": [_pair_dict(p, o.plan.name) for o in outcomes for p in o.plan.pairs],
            "measurements": [m.to_dict() for o in outcomes for m in o.ledger.records],
            "failed": [m.to_dict() for o in outcomes for m in o.ledger.failed()],
            "bridge_failures": self.bridge_failures,
            "isolated": isolated,
            "solve": result.to_dict(),
            "target_ns": result.target_ns,
            "anchor": result.anchor,
            "plan_ns": round(sum(result.shifts.values()), 6),
            "budget_ns": round(sum(self.session.delays.values()), 6),
            "residual": residual,
            "converged": converged,
            "aligned": bool(converged and tolerable and not self.aborted),
            "aborted": self.aborted,
            "written": bool(receipt and receipt.get("written")),
            "receipt": receipt,
            "ledger": {"records": sum(len(o.ledger.records) for o in outcomes),
                       "accepted": sum(len(o.ledger.accepted()) for o in outcomes),
                       "failed": sum(len(o.ledger.failed()) for o in outcomes),
                       "requests": sum(len(o.ledger.requests) for o in outcomes),
                       "aborted": next((o.ledger.aborted for o in outcomes
                                        if o.ledger.aborted), None)},
            "review": self.center.stats(),
        }


def run_staged_pipeline(center: ReviewCenter, chip: dict, *,
                        truth: dict[str, float] | None = None,
                        max_replacements: int = 8) -> dict:
    """函数式入口：四组顺序跑完并出报告（truth 只用于离线残差）。"""
    return StagedCalibrationPipeline(chip, center, max_replacements=max_replacements).run(truth)


# ===================== CLI：按组单独驱动 =====================

def _load_json(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _pad(s: str, width: int) -> str:
    """按显示宽度补空格（CJK 占 2 列，否则表格会左右错位）。"""
    wide = sum(2 if ord(ch) > 0x2E80 else 1 for ch in str(s))
    return str(s) + " " * max(1, width - wide)


def _print_status(status: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return
    print(f"芯片 {status['chip']} · 指纹 {status['config_hash']} · "
          f"headroom 配置 {status['readout_headroom_ns']:g} ns"
          f" · 已授予未收回 {round(sum(status['granted_headroom'].values()), 3):g} ns"
          f" · 并行提交粒度 {status['max_parallel']}")
    print(f"{_pad('组', 16)}{_pad('写回', 12)}{_pad('边', 5)}{_pad('状态', 11)}"
          f"{_pad('可跑', 6)}{_pad('并行波', 14)}卡在哪里")
    for name, g in status["groups"].items():
        waves = (g.get("waves") or {})
        wsum = (f"{waves.get('wave_count', 0)} 波"
                f"（{'+'.join(str(len(w['pairs'])) for w in waves.get('waves', [])) or '0'}）")
        print(f"{_pad(name, 16)}{_pad(g['writeback'], 12)}{_pad(g['pairs'], 5)}"
              f"{_pad(g['state'], 11)}{_pad('是' if g['ready'] else '否', 6)}"
              f"{_pad(wsum, 14)}{'、'.join(g['blocked_by']) if g['blocked_by'] else '—'}")
    print(f"步 5 整批写回：{'已生效' if status['final']['applied'] else '未生效'}")
    print(f"下一步可做：{' → '.join(status['todo']) or '（无可做项：已全部生效）'}")

def _print_stage(outcome: StageOutcome, residual: dict | None) -> None:
    plan = outcome.plan
    shifts = {**outcome.solve.shifts} if outcome.solve else {}
    moved = round(sum(shifts.values()), 3)
    print(f"组 {plan.name}（{plan.title}）· 边 {len(plan.pairs)} 条 · "
          f"{plan.wave_summary} · "
          f"合格 {len(outcome.ledger.accepted())} · 落盘 {outcome.written} · "
          f"位移 {moved:g} ns"
          + (f" · 残差 {residual['residual_ns']:.3f} ns" if residual else "")
          + (f" · {outcome.resumed}" if outcome.resumed else "")
          + (f"  ⚠ {outcome.aborted}" if outcome.aborted else ""))


def main() -> None:
    ap = argparse.ArgumentParser(
        description="分组校准：按组单独执行（供上层调度器驱动）或一次跑完")
    ap.add_argument("--chip", required=True, help="芯片配置文件")
    ap.add_argument("--pending", default="runs/wiring-pending.json",
                    help="待测文件；不存在则自动生成全零初始态")
    ap.add_argument("--truth", default=None,
                    help="真实值保留文件（只装载设备与事后对账，校准流程不读）")
    ap.add_argument("--status", action="store_true", help="只报告各组状态与下一步建议，不测量")
    ap.add_argument("--json", action="store_true", help="--status 的输出改为 JSON（供程序读取）")
    ap.add_argument("--stage", default=None,
                    help="只跑某一组：readout | qubit | coupler | readout_final")
    ap.add_argument("--final", action="store_true",
                    help="只跑步 5：延后组观测合并求解 + 整批一次写回")
    ap.add_argument("--headroom", type=float, default=0.0,
                    help="读取线临时余量 ns（默认 0；步 6 按需收回）")
    ap.add_argument("--noise", type=float, default=2.0)
    ap.add_argument("--shots", type=int, default=200)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--tol", type=float, default=0.5)
    ap.add_argument("--d-max", type=float, default=5000.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true", help="只求解不写回")
    ap.add_argument("--parallel", type=int, default=1, metavar="N",
                    help="每条提交束最多几条边一起提交（默认 1 = 逐条；>1 才启用波的并行提交）")
    ap.add_argument("--wait", type=float, default=0.0, metavar="SEC",
                    help="一波结果未齐时最多再等多少秒（异步执行方才用得上）")
    ap.add_argument("--out", default="runs", help="报告输出目录")
    args = ap.parse_args()

    chip = _load_json(args.chip)
    truth_data = _load_json(args.truth) if args.truth else None
    truth = ({e["line"]: float(e["delay"]) for e in truth_data["lines"]}
             if truth_data else {})

    pending = Path(args.pending)
    if not pending.exists():
        from .wiring_gen import generate_wiring, save_wiring
        save_wiring(generate_wiring(chip, kind="pending"), pending)
    delays = read_delays(pending)
    if any(d > 0 for d in delays.values()):
        print(f"待测文件已含非零延时 max(d)={max(delays.values()):.1f} ns → 按文件状态续跑")

    from .review.center import ReviewCenter
    from .review.policies import Thresholds
    from .review.queue_io import DirectExecutor
    from .sim import SimChip, SimExecutor, seed_applied_shifts

    device = SimChip(truth, noise_ns=args.noise, seed=args.seed)
    seeded = seed_applied_shifts(device, delays)   # 设备与待测文件同起点（分批调度的前提）
    if seeded:
        print(f"设备已按待测文件还原 {seeded} 条线的累计位移（续跑基线）")
    if not args.status and not truth:
        raise SystemExit("✗ 测量需要 --truth（真实值保留文件）：模拟设备要从它装载各线的 L；"
                         "只看状态（--status）才允许省略")

    center = ReviewCenter(
        CalibrationSession.from_pending(pending),
        DirectExecutor(SimExecutor(device, seed=args.seed)),
        thresholds=Thresholds(tol_ns=args.tol, d_max_ns=args.d_max),
        pending_path=pending, write=not args.dry_run,
        params={"n_shots": args.shots, "repeats": args.repeats, "noise_ns": args.noise,
                "readout_headroom_ns": args.headroom})
    pipe = StagedCalibrationPipeline(chip, center, readout_headroom_ns=args.headroom,
                                     max_parallel=args.parallel, wait_s=args.wait)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.status:
        status = pipe.stage_status()
        _print_status(status, as_json=args.json)
        if not args.json:
            (out_dir / "stage-status.json").write_text(
                json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return

    if args.final:
        report = pipe.run_final(truth or None)
        (out_dir / "final-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        res = report.get("residual") or {}
        print(f"步 5 整批写回 · 位移 {report['plan_ns']:.1f} ns · "
              f"残差 {res.get('residual_ns', float('nan')):.3f} ns · "
              f"written={report['written']}"
              + (f"  ⚠ {report['aborted']}" if report.get("aborted") else ""))
        print(f"报告 → {out_dir / 'final-report.json'}")
        return

    if args.stage:
        known = [p.name for p in pipe.stage_plans()]
        if args.stage not in known:
            raise SystemExit(f"✗ 未知实验组 {args.stage!r}（应为 {known}）")
        outcome = pipe.run_group(args.stage)
        payload = {"status": pipe.stage_status(), "outcome": outcome.to_dict()}
        (out_dir / f"{args.stage}-report.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        residual = (position_residual(truth, pipe.session.delays)
                    if truth and not outcome.aborted else None)
        _print_stage(outcome, residual)
        print(f"报告 → {out_dir / f'{args.stage}-report.json'}")
        if outcome.aborted:
            raise SystemExit(1)
        return

    # 不给任何单选参数：四组一次跑完（demo / 冒烟用）
    report = pipe.run(truth or None)
    (out_dir / "pipeline-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    res = report.get("residual") or {}
    print(f"线 {report['lines']} 条 · 组 "
          f"{' → '.join(s['name'] for s in report['stages'])} · "
          f"残差 {res.get('residual_ns', float('nan')):.3f} ns · "
          f"aligned={report['aligned']}"
          + (f"  ⚠ {report['aborted']}" if report.get("aborted") else ""))
    print(f"报告 → {out_dir / 'pipeline-report.json'}")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
pipeline.py — 批量校准流水线（测量 → 全局求解 → 一次性写回）

旧流程是"测一对、裁决一对、立刻写回一对"：每步都以上一步的结果为起点，
单步几十 ps 的误差沿链路累加，校到后面越跑越歪，还白白吃掉延时预算。

现在拆成两个互不干扰的阶段：

  阶段 A  测量（只读）
       按代价最小生成树挑出 N−1 条边 → 逐条测相对时序 → 全部记进 MeasurementLedger。
       期间文件、会话位移、设备一律不动，因此所有 skew 描述的是同一时刻的物理状态。
       坏边：先重试；用尽仍不行就用替补边把图重新连通；实在接不上才划为隔离区，
       本轮不参与求解，列进报告 isolated 交给下一轮。
       每条边测完即追加进 pending 旁的测量日志（measurements-audit.jsonl）；
       崩溃重启后按 config_hash 恢复同世界的已测边，只补测剩下的（断点续跑）。

  阶段 B  求解与写回（只写一次）
       把全部观测喂给 solver 做一次加权最小二乘 → 得到每条线的相对位置 →
       目标取当前最右那条线 → 逐线位移 δ_i = max(o) − o_i ≥ 0 →
       一张 WritePlan 走四道守卫，整批一次落盘。

契约与守卫一个都没改，只是调用次数从「每条边一次」变成「整批一次」。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .review import graph
from .review.ledger import MeasurementLedger
from .review.session import CalibrationSession, position_residual
from .review.solver import SolveResult, solve_main_component
from .review.writeback import WriteGuardError, read_delays


_EMPTY_SOLVE = SolveResult(offsets={}, shifts={}, request_id="batch-none", reason="无合格观测")


class CalibrationPipeline:
    """两阶段批量校准。编排器只决定"测谁"与"何时写"，判据都在各自专业模块里。"""

    def __init__(self, chip: dict, center, *, max_replacements: int = 8,
                 max_iter: int | None = None, id_prefix: str = "batch",
                 max_parallel: int = 1, wait_s: float = 0.0):
        self.chip = chip
        self.center = center
        self.session: CalibrationSession = center.session
        self.max_replacements = int(max_replacements)
        self.max_iter = max_iter
        self.id_prefix = id_prefix
        # 并行：1 = 逐条提交（默认，行为与历史版本逐位一致）；>1 = 按波的提交束一起提交。
        # 波只是提交提示：执行方可按顺序跑完一束（hardware_hold 独占时本就如此）。
        self.max_parallel = max(1, int(max_parallel))
        self.wait_s = max(0.0, float(wait_s))
        self.aborted: str | None = None
        self.bridge_failures: list[dict] = []
        self.resumed_edges = 0            # 从测量日志恢复的同版本观测条数（断点续跑）

    # ---------------- 阶段 A：选边 + 测量 ----------------

    def lines(self) -> list[str]:
        return self.session.lines()

    def candidate_pairs(self, lines: list[str] | None = None) -> list[graph.Pair]:
        return graph.candidate_pairs(self.chip, lines if lines is not None else self.lines())

    def plan_waves(self, lines: list[str] | None = None):
        """生成树的并行策略：冲突自由的波（同波内的边不共享线名）。"""
        from .review.parallel import plan_waves
        return plan_waves(self.plan_tree(lines), bundle_size=self.max_parallel)

    def plan_tree(self, lines: list[str] | None = None) -> list[graph.Pair]:
        """最少测量集：连通时恰为 N−1 条边。"""
        nodes = lines if lines is not None else self.lines()
        return graph.minimum_spanning_forest(nodes, self.candidate_pairs(nodes))

    def measure(self, pairs: list[graph.Pair] | None = None) -> MeasurementLedger:
        """测完全部边并完成坏边替补重连。全程不写回。

        断点续跑：先从测量日志恢复与本轮同一版本（config_hash 一致）的合格观测，
        已测过的边直接跳过，只补测剩下的。
        """
        nodes = self.lines()
        tree = pairs if pairs is not None else self.plan_tree(nodes)
        ledger = MeasurementLedger(self.session, self.center.executor, th=self.center.th,
                                   params=self.center.default_params)
        self.resumed_edges = ledger.preload()   # 同版本观测：崩溃前测过的边不再重测
        # 波划分要基于**实际要测的边**（tree 尊重了入参，波不能回头又按默认树规划）
        from .review.parallel import plan_waves as _plan_waves
        waves = _plan_waves(tree, bundle_size=self.max_parallel)
        # 单波组（全部边互不冲突）同样要走 run_waves：整波一次性提交
        if self.max_parallel > 1 and waves.wave_count >= 1:
            ledger.run_waves(waves, wait_s=self.wait_s)
        else:
            ledger.run(tree)                    # 串行默认路径：与历史版本逐位一致
        if ledger.aborted:
            self.aborted = f"测量中止：{ledger.aborted}"
            return ledger
        self._reconnect(ledger, nodes)
        return ledger

    def _reconnect(self, ledger: MeasurementLedger, lines: list[str]) -> None:
        """坏边把图劈开时，用最便宜的替补边重新搭桥，尽量别划出隔离区。"""
        self.bridge_failures.extend(ledger.reconnect(
            lines, self.candidate_pairs(lines), max_replacements=self.max_replacements))
        if ledger.aborted:
            self.aborted = f"测量中止：{ledger.aborted}"

    # ---------------- 阶段 B：求解 + 一次性写回 ----------------

    def solve(self, ledger: MeasurementLedger) -> SolveResult:
        """把合格观测一次性解成逐线位移；隔离区之外的线才参与。"""
        accepted = ledger.accepted()
        return solve_main_component(sorted(self.lines()), [m.as_edge() for m in accepted],
                                    request_id=f"{self.id_prefix}-solve", max_iter=self.max_iter)

    def write_once(self, result: SolveResult) -> dict | None:
        """整批一次落盘：四道守卫缺一不可；任一条线超预算则本批全不写。"""
        try:
            return self.center.commit_solve(result)
        except (WriteGuardError, OSError, ValueError) as exc:
            self.aborted = f"回写守卫拒绝（整批未写）：{exc}"
            return None

    def finish(self, ledger: MeasurementLedger,
               truth: dict[str, float] | None = None) -> dict:
        """阶段 B：求解 → 整批一次落盘 → 写成功才推进会话与设备 → 出报告。

        与 `measure()` 分开调用，便于把"只测不写"的阶段 A 单独演示／单独测试。
        """
        if not ledger.accepted():
            self.aborted = ("测量全失败：无一条边取得合格的相对时序，整批不求解、不写回"
                            + (f"（{ledger.failed()[0].reason}）" if ledger.failed() else ""))
        result = self.solve(ledger) if not self.aborted else _EMPTY_SOLVE
        receipt = None if self.aborted else self.write_once(result)

        if not self.aborted:
            self.session.apply_plan(result.plan())      # 位移恒 ≥ 0（求解已保证）
            if receipt is not None:                     # 真的落了盘才推进指纹与设备
                self.session.refresh_hash()
                self.center.executor.apply(result.plan())
        return self.report(ledger, result, receipt, truth)

    def run(self, truth: dict[str, float] | None = None) -> dict:
        """两阶段一次跑完：阶段 A（measure）+ 阶段 B（finish）。"""
        return self.finish(self.measure(), truth)

    # ---------------- 报告 ----------------

    def report(self, ledger: MeasurementLedger, result: SolveResult,
               receipt: dict | None, truth: dict[str, float] | None = None) -> dict:
        isolated = sorted(set(self.lines()) - set(result.offsets))
        residual = position_residual(truth, self.session.delays) if truth else None
        converged = not isolated and bool(result.offsets) and result.solver_converged
        tol = self.center.th.tol_ns
        return {
            "chip": self.session.chip_id,
            "lines": len(self.lines()),
            "tree": [p.to_dict() for p in self.plan_tree()],
            "waves": self.plan_waves().to_dict(),
            "resumed_edges": self.resumed_edges,
            "measurements": [m.to_dict() for m in ledger.records],
            "failed": [m.to_dict() for m in ledger.failed()],
            "bridge_failures": self.bridge_failures,
            "isolated": isolated,
            "solve": result.to_dict(),
            "target_ns": result.target_ns,
            "anchor": result.anchor,
            "plan_ns": round(sum(result.shifts.values()), 6),
            "budget_ns": round(sum(self.session.delays.values()), 6),
            "residual": residual,
            "converged": converged,
            "aligned": bool(converged and not self.aborted and (residual is None
                                           or residual["residual_ns"] <= tol)),
            "aborted": self.aborted,
            "written": bool(receipt and receipt.get("written")),
            "receipt": receipt,
            "ledger": ledger.stats(),
            "review": self.center.stats(),
        }


def run_pipeline(center, chip: dict, *, truth: dict[str, float] | None = None,
                 max_replacements: int = 8, max_parallel: int = 1) -> dict:
    """函数式入口：跑完两阶段并出报告（truth 只用于离线残差，校准流程不读）。"""
    return CalibrationPipeline(chip, center, max_replacements=max_replacements,
                               max_parallel=max_parallel).run(truth)


def main() -> None:
    ap = argparse.ArgumentParser(description="批量校准：一次性测量相对时序 → 一次性写回")
    ap.add_argument("--chip", required=True, help="芯片配置文件")
    ap.add_argument("--truth", required=True, help="真实值保留文件（line + delay = L）")
    ap.add_argument("--pending", default="runs/wiring-pending.json",
                    help="待测文件；不存在则自动生成全零初始态")
    ap.add_argument("--noise", type=float, default=2.0, help="单次测量噪声 ns")
    ap.add_argument("--shots", type=int, default=200)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--tol", type=float, default=0.5)
    ap.add_argument("--d-max", type=float, default=5000.0, help="逐线累计右移预算 ns")
    ap.add_argument("--replacements", type=int, default=8, help="坏边替补重连最多尝试次数")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true", help="只求解不写回")
    ap.add_argument("--parallel", type=int, default=1, metavar="N",
                    help="每条提交束最多几条边一起提交（默认 1 = 逐条；>1 才启用波的并行提交）")
    ap.add_argument("--wait", type=float, default=0.0, metavar="SEC",
                    help="一波结果未齐时最多再等多少秒（异步执行方才用得上）")
    ap.add_argument("--out", default="runs/pipeline-report.json")
    args = ap.parse_args()

    chip = json.loads(Path(args.chip).read_text(encoding="utf-8"))
    truth_data = json.loads(Path(args.truth).read_text(encoding="utf-8"))
    truth = {e["line"]: float(e["delay"]) for e in truth_data["lines"]}

    pending = Path(args.pending)
    if not pending.exists():
        from .wiring_gen import generate_wiring, save_wiring
        save_wiring(generate_wiring(chip, kind="pending"), pending)
    delays = read_delays(pending)
    if any(d > 0 for d in delays.values()):
        print(f"待测文件已含非零延时 max(d)={max(delays.values()):.1f} ns → 按文件状态续跑"
              f"（设备同步还原，已对齐的线只会测到 ≈0 的 skew、不再写回）")

    from .review.center import ReviewCenter
    from .review.policies import Thresholds
    from .review.queue_io import DirectExecutor
    from .review.session import CalibrationSession
    from .sim import SimChip, SimExecutor, seed_applied_shifts

    device = SimChip(truth, noise_ns=args.noise, seed=args.seed)
    seed_applied_shifts(device, delays)      # 设备与待测文件同起点，复跑不会叠加
    center = ReviewCenter(
        CalibrationSession.from_pending(pending),
        DirectExecutor(SimExecutor(device, seed=args.seed)),
        thresholds=Thresholds(tol_ns=args.tol, d_max_ns=args.d_max),
        params={"n_shots": args.shots, "repeats": args.repeats, "noise_ns": args.noise},
        write=not args.dry_run)
    report = run_pipeline(center, chip, truth=truth, max_replacements=args.replacements,
                          max_parallel=args.parallel)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    center.session.save(out.parent / "session.json")

    res = report["residual"] or {}
    solveinfo = report["solve"]
    print(f"线 {report['lines']} 条 · 测量 {report['ledger']['records']} 次"
          f"（生成树 {len(report['tree'])} 条，含替补"
          + (f"，日志恢复 {report['resumed_edges']} 条" if report['resumed_edges'] else "")
          + f"）· 目标锚线 {report['anchor']} · "
          f"总位移 {report['plan_ns']:.1f} ns · "
          f"残差 max(q)-min(q)={res.get('residual_ns', float('nan')):.3f} ns · "
          f"aligned={report['aligned']}")
    print(f"求解 {solveinfo['iterations']} 次迭代 · 观测最大残差 "
          f"{solveinfo['max_resid_ns']:.3f} ns · 失败边 {len(report['failed'])} · "
          f"隔离区 {len(report['isolated'])} · written={report['written']}")
    print(f"待测文件 → {pending} 报告 → {out}"
          + (f"  ⚠ {report['aborted']}" if report["aborted"] else ""))


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""分组校准（读取线 → 单比特 XY-Z → 比特间/耦合器）的顺序与依赖测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qxtrl_timing_cali import (CalibrationSession, DirectExecutor, ReviewCenter, SimChip,
                               SimExecutor, Thresholds, generate_chip, generate_wiring,
                               line_names, position_residual, save_wiring)

from qxtrl_timing_cali import StagedCalibrationPipeline, plan_stages
from qxtrl_timing_cali.review.solver import RelativeEdge, solve_components


def _chip():
    return generate_chip(num_qubits=4, topology="grid", shape=(2, 2), cavities_per_line=2,
                         tunable=True, seed=7)


def _truth(chip):
    return {e["line"]: float(e["delay"]) for e in generate_wiring(
        chip, kind="truth", seed=9, delay_lo=40.0, delay_hi=200.0)["lines"]}


class TestStagePlans(unittest.TestCase):
    """计划阶段：顺序、依赖、写回方式、边是否各归其组。"""

    def setUp(self):
        self.chip = _chip()
        self.lines = line_names(self.chip)
        self.plans = plan_stages(self.chip, self.lines)

    def test_order_and_writeback_policy(self):
        self.assertEqual([p.name for p in self.plans],
                         ["readout", "qubit", "coupler", "readout_final"])
        self.assertEqual(self.plans[0].writeback, "immediate")
        self.assertEqual([p.writeback for p in self.plans[1:]],
                         ["deferred", "deferred", "immediate"])

    def test_dependencies_are_physical(self):
        self.assertEqual(self.plans[0].depends_on, ())
        self.assertEqual(self.plans[1].depends_on, ("readout",))      # 单比特靠读取基准
        self.assertEqual(self.plans[2].depends_on, ("qubit",))        # 耦合实验靠单比特门
        self.assertEqual(self.plans[3].depends_on, ("coupler",))      # final 等 qubit/coupler 全局对齐后

    def test_readout_group_covers_every_ro_ri(self):
        ro = self.plans[0]
        self.assertEqual(sorted(ro.nodes),
                         sorted(n for n in self.lines if n.split(".")[0].startswith("RL")))
        self.assertEqual(len(ro.pairs), len(self.chip["readout_lines"]))   # 每条读取线一条
        self.assertTrue(all({p.a.split(".")[1], p.b.split(".")[1]} == {"ro", "ri"}
                            for p in ro.pairs))
        # 各条读取线之间暂无共同基准 → 分量数 = 读取线条数
        self.assertEqual(ro.expected_components, len(self.chip["readout_lines"]))

    def test_qubit_group_only_does_xy_z(self):
        qb = self.plans[1]
        self.assertTrue(all(n.endswith((".xy", ".z")) for n in qb.nodes))
        self.assertEqual(len(qb.pairs), len(self.chip["qubits"]))      # 每比特一条 xy-z
        for p in qb.pairs:
            self.assertTrue(p.a.split(".")[0] == p.b.split(".")[0])  # 同比特内部

    def test_coupler_group_uses_z_z_and_coupler_z(self):
        cp = self.plans[2]
        # 节点里既有比特 xy/z，也有耦合器 z
        self.assertTrue(any(n.endswith(".z") and n.startswith("Q") for n in cp.nodes))
        self.assertTrue(any(n.startswith("CP") and n.endswith(".z") for n in cp.nodes))
        self.assertTrue(all(".xy" not in p.a or ".xy" not in p.b for p in cp.pairs))
        # 耦合器图应连通为一个分量
        self.assertEqual(cp.expected_components, 1)

    def test_readout_final_hangs_on_first_cavity_qubit(self):
        fin = self.plans[3]
        self.assertTrue(all(n.split(".")[0].startswith("RL") or n.endswith(".xy")
                            for n in fin.nodes))
        # 步 1 之后 ro/ri 同步增减 ⇒ final 组每线只测一条边（代表线 ro），
        # ri 不占实验、只在解里随 ro 同量补齐
        self.assertEqual(len(fin.pairs), len(self.chip["readout_lines"]))
        self.assertTrue(all(p.a.endswith(".ro") and p.b.endswith(".xy") for p in fin.pairs))
        for line in self.chip["readout_lines"]:
            lid = line["id"]
            first = next(c["qubit"] for c in self.chip["cavities"] if c["line"] == lid)
            self.assertIn(f"{lid}.ro", fin.nodes)
            self.assertNotIn(f"{lid}.ri", fin.nodes, "ri 不单独测量")
            self.assertIn(f"{first}.xy", fin.nodes)


class _StagedCase(unittest.TestCase):
    def setUp(self):
        self.chip = _chip()
        self.truth = _truth(self.chip)
        self.work = Path(tempfile.mkdtemp(prefix="qxtrl-stages-"))
        self.pending = self.work / "pending.json"
        save_wiring(generate_wiring(self.chip, kind="pending"), self.pending)
        self.th = Thresholds(tol_ns=0.5, sigma_max_ns=1.0, quality_min=0.6,
                            skew_max_ns=1500.0, d_max_ns=1000.0, max_attempts=2)

    def center(self, *, write=True, force=None, seed=5, pending=None):
        device = SimChip(self.truth, noise_ns=1.0, seed=seed)
        if pending is not None:                     # 设备从该待测文件的当前状态起步
            for e in json.loads(Path(pending).read_text(encoding="utf-8"))["lines"]:
                if e["delay"] > 0:
                    device.apply_shift(e["line"], e["delay"])
        return ReviewCenter(
            CalibrationSession.from_pending(pending or self.pending),
            DirectExecutor(SimExecutor(device, seed=seed, force=force)),
            thresholds=self.th, pending_path=pending or self.pending, write=write,
            params={"n_shots": 120, "repeats": 2, "noise_ns": 1.0})

    def on_disk(self, pending=None):
        return {e["line"]: float(e["delay"])
                for e in json.loads(Path(pending or self.pending)
                                    .read_text(encoding="utf-8"))["lines"]}


class TestStagedRun(_StagedCase):
    def test_stage_one_writes_back_immediately(self):
        pipe = StagedCalibrationPipeline(self.chip, self.center())
        plans = {p.name: p for p in pipe.stage_plans()}
        before = self.on_disk()

        ro = pipe.run_stage(plans["readout"])
        moved = {k for k, v in self.on_disk().items() if v > before[k] + 1e-9}

        self.assertFalse(ro.aborted)
        self.assertTrue(ro.written, "读取线组必须即时落盘，否则后续实验没有读取基准")
        self.assertTrue(moved, "磁盘上应当已经能看到组 1 的右移")
        self.assertTrue(all(k.split(".")[0].startswith("RL") for k in moved))

    def test_deferred_stages_leave_disk_untouched(self):
        pipe = StagedCalibrationPipeline(self.chip, self.center())
        plans = {p.name: p for p in pipe.stage_plans()}
        pipe.run_stage(plans["readout"])
        snapshot = self.on_disk()

        qb = pipe.run_stage(plans["qubit"])
        after_qb = self.on_disk()
        cp = pipe.run_stage(plans["coupler"])

        self.assertIsNone(qb.receipt)
        self.assertIsNone(cp.receipt)
        self.assertEqual(after_qb, snapshot, "单比特组只记录，不应落盘")
        self.assertEqual(self.on_disk(), snapshot, "耦合器组只记录，不应落盘")

    def test_end_to_end_aligns_every_line(self):
        report = StagedCalibrationPipeline(self.chip, self.center()).run(self.truth)
        self.assertIsNone(report["aborted"], report["aborted"])
        self.assertTrue(report["written"])
        self.assertEqual(report["isolated"], [])
        self.assertEqual([s["name"] for s in report["stages"]],
                         ["readout", "qubit", "coupler", "readout_final"])
        self.assertEqual([s["written"] for s in report["stages"]],
                         [True, False, False, True])
        res = position_residual(self.truth, self.on_disk())
        self.assertLess(res["residual_ns"], self.th.tol_ns)
        self.assertLess(max(self.on_disk().values()), self.th.d_max_ns)

    def test_dry_run_never_touches_disk(self):
        report = StagedCalibrationPipeline(self.chip, self.center(write=False)).run(self.truth)
        self.assertFalse(report["written"])
        self.assertTrue(all(v == 0.0 for v in self.on_disk().values()), "dry-run 不得写盘")
        # 未落盘也要保持"设备跟随会话"，否则组 2/3 的数据会来自另一个世界
        self.assertIsNone(report["aborted"])
        self.assertLess(report["residual"]["residual_ns"], self.th.tol_ns)

    def test_missing_readout_baseline_stops_later_stages(self):
        pipe = StagedCalibrationPipeline(self.chip, self.center(seed=9, force={"quality": 0.1}))
        report = pipe.run(self.truth)
        self.assertTrue(report["aborted"], "读取线组失败应中止整轮")
        self.assertEqual([s["name"] for s in report["stages"]], ["readout"])
        self.assertFalse(report["written"])
        self.assertTrue(all(v == 0.0 for v in self.on_disk().values()))

    def test_readout_headroom_is_applied(self):
        # 把 d_max 放宽，避免 headroom 触发预算守卫
        th = Thresholds(tol_ns=0.5, sigma_max_ns=1.0, quality_min=0.6,
                        skew_max_ns=1500.0, d_max_ns=3000.0, max_attempts=2)
        center = ReviewCenter(
            CalibrationSession.from_pending(self.pending),
            DirectExecutor(SimExecutor(SimChip(self.truth, noise_ns=1.0, seed=5), seed=5)),
            thresholds=th, pending_path=self.pending, write=True,
            params={"n_shots": 120, "repeats": 2, "noise_ns": 1.0})
        pipe = StagedCalibrationPipeline(self.chip, center, readout_headroom_ns=1000.0)
        plans = {p.name: p for p in pipe.stage_plans()}
        ro = pipe.run_stage(plans["readout"])
        self.assertTrue(ro.written)
        self.assertTrue(ro.solve)
        # headroom 体现在所有读取线的 shift 都额外增加了 1000 ns
        for ln, d in self.on_disk().items():
            if ln.split(".")[0].startswith("RL"):
                self.assertGreaterEqual(d, 1000.0 - 1e-6, f"{ln} 未加上 headroom")

    def test_headroom_is_pulled_back_by_final_align(self):
        """headroom 右移后，步 6 必须把它收回：读取线往左拉回对齐，仍 aligned=True。

        回归背景：步 5 全局解只覆盖 qubit/coupler、步 6 若只把 readout 向右拉，
        headroom 永远收不回来（残差 ≈ headroom，aligned=False）。收回走
        headroom_return 通道，受会话授予余额约束，d 不低于校准基线。
        """
        th = Thresholds(tol_ns=0.5, sigma_max_ns=1.0, quality_min=0.6,
                        skew_max_ns=1500.0, d_max_ns=3000.0, max_attempts=2)
        center = ReviewCenter(
            CalibrationSession.from_pending(self.pending),
            DirectExecutor(SimExecutor(SimChip(self.truth, noise_ns=1.0, seed=5), seed=5)),
            thresholds=th, pending_path=self.pending, write=True,
            params={"n_shots": 120, "repeats": 2, "noise_ns": 1.0})
        pipe = StagedCalibrationPipeline(self.chip, center, readout_headroom_ns=1000.0)
        report = pipe.run(self.truth)
        self.assertIsNone(report["aborted"], report["aborted"])
        self.assertTrue(report["aligned"])
        disk = self.on_disk()
        res = position_residual(self.truth, disk)
        self.assertLess(res["residual_ns"], th.tol_ns)
        rl = {k: v for k, v in disk.items() if k.split(".")[0].startswith("RL")}
        # 读取线把 headroom 收回了：最终 d 远低于步 2 的右移水平（≥1000）
        self.assertLess(max(rl.values()), 1000.0)
        # 收回走 headroom_return 通道：授予余额被消耗，shifts 本身没有负数
        fin = next(s for s in report["stages"] if s["name"] == "readout_final")
        self.assertTrue(fin["solve"]["headroom_return"])
        self.assertTrue(all(v >= 0 for v in fin["solve"]["shifts"].values()))
        self.assertTrue(all(v < 1000.0 for v in center.session.headroom.values()),
                        "授予余额应被收回部分消耗")


class TestSingleStageDriving(_StagedCase):
    """分次驱动（上层调度器每次只推进一步）：语义、依赖、断点续跑。"""

    def _pipe(self, *, pending=None, headroom=0.0, write=True):
        return StagedCalibrationPipeline(self.chip, self.center(pending=pending, write=write),
                                         readout_headroom_ns=headroom)

    def _fresh_pending(self, name):
        p = self.work / name
        save_wiring(generate_wiring(self.chip, kind="pending"), p)
        return p

    def test_status_only_readout_is_runnable_first(self):
        st = self._pipe().stage_status()
        self.assertEqual(list(st["groups"]),
                         ["readout", "qubit", "coupler", "readout_final"])
        self.assertTrue(st["groups"]["readout"]["ready"])
        self.assertEqual(st["groups"]["readout"]["state"], "pending")
        self.assertFalse(st["groups"]["qubit"]["ready"])
        self.assertIn("readout", st["groups"]["qubit"]["blocked_by"])
        self.assertFalse(st["groups"]["coupler"]["ready"])
        self.assertIn("qubit", st["groups"]["coupler"]["blocked_by"])
        # final 组同时卡在"步 5 未生效"
        self.assertFalse(st["groups"]["readout_final"]["ready"])
        self.assertTrue(any("final" in b for b in st["groups"]["readout_final"]["blocked_by"]))
        self.assertEqual(st["todo"], ["readout"])
        self.assertFalse(st["final"]["applied"])
        # 纯读：一次 status 不该动磁盘
        self.assertTrue(all(v == 0.0 for v in self.on_disk().values()))

    def test_blocked_group_is_refused_without_touching_disk(self):
        pipe = self._pipe()
        out = pipe.run_group("coupler")
        self.assertTrue(out.aborted)
        self.assertIn("qubit", out.aborted)
        self.assertFalse(out.written)
        self.assertEqual(out.ledger.accepted(), [])
        self.assertTrue(all(v == 0.0 for v in self.on_disk().values()),
                        "拒绝执行时不许写一个字节")

    def test_unknown_group_name_raises(self):
        with self.assertRaises(KeyError):
            self._pipe().run_group("nope")

    def test_group_by_group_equals_one_shot(self):
        """每次只推进一步（且每次新建 pipeline，模拟调度器多进程驱动）≡ 一次跑完。"""
        reference = self._pipe(write=True).run(self.truth)
        self.assertTrue(reference["aligned"], reference["aborted"])

        pending = self._fresh_pending("split-pending.json")
        for name in ("readout", "qubit", "coupler"):
            pipe = self._pipe(pending=pending, headroom=50.0)
            self.assertIn(name, pipe.stage_status()["todo"])
            out = pipe.run_group(name)
            self.assertFalse(out.aborted, f"{name}: {out.aborted}")
            # 延后组绝不落盘
            self.assertEqual(out.written, name == "readout")
        after_step2 = {k: v for k, v in self.on_disk(pending).items()
                       if k.split(".")[0].startswith("RL")}
        # 步 5：从测量日志重建观测，整批一次写回
        report = self._pipe(pending=pending, headroom=50.0).run_final(self.truth)
        self.assertIsNone(report["aborted"])
        self.assertTrue(report["written"])
        # 步 6：又一个新进程，仍能收回 headroom 并拉回对齐
        fin = self._pipe(pending=pending, headroom=50.0).run_group("readout_final")
        self.assertIsNone(fin.aborted, fin.aborted)
        self.assertTrue(fin.written)
        self.assertTrue(fin.solve.headroom_return, "headroom 必须被收回")
        self.assertTrue(all(v >= 0 for v in fin.solve.shifts.values()))

        res = position_residual(self.truth, self.on_disk(pending))
        self.assertLess(res["residual_ns"], self.th.tol_ns)
        self.assertTrue(reference["aligned"])
        # headroom 确实被收回：读取线的 d 低于步 2 右移后的水平（回到校准基线）
        rl = {k: v for k, v in self.on_disk(pending).items()
              if k.split(".")[0].startswith("RL")}
        self.assertTrue(rl)
        for ln, d in rl.items():
            self.assertLess(d, after_step2[ln] + 1e-9,
                            f"{ln} 未把 headroom 收回来（{d} ≥ {after_step2[ln]}）")

    def test_applied_group_is_skipped_on_next_invocation(self):
        self._pipe().run_group("readout")
        disk_after_first = self.on_disk()
        pipe = StagedCalibrationPipeline(self.chip, self.center())
        out = pipe.run_group("readout")
        self.assertTrue(out.resumed, "已随写回生效的组应直接跳过")
        self.assertFalse(out.written)
        self.assertEqual(out.ledger.records, out.ledger.records)      # 留痕来自日志
        self.assertTrue(out.ledger.records)
        self.assertEqual(self.on_disk(), disk_after_first, "跳过时不许再写")

    def test_measured_deferred_group_is_not_re_measured(self):
        pending = self._fresh_pending("deferred.json")
        self._pipe(pending=pending).run_group("readout")
        first = self._pipe(pending=pending).run_group("qubit")
        self.assertEqual(len(first.ledger.records), 4)      # 4 条 xy↔z 边
        # 同版本下重跑同一延后组：观测从日志恢复，不再消耗实验次数
        pipe = StagedCalibrationPipeline(self.chip, self.center(pending=pending))
        st = pipe.stage_status()
        self.assertEqual(st["groups"]["qubit"]["state"], "measured")
        self.assertNotIn("qubit", st["todo"])
        again = pipe.run_group("qubit")
        self.assertEqual(len(again.ledger.records), 4)
        self.assertEqual(len(pipe.center.executor.target.requests),
                         len(pipe.center.executor.target.requests))    # 未新增申请
        self.assertFalse(again.written)

    def test_run_final_without_measurements_aborts_quietly(self):
        report = self._pipe().run_final(self.truth)
        self.assertTrue(report["aborted"])
        self.assertFalse(report["written"])
        self.assertTrue(all(v == 0.0 for v in self.on_disk().values()))

    def test_run_final_twice_writes_once(self):
        pending = self._fresh_pending("twice.json")
        for name in ("readout", "qubit", "coupler"):
            self._pipe(pending=pending).run_group(name)
        first = self._pipe(pending=pending).run_final(self.truth)
        self.assertTrue(first["written"])
        disk = self.on_disk(pending)
        second = self._pipe(pending=pending).run_final(self.truth)
        self.assertFalse(second["written"], "步 5 只写一次，重跑不再落盘")
        self.assertEqual(self.on_disk(pending), disk)


class TestSolveComponents(unittest.TestCase):
    """多分量时各自定标到本分量最右线，δ 恒 ≥ 0。"""

    def test_two_components_are_scaled_independently(self):
        res = solve_components(
            ["A", "B", "C", "D"],
            [RelativeEdge("A", "B", -30.0, 0.1), RelativeEdge("C", "D", 10.0, 0.1)])
        self.assertEqual(res.shifts["A"], 30.0)     # A 在左 → 右移 30
        self.assertEqual(res.shifts["B"], 0.0)
        self.assertEqual(res.shifts["D"], 10.0)     # D 在左 → 右移 10（另一个分量）
        self.assertEqual(res.shifts["C"], 0.0)
        self.assertTrue(all(v >= 0 for v in res.shifts.values()))


if __name__ == "__main__":
    unittest.main(verbosity=2)

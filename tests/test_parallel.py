# -*- coding: utf-8 -*-
"""并行策略（parallel.py）：冲突规则、波划分、提交束。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qxtrl_timing_cali import (CalibrationSession, ReviewCenter, SimChip, SimExecutor,
                               Thresholds, generate_chip, generate_wiring, line_names,
                               save_wiring)
from qxtrl_timing_cali.review.contracts import ExperimentResult
from qxtrl_timing_cali.review.graph import GROUP_QUBIT
from qxtrl_timing_cali.review.ledger import MeasurementLedger
from qxtrl_timing_cali.review.queue_io import ExecutorAdapter
from qxtrl_timing_cali.stages import StagedCalibrationPipeline
from qxtrl_timing_cali.review.graph import (STAGE_GROUPS, Pair, candidate_pairs,
                                            minimum_spanning_forest, pairs_of_group)
from qxtrl_timing_cali.review.parallel import (plan_waves, shares_line, summarize)


def _groups(chip, lines):
    """按组分好的最少测量集（与 plan_stages 同源）。"""
    cands = candidate_pairs(chip, lines)
    out = {}
    for g in STAGE_GROUPS:
        gp = pairs_of_group(cands, g)
        nodes = sorted({n for p in gp for n in (p.a, p.b)})
        out[g] = minimum_spanning_forest(nodes, gp)
    return out


class TestWaves(unittest.TestCase):
    def setUp(self):
        self.chip = generate_chip(num_qubits=4, topology="grid", shape=(2, 2),
                                  cavities_per_line=2, tunable=True, seed=7)
        self.lines = line_names(self.chip)
        self.groups = _groups(self.chip, self.lines)

    def test_shared_line_is_the_only_conflict(self):
        self.assertTrue(shares_line(Pair("Q0.z", "Q1.z"), Pair("CP1.z", "Q0.z")))
        self.assertTrue(shares_line(Pair("Q0.z", "Q0.xy"), Pair("Q0.z", "Q1.z")))
        self.assertFalse(shares_line(Pair("Q0.z", "Q0.xy"), Pair("Q1.z", "Q1.xy")))
        # 同一读取线上的两个比特：线名不共享 ⇒ 不算冲突（多音读取各自解调）
        self.assertFalse(shares_line(Pair("Q0.z", "Q0.xy"), Pair("Q1.z", "Q1.xy")))

    def test_wave_internals_are_conflict_free(self):
        for name, pairs in self.groups.items():
            plan = plan_waves(pairs)
            self.assertEqual(plan.edge_count, len(pairs), f"{name}: 边不能丢")
            for wave in plan.waves:
                for i, p in enumerate(wave.pairs):
                    for q in wave.pairs[i + 1:]:
                        self.assertFalse(shares_line(p, q),
                                         f"{name} 波 {wave.index} 内共享线名：{p.label} / {q.label}")

    def test_wave_count_per_stage(self):
        """单比特与读取线组全并行；耦合器组必须分波（共享 z 线）。"""
        self.assertEqual(plan_waves(self.groups["readout"]).wave_count, 1)
        self.assertEqual(plan_waves(self.groups["qubit"]).wave_count, 1)
        self.assertEqual(plan_waves(self.groups["readout_final"]).wave_count, 1)
        coupler = plan_waves(self.groups["coupler"])
        self.assertGreaterEqual(coupler.wave_count, 2, "共享 Q.z 的边不能同波")

    def test_eight_qubit_chain_waves(self):
        chip = generate_chip(num_qubits=8, topology="chain", seed=42)
        groups = _groups(chip, line_names(chip))
        self.assertEqual(plan_waves(groups["qubit"]).wave_count, 1,
                         "8 个比特的 xy↔z 互不冲突，应能一次全开")
        self.assertEqual(len(plan_waves(groups["qubit"]).waves[0].pairs), 8)
        self.assertGreaterEqual(plan_waves(groups["coupler"]).wave_count, 2)

    def test_cheap_edges_claim_first_wave(self):
        plan = plan_waves([Pair("CP1.z", "Q0.z", cost=3.0), Pair("Q0.z", "Q0.xy", cost=1.0),
                           Pair("Q2.z", "Q3.z", cost=2.0)])
        self.assertEqual([p.label for p in plan.waves[0].pairs],
                         ["Q0.z ↔ Q0.xy", "Q2.z ↔ Q3.z"])

    def test_bundles_respect_bundle_size(self):
        plan = plan_waves(self.groups["qubit"], bundle_size=2)
        bundles = plan.bundles()
        self.assertEqual([len(b) for _, b in bundles], [2, 2])
        self.assertTrue(all(w == 1 for w, _ in bundles), "同一波的束波号相同")

    def test_empty_and_single(self):
        self.assertEqual(plan_waves([]).wave_count, 0)
        self.assertEqual(plan_waves([Pair("A", "B")]).wave_count, 1)

    def test_serialization_round_trips(self):
        plan = plan_waves(self.groups["coupler"], bundle_size=2)
        d = plan.to_dict()
        self.assertEqual(d["wave_count"], plan.wave_count)
        self.assertEqual(d["edge_count"], plan.edge_count)
        self.assertEqual(d["bundle_size"], 2)
        self.assertEqual(sum(len(w["pairs"]) for w in d["waves"]), d["edge_count"])
        self.assertTrue(all("rule" in d and "why" in w for w in d["waves"]))
        self.assertIn("波", summarize(plan))


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ===================== 按波提交（ledger.run_waves + 适配器） =====================

class AsyncAdapter(ExecutorAdapter):
    """提交即挂起、poll 才结算的适配器——验证"整束先提交、再统一收结果"。"""

    def __init__(self, target):
        self.target = target
        self.pending: dict[str, ExperimentRequest] = {}
        self.submit_log: list[str] = []
        self.polls = 0

    def submit(self, request):
        self.submit_log.append(request.id)
        self.pending[request.id] = request

    def poll(self, timeout_s=None):
        self.polls += 1
        out = [self.target.run(req) for req in self.pending.values()]
        self.pending.clear()
        return out

    def inflight(self):
        return list(self.pending)

    def apply(self, plan):
        self.target.apply_plan(plan)       # 落盘的位移必须下发设备


class _WaveCase(unittest.TestCase):
    def setUp(self):
        self.chip = generate_chip(num_qubits=4, topology="grid", shape=(2, 2),
                                  cavities_per_line=2, tunable=True, seed=7)
        self.truth = {e["line"]: float(e["delay"]) for e in generate_wiring(
            self.chip, kind="truth", seed=9, delay_lo=40.0, delay_hi=200.0)["lines"]}
        self.work = Path(tempfile.mkdtemp(prefix="qxtrl-par-"))
        self.pending = self.work / "pending.json"
        save_wiring(generate_wiring(self.chip, kind="pending"), self.pending)
        self.th = Thresholds(tol_ns=0.5, sigma_max_ns=1.0, quality_min=0.6,
                             skew_max_ns=1500.0, d_max_ns=3000.0, max_attempts=2)

    def center(self, adapter, *, write=True, force=None):
        return ReviewCenter(CalibrationSession.from_pending(self.pending), adapter,
                            thresholds=self.th, pending_path=self.pending, write=write,
                            params={"n_shots": 120, "repeats": 2, "noise_ns": 1.0})

    def tree_of(self, group):
        cands = candidate_pairs(self.chip, line_names(self.chip))
        gp = pairs_of_group(cands, group)
        nodes = sorted({n for p in gp for n in (p.a, p.b)})
        return minimum_spanning_forest(nodes, gp)


class TestWaveSubmission(_WaveCase):
    def test_bundle_is_submitted_before_results_are_collected(self):
        adapter = AsyncAdapter(SimExecutor(SimChip(self.truth, noise_ns=1.0, seed=5), seed=5))
        center = self.center(adapter, write=False)
        ledger = MeasurementLedger(center.session, adapter, th=center.th,
                                   params=center.default_params)
        tree = self.tree_of(GROUP_QUBIT)
        records = ledger.run_waves(plan_waves(tree, bundle_size=len(tree)), wait_s=1.0)
        self.assertEqual(len(ledger.accepted()), len(tree), ledger.aborted)
        self.assertEqual(len(adapter.submit_log), len(tree), "每条边都要提交")
        self.assertGreaterEqual(adapter.polls, 1)
        self.assertEqual([m.request_id for m in records], adapter.submit_log,
                         "收集顺序应与提交顺序一致")
        self.assertTrue(all(m.wave == 1 for m in records), "一边一波内 wave 应记为 1")

    def test_sequential_default_path_is_untouched(self):
        """max_parallel=1（默认）走老路：逐条提交、逐条收，波字段为 0。"""
        adapter = AsyncAdapter(SimExecutor(SimChip(self.truth, noise_ns=1.0, seed=5), seed=5))
        center = self.center(adapter, write=False)
        ledger = MeasurementLedger(center.session, adapter, th=center.th,
                                   params=center.default_params)
        tree = self.tree_of(GROUP_QUBIT)
        records = [ledger.measure(p) for p in tree]
        self.assertEqual(len(ledger.accepted()), len(tree))
        self.assertEqual(adapter.polls, len(tree), "串行路径每测一条 poll 一次")
        self.assertTrue(all(m.wave == 0 for m in records))

    def test_bad_edge_retries_and_others_survive(self):
        """束内某条边质量不达标：只重试它，其余边照常合格。"""
        device = SimChip(self.truth, noise_ns=1.0, seed=5)
        inner = SimExecutor(device, seed=5)
        adapter = AsyncAdapter(inner)
        center = self.center(adapter, write=False, force=None)
        center.default_params["flags"] = []
        # 让第一条边永远低分：通过 PoisonExecutor 风格的包装
        class _Once(SimExecutor):
            def run(self, request):
                res = super().run(request)
                if request.targets[0].endswith(".z") and request.note.startswith("Q0"):
                    payload = res.to_dict()
                    payload["quality"] = 0.05
                    return ExperimentResult.from_dict(payload)
                return res
        inner2 = _Once(device, seed=5)
        adapter.target = inner2
        ledger = MeasurementLedger(center.session, adapter, th=center.th,
                                   params=center.default_params)
        tree = self.tree_of(GROUP_QUBIT)
        ledger.run_waves(plan_waves(tree, bundle_size=len(tree)), wait_s=0.5)
        failed = ledger.failed()
        self.assertEqual(len(failed), 1, failed)
        self.assertEqual(len(ledger.accepted()), len(tree) - 1)
        self.assertGreaterEqual(failed[0].attempts, 2, "坏边应重试到用尽")

    def test_hash_drift_aborts_whole_batch(self):
        """测量期间待测文件被改动（config_hash 漂移）：整批作废。"""
        adapter = AsyncAdapter(SimExecutor(SimChip(self.truth, noise_ns=1.0, seed=5), seed=5))
        center = self.center(adapter, write=False)
        ledger = MeasurementLedger(center.session, adapter, th=center.th,
                                   params=center.default_params)

        class _Stale(AsyncAdapter):
            def poll(self, timeout_s=None):
                out = super().poll(timeout_s)
                for res in out:
                    res.config_hash = "stale-hash"
                return out
        stale = _Stale(adapter.target)
        ledger.executor = stale
        ledger.run_waves(plan_waves(self.tree_of(GROUP_QUBIT), bundle_size=4))
        self.assertTrue(ledger.aborted, "指纹漂移必须停手")
        self.assertEqual(ledger.accepted(), [])

    def test_end_to_end_parallel_still_aligns(self):
        """max_parallel>1 跑完整轮：统计上仍应对齐（不要求与串行逐位相同）。"""
        adapter = AsyncAdapter(SimExecutor(SimChip(self.truth, noise_ns=1.0, seed=5), seed=5))
        pipe = StagedCalibrationPipeline(self.chip, self.center(adapter, write=True),
                                         max_parallel=8, wait_s=1.0,
                                         readout_headroom_ns=100.0)
        report = pipe.run(self.truth)
        self.assertIsNone(report["aborted"], report["aborted"])
        self.assertTrue(report["aligned"], report["solve"])
        # 波结构进了报告
        waves = {s["name"]: s["waves"] for s in report["stages"]}
        self.assertEqual(waves["qubit"]["wave_count"], 1)
        self.assertGreaterEqual(waves["coupler"]["wave_count"], 1)
        self.assertTrue(any(m["wave"] > 0 for s in report["stages"]
                            for m in s["measurements"]))

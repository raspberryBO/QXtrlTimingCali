# -*- coding: utf-8 -*-
"""守卫与状态机加固的回归测试（NaN / headroom 台账 / 连通性 / 坏输入健壮性）。

每条测试对应一次真实事故或审计发现：
  * NaN 与任何数比较均为 False，曾可无声穿透全部阈值判据与四道回写守卫落盘；
  * headroom 台账曾按"增量补账"重放——resumed 阶段重复入账（余额翻倍）、
    收回事件不入日志（已收回的余额在新进程里又加回来）；
  * 延后组求解曾对不连通图直接全局解，给从未测过的线写出幻影位移；
  * 校验器 / 文件队列曾在坏输入上抛异常而不是记错误（本职场景失效）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qxtrl_timing_cali import (CalibrationSession, DirectExecutor, ReviewCenter, SimChip,
                               SimExecutor, Thresholds, generate_chip, generate_wiring,
                               save_wiring)
from qxtrl_timing_cali.pipeline import CalibrationPipeline
from qxtrl_timing_cali.review.contracts import ExperimentResult
from qxtrl_timing_cali.review.ledger import MeasurementLedger
from qxtrl_timing_cali.review.parallel import plan_waves
from qxtrl_timing_cali.review.policies import quality_problems
from qxtrl_timing_cali.review.queue_io import FileQueue
from qxtrl_timing_cali.review.writeback import (WriteGuardError, commit_shifts,
                                                content_hash, load_pending)
from qxtrl_timing_cali.stages import StagedCalibrationPipeline, plan_stages
from qxtrl_timing_cali.validate import validate_chip, validate_wiring

NAN = float("nan")


def _chip(**kw):
    return generate_chip(num_qubits=4, topology="grid", shape=(2, 2), cavities_per_line=2,
                         tunable=True, seed=7, **kw)


def _truth(chip, seed=9):
    return {e["line"]: float(e["delay"]) for e in generate_wiring(
        chip, kind="truth", seed=seed, delay_lo=40.0, delay_hi=200.0)["lines"]}


def _result(**over):
    base = dict(request_id="r1", kind="timing.align", config_hash="x" * 16,
                measured_at="2026-09-30T00:00:00", skew_ns=1.0, sigma_ns=0.2, quality=0.9)
    base.update(over)
    return ExperimentResult(**base)


def _pending(work: Path, chip=None):
    p = work / "pending.json"
    save_wiring(generate_wiring(chip or _chip(), kind="pending"), p)
    return p


# ===================== NaN：契约 → 判据 → 守卫 三层都拒收 =====================

class TestNaNRejection(unittest.TestCase):
    def test_from_dict_rejects_nan_main_fields(self):
        for key in ("skew_ns", "sigma_ns", "quality"):
            d = _result().to_dict()
            d[key] = NAN
            with self.assertRaises(ValueError, msg=key):
                ExperimentResult.from_dict(d)

    def test_from_dict_rejects_nan_repeats(self):
        d = _result().to_dict()
        d["repeats"] = [{"skew_ns": NAN}]
        with self.assertRaises(ValueError):
            ExperimentResult.from_dict(d)

    def test_quality_problems_flags_nonfinite(self):
        """直接构造的结果（绕过 from_dict）也要被质量判据拦下。"""
        probs = quality_problems(_result(skew_ns=NAN, sigma_ns=NAN, quality=NAN),
                                 Thresholds())
        self.assertTrue(probs, "NaN 主判据必须算作质量问题")

    def test_quality_problems_flags_nonfinite_repeats(self):
        """直接构造（绕过 from_dict 的拒收）也要被质量判据拦下。"""
        res = _result(repeats=[{"skew_ns": 1.0}, {"skew_ns": NAN}])
        probs = quality_problems(res, Thresholds())
        self.assertTrue(probs)

    def test_commit_shifts_rejects_nan(self):
        """最后一道防线：NaN 位移绝不落盘。"""
        work = Path(tempfile.mkdtemp(prefix="qxtrl-nan-"))
        pend = _pending(work)
        before = pend.read_text(encoding="utf-8")
        h = content_hash(json.loads(before))
        with self.assertRaises(WriteGuardError):
            commit_shifts(pend, {"Q0.z": NAN}, expected_hash=h, d_max_ns=2000.0,
                          audit_path=work / "audit.jsonl")
        self.assertEqual(pend.read_text(encoding="utf-8"), before, "文件必须保持原样")

    def test_write_plan_from_dict_rejects_nan(self):
        from qxtrl_timing_cali.review.contracts import WritePlan
        with self.assertRaises(ValueError):
            WritePlan.from_dict({"request_id": "r", "shifts": {"Q0.z": NAN}})
        with self.assertRaises(ValueError):
            WritePlan.from_dict({"request_id": "r", "headroom_return": {"RL0.ro": NAN}})


# ===================== headroom 台账：跨进程不翻倍、收回要入账 =====================

class TestHeadroomLedger(unittest.TestCase):
    def setUp(self):
        self.chip = _chip()
        self.truth = _truth(self.chip)
        self.work = Path(tempfile.mkdtemp(prefix="qxtrl-hr-"))
        self.pending = _pending(self.work, self.chip)

    def _pipe(self):
        """每个"进程"一份全新对象，共享同一批文件——分阶段跨进程驱动的形态。"""
        device = SimChip(self.truth, noise_ns=1.0, seed=3)
        center = ReviewCenter(
            CalibrationSession.from_pending(self.pending),
            DirectExecutor(SimExecutor(device, seed=3)),
            thresholds=Thresholds(d_max_ns=6000.0), pending_path=self.pending, write=True)
        return StagedCalibrationPipeline(self.chip, center, readout_headroom_ns=100.0)

    def test_resumed_stage_does_not_double_grant(self):
        """stage_status 重放授予后，run_group 再走 resumed 路径不得二次入账。"""
        p1 = self._pipe()
        p1.run_group("readout")
        n_ro = len([l for l in self.truth if l.endswith((".ro", ".ri"))])
        self.assertAlmostEqual(sum(p1.center.session.headroom.values()), 100.0 * n_ro)

        p2 = self._pipe()                       # 新进程：同一批文件
        p2.stage_status()                       # 重放授予
        p2.run_group("readout")                 # 已生效 → resumed 路径
        balance = p2.center.session.headroom
        self.assertTrue(balance)
        for ln, v in balance.items():
            self.assertAlmostEqual(v, 100.0, msg=f"{ln} 授予余额被重复入账：{v}")

    def test_replay_subtracts_returns(self):
        """整轮跑完后，新进程重放的余额必须等于「授予 − 日志里的收回」——
        收回只取对齐所需、不必是全额，残余余额是正常状态；但绝不能把
        已收回的部分重新加回来（旧实现重放成全额授予）。"""
        self._pipe().run(self.truth)
        from qxtrl_timing_cali.review.ledger import load_journal, journal_path_for
        expected: dict[str, float] = {}
        for d in load_journal(journal_path_for(self.pending)):
            if d.get("event") != "stage_done":
                continue
            grant = d.get("headroom") or {}
            amt = float(grant.get("amount_ns", 0.0) or 0.0)
            for ln in grant.get("lines", []):
                expected[str(ln)] = round(expected.get(str(ln), 0.0) + amt, 6)
            for ln, v in (d.get("headroom_return") or {}).items():
                expected[str(ln)] = round(expected.get(str(ln), 0.0) - float(v), 6)
        st = self._pipe().stage_status()
        got = st["granted_headroom"]
        for ln, exp in expected.items():
            self.assertAlmostEqual(got.get(ln, 0.0), max(0.0, exp), places=4,
                                   msg=f"{ln} 重放余额应 = 授予 − 收回")
        # 语义对照：日志里有收回事件时，重放余额不得是全额授予（旧实现的症状）
        if any(v < 0 for v in expected.values()):
            self.assertTrue(any(v < 100.0 - 1e-6 for v in got.values()),
                            "日志里有收回事件，重放余额却仍是全额授予")


# ===================== 连通性：不连通的延后图只解最大分量 =====================

class TestDisconnectedFinalSolve(unittest.TestCase):
    def test_isolated_lines_get_no_phantom_shifts(self):
        """两条独立链（无桥接边）⇒ 延后图天然不连通：只解最大分量，
        其余线进隔离区，绝不给从未测过的线凭空造位移。"""
        chip = generate_chip(num_qubits=4, topology="chains", shape=(2, 2),
                             cavities_per_line=2, tunable=True, seed=5)
        truth = _truth(chip, seed=11)
        work = Path(tempfile.mkdtemp(prefix="qxtrl-disc-"))
        pending = _pending(work, chip)
        device = SimChip(truth, noise_ns=1.0, seed=3)
        center = ReviewCenter(
            CalibrationSession.from_pending(pending),
            DirectExecutor(SimExecutor(device, seed=3)),
            thresholds=Thresholds(d_max_ns=8000.0), pending_path=pending, write=True)
        pipe = StagedCalibrationPipeline(chip, center)

        outcomes = pipe.measure()               # 三组主阶段（不含 final）
        report = pipe.finish(outcomes, truth)
        isolated = report["isolated"]
        self.assertTrue(isolated, "两链无桥接：较小那条链必须进隔离区")
        offsets = report["solve"]["offsets"]
        for ln in isolated:
            self.assertNotIn(ln, offsets, f"{ln} 从未被观测，却在解里拿到了位移")
        self.assertFalse(report["converged"], "有隔离区时不得宣称收敛")


# ===================== 契约与守卫的边角 =====================

class TestContractEdges(unittest.TestCase):
    def test_targets_needs_two_lines(self):
        from qxtrl_timing_cali.review.contracts import ExperimentRequest
        d2 = {"id": "r", "kind": "k", "config_hash": "h", "targets": ["Q0.z"], "params": {}}
        with self.assertRaises(ValueError):
            ExperimentRequest.from_dict(d2)

    def test_load_pending_rejects_missing_lines(self):
        work = Path(tempfile.mkdtemp(prefix="qxtrl-lp-"))
        p = work / "p.json"
        p.write_text(json.dumps({"meta": {"kind": "pending"}}), encoding="utf-8")
        with self.assertRaises(WriteGuardError):
            load_pending(p)

    def test_commit_write_refuses_dry_run_center(self):
        work = Path(tempfile.mkdtemp(prefix="qxtrl-dr-"))
        pend = _pending(work)
        center = ReviewCenter(CalibrationSession.from_pending(pend),
                              DirectExecutor(SimExecutor(SimChip({"Q0.z": 1.0}))),
                              pending_path=pend, write=False)
        from qxtrl_timing_cali.review.contracts import WritePlan
        with self.assertRaises(WriteGuardError):
            center.commit_write(WritePlan(request_id="r", shifts={"Q0.z": 1.0}))


# ===================== 文件队列：半截文件与坏契约不炸不丢 =====================

class TestFileQueueRobustness(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="qxtrl-fq-"))
        self.q = FileQueue(self.work)

    def test_fresh_half_written_file_is_not_archived(self):
        """执行方还在写的半截文件：留在原地等下一轮，不进 rejected。"""
        p = self.q.results / "r-half.json"
        p.write_text('{"request_id": "r-half", "skew', encoding="utf-8")  # 刚落盘
        self.assertEqual(self.q.poll(), [])
        self.assertTrue(p.exists(), "半截文件不该被归档")
        self.assertFalse(list((self.work / "rejected").glob("*")))

    def test_stale_corrupt_file_is_archived(self):
        p = self.q.results / "r-old.json"
        p.write_text("{不是JSON", encoding="utf-8")
        stale = p.stat().st_mtime - 3600
        os.utime(p, (stale, stale))             # 伪装成很久以前就没写完的残骸
        self.assertEqual(self.q.poll(), [])
        self.assertFalse(p.exists())
        self.assertTrue((self.work / "rejected" / "r-old.json").exists())

    def test_type_violating_payload_is_archived_without_crashing(self):
        """契约字段类型错误（如 flags 传数字）抛 TypeError：归档 rejected，不炸 poll。"""
        payload = _result(request_id="r-bad").to_dict()
        payload["flags"] = 5                    # 不是列表
        (self.q.results / "r-bad.json").write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.q.poll(), [])
        self.assertTrue((self.work / "rejected" / "r-bad.json").exists())

    def test_duplicate_delivery_keeps_both_copies(self):
        good = _result(request_id="r-dup")
        self.q.publish_result(good)
        self.q.poll()
        self.q.publish_result(good)             # 执行方重复投递同一 id
        self.q.poll()
        consumed = list((self.work / "consumed").glob("r-dup*"))
        self.assertEqual(len(consumed), 2, "重复投递不该覆盖既有留痕")


# ===================== 校验器：坏输入记错误而非崩溃 =====================

class TestValidatorRobustness(unittest.TestCase):
    def _base_chip(self):
        return {"schema_version": "1.0",
                "meta": {"id": "c1", "tunable": True}, "units": {"delay": "ns"},
                "qubits": [], "cavities": [], "couplers": [], "readout_lines": [],
                "connections": []}

    def assertReports(self, data):
        errs, _ = validate_chip(data)
        self.assertTrue(errs, "坏输入必须被记成错误而不是绕过")
        return errs

    def test_sections_of_wrong_type(self):
        data = self._base_chip()
        data["qubits"] = {"Q0": {}}
        self.assertReports(data)

    def test_non_dict_elements(self):
        data = self._base_chip()
        data["qubits"] = ["Q0", 5]
        self.assertReports(data)

    def test_coupler_with_non_list_qubits(self):
        data = self._base_chip()
        data["couplers"] = [{"id": "CP0", "qubits": 5}]
        self.assertReports(data)

    def test_coupler_referencing_missing_qubit(self):
        data = self._base_chip()
        data["couplers"] = [{"id": "CP0", "qubits": ["QX", "QY"]}]   # 比特不存在
        self.assertReports(data)

    def test_connections_not_a_list(self):
        data = self._base_chip()
        data["connections"] = "oops"
        self.assertReports(data)

    def test_wiring_nan_and_bool_delay(self):
        data = {"schema_version": "1.0", "meta": {"id": "w1", "chip": "c1", "kind": "pending"},
                "lines": [{"line": "Q0.z", "delay": NAN}, {"line": "Q1.z", "delay": True},
                          {"line": "Q2.z"}, "junk"]}
        errs, _ = validate_wiring(data)
        joined = "\n".join(errs)
        self.assertIn("不是有限数值", joined)
        self.assertIn("非数字", joined)
        self.assertIn("非对象条目", joined)

    def test_wiring_lines_not_a_list(self):
        data = {"schema_version": "1.0", "meta": {"id": "w1", "chip": "c1", "kind": "pending"},
                "lines": {"Q0.z": 1.0}}
        self.assertTrue(validate_wiring(data)[0])


# ===================== 设备下发与台账细节 =====================

class TestApplyAndLedgerDetails(unittest.TestCase):
    def test_sim_apply_plan_is_atomic_on_unknown_lines(self):
        chip_dev = SimChip({"Q0.z": 10.0, "Q1.z": 20.0})
        from qxtrl_timing_cali.review.contracts import WritePlan
        plan = WritePlan(request_id="r", shifts={"Q0.z": 5.0, "NOPE.z": 1.0})
        with self.assertRaises(KeyError):
            SimExecutor(chip_dev).apply_plan(plan)
        self.assertEqual(chip_dev.position("Q0.z"), 10.0, "校验失败的计划一个位移都不该下发")

    def test_run_waves_counts_first_attempt(self):
        """波内首试即成功的边，attempts 应为 1（与串行路径一致），不是 0。"""
        work = Path(tempfile.mkdtemp(prefix="qxtrl-att-"))
        chip = _chip()
        pending = _pending(work, chip)
        truth = _truth(chip)
        center = ReviewCenter(
            CalibrationSession.from_pending(pending),
            DirectExecutor(SimExecutor(SimChip(truth, noise_ns=0.5, seed=2), seed=2)),
            thresholds=Thresholds(), pending_path=pending, write=False)
        ledger = MeasurementLedger(center.session, center.executor, th=center.th,
                                   params=center.default_params)
        pairs = plan_stages(chip, center.session.lines())[1].pairs     # qubit 组
        ledger.run_waves(plan_waves(pairs, bundle_size=2))
        self.assertTrue(ledger.accepted())
        for m in ledger.accepted():
            self.assertEqual(m.attempts, 1, f"{m.a}↔{m.b} 首试成功应记 attempts=1")


class TestPipelineRespectsCustomPairs(unittest.TestCase):
    def test_parallel_branch_measures_given_pairs(self):
        """measure(pairs=...) 的入参在并行分支也要生效——不能回头按默认树规划波。"""
        work = Path(tempfile.mkdtemp(prefix="qxtrl-pairs-"))
        chip = _chip()
        pending = _pending(work, chip)
        truth = _truth(chip)
        center = ReviewCenter(
            CalibrationSession.from_pending(pending),
            DirectExecutor(SimExecutor(SimChip(truth, noise_ns=0.5, seed=2), seed=2)),
            thresholds=Thresholds(d_max_ns=8000.0), pending_path=pending, write=False)
        pipe = CalibrationPipeline(chip, center, max_parallel=2)
        tree = pipe.plan_tree()
        tree_keys = {p.key for p in tree}
        extra = next(p for p in pipe.candidate_pairs() if p.key not in tree_keys)
        ledger = pipe.measure(pairs=tree[:-1] + [extra])
        measured = {m.pair.key for m in ledger.records}
        self.assertIn(extra.key, measured, "并行分支丢掉了调用者指定的边")


class TestCompactCore(unittest.TestCase):
    """数值验收、原子簿记与真实批量提交的边界回归。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qxtrl-core-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.chip = _chip()
        self.pending = _pending(self.work, self.chip)

    def center(self, *, write=True, budget=2000.0):
        return ReviewCenter(
            CalibrationSession.from_pending(self.pending),
            DirectExecutor(SimExecutor(SimChip(_truth(self.chip), noise_ns=0.0, seed=5), seed=5)),
            thresholds=Thresholds(d_max_ns=budget), write=write)

    def test_unconverged_batch_never_applies_even_in_dry_run(self):
        for write in (True, False):
            with self.subTest(write=write):
                center = self.center(write=write)
                before = self.pending.read_bytes()
                delays = dict(center.session.delays)
                report = CalibrationPipeline(self.chip, center, max_iter=1).run()
                self.assertFalse(report["solve"]["solver_converged"])
                self.assertFalse(report["aligned"])
                self.assertFalse(report["written"])
                self.assertTrue(report["aborted"])
                self.assertEqual(self.pending.read_bytes(), before)
                self.assertEqual(center.session.delays, delays)

    def test_unconverged_deferred_stage_keeps_measurement_baseline(self):
        center = self.center()
        pipe = StagedCalibrationPipeline(self.chip, center, max_iter=1)
        outcomes = pipe.measure()
        before, delays = self.pending.read_bytes(), dict(center.session.delays)
        report = pipe.finish(outcomes)
        self.assertFalse(report["aligned"])
        self.assertTrue(report["aborted"])
        self.assertEqual(self.pending.read_bytes(), before)
        self.assertEqual(center.session.delays, delays)

    def test_budget_rejection_is_not_alignment(self):
        report = CalibrationPipeline(self.chip, self.center(budget=0)).run()
        self.assertFalse(report["aligned"])
        self.assertFalse(report["written"])
        self.assertTrue(report["aborted"])

    def test_iterator_keeps_inconsistent_cycle_residuals(self):
        from qxtrl_timing_cali.review.solver import RelativeEdge, solve
        edges = [RelativeEdge("A", "B", 1), RelativeEdge("B", "C", 1),
                 RelativeEdge("A", "C", 3)]
        expected = solve(["A", "B", "C"], edges)
        actual = solve(["A", "B", "C"], iter(edges))
        self.assertEqual(actual.to_dict(), expected.to_dict())
        self.assertGreater(actual.max_resid_ns, 0)

    def test_illegal_sigma_and_quality_are_rejected(self):
        for values in ({"sigma_ns": -1}, {"quality": 2}, {"quality": -1}):
            with self.subTest(values=values):
                self.assertTrue(quality_problems(_result(**values), Thresholds()))
                with self.assertRaises(ValueError):
                    ExperimentResult.from_dict(_result(**values).to_dict())

    def test_disconnected_solver_refuses_unrelated_zero_points(self):
        from qxtrl_timing_cali.review.solver import RelativeEdge, solve, solve_main_component
        edges = [RelativeEdge("A", "B", 1)]
        with self.assertRaises(ValueError):
            solve(["A", "B", "C"], edges)
        result = solve_main_component(["A", "B", "C"], edges)
        self.assertEqual(set(result.offsets), {"A", "B"})
        self.assertFalse(solve_main_component(["A"], []).offsets)

    def test_poll_keeps_results_after_requested_result(self):
        center = self.center()
        ledger = MeasurementLedger(center.session, center.executor)
        pair = plan_stages(self.chip, center.session.lines())[1].pairs[0]
        center.session.ensure_block(pair.a, [pair.a])
        center.session.ensure_block(pair.b, [pair.b])
        first = center.session.new_request(pair.kind, pair.a, pair.b)
        second = center.session.new_request(pair.kind, pair.a, pair.b)
        center.executor._inbox = [_result(request_id=first.id), _result(request_id=second.id)]
        self.assertEqual(ledger._take(first).request_id, first.id)
        self.assertEqual(ledger._take(second).request_id, second.id)

    def test_invalid_budget_and_negative_final_delay_never_write(self):
        before = self.pending.read_bytes()
        h = load_pending(self.pending)[1]
        for kwargs in ({"d_max_ns": NAN},
                       {"headroom_return": {"Q0.z": 1}, "granted_headroom": {"Q0.z": 1}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(WriteGuardError):
                commit_shifts(self.pending, {}, expected_hash=h,
                              audit_path=self.work / "audit.jsonl", **kwargs)
            self.assertEqual(self.pending.read_bytes(), before)

    def test_full_staged_report_preserves_isolation(self):
        self.chip = generate_chip(num_qubits=4, topology="chains", shape=(2, 2),
                                  cavities_per_line=2, tunable=True, seed=5)
        self.pending = _pending(self.work, self.chip)
        report = StagedCalibrationPipeline(self.chip, self.center()).run()
        self.assertTrue(report["isolated"])
        self.assertFalse(report["aligned"])
        self.assertFalse(report["converged"])

    def test_invalid_pending_is_never_loaded(self):
        original = json.loads(self.pending.read_text(encoding="utf-8"))
        for bad in (NAN, float("inf"), -1, True):
            data = json.loads(json.dumps(original))
            data["lines"][0]["delay"] = bad
            self.pending.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(WriteGuardError):
                load_pending(self.pending)

    def test_invalid_plan_leaves_entire_session_unchanged(self):
        from qxtrl_timing_cali.review.contracts import WritePlan
        session = CalibrationSession({"A": 10, "B": 20})
        for shifts in ({"A": 1, "unknown": 2}, {"A": 1, "B": NAN}):
            with self.assertRaises(ValueError):
                session.apply_plan(WritePlan("r", shifts=shifts))
            self.assertEqual(session.delays, {"A": 10, "B": 20})
        session.ensure_block("A", ["A"])
        for merge in ({"new": "A", "ref": "A"}, {"new": "A", "ref": "unknown"}):
            with self.assertRaises(ValueError):
                session.apply_plan(WritePlan("r", shifts={"A": 1}, merge=merge))
            self.assertEqual(session.delays, {"A": 10, "B": 20})
            self.assertEqual(session.blocks, {"A": ["A"]})

    def test_wave_uses_executor_batch_entry(self):
        class BatchExecutor(DirectExecutor):
            batches = 0

            def submit_many(self, requests):
                self.batches += 1
                super().submit_many(requests)

        center = self.center()
        executor = BatchExecutor(center.executor.target)
        ledger = MeasurementLedger(center.session, executor)
        pairs = plan_stages(self.chip, center.session.lines())[1].pairs
        ledger.run_waves(plan_waves(pairs, bundle_size=len(pairs)))
        self.assertEqual(executor.batches, 1)
        self.assertEqual(len(ledger.accepted()), len(pairs))


if __name__ == "__main__":
    unittest.main(verbosity=2)

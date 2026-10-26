# -*- coding: utf-8 -*-
"""审查中心端测试：契约 / 会话簿记 / 守卫回写 / 策略裁决 / 队列适配 / 批量闭环。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from qxtrl_timing_cali.chip_gen import generate_chip
from qxtrl_timing_cali.pipeline import CalibrationPipeline
from qxtrl_timing_cali.review import (CalibrationSession, DirectExecutor, FileQueue,
                                     Pair, RelativeEdge, ReviewCenter, SolveResult,
                                     Thresholds, Verdict, WriteGuardError, WritePlan,
                                     candidate_pairs, commit_shifts, components,
                                     content_hash, minimum_spanning_forest,
                                     position_residual, quality_problems, read_delays,
                                     solve)
from qxtrl_timing_cali.review.contracts import ExperimentRequest, ExperimentResult
from qxtrl_timing_cali.review.policies import analyze
from qxtrl_timing_cali.review.writeback import load_pending
from qxtrl_timing_cali.sim import SimChip, SimExecutor, seed_applied_shifts
from qxtrl_timing_cali.validate import validate_wiring
from qxtrl_timing_cali.wiring_gen import generate_wiring, save_wiring


def make_result(req: ExperimentRequest, skew: float, **kw) -> ExperimentResult:
    return ExperimentResult(request_id=req.id, kind=req.kind,
                            config_hash=kw.pop("config_hash", req.config_hash),
                            measured_at="2026-09-20T00:00:00", skew_ns=skew,
                            sigma_ns=kw.pop("sigma_ns", 0.05),
                            quality=kw.pop("quality", 0.95), **kw)


class FakeExecutor:
    """可控执行方：脚本化吐结果（数字=skew；callable=自定义结果）。"""

    def __init__(self, script):
        self.script = list(script)
        self.sent: list[ExperimentRequest] = []

    def run(self, req):
        self.sent.append(req)
        item = self.script.pop(0)
        if callable(item):
            return item(req)
        kw = {k: v for k, v in req.params.items() if k in ("quality", "sigma_ns", "flags")}
        return make_result(req, item, **kw).to_dict()


class Case(unittest.TestCase):
    """公共脚手架：4 比特可调芯片 + truth / pending 文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

        self.chip = generate_chip(num_qubits=4, seed=8)
        self.chip_path = self.root / "chip.json"
        self.chip_path.write_text(json.dumps(self.chip, ensure_ascii=False), encoding="utf-8")
        self.truth_path = save_wiring(generate_wiring(self.chip, kind="truth", seed=9),
                                      self.root / "truth.json")
        self.pending_path = save_wiring(generate_wiring(self.chip, kind="pending"),
                                        self.root / "pending.json")
        self.truth = {e["line"]: float(e["delay"]) for e in
                      json.loads(self.truth_path.read_text(encoding="utf-8"))["lines"]}

    def build_center(self, script, **th_kw):
        session = CalibrationSession.from_pending(self.pending_path)
        center = ReviewCenter(session, DirectExecutor(FakeExecutor(script)),
                              thresholds=Thresholds(**th_kw),
                              audit_path=self.root / "audit.jsonl")
        return session, center

    def pair(self, session):
        """建两条单线块，返回 (new_block, ref_block)。"""
        session.ensure_block("Q0.z", ["Q0.z"])
        session.ensure_block("Q0.xy", ["Q0.xy"])
        return "Q0.z", "Q0.xy"


class TestContracts(Case):
    def test_round_trip(self):
        req = ExperimentRequest(id="r1", kind="timing.align", targets=["Q0.z", "Q0.xy"],
                                config_hash="abc", intent={"op": "align"}, params={"n_shots": 10})
        self.assertEqual(ExperimentRequest.from_dict(req.to_dict()).to_dict(), req.to_dict())
        res = make_result(req, -3.5, flags=["drift"], n_shots=10,
                          repeats=[{"skew_ns": -3.4}, {"skew_ns": -3.6}])
        back = ExperimentResult.from_dict(res.to_dict())
        self.assertEqual((back.skew_ns, back.flags, len(back.repeats)), (-3.5, ["drift"], 2))

    def test_missing_required_raises(self):
        with self.assertRaises(ValueError):
            ExperimentResult.from_dict({"request_id": "r", "kind": "k"})
        with self.assertRaises(ValueError):
            ExperimentRequest.from_dict({"id": "r", "targets": ["a"], "config_hash": "h"})

    def test_negative_shift_rejected(self):
        with self.assertRaises(ValueError):
            WritePlan.from_dict({"request_id": "r", "shifts": {"Q0.xy": -1.0}})

    def test_headroom_return_round_trip(self):
        plan = WritePlan.from_dict({"request_id": "r", "headroom_return": {"Q0.xy": 5.0}})
        self.assertEqual(plan.headroom_return, {"Q0.xy": 5.0})
        self.assertTrue(plan.needs_write)
        self.assertEqual(plan.to_dict()["headroom_return"], {"Q0.xy": 5.0})
        with self.assertRaises(ValueError):
            WritePlan.from_dict({"request_id": "r", "headroom_return": {"Q0.xy": -1.0}})

    def test_verdict_action_enum(self):
        with self.assertRaises(ValueError):
            Verdict("yolo", "reason")
        self.assertTrue(Verdict("noop", "r").accepted)
        self.assertFalse(Verdict("retry", "r").accepted)


class TestSession(Case):
    def test_plan_align_moves_the_lagging_block(self):
        s = CalibrationSession.from_pending(self.pending_path)
        self.pair(s)
        self.assertEqual(s.plan_align("Q0.z", "Q0.xy", -12.0).shifts, {"Q0.z": 12.0})
        self.assertEqual(s.plan_align("Q0.z", "Q0.xy", 5.0).shifts, {"Q0.xy": 5.0})

    def test_plan_align_tol_zeroes_shift(self):
        s = CalibrationSession.from_pending(self.pending_path)
        self.pair(s)
        plan = s.plan_align("Q0.z", "Q0.xy", 0.3, tol_ns=0.5)
        self.assertEqual(plan.shifts, {})
        self.assertFalse(plan.needs_write)

    def test_merge_keeps_ref_repr_first(self):
        s = CalibrationSession.from_pending(self.pending_path)
        self.pair(s)
        s.apply_plan(s.plan_align("Q0.z", "Q0.xy", -4.0, into="core"))
        self.assertEqual(s.blocks["core"], ["Q0.xy", "Q0.z"])
        self.assertEqual((s.delay("Q0.z"), s.block_repr("core")), (4.0, "Q0.xy"))
        self.assertEqual(set(s.blocks), {"core"})              # 源块已并入

    def test_apply_plan_is_right_shift_only(self):
        s = CalibrationSession.from_pending(self.pending_path)
        self.pair(s)
        with self.assertRaises(ValueError):
            s.apply_plan(WritePlan("x", shifts={"Q0.xy": -1.0}))

    def test_headroom_grant_and_return(self):
        s = CalibrationSession.from_pending(self.pending_path)
        s.apply_plan(WritePlan("h", shifts={"Q0.xy": 100.0}))
        s.grant_headroom(["Q0.xy"], 100.0)
        s.apply_plan(WritePlan("r", headroom_return={"Q0.xy": 30.0}))
        self.assertEqual(s.delay("Q0.xy"), 70.0)
        self.assertEqual(s.headroom["Q0.xy"], 70.0)
        # 收回超过授予余额 → 拒绝
        with self.assertRaises(ValueError):
            s.apply_plan(WritePlan("r2", headroom_return={"Q0.xy": 999.0}))
        # shifts 部分依旧只允许右移
        with self.assertRaises(ValueError):
            s.apply_plan(WritePlan("r3", shifts={"Q0.xy": -1.0}))

    def test_ensure_block_rejects_unknown_line(self):
        s = CalibrationSession.from_pending(self.pending_path)
        with self.assertRaises(KeyError):
            s.ensure_block("nope", ["Q99.xy"])

    def test_position_residual(self):
        res = position_residual({"a": 100.0, "b": 120.0}, {"a": 20.0, "b": 0.0})
        self.assertEqual((res["residual_ns"], res["max_ns"], res["count"]), (0.0, 120.0, 2))


class TestWriteback(Case):
    def test_hash_ignores_bookkeeping_fields(self):
        _, h0 = load_pending(self.pending_path)
        data, _ = load_pending(self.pending_path)
        data["meta"]["date"] = "2000-01-01"
        self.assertEqual(h0, content_hash(data))
        data["lines"][0]["delay"] = 1.0
        self.assertNotEqual(h0, content_hash(data))

    def test_commit_atomic_with_backup_and_audit(self):
        _, h0 = load_pending(self.pending_path)
        receipt = commit_shifts(self.pending_path, {"Q0.xy": 3.5}, expected_hash=h0,
                                audit_path=self.root / "audit.jsonl")
        self.assertEqual(read_delays(self.pending_path)["Q0.xy"], 3.5)
        self.assertTrue((self.root / "pending.json.bak").exists())
        self.assertEqual(receipt["new_hash"], load_pending(self.pending_path)[1])
        lines = (self.root / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(json.loads(lines[0])["ok"])

    def test_cas_rejects_stale_write(self):
        with self.assertRaises(WriteGuardError):
            commit_shifts(self.pending_path, {"Q0.xy": 1.0}, expected_hash="0" * 16,
                          audit_path=self.root / "audit.jsonl")
        self.assertEqual(read_delays(self.pending_path)["Q0.xy"], 0.0)
        self.assertTrue((self.root / "audit.jsonl").exists())      # 拒绝也留痕

    def test_budget_and_monotony_guards(self):
        _, h0 = load_pending(self.pending_path)
        with self.assertRaises(WriteGuardError):
            commit_shifts(self.pending_path, {"Q0.xy": 9.9}, expected_hash=h0, d_max_ns=5.0,
                          audit_path=self.root / "audit.jsonl")
        with self.assertRaises(WriteGuardError):
            commit_shifts(self.pending_path, {"Q0.xy": -1.0}, expected_hash=h0,
                          audit_path=self.root / "audit.jsonl")
        self.assertEqual(read_delays(self.pending_path)["Q0.xy"], 0.0)

    def test_unknown_line_and_truth_file_rejected(self):
        _, h0 = load_pending(self.pending_path)
        with self.assertRaises(WriteGuardError):
            commit_shifts(self.pending_path, {"Q99.xy": 1.0}, expected_hash=h0,
                          audit_path=self.root / "audit.jsonl")
        _, ht = load_pending(self.pending_path)      # truth 文件不得被校准流程改写
        with self.assertRaises(WriteGuardError):
            commit_shifts(self.truth_path, {"Q0.xy": 1.0}, expected_hash=ht,
                          audit_path=self.root / "audit.jsonl")

    def test_noop_write_is_recorded_but_not_rewritten(self):
        _, h0 = load_pending(self.pending_path)
        receipt = commit_shifts(self.pending_path, {"Q0.xy": 0.0}, expected_hash=h0,
                               audit_path=self.root / "audit.jsonl")
        self.assertFalse(receipt["written"])
        self.assertFalse((self.root / "pending.json.bak").exists())

    def test_headroom_return_lowers_delay_within_grant(self):
        _, h0 = load_pending(self.pending_path)
        r1 = commit_shifts(self.pending_path, {"RL0.ro": 100.0}, expected_hash=h0,
                           audit_path=self.root / "audit.jsonl")
        # 授予 headroom 台账后，允许以收回方式把 d 往左拉
        r2 = commit_shifts(self.pending_path, {}, expected_hash=r1["new_hash"],
                           headroom_return={"RL0.ro": 40.0},
                           granted_headroom={"RL0.ro": 100.0},
                           audit_path=self.root / "audit.jsonl")
        self.assertTrue(r2["written"])
        self.assertEqual(read_delays(self.pending_path)["RL0.ro"], 60.0)
        self.assertEqual(r2["applied"][0]["delta"], -40.0)

    def test_headroom_return_beyond_grant_rejected(self):
        _, h0 = load_pending(self.pending_path)
        with self.assertRaises(WriteGuardError):
            commit_shifts(self.pending_path, {}, expected_hash=h0,
                          headroom_return={"RL0.ro": 1.0},
                          granted_headroom={"RL0.ro": 0.0},
                          audit_path=self.root / "audit.jsonl")
        self.assertEqual(read_delays(self.pending_path)["RL0.ro"], 0.0)


class TestCenterVerdicts(Case):
    def test_commit_writes_file_and_advances_hash(self):
        session, center = self.build_center([-7.0])
        new, ref = self.pair(session)
        center.request_align(new, ref, into="core")
        v = center.pump()[0]
        self.assertEqual((v.action, v.write_plan.shifts), ("commit", {"Q0.z": 7.0}))
        self.assertEqual(read_delays(self.pending_path)["Q0.z"], 7.0)
        self.assertEqual(session.blocks["core"], ["Q0.xy", "Q0.z"])
        self.assertEqual(session.config_hash, load_pending(self.pending_path)[1])

    def test_noop_merges_without_writing(self):
        session, center = self.build_center([0.2], tol_ns=0.5)
        new, ref = self.pair(session)
        center.request_align(new, ref, into="core")
        before = self.pending_path.read_bytes()
        v = center.pump()[0]
        self.assertEqual(v.action, "noop")
        self.assertEqual(self.pending_path.read_bytes(), before)
        self.assertEqual(session.blocks["core"], ["Q0.xy", "Q0.z"])

    def test_stale_result_is_rechecked_not_written(self):
        session, center = self.build_center([-7.0])
        new, ref = self.pair(session)
        req = center.request_align(new, ref, into="core")
        v = center.review(make_result(req, -7.0, config_hash="stale-hash"))
        self.assertEqual(v.action, "recheck")
        self.assertEqual(read_delays(self.pending_path)["Q0.z"], 0.0)

    def test_bad_quality_retries_then_escalates(self):
        session, center = self.build_center([1.0, 1.0], quality_min=0.9, max_attempts=2)
        new, ref = self.pair(session)
        req = center.request_align(new, ref, into="core", params={"quality": 0.1})
        self.assertEqual(center.pump()[0].action, "retry")
        again = center.resubmit(req.id)
        self.assertEqual(again.params["quality"], 0.1)
        self.assertEqual(center.pump()[0].action, "escalate")
        self.assertEqual(read_delays(self.pending_path)["Q0.z"], 0.0)

    def test_over_budget_escalates(self):
        session, center = self.build_center([-50.0], d_max_ns=20.0)
        new, ref = self.pair(session)
        req = center.request_align(new, ref, into="core")
        v = center.review(make_result(req, -50.0))
        self.assertEqual(v.action, "escalate")
        self.assertIn("d_max", v.reason)

    def test_flags_are_graded_known_fatal_unknown(self):
        """已知瑕疵放行、致命标志判废、名单外一律按异常处理。"""
        session, center = self.build_center([1.0])
        new, ref = self.pair(session)
        req = center.request_align(new, ref, into="core")
        th = center.th
        self.assertEqual(quality_problems(make_result(req, 1.0, flags=["drift"]), th), [])
        self.assertTrue(any("致命标志" in p for p in
                            quality_problems(make_result(req, 1.0, flags=["saturation"]), th)))
        self.assertTrue(any("未知标志" in p for p in
                            quality_problems(make_result(req, 1.0, flags=["who_knows"]), th)))

    def test_untraceable_result_escalates(self):
        session, center = self.build_center([])
        ghost = ExperimentRequest(id="ghost", kind="timing.align", targets=["a", "b"],
                                  config_hash=session.config_hash)
        self.assertEqual(center.review(make_result(ghost, 1.0)).action, "escalate")

    def test_missing_block_leads_to_replan(self):
        session, center = self.build_center([-7.0])
        new, ref = self.pair(session)
        req = center.request_align(new, ref, into="core")
        del session.blocks[new]                       # 会话状态被外部改动
        self.assertEqual(center.review(make_result(req, -7.0)).action, "replan")

    def test_assessment_facts(self):
        session, center = self.build_center([-7.0])
        new, ref = self.pair(session)
        req = center.request_align(new, ref, into="core")
        a = analyze(make_result(req, -7.0, repeats=[{"skew_ns": -7.2}, {"skew_ns": -6.6}]),
                    session, req)
        self.assertTrue(a.ok)
        self.assertEqual((a.facts["abs_skew_ns"], a.facts["spread_ns"]), (7.0, 0.6))
        self.assertEqual(a.proposed.shifts, {"Q0.z": 7.0})
        self.assertEqual(a.to_dict()["proposed"]["shifts"], {"Q0.z": 7.0})

    def test_history_serializable(self):
        session, center = self.build_center([-7.0])
        new, ref = self.pair(session)
        center.request_align(new, ref, into="core")
        center.pump()
        self.assertEqual(center.stats()["commit"], 1)
        rec = json.loads(center.dump_history(self.root / "history.json")
                         .read_text(encoding="utf-8"))[0]
        self.assertEqual(rec["request_id"], "req-001-align")
        self.assertEqual(rec["written"][0]["after"], 7.0)

    def test_shift_block_bypasses_experiment_but_keeps_guards(self):
        session, center = self.build_center([])
        session.ensure_block("readout", ["Q0.xy", "Q0.z"])
        self.assertEqual(center.shift_block("readout", 1000.0, note="S2").action, "commit")
        self.assertEqual(read_delays(self.pending_path)["Q0.z"], 1000.0)
        with self.assertRaises(ValueError):
            center.shift_block("readout", -1.0)


class TestExecutors(Case):
    def test_sim_executor_matches_direct_measurement(self):
        chip = SimChip(self.truth, seed=3)
        session = CalibrationSession.from_pending(self.pending_path)
        new, ref = self.pair(session)
        req = session.new_request("timing.align", new, ref, into="core")
        res = SimExecutor(chip, seed=4).run(req)
        self.assertAlmostEqual(res.skew_ns, chip.position(new) - chip.position(ref), delta=0.5)
        self.assertEqual(res.config_hash, req.config_hash)
        self.assertEqual(res.request_id, req.id)

    def test_sim_executor_applies_approved_plan(self):
        chip = SimChip(self.truth)
        ex = SimExecutor(chip)
        plan = WritePlan("r", shifts={"Q0.xy": 12.0})
        ex.apply_plan(plan)
        self.assertEqual(chip.applied_shift("Q0.xy"), 12.0)
        self.assertEqual(read_delays(self.pending_path)["Q0.xy"], 0.0)   # 文件由回写工具负责

    def test_file_queue_roundtrip_and_reject(self):
        q = FileQueue(self.root / "runs")
        req = ExperimentRequest(id="r9", kind="timing.align", targets=["Q0.xy", "Q0.z"],
                                config_hash="h")
        q.submit(req)
        self.assertEqual(q.pending_requests()[0].id, "r9")
        self.assertEqual(q.poll(), [])
        q.publish_result(make_result(req, 2.0))
        (q.results / "broken.json").write_text("{not json", encoding="utf-8")
        # 半截文件有数秒宽限（执行方可能还在写）：伪装成早就写完的残骸再收
        os.utime(q.results / "broken.json", (0, 0))
        out = q.poll()
        self.assertEqual([r.request_id for r in out], ["r9"])
        self.assertTrue((q.consumed / "r9.json").exists())
        self.assertTrue((q.rejected / "broken.json").exists())
        self.assertEqual(len(q.errors), 1)
        self.assertEqual(q.poll(), [])


class PoisonExecutor:
    """包装 SimExecutor：可选的两种坏行为 —— 指定线一律低质量 / 结果指纹过期。"""

    def __init__(self, inner, spoiled=(), stale=False):
        self.inner = inner
        self.spoiled = set(spoiled)
        self.stale = stale
        self.applied: list[WritePlan] = []

    def run(self, req):
        res = self.inner.run(req)
        payload = res.to_dict()
        if set(req.targets) & self.spoiled:
            payload.update(quality=0.05, flags=["drift"])
        if self.stale:
            payload.update(config_hash="stale-hash")
        return ExperimentResult.from_dict(payload)

    def apply_plan(self, plan):
        self.applied.append(plan)


class TestBatchPipeline(Case):
    """批量校准闭环：最少测量 → 只记录 → 全局求解 → 一次性写回。"""

    def build(self, *, noise=0.2, seed=11, poison=(), stale=False, **th_kw):
        session = CalibrationSession.from_pending(self.pending_path)
        device = SimChip(self.truth, noise_ns=noise, seed=seed)
        inner = SimExecutor(device, seed=seed + 1)
        target = PoisonExecutor(inner, poison, stale)
        defaults = {"d_max_ns": 5000.0}
        defaults.update(th_kw)
        center = ReviewCenter(session, DirectExecutor(target),
                              thresholds=Thresholds(**defaults),
                              audit_path=self.root / "audit.jsonl",
                              params={"n_shots": 200, "repeats": 3, "noise_ns": noise})
        return session, center, target

    def pending_bytes(self):
        return self.pending_path.read_bytes()

    def test_tree_is_minimal_spanning(self):
        _, center, _ = self.build()
        pipe = CalibrationPipeline(self.chip, center)
        lines = pipe.lines()
        tree = pipe.plan_tree()
        self.assertEqual(len(tree), len(lines) - 1)      # N 条线只需 N−1 条边
        self.assertEqual(len(components(lines, tree)), 1)
        self.assertTrue(all(set(p.key) <= set(lines) for p in tree))

    def test_measure_phase_writes_nothing(self):
        session, center, _ = self.build()
        before = self.pending_bytes()
        pipe = CalibrationPipeline(self.chip, center)
        ledger = pipe.measure()
        self.assertEqual(self.pending_bytes(), before)          # 文件纹丝不动
        self.assertTrue(all(v == 0.0 for v in session.delays.values()))
        self.assertEqual(len(ledger.accepted()), len(pipe.plan_tree()))
        self.assertFalse((self.root / "writeback-audit.jsonl").exists())

    def test_single_commit_converges(self):
        session, center, target = self.build()
        report = CalibrationPipeline(self.chip, center).run(self.truth)
        self.assertIsNone(report["aborted"], report["aborted"])
        self.assertTrue(report["aligned"], report["solve"])
        self.assertTrue(report["written"])
        self.assertEqual(report["isolated"], [])
        self.assertLessEqual(report["residual"]["residual_ns"], 0.5)
        self.assertTrue(all(v >= 0 for v in read_delays(self.pending_path).values()))
        # 一次性写回：整批只留一条审计，设备只被下发过一次位移
        audit = (self.root / "writeback-audit.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(audit), 1)
        self.assertTrue(json.loads(audit[0])["ok"])
        self.assertEqual(len(target.applied), 1)
        errs, warns = validate_wiring(self.pending_path, chip=self.chip_path)
        self.assertEqual((errs, warns), ([], []))
        self.assertTrue((self.root / "pending.json.bak").exists())

    def test_dry_run_leaves_journal_for_resume(self):
        """dry-run 不落盘 ⇒ 同版本观测留在日志里，二次运行应恢复而不重测。"""
        session, center, target = self.build()
        center.write = False                          # dry-run：只求解不写回
        pipe = CalibrationPipeline(self.chip, center)
        dry = pipe.run(self.truth)
        self.assertFalse(dry["written"])
        self.assertEqual(pipe.resumed_edges, 0, "首轮日志是空的，没什么可恢复")
        requests_after_dry = len(target.inner.requests)

        # 同一份 pending 再跑一次（崩溃后重启的形态）：同版本观测直接恢复
        session2 = CalibrationSession.from_pending(self.pending_path)
        center2 = ReviewCenter(session2, DirectExecutor(target),
                               thresholds=center.th, audit_path=self.root / "audit.jsonl",
                               params=center.default_params)
        resume = CalibrationPipeline(self.chip, center2).run(self.truth)
        self.assertEqual(resume["resumed_edges"], len(resume["tree"]),
                         "测量期没写回 ⇒ 重启后应按边跳过，不重测")
        self.assertEqual(len(target.inner.requests), requests_after_dry, "不得新增实验申请")
        self.assertTrue(resume["written"])
        self.assertTrue(resume["aligned"], resume["solve"])
        self.assertLessEqual(resume["residual"]["residual_ns"], 0.5)

    def test_rerun_after_writeback_does_not_stack(self):
        """已写回的文件复跑：设备按文件状态还原，只会测到 ≈0 的 skew、不再叠加位移。"""
        _, center, target = self.build()
        first = CalibrationPipeline(self.chip, center).run(self.truth)
        self.assertTrue(first["written"])
        disk_after_first = read_delays(self.pending_path)

        session2 = CalibrationSession.from_pending(self.pending_path)
        device = SimChip(self.truth, noise_ns=0.2, seed=11)
        seed_applied_shifts(device, disk_after_first)               # 设备与文件同起点
        center2 = ReviewCenter(session2,
                               DirectExecutor(SimExecutor(device, seed=12)),
                               thresholds=Thresholds(d_max_ns=5000.0),
                               audit_path=self.root / "audit.jsonl",
                               params={"n_shots": 200, "repeats": 3, "noise_ns": 0.2})
        again = CalibrationPipeline(self.chip, center2).run(self.truth)
        self.assertFalse(again["written"], "已对齐的线不应再产生位移")
        self.assertEqual(again["plan_ns"], 0.0)
        self.assertEqual(read_delays(self.pending_path), disk_after_first, "复跑不得叠加 d")
        self.assertLessEqual(again["residual"]["residual_ns"], 0.5)

    def test_total_shift_is_minimal(self):
        """目标=最右线，总线位移应等于"各自与最右线的差距之和"，不含往返虚耗。"""
        _, center, _ = self.build(noise=0.0, seed=3)
        report = CalibrationPipeline(self.chip, center).run(self.truth)
        truth = self.truth
        rightmost = max(truth.values())
        expected = sum(rightmost - v for v in truth.values())
        self.assertAlmostEqual(report["plan_ns"], round(expected, 6), places=2)
        self.assertEqual(report["anchor"], min(truth, key=lambda k: (-truth[k], k)))

    def test_budget_exceeded_writes_nothing(self):
        _, center, target = self.build(d_max_ns=5.0)
        before = self.pending_bytes()
        report = CalibrationPipeline(self.chip, center).run(self.truth)
        self.assertIn("回写守卫拒绝", report["aborted"])
        self.assertFalse(report["written"])
        self.assertEqual(self.pending_bytes(), before)
        self.assertEqual(target.applied, [])
        audit = (self.root / "writeback-audit.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(audit), 1)
        self.assertFalse(json.loads(audit[0])["ok"])          # 拒绝了也留痕

    def test_persistent_bad_edges_are_isolated(self):
        """某条线怎么测都不合格 → 划入隔离区，其余线照常一次写回。"""
        _, center, _ = self.build(poison=["Q3.xy"], quality_min=0.6)
        report = CalibrationPipeline(self.chip, center).run(self.truth)
        self.assertIn("Q3.xy", report["isolated"])
        self.assertFalse(report["aligned"])
        self.assertTrue(report["written"])                    # 其余部分没有被牵连
        delays = read_delays(self.pending_path)
        self.assertEqual(delays["Q3.xy"], 0.0)                # 隔离线一个字节都没动
        self.assertGreater(delays["Q0.xy"], 0.0)              # 但主体确实写回了
        self.assertTrue(report["failed"])

    def test_stale_result_aborts_batch_without_writing(self):
        """执行方回的结果不是本版配置 → 测量立刻停手，整批不写。"""
        _, center, target = self.build(stale=True)
        before = self.pending_bytes()
        report = CalibrationPipeline(self.chip, center).run(self.truth)
        self.assertIn("作废", report["aborted"])
        self.assertFalse(report["written"])
        self.assertEqual(self.pending_bytes(), before)
        self.assertEqual(target.applied, [])
        self.assertFalse(report["aligned"])


class TestSolver(unittest.TestCase):
    """全局最小二乘：合成的观测数据能被精确复原。"""

    def test_chain_recovers_relative_positions(self):
        truth = {"A": 10.0, "B": 34.0, "C": 21.0}
        edges = [RelativeEdge("A", "B", truth["A"] - truth["B"], 0.1, "r1"),
                 RelativeEdge("C", "B", truth["C"] - truth["B"], 0.1, "r2")]
        res = solve(["A", "B", "C"], edges)
        target = max(truth.values())
        for ln, v in truth.items():
            self.assertAlmostEqual(res.shifts[ln], round(target - v, 6), places=6)
        self.assertTrue(all(v >= 0 for v in res.shifts.values()))
        self.assertEqual(res.anchor, "B")

    def test_redundant_edge_is_least_squares_weighted(self):
        # A-B 测 10（σ 大），A-C 3 / C-B 7（σ 小）：解应更贴近小 σ 的一组
        edges = [RelativeEdge("A", "B", 12.0, 5.0),
                 RelativeEdge("A", "C", 3.0, 0.5),
                 RelativeEdge("C", "B", 7.0, 0.5)]
        res = solve(["A", "B", "C"], edges)
        self.assertAlmostEqual(res.offsets["A"] - res.offsets["C"], 3.0, delta=0.6)
        self.assertGreater(abs(res.residuals[0]["resid_ns"]), 0.5)    # 离群边残留最大
        self.assertTrue(res.solver_converged)

    def test_plan_is_json_round_trippable(self):
        res = solve(["A", "B"], [RelativeEdge("A", "B", 2.0, 0.1)])
        plan = res.plan()
        self.assertEqual(plan.shifts, {"A": 0.0, "B": 2.0})
        self.assertEqual(WritePlan.from_dict(plan.to_dict()).shifts, plan.shifts)


if __name__ == "__main__":
    unittest.main(verbosity=2)

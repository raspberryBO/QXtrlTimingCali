# -*- coding: utf-8 -*-
"""测量日志与断点续跑：中途"崩溃"后恢复，不重测已完成的边。

核心判据只有一条：**日志里 config_hash 与当前待测文件一致的合格观测，
描述的正是眼前这个世界**——可直接跳过；hash 不一致的属于已随写回生效的
旧世界——按 stage_done 事件整阶段跳过。设备（SimChip）跨"崩溃"存活，
模拟真实机台状态不随进程消失。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qxtrl_timing_cali import (CalibrationPipeline, CalibrationSession, DirectExecutor,
                               ReviewCenter, SimChip, SimExecutor,
                               StagedCalibrationPipeline, Thresholds, generate_chip,
                               generate_wiring, journal_path_for, load_journal,
                               position_residual, save_wiring)


class _Crash(Exception):
    """模拟进程崩溃：流水线代码不捕获它，等同当场退出。"""


class _Fuse:
    """包装执行方：第 limit+1 次实验起抛 _Crash（模拟跑到一半进程挂掉）。"""

    def __init__(self, inner, limit: int):
        self.inner, self.limit, self.n = inner, int(limit), 0

    def run(self, request):
        self.n += 1
        if self.n > self.limit:
            raise _Crash(f"第 {self.n} 次实验前崩溃")
        return self.inner.run(request)

    def apply_plan(self, plan):
        self.inner.apply_plan(plan)


class _Counting:
    """包装执行方：只数实验次数（恢复后应只补测剩余的边）。"""

    def __init__(self, inner):
        self.inner, self.n = inner, 0

    def run(self, request):
        self.n += 1
        return self.inner.run(request)

    def apply_plan(self, plan):
        self.inner.apply_plan(plan)


def _executor(device, *, seed=5):
    return SimExecutor(device, seed=seed)


class _ResumeCase(unittest.TestCase):
    """2x2 方格芯片 · 16 线 · 零噪声（保证测量次数可精确对账）。"""

    def setUp(self):
        self.chip = generate_chip(num_qubits=4, topology="grid", shape=(2, 2),
                                  cavities_per_line=2, tunable=True, seed=7)
        self.truth = {e["line"]: float(e["delay"]) for e in generate_wiring(
            self.chip, kind="truth", seed=9, delay_lo=40.0, delay_hi=200.0)["lines"]}
        self.work = Path(tempfile.mkdtemp(prefix="qxtrl-resume-"))
        self.device = SimChip(self.truth, noise_ns=0.0, seed=5)   # 设备跨"崩溃"存活

    def fresh_pending(self) -> Path:
        pending = self.work / f"pending-{len(list(self.work.glob('pending-*.json')))}.json"
        save_wiring(generate_wiring(self.chip, kind="pending"), pending)
        return pending

    def center(self, pending: Path, executor, *, d_max=1000.0):
        return ReviewCenter(
            CalibrationSession.from_pending(pending),
            DirectExecutor(executor),
            thresholds=Thresholds(tol_ns=0.5, sigma_max_ns=1.0, quality_min=0.6,
                                  skew_max_ns=1500.0, d_max_ns=d_max, max_attempts=2),
            pending_path=pending, write=True,
            params={"n_shots": 50, "repeats": 2, "noise_ns": 0.0})

    def journal(self, pending: Path) -> list[dict]:
        return load_journal(journal_path_for(pending))

    def ok_by_group(self, pending: Path) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.journal(pending):
            if not d.get("event") and d.get("ok"):
                out[d["group"]] = out.get(d["group"], 0) + 1
        return out


class TestJournalResume(_ResumeCase):
    def test_reference_run_counts(self):
        """基准：一次跑通的实验总数与分组分布（其余用例与之对账）。"""
        pending = self.fresh_pending()
        counter = _Counting(_executor(SimChip(self.truth, noise_ns=0.0, seed=5)))
        report = StagedCalibrationPipeline(
            self.chip, self.center(pending, counter)).run(self.truth)
        self.assertIsNone(report["aborted"], report["aborted"])
        self.assertTrue(report["aligned"])
        self.assertEqual(self.ok_by_group(pending),
                         {"readout": 2, "qubit": 4, "coupler": 7, "readout_final": 2})
        self.assertEqual(counter.n, 15)

    def test_crash_mid_coupler_resumes_without_remeasuring(self):
        """组 3 中途崩溃：恢复后不重测组 1/组 2，只补测剩余边。"""
        ref = self.fresh_pending()
        ref_counter = _Counting(_executor(SimChip(self.truth, noise_ns=0.0, seed=5)))
        StagedCalibrationPipeline(self.chip, self.center(ref, ref_counter)).run(self.truth)
        ref_total = ref_counter.n

        pending = self.fresh_pending()
        fuse = _Fuse(_executor(self.device), limit=6)          # readout 2 + qubit 4 后崩
        with self.assertRaises(_Crash):
            StagedCalibrationPipeline(self.chip, self.center(pending, fuse)).run(self.truth)
        crash_ok = self.ok_by_group(pending)
        self.assertEqual(crash_ok.get("readout"), 2, "组 1 应已测完并写回")
        self.assertEqual(crash_ok.get("qubit"), 4, "组 2 应已测完（未写回）")

        counter = _Counting(_executor(self.device, seed=6))
        report = StagedCalibrationPipeline(
            self.chip, self.center(pending, counter)).run(self.truth)
        self.assertIsNone(report["aborted"], report["aborted"])
        self.assertTrue(report["aligned"])
        # 恢复期只补测：coupler 剩余 + readout_final；组 1/组 2 一条都不重测
        self.assertEqual(counter.n, ref_total - 6)
        final_ok = self.ok_by_group(pending)
        self.assertEqual(final_ok["readout"], 2)
        self.assertEqual(final_ok["qubit"], 4)
        self.assertEqual(final_ok["coupler"], 7)
        self.assertEqual(final_ok["readout_final"], 2)
        # 报告里能看到"阶段跳过"的留痕
        self.assertTrue(report["stages"][0]["resumed"])

    def test_crash_during_readout_resumes_from_journal(self):
        """组 1 都没测完（无任何写回）：恢复后从日志续测，不重测已完成的边。"""
        ref = self.fresh_pending()
        ref_counter = _Counting(_executor(SimChip(self.truth, noise_ns=0.0, seed=5)))
        StagedCalibrationPipeline(self.chip, self.center(ref, ref_counter)).run(self.truth)

        pending = self.fresh_pending()
        fuse = _Fuse(_executor(self.device), limit=1)          # 第 1 条读取线测完即崩
        with self.assertRaises(_Crash):
            StagedCalibrationPipeline(self.chip, self.center(pending, fuse)).run(self.truth)
        self.assertEqual(self.ok_by_group(pending), {"readout": 1})
        self.assertFalse(any(d.get("event") for d in self.journal(pending)),
                        "组 1 未完成，不应有任何 stage_done 事件")

        counter = _Counting(_executor(self.device, seed=6))
        report = StagedCalibrationPipeline(
            self.chip, self.center(pending, counter)).run(self.truth)
        self.assertTrue(report["aligned"])
        self.assertEqual(counter.n, ref_counter.n - 1)
        self.assertFalse(any(s["resumed"] for s in report["stages"]),
                         "没有阶段被整体跳过，只是边级续测")

    def test_rerun_after_completion_measures_nothing(self):
        """整轮已完成：再跑一遍零测量、零写回，报告仍对齐。"""
        pending = self.fresh_pending()
        first = _Counting(_executor(self.device))
        report1 = StagedCalibrationPipeline(
            self.chip, self.center(pending, first)).run(self.truth)
        self.assertTrue(report1["aligned"])
        journal_after_first = len(self.journal(pending))

        counter = _Counting(_executor(self.device, seed=6))
        report2 = StagedCalibrationPipeline(
            self.chip, self.center(pending, counter)).run(self.truth)
        self.assertIsNone(report2["aborted"], report2["aborted"])
        self.assertEqual(counter.n, 0, "全部阶段已生效：不应再发任何实验")
        self.assertFalse(report2["written"])
        self.assertTrue(all(s["resumed"] for s in report2["stages"]))
        self.assertEqual(len(self.journal(pending)), journal_after_first,
                         "重跑不得追加任何日志行")
        res = position_residual(self.truth, {e["line"]: float(e["delay"]) for e in
                                             __import__("json").loads(
                                                 pending.read_text(encoding="utf-8"))["lines"]})
        self.assertLess(res["residual_ns"], 0.5)

    def test_headroom_grant_is_replayed_on_resume(self):
        """headroom 授予台账随日志重放：恢复后步 6 仍能把 headroom 收回。

        若授予丢失，步 6 无余额可收，读取线停在 +1000 ns 的位置，
        残差 ≈ headroom，aligned 必然 False。
        """
        pending = self.fresh_pending()
        fuse = _Fuse(_executor(self.device), limit=3)          # 组 1 已写回+授予，组 2 刚开头
        with self.assertRaises(_Crash):
            StagedCalibrationPipeline(
                self.chip, self.center(pending, fuse, d_max=3000.0),
                readout_headroom_ns=1000.0).run(self.truth)
        ev = next(d for d in self.journal(pending)
                  if d.get("event") == "stage_done" and d.get("stage") == "readout")
        self.assertEqual(ev["headroom"]["amount_ns"], 1000.0)

        counter = _Counting(_executor(self.device, seed=6))
        report = StagedCalibrationPipeline(
            self.chip, self.center(pending, counter, d_max=3000.0),
            readout_headroom_ns=1000.0).run(self.truth)
        self.assertIsNone(report["aborted"], report["aborted"])
        self.assertTrue(report["aligned"], "授予未重放则 headroom 收不回，此处必然失败")
        disk = {e["line"]: float(e["delay"]) for e in __import__("json").loads(
            pending.read_text(encoding="utf-8"))["lines"]}
        rl = {k: v for k, v in disk.items() if k.split(".")[0].startswith("RL")}
        self.assertLess(max(rl.values()), 1000.0, "headroom 应在步 6 被收回")

    def test_batch_pipeline_resume(self):
        """两阶段批量流程同样支持断点续跑。"""
        ref_pending = self.fresh_pending()
        ref_counter = _Counting(_executor(SimChip(self.truth, noise_ns=0.0, seed=5)))
        ref_report = CalibrationPipeline(
            self.chip, self.center(ref_pending, ref_counter)).run(self.truth)
        self.assertTrue(ref_report["aligned"])
        ref_total = ref_counter.n

        pending = self.fresh_pending()
        fuse = _Fuse(_executor(self.device), limit=8)
        with self.assertRaises(_Crash):
            CalibrationPipeline(self.chip, self.center(pending, fuse)).run(self.truth)

        counter = _Counting(_executor(self.device, seed=6))
        report = CalibrationPipeline(self.chip, self.center(pending, counter)).run(self.truth)
        self.assertIsNone(report["aborted"], report["aborted"])
        self.assertTrue(report["aligned"])
        self.assertEqual(counter.n, ref_total - 8, "恢复后只补测剩余的树边")


class TestJournalUnits(unittest.TestCase):
    """日志读写与序号续接的单元行为。"""

    def test_load_journal_skips_corrupt_lines(self):
        from qxtrl_timing_cali import append_journal
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "measurements-audit.jsonl"
            append_journal(p, {"a": "Q0.z", "b": "Q0.xy", "ok": True})
            with p.open("a", encoding="utf-8") as fh:
                fh.write('{"a": "Q1.z", "b": "Q1.xy", "ok": Tru')   # 崩溃写了一半
            append_journal(p, {"a": "Q2.z", "b": "Q2.xy", "ok": True})
            rows = load_journal(p)
            self.assertEqual([r["a"] for r in rows], ["Q0.z", "Q2.z"])

    def test_advance_seq_past_is_idempotent(self):
        session = CalibrationSession({"A": 0.0})
        session.ensure_block("A", ["A"])
        ids = ["req-003-align", "req-007-align", "not-a-request"]
        session.advance_seq_past(ids)
        self.assertEqual(session.new_request("timing.align", "A", "A").id, "req-008-align")
        session.advance_seq_past(ids)                    # 重复恢复不得二次跳号
        session.advance_seq_past(["req-009-align"])
        self.assertEqual(session.new_request("timing.align", "A", "A").id, "req-010-align")

    def test_preload_filters_by_hash(self):
        session = CalibrationSession({"A": 0.0, "B": 0.0, "C": 0.0}, config_hash="h1")
        from qxtrl_timing_cali import MeasurementLedger
        ledger = MeasurementLedger(session, DirectExecutor(None), journal_path=None)
        rows = [{"a": "A", "b": "B", "ok": True, "skew_ns": 1.0, "sigma_ns": 0.1,
                 "quality": 0.9, "group": "qubit", "request_id": "req-001-align",
                 "config_hash": "h1"},
                {"a": "B", "b": "C", "ok": True, "skew_ns": 2.0, "sigma_ns": 0.1,
                 "quality": 0.9, "group": "qubit", "request_id": "req-002-align",
                 "config_hash": "h0"},                     # 旧世界：作废
                {"event": "stage_done", "stage": "qubit"}]
        self.assertEqual(ledger.preload(rows), 1)
        self.assertEqual([m.a for m in ledger.accepted()], ["A"])
        # require_hash=False 仅供展示旧世界留痕（阶段跳过的报告）
        self.assertEqual(ledger.preload(rows, require_hash=False), 1)
        self.assertEqual(sorted(m.a for m in ledger.accepted()), ["A", "B"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

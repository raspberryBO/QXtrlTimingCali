# -*- coding: utf-8 -*-
"""并行演示动画：几何自检、无占位符残留、数据可由真跑流水线复现、波不变量。"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qxtrl_timing_cali import (Thresholds, generate_wiring, save_wiring)  # noqa: E402
from qxtrl_timing_cali.review.parallel import ParallelPlan, MeasurementWave


def load_doc_script(filename: str, module_name: str):
    """docs/ 下的脚本不是包的一部分，按路径加载。"""
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "docs" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = load_doc_script("gen_parallel_animation.py", "gen_parallel_animation")
gen_data = load_doc_script("gen_parallel_data.py", "gen_parallel_data")


class TestParallelAnimation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((ROOT / "docs" / "parallel-data.json").read_text(encoding="utf-8"))
        cls.scenes = gen.build_scenes(cls.data)
        cls.lay = gen.build_layout(cls.data)
        cls.html = (ROOT / "docs" / "demo-animation-parallel.html").read_text(encoding="utf-8")

    def test_selfcheck_clean(self):
        self.assertEqual(gen.selfcheck(self.data, self.lay), [])

    def test_all_grid_neighbors_are_physically_connected(self):
        """所有上下、左右最近邻均有耦合器，并完整显示；不补跨行远程边。"""
        expected = {frozenset((f"Q{r * 4 + c}", f"Q{r * 4 + c + 1}"))
                    for r in range(4) for c in range(3)}
        expected |= {frozenset((f"Q{r * 4 + c}", f"Q{(r + 1) * 4 + c}"))
                     for r in range(3) for c in range(4)}
        chip = self.data["chip"]
        self.assertEqual(chip["meta"]["topology"], "grid")
        self.assertEqual({frozenset(cp["qubits"]) for cp in chip["couplers"]}, expected)
        displayed = {cp["id"] for cp in self.lay["intra"] + self.lay["bridges"]}
        self.assertEqual(displayed, {cp["id"] for cp in chip["couplers"]})
        self.assertEqual(len(self.lay["intra"]), 12)
        self.assertEqual(len(self.lay["bridges"]), 12)

    def test_calibration_bars_preserve_actual_positions(self):
        """55 条线须覆盖真实三次写回，含向左收回，且全部落在画布内。"""
        lines = self.data["calibration_lines"]
        self.assertEqual(len(lines), self.data["meta"]["lines"])
        self.assertEqual(len({ln["name"] for ln in lines}), len(lines))
        self.assertTrue(any(ln["d3"] < 0 for ln in lines))
        cal = gen.calibration_layout(self.data)
        for ln in lines:
            self.assertAlmostEqual(ln["q1"], ln["L"] + ln["d1"] + ln["d2"] + ln["d3"])
            for position in (ln["L"], ln["L"] + ln["d1"], ln["L"] + ln["d1"] + ln["d2"], ln["q1"]):
                self.assertGreaterEqual(position, 0)
                self.assertLess(position, cal["dom"])
        spread = max(ln["q1"] for ln in lines) - min(ln["q1"] for ln in lines)
        self.assertAlmostEqual(spread, self.data["summary"]["residual_ns"], places=5)
        svg = gen.svg_calibration(self.data)
        self.assertEqual(svg.count('class="cal-row"'), len(lines))
        self.assertIn('id="calibration"', self.html)

    def test_no_placeholder_left(self):
        for ph in ("__DATA__", "__SCENES__", "__COLOR__", "__CHIPDESC__",
                   "{CHIP}", "{MATRIX}", "{TIMELINE}"):
            self.assertNotIn(ph, self.html)

    def test_every_edge_has_anchors(self):
        """每条合格测量边都要能画出来（否则动画会缺边）。"""
        anchors = self.lay["anchors"]
        for s in self.data["stages"]:
            for m in s["measurements"]:
                if m["ok"]:
                    self.assertIn(m["a"], anchors, m["a"])
                    self.assertIn(m["b"], anchors, m["b"])

    def test_waves_are_conflict_free_and_complete(self):
        for s in self.data["stages"]:
            waves = (s.get("waves") or {}).get("waves", [])
            seen = set()
            for w in waves:
                lines = [ln for p in w["pairs"] for ln in (p["a"], p["b"])]
                self.assertEqual(len(lines), len(set(lines)),
                                 f"{s['name']} 波 {w['index']} 内有共享线名")
                for p in w["pairs"]:
                    key = frozenset((p["a"], p["b"]))
                    self.assertNotIn(key, seen, f"{s['name']} 有重复边")
                    seen.add(key)
            self.assertEqual(len(seen), len(s["pairs"]),
                             f"{s['name']} 波划分覆盖的边数应与计划一致")

    def test_parallel_numbers_are_consistent(self):
        """串行次数 = 各组合计边数；冲突波比值不表示实测加速。"""
        seq = sum(len(s["pairs"]) for s in self.data["stages"])
        waves = sum(len((s.get("waves") or {}).get("waves", [])) for s in self.data["stages"])
        self.assertEqual(self.data["summary"]["rounds_sequential"], seq)
        self.assertEqual(self.data["summary"]["rounds_parallel"], waves)
        self.assertEqual(len(self.lay["timeline"]["cells"]), waves)
        self.assertAlmostEqual(self.data["summary"]["wave_reduction_ratio"], round(seq / waves, 2), places=6)

    def test_bundle_ratio_uses_actual_submission_plan(self):
        """理论轮次须按运行时提交束计算，不能拿冲突波替代提交次数。"""
        total = 0
        for stage in self.data["stages"]:
            waves = stage["waves"]
            plan = ParallelPlan(
                waves=[MeasurementWave(index=w["index"], pairs=w["pairs"])
                       for w in waves["waves"]], bundle_size=waves["bundle_size"])
            count = len(plan.bundles())
            total += count
            entry = next(p for p in self.data["parallel"] if p["stage"] == stage["name"])
            self.assertEqual(entry["bundle_count"], count)
        summary = self.data["summary"]
        self.assertEqual(summary["bundles_total"], total)
        self.assertEqual(total, 18)
        self.assertEqual(summary["ideal_bundle_speedup"], round(summary["rounds_sequential"] / total, 2))
        self.assertLess(summary["ideal_bundle_speedup"], summary["wave_reduction_ratio"])

    def test_shipped_html_matches_current_data_and_renderer(self):
        """JSON 更新后必须同步 HTML，避免页面内嵌旧指标。"""
        self.assertEqual(self.html, gen.render(self.data, self.scenes, self.lay))

    def test_qubit_group_is_single_wave(self):
        """16 条 xy↔z 边互不共享线名，应同波；提交束另受并发上限约束。"""
        qubit = next(s for s in self.data["stages"] if s["name"] == "qubit")
        waves = (qubit.get("waves") or {}).get("waves", [])
        self.assertEqual(len(waves), 1)
        self.assertEqual(len(waves[0]["pairs"]), len(qubit["pairs"]))
        self.assertEqual({m["wave"] for m in qubit["measurements"]}, {1})

    def test_js_references_existing_elements(self):
        """JS 里用到的元素 id 都必须真实存在（否则一运行就抛错）。"""
        ids = set(re.findall(r'getElementById\("([^"]+)"\)', self.html))
        ids |= set(re.findall(r'\$\("#([\w-]+)"\)', self.html))
        self.assertTrue(ids, "没抓到任何 id 引用，正则可能失效")
        for i in sorted(ids):
            self.assertIn(f'id="{i}"', self.html, f"JS 引用了不存在的元素 #{i}")

    def test_embedded_payload_is_complete(self):
        """内嵌数据必须带齐 JS 要用的字段（波总数、每束条数、汇总数字）。"""
        m = re.search(r"const DATA = (\{.*?\});$", self.html, re.M)
        self.assertIsNotNone(m, "没找到内嵌 DATA")
        payload = json.loads(m.group(1))
        self.assertEqual(payload["wave_total"], len(self.lay["timeline"]["cells"]))
        self.assertEqual(payload["wave_total"], self.data["summary"]["rounds_parallel"])
        self.assertIn("max_parallel", payload["meta"])
        self.assertIn("bundles_total", payload["summary"])
        self.assertIn("ideal_bundle_speedup", payload["summary"])
        for s in payload["stages"]:
            self.assertIn("waves", s)
        m2 = re.search(r"const SCENES = (\[.*?\]);$", self.html, re.M)
        self.assertIsNotNone(m2, "没找到内嵌 SCENES")
        self.assertEqual(len(json.loads(m2.group(1))), len(self.scenes))

    def test_demo_ends_aligned(self):
        self.assertTrue(self.data["summary"]["aligned"], self.data["summary"])
        self.assertLessEqual(self.data["summary"]["residual_ns"], self.data["meta"]["tol_ns"])

    def test_no_doubled_braces(self):
        """CSS/JS 完整性：模板残留 {{ 会废掉整张样式表（v0.4 的真实事故）。"""
        self.assertNotIn("{{", self.html)
        self.assertIn(".edge{opacity", self.html, "样式表应保持有效")

    def test_scene_engine(self):
        """内嵌 SCENES：9 幕，收尾测量须在整批写回之后。"""
        m = re.search(r"const SCENES = (\[.*?\]);$", self.html, re.M)
        self.assertIsNotNone(m, "没找到内嵌 SCENES")
        scenes = json.loads(m.group(1))
        self.assertEqual(len(scenes), 9)
        ids = [s["id"] for s in scenes]
        self.assertLess(ids.index("write"), ids.index("finish"))
        self.assertEqual(len({s["id"] for s in scenes}), len(scenes))
        for s in scenes:
            self.assertGreater(s["dur"], 0)

    def test_bundles_within_max_parallel(self):
        """提交束按 ParallelPlan.bundles 同款规则切块：每束 ≤ max_parallel 且拼回 = 波大小。"""
        mp = self.data["meta"]["max_parallel"]
        _, wmeta = gen.build_meas(self.data)
        self.assertEqual(sum(w["size"] for w in wmeta), len(self.lay["meas"]))
        for w in wmeta:
            self.assertTrue(w["bundles"])
            self.assertLessEqual(max(w["bundles"]), mp)
            self.assertEqual(sum(w["bundles"]), w["size"])

    def test_matrix_and_gantt_match_waves(self):
        """矩阵格与甘特段/刻度都要和数据的波划分、边数一一对应。"""
        for row in self.lay["matrix"]["rows"]:
            stage = next(s for s in self.data["stages"] if s["name"] == row["stage"])
            waves = (stage.get("waves") or {}).get("waves", [])
            self.assertEqual([c["size"] for c in row["cells"]],
                             [len(w["pairs"]) for w in waves])
            self.assertEqual([c["index"] for c in row["cells"]],
                             [w["index"] for w in waves])
        self.assertEqual(len(self.lay["timeline"]["cells"]),
                         self.data["summary"]["rounds_parallel"])
        self.assertEqual(len(self.lay["timeline"]["ticks"]),
                         self.data["summary"]["rounds_sequential"])

    def test_regeneration_reproduces_shipped_dataset(self):
        """库里那份数据必须能由真跑流水线逐字段复现（时间戳/临时路径除外）。"""
        # 芯片日期钉在入库数据上：generate_chip 缺省写"今天"，不钉住的话
        # 这条测试只在数据生成当天能过（animation 测试同款做法）
        chip = gen_data.generate_chip(**{**gen_data.CHIP,
                                         "day": self.data["meta"]["chip"]["date"]})
        truth = {e["line"]: float(e["delay"]) for e in generate_wiring(
            chip, kind="truth", seed=gen_data.TRUTH_SEED,
            delay_lo=40.0, delay_hi=200.0)["lines"]}
        work = Path(tempfile.mkdtemp(prefix="qxtrl-paranim-"))
        pending = work / "pending.json"
        save_wiring(generate_wiring(chip, kind="pending"), pending)
        fresh = gen_data.build(chip, truth, pending,
                               th=Thresholds(tol_ns=0.5, d_max_ns=6000.0),
                               params={"n_shots": 400, "repeats": 3, "noise_ns": 1.0,
                                       "readout_headroom_ns": 100.0},
                               max_parallel=4, write=True)

        def strip(obj):
            if isinstance(obj, dict):
                return {k: strip(v) for k, v in obj.items()
                        if k not in ("ts", "pending", "request_id", "measured_at")}
            if isinstance(obj, list):
                return [strip(x) for x in obj]
            return obj

        self.assertEqual(strip(fresh), strip(self.data),
                         "docs/parallel-data.json 与真实流水线输出不再一致")


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""生成 → 校验 → 可视化 全链路冒烟测试（stdlib unittest）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qxtrl_timing_cali import (generate_chip, generate_wiring, line_names,
                               render_chip_html, validate_chip, validate_wiring)


class TestChipGen(unittest.TestCase):
    def test_default_4q_valid(self):
        chip = generate_chip(num_qubits=4, seed=1)
        errs, warns = validate_chip(chip)
        self.assertEqual(errs, [], f"errors: {errs}")
        self.assertEqual(warns, [], f"warnings: {warns}")

    def test_scale_10q_3perline(self):
        chip = generate_chip(num_qubits=10, cavities_per_line=3, seed=7)
        errs, _ = validate_chip(chip)
        self.assertEqual(errs, [], f"errors: {errs}")
        self.assertEqual(len(chip["readout_lines"]), 4)
        self.assertEqual(len(line_names(chip)), 10 * 2 + 9 + 4 * 2)

    def test_fixed_qubit_no_z_lines(self):
        chip = generate_chip(num_qubits=3, tunable=False, seed=2)
        errs, _ = validate_chip(chip)
        self.assertEqual(errs, [])
        self.assertFalse(any(n.endswith(".z") for n in line_names(chip)))


class TestTopologies(unittest.TestCase):
    """构型参数：pos / couplers / connections 全部由 topology 推导。"""

    def _edges(self, chip):
        return {frozenset(cp["qubits"]) for cp in chip["couplers"]}

    def test_grid_3x3(self):
        chip = generate_chip(shape=(3, 3), topology="grid",
                             cavities_per_line=None, seed=21)
        errs, warns = validate_chip(chip)
        self.assertEqual(errs, [], f"errors: {errs}")
        self.assertEqual(warns, [], f"warnings: {warns}")
        self.assertEqual(len(chip["qubits"]), 9)
        self.assertEqual(len(chip["couplers"]), 12)          # 横 6 + 竖 6
        self.assertEqual(len(self._edges(chip)), 12)          # 无重复边
        self.assertEqual(len(chip["readout_lines"]), 3)       # 一行一条读取线
        deg = {q["id"]: sum(q["id"] in cp["qubits"] for cp in chip["couplers"])
               for q in chip["qubits"]}
        self.assertEqual(deg["Q4"], 4)                        # 中心比特度数 4
        self.assertEqual({tuple(q["pos"]) for q in chip["qubits"]},
                         {(round(c * 1.6, 3), round(r * 1.6, 3))
                          for r in range(3) for c in range(3)})

    def test_ring_and_ladder_edge_counts(self):
        ring = generate_chip(num_qubits=6, topology="ring", seed=22)
        self.assertEqual(len(ring["couplers"]), 6)
        ladder = generate_chip(num_qubits=6, topology="ladder", seed=23)
        self.assertEqual(len(ladder["couplers"]), 7)          # 两轨 4 + 横档 3
        for chip in (ring, ladder):
            errs, _ = validate_chip(chip)
            self.assertEqual(errs, [], f"errors: {errs}")

    def test_extra_edges_append_long_range(self):
        chip = generate_chip(num_qubits=6, topology="chain",
                             extra_edges=[(0, 5), (1, 3), (0, 5)], seed=24)
        self.assertEqual(len(chip["couplers"]), 7)             # 5 链边 + 2 长程（重复项已去重）
        self.assertIn(frozenset({"Q0", "Q5"}), self._edges(chip))
        errs, _ = validate_chip(chip)
        self.assertEqual(errs, [], f"errors: {errs}")

    def test_bad_inputs_raise(self):
        with self.assertRaises(ValueError):
            generate_chip(num_qubits=5, topology="ladder")
        with self.assertRaises(ValueError):
            generate_chip(num_qubits=2, topology="ring")
        with self.assertRaises(ValueError):
            generate_chip(num_qubits=4, topology="hex")
        with self.assertRaises(ValueError):
            generate_chip(num_qubits=4, extra_edges=[(0, 9)])
        with self.assertRaises(ValueError):
            generate_chip(num_qubits=4, extra_edges=[(2, 2)])

    def test_shape_overrides_num_qubits(self):
        chip = generate_chip(num_qubits=4, topology="grid", shape=(2, 5), seed=25)
        self.assertEqual(len(chip["qubits"]), 10)


class TestWiringGen(unittest.TestCase):
    def test_pending_kind_is_all_zero(self):
        chip = generate_chip(num_qubits=4, seed=3)
        wiring = generate_wiring(chip)
        errs, warns = validate_wiring(wiring, chip=chip)
        self.assertEqual(errs, [], f"errors: {errs}")
        self.assertEqual(warns, [], f"warnings: {warns}")
        self.assertEqual(wiring["meta"]["kind"], "pending")
        self.assertTrue(all(e["delay"] == 0.0 for e in wiring["lines"]))

    def test_truth_kind_holds_initial_position(self):
        chip = generate_chip(num_qubits=6, seed=4)
        wiring = generate_wiring(chip, kind="truth", seed=5,
                                 delay_lo=40.0, delay_hi=200.0)
        errs, _ = validate_wiring(wiring, chip=chip)
        self.assertEqual(errs, [])
        self.assertEqual(wiring["meta"]["kind"], "truth")
        self.assertTrue(all(40.0 <= e["delay"] <= 200.0 for e in wiring["lines"]))

    def test_truth_and_pending_share_line_set(self):
        """两种语义的文件结构一致，可直接逐线相加 q = L + d。"""
        chip = generate_chip(shape=(3, 3), topology="grid",
                             cavities_per_line=None, seed=12)
        truth = generate_wiring(chip, kind="truth", seed=13)
        pending = generate_wiring(chip, kind="pending")
        self.assertEqual([e["line"] for e in truth["lines"]],
                         [e["line"] for e in pending["lines"]])
        q = {e["line"]: e["delay"] for e in truth["lines"]}
        for e in pending["lines"]:
            q[e["line"]] += e["delay"]          # 待测初值 0 → q = L + 0
        self.assertEqual(set(q), set(line_names(chip)))
        self.assertEqual(q["Q0.xy"], truth["lines"][0]["delay"])

    def test_minimal_shape_no_channel(self):
        """连线文件只含 line + delay。"""
        chip = generate_chip(num_qubits=4, seed=11)
        wiring = generate_wiring(chip, kind="truth", seed=1)
        self.assertTrue(all(set(e) == {"line", "delay"} for e in wiring["lines"]))
        errs, warns = validate_wiring(wiring, chip=chip)
        self.assertEqual(errs, [], f"errors: {errs}")
        self.assertEqual(warns, [])

    def test_kind_declared_required(self):
        chip = generate_chip(num_qubits=4, seed=14)
        wiring = generate_wiring(chip, kind="truth", seed=1)
        del wiring["meta"]["kind"]
        _, warns = validate_wiring(wiring, chip=chip)
        self.assertTrue(any("meta.kind" in w for w in warns), warns)
        wiring["meta"]["kind"] = "calibrated"
        errs, _ = validate_wiring(wiring, chip=chip)
        self.assertTrue(any("meta.kind" in e for e in errs), errs)

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            generate_wiring(generate_chip(num_qubits=4, seed=15), kind="calibrated")

    def test_mismatch_detected(self):
        chip = generate_chip(num_qubits=4, seed=6)
        wiring = generate_wiring(chip, wiring_id="x")
        wiring["lines"].pop()
        errs, _ = validate_wiring(wiring, chip=chip)
        self.assertTrue(any("缺少线路" in e for e in errs), errs)


class TestVisualize(unittest.TestCase):
    def test_html_contains_key_info(self):
        chip = generate_chip(num_qubits=4, seed=8)
        wiring = generate_wiring(chip, kind="truth", seed=9)
        doc = render_chip_html(chip, wiring)
        for token in ("Q0", "CP0001", "RL0", "delay", "真实值保留"):
            self.assertIn(token, doc)
        self.assertNotIn("channel", doc)
        self.assertIn("<svg", doc)
        pending_doc = render_chip_html(chip, generate_wiring(chip, kind="pending"))
        self.assertIn("待测", pending_doc)
        self.assertNotIn("真实值保留", pending_doc)

    def test_html_chip_only(self):
        doc = render_chip_html(generate_chip(num_qubits=2, seed=10))
        self.assertNotIn("连线表", doc)

    def test_html_2d_grid(self):
        chip = generate_chip(shape=(3, 3), topology="grid",
                             cavities_per_line=None, seed=31)
        doc = render_chip_html(chip)
        self.assertIn("polyline", doc)
        self.assertIn("grid", doc)
        self.assertIn('viewBox="0 0 1160 ', doc)

    def test_grid_px_overrides_auto_scale(self):
        import re
        chip = generate_chip(shape=(3, 3), topology="grid",
                             cavities_per_line=None, seed=31)
        wide_chip = generate_chip(shape=(3, 4), topology="grid",
                                  cavities_per_line=None, seed=32)
        box = lambda doc: tuple(float(v) for v in
                               re.search(r'viewBox="0 0 (\d+) (\d+)', doc).groups())
        circles = lambda doc: [float(m) for m in re.findall(r'<circle cx="([\d.]+)"', doc)]
        auto, wide = render_chip_html(chip), render_chip_html(chip, grid_px=280.0)
        self.assertGreater(max(circles(wide)), max(circles(auto)))   # 间距被拉开
        self.assertGreater(box(wide)[1], box(auto)[1])               # 画布高度随之撑开
        self.assertLess(max(circles(wide)), box(wide)[0])           # 不被裁掉
        over = render_chip_html(wide_chip, grid_px=280.0)           # 4 列时宽度也要外扩
        self.assertGreater(box(over)[0], 1160.0)
        self.assertLess(max(circles(over)), box(over)[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)

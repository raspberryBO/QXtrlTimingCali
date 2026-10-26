# -*- coding: utf-8 -*-
"""动画生成器的几何自检测试：换芯片 / 换量级都不能把图画溢出。"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qxtrl_timing_cali import (Thresholds, generate_chip, generate_wiring, line_names,   # noqa: E402
                               save_wiring)


def load_doc_script(filename: str, module_name: str):
    """docs/ 下的脚本不是包的一部分，按路径加载。"""
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "docs" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = load_doc_script("gen_animation.py", "gen_animation")


def synth(chip: dict, *, l_max: float) -> dict:
    """造一份只用于布局校验的数据集：L 铺到 [40, l_max]，写回后全部对齐到 l_max。"""
    names = line_names(chip)
    lo = 40.0
    hi = max(l_max, lo + 1)
    lines, shifts = [], {}
    for i, name in enumerate(names):
        head = name.split(".")[0]
        group = ("coupler" if head.startswith("CP")
                 else "readout" if head.startswith("RL") else "qubit")
        length = lo + (hi - lo) * i / max(1, len(names) - 1)
        shifts[name] = round(hi - length, 6)
        lines.append({"name": name, "group": group, "L": round(length, 6),
                      "d": shifts[name], "q1": round(hi, 6)})
    return {"meta": {"lines": len(names), "tol_ns": 0.5, "chip": {"description": "synthetic"}},
            "chip": chip, "lines": lines, "candidates": [], "tree": [], "measurements": [],
            "solve": {"anchor": names[-1], "shifts": shifts, "iterations": 7},
            "report": {"target_ns": hi, "aligned": True,
                       "residual": {"residual_ns": 0.2, "min_ns": hi - 0.2, "max_ns": hi}}}


class TestShippedData(unittest.TestCase):
    """入库的那份数据必须原地通过自检，且视觉参数与 v0.3 首版一致。"""

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((ROOT / "docs" / "animation-data.json").read_text(encoding="utf-8"))
        cls.lay = gen.build_layout(cls.data)

    def test_selfcheck_clean(self):
        self.assertEqual(gen.selfcheck(self.data, self.lay), [])

    def test_time_axis_covers_data(self):
        tl = self.lay["time"]
        hi = max(max(l["L"] for l in self.data["lines"]), max(l["q1"] for l in self.data["lines"]))
        self.assertGreaterEqual(tl["dom"], hi)
        self.assertEqual(tl["dom"], 200.0)                       # 好看的整数上限
        self.assertEqual(tl["ticks"], [0.0, 50.0, 100.0, 150.0, 200.0])

    def test_anchor_and_label_stay_inside(self):
        tl = self.lay["time"]
        self.assertLessEqual(tl["x0"], tl["targetX"])
        self.assertLessEqual(tl["targetX"], tl["x1"])
        self.assertLessEqual(tl["labelMaxPx"], tl["w"])

    def test_readout_lines_get_distinct_slots(self):
        anchors = self.lay["chip"]["anchor"]
        ids = [r["id"] for r in self.lay["chip"]["readouts"]]
        self.assertEqual(len(ids), len(self.data["chip"]["readout_lines"]))
        ys = {round(anchors[f"{r}.ro"][1], 1) for r in ids}
        self.assertEqual(len(ys), len(ids), "读取线锚点互相重叠")

    def test_headroom_is_pulled_back_left(self):
        """加了 headroom 的数据：读取线先右移 headroom，末尾必须往左收回对齐。"""
        meta = self.data["meta"]
        h = meta.get("headroom_ns", 0.0)
        if not h:
            self.skipTest("demo 数据未加 headroom")
        ro = [l for l in self.data["lines"] if l["group"] == "readout"]
        self.assertTrue(ro)
        for l in ro:
            self.assertGreaterEqual(l["d1"], h - 1e-6, f"{l['name']} 未右移 headroom")
            self.assertLess(l["d3"], 0.0, f"{l['name']} 末尾没有往左收回 headroom")
        qs = [l["q1"] for l in self.data["lines"]]
        self.assertLess(max(qs) - min(qs), meta["tol_ns"], "收回后未对齐")

    def test_render_leaves_no_placeholder(self):
        html = gen.render(self.data)
        for ph in ("__ANIM_DATA__", "__CHIPDESC__", "__ANCHOR__", "__CHIP_VB__",
                   "__TIME_VB__", "__CHIP_COL__"):
            self.assertNotIn(ph, html)
        self.assertIn('"layout":{"chip"', html.replace(" ", ""))


class TestLayoutScales(unittest.TestCase):
    """10 比特 / 33 条线 / 0–1000 ns / 4 条读取线：布局必须跟着长，而不是把内容裁掉。"""

    @classmethod
    def setUpClass(cls):
        cls.chip = generate_chip(num_qubits=10, cavities_per_line=3, seed=7)
        cls.data = synth(cls.chip, l_max=1000.0)
        cls.lay = gen.build_layout(cls.data)

    def test_selfcheck_clean(self):
        self.assertEqual(gen.selfcheck(self.data, self.lay), [])

    def test_dom_follows_delay_magnitude(self):
        tl = self.lay["time"]
        self.assertEqual(tl["dom"], 1000.0)
        self.assertEqual(tl["ticks"][0], 0.0)
        self.assertEqual(tl["ticks"][-1], 1000.0)
        self.assertLessEqual(len(tl["ticks"]), 8)

    def test_more_lines_mean_taller_canvas(self):
        tl = self.lay["time"]
        n = len(self.data["lines"])
        self.assertGreater(tl["h"], tl["y0"] + n * tl["step"])
        self.assertLess(tl["step"], 22.0, "行多时应压缩行高")
        small = gen.time_layout(self.data["lines"][:8], 1000.0, "Q0.xy")
        self.assertGreater(tl["h"], small["h"])

    def test_more_readout_lines_mean_more_slots(self):
        cl = self.lay["chip"]
        self.assertEqual(len(cl["readouts"]), len(self.chip["readout_lines"]))
        ys = sorted(r["cy"] for r in cl["readouts"])
        for a, b in zip(ys, ys[1:]):
            self.assertGreater(b - a, 28.0, "读取线方框重叠")

    def test_chip_grid_fits_canvas(self):
        cl = self.lay["chip"]
        for q in cl["qubits"]:
            self.assertLessEqual(q["cx"] + cl["r"], cl["w"] - 2)
            self.assertLessEqual(q["cy"] + cl["r"], cl["h"] - 2)
        self.assertGreater(min(q["cx"] for q in cl["qubits"]) - cl["r"],
                           max(r["cx"] + r["w"] / 2 for r in cl["readouts"]),
                           "比特方阵压到了读取线列")

    def test_render_survives_missing_anchor_and_residual(self):
        broken = json.loads(json.dumps(self.data))
        broken["solve"]["anchor"] = None
        broken["report"]["residual"] = None
        html = gen.render(broken)
        self.assertIn('"anchor":null', html.replace(" ", ""))
        self.assertNotIn("__TIME_VB__", html)


class TestNumbersComeFromPipeline(unittest.TestCase):
    """动画里的数字不许手写：默认种子下重跑流水线，必须逐字段还原入库那份数据。"""

    @staticmethod
    def _strip(obj):
        """忽略复现无关项：边上的 kind（JS 不用）、meta.seed（新增凭据）、
        收据里的 pending 路径与 ts 墙钟时间（随机器/时刻变）。"""
        drop = ("kind", "seed", "pending", "ts")
        if isinstance(obj, dict):
            return {k: TestNumbersComeFromPipeline._strip(v) for k, v in obj.items()
                    if k not in drop}
        if isinstance(obj, list):
            return [TestNumbersComeFromPipeline._strip(v) for v in obj]
        return obj

    def test_regeneration_reproduces_shipped_dataset(self):
        exp = load_doc_script("gen_animation_data.py", "gen_animation_data")
        shipped = json.loads((ROOT / "docs" / "animation-data.json").read_text(encoding="utf-8"))
        meta = shipped["meta"]
        chip = generate_chip(num_qubits=4, topology="grid", shape=(2, 2), cavities_per_line=2,
                             tunable=True, seed=7, day=meta["chip"]["date"])
        truth = {e["line"]: float(e["delay"]) for e in generate_wiring(
            chip, kind="truth", seed=9, delay_lo=40.0, delay_hi=200.0)["lines"]}
        work = Path(tempfile.mkdtemp(prefix="qxtrl-anim-test-"))
        pending = work / "pending.json"
        save_wiring(generate_wiring(chip, kind="pending"), pending)
        data = exp.build(chip, truth, pending,
                         th=Thresholds(tol_ns=meta["tol_ns"], d_max_ns=meta["d_max_ns"]),
                         params={"n_shots": meta["n_shots"], "repeats": meta["repeats"],
                                 "noise_ns": meta["noise_ns"], "seed": 8,
                                 "headroom_ns": meta.get("headroom_ns", 0.0)}, write=True)
        self.assertEqual(self._strip(data), self._strip(shipped),
                         "docs/animation-data.json 与真实流水线输出不再一致")


if __name__ == "__main__":
    unittest.main(verbosity=2)

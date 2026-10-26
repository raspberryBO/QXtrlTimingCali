# -*- coding: utf-8 -*-
"""架构图连线可见性：整段穿框、边框重合、标签遮挡与无效连接点。"""
from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gen_architecture", ROOT / "docs/gen_architecture.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


class TestArchitectureGeometry(unittest.TestCase):
    def test_shipped_diagram_matches_checked_generator(self):
        self.assertEqual(gen.selfcheck(), [])
        self.assertEqual(gen.out.read_text(encoding="utf-8"), gen.HTML)
        self.assertIn('orient="auto"', gen.HTML)
        self.assertNotIn('orient="auto-start-end"', gen.HTML)

    def test_segment_crossing_without_internal_vertices_is_rejected(self):
        """两个端点都在框外，整段仍可能穿框；旧顶点检查会漏掉。"""
        edges = copy.deepcopy(gen.edges)
        edges[0]["pts"] = [(20, gen.B1_Y + 40), (340, gen.B1_Y + 40)]
        self.assertTrue(any("连线穿过节点" in e for e in gen.selfcheck(check_edges=edges)))

    def test_old_audit_border_route_is_rejected(self):
        edges = copy.deepcopy(gen.edges)
        audit = edges[-1]
        audit["pts"] = [(608, gen.R3Y + gen.NH), (608, gen.B4_Y), (180, gen.B4_Y)]
        problems = gen.selfcheck(check_edges=edges)
        self.assertTrue(any("writeback-audit.jsonl" in e for e in problems))
        self.assertTrue(any("review-history.json" in e for e in problems))

    def test_labels_cannot_hide_line_or_node(self):
        edges = copy.deepcopy(gen.edges)
        edges[0].update(lx=180, ly=176, anchor="middle")
        self.assertTrue(any("标签遮挡" in e for e in gen.selfcheck(check_edges=edges)))
        edges[0].update(lx=180, ly=gen.B2_Y + 35)
        self.assertTrue(any("标签覆盖节点" in e for e in gen.selfcheck(check_edges=edges)))

    def test_audit_connects_writeback_and_enters_from_above(self):
        audit = gen.edges[-1]
        source, target = audit["ports"]
        self.assertEqual(source, (gen.LC1 + gen.CW1, gen.R3Y + gen.NH / 2))
        self.assertEqual(target, (180, gen.B4_Y))
        self.assertEqual(audit["pts"][-1][0], audit["pts"][-2][0])
        self.assertEqual(target[1] - audit["pts"][-1][1], 4)

    def test_frame_only_source_is_rejected(self):
        edges = copy.deepcopy(gen.edges)
        edges[-1]["ports"] = ((608, gen.R3Y + gen.NH), edges[-1]["ports"][-1])
        self.assertTrue(any("未连接节点边界" in e for e in gen.selfcheck(check_edges=edges)))


if __name__ == "__main__":
    unittest.main()

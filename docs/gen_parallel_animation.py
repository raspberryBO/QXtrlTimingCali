# -*- coding: utf-8 -*-
"""
gen_parallel_animation.py — 并行校准演示动画（4×4 最近邻网格 · 全可调）

`python docs/gen_parallel_animation.py` → docs/demo-animation-parallel.html

数据来自 `gen_parallel_data.py` 真跑分组流水线的导出（`docs/parallel-data.json`），
动画只做重放，不编数字。全局视图同时展示：
* 4×4 最近邻网格的全部 24 个耦合器（横向 12、纵向 12）及按波点亮的测量边；
* 各组波矩阵、提交束及测量进度，理论比值不代表实测加速；
* 64 条信号线的初始延迟、三次写回位移与最终位置；
* 状态、守卫与可展开的测量台账。收尾测量在整批写回后单独演示。

提交束（bundle）不是新造的数字：与 `review/parallel.py` 的 `ParallelPlan.bundles()`
同款规则——波内 pairs 按原顺序、每不超过 `meta.max_parallel` 条切一块。
注意 request 仍是每边一个（`ledger.requests` == 边数），束只是提交分组，文案不混淆两者。

单文件、零依赖、离线可开；播放/暂停、上一幕/下一幕、进度条 seek、0.5/1/2/4× 变速、
重播，空格 / ←→ / R 同样可用。渲染是纯幂等的：每帧由全局时钟算出视图模型再刷属性，
所以拖动进度条、点幕跳转都能正确回放。布局全部在本机用 Python 算好（`build_layout`），
JS 只按像素刷状态；生成后跑 `selfcheck` 复核几何（行距/走廊/出界/束尺寸）。
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "parallel-data.json"
OUT = HERE / "demo-animation-parallel.html"

GROUP_COLOR = {"readout": "#1d7a3c", "qubit": "#2c6cb0",
               "coupler": "#6b3fa0", "readout_final": "#c2410c"}
GROUP_LIGHT = {"readout": "#bfe6cd", "qubit": "#c3dbf2",
               "coupler": "#d9c8ef", "readout_final": "#f6d3bb"}
GROUP_TAG = {"readout": "读", "qubit": "单", "coupler": "耦", "readout_final": "终"}
STAGE_ORDER = ["readout", "qubit", "coupler", "readout_final"]
HOT = "#d97706"                      # 正在测量 / 当前波的高亮色

# ---------------- 芯片图几何：每链一行 = 读取线槽 → 间隙 → 比特行 ----------------
Q_R = 14.0                           # 比特半径
Q_DX = 78.0                          # 列距
LANE_H = 104.0                       # 行距（读取线框 26 + 间隙 30 + 比特 28 + 底部通道 20）
RL_H = 26.0                          # 读取线框高
RL_EXT = 30.0                        # 框比首末比特中心外扩
RL_UP = 60.0                         # 读取线框顶在比特中心上方多少
Q_X0, Q_Y0 = 130.0, 82.0             # 第 0 列比特中心 / 第 0 行比特中心
MIN_RL_GAP = 14.0                    # 读取线框与上一行比特底缘的最小间隙（自检钉）
CHIP_W, CHIP_H = 448.0, 424.0        # 芯片图画布

# 波矩阵 / 甘特画布
PANEL_W = 560.0
CELL_W, CELL_H = 60.0, 40.0
GANTT_X0, GANTT_X1 = 118.0, 548.0


# ---------------- 布局 ----------------

def chip_layout(chip: dict) -> dict:
    """4×4 最近邻网格：每行一条读取线，读取线放左侧，纵向耦合保持可见。"""
    cols = 4
    rows = (len(chip["qubits"]) + cols - 1) // cols
    qubits = {}
    for q in chip["qubits"]:
        i = int(q["id"][1:])
        r, c = divmod(i, cols)
        qubits[q["id"]] = (Q_X0 + c * Q_DX, Q_Y0 + r * LANE_H)
    couplers = {cp["id"]: cp["qubits"] for cp in chip["couplers"]}
    intra, bridges = [], []
    for cpid, (qa, qb) in couplers.items():
        (xa, ya), (xb, yb) = qubits[qa], qubits[qb]
        if ya == yb:
            intra.append({"id": cpid, "lane": int(qa[1:]) // cols,
                          "x1": xa + Q_R + 2, "x2": xb - Q_R - 2,
                          "mx": (xa + xb) / 2, "my": ya})
        else:
            # 链间桥：CP.z 挂低编号比特 ⇒ 锚点取行号小的那一端
            top_q, bot_q = (qa, qb) if ya < yb else (qb, qa)
            bridges.append({"id": cpid, "lane": int(top_q[1:]) // cols,
                            "qa": top_q, "qb": bot_q})
    readouts = []
    for li, rl in enumerate(chip["readout_lines"]):
        first, last = rl["cavities"][0], rl["cavities"][-1]
        qa = next(c["qubit"] for c in chip["cavities"] if c["id"] == first)
        qb = next(c["qubit"] for c in chip["cavities"] if c["id"] == last)
        x0, x1 = 8.0, 88.0
        readouts.append({"id": rl["id"], "lane": li, "x": x0,
                         "y": qubits[qa][1] - 25, "w": x1 - x0, "h": 36,
                         "cy": qubits[qa][1],
                         "qubits": [c["qubit"] for c in chip["cavities"]
                                    if c["id"] in rl["cavities"]]})
    return {"qubits": qubits, "intra": intra, "bridges": bridges, "readouts": readouts,
            "rows": rows, "cols": cols, "w": CHIP_W, "h": CHIP_H}


def bridge_route(a: tuple[float, float], b: tuple[float, float]) -> list:
    """桥接边折线：行间带 → 左侧走廊（避开下一行的读取线框）→ 目标上方。"""
    if abs(a[0] - b[0]) < 1:
        side = a[0] - 23
        return [a, (side, a[1]), (side, b[1]), b]
    corr_x = b[0] - 36.0
    return [a, (corr_x, b[1] - 49.0), (corr_x - 34.0, b[1] - 9.0), b]


def line_anchors(lay: dict, chip: dict) -> dict[str, tuple[float, float]]:
    """每条信号线在芯片图上的锚点（测量边画在两个锚点之间）。"""
    a: dict[str, tuple[float, float]] = {}
    for qid, (x, y) in lay["qubits"].items():
        a[f"{qid}.xy"] = (x, y + Q_R + 3)
        a[f"{qid}.z"] = (x, y - Q_R - 3)
    for c in lay["intra"]:                       # 链内耦合器：连接线中点
        a[f"{c['id']}.z"] = (c["mx"], c["my"])
    for b in lay["bridges"]:                     # 纵向耦合器位于物理连接中点
        xa, ya = lay["qubits"][b["qa"]]
        xb, yb = lay["qubits"][b["qb"]]
        a[f"{b['id']}.z"] = ((xa + xb) / 2, (ya + yb) / 2)
    for rl in lay["readouts"]:
        a[f"{rl['id']}.ro"] = (rl["x"] + 10.0, rl["cy"])
        a[f"{rl['id']}.ri"] = (rl["x"] + rl["w"] - 10.0, rl["cy"])
    return a


def _edge_shape(stage: str, an: str, bn: str, p: tuple, q: tuple) -> str:
    """按边类型给 SVG path：比特弧 / 桥接折线 / 收尾曲线 / 直线。"""
    if stage == "qubit":                         # 同一比特 z→xy：右半圆弧
        r = Q_R + 3
        return f"M{p[0]:.1f},{p[1]:.1f} A{r:.1f},{r:.1f} 0 0 1 {q[0]:.1f},{q[1]:.1f}"
    if stage == "readout_final":                 # 读取线框 → 首腔比特：贴左走廊的曲线
        return (f"M{p[0]:.1f},{p[1]:.1f} C{p[0] + 32:.1f},{p[1] + 17:.1f} "
                f"{q[0] - 36:.1f},{q[1] - 13:.1f} {q[0]:.1f},{q[1]:.1f}")
    if (stage == "coupler" and abs(p[0] - q[0]) < 1
            and abs(p[1] - q[1]) > 1):           # 纵向测量绕开实心比特圆
        pts = bridge_route(p, q)
        return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    return f"M{p[0]:.1f},{p[1]:.1f} L{q[0]:.1f},{q[1]:.1f}"


def _chip_pos(stage: str, an: str, bn: str, p: tuple, q: tuple) -> tuple[float, float]:
    """skew 结果小徽标的落点（避开节点与文字）。注意锚点在比特中心 ±17px，
    同行 z-z 的徽标要落到「读取线框底(48) 与 圈顶(68) 之间的行带」⇒ y = 锚y − 8。"""
    if stage == "qubit":
        return (p[0] + Q_R + 8, (p[1] + q[1]) / 2 - 9)
    if stage == "readout":
        return ((p[0] + q[0]) / 2, p[1] + 9)
    if stage == "readout_final":
        return ((p[0] + q[0]) / 2, q[1] + 8)
    if abs(p[0] - q[0]) < 1 and abs(p[1] - q[1]) > 1:
        return (p[0] - 35, (p[1] + q[1]) / 2)
    if an.startswith("CP") or bn.startswith("CP"):
        return ((p[0] + q[0]) / 2 + 8, (p[1] + q[1]) / 2 - 6)
    if abs(p[1] - q[1]) > 1:                     # 桥接折线：走廊上方
        return (q[0] - 36, q[1] - 62)
    return ((p[0] + q[0]) / 2, p[1] - 8)


def build_meas(data: dict) -> tuple[list[dict], list[dict]]:
    """测量边拉平成提交顺序（= request 顺序），并按 `ParallelPlan.bundles()`
    的同款规则标上"第几束 / 波内第几条"；同时导出全局波序元数据。"""
    mp = int(data["meta"].get("max_parallel") or 0)
    meas, wmeta = [], []
    for st in data["stages"]:
        wv = st.get("waves") or {}
        bsize = int(wv.get("bundle_size") or mp)
        for w in (wv.get("waves") or []):
            pairs = w["pairs"]
            bs = bsize if bsize > 0 else len(pairs)
            bundles, slot = {}, {}
            for bi, i in enumerate(range(0, len(pairs), bs)):
                for wi, p in enumerate(pairs[i:i + bs]):
                    bundles[frozenset((p["a"], p["b"]))] = bi + 1
                    slot[frozenset((p["a"], p["b"]))] = i + wi
            wmeta.append({"gi": len(wmeta), "stage": st["name"], "index": w["index"],
                          "size": len(pairs), "bundles":
                          [len(pairs[i:i + bs]) for i in range(0, len(pairs), bs)],
                          "why": w.get("why", ""),
                          "lines": sorted({ln for p in pairs for ln in (p["a"], p["b"])})})
            for m in st["measurements"]:
                if not m["ok"] or m.get("wave") != w["index"]:
                    continue
                key = frozenset((m["a"], m["b"]))
                meas.append({"mi": len(meas), "stage": st["name"],
                             "tag": GROUP_TAG[st["name"]],
                             "a": m["a"], "b": m["b"], "wave": w["index"],
                             "bundle": bundles.get(key, 1), "wi": slot.get(key, 0),
                             "gi": wmeta[-1]["gi"],
                             "skew": m["skew_ns"], "sigma": m["sigma_ns"],
                             "q": m["quality"], "req": m["request_id"],
                             "hash": m["config_hash"]})
    return meas, wmeta


def build_layout(data: dict) -> dict:
    chip = data["chip"]
    lay = chip_layout(chip)
    lay["anchors"] = line_anchors(lay, chip)
    meas, wmeta = build_meas(data)
    lay["meas"], lay["waves_meta"] = meas, wmeta
    n = len(meas)
    gi_bundles = {w["gi"]: w["bundles"] for w in wmeta}

    # ---- 波矩阵（右上）----
    mx, row_h = 170.0, 48.0
    max_waves = max(len((st.get("waves") or {}).get("waves", [])) for st in data["stages"])
    cell_w = min(CELL_W, (PANEL_W - mx - 8) / max(1, max_waves) - 4)
    matrix = {"x": 0.0, "y": 0.0, "w": PANEL_W, "rows": [], "h": 0.0}
    y = 30.0
    for st in data["stages"]:
        name = st["name"]
        waves = (st.get("waves") or {}).get("waves", [])
        note = {"readout": "同线 ri↔ro · 测完立刻写回",
                "qubit": "互不共享线名 ⇒ 同波分束提交",
                "coupler": f"共享 Q.z ⇒ 切成 {len(waves)} 波",
                "readout_final": "收回 headroom · 拉回对齐"}[name]
        cells = []
        for w in waves:
            gi = next(m["gi"] for m in wmeta
                      if m["stage"] == name and m["index"] == w["index"])
            cells.append({"x": mx + (w["index"] - 1) * (cell_w + 4), "y": y + 2,
                          "w": cell_w, "h": CELL_H, "index": w["index"],
                          "size": len(w["pairs"]), "bundles": gi_bundles[gi], "gi": gi})
        matrix["rows"].append({"stage": name, "seq": len(st["pairs"]),
                               "wave_count": len(waves), "note": note,
                               "y": y, "cells": cells})
        y += row_h
    matrix["h"] = y + 6.0

    # ---- 串行 vs 并行甘特（右中）----
    tw = GANTT_X1 - GANTT_X0
    timeline = {"x": 0.0, "y": 0.0, "w": PANEL_W, "h": 132.0,
                "x0": GANTT_X0, "x1": GANTT_X1, "cells": [], "ticks": []}
    pitch = tw / max(1, n)
    for i, m in enumerate(meas):
        timeline["ticks"].append({"x": GANTT_X0 + i * pitch, "y": 22.0,
                                  "w": pitch - 1.0, "h": 12.0, "stage": m["stage"]})
    xx = GANTT_X0
    for wm in wmeta:
        w = wm["size"] / max(1, n) * tw
        timeline["cells"].append({"x": xx, "y": 50.0, "w": w, "h": 26.0, **wm})
        xx += w
    lay["matrix"] = matrix
    lay["timeline"] = timeline
    lay["canvas"] = {"w": CHIP_W, "h": CHIP_H}
    return lay


# ---------------- 场景 ----------------

def build_scenes(data: dict) -> list[dict]:
    s = data["summary"]
    meta = data["meta"]
    seq, par = s["rounds_sequential"], s["rounds_parallel"]
    stages = {x["name"]: x for x in data["stages"]}
    nq, nc = len(stages["qubit"]["pairs"]), len(stages["coupler"]["pairs"])
    return [
        {"id": "chip", "name": "① 芯片", "title": "① 芯片：4×4 最近邻全连接网格",
         "sub": "16 个全可调比特 · 24 个耦合器覆盖全部上下、左右相邻连接 · 每行一条读取线（4 腔，多音读取）。",
         "dur": 4.0},
        {"id": "truth", "name": "② 真值", "title": "② 真值与待测文件",
         "sub": f"隐藏真值 L：{meta['lines']} 条信号线随机落在 40–200 ns（初始失配 160 ns "
                f"量级）；待测文件 delay 全零，校准只右移，headroom 可按余额收回。",
         "dur": 3.0},
        {"id": "tree", "name": "③ 选边", "title": "③ 选边：每组只测「最少且够用」的一组",
         "sub": f"最小生成树：N 条线只需 N−1 条边。四组合计 {seq} 条边，按物理依赖分四组"
                f"（读 → 单 → 耦 → 终）——这就是「串行要 {seq} 次」的由来。",
         "dur": 5.0},
        {"id": "waves", "name": "④ 波划分", "title": "④ 波划分：哪些边可以同时测",
         "sub": "冲突只有一条——两条边共享线名。同一读取线上的 4 个比特可多音读取分别解调，"
                f"所以单比特组 16 条边可同波，按每束最多 {meta['max_parallel']} 条分批提交。",
         "dur": 7.0},
        {"id": "measure", "name": "⑤ 按波测量", "title": f"⑤ 按波测量：{par} 波 / {s['bundles_total']} 束 / {seq} 条边",
         "sub": f"同波按每束 ≤{meta['max_parallel']} 条提交，收齐一束后推进下一束。"
                "执行方可并发或串行，结果按 request_id 归位；每组内保持同一 config_hash，"
                "单比特与耦合器组共用基线；组间写回后刷新指纹。",
         "dur": 16.0},
        {"id": "solve", "name": "⑥ 求解", "title": "⑥ 一次全局最小二乘",
         "sub": f"{nq + nc} 条 qubit + coupler 观测按 1/σ² 一次解完（读取线由收尾步单独挂接），"
                f"锚线取当前最右的 {s['anchor']} ⇒ 逐线 δ = max(o) − o_i ≥ 0。",
         "dur": 5.0},
        {"id": "write", "name": "⑦ 写回", "title": "⑦ 整批一次写回（+ headroom 收回）",
         "sub": "单比特与耦合器统一补偿走四道守卫：任一条线超预算则整批一个字节都不写。",
         "dur": 5.0},
        {"id": "finish", "name": "⑧ 收尾", "title": "⑧ 读取线收尾测量与余量收回",
         "sub": "先完成单比特与耦合器写回，再测 ro↔xy；读取线按实测结果收回 headroom，向左拉回对齐。",
         "dur": 6.0},
        {"id": "audit", "name": "⑨ 对账", "title": "⑨ 对账：波数、提交束与理论比值",
         "sub": (f"{seq} 次测量 / {par} 个冲突波 / {s['bundles_total']} 个提交束；"
                 f"各实验等时、束内完全并发且忽略开销时，理论比值 {s['ideal_bundle_speedup']}×，非实测。残差 "
                 f"{s['residual_ns']:.3f} ns ≤ tol {meta['tol_ns']:g} ns · "
                 f"aligned={s['aligned']}。模拟执行方串行计算，未测量真实耗时。"),
         "dur": 6.0, "final": True},
    ]


# ---------------- SVG 生成 ----------------

def esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def svg_chip(lay: dict, chip: dict) -> str:
    out = [f'<svg id="chip" viewBox="0 0 {lay["w"]:.0f} {lay["h"]:.0f}" '
           f'xmlns="http://www.w3.org/2000/svg">']
    qrow = {qid: int(qid[1:]) // lay["cols"] for qid in lay["qubits"]}
    # 逐行结构（场景①按行点亮）：读取线框 → 链内耦合器 → 桥接耦合器
    for r in range(lay["rows"]):
        out.append(f'<g class="lane" data-lane="{r}">')
        for rl in lay["readouts"]:
            if rl["lane"] != r:
                continue
            out.append(f'<rect class="rl" x="{rl["x"]:.1f}" y="{rl["y"]:.1f}" '
                       f'width="{rl["w"]:.1f}" height="{rl["h"]:.0f}" rx="7"/>')
            cy = rl["cy"]
            out.append(f'<text class="rl-t" x="{rl["x"] + 9:.1f}" y="{rl["y"] + 11:.1f}">'
                       f'{esc(rl["id"])} · {len(rl["qubits"])} 腔</text>')
            out.append(f'<circle class="port-ro" cx="{rl["x"] + 10:.1f}" cy="{cy:.1f}" '
                       f'r="3.5"/>')
            out.append(f'<circle class="port-ri" cx="{rl["x"] + rl["w"] - 10:.1f}" '
                       f'cy="{cy:.1f}" r="3.5"/>')
            for k in range(1, len(rl["qubits"])):      # 腔间刻度
                tx = rl["x"] + 10 + (rl["w"] - 20) * k / len(rl["qubits"])
                out.append(f'<line class="cav-t" x1="{tx:.1f}" y1="{cy - 6:.1f}" '
                           f'x2="{tx:.1f}" y2="{cy + 6:.1f}"/>')
        for c in lay["intra"]:
            if c["lane"] != r:
                continue
            out.append(f'<line class="intra" x1="{c["x1"]:.1f}" y1="{c["my"]:.1f}" '
                       f'x2="{c["x2"]:.1f}" y2="{c["my"]:.1f}"/>')
            out.append(f'<rect class="cpnode" x="{c["mx"] - 3.5:.1f}" '
                       f'y="{c["my"] - 3.5:.1f}" width="7" height="7" rx="1.5"/>')
            out.append(f'<text class="cp-t" x="{c["mx"]:.1f}" y="{c["my"] + 15:.1f}">'
                       f'{esc(c["id"])}</text>')
        for b in lay["bridges"]:
            if b["lane"] != r:
                continue
            xa, ya = lay["qubits"][b["qa"]]
            xb, yb = lay["qubits"][b["qb"]]
            d = f"M{xa:.1f},{ya + Q_R:.1f} L{xb:.1f},{yb - Q_R:.1f}"
            ax, ay = lay["anchors"][f"{b['id']}.z"]
            out.append(f'<path class="bridgeline" d="{d}"/>')
            out.append(f'<rect class="cpnode" x="{ax - 3.5:.1f}" y="{ay - 3.5:.1f}" '
                       f'width="7" height="7" rx="1.5"/>')
            out.append(f'<text class="cp-t" x="{ax + 7:.1f}" y="{ay + 3:.1f}" style="text-anchor:start">'
                       f'{esc(b["id"])}</text>')
        out.append("</g>")
    # 测量边（提交顺序 = mi）+ skew 结果徽标
    for m in lay["meas"]:
        p, q = lay["anchors"][m["a"]], lay["anchors"][m["b"]]
        d = _edge_shape(m["stage"], m["a"], m["b"], p, q)
        out.append(f'<path class="edge" data-stage="{m["stage"]}" data-wave="{m["wave"]}" '
                   f'data-gi="{m["gi"]}" data-mi="{m["mi"]}" d="{d}" '
                   f'stroke="{GROUP_COLOR[m["stage"]]}"/>')
        cx, cy = _chip_pos(m["stage"], m["a"], m["b"], p, q)
        out.append(f'<text class="chip-t" data-mi="{m["mi"]}" x="{cx:.1f}" y="{cy:.1f}" '
                   f'fill="{GROUP_COLOR[m["stage"]]}">{m["skew"]:+.1f}</text>')
    # 比特（最后画，压在边上面）+ 当前波高亮环 —— 按行分组，场景①逐行点亮
    for r in range(lay["rows"]):
        out.append(f'<g class="lane" data-lane="{r}">')
        for qid, (x, y) in lay["qubits"].items():
            if qrow[qid] != r:
                continue
            out.append(f'<circle class="ring" data-lines="{qid}.xy {qid}.z" '
                       f'cx="{x:.1f}" cy="{y:.1f}" r="{Q_R + 4.5:.1f}"/>')
            out.append(f'<circle class="q" cx="{x:.1f}" cy="{y:.1f}" r="{Q_R:.0f}"/>')
            out.append(f'<text class="q-t" x="{x:.1f}" y="{y + 3.5:.1f}">{esc(qid)}</text>')
        out.append("</g>")
    for rl in lay["readouts"]:
        out.append(f'<rect class="ring" data-lines="{rl["id"]}.ro {rl["id"]}.ri" '
                   f'x="{rl["x"] - 4:.1f}" y="{rl["y"] - 4:.1f}" '
                   f'width="{rl["w"] + 8:.1f}" height="{rl["h"] + 8:.0f}" rx="9"/>')
    for c in lay["intra"]:
        out.append(f'<rect class="ring" data-lines="{c["id"]}.z" '
                   f'x="{c["mx"] - 6.5:.1f}" y="{c["my"] - 6.5:.1f}" width="13" '
                   f'height="13" rx="3"/>')
    for b in lay["bridges"]:
        ax, ay = lay["anchors"][f"{b['id']}.z"]
        out.append(f'<rect class="ring" data-lines="{b["id"]}.z" '
                   f'x="{ax - 6.5:.1f}" y="{ay - 6.5:.1f}" width="13" height="13" rx="3"/>')
    out.append("</svg>")
    return "".join(out)


def svg_matrix(lay: dict) -> str:
    m = lay["matrix"]
    out = [f'<svg id="matrix" viewBox="0 0 {m["w"]:.0f} {m["h"]:.0f}" '
           f'xmlns="http://www.w3.org/2000/svg">']
    out.append('<text class="h-m" x="0" y="16">波矩阵 · 串行次数 → 波数与每波条数</text>')
    for row in m["rows"]:
        color = GROUP_COLOR[row["stage"]]
        out.append(f'<text class="row-t" x="0" y="{row["y"] + 16:.0f}" '
                   f'fill="{color}">{esc(row["stage"])}</text>')
        out.append(f'<text class="row-n" x="0" y="{row["y"] + 29:.0f}">'
                   f'{esc(row["note"])}</text>')
        out.append(f'<text class="row-s" x="0" y="{row["y"] + 42:.0f}" fill="{color}">'
                   f'{row["seq"]} 次 → {row["wave_count"]} 波</text>')
        for c in row["cells"]:
            nb = "+".join(str(x) for x in c["bundles"])
            out.append(f'<g class="cell" data-stage="{row["stage"]}" '
                       f'data-index="{c["index"]}" role="button" tabindex="0" '
                       f'aria-label="播放 {esc(row["stage"])} 第 {c["index"]} 波">'
                       f'<rect class="cellbg" x="{c["x"]:.1f}" y="{c["y"]:.1f}" '
                       f'width="{c["w"]:.0f}" height="{c["h"]:.0f}" rx="7"/>'
                       f'<rect class="cellfill" fill="{color}" x="{c["x"]:.1f}" '
                       f'y="{c["y"]:.1f}" width="0" height="{c["h"]:.0f}" rx="7"/>'
                       f'<text class="cell-t" x="{c["x"] + c["w"] / 2:.1f}" '
                       f'y="{c["y"] + 16:.1f}" fill="{color}">波 {c["index"]}</text>'
                       f'<text class="cell-n" x="{c["x"] + c["w"] / 2:.1f}" '
                       f'y="{c["y"] + 30:.1f}" fill="{color}">{c["size"]} 条·'
                       f'{len(c["bundles"])} 束</text>'
                       f'<title>{esc(row["stage"])} 波 {c["index"]}：{c["size"]} 条 · '
                       f'提交束 {nb}</title></g>')
    out.append("</svg>")
    return "".join(out)


def svg_timeline(lay: dict) -> str:
    t = lay["timeline"]
    n, nw = len(t["ticks"]), len(t["cells"])
    out = [f'<svg id="gantt" viewBox="0 0 {t["w"]:.0f} {t["h"]:.0f}" '
           f'xmlns="http://www.w3.org/2000/svg">']
    out.append('<text class="h-m" x="0" y="12">测量边进度 · 段宽按边数，非耗时</text>')
    out.append(f'<text class="gh" x="0" y="31">串行 {n} 次</text>')
    for i, tk in enumerate(t["ticks"]):
        out.append(f'<rect class="tickA" data-i="{i}" fill="{GROUP_COLOR[tk["stage"]]}" '
                   f'x="{tk["x"]:.1f}" y="{tk["y"]:.0f}" width="{tk["w"]:.2f}" '
                   f'height="{tk["h"]:.0f}" rx="1.5"/>')
    out.append(f'<text class="gh" x="0" y="67">并行 {nw} 波</text>')
    for c in t["cells"]:
        color = GROUP_COLOR[c["stage"]]
        seps = []
        acc = 0
        for k in range(len(c["bundles"]) - 1):
            acc += c["bundles"][k]
            sx = c["x"] + c["w"] * acc / c["size"]
            seps.append(f'<line x1="{sx:.1f}" y1="{c["y"]:.0f}" x2="{sx:.1f}" '
                        f'y2="{c["y"] + c["h"]:.0f}" stroke="#ffffff" stroke-width="1.5"/>')
        out.append(f'<g class="seg" data-gi="{c["gi"]}" role="button" tabindex="0" '
                   f'aria-label="播放 {esc(c["stage"])} 第 {c["index"]} 波">'
                   f'<rect class="cellbg" x="{c["x"]:.1f}" y="{c["y"]:.0f}" '
                   f'width="{c["w"]:.1f}" height="{c["h"]:.0f}" rx="4"/>'
                   f'<rect class="cellfill" fill="{color}" x="{c["x"]:.1f}" '
                   f'y="{c["y"]:.0f}" width="0" height="{c["h"]:.0f}" rx="4"/>'
                   + "".join(seps) +
                   f'<title>{esc(c["stage"])} 波 {c["index"]}：{c["size"]} 条 · '
                   f'提交束 {"+".join(str(x) for x in c["bundles"])}</title></g>')
        out.append(f'<text class="seg-t" x="{c["x"] + c["w"] / 2:.1f}" '
                   f'y="{c["y"] + 40:.0f}" fill="{color}">'
                   f'{GROUP_TAG[c["stage"]]}{c["index"]}·{c["size"]}</text>')
    out.append(f'<line id="playhead" class="playhead" x1="0" y1="16" x2="0" y2="92"/>')
    out.append('<text class="gh" x="0" y="106">上下轨同步展示边进度；白线分提交束，不表示耗时。</text>')
    out.append("</svg>")
    return "".join(out)


def calibration_layout(data: dict) -> dict:
    """条形图覆盖初始、中间与最终位置，读取线放在顶部便于看余量收回。"""
    lines = sorted(data["calibration_lines"], key=lambda ln: 0 if ln["name"].endswith((".ro", ".ri")) else 1)
    maximum = max(max(ln["L"], ln["L"] + ln["d1"],
                      ln["L"] + ln["d1"] + ln["d2"], ln["q1"]) for ln in lines)
    return {"lines": lines, "dom": (int(maximum / 50) + 1) * 50,
            "x0": 100, "x1": 650, "step": 20, "y0": 42}


def svg_calibration(data: dict) -> str:
    cal = calibration_layout(data)
    height = cal["y0"] + len(cal["lines"]) * cal["step"] + 12
    out = [f'<svg id="calibration" viewBox="0 0 820 {height}" xmlns="http://www.w3.org/2000/svg">']
    out.append('<g id="calAxis">')
    for value in range(0, cal["dom"] + 1, 50):
        x = cal["x0"] + value / cal["dom"] * (cal["x1"] - cal["x0"])
        out.append(f'<line x1="{x}" x2="{x}" y1="26" y2="{height - 8}" stroke="#e8edf2"/>'
                   f'<text x="{x}" y="18" text-anchor="middle" class="cal-label">{value}</text>')
    out.append('<text x="710" y="18" class="cal-label">q / d（ns）</text>')
    out.append('</g>')
    for i, ln in enumerate(cal["lines"]):
        y = cal["y0"] + i * cal["step"]
        out.append(f'<g class="cal-row" data-i="{i}">'
                   '<title></title>'
                   f'<text x="90" y="{y + 4}" text-anchor="end" class="cal-name cal-label">{esc(ln["name"])}</text>'
                   f'<rect class="cal-base" x="100" y="{y - 5}" height="10" width="0" fill="#c7ced6"/>'
                   f'<rect class="cal-d1" y="{y - 5}" height="10" width="0" fill="#1d7a3c"/>'
                   f'<rect class="cal-d2" y="{y - 5}" height="10" width="0" '
                   f'fill="{GROUP_COLOR["coupler" if ln["name"].startswith("CP") else "qubit"]}"/>'
                   f'<rect class="cal-d3" y="{y - 7}" height="14" width="0" fill="#c2410c" fill-opacity=".55"/>'
                   f'<line class="cal-end" y1="{y - 8}" y2="{y + 8}" stroke="#1f2933"/>'
                   f'<text class="cal-value cal-label" x="682" y="{y + 4}"></text></g>')
    out.append('</svg>')
    return ''.join(out)


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>QXrlTimingCali — 并行校准 Demo 动画（4 链 × 4 比特）</title>
<style>
 *{box-sizing:border-box}
 body{margin:0;padding:10px 12px;background:#fbfcfd;color:#1f2933;
      height:100dvh;display:flex;flex-direction:column;overflow:hidden;gap:5px;
      font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;font-size:13px}
 h1{font-size:19px;margin:0 0 4px}
 .sub{font-size:12.5px;color:#5c6b7a;margin:0 0 12px;line-height:1.7}
 code{background:#eef2f6;padding:1px 4px;border-radius:4px;font-size:12px}
 .ctrl{display:flex;align-items:center;gap:8px;margin:0 0 10px;padding:8px 12px;
       background:#fff;border:1px solid #e3e8ee;border-radius:10px}
 .ctrl button{font:inherit;font-size:12px;padding:4px 10px;border:1px solid #dde3ea;
       border-radius:8px;background:#fff;color:#1f2933;cursor:pointer}
 .ctrl button:hover{border-color:#9fb3c8}
 .ctrl button.pri,.ctrl button.on{background:#2c6cb0;border-color:#2c6cb0;color:#fff}
 input[type=range]{flex:1;accent-color:#2c6cb0}
 .clock{font-variant-numeric:tabular-nums;font-size:12px;color:#5c6b7a;min-width:96px;
        text-align:right}
 .nav{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px}
 .pill{border:1px solid #dde3ea;background:#fff;color:#5c6b7a;border-radius:14px;
       padding:4px 11px;font-size:12px;cursor:pointer}
 .pill:hover{border-color:#9fb3c8}
 .pill.on{background:#2c6cb0;border-color:#2c6cb0;color:#fff;font-weight:600}
 .pill.done{background:#e8f0fa;border-color:#b9cff0;color:#2c6cb0}
 .grid{display:grid;grid-template-columns:minmax(0, .8fr) minmax(0, 1.4fr);
       grid-template-rows:minmax(0, .9fr) minmax(0, 1.15fr) auto;
       gap:7px;flex:1;min-height:0}
 .col{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,.85fr);gap:7px;min-height:0}
 .chip-card{display:flex;flex-direction:column;min-height:0}
 .chip-card #chip{flex:1;min-height:0;width:100%;height:100%;border:0}
 .col .card{display:flex;flex-direction:column;min-height:0}
 .col svg{flex:1;min-height:0;height:100%;border:0}
 .cal-card{grid-column:1 / -1}
 .cal-card{display:flex;flex-direction:column;min-height:0}
 .cal-scroll{flex:1;min-height:0;overflow:hidden}
 .cal-scroll svg{width:100%;height:100%;border:0}
 .cal-label{font-size:11px;fill:#414f5c;font-variant-numeric:tabular-nums}
 .cal-value{display:none}
 dialog{max-width:95vw;border:1px solid #d4dae1;border-radius:10px;padding:16px}
 dialog .scroll{max-height:65vh}
 dialog::backdrop{background:#1f293355}
 .status-card{grid-column:1 / -1;display:grid;grid-template-columns:1fr 1fr;gap:4px 10px}
 .status-card h2,.status-card .scroll,.status-card .rule{display:none}
 .status-card .say{grid-column:1 / -1;margin:0;min-height:0;font-size:10px;line-height:1.4;padding:3px 7px}
 .status-card .badges,.status-card .guards{margin:0;align-items:center}
 .badge{padding:2px 5px!important;font-size:9px!important}
 .guard{font-size:9px!important;padding:3px 1px!important}
 .card{background:#fff;border:1px solid #e3e8ee;border-radius:9px;padding:7px 9px;min-width:0;overflow:hidden}
 .card h2{font-size:13px;margin:0 0 6px;color:#1d4e75}
 .hint{font-size:11.5px;color:#7a8590;margin:0 0 6px;min-height:16px}
 svg{display:block;width:100%;height:auto;background:#fff;border:1px solid #eef1f4;
     border-radius:8px}
 .lane{transition:opacity .3s}
 .rl{fill:#fdf5df;stroke:#a9760a;stroke-width:1.2}
 .rl-t{font-size:10px;fill:#8a6a1e}
 .port-ro{fill:#1d7a3c} .port-ri{fill:#7fb894}
 .cav-t{stroke:#d9c68a;stroke-width:1}
 .intra{stroke:#9db8a0;stroke-width:1.6}
 .cpnode{fill:#f2eafb;stroke:#6b3fa0;stroke-width:1.2}
 .cp-t{font-size:8.5px;fill:#6b3fa0;text-anchor:middle}
 .bridgeline{fill:none;stroke:#9db8a0;stroke-width:1.6}
 .q{fill:#eaf2fb;stroke:#2c6cb0;stroke-width:1.4}
 .q-t{font-size:9.5px;text-anchor:middle;fill:#1f2933}
 .ring{fill:none;stroke:#d97706;stroke-width:2;stroke-dasharray:4 3;opacity:0;
       transition:opacity .2s}
 .edge{opacity:.07;fill:none;transition:opacity .22s}
 .edge.lit{opacity:.95}
 .edge.hot{opacity:1;animation:pulse .8s ease-in-out infinite}
 .chip-t{font-size:8.5px;text-anchor:middle;opacity:0;transition:opacity .2s;
         paint-order:stroke;stroke:#fff;stroke-width:3px;font-weight:600}
 .chip-t.on{opacity:1}
 .h-m{font-size:12.5px;font-weight:700;fill:#1d4e75}
 .row-t{font-size:11.5px;font-weight:700}
 .row-n{font-size:9px;fill:#7a8590}
 .row-s{font-size:10px;font-weight:600}
 .cellbg{fill:#f6f7f9;stroke:#d4dae1}
 .cellfill{opacity:.88;transition:width .15s}
 .cell,.seg{cursor:pointer;outline:none}
 .cell:hover .cellbg,.cell:focus-visible .cellbg,
 .seg:hover .cellbg,.seg:focus-visible .cellbg{stroke:#d97706;stroke-width:2}
 .cell.selected .cellbg,.seg.selected .cellbg{stroke:#d97706;stroke-width:2}
 .cell-t{font-size:9px;text-anchor:middle;font-weight:600;
         paint-order:stroke;stroke:#fff;stroke-width:3px}
 .cell-n{font-size:8.5px;text-anchor:middle;
         paint-order:stroke;stroke:#fff;stroke-width:3px}
 .tickA{opacity:.16;transition:opacity .15s}
 .tickA.lit{opacity:1}
 .gh{font-size:10px;fill:#7a8590}
 .seg-t{font-size:8.5px;text-anchor:middle;font-weight:600}
 .playhead{stroke:#d97706;stroke-width:1.6;opacity:0;transition:opacity .2s}
 table{border-collapse:collapse;font-size:10.5px;width:100%}
 th,td{border-bottom:1px solid #eef2f6;padding:2px 5px;text-align:left;white-space:nowrap}
 th{color:#7a8590;font-weight:600}
 .scroll{max-height:148px;overflow:auto}
 tr.new{background:#fff6e6}
 .badges{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 6px}
 .badge{border:1px solid #dde3ea;border-radius:8px;padding:3px 8px;font-size:11px;
        background:#f6f7f9;color:#5c6b7a}
 .badge b{color:#1f2933}
 .badge.yes{border-color:#a8d5ba;background:#e9f6ec;color:#1d7a3c}
 .guards{display:flex;gap:6px;margin:0 0 8px}
 .guard{flex:1;border:1px solid #dde3ea;border-radius:8px;padding:4px 2px;text-align:center;
        font-size:10.5px;color:#8a95a0;background:#f6f7f9}
 .guard.on{border-color:#a8d5ba;background:#e9f6ec;color:#1d7a3c;font-weight:600}
 .say{font-size:12px;color:#1f2933;line-height:1.7;background:#f6f9fc;
      border-left:3px solid #2c6cb0;padding:6px 10px;border-radius:0 8px 8px 0;min-height:38px}
 .big{display:flex;gap:22px;margin:8px 0 2px}
 .big div{font-size:11.5px;color:#5c6b7a}
 .big b{display:block;font-size:22px;color:#1f2933}
 .legend{display:flex;gap:12px;flex-wrap:wrap;font-size:11px;color:#5c6b7a;margin:8px 0 0}
 .legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;
           vertical-align:-1px}
 .rule{font-size:11.5px;color:#414f5c;background:#eef2f6;border-radius:8px;padding:6px 10px;
       margin:8px 0 0}
 @keyframes pulse{0%{opacity:.45}50%{opacity:1}100%{opacity:.45}}
 h1{font-size:15px;margin:0}
 .sub{font-size:10px;line-height:1.3;margin:0}
 .ctrl{padding:4px 7px;margin:0;gap:5px}
 .ctrl button{font-size:11px;padding:3px 6px}
 .ctrl input{min-width:30px}
 .nav{gap:4px;margin:0;flex-wrap:nowrap}
 .pill{padding:3px 5px;font-size:10px;white-space:nowrap}
 .card h2{font-size:11px;margin:0 0 3px}
 .hint{font-size:10px;margin:0 0 3px;min-height:0;line-height:1.3}
 .legend{font-size:9px;gap:4px 9px;margin:3px 0}
 .big{gap:9px;margin:3px 0;flex-wrap:wrap}
 .big div{font-size:9px}.big b{font-size:15px}
 .chip-card .legend{display:none}
 .col .hint{display:none}
 @media(max-width:950px){.col{grid-template-columns:1fr;grid-template-rows:minmax(0,1.35fr) minmax(0,1fr)}
   .big{display:none}.status-card{grid-template-columns:1fr}.nav{gap:2px}.pill{font-size:9px}}
 @media(max-height:650px){.sub{display:none}.hint{font-size:9px}.say{font-size:9px!important}}
</style></head><body>
<h1>QXtrlTimingCali · 并行校准 Demo（4×4 最近邻全连接 · 全可调）</h1>
<p class="sub">真实流水线数据 · __LINE_COUNT__ 条信号线 · __SEQ_COUNT__ 条测量 / __WAVE_COUNT__ 波 / __BUNDLE_COUNT__ 束 · 全局视图 · 空格暂停，← → 切幕，R 重播</p>
<div class="ctrl">
 <button id="play" class="pri">▶ 播放</button>
 <button id="prev">⏮</button>
 <button id="next">⏭</button>
 <input type="range" id="bar" min="0" max="1000" value="0">
 <span class="clock" id="clock"></span>
 <button id="speed">1×</button>
 <button id="restart">↺ 重播</button>
 <button id="showLedger">台账明细</button>
</div>
<div class="nav" id="nav"></div>
<div class="grid">
 <div class="card chip-card">
  <h2>芯片图 · 测量边按组着色、按波点亮</h2>
  <p class="hint" id="chipHint"></p>
  __CHIP__
  <div class="legend">
   <span><i style="background:#1d7a3c"></i>读取线 ri↔ro</span>
   <span><i style="background:#2c6cb0"></i>单比特 xy↔z</span>
   <span><i style="background:#6b3fa0"></i>耦合器 z（横向 + 纵向）</span>
   <span><i style="background:#c2410c"></i>收尾 ro↔xy</span>
   <span><i style="background:#d97706"></i>正在测量 / 当前波</span>
  </div>
 </div>
 <div class="col">
  <div class="card">
   <h2>并行度 · 波矩阵（点击波次播放）</h2>
   <p class="hint" id="matHint"></p>
   __MATRIX__
  </div>
  <div class="card">
   <h2>调度计数 · 边进度与理论比值</h2>
   <p class="hint" id="ganttHint"></p>
   __GANTT__
   <div class="big" id="big"></div>
  </div>
 </div>
 <div class="card cal-card">
  <h2>逐线校准数据 · q = L + d（ns）</h2>
  <p class="hint" id="calHint"></p>
  <div class="legend">
   <span><i style="background:#c7ced6"></i>初始延迟 L</span>
   <span><i style="background:#1d7a3c"></i>读取线补偿 d₁（含 headroom）</span>
   <span><i style="background:#2c6cb0"></i>单比特 d₂</span>
   <span><i style="background:#6b3fa0"></i>耦合器 d₂</span>
   <span><i style="background:#c2410c"></i>收尾净位移 d₃（橙色覆盖段表示收回）</span>
  </div>
  <div class="cal-scroll">__CALIBRATION__</div>
 </div>
 <div class="card status-card">
  <h2>测量台账 · 状态与守卫</h2>
  <div class="badges" id="badges"></div>
  <div class="guards" id="guards"></div>
  <p class="say" id="say"></p>
 </div>
</div>
<dialog id="ledgerDialog">
 <button id="closeLedger">关闭明细</button>
 <h2>测量台账 · 已完成的实验</h2>
 <div class="scroll" id="lscroll"><table id="ledger"></table></div>
 <p class="rule" id="rule"></p>
</dialog>
<script>
const DATA = __DATA__;
const SCENES = __SCENES__;
const MEAS = DATA.meas, WAVES = DATA.waves_meta, S = DATA.summary, META = DATA.meta;
const COLOR = __COLOR__;
const $ = s => document.querySelector(s);
const clamp01 = t => Math.max(0, Math.min(1, t));
const ease = t => t < .5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
const fmt = (v, n) => Number(v).toFixed(n === undefined ? 1 : n);

const START = [];
SCENES.reduce((acc, sc) => { START.push(acc); return acc + sc.dur; }, 0);
const TOTAL = START[START.length - 1] + SCENES[SCENES.length - 1].dur;
const NT = MEAS.length, NW = WAVES.length;

/* ---------- 静态元素 ---------- */
const chipSvg = $("#chip"), matSvg = $("#matrix"), gSvg = $("#gantt");
const lanes = [...chipSvg.querySelectorAll(".lane")];
const edges = [...chipSvg.querySelectorAll(".edge")];
const chips = [...chipSvg.querySelectorAll(".chip-t")];
const rings = [...chipSvg.querySelectorAll(".ring")];
const cells = [...matSvg.querySelectorAll(".cell")];
const ticks = [...gSvg.querySelectorAll(".tickA")];
const segs = [...gSvg.querySelectorAll(".seg")];
const playhead = $("#playhead");
const waveLines = WAVES.map(w => new Set(w.lines));
const giOf = {};
WAVES.forEach(w => { giOf[w.stage + "|" + w.index] = w.gi; });
const stageFirstGi = {}, stageFirstMi = {};
WAVES.forEach(w => { if (!(w.stage in stageFirstGi)) stageFirstGi[w.stage] = w.gi; });
MEAS.forEach(m => { if (!(m.stage in stageFirstMi)) stageFirstMi[m.stage] = m.mi; });
function waveStartEdge(gi) {
  let n = 0;
  for (const w of WAVES) { if (w.gi === gi) return n; n += w.size; }
  return n;
}

/* ---------- 导航 ---------- */
const nav = $("#nav"), bar = $("#bar"), clockEl = $("#clock");
const playBtn = $("#play"), spdBtn = $("#speed");
$("#showLedger").onclick = () => $("#ledgerDialog").showModal();
$("#closeLedger").onclick = () => $("#ledgerDialog").close();
const pills = SCENES.map((sc, i) => {
  const b = document.createElement("button");
  b.className = "pill"; b.textContent = sc.name; b.title = sc.title;
  b.onclick = () => { setPlay(false); jump(i); };
  nav.appendChild(b); return b;
});
function setPlay(on) {
  if (on && wavePlayback && wavePlayback.done) {
    T = waveTime(wavePlayback.gi); wavePlayback.done = false;
  }
  if (on && T >= TOTAL - 1e-3) T = 0;
  playing = on; last = 0;
  playBtn.textContent = on ? "⏸ 暂停" : "▶ 播放";
}
function waveTime(gi, end = false) {
  const wave = WAVES[gi];
  if (!wave) return null;
  const final = wave.stage === "readout_final";
  const scene = SCENES.findIndex(sc => sc.id === (final ? "finish" : "measure"));
  const first = final ? stageFirstMi.readout_final : 0;
  const count = final ? NT - first : stageFirstMi.readout_final;
  // 与 vm 的逐边进度反解一致，确保点击后立即进入所选波的首条测量。
  const offset = end ? wave.size : .01;
  const p = (waveStartEdge(gi) - first + offset) / (count + 1) * (final ? .55 : .97);
  return START[scene] + SCENES[scene].dur * p;
}
function playWave(gi) {
  const time = waveTime(gi);
  if (time === null) return;
  wavePlayback = {gi, end: waveTime(gi, true), done: false};
  T = time; setPlay(true); draw();
}
function bindWave(el, gi) {
  el.onclick = () => playWave(gi);
  el.onkeydown = e => {
    if (e.code === "Enter" || e.code === "Space") {
      e.preventDefault(); e.stopPropagation(); playWave(gi);
    }
  };
}
cells.forEach(el => bindWave(el, giOf[el.dataset.stage + "|" + el.dataset.index]));
segs.forEach(el => bindWave(el, +el.dataset.gi));
playBtn.onclick = () => setPlay(!playing);
$("#prev").onclick = () => { setPlay(false); jump(curIndex() - 1); };
$("#next").onclick = () => { setPlay(false); jump(curIndex() + 1); };
$("#restart").onclick = () => { wavePlayback = null; T = 0; setPlay(true); };
spdBtn.onclick = () => {
  rate = rate === 1 ? 2 : (rate === 2 ? 4 : (rate === 4 ? .5 : 1));
  spdBtn.textContent = rate + "×";
};
bar.oninput = () => { wavePlayback = null; setPlay(false); T = (bar.value / 1000) * TOTAL; draw(); };
addEventListener("keydown", e => {
  if (e.code === "Space") { e.preventDefault(); setPlay(!playing); }
  else if (e.code === "ArrowRight") $("#next").onclick();
  else if (e.code === "ArrowLeft") $("#prev").onclick();
  else if (e.key === "r" || e.key === "R") $("#restart").onclick();
});

/* ---------- 时钟 ---------- */
let T = 0, playing = false, rate = 1, last = 0;
let wavePlayback = null;
function curIndex() {
  let i = 0;
  for (let k = 0; k < START.length; k++) if (T >= START[k]) i = k;
  return i;
}
function jump(i) {
  wavePlayback = null;
  i = Math.max(0, Math.min(SCENES.length - 1, i));
  T = START[i] + 1e-6; draw();
}

/* ---------- 视图模型：全局时钟 → 幂等状态 ---------- */
function vm(t) {
  const i = curIndex(), sc = SCENES[i], p = clamp01((t - START[i]) / sc.dur);
  const v = { id: sc.id, p, i, k: 0, hot: -1, waveFull: -1, waveFrac: 0,
              guards: 0, written: "", applied: false,
              hintC: "", hintM: "", hintG: "" };
  if (v.id === "chip") {
    v.hintC = "比特 / 读取线 / 耦合器逐行就位";
  } else if (v.id === "truth") {
    v.hintC = "结构不变，等待测量";
  } else if (v.id === "tree") {
    v.k = Math.floor(ease(clamp01((p - .08) / .86)) * NT);
    v.hot = v.k < NT ? v.k : -1;
    v.hintC = "已选 " + v.k + " / " + NT + " 条边（按组：读 → 单 → 耦 → 终）";
    v.hintM = "四组各自的「串行 N 次」";
    v.hintG = "串行逐条提交的节奏（上轨）";
  } else if (v.id === "waves") {
    const jw = clamp01(p / .92) * NW;
    v.waveFull = Math.min(NW - 1, Math.floor(jw));
    v.waveFrac = jw - v.waveFull;
    const wm = WAVES[v.waveFull];
    v.hintC = "第 " + wm.index + " 波（" + wm.stage + " · " + wm.size + " 条）一起提交："
      + wm.why;
    v.hintM = "波逐个点亮 · 格内 = 条数 · 束数";
    v.hintG = NW + " 段波逐段点亮（下轨）";
  } else if (v.id === "measure") {
    const stop = stageFirstMi.readout_final;
    v.k = Math.min(stop, Math.floor(clamp01(p / .97) * (stop + 1)));
    v.hot = v.k < stop ? v.k : -1;
    v.waveFull = v.hot >= 0 ? MEAS[v.hot].gi : stageFirstGi.readout_final - 1;
    v.waveFrac = v.hot >= 0
      ? (v.k - waveStartEdge(v.waveFull)) / WAVES[v.waveFull].size : 1;
    v.hintC = v.hot >= 0
      ? "在测：" + MEAS[v.hot].a + " ⇄ " + MEAS[v.hot].b + "（" + MEAS[v.hot].req + "）"
      : stop + " 条读取线 / 单比特 / 耦合器测量完成，收尾等待整批写回";
    v.hintM = "全局第 " + (v.waveFull + 1) + "/" + NW + " 波 · 已提交 " + v.k + "/" + NT;
    v.hintG = "同一进度尺：串行已走 " + v.k + " 次 = 并行第 " + (v.waveFull + 1) + " 波";
    v.written = v.k >= stageFirstMi.qubit
      ? "readout 已落盘 · qubit/coupler 只记录" : "readout 测完立刻写回";
  } else if (v.id === "solve") {
    v.k = stageFirstMi.readout_final; v.waveFull = stageFirstGi.readout_final - 1; v.waveFrac = 1;
    v.hintC = "单比特与耦合器观测就位，统一求解";
    v.hintM = "收尾组等待整批写回";
    v.hintG = "求解得到逐线补偿计划，尚未落盘";
  } else if (v.id === "write") {
    v.k = stageFirstMi.readout_final; v.waveFull = stageFirstGi.readout_final - 1; v.waveFrac = 1;
    v.guards = Math.floor(clamp01(p / .55) * 4);
    v.written = p >= .55 ? "单比特 / 耦合器整批已落盘" : "四道守卫检查中…";
    v.applied = p >= .85;
    v.hintG = p >= .55 ? "一张 WritePlan 一次写完" : "守卫未过 = 一个字节都不写";
  } else if (v.id === "finish") {
    const start = stageFirstMi.readout_final;
    v.k = start + Math.min(NT - start, Math.floor(clamp01(p / .55) * (NT - start + 1)));
    v.hot = v.k < NT ? v.k : -1;
    v.waveFull = v.hot >= 0 ? MEAS[v.hot].gi : NW - 1;
    v.waveFrac = v.hot >= 0 ? (v.k - waveStartEdge(v.waveFull)) / WAVES[v.waveFull].size : 1;
    v.guards = p >= .6 ? 4 : 0;
    v.written = p >= .6 ? "收尾已落盘 · headroom 已收回" : "读取线收尾测量中";
    v.applied = p >= .9;
    v.hintC = v.hot >= 0 ? "收尾：" + MEAS[v.hot].a + " ⇄ " + MEAS[v.hot].b : "读取线收回余量，向左拉回";
    v.hintM = "最后一组在整批写回之后测量";
    v.hintG = "收齐收尾结果后单独求解、写回并下发";
  } else {
    v.k = NT; v.waveFull = NW - 1; v.waveFrac = 1;
    v.guards = 4; v.written = "全部落盘"; v.applied = true;
    v.hintG = NT + " 次测量 / " + NW + " 冲突波 / " + S.bundles_total + " 提交束；非耗时图";
  }
  if (wavePlayback && wavePlayback.done) {
    const wave = WAVES[wavePlayback.gi];
    v.k = waveStartEdge(wavePlayback.gi) + wave.size;
    v.hot = -1; v.waveFull = wavePlayback.gi; v.waveFrac = 1;
    v.hintC = wave.stage + " 第 " + wave.index + " 波已完成，自动暂停";
    v.hintM = "点击其他波次播放；点击播放可重放当前波";
  }
  v.cal = calibrationState(v);
  return v;
}

function calibrationState(v) {
  let a = 0, b = 0, c = 0, plan = false;
  if (v.id === "measure") a = v.k >= stageFirstMi.qubit ? 1 : 0;
  if (["solve", "write", "finish", "audit"].includes(v.id)) a = 1;
  if (v.id === "solve") { b = ease(v.p); plan = true; }
  if (v.id === "write") { b = v.p >= .55 ? ease(clamp01((v.p - .55) / .3)) : 1; plan = v.p < .55; }
  if (["finish", "audit"].includes(v.id)) b = 1;
  if (v.id === "finish") c = ease(clamp01((v.p - .6) / .3));
  if (v.id === "audit") c = 1;
  return {a, b, c, plan};
}

/* ---------- 台账 / 徽标 / 守卫（静态构建一次） ---------- */
const ledTab = $("#ledger"), lscroll = $("#lscroll");
const ledHead = document.createElement("tr");
["幕", "波", "束", "边", "skew ns", "σ ns", "quality", "request_id"].forEach(h => {
  const th = document.createElement("th"); th.textContent = h;
  ledHead.appendChild(th);
});
ledTab.appendChild(ledHead);
const ledRows = MEAS.map(m => {
  const tr = document.createElement("tr");
  [m.tag, "W" + m.wave, "B" + m.bundle, m.a + " ⇄ " + m.b, fmt(m.skew, 2),
   fmt(m.sigma, 3), fmt(m.q, 3), m.req].forEach(c => {
    const td = document.createElement("td"); td.textContent = c; tr.appendChild(td);
  });
  tr.style.display = "none";
  ledTab.appendChild(tr);
  return tr;
});
const bd = {};
[["sub", "提交"], ["base", "基线"], ["wr", "写回"], ["dev", "设备"]].forEach(it => {
  const d = document.createElement("div"); d.className = "badge";
  const b = document.createElement("b"); b.textContent = it[1] + "：";
  const sp = document.createElement("span");
  d.appendChild(b); d.appendChild(sp);
  $("#badges").appendChild(d); bd[it[0]] = { box: d, val: sp };
});
const gEls = ["CAS 指纹", "单调 δ≥0", "预算 d≤dmax", "原子写 + 审计"].map(tx => {
  const d = document.createElement("div"); d.className = "guard"; d.textContent = tx;
  $("#guards").appendChild(d); return d;
});

/* ---------- 幂等渲染 ---------- */
function draw() {
  const v = vm(T), sc = SCENES[v.i], s = S;
  drawCalibration(v);
  bar.value = String(Math.round(T / TOTAL * 1000));
  clockEl.textContent = fmt(T) + "s / " + fmt(TOTAL) + "s";
  pills.forEach((b, i) => {
    b.className = "pill" + (i === v.i ? " on" : (i < v.i ? " done" : ""));
  });

  /* 芯片图：结构按行点亮 / 边按状态 / 当前波画环 */
  const reveal = v.id === "chip" ? clamp01(v.p / .7) * (LAY.rows + .001) : 99;
  lanes.forEach(g => {
    g.style.opacity = +g.dataset.lane < reveal ? 1 : 0;
  });
  const curLines = (v.id === "waves" || v.id === "measure" || v.id === "finish") && v.waveFull >= 0
    ? waveLines[v.waveFull] : null;
  rings.forEach(r => {
    const on = curLines && (r.dataset.lines || "").split(" ").some(ln => curLines.has(ln));
    r.style.opacity = on ? .9 : 0;
  });
  edges.forEach(el => {
    const mi = +el.dataset.mi, gi = +el.dataset.gi;
    let on = false;
    if (v.id === "tree") on = mi < v.k;
    else if (v.id === "waves")
      on = gi < v.waveFull || (gi === v.waveFull &&
        MEAS[mi].wi < Math.floor(v.waveFrac * WAVES[gi].size));
    else on = mi < v.k;
    el.classList.toggle("lit", on);
    el.classList.toggle("hot", mi === v.hot);
  });
  chips.forEach(c => {
    const mi = +c.dataset.mi;
    c.classList.toggle("on",
      (v.id === "measure" || v.id === "finish") && mi < v.k && MEAS[mi].gi === v.waveFull);
  });

  /* 波矩阵 / 甘特：同一进度分别驱动 */
  cells.forEach(c => {
    const gi = giOf[c.dataset.stage + "|" + c.dataset.index];
    c.classList.toggle("selected", gi === v.waveFull);
    c.querySelector(".cellfill").setAttribute("width",
      String(c.querySelector(".cellbg").width.baseVal.value * fillFrac(v, gi)));
  });
  segs.forEach(sg => {
    sg.classList.toggle("selected", +sg.dataset.gi === v.waveFull);
    sg.querySelector(".cellfill").setAttribute("width",
      String(sg.querySelector(".cellbg").width.baseVal.value * fillFrac(v, +sg.dataset.gi)));
  });
  const tickMode = v.id === "tree" ? v.k : (v.id === "chip" || v.id === "truth"
    || v.id === "waves" ? 0 : v.k);
  ticks.forEach(tk => tk.classList.toggle("lit", +tk.dataset.i < tickMode));
  const phOn = v.id === "tree" || v.id === "measure" || v.id === "finish";
  playhead.style.opacity = phOn ? 1 : 0;
  if (phOn) {
    const px = GX0 + v.k / NT * (GX1 - GX0);
    playhead.setAttribute("x1", px);
    playhead.setAttribute("x2", px);
  }

  /* 台账 */
  const showLed = v.k > 0 && v.id !== "waves" && v.id !== "tree";
  ledRows.forEach((tr, i) => {
    tr.style.display = showLed && i < v.k ? "" : "none";
    tr.className = showLed && v.hot >= 0 && i === v.k - 1 ? "new" : "";
  });
  if (showLed && v.id === "measure") lscroll.scrollTop = lscroll.scrollHeight;

  /* 徽标 / 守卫 / 大数字 / 字幕 */
  const wu = v.waveFull >= 0 ? v.waveFull + 1 : 0;
  const setB = (key, text, yes) => {
    bd[key].val.textContent = text;
    bd[key].box.className = "badge" + (yes ? " yes" : "");
  };
  setB("sub", v.k + "/" + NT + " · " + wu + "/" + NW + " 波", v.k >= NT);
  setB("base", META.qc_same
    ? META.qc_hash.slice(0, 8) + "（单/耦 " + (stageFirstMi.readout_final - stageFirstMi.qubit) + " 条跨波同基线）"
    : (META.one_per_stage ? "每组内 1 个基线" : "基线有变化！"), !!META.qc_same);
  setB("wr", v.written || "未写回", (v.written || "").indexOf("落盘") >= 0);
  setB("dev", v.applied ? "已下发" : "未下发", v.applied);
  gEls.forEach((g, i) => { g.className = "guard" + (i < v.guards ? " on" : ""); });
  $("#big").innerHTML = v.id === "audit"
    ? "<div><b>" + s.rounds_sequential + "</b>串行测量次数</div>"
      + "<div><b>" + s.rounds_parallel + " 波 / " + s.bundles_total + " 束</b>冲突波 / 提交束</div>"
      + "<div><b>" + s.ideal_bundle_speedup + "×</b>等时完全并发理论比值 · 非实测</div>"
      + "<div><b>" + fmt(s.residual_ns, 3) + "</b>校准后残差 ns</div>"
    : (v.id === "solve"
      ? "<div><b>" + (stageFirstMi.readout_final - stageFirstMi.qubit) + "</b>条观测一次解</div>"
        + "<div><b>" + S.anchor + "</b>锚线（取最右）</div>"
        + "<div><b>" + fmt(s.plan_ns, 0) + "</b>plan 幅度 ns</div>"
      : "<div><b>" + v.k + "</b>/ " + NT + " 已提交</div>"
        + "<div><b>" + wu + "</b>/ " + NW + " 波</div>"
        + "<div><b>≤" + META.max_parallel + "</b>条 / 提交束</div>");
  $("#chipHint").textContent = v.hintC;
  $("#matHint").textContent = v.hintM;
  $("#ganttHint").textContent = v.hintG;
  $("#say").innerHTML = "<b>" + sc.title + "</b>　" + sc.sub;
  $("#rule").textContent = META.rule + "；波只是提交提示，不是并发承诺——执行方可按顺序"
    + "跑完一波（hardware_hold 独占时本就如此），结果按 request_id 归位。";
}
function fillFrac(v, gi) {
  if (v.id === "waves" || v.id === "measure" || v.id === "finish")
    return gi < v.waveFull ? 1 : (gi === v.waveFull ? clamp01(v.waveFrac) : 0);
  if (v.id === "solve" || v.id === "write") return gi < stageFirstGi.readout_final ? 1 : 0;
  if (v.id === "audit") return 1;
  return 0;
}

function drawCalibration(v) {
  const cal = DATA.calibration, state = v.cal;
  const x = value => cal.x0 + value / cal.dom * (cal.x1 - cal.x0);
  [...document.querySelectorAll(".cal-row")].forEach(row => {
    const ln = cal.lines[+row.dataset.i], a = ln.d1 * state.a,
          b = ln.d2 * state.b, c = ln.d3 * state.c;
    const segment = (cls, from, to) => {
      const el = row.querySelector(cls);
      el.setAttribute("x", x(Math.min(from, to)));
      el.setAttribute("width", Math.abs(x(to) - x(from)));
    };
    segment(".cal-base", 0, ln.L);
    segment(".cal-d1", ln.L, ln.L + a);
    segment(".cal-d2", ln.L + a, ln.L + a + b);
    segment(".cal-d3", ln.L + a + b, ln.L + a + b + c);
    const planned = row.querySelector(".cal-d2");
    planned.setAttribute("fill-opacity", state.plan ? .15 : 1);
    planned.setAttribute("stroke", state.plan ? "#2c6cb0" : "none");
    planned.setAttribute("stroke-dasharray", state.plan ? "4 3" : "none");
    const q = ln.L + a + (state.plan ? 0 : b) + c;
    const end = row.querySelector(".cal-end");
    end.setAttribute("x1", x(q)); end.setAttribute("x2", x(q));
    row.querySelector(".cal-value").textContent = fmt(q, 2) + " / " + fmt(q - ln.L, 2);
    row.querySelector("title").textContent = ln.name + "：L=" + fmt(ln.L, 3)
      + " ns，q=" + fmt(q, 3) + " ns，d=" + fmt(q - ln.L, 3)
      + " ns；d₁=" + fmt(a, 3) + "，d₂=" + fmt(b, 3) + "，d₃=" + fmt(c, 3);
  });
  $("#calHint").textContent = state.plan ? "虚线表示求解计划，尚未写回；右侧数字是已应用的 q / d。"
    : state.c > 0 ? "读取线收尾：橙色覆盖段表示收回量，黑色端点向左移动；最终逐线对齐。"
    : state.b > 0 ? "单比特 / 耦合器整批补偿落盘；读取线等待收尾测量。"
    : state.a > 0 ? "读取线先对齐并加入 headroom；单比特 / 耦合器仍只记录观测。"
    : META.lines + " 条线全部显示；灰条为 L，彩条为补偿，黑线为当前 q；悬停查看精确数值。";
}

function calibrationViewport(width, height, count) {
  // 按可用画布分栏，让全部信号线同时可见。
  const columns = width >= 1100 ? 4 : (width >= 650 ? 3 : 2);
  const rows = Math.ceil(count / columns), cellWidth = width / columns;
  const step = (height - 28) / rows;
  return {width, height, columns, rows, cellWidth, step,
          x0: Math.min(62, cellWidth * .28), x1: cellWidth - 12,
          font: Math.max(6, Math.min(11, step * .75))};
}

function fitCalibration() {
  const svg = $("#calibration"), box = svg.parentElement.getBoundingClientRect();
  if (box.width <= 0 || box.height <= 28) return;
  const cal = DATA.calibration, layout = calibrationViewport(box.width, box.height, cal.lines.length);
  svg.setAttribute("viewBox", "0 0 " + layout.width + " " + layout.height);
  cal.x0 = layout.x0; cal.x1 = layout.x1;
  [...svg.querySelectorAll(".cal-row")].forEach((row, i) => {
    const col = Math.floor(i / layout.rows), index = i % layout.rows;
    const oldY = cal.y0 + i * cal.step, y = 28 + (index + .5) * layout.step;
    row.setAttribute("transform", "translate(" + col * layout.cellWidth + "," + (y - oldY) + ")");
    row.querySelector(".cal-name").setAttribute("x", layout.x0 - 5);
    row.querySelector(".cal-name").style.fontSize = layout.font + "px";
    for (const cls of [".cal-base", ".cal-d1", ".cal-d2", ".cal-d3"]) {
      const el = row.querySelector(cls), h = Math.min(10, layout.step * .6);
      el.setAttribute("y", oldY - h / 2); el.setAttribute("height", h);
    }
    const end = row.querySelector(".cal-end");
    end.setAttribute("y1", oldY - layout.step * .35);
    end.setAttribute("y2", oldY + layout.step * .35);
  });
  const axis = $("#calAxis"); axis.replaceChildren();
  for (let col = 0; col < layout.columns; col++) {
    for (const value of [0, cal.dom / 2, cal.dom]) {
      const x = col * layout.cellWidth + cal.x0 + value / cal.dom * (cal.x1 - cal.x0);
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      for (const [k,v] of Object.entries({x1:x,x2:x,y1:22,y2:layout.height,stroke:"#e8edf2"})) line.setAttribute(k,v);
      axis.appendChild(line);
      const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
      for (const [k,v] of Object.entries({x,y:15,"text-anchor":"middle",class:"cal-label"})) text.setAttribute(k,v);
      text.style.fontSize = layout.font + "px"; text.textContent = value + (value === cal.dom ? " ns" : "");
      axis.appendChild(text);
    }
  }
  draw();
}

/* ---------- 主循环 ---------- */
const LAY = DATA.layout.chip, GX0 = DATA.layout.timeline.x0,
      GX1 = DATA.layout.timeline.x1;
function frame(ts) {
  if (!last) last = ts;
  const dt = Math.min(64, ts - last); last = ts;
  if (playing) {
    // 场景时长以秒计，requestAnimationFrame 的时间戳以毫秒计。
    T += dt / 1000 * rate;
    if (wavePlayback && T >= wavePlayback.end) {
      T = wavePlayback.end; wavePlayback.done = true; setPlay(false);
    } else if (T >= TOTAL) { T = TOTAL - 1e-3; setPlay(false); }
    draw();
  }
  requestAnimationFrame(frame);
}
jump(0);
new ResizeObserver(fitCalibration).observe($("#calibration").parentElement);
requestAnimationFrame(frame);
</script>
</body></html>
"""


def render(data: dict, scenes: list[dict], lay: dict) -> str:
    hashes = {m["hash"] for m in lay["meas"]}
    stage_hash = {}
    for m in lay["meas"]:
        stage_hash.setdefault(m["stage"], set()).add(m["hash"])
    one_per_stage = all(len(v) == 1 for v in stage_hash.values())
    qc_same = (len(stage_hash.get("qubit", {"x"})) == 1
               and stage_hash.get("qubit") == stage_hash.get("coupler"))
    payload = {
        "meta": {"version": data["meta"]["version"],
                 "chip_desc": data["chip"]["meta"]["description"],
                 "lines": data["meta"]["lines"],
                 "max_parallel": data["meta"]["max_parallel"],
                 "tol_ns": data["meta"]["tol_ns"],
                 "rule": data["meta"]["rule"],
                 "one_hash": len(hashes) == 1,
                 "one_per_stage": one_per_stage,
                 "qc_same": qc_same,
                 "qc_hash": (next(iter(stage_hash["coupler"]))
                             if qc_same else "")},
        "stages": [{"name": s["name"], "title": s["title"],
                    "waves": {"waves": (s.get("waves") or {}).get("waves", []),
                              "bundle_size": (s.get("waves") or {}).get("bundle_size", 0)}}
                   for s in data["stages"]],
        "meas": lay["meas"], "waves_meta": lay["waves_meta"],
        "calibration": calibration_layout(data),
        "summary": data["summary"], "wave_total": len(lay["timeline"]["cells"]),
        "layout": {"chip": {"rows": lay["rows"]},
                   "timeline": {"x0": lay["timeline"]["x0"],
                                "x1": lay["timeline"]["x1"]}},
    }
    html = (HTML
            .replace("__DATA__", json.dumps(payload, ensure_ascii=False,
                                            separators=(",", ":")))
            .replace("__SCENES__", json.dumps(scenes, ensure_ascii=False))
            .replace("__COLOR__", json.dumps(GROUP_COLOR, ensure_ascii=False))
            .replace("__CHIPDESC__", esc(payload["meta"]["chip_desc"]))
            .replace("__LINE_COUNT__", str(data["meta"]["lines"]))
            .replace("__SEQ_COUNT__", str(data["summary"]["rounds_sequential"]))
            .replace("__WAVE_COUNT__", str(data["summary"]["rounds_parallel"]))
            .replace("__BUNDLE_COUNT__", str(data["summary"]["bundles_total"]))
            .replace("__CHIP__", svg_chip(lay, data["chip"]))
            .replace("__MATRIX__", svg_matrix(lay))
            .replace("__GANTT__", svg_timeline(lay))
            .replace("__CALIBRATION__", svg_calibration(data)))
    leftovers = [ph for ph in ("__DATA__", "__SCENES__", "__COLOR__", "__CHIPDESC__",
                               "__CHIP__", "__MATRIX__", "__GANTT__") if ph in html]
    if leftovers:
        raise SystemExit(f"模板占位符未替换：{leftovers}")
    if "{{" in html:
        raise SystemExit("模板残留 {{ —— CSS/JS 会被双大括号破坏（本次重写前的根因）")
    return html


# ---------------- 几何自检 ----------------

def _bez_sample(p0, p1, p2, p3, n=24):
    pts = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        pts.append((u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0]
                    + t**3 * p3[0],
                    u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1]
                    + t**3 * p3[1]))
    return pts


def selfcheck(data: dict, lay: dict) -> list[str]:
    """坐标出界 / 槽位间隙 / 走廊压框 / 束超宽 / 波划分不自洽都会报出来。"""
    bad: list[str] = []
    c = lay["canvas"]
    for name, (x, y) in lay["anchors"].items():
        if not (4 <= x <= c["w"] - 4 and 4 <= y <= c["h"] - 4):
            bad.append(f"锚点出界: {name} ({x:.0f},{y:.0f})")
    # 读取线槽与上一行比特底缘的间隙（上一版动画的真实重叠点）
    row_ys = sorted({y for _, (x, y) in lay["qubits"].items()})
    for r in range(1, len(row_ys)):
        gap = (row_ys[r] - RL_UP) - (row_ys[r - 1] + Q_R)
        if gap < MIN_RL_GAP:
            bad.append(f"读取线槽与上一行比特间隙只有 {gap:.0f}px（< {MIN_RL_GAP}）")
    for rl in lay["readouts"]:
        if rl["x"] < 2 or rl["x"] + rl["w"] > c["w"] - 2:
            bad.append(f"读取线框出界: {rl['id']}")
    boxes = [(rl["x"], rl["y"], rl["x"] + rl["w"], rl["y"] + rl["h"])
             for rl in lay["readouts"]]

    def hit_box(x: float, y: float, skip: int = -1) -> int:
        for bi, bx in enumerate(boxes):
            if bi != skip and bx[0] + 2 <= x <= bx[2] - 2 and bx[1] + 2 <= y <= bx[3] - 2:
                return bi
        return -1

    # 桥接折线（结构线与测量边同一路径）不得压任何读取线框
    for b in lay["bridges"]:
        za, zb = lay["anchors"][f"{b['qa']}.z"], lay["anchors"][f"{b['qb']}.z"]
        pts = bridge_route(za, zb)
        for i in range(len(pts) - 1):
            for k in range(13):
                t = k / 12
                x = pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t
                y = pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t
                bi = hit_box(x, y)
                if bi >= 0:
                    bad.append(f"桥接 {b['id']} 折线压读取线框 {boxes[bi]} ({x:.0f},{y:.0f})")
                    break
    # 收尾曲线（readout_final）：允许从自己的框出发，不得压别的框
    rl_index = {rl["id"]: i for i, rl in enumerate(lay["readouts"])}
    for m in lay["meas"]:
        if m["stage"] != "readout_final":
            continue
        p, q = lay["anchors"][m["a"]], lay["anchors"][m["b"]]
        own = rl_index[m["a"].split(".")[0]]
        for x, y in _bez_sample(p, (p[0] - 36, p[1] + 17), (q[0] - 36, q[1] - 13), q):
            bi = hit_box(x, y, skip=own)
            if bi >= 0:
                bad.append(f"收尾曲线 {m['a']}→{m['b']} 压读取线框 {bi} ({x:.0f},{y:.0f})")
                break
        if not (2 <= x <= c["w"] - 2 and 2 <= y <= c["h"] - 2):
            bad.append(f"收尾曲线终点出界 ({x:.0f},{y:.0f})")
    # skew 徽标落点在画布内
    for m in lay["meas"]:
        p, q = lay["anchors"][m["a"]], lay["anchors"][m["b"]]
        cx, cy = _chip_pos(m["stage"], m["a"], m["b"], p, q)
        if not (2 <= cx <= c["w"] - 2 and 2 <= cy <= c["h"] - 2):
            bad.append(f"skew 徽标出界: {m['a']}⇄{m['b']} ({cx:.0f},{cy:.0f})")
    # 矩阵 / 甘特几何
    for row in lay["matrix"]["rows"]:
        if row["cells"] and row["cells"][-1]["x"] + row["cells"][-1]["w"] > lay["matrix"]["w"]:
            bad.append(f"波矩阵出界: {row['stage']}")
    t = lay["timeline"]
    if t["cells"] and t["cells"][-1]["x"] + t["cells"][-1]["w"] > t["x1"] + 0.5:
        bad.append("甘特下轨出界")
    if len(t["ticks"]) != sum(len(st["pairs"]) for st in data["stages"]):
        bad.append("串行刻度数与边数不一致")
    if len(t["cells"]) != sum(len((s.get("waves") or {}).get("waves", []))
                              for s in data["stages"]):
        bad.append("甘特波段数与总波数不一致")
    # 束尺寸 ≤ max_parallel，且拼回等于波大小
    mp = int(data["meta"].get("max_parallel") or 0)
    for wm in lay["waves_meta"]:
        if mp > 0 and wm["bundles"] and max(wm["bundles"]) > mp:
            bad.append(f"波 {wm['gi']} 提交束超宽: {wm['bundles']} > {mp}")
        if sum(wm["bundles"]) != wm["size"]:
            bad.append(f"波 {wm['gi']} 束尺寸拼不回波大小")
    # 每条合格测量都有 wave / request_id / config_hash，且波号在计划内
    plan_pairs = {(s["name"], w["index"]) for s in data["stages"]
                  for w in (s.get("waves") or {}).get("waves", [])}
    for m in lay["meas"]:
        if not m.get("req") or not m.get("hash"):
            bad.append(f"测量缺 request_id / config_hash: {m['a']}⇄{m['b']}")
        if (m["stage"], m["wave"]) not in plan_pairs:
            bad.append(f"测量波号不在计划内: {m['a']}⇄{m['b']}")
    if len(lay["meas"]) != sum(len(st["pairs"]) for st in data["stages"]):
        bad.append("台账条数与计划边数不一致")
    # 同波内不得共享线名（动画的立论基础）
    for s in data["stages"]:
        for w in (s.get("waves") or {}).get("waves", []):
            seen = set()
            for p in w["pairs"]:
                for ln in (p["a"], p["b"]):
                    if ln in seen:
                        bad.append(f"{s['name']} 波 {w['index']} 内共享线名: {ln}")
                    seen.add(ln)
    # 每条测量边的两个锚点都必须存在（否则动画里会缺边）
    missing = {m["a"] for s in data["stages"] for m in s["measurements"]
               if m["ok"] and (m["a"] not in lay["anchors"] or m["b"] not in lay["anchors"])}
    if missing:
        bad.append(f"缺少锚点: {sorted(missing)[:4]}")
    return bad


def main() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    scenes = build_scenes(data)
    lay = build_layout(data)
    problems = selfcheck(data, lay)
    for p in problems:
        print("自检未通过:", p)
    html = render(data, scenes, lay)
    OUT.write_text(html, encoding="utf-8")
    print(("OK" if not problems else f"{len(problems)} 处问题") +
          f" · 边 {len(lay['meas'])} 条 · 波 {len(lay['waves_meta'])} · "
          f"时长 {sum(sc['dur'] for sc in scenes):.0f}s · {OUT.name}")


if __name__ == "__main__":
    main()

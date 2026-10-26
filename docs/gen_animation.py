# -*- coding: utf-8 -*-
"""动画生成器：`python docs/gen_animation.py` → docs/demo-animation.html

数据来自 `docs/animation-data.json`（由 `python docs/gen_animation_data.py` 真实跑**四组
分组流水线**导出），在这里内联进 HTML —— 产物是**单文件、零依赖、离线可开**的交互动画：

  ① 读取线校准（ro/ri）测完**立刻写回**
  ② 读取线整体右移 headroom（可设 0–1000 ns，给后续阶段留空间）→ **立刻写回**
  ③ 单比特 XY-Z、④ 比特间/耦合器 Z-Z **只记录不写回**
  ⑤ 末尾把 qubit/coupler 一起求解、**整批一次写回**
  ⑥ 读取线收回 headroom、往左拉回对齐到已对齐的 qubit.xy → **立刻写回**。

左边芯片图看四组的测量边按组点亮，右边时间轴看每条线的位移分三段：
`d1`（readout 含 headroom）、`d2`（qubit/coupler 末尾落盘）、`d3`（readout_final，
收回 headroom 时为负 = 往左）。

布局全部在本机用 Python 算好（`build_layout`），JS 只按像素作图 ——
换芯片 / 换延时量级都不会把图画溢出画布。生成后跑 `selfcheck` 复核几何。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "animation-data.json"
OUT = HERE / "demo-animation.html"

# ---- 芯片图画布（px）：左侧读取线列 + 右侧比特方阵 ----
CHIP_W = 520
PAD_L, PAD_R, PAD_T, PAD_B = 132, 34, 46, 54
NODE_R = 27.0          # 比特圆半径上限
RL_SLOT = 44.0         # 每条读取线至少占的竖直空间
RL_W, RL_H, RL_TAP = 88.0, 28.0, 17.0
MAX_GRID_H = 900.0

# ---- 时间轴画布（px）----
TIME_X0, TIME_X1 = 96.0, 742.0
TIME_BASE_W = 780.0
TIME_Y0, TIME_TOP = 46.0, 30.0
TICK_STEPS = (1, 2, 2.5, 5, 10)


# ===================== 布局推导 =====================

def _text_px(s: str, size: float) -> float:
    """粗略估算文本像素宽度：CJK 按 1em，半角按 0.56em，加粗再抬 6%。"""
    return sum(size if ord(ch) > 0x2E80 else size * 0.56 for ch in s) * 1.06


def _nice_scale(v: float, want: int = 4) -> float:
    """把量程分成 want 段左右的好看步长（1/2/2.5/5 × 10^k）。"""
    raw = max(v, 1e-9) / want
    mag = 10.0 ** math.floor(math.log10(raw))
    for m in TICK_STEPS:
        if m * mag >= raw - 1e-12:
            return m * mag
    return 10.0 * mag


def chip_layout(chip: dict) -> dict:
    """由芯片结构算出像素坐标：比特方阵居中，读取线沿左列均布（条数不限）。"""
    pos = {q["id"]: [float(q["pos"][0]), float(q["pos"][1])]
           for q in chip.get("qubits", []) if q.get("pos")}
    gx = [p[0] for p in pos.values()] or [0.0]
    gy = [p[1] for p in pos.values()] or [0.0]
    span_x, span_y = max(gx) - min(gx), max(gy) - min(gy)
    mx, my = (max(gx) + min(gx)) / 2, (max(gy) + min(gy)) / 2

    inner_w = CHIP_W - PAD_L - PAD_R - 2 * NODE_R
    scale = 150.0
    if span_x > 0:
        scale = min(scale, inner_w / span_x)
    if span_y > 0:
        scale = min(scale, (MAX_GRID_H - PAD_T - PAD_B - 2 * NODE_R) / span_y)

    gap = min(v for v in (span_x * scale, span_y * scale) if v > 0) if (span_x or span_y) else 0
    r = NODE_R if gap <= 0 else max(8.0, min(NODE_R, 0.45 * gap))

    grid_h = PAD_T + PAD_B + 2 * r + span_y * scale
    tail_h = PAD_T + PAD_B + 2 * r + max(1, len(chip.get("readout_lines", []))) * RL_SLOT
    h = max(260.0, grid_h, tail_h)
    cx0 = PAD_L + r + (CHIP_W - PAD_L - PAD_R - 2 * r) / 2
    cy0 = PAD_T + r + (h - PAD_T - PAD_B - 2 * r) / 2

    qp = {qid: [round(cx0 + (p[0] - mx) * scale, 1), round(cy0 + (p[1] - my) * scale, 1)]
          for qid, p in pos.items()}
    cp = {}
    missing = []
    for c in chip.get("couplers", []):
        a, b = (qp.get(q) for q in c["qubits"])
        if a and b:
            cp[c["id"]] = [round((a[0] + b[0]) / 2, 1), round((a[1] + b[1]) / 2, 1)]
        else:
            missing.append(c["id"])

    cav_qubit = {c["id"]: c.get("qubit") for c in chip.get("cavities", [])}
    rows = []
    for line in chip.get("readout_lines", []):
        ys = [qp[q][1] for q in (cav_qubit.get(c) for c in line.get("cavities", []))
              if q in qp]
        rows.append({"id": line["id"], "y": sum(ys) / len(ys) if ys else cy0})
    rows.sort(key=lambda x: (x["y"], x["id"]))

    top, bot = PAD_T + r, h - PAD_B - r
    slot = max(RL_SLOT, (bot - top) / max(1, len(rows)))
    rl = {}
    for i, row in enumerate(rows):
        rl[row["id"]] = [round(PAD_L / 2 + 4, 1), round(top + slot * (i + 0.5), 1)]

    dx, dy = round(r * 0.41, 1), round(r * 0.11, 1)
    anchor: dict[str, list[float]] = {}
    for qid, (x, y) in qp.items():
        anchor[f"{qid}.xy"] = [round(x - dx, 1), round(y - dy, 1)]
        anchor[f"{qid}.z"] = [round(x + dx, 1), round(y + dy, 1)]
    for cid, (x, y) in cp.items():
        anchor[f"{cid}.z"] = [x, y]
    for rid, (x, y) in rl.items():
        anchor[f"{rid}.ro"] = [round(x - RL_TAP, 1), y]
        anchor[f"{rid}.ri"] = [round(x + RL_TAP, 1), y]

    return {
        "w": CHIP_W, "h": round(h, 1), "vb": f"0 0 {CHIP_W} {round(h, 1)}",
        "r": round(r, 1), "dot": {"dx": dx, "dy": dy, "rad": round(r * 0.26, 1)},
        "qubits": [{"id": qid, "cx": qp[qid][0], "cy": qp[qid][1]} for qid in qp],
        "couplers": [{"id": cid, "cx": cp[cid][0], "cy": cp[cid][1], "half": round(r * 0.3, 1)}
                     for cid in cp],
        "readouts": [{"id": rid, "cx": rl[rid][0], "cy": rl[rid][1],
                      "w": RL_W, "h": RL_H, "tap": RL_TAP} for rid in rl],
        "anchor": anchor, "missingCouplers": missing,
    }


def time_layout(lines: list[dict], target: float, anchor: str | None) -> dict:
    """时间轴：量程、刻度、行数、文字宽度都由数据算，写死只会溢出。"""
    hi = max([float(l.get("L", 0.0)) for l in lines] +
             [float(l.get("q1", 0.0)) for l in lines] + [float(target or 0.0), 1e-9])
    step_ns = _nice_scale(hi)
    dom = math.ceil(hi / step_ns - 1e-9) * step_ns
    ticks = [round(v, 6) for v in _frange(0.0, dom, step_ns)]

    n = max(1, len(lines))
    step = max(14.0, min(22.0, math.floor(460.0 / n)))
    axis_y = TIME_Y0 + n * step - 6
    res_y, sub_y = TIME_Y0 + n * step + 44, TIME_Y0 + n * step + 62
    w = max(TIME_BASE_W, TIME_X1 + 38)

    xs = lambda v: TIME_X0 + (v / dom) * (TIME_X1 - TIME_X0)      # noqa: E731
    tx = round(xs(min(dom, max(0.0, target or 0.0))), 1)
    label = f"目标锚线 {anchor or '—'}"
    half = _text_px(label, 10.5) / 2
    ta = "middle"
    if tx + half > w - 4:
        ta = "end"
    if tx - half < 4:
        ta = "start"

    wide = f"{dom:.3f}"      # 最坏情况下的数字宽度（量程本身比任何读数都长）
    res_txt = f"当前残差 max(q) − min(q) = {wide} ns　→　aligned = True"
    sub_txt = (f"初始失配 {wide} ns · 已落盘 max(d) = {wide} ns · 总位移 {wide} ns")
    need = TIME_X0 + max(_text_px(res_txt, 12.0), _text_px(sub_txt, 10.5)) + 12
    if need > w:
        w = math.ceil(need)

    box_h = round(res_y + 34, 1)     # 末行文字基线下方留出 descender
    return {
        "w": int(w), "h": box_h, "vb": f"0 0 {int(w)} {box_h}",
        "x0": TIME_X0, "x1": TIME_X1, "y0": TIME_Y0, "step": step, "dom": dom,
        "ticks": ticks, "axisY": axis_y, "resY": res_y, "subY": sub_y,
        "nameX": 8.0, "targetX": tx, "targetAnchor": ta, "labelMaxPx": round(need, 1),
    }


def _frange(a: float, b: float, s: float):
    v = a
    while v <= b + 1e-9:
        yield v
        v += s


def build_layout(data: dict) -> dict:
    """时间轴竖线 = 锚线对齐后的**绝对**时序位置（q1）。

    注意不能用 `report.target_ns`：那是求解器**相对坐标系**里的目标
    （去掉公共平移、以重心为原点后的 max 偏移），跟时间轴上的绝对位置不可比。
    """
    lines = data.get("lines", [])
    anchor = (data.get("solve") or {}).get("anchor")
    by_name = {l.get("name"): l for l in lines}
    target = None
    if anchor and anchor in by_name:
        target = by_name[anchor].get("q1")
    if target is None:                      # 锚线缺失（旧数据 / 未对齐）→ 退化为最右线
        target = max((l.get("q1", 0.0) for l in lines), default=0.0)
    return {"chip": chip_layout(data.get("chip", {})),
            "time": time_layout(lines, target, anchor)}


# ===================== 几何自检 =====================

def selfcheck(data: dict, lay: dict) -> list[str]:
    """复核：所有要画的东西都落在各自 viewBox 内，且数据里的名字都有坐标。"""
    bad: list[str] = []
    cl, tl = lay["chip"], lay["time"]

    for cid in cl["missingCouplers"]:
        bad.append(f"芯片：耦合器 {cid} 的端点比特没有坐标")

    def out(name: str, x: float, y: float, m: float) -> None:
        if not (m <= x <= cl["w"] - m and m <= y <= cl["h"] - m):
            bad.append(f"芯片：{name} 锚点 ({x},{y}) 出界（画布 {cl['w']}×{cl['h']}）")

    for q in cl["qubits"]:
        if q["cy"] + cl["r"] + 18 > cl["h"] - 6:
            bad.append(f"芯片：{q['id']} 标签越过底边")
    for key, (x, y) in cl["anchor"].items():
        out(key, x, y, 4)
    for r in cl["readouts"]:
        if r["cx"] - r["w"] / 2 < 2 or r["cx"] + r["w"] / 2 > PAD_L - 2:
            bad.append(f"芯片：读取线 {r['id']} 方框越出左列")
    ys = sorted(r["cy"] for r in cl["readouts"])
    for a, b in zip(ys, ys[1:]):
        if b - a < RL_H + 6:
            bad.append(f"芯片：相邻读取线方框竖直重叠（间距 {b - a:.1f}px）")
    if cl["qubits"] and cl["readouts"]:
        left = min(q["cx"] for q in cl["qubits"]) - cl["r"]
        right = max(r["cx"] + r["w"] / 2 for r in cl["readouts"])
        if left <= right:
            bad.append(f"芯片：比特方阵（左沿 {left:.1f}）压到读取线列（右沿 {right:.1f}）")

    edges = list(data.get("candidates", [])) + list(data.get("tree", []))
    for st in data.get("stages", []):            # 分组数据（旧格式没有就跳过）
        edges += list(st.get("pairs", []))
    for edge in edges:
        for end in (edge["a"], edge["b"]):
            if end not in cl["anchor"]:
                bad.append(f"边 {edge['a']}↔{edge['b']}：端点 {end} 没有像素坐标")

    names = {l["name"] for l in data.get("lines", [])}
    for m in data.get("measurements", []):
        if m["a"] not in names or m["b"] not in names:
            bad.append(f"台账：{m['a']}⇄{m['b']} 有一端不在时间轴上")

    if tl["dom"] <= 0:
        bad.append("时间轴：量程为 0")
    hi = max([float(l.get("L", 0)) for l in data.get("lines", [])] +
             [float(l.get("q1", 0)) for l in data.get("lines", [])] + [0.0])
    if hi > tl["dom"] + 1e-6:
        bad.append(f"时间轴：最大位置 {hi} 超出量程 {tl['dom']}")
    if not (tl["x0"] <= tl["targetX"] <= tl["x1"]):
        bad.append(f"时间轴：目标锚线 x={tl['targetX']} 不在绘图区内")
    if tl["labelMaxPx"] > tl["w"]:
        bad.append(f"时间轴：底部文字需 {tl['labelMaxPx']}px，画布只有 {tl['w']}px")
    if len(data.get("lines", [])) * tl["step"] + tl["y0"] > tl["h"]:
        bad.append("时间轴：行数放不下，画布高度不足")
    if tl["subY"] + 6 > tl["h"]:
        bad.append(f"时间轴：底端文字基线 {tl['subY']} 贴出画布（高 {tl['h']}）")
    return bad


# ===================== HTML 模板 =====================

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>QXtrlTimingCali · 分组校准 Demo 动画</title>
<style>
 body{margin:0;padding:22px 26px 26px;background:#fbfcfd;color:#1f2933;
      font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;font-size:13px}
 h1{font-size:19px;margin:0 0 3px}
 .sub{font-size:12.5px;color:#5c6b7a;margin:0 0 14px;line-height:1.6}
 code{background:#eef2f6;padding:1px 5px;border-radius:4px;font-size:12px}
 .nav{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
 .pill{border:1px solid #dde3ea;background:#fff;color:#5c6b7a;border-radius:14px;
       padding:5px 12px;font-size:12px;cursor:pointer;transition:.15s}
 .pill:hover{border-color:#9fb3c8;color:#1f2933}
 .pill.on{background:#1d7a3c;border-color:#1d7a3c;color:#fff;font-weight:600}
 .pill.done{background:#e9f6ec;border-color:#a8d5ba;color:#1d7a3c}
 .grid{display:grid;grid-template-columns:__CHIP_COL__ 1fr;gap:14px;align-items:start}
 .card{background:#fff;border:1px solid #e3e8ee;border-radius:12px;padding:12px 14px 10px}
 .card h2{font-size:13px;margin:0 0 6px;color:#1d4e75;font-weight:700}
 .card .hint{font-size:11.5px;color:#7a8590;margin:0 0 6px}
 svg{display:block;width:100%;height:auto}
 .t{font-size:12px;font-weight:600} .s{font-size:10.5px;fill:#4a5560}
 .tick{font-size:9.5px;fill:#8a95a0} .name{font-size:10.5px;fill:#3d4b57}
 .below{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}
 table{border-collapse:collapse;font-size:11.5px;width:100%}
 th,td{border-bottom:1px solid #eef2f6;padding:3px 6px;text-align:left;white-space:nowrap}
 th{color:#7a8590;font-weight:600}
 .scroll{max-height:176px;overflow:auto}
 tr.new{background:#fff6e6}
 .badges{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}
 .badge{border:1px solid #dde3ea;border-radius:8px;padding:4px 9px;font-size:11.5px;
        background:#f6f7f9;color:#5c6b7a}
 .badge b{color:#1f2933}
 .badge.no{border-color:#f0c9c4;background:#fdecea;color:#b3261e}
 .badge.yes{border-color:#a8d5ba;background:#e9f6ec;color:#1d7a3c}
 .guards{display:flex;gap:6px;margin:8px 0 4px}
 .guard{flex:1;border:1px solid #dde3ea;border-radius:8px;padding:5px 4px;text-align:center;
        font-size:11px;color:#8a95a0;background:#f6f7f9}
 .guard.on{border-color:#a8d5ba;background:#e9f6ec;color:#1d7a3c;font-weight:600}
 .say{font-size:12.5px;color:#1f2933;line-height:1.7;background:#f6f9fc;border-left:3px solid #2c6cb0;
      padding:7px 10px;border-radius:0 8px 8px 0;min-height:40px}
 .ctrl{display:flex;align-items:center;gap:10px;margin-top:14px;padding:10px 14px;
       background:#fff;border:1px solid #e3e8ee;border-radius:12px}
 button{font:inherit;font-size:12.5px;padding:5px 12px;border:1px solid #dde3ea;border-radius:8px;
        background:#fff;color:#1f2933;cursor:pointer}
 button:hover{border-color:#9fb3c8}
 button.pri{background:#1d7a3c;border-color:#1d7a3c;color:#fff}
 input[type=range]{flex:1;accent-color:#1d7a3c}
 .clock{font-variant-numeric:tabular-nums;font-size:12px;color:#5c6b7a;min-width:88px;text-align:right}
 .footer{margin-top:14px;font-size:12px;color:#5c6b7a;line-height:1.8}
 .legend{display:flex;gap:12px;flex-wrap:wrap;font-size:11px;color:#5c6b7a;margin:4px 0 2px}
 .legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;
           vertical-align:-1px}
 @keyframes pulse{0%{opacity:.35}50%{opacity:1}100%{opacity:.35}}
 .hot{animation:pulse .9s ease-in-out infinite}
</style></head><body>
<h1>分组校准 Demo · 六步流程动画回放</h1>
<p class="sub">数据由 <code>docs/gen_animation_data.py</code> 真实跑**四组流水线**导出（<code>__CHIPDESC__</code>）——
动画只做重放，不编数字。流程：<b>① 读取线 ro/ri 对齐</b> → <b>② 整体右移 headroom</b>（给后续留空间）→
<b>③ 单比特 XY-Z</b> → <b>④ 比特间/耦合器 Z-Z</b>（③④ 不写回，skew 同基线）→
<b>⑤ 末尾统一求解 qubit/coupler 并整批写回</b> →
<b>⑥ 读取线收回 headroom、往左拉回对齐</b>。
按 <code>空格</code> 播放/暂停，<code>←</code> <code>→</code> 切幕。</p>

<div class="nav" id="nav"></div>

<div class="grid">
  <div class="card">
    <h2>芯片图 · 四组测量边（读取线 → 单比特 → 耦合器 → 收尾拉回对齐）</h2>
    <p class="hint" id="chipHint"></p>
    <svg id="chip" viewBox="__CHIP_VB__"></svg>
    <div class="legend">
      <span><i style="background:#1d7a3c"></i>组 1 读取线 ro / ri</span>
      <span><i style="background:#2c6cb0"></i>组 2 比特 xy / z</span>
      <span><i style="background:#6b3fa0"></i>组 3 耦合器 z / 跨比特</span>
      <span><i style="background:#c2410c"></i>组 4 收回 headroom · 拉回对齐</span>
      <span><i style="background:#d97706"></i>正在测量的边</span>
    </div>
  </div>
  <div class="card">
    <h2>时间轴 · 每条线的时序位置 q = L + d₁ + d₂ + d₃</h2>
    <p class="hint">灰条 = 初始真值 L（不动）· 彩条 = 位移：<b>d₁</b> readout（含 headroom），
    <b>d₂</b> qubit/coupler 末尾落盘，<b>d₃</b> readout_final 收尾——
    <b>d₃ 可为负</b>（收回 headroom = 往左拉回）· 虚线描边 = 计划未落盘 · 竖线 = 目标锚线</p>
    <svg id="time" viewBox="__TIME_VB__"></svg>
  </div>
</div>

<div class="below">
  <div class="card">
    <h2>测量台账（组 1 即时写回 · 组 2/3 只记录）</h2>
    <div class="scroll"><table id="ledger"></table></div>
  </div>
  <div class="card">
    <h2>状态与守卫</h2>
    <div class="badges" id="badges"></div>
    <div class="guards" id="guards"></div>
    <div class="say" id="say"></div>
  </div>
</div>

<div class="ctrl">
  <button id="play" class="pri">⏸ 暂停</button>
  <button id="prev">⏮ 上一幕</button>
  <button id="next">下一幕 ⏭</button>
  <input type="range" id="bar" min="0" max="1000" value="0">
  <span class="clock" id="clock"></span>
  <button id="speed">1×</button>
  <button id="restart">↺ 重播</button>
</div>

<p class="footer">
<b>六步流程要点：</b>读取脉冲与采样窗口没对齐，X_π 做出来也读不出信号，所以<b>组 1 不能等</b>——
测完立刻求解、立刻落盘，随后给读取线整体加 <i>headroom</i>（本例 <code>__HEADROOM__</code> ns）右移写回，
主动让出空间。组 2（单比特 XY-Z）与组 3（比特间 Z-Z / 耦合器 Z）之间<b>一个字节都不写</b>，
两组 skew 同基线，因此末尾能合成<b>一次</b>加权最小二乘，把所有 xy/z 对齐（<i>d₂</i> 后走）。
最后组 4 用 readout↔首腔 qubit.xy 的实测 skew 收尾（<i>d₃</i> 最后走）：锚取 qubit/coupler 最右的线，
读取线把步 2 主动右移的 headroom <b>按需收回（d₃ 为负、往左拉回，不超过步 2 授予的额度）</b>，对齐到 qubit.xy。
单调守卫 δ≥0 管的是<b>校准位移</b>；headroom 是流水线自己授予的临时余量，单独记账——
收回量受授予台账约束，d 永远不会低于加 headroom 之前的校准基线。没加 headroom 时
读取线本来就在锚线左边，照旧只向右拉。
锚点取 qubit/coupler 中当前最靠右的线（本例 <code>__ANCHOR__</code>）：只能右移，它既可达、又让总位移最小。
</p>

<script>
const DATA = __ANIM_DATA__;
const LINES = DATA.lines, TREE = DATA.tree, MEAS = DATA.measurements, CAND = DATA.candidates;
const STAGES = DATA.stages || [];
const SOLVE = DATA.solve || {}, REP = DATA.report || {};
const META = DATA.meta || {}, CL = DATA.layout.chip, TL = DATA.layout.time;
const N = LINES.length, ANCHOR = SOLVE.anchor || '—';
const TARGET = Math.max(...LINES.map(l => l.L));

/* 三段位移：d1 = 组 1（读取线）即时落盘的那段；d2 = 末尾整批落盘的那段；
   d3 = readout_final 的**净**位移（shifts − headroom_return），收回 headroom 时为负 */
const stByName = {};
STAGES.forEach(s => { stByName[s.name] = s; });
const S1 = (stByName.readout || {}).shifts || {};
const S2 = SOLVE.shifts || {};
const S3f = (stByName.readout_final || {}).shifts || {};
const S3r = (stByName.readout_final || {}).headroom_return || {};
const S3 = {};
new Set([...Object.keys(S3f), ...Object.keys(S3r)]).forEach(ln => {
  S3[ln] = (S3f[ln] || 0) - (S3r[ln] || 0);
});
const TOTD = Object.values(S2).reduce((a, b) => a + b, 0);
const TOTD3 = Object.values(S3).reduce((a, b) => a + b, 0);
const SUMD1 = Object.values(S1).reduce((a, b) => a + b, 0);
const SUMD3 = Object.values(S3).reduce((a, b) => a + b, 0);
const SUMRET = Object.values(S3r).reduce((a, b) => a + b, 0);
const MAXD1 = Math.max(0, ...Object.values(S1));
const MAXD = Math.max(0, ...LINES.map(l => (l.d1 || 0) + (l.d2 || 0) + (l.d3 || 0)));
const RES0 = TARGET - Math.min(...LINES.map(l => l.L));
const RES1 = REP.residual ? REP.residual.residual_ns : 0;

/* 每组各自的测量列表（顺序与 MEAS 一致：readout → qubit → coupler → readout_final） */
const SGTAG = {readout: '读', qubit: '单', coupler: '耦', readout_final: '终'};
const MEAS1 = MEAS.filter(m => m.stage === 'readout');
const MEAS2 = MEAS.filter(m => m.stage === 'qubit');
const MEAS3 = MEAS.filter(m => m.stage === 'coupler');
const MEAS4 = MEAS.filter(m => m.stage === 'readout_final');
const EDGE1 = TREE.filter(t => t.group === 'readout');
const EDGE2 = TREE.filter(t => t.group === 'qubit');
const EDGE3 = TREE.filter(t => t.group === 'coupler');
const EDGE4 = TREE.filter(t => t.group === 'readout_final');

const ACTS = [
  {id:'chip',    name:'① 生成芯片',                 dur:2600},
  {id:'truth',   name:'② 真值 L',                   dur:2500},
  {id:'pending', name:'③ 待测文件',                 dur:1900},
  {id:'plan',    name:'④ 四组实验计划',             dur:5600},
  {id:'meas1',   name:'⑤ 组 1 · 读取线测量',        dur:4200},
  {id:'write1',  name:'⑥ 组 1 · 立刻写回（含 headroom）', dur:4600},
  {id:'meas2',   name:'⑦ 组 2 · 单比特（只记录）',  dur:5200},
  {id:'meas3',   name:'⑧ 组 3 · 耦合器（只记录）',  dur:4400},
  {id:'solve',   name:'⑨ 末尾统一求解 qubit/coupler', dur:4200},
  {id:'write',   name:'⑩ 整批一次写回',             dur:4000},
  {id:'meas4',   name:'⑪ 组 4 · readout_final 测量', dur:4200},
  {id:'write4',  name:'⑫ 组 4 · 收回 headroom 写回', dur:4000},
  {id:'check',   name:'⑬ 与真值对账',               dur:3000}
];
const START = [];
ACTS.reduce((acc, a) => { START.push(acc); return acc + a.dur; }, 0);
const TOTAL = START[START.length - 1] + ACTS[ACTS.length - 1].dur;

const SVGNS = 'http://www.w3.org/2000/svg';
function mk(tag, attrs, parent) {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
const ease = t => t < .5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
const clamp01 = t => Math.max(0, Math.min(1, t));
const fmt = (v, n) => Number(v).toFixed(n === undefined ? 1 : n);

/* ---------------- 芯片图：坐标全部来自 DATA.layout ---------------- */
const ANCH = CL.anchor;
const keyOf = (a, b) => [a, b].sort().join('|');
const anchor = line => ANCH[line] || null;
const colorOf = g => g === 'coupler' ? '#6b3fa0' : (g === 'readout' ? '#1d7a3c' : (g === 'readout_final' ? '#c2410c' : '#2c6cb0'));

const chipSvg = document.getElementById('chip');
const gCand = mk('g', {}, chipSvg), gTree = mk('g', {}, chipSvg), gNode = mk('g', {}, chipSvg);
const candEls = {}, treeEls = {};

CAND.forEach(c => {
  const p1 = anchor(c.a), p2 = anchor(c.b);
  if (!p1 || !p2) return;
  candEls[keyOf(c.a, c.b)] = mk('line', {x1: p1[0], y1: p1[1], x2: p2[0], y2: p2[1],
                                         stroke: '#c7ced6', 'stroke-width': 1}, gCand);
});
TREE.forEach(t => {
  const p1 = anchor(t.a), p2 = anchor(t.b);
  if (!p1 || !p2) return;
  const g = t.group || (t.cost <= 1 ? 'readout' : (t.cost <= 2 ? 'qubit' : 'coupler'));
  const el = mk('line', {x1: p1[0], y1: p1[1], x2: p2[0], y2: p2[1],
                         stroke: colorOf(g), 'stroke-width': 2.2,
                         'stroke-linecap': 'round', opacity: 0}, gTree);
  treeEls[keyOf(t.a, t.b)] = {el, group: g, edge: t};
});

const nodeEls = {};
CL.couplers.forEach(c => {
  const g = mk('g', {}, gNode), half = c.half;
  mk('rect', {x: c.cx - half, y: c.cy - half, width: 2 * half, height: 2 * half, rx: 3,
              fill: '#f2eafb', stroke: '#6b3fa0', 'stroke-width': 1.4}, g);
  nodeEls[c.id + '.z'] = g;
});
CL.readouts.forEach(r => {
  const g = mk('g', {}, gNode);
  mk('rect', {x: r.cx - r.w / 2, y: r.cy - r.h / 2, width: r.w, height: r.h, rx: 8,
              fill: '#e9f6ec', stroke: '#1d7a3c', 'stroke-width': 1.4}, g);
  mk('text', {x: r.cx, y: r.cy - r.h / 2 - 6, 'text-anchor': 'middle',
              style: 'font-size:10.5px;fill:#1d7a3c'}, g).textContent = r.id;
  mk('circle', {cx: r.cx - r.tap, cy: r.cy, r: 5.5, fill: '#1d7a3c'}, g);
  mk('circle', {cx: r.cx + r.tap, cy: r.cy, r: 5.5, fill: '#8fc7a4'}, g);
  nodeEls[r.id + '.ro'] = g; nodeEls[r.id + '.ri'] = g;
});
CL.qubits.forEach(q => {
  const g = mk('g', {}, gNode), D = CL.dot;
  mk('circle', {cx: q.cx, cy: q.cy, r: CL.r, fill: '#eaf2fb', stroke: '#2c6cb0',
                'stroke-width': 1.6}, g);
  mk('text', {x: q.cx, y: q.cy + CL.r + 15, 'text-anchor': 'middle', class: 't',
              fill: '#2c6cb0'}, g).textContent = q.id;
  mk('circle', {cx: q.cx - D.dx, cy: q.cy - D.dy, r: D.rad, fill: '#2c6cb0'}, g);
  mk('circle', {cx: q.cx + D.dx, cy: q.cy + D.dy, r: D.rad, fill: '#6b3fa0'}, g);
  nodeEls[q.id + '.xy'] = g; nodeEls[q.id + '.z'] = g;
});

/* ---------------- 时间轴：量程 / 行高同样来自 DATA.layout ---------------- */
const timeSvg = document.getElementById('time');
const {x0: X0, x1: X1, y0: Y0, step: STEP, dom: DOM} = TL;
const xs = v => X0 + (Math.max(0, Math.min(DOM, v)) / DOM) * (X1 - X0);
const rowEls = [];
LINES.forEach((ln, i) => {
  const y = Y0 + i * STEP, g = mk('g', {}, timeSvg), col = colorOf(ln.group);
  mk('text', {x: TL.nameX, y: y + 4, class: 'name'}, g).textContent = ln.name;
  const base = mk('rect', {x: xs(0), y: y - 5, width: 0, height: 11, rx: 2,
                           fill: '#c7ced6'}, g);
  /* 三段位移条：d1（readout 含 headroom）、d2（qubit/coupler 末尾）、d3（readout_final） */
  const mov1 = mk('rect', {x: xs(ln.L), y: y - 5, width: 0, height: 11, rx: 2,
                           fill: col, stroke: col, 'stroke-width': 0}, g);
  const mov2 = mk('rect', {x: xs(ln.L), y: y - 5, width: 0, height: 11, rx: 2,
                           fill: col, stroke: col, 'stroke-width': 0}, g);
  const mov3 = mk('rect', {x: xs(ln.L), y: y - 5, width: 0, height: 11, rx: 2,
                           fill: col, stroke: col, 'stroke-width': 0}, g);
  const tip = mk('rect', {x: xs(ln.L) - 2, y: y - 8, width: 4, height: 17, rx: 1.5,
                          fill: '#1f2933'}, g);
  rowEls.push({g, base, mov1, mov2, mov3, tip, ln});
});
const targetLine = mk('line', {x1: TL.targetX, y1: Y0 - 18, x2: TL.targetX, y2: TL.axisY,
                               stroke: '#1d7a3c', 'stroke-width': 1.4, 'stroke-dasharray': '5 4',
                               opacity: 0}, timeSvg);
const targetTxt = mk('text', {x: TL.targetX, y: Y0 - 24, 'text-anchor': TL.targetAnchor,
                              class: 's', fill: '#1d7a3c', opacity: 0}, timeSvg);
targetTxt.textContent = '目标锚线 ' + ANCHOR;
TL.ticks.forEach(v => {
  mk('line', {x1: xs(v), y1: TL.axisY, x2: xs(v), y2: Y0 + N * STEP - 2,
              stroke: '#c7ced6', 'stroke-width': 1}, timeSvg);
  mk('text', {x: xs(v), y: Y0 + N * STEP + 15, 'text-anchor': 'middle', class: 'tick'}, timeSvg)
    .textContent = (v % 1 ? v.toFixed(1) : v) + ' ns';
});
const resTxt = mk('text', {x: X0, y: TL.resY, class: 't', fill: '#1f2933'}, timeSvg);
const resSub = mk('text', {x: X0, y: TL.subY, class: 's', fill: '#5c6b7a'}, timeSvg);

/* ---------------- 台账 / 徽标 / 守卫 / 字幕 ---------------- */
const ledTab = document.getElementById('ledger');
const head = document.createElement('tr');
['组', '边', 'skew ns', 'σ ns', 'quality', '尝试'].forEach(h => {
  const th = document.createElement('th'); th.textContent = h; head.appendChild(th);
});
ledTab.appendChild(head);
const ledRows = MEAS.map(m => {
  const tr = document.createElement('tr');
  [SGTAG[m.stage] || '—', m.a + ' ⇄ ' + m.b, fmt(m.skew_ns, 2), fmt(m.sigma_ns, 3),
   fmt(m.quality, 3), m.attempts].forEach(v => {
    const td = document.createElement('td'); td.textContent = v; tr.appendChild(td);
  });
  ledTab.appendChild(tr);
  return tr;
});
const badges = document.getElementById('badges');
const bd = {};
[['file', '待测文件'], ['sess', '会话累计 d'], ['dev', '设备已下发']].forEach(it => {
  const d = document.createElement('div'); d.className = 'badge';
  const b = document.createElement('b'); b.textContent = it[1] + '：';
  const s = document.createElement('span'); d.appendChild(b); d.appendChild(s);
  badges.appendChild(d); bd[it[0]] = {box: d, val: s};
});
const guardBox = document.getElementById('guards');
const gEls = ['CAS 指纹', '单调 δ≥0', '预算 d≤dmax', '原子写 + 审计'].map(t => {
  const d = document.createElement('div'); d.className = 'guard'; d.textContent = t;
  guardBox.appendChild(d); return d;
});
const say = document.getElementById('say');

/* ---------------- 导航 ---------------- */
const nav = document.getElementById('nav');
const pills = ACTS.map((a, i) => {
  const b = document.createElement('button'); b.className = 'pill'; b.textContent = a.name;
  b.onclick = () => seek(START[i] + 1);
  nav.appendChild(b); return b;
});

/* ---------------- 时钟 ---------------- */
let T = 0, playing = true, rate = 1, last = 0;
const bar = document.getElementById('bar'), clock = document.getElementById('clock');
const playBtn = document.getElementById('play'), spdBtn = document.getElementById('speed');
playBtn.onclick = () => {
  if (!playing && T >= TOTAL - 1) T = 0;
  playing = !playing; last = 0;
  playBtn.textContent = playing ? '⏸ 暂停' : '▶ 播放';
};
spdBtn.onclick = () => {
  rate = rate === 1 ? 2 : (rate === 2 ? 4 : (rate === 4 ? .5 : 1));
  spdBtn.textContent = rate + '×';
};
document.getElementById('restart').onclick = () => {
  T = 0; playing = true; playBtn.textContent = '⏸ 暂停';
};
function actIndex(t) {
  let i = 0;
  for (let k = 0; k < START.length; k++) if (t >= START[k]) i = k;
  return i;
}
function step(dir) {
  const cur = actIndex(T);
  if (dir < 0) T = T - START[cur] > 350 ? START[cur] : START[Math.max(0, cur - 1)];
  else T = cur + 1 < ACTS.length ? START[cur + 1] : TOTAL - 1;
}
function seek(t) { T = Math.max(0, Math.min(TOTAL - 1, t)); }
document.getElementById('prev').onclick = () => step(-1);
document.getElementById('next').onclick = () => step(1);
bar.oninput = () => { T = (bar.value / 1000) * TOTAL; };
document.onkeydown = e => {
  if (e.code === 'Space') { e.preventDefault(); playBtn.onclick(); }
  if (e.code === 'ArrowRight') step(1);
  if (e.code === 'ArrowLeft') step(-1);
  if (e.key === 'r' || e.key === 'R') document.getElementById('restart').onclick();
};

/* ---------------- 每帧渲染 ---------------- */
function locate(t) {
  const i = actIndex(t);
  return {i, p: clamp01((t - START[i]) / ACTS[i].dur)};
}

function vm(t) {
  const loc = locate(t), i = loc.i, p = loc.p, id = ACTS[i].id;
  const v = {id, p, i, lit: {readout: 0, qubit: 0, coupler: 0, readout_final: 0}, candAlpha: 0,
             measured: 0, hot: null, f1: 0, f2: 0, f3: 0, solid1: false, solid2: false, solid3: false,
             guard: 0, written1: false, written2: false, written3: false, applied: false,
             showTarget: 0, hint: '', sub: ''};
  const at = (arr, n) => arr[Math.max(0, Math.min(n, arr.length - 1))] || null;

  if (id === 'chip') {
    v.sub = '由构型参数推出 pos / 耦合器 / connections —— ' + (META.chip || {}).description;
  } else if (id === 'truth') {
    v.sub = '每条线拿到一个隐藏真值 L（' + fmt(Math.min(...LINES.map(l => l.L))) + '–'
          + fmt(TARGET) + ' ns）；初始失配 ' + fmt(RES0) + ' ns，这就是要校掉的东西。';
  } else if (id === 'pending') {
    v.sub = '待测文件全零：d = 0，q = L。truth 只用来装载设备、事后对账，流程全程不读它。';
  } else if (id === 'plan') {
    v.candAlpha = clamp01(p / .2);
    const seg = clamp01((p - .2) / .75);
    v.lit = {readout: Math.floor(clamp01(seg / .25) * EDGE1.length),
             qubit: Math.floor(clamp01((seg - .25) / .25) * EDGE2.length),
             coupler: Math.floor(clamp01((seg - .50) / .25) * EDGE3.length),
             readout_final: Math.floor(clamp01((seg - .75) / .25) * EDGE4.length)};
    const nsel = v.lit.readout + v.lit.qubit + v.lit.coupler + v.lit.readout_final;
    v.hint = '候选边 ' + CAND.length + ' 条 · 已选 ' + nsel + ' / ' + TREE.length + ' 条：'
           + '组 1 读取线 ' + v.lit.readout + ' · 组 2 单比特 ' + v.lit.qubit
           + ' · 组 3 耦合器 ' + v.lit.coupler + ' · 组 4 最终对齐 ' + v.lit.readout_final;
    v.sub = '按物理依赖分四组，各自挑代价最小的生成树：<b>组 1</b> "同线 ri↔ro" 对齐后'
          + '整体右移 headroom（给后续留空间）；<b>组 2</b> 每比特 z↔xy；<b>组 3</b> 比特间 z-z、'
          + '耦合器 CP.z 挂相邻比特（可调用 Q.z，不可调用 Q.xy）；<b>组 4</b> 用 readout↔'
          + '首腔 qubit.xy 的实测 skew 收尾：收回 headroom，把读取线拉回对齐。'
          + '前一组没就绪，后一组不会开始。';
  } else if (id === 'meas1') {
    v.candAlpha = 1; v.lit = {readout: EDGE1.length, qubit: 0, coupler: 0};
    v.measured = Math.floor(p * MEAS1.length);
    v.hot = at(MEAS1, v.measured);
    v.hint = '组 1 测量 ' + v.measured + ' / ' + MEAS1.length + ' 条 · 当前：'
           + (v.hot ? v.hot.a + ' ⇄ ' + v.hot.b : '—');
    v.sub = '读取脉冲的发射(ro)与采样窗口(ri)没对准，后面<b>什么都读不出来</b>，X_π 更无从谈起'
          + '——所以这组必须最先做，而且结果<b>不能攒到最后</b>。';
  } else if (id === 'write1') {
    v.candAlpha = 1; v.lit = {readout: EDGE1.length, qubit: 0, coupler: 0};
    v.measured = MEAS1.length;
    v.f1 = ease(clamp01(p / .8));
    v.guard = Math.floor(clamp01(p / .55) * 4);
    v.written1 = p >= .55; v.solid1 = true; v.applied = p >= .8;
    v.hint = '组 1 求解 → 四道守卫 → 立刻落盘 · 右移 ' + fmt(SUMD1) + ' ns';
    v.sub = v.written1
      ? '每条读取线内部按分量定标后就地写回：<b>d₁ 这两段先走</b>。'
        + '后面的单比特、耦合器实验都站在"读取已可用"的新基线上。'
      : '组 1 的分量求解（各条读取线互无基准）→ 守卫依次检查：CAS 指纹 → 单调 δ≥0 → '
        + '预算 d≤dmax → 原子写。';
  } else if (id === 'meas2') {
    v.candAlpha = 1; v.lit = {readout: EDGE1.length, qubit: EDGE2.length, coupler: 0, readout_final: 0};
    v.measured = MEAS1.length + Math.floor(p * MEAS2.length);
    v.hot = at(MEAS2, v.measured - MEAS1.length);
    v.f1 = 1; v.solid1 = true; v.written1 = true; v.applied = true;
    v.hint = '组 2 测量 ' + (v.measured - MEAS1.length) + ' / ' + MEAS2.length + ' 条 · 当前：'
           + (v.hot ? v.hot.a + ' ⇄ ' + v.hot.b : '—');
    v.sub = '有了读取基准，才能逐比特把 xy 与 z 对准。<b>这一组只记录不写回</b>——'
          + '现在写回会让组 3 站在另一个基线上，两组 skew 就不再可比。';
  } else if (id === 'meas3') {
    v.candAlpha = 1; v.lit = {readout: EDGE1.length, qubit: EDGE2.length, coupler: EDGE3.length, readout_final: 0};
    v.measured = MEAS1.length + MEAS2.length + Math.floor(p * MEAS3.length);
    v.hot = at(MEAS3, v.measured - MEAS1.length - MEAS2.length);
    v.f1 = 1; v.solid1 = true; v.written1 = true; v.applied = true;
    v.hint = '组 3 测量 ' + (v.measured - MEAS1.length - MEAS2.length) + ' / ' + MEAS3.length
           + ' 条 · 当前：' + (v.hot ? v.hot.a + ' ⇄ ' + v.hot.b : '—');
    v.sub = '单比特门能跑通之后，做相邻比特 z-z 与耦合器 CP.z 挂到相邻比特。组 2 与组 3 之间<b>没有任何写回</b>，'
          + '所以这两组观测<b>同基线</b>——这正是末尾能把它们合并求解的前提。';
  } else if (id === 'solve') {
    v.candAlpha = 1; v.lit = {readout: EDGE1.length, qubit: EDGE2.length, coupler: EDGE3.length, readout_final: 0};
    v.measured = MEAS.length;
    v.f1 = 1; v.solid1 = true; v.written1 = true; v.applied = true;
    v.f2 = ease(clamp01(p / .8));
    v.showTarget = clamp01((p - .1) / .3);
    v.hint = '末尾统一求解：' + N + ' 条线 · ' + (MEAS2.length + MEAS3.length)
           + ' 条观测 · 迭代 ' + (SOLVE.iterations || 0) + ' 次';
    v.sub = '输入 = 组 2 + 组 3 的全部合格观测（同基线），做加权最小二乘（权重 1/σ²）把误差摊到整张图，'
          + '锚点取当前最右的 <code>' + ANCHOR + '</code>。此刻<b>d₂ 还只是计划</b>（虚线），'
          + 'readout 暂时不动。';
  } else if (id === 'write') {
    v.candAlpha = 1; v.lit = {readout: EDGE1.length, qubit: EDGE2.length, coupler: EDGE3.length, readout_final: 0};
    v.measured = MEAS.length;
    v.f1 = 1; v.f2 = 1; v.solid1 = true; v.solid2 = true;
    v.written1 = true; v.showTarget = 1;
    v.guard = Math.floor(clamp01(p / .55) * 4);
    v.written2 = p >= .55; v.applied = p >= .8;
    v.hint = '四道守卫 → 整批一次落盘 · qubit/coupler 总位移 ' + fmt(TOTD) + ' ns';
    v.sub = v.written2
      ? '四道守卫全过：<b>一张</b> WritePlan 一次写完所有 xy/z（d₂ 这段后走），readout 暂时不动。'
      : '守卫依次检查：CAS 指纹 → 单调 δ≥0 → 预算 d≤dmax → 原子写（os.replace + .bak + JSONL 审计）。';
  } else if (id === 'meas4') {
    v.candAlpha = 1; v.lit = {readout: EDGE1.length, qubit: EDGE2.length, coupler: EDGE3.length, readout_final: EDGE4.length};
    v.measured = MEAS1.length + MEAS2.length + MEAS3.length + Math.floor(p * MEAS4.length);
    v.hot = at(MEAS4, v.measured - MEAS1.length - MEAS2.length - MEAS3.length);
    v.f1 = 1; v.f2 = 1; v.solid1 = true; v.solid2 = true;
    v.written1 = true; v.written2 = true; v.applied = true;
    v.hint = '组 4 测量 ' + (v.measured - MEAS1.length - MEAS2.length - MEAS3.length) + ' / ' + MEAS4.length
           + ' 条 · 当前：' + (v.hot ? v.hot.a + ' ⇄ ' + v.hot.b : '—');
    v.sub = 'qubit/coupler 已全局对齐并写回后，测量每条读取线的 ro/ri 与其首个腔 qubit.xy 的 '
          + 'skew——<b>这组观测是全流程里唯一把"读取线世界"和"qubit/coupler 世界"连起来的桥</b>，'
          + '末尾据此把读取线拉回对齐。';
  } else if (id === 'write4') {
    v.candAlpha = 1; v.lit = {readout: EDGE1.length, qubit: EDGE2.length, coupler: EDGE3.length, readout_final: EDGE4.length};
    v.measured = MEAS.length;
    v.f1 = 1; v.f2 = 1; v.f3 = ease(clamp01(p / .8));
    v.solid1 = true; v.solid2 = true;
    v.written1 = true; v.written2 = true; v.applied = true;
    v.guard = Math.floor(clamp01(p / .55) * 4);
    v.written3 = p >= .55; v.solid3 = p >= .55;
    v.hint = '组 4 收尾求解 → 四道守卫（含 headroom 收回校验）→ 立刻落盘 · 收回 '
           + fmt(SUMRET) + ' ns';
    v.sub = v.written3
      ? '锚取 qubit/coupler 最右的线：读取线<b>收回步 2 主动右移的 headroom（d₃ 为负、往左）</b>，'
        + '拉回到 qubit.xy 对齐；收回量受授予台账约束，d 不低于校准基线。'
      : 'readout↔qubit.xy 实测 skew + "qubit/coupler 已对齐"软约束（σ 取步 5 残差）'
        + '合成一次全局最小二乘；读取线在锚线右侧的部分以收回 headroom 的方式往左拉回。';
  } else {
    const r = REP.residual || {};
    v.candAlpha = 1; v.lit = {readout: EDGE1.length, qubit: EDGE2.length, coupler: EDGE3.length, readout_final: EDGE4.length};
    v.measured = MEAS.length;
    v.f1 = 1; v.f2 = 1; v.f3 = 1;
    v.solid1 = true; v.solid2 = true; v.solid3 = true;
    v.written1 = true; v.written2 = true; v.written3 = true; v.showTarget = 1; v.guard = 4; v.applied = true;
    v.hint = '对账：残差 ' + fmt(RES1, 3) + ' ns ≤ tol ' + fmt(META.tol_ns) + ' ns';
    v.sub = r.residual_ns === undefined
      ? '本轮没有真值可对账（离线演示或未传 truth）：只看求解与写回收据。'
      : '与隐藏真值对账：' + N + ' 条线全部落在 ' + fmt(r.min_ns, 2) + '–' + fmt(r.max_ns, 2)
        + ' ns，残差 ' + fmt(RES1, 3) + ' ns → <b>aligned = ' + (REP.aligned ? 'True' : 'False')
        + '</b>。三段落盘：组 1 ' + fmt(SUMD1) + ' ns，末尾 ' + fmt(TOTD) + ' ns，'
        + '组 4 往左收回 headroom ' + fmt(SUMRET) + ' ns。';
  }
  return v;
}

function draw(v) {
  /* 芯片图：每组边只亮到该组的进度为止 */
  gCand.setAttribute('opacity', v.candAlpha * (v.id === 'plan' ? .9 : .5));
  const seen = {readout: 0, qubit: 0, coupler: 0};
  TREE.forEach(t => {
    const rec = treeEls[keyOf(t.a, t.b)];
    if (!rec) return;
    const g = rec.group, idx = seen[g]++;
    const lit = idx < (v.lit[g] || 0);
    rec.el.setAttribute('opacity', lit ? .95 : 0);
    const hot = v.hot && keyOf(v.hot.a, v.hot.b) === keyOf(t.a, t.b);
    rec.el.setAttribute('stroke', hot ? '#d97706' : colorOf(g));
    rec.el.setAttribute('stroke-width', hot ? 4.5 : 2.2);
    if (hot) rec.el.setAttribute('class', 'hot'); else rec.el.removeAttribute('class');
  });
  for (const k in nodeEls) {
    const g = nodeEls[k];
    const hot = v.hot && (v.hot.a === k || v.hot.b === k);
    let op = 1;
    if (v.id === 'chip') op = clamp01(v.p / .6);
    else if (v.id === 'plan') op = (v.lit.readout + v.lit.qubit + v.lit.coupler) > 0 ? 1 : .45;
    else if (v.hot) op = hot ? 1 : .45;
    g.setAttribute('opacity', op);
  }
  document.getElementById('chipHint').textContent = v.hint
    || ('候选边 ' + CAND.length + ' 条 · 四组共 ' + TREE.length + ' 条');

  /* 时间轴：d1（readout 含 headroom）→ d2（qubit/coupler 末尾）→ d3（readout_final） */
  let mx = -1e18, mn = 1e18;
  rowEls.forEach(r => {
    const d1 = r.ln.d1 || 0, d2 = r.ln.d2 || 0, d3 = r.ln.d3 || 0;
    const q = r.ln.L + d1 * v.f1 + d2 * v.f2 + d3 * v.f3;
    const xL = xs(r.ln.L), xA = xs(r.ln.L + d1 * v.f1), xB = xs(r.ln.L + d1 * v.f1 + d2 * v.f2), xQ = xs(q);
    r.base.setAttribute('width', Math.max(0, xL - xs(0)));
    /* 段 = [xFrom, xTo]；收回 headroom 时 d3 < 0，段往左画（xTo < xFrom） */
    const paint = (el, xFrom, xTo, solid, frac) => {
      el.setAttribute('x', Math.min(xFrom, xTo));
      el.setAttribute('width', Math.abs(xTo - xFrom));
      el.setAttribute('fill-opacity', solid ? 1 : .45);
      el.setAttribute('stroke-width', (!solid && frac) ? 1.3 : 0);
      if (!solid && frac) el.setAttribute('stroke-dasharray', '4 3');
      else el.removeAttribute('stroke-dasharray');
    };
    paint(r.mov1, xL, xA, v.solid1, d1 * v.f1);
    paint(r.mov2, xA, xB, v.solid2, d2 * v.f2);
    paint(r.mov3, xB, xQ, v.solid3, d3 * v.f3);
    r.tip.setAttribute('x', xQ - 2);
    r.tip.setAttribute('fill', (v.solid3 || v.solid2 || v.solid1) ? '#1f2933' : '#8a95a0');
    mx = Math.max(mx, q); mn = Math.min(mn, q);
  });
  targetLine.setAttribute('opacity', v.showTarget);
  targetTxt.setAttribute('opacity', v.showTarget);
  resTxt.textContent = '当前残差 max(q) − min(q) = ' + fmt(mx - mn, 3) + ' ns'
    + (v.id === 'check' ? '　→　aligned = ' + (REP.aligned ? 'True' : 'False') : '');
  let st = '初始失配 ' + fmt(RES0) + ' ns';
  if (v.written3) st += ' · 已落盘 max(d) = ' + fmt(MAXD) + ' ns'
                      + '（组 1 ' + fmt(SUMD1) + ' + 末尾 ' + fmt(TOTD) + ' + 组 4 ' + fmt(SUMD3) + '）';
  else if (v.written2) st += ' · 已落盘 qubit/coupler ' + fmt(TOTD) + ' ns · readout_final 未写回';
  else if (v.written1) st += ' · 组 1 已落盘 ' + fmt(SUMD1) + ' ns · 组 2/3 只记录未写回';
  else st += ' · 尚未写回（文件保持全零）';
  resSub.textContent = st;

  /* 台账 */
  ledRows.forEach((tr, i) => {
    const on = i < v.measured;
    tr.style.display = on ? '' : 'none';
    tr.className = (on && i === v.measured - 1 && v.hot) ? 'new' : '';
  });

  /* 徽标 */
  const setBadge = (k, text, ok) => {
    bd[k].val.textContent = text;
    bd[k].box.className = 'badge ' + (ok === null ? '' : (ok ? 'yes' : 'no'));
  };
  const sessVal = (v.written1 ? SUMD1 : 0) + (v.written2 ? TOTD : 0) + (v.written3 ? TOTD3 : 0);
  setBadge('file', v.written3 ? 'max(d) = ' + fmt(MAXD) + ' ns'
                 : (v.written2 ? 'qubit/coupler 已写'
                               : (v.written1 ? '组 1 已写：max(d₁) = ' + fmt(MAXD1) + ' ns'
                                           : '全零（未写回）')),
           (v.written1 || v.written2 || v.written3) ? true : null);
  setBadge('sess', fmt(sessVal) + ' ns', sessVal > 0 ? true : null);
  setBadge('dev', v.applied ? '已下发' : '未下发', v.applied ? true : false);

  /* 守卫 */
  gEls.forEach((g, i) => { g.className = 'guard' + (i < v.guard ? ' on' : ''); });

  /* 字幕 */
  say.innerHTML = '<b>' + ACTS[v.i].name + '</b>　' + v.sub;

  /* 导航 */
  pills.forEach((b, i) => {
    b.className = 'pill' + (i === v.i ? ' on' : (i < v.i ? ' done' : ''));
  });
}

function frame(ts) {
  if (!last) last = ts;
  const dt = Math.min(64, ts - last); last = ts;
  if (playing) { T += dt * rate; if (T >= TOTAL) { T = TOTAL - 1; playing = false;
    playBtn.textContent = '▶ 播放'; } }
  draw(vm(T));
  bar.value = Math.round((T / TOTAL) * 1000);
  clock.textContent = fmt(T / 1000) + 's / ' + fmt(TOTAL / 1000) + 's';
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
</script>
</body></html>
"""


def render(data: dict) -> str:
    """把布局算好塞进 DATA.layout，模板里的占位符逐个替换成真实数字。"""
    payload = dict(data)
    payload["layout"] = build_layout(data)
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    tl, cl = payload["layout"]["time"], payload["layout"]["chip"]
    anchor = (data.get("solve") or {}).get("anchor") or "—"
    desc = ((data.get("meta") or {}).get("chip") or {}).get("description") or "chip"
    headroom = 0.0
    for s in data.get("stages", []):
        if s.get("name") == "readout":
            headroom = float(s.get("headroom_ns") or 0.0)
            break
    html = (TEMPLATE
            .replace("__ANIM_DATA__", blob)
            .replace("__CHIPDESC__", desc)
            .replace("__ANCHOR__", str(anchor))
            .replace("__HEADROOM__", str(int(round(headroom))))
            .replace("__CHIP_VB__", cl["vb"])
            .replace("__TIME_VB__", tl["vb"])
            .replace("__CHIP_COL__", f'{cl["w"] + 28}px'))
    leftovers = [ph for ph in ("__ANIM_DATA__", "__CHIPDESC__", "__ANCHOR__", "__HEADROOM__",
                               "__CHIP_VB__", "__TIME_VB__", "__CHIP_COL__")
                  if ph in html]
    if leftovers:
        raise SystemExit(f"模板占位符未替换：{leftovers}")
    return html


def main() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    lay = build_layout(data)
    problems = selfcheck(data, lay)
    for msg in problems:
        print("  ·", msg)
    if problems:
        print(f"自检未通过：{len(problems)} 处几何问题 —— 仍写出 HTML，但请按上面修数据/布局")
    html = render(data)
    OUT.write_text(html, encoding="utf-8")
    tl, cl = lay["time"], lay["chip"]
    print(f"OK → {OUT.name}  ({len(html) / 1024:.0f} KB · 线 {len(data['lines'])} 条 · "
          f"边 {len(data['tree'])} 条 · 芯片图 {cl['vb'].split(' ')[2]}×{cl['vb'].split(' ')[3]}"
          f" · 时间轴 {tl['vb'].split(' ')[2]}×{tl['vb'].split(' ')[3]} 量程 {tl['dom']} ns)")
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
visualize.py — 芯片可视化前端（自包含 HTML/SVG，零依赖）

职责：把芯片文件（可选叠加连线文件）渲染为单文件 HTML 报告，便于观察拓扑与连线问题：
  * render_chip_html(chip, wiring=None) -> str   — 生成 HTML 字符串；
  * save_visualization(chip, wiring, out)        — 落盘（可选浏览器打开）。

图面（按 pos 二维布局）：比特（圆）· 读取腔（圆角矩形，悬挂于比特上方）·
      读取线（折线串起本线所有腔）· 耦合器（比特间连线 + 中点标）；
      悬停显示设计参数与线路 delay（含通道时一并显示）。
连线存在时另附完整 lines 表格（line → [channel] → delay）。

CLI：
  python -m qxtrl_timing_cali.visualize --chip <chip.json> [--wiring <wiring.json>]
                                        --out chip-viz.html [--open]
"""
from __future__ import annotations

import argparse
import html
import json
import math
import webbrowser
from pathlib import Path

from .chip_gen import line_names

_RL_COLORS = ["#e08a3c", "#c2554f", "#8a6fbf", "#3a9d9d", "#b0873a", "#6a8f3a"]
_Q_COLOR, _C_COLOR, _CP_COLOR = "#2f6fb7", "#7a5cc0", "#58a05c"

_W, _PAD_X, _TOP, _BOTTOM = 1160, 130, 80, 90
_HANG = 58                       # 腔悬挂在比特上方的距离（px）
_R_Q, _W_CAV, _H_CAV = 26, 52, 26


def _esc(v) -> str:
    return html.escape(str(v), quote=True)


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _layout(chip: dict, grid_px: float | None = None
            ) -> tuple[dict[str, tuple[float, float]], float, float, float]:
    """二维布局：pos(mm) → 画布像素。返回 (坐标表, 悬挂距离 hang, 画布高, 画布宽)。

    grid_px 给出时按「每毫米多少像素」固定缩放（覆盖自动上限 260），
    画布宽度随之撑开，不会被裁掉。
    """
    qs = chip["qubits"]
    xs = [q["pos"][0] for q in qs] or [0.0]
    ys = [(q["pos"][1] if len(q["pos"]) > 1 else 0.0) for q in qs] or [0.0]
    lo_x, lo_y = min(xs), min(ys)
    span_x = max(max(xs) - lo_x, 0.8)
    span_y = max(max(ys) - lo_y, 0.0)
    s = float(grid_px) if grid_px else min(260.0, (_W - 2 * _PAD_X) / span_x)

    rows = sorted(set(ys))
    gaps = [rows[i + 1] - rows[i] for i in range(len(rows) - 1)]
    hang = _HANG if not gaps else max(26.0, min(_HANG, 0.45 * min(gaps) * s))
    pos = {q["id"]: (_PAD_X + (q["pos"][0] - lo_x) * s,
                     _TOP + hang + ((q["pos"][1] if len(q["pos"]) > 1 else 0.0) - lo_y) * s)
           for q in qs}
    return (pos, hang, _TOP + hang + span_y * s + _BOTTOM,
            max(_W, 2 * _PAD_X + span_x * s))


def _extend(p: tuple[float, float], toward: tuple[float, float], amount: float) -> tuple[float, float]:
    """从 p 沿 p→toward 方向移动 amount（负值反向）。"""
    dx, dy = toward[0] - p[0], toward[1] - p[1]
    n = math.hypot(dx, dy) or 1.0
    return p[0] + dx / n * amount, p[1] + dy / n * amount


def _pts_str(pts: list[tuple[float, float]]) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def _svg(chip: dict, wiring_by_line: dict, grid_px: float | None = None) -> str:
    pos, hang, height, width = _layout(chip, grid_px)
    tunable = bool(chip["meta"].get("tunable"))
    cav_by_id = {c["id"]: c for c in chip["cavities"]}
    q_by_id = {q["id"]: q for q in chip["qubits"]}
    parts: list[str] = []

    def winfo(name: str, prefix: str) -> list[str]:
        e = wiring_by_line.get(name)
        if not e:
            return []
        ch = e.get("channel")
        return [f"{prefix}: {ch} · delay={_fmt(e['delay'])} ns" if ch
                else f"{prefix}: delay={_fmt(e['delay'])} ns"]

    def cav_xy(cid: str) -> tuple[float, float] | None:
        cav = cav_by_id.get(cid)
        if not cav or cav["qubit"] not in pos:
            return None
        px, py = pos[cav["qubit"]]
        return px, py - hang

    # -- 读取线（串起本线所有腔） --
    for k, rl in enumerate(chip["readout_lines"]):
        color = _RL_COLORS[k % len(_RL_COLORS)]
        pts = [p for p in (cav_xy(c) for c in rl["cavities"]) if p]
        if not pts:
            continue
        if len(pts) == 1:
            pts = [(pts[0][0] - 42, pts[0][1]), (pts[0][0] + 42, pts[0][1])]
        head = _extend(pts[0], pts[1], -42)
        tail = _extend(pts[-1], pts[-2], 42)
        line_pts = [head, *pts, tail]
        tip = [f"读取线 {rl['id']}（{len(rl['cavities'])} 腔：{'、'.join(rl['cavities'])}）"] \
            + winfo(f"{rl['id']}.ro", "ro") + winfo(f"{rl['id']}.ri", "ri")
        parts.append(
            f'<g><title>{_esc(chr(10).join(tip))}</title>'
            f'<polyline points="{_pts_str(line_pts)}" fill="none" stroke="{color}" '
            f'stroke-width="7" stroke-linejoin="round" stroke-linecap="round" opacity="0.85"/>'
            f'<text x="{head[0] - 8:.1f}" y="{head[1] - 10:.1f}" class="rlab" '
            f'fill="{color}">{_esc(rl["id"])}</text></g>')

    # -- 耦合器（比特间连线 + 中点标） --
    for cp in chip["couplers"]:
        a, b = (q_by_id.get(x) for x in cp["qubits"])
        if not (a and b) or a["id"] not in pos or b["id"] not in pos:
            continue
        ax, ay = pos[a["id"]]
        bx, by = pos[b["id"]]
        dx, dy = bx - ax, by - ay
        dist = math.hypot(dx, dy) or 1.0
        ux, uy = dx / dist, dy / dist
        sx, sy = ax + ux * (_R_Q + 2), ay + uy * (_R_Q + 2)
        ex, ey = bx - ux * (_R_Q + 2), by - uy * (_R_Q + 2)
        mx, my = (ax + bx) / 2, (ay + by) / 2
        ox, oy = -uy * 16, ux * 16          # 标签沿垂线方向让开连线
        tip = [f"耦合器 {cp['id']}：{cp['qubits'][0]} ↔ {cp['qubits'][1]}",
               f"g_max={_fmt(cp['design'].get('g_max'))} MHz · g_off={_fmt(cp['design'].get('g_off'))} MHz"] \
            + (winfo(f"{cp['id']}.z", "z") if tunable else [])
        parts.append(
            f'<g><title>{_esc(chr(10).join(tip))}</title>'
            f'<line x1="{sx:.1f}" y1="{sy:.1f}" x2="{ex:.1f}" y2="{ey:.1f}" '
            f'stroke="{_CP_COLOR}" stroke-width="3"/>'
            f'<rect x="{mx - 7}" y="{my - 7}" width="14" height="14" rx="3" fill="{_CP_COLOR}"/>'
            f'<text x="{mx + ox:.1f}" y="{my + oy + 4:.1f}" class="clab">{_esc(cp["id"])}</text></g>')

    # -- 腔 + 悬挂线 + 比特 --
    for q in chip["qubits"]:
        px, py = pos[q["id"]]
        cav = cav_by_id.get(q.get("cavity"))
        if cav:
            cy = py - hang
            rl_idx = next((i for i, l in enumerate(chip["readout_lines"])
                           if cav["id"] in l["cavities"]), 0)
            color = _RL_COLORS[rl_idx % len(_RL_COLORS)]
            d = cav.get("design", {})
            cav_tip = (f"腔 {cav['id']}：f_r={_fmt(d.get('f_r'))} GHz · "
                       f"kappa={_fmt(d.get('kappa'))} MHz · line={cav.get('line')}")
            parts.append(
                f'<g><title>{_esc(cav_tip)}</title>'
                f'<line x1="{px:.1f}" y1="{cy:.1f}" x2="{px:.1f}" y2="{py - _R_Q:.1f}" '
                f'stroke="{color}" stroke-width="2" opacity="0.7"/>'
                f'<rect x="{px - _W_CAV / 2:.1f}" y="{cy - _H_CAV / 2:.1f}" '
                f'width="{_W_CAV}" height="{_H_CAV}" rx="7" fill="{_C_COLOR}" opacity="0.9"/>'
                f'<text x="{px:.1f}" y="{cy + 5:.1f}" class="ctab">{_esc(cav["id"])}</text></g>')
        dq = q.get("design", {})
        tip = [f"比特 {q['id']}（{'可调' if tunable else '固定'}）· pos=({_fmt(q['pos'][0])}, "
               f"{_fmt(q['pos'][1] if len(q['pos']) > 1 else 0.0)}) mm",
               f"f01={_fmt(dq.get('f01'))} GHz · annh={_fmt(dq.get('anharmonicity'))} MHz",
               f"g={_fmt(dq.get('g_cavity'))} MHz · chi={_fmt(dq.get('chi'))} MHz",
               f"sweet_spot={_fmt(dq.get('sweet_spot'))} Phi0"] \
            + winfo(f"{q['id']}.xy", "xy") + (winfo(f"{q['id']}.z", "z") if tunable else [])
        parts.append(
            f'<g><title>{_esc(chr(10).join(tip))}</title>'
            f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{_R_Q}" fill="{_Q_COLOR}" '
            f'stroke="#1c4670" stroke-width="2"/>'
            f'<text x="{px:.1f}" y="{py + 6:.1f}" class="qtab">{_esc(q["id"])}</text></g>')

    return (f'<svg viewBox="0 0 {width:.0f} {height:.0f}" xmlns="http://www.w3.org/2000/svg" '
            f'role="img">' + "".join(parts) + "</svg>")


def render_chip_html(chip: dict, wiring: dict | None = None,
                     grid_px: float | None = None) -> str:
    meta = chip.get("meta", {})
    wiring_by_line = {e["line"]: e for e in (wiring or {}).get("lines", [])}
    tunable = bool(meta.get("tunable"))
    svg = _svg(chip, wiring_by_line, grid_px)

    missing = sorted(set(line_names(chip)) - set(wiring_by_line)) if wiring else []
    extra = sorted(set(wiring_by_line) - set(line_names(chip))) if wiring else []

    table = ""
    if wiring:
        has_ch = any(e.get("channel") for e in wiring["lines"])
        unit = (wiring.get("units") or {}).get("time", "ns")
        ch_head = "<th>channel</th>" if has_ch else ""
        ch_cell = (lambda e: f"<td>{_esc(e.get('channel', ''))}</td>") if has_ch else (lambda e: "")
        rows = "".join(
            f"<tr><td>{_esc(e['line'])}</td>{ch_cell(e)}"
            f"<td class='num'>{_fmt(e.get('delay'))}</td></tr>"
            for e in wiring["lines"])
        kind = (wiring.get("meta") or {}).get("kind")
        kind_label = {"truth": "真实值保留 · delay = 初始位置 L",
                      "pending": "待测 · delay 初值 0"}.get(kind, "delay 语义未声明")
        table = (f"<h2>连线表（{len(wiring['lines'])} 条 · {_esc(kind_label)}）</h2>"
                 f"<table><tr><th>line</th>{ch_head}<th>delay ({_esc(unit)})</th></tr>{rows}</table>")

    warns = ""
    if missing or extra:
        warns = ('<div class="bad">线路集合不一致：'
                 + (f"连线缺失 {missing}；" if missing else "")
                 + (f"连线多出 {extra}" if extra else "") + "</div>")
    chip_id_mismatch = ""
    if wiring and meta.get("id") and wiring.get("meta", {}).get("chip") != meta["id"]:
        chip_id_mismatch = (f'<div class="bad">wiring.meta.chip='
                            f'{_esc(wiring["meta"].get("chip"))} ≠ chip.meta.id='
                            f'{_esc(meta["id"])}</div>')

    stats = (f'<span>构型 {_esc(meta.get("topology", "—"))}</span>'
             f'<span>比特 {len(chip["qubits"])}</span>'
             f'<span>腔 {len(chip["cavities"])}</span>'
             f'<span>读取线 {len(chip["readout_lines"])}</span>'
             f'<span>耦合器 {len(chip["couplers"])}</span>'
             f'<span>信号线 {len(line_names(chip))}</span>'
             f'<span>可调 {"是" if tunable else "否"}</span>')

    legend_items = [("比特", _Q_COLOR), ("读取腔", _C_COLOR), ("耦合器", _CP_COLOR)] \
        + [(rl["id"], _RL_COLORS[k % len(_RL_COLORS)]) for k, rl in enumerate(chip["readout_lines"])]
    legend = "".join(f'<i><b style="background:{c}"></b>{_esc(t)}</i>' for t, c in legend_items)

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>{_esc(meta.get('id', 'chip'))} · 芯片可视化</title>
<style>
 body{{font-family:"Segoe UI",system-ui,sans-serif;margin:24px auto;max-width:1220px;color:#20303f;
      background:#fafbfc}}
 h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:26px 0 8px}}
 .sub{{color:#5b6b7a;font-size:13px;margin-bottom:14px}}
 .stats span{{display:inline-block;background:#eef2f6;border-radius:10px;padding:3px 10px;
             margin:2px 4px 2px 0;font-size:12.5px}}
 .legend i{{font-style:normal;margin-right:14px;font-size:12.5px}}
 .legend b{{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:4px;
           vertical-align:-1px}}
 svg{{width:100%;height:auto;background:#fff;border:1px solid #e2e8ee;border-radius:8px}}
 g:hover *{{filter:brightness(1.12)}}
 text{{font-family:inherit}} .qtab{{fill:#fff;font-size:13px;text-anchor:middle;font-weight:600}}
 .ctab{{fill:#fff;font-size:11px;text-anchor:middle}} .clab{{fill:#3d6a42;font-size:11px;text-anchor:middle}}
 .rlab{{font-size:12px;text-anchor:end;font-weight:600}}
 table{{border-collapse:collapse;font-size:13px;width:100%}}
 th,td{{border:1px solid #dde4ea;padding:4px 10px;text-align:left}}
 th{{background:#eef2f6}} td.num{{text-align:right;font-variant-numeric:tabular-nums}}
 .bad{{color:#a1291f;background:#fdeeec;border:1px solid #f0c4be;border-radius:6px;
      padding:8px 12px;margin:10px 0;font-size:13px}}
</style></head><body>
<h1>{_esc(meta.get('name', meta.get('id', '')))} <small>({_esc(meta.get('id'))} ·
    {_esc(meta.get('revision', ''))} · {_esc(meta.get('date', ''))})</small></h1>
<div class="sub">{_esc(meta.get('description', ''))} · 维护：{_esc(meta.get('maintainer', ''))}</div>
<div class="stats">{stats}</div>
<div class="legend">{legend}</div>
{warns}{chip_id_mismatch}
{svg}
{table}
</body></html>
"""


def save_visualization(chip_path, wiring_path=None, out_path="chip-viz.html",
                       open_browser: bool = False, grid_px: float | None = None) -> Path:
    chip = json.loads(Path(chip_path).read_text(encoding="utf-8"))
    wiring = json.loads(Path(wiring_path).read_text(encoding="utf-8")) if wiring_path else None
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_chip_html(chip, wiring, grid_px), encoding="utf-8")
    if open_browser:
        webbrowser.open(out.resolve().as_uri())
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="芯片可视化（自包含 HTML）")
    ap.add_argument("--chip", required=True)
    ap.add_argument("--wiring", default=None, help="连线文件（提供则叠加通道/delay 信息）")
    ap.add_argument("--grid", type=float, default=None, metavar="PX",
                    help="每毫米像素数：覆盖自动缩放（默认上限 260），网格芯片想拉大间距时用")
    ap.add_argument("--out", default="chip-viz.html")
    ap.add_argument("--open", action="store_true", help="生成后用浏览器打开")
    args = ap.parse_args()
    out = save_visualization(args.chip, args.wiring, args.out, args.open, args.grid)
    print(f"已生成 {out}" + (f"（缩放 {args.grid:g} px/mm）" if args.grid else ""))


if __name__ == "__main__":
    main()

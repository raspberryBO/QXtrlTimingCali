# -*- coding: utf-8 -*-
"""
chip_gen.py — 芯片文件生成函数（Chip File v1.0 · JSON）

职责：以编程方式生成结构自洽的芯片配置文件（演示芯片 / 模拟用规模拓扑）。
  * generate_chip(...)     — 按【构型参数】+ 随机设计值构造芯片 dict（纯函数，不落盘）；
  * line_names(chip)       — 按命名约定推导全部信号线（校验 / 连线生成共用）；
  * save_chip(chip, path)  — 写出 JSON（UTF-8 · 缩进 2）。

构型（topology）决定比特坐标 pos 与耦合器边表，无需逐条描述连接方式：
  chain  链式（一维）        · ring  环（链 + 首尾闭合）
  ladder 梯形（两链 + 横档） · grid  方格（最近邻，度数 ≤4）
  extra_edges 追加长程边（small-world / 跨链连接）；生成结果可再手工精修。

约定（v1.0 规格）：
  * 一条读取线串多个读取腔；每腔 1:1 挂载一个比特；
  * 可调性由 meta.tunable 全局声明；
  * 线名推导：<Q>.xy / <Q>.z · <CP>.z · <RL>.ro / <RL>.ri；
  * 字段名不带单位（频率 GHz、率值 MHz、时间 ns）；
  * 本文件不含任何标定值（delay 归连线文件）。
"""
from __future__ import annotations

import argparse
import json
import math
import random
from datetime import date
from pathlib import Path

SCHEMA_VERSION = "1.0"
TOPOLOGIES = ("chain", "ring", "ladder", "grid", "chains")

# 设计值随机范围（与演示芯片量级一致）
_Q01_RANGE = (4.80, 5.10)      # GHz
_ANNH_RANGE = (-230.0, -200.0)  # MHz
_GCAV_RANGE = (75.0, 90.0)     # MHz
_CHI_RANGE = (-3.0, -2.0)      # MHz
_SWEET_RANGE = (-0.02, 0.02)   # Phi0
_FR_RANGE = (6.80, 7.10)       # GHz
_KAPPA_RANGE = (2.2, 2.8)      # MHz
_GMAX_RANGE = (40.0, 55.0)     # MHz
_GOFF_RANGE = (0.3, 0.6)       # MHz


def _rand_design(rng: random.Random, lo: float, hi: float, nd: int = 3) -> float:
    return round(rng.uniform(lo, hi), nd)


def _auto_shape(n: int) -> tuple[int, int]:
    """接近正方形的 (rows, cols)。"""
    r = int(math.isqrt(n))
    while r > 1 and n % r:
        r -= 1
    return r, n // r


def _topology_layout(n: int, topology: str, shape: tuple[int, int] | None,
                     spacing: float) -> tuple[dict[int, tuple[float, float]], list[tuple[int, int]]]:
    """由构型参数生成 (比特坐标表, 最近邻边表)。"""
    if topology == "chain":
        coords = {i: (i * spacing, 0.0) for i in range(n)}
        edges = [(i, i + 1) for i in range(n - 1)]
    elif topology == "ring":
        if n < 3:
            raise ValueError("ring 构型至少需要 3 个比特")
        radius = spacing / (2 * math.sin(math.pi / n))
        coords = {i: (radius * math.cos(2 * math.pi * i / n),
                      radius * math.sin(2 * math.pi * i / n)) for i in range(n)}
        edges = [(i, (i + 1) % n) for i in range(n)]
    elif topology == "ladder":
        if n < 4 or n % 2:
            raise ValueError("ladder 构型需要偶数个比特（至少 4）")
        cols = n // 2
        coords = {(r * cols + c): (c * spacing, r * spacing) for r in (0, 1) for c in range(cols)}
        edges = [(r * cols + c, r * cols + c + 1) for r in (0, 1) for c in range(cols - 1)]
        edges += [(c, cols + c) for c in range(cols)]
    elif topology == "grid":
        rows, cols = shape if shape else _auto_shape(n)
        if rows * cols != n:
            raise ValueError(f"shape={rows}x{cols} 与 num_qubits={n} 不一致")
        coords = {(r * cols + c): (c * spacing, r * spacing) for r in range(rows) for c in range(cols)}
        edges = [(r * cols + c, r * cols + c + 1) for r in range(rows) for c in range(cols - 1)]
        edges += [(r * cols + c, (r + 1) * cols + c) for r in range(rows - 1) for c in range(cols)]
    elif topology == "chains":
        # 多条独立链：shape = (链数, 每链比特数)，链间不耦合（长程边用 extra_edges 补）。
        # 每链一行排布，行距稍大，便于与 ladder/grid 一眼区分。
        rows, cols = shape if shape else (max(1, n // 4), 4)
        if rows * cols != n:
            raise ValueError(f"shape={rows}x{cols} 与 num_qubits={n} 不一致")
        if cols < 2:
            raise ValueError("每条链至少 2 个比特")
        coords = {(r * cols + c): (c * spacing, r * spacing * 1.4) for r in range(rows) for c in range(cols)}
        edges = [(r * cols + c, r * cols + c + 1) for r in range(rows) for c in range(cols - 1)]
    else:
        raise ValueError(f"未知构型 {topology!r}，可选：{TOPOLOGIES}")

    xs = [p[0] for p in coords.values()]
    ys = [p[1] for p in coords.values()]
    x0, y0 = min(xs), min(ys)
    coords = {i: (round(x - x0, 4), round(y - y0, 4)) for i, (x, y) in coords.items()}
    return coords, edges


def _group_readout(n: int, coords: dict[int, tuple[float, float]],
                   per_line: int | None) -> list[list[int]]:
    """读取线分组：per_line 为整数→按比特序号顺序切块；None→同一行的比特归一条线。"""
    if per_line is None:
        rows: dict[float, list[int]] = {}
        for i in range(n):
            rows.setdefault(coords[i][1], []).append(i)
        return [v for _, v in sorted(rows.items())]
    if per_line < 1:
        raise ValueError("cavities_per_line 至少为 1（或传 None 表示按行分组）")
    return [list(range(j, min(j + per_line, n))) for j in range(0, n, per_line)]


def generate_chip(
    num_qubits: int = 4,
    topology: str = "chain",
    shape: tuple[int, int] | None = None,
    extra_edges: list[tuple[int, int]] | None = None,
    tunable: bool = True,
    cavities_per_line: int | None = 2,
    spacing_mm: float = 1.6,
    seed: int | None = None,
    chip_id: str = "chip-gen",
    name: str = "程序生成演示芯片",
    maintainer: str = "QXtrlTimingCali",
    revision: str = "rev-A",
    day: str | None = None,
) -> dict:
    """生成芯片 dict（Chip File v1.0）：pos / couplers / connections 全部由构型参数推导。

    参数：
      topology        — chain | ring | ladder | grid | chains（见模块文档）；
      shape           — grid 的 (rows, cols)、chains 的 (链数, 每链比特数)；
                        给出时 num_qubits 以其为准；
      extra_edges     — 追加的长程耦合边（比特下标对），与已有边重复则忽略；
      cavities_per_line — 每条读取线的腔数；None 表示一行一条读取线。
    """
    if shape:
        num_qubits = shape[0] * shape[1]
    if num_qubits < 1:
        raise ValueError("num_qubits 至少为 1")
    rng = random.Random(seed)

    coords, edges = _topology_layout(num_qubits, topology, shape, spacing_mm)
    seen = {frozenset(e) for e in edges}
    for a, b in extra_edges or []:
        if a == b:
            raise ValueError(f"extra_edges 出现自环：{a}")
        if not (0 <= a < num_qubits and 0 <= b < num_qubits):
            raise ValueError(f"extra_edges 下标越界：({a}, {b})，有效范围 0–{num_qubits - 1}")
        if frozenset((a, b)) not in seen:
            seen.add(frozenset((a, b)))
            edges.append((min(a, b), max(a, b)))

    qubits, cavities, connections = [], [], []
    for i in range(num_qubits):
        qid, cid = f"Q{i}", f"C{i}"
        x, y = coords[i]
        qubits.append({
            "id": qid, "cavity": cid, "pos": [x, y],
            "design": {
                "f01": _rand_design(rng, *_Q01_RANGE, 3),
                "anharmonicity": _rand_design(rng, *_ANNH_RANGE, 1),
                "g_cavity": _rand_design(rng, *_GCAV_RANGE, 1),
                "chi": _rand_design(rng, *_CHI_RANGE, 1),
                "sweet_spot": _rand_design(rng, *_SWEET_RANGE, 3) if tunable else 0.0,
            },
        })
        connections.append({"type": "qubit-cavity", "qubit": qid, "cavity": cid})

    groups = _group_readout(num_qubits, coords, cavities_per_line)
    line_of = {i: f"RL{k}" for k, g in enumerate(groups) for i in g}
    for i in range(num_qubits):
        cavities.append({
            "id": f"C{i}", "qubit": f"Q{i}", "line": line_of[i],
            "design": {
                "f_r": _rand_design(rng, *_FR_RANGE, 2),
                "kappa": _rand_design(rng, *_KAPPA_RANGE, 1),
            },
        })
    for c in cavities:
        connections.append({"type": "cavity-line", "cavity": c["id"], "line": c["line"]})

    couplers = []
    for a, b in edges:
        cpid = f"CP{a:02d}{b:02d}"
        couplers.append({
            "id": cpid, "qubits": [f"Q{a}", f"Q{b}"],
            "design": {
                "g_max": _rand_design(rng, *_GMAX_RANGE, 1),
                "g_off": _rand_design(rng, *_GOFF_RANGE, 2),
            },
        })
        connections.append({"type": "qubit-qubit", "u": f"Q{a}", "v": f"Q{b}", "via": cpid})

    readout_lines = [{"id": f"RL{k}", "cavities": [f"C{i}" for i in g]}
                     for k, g in enumerate(groups)]

    return {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "id": chip_id, "name": name,
            "description": (f"{topology} 构型 · {num_qubits} 比特 / {len(couplers)} 耦合器 · "
                            f"{len(readout_lines)} 条读取线"
                            f"（每腔 1 比特）· {'全部可调' if tunable else '全部不可调'}"),
            "topology": topology,
            "tunable": tunable, "revision": revision,
            "date": day or date.today().isoformat(), "maintainer": maintainer,
        },
        "units": {"freq": "GHz", "rate": "MHz", "time": "ns",
                  "flux": "Phi0", "length": "mm"},
        "readout_lines": readout_lines,
        "cavities": cavities,
        "qubits": qubits,
        "couplers": couplers,
        "connections": connections,
    }


def line_names(chip: dict) -> list[str]:
    """全部信号线（顺序：比特 xy[/z] → 耦合器 z → 读取线 ro/ri）。"""
    tunable = bool(chip["meta"].get("tunable"))
    lines: list[str] = []
    for q in chip["qubits"]:
        lines.append(f"{q['id']}.xy")
        if tunable:
            lines.append(f"{q['id']}.z")
    for cp in chip["couplers"]:
        if tunable:
            lines.append(f"{cp['id']}.z")
    for l in chip["readout_lines"]:
        lines.append(f"{l['id']}.ro")
        lines.append(f"{l['id']}.ri")
    return lines


def save_chip(chip: dict, path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(chip, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def _parse_shape(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    rows, _, cols = text.partition("x")
    return int(rows), int(cols)


def _parse_edges(text: str | None) -> list[tuple[int, int]]:
    if not text:
        return []
    out = []
    for part in text.split(","):
        a, _, b = part.partition("-")
        out.append((int(a), int(b)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="生成芯片文件（Chip File v1.0 · 按构型参数）")
    ap.add_argument("--qubits", type=int, default=4)
    ap.add_argument("--topology", choices=TOPOLOGIES, default="chain",
                    help="chain 一维链 · ring 环 · ladder 梯形 · grid 方格 · "
                         "chains 多条独立链（shape=(链数, 每链比特数)，链间不耦合）")
    ap.add_argument("--shape", default=None, help="grid 的行数列数，如 3x4（给出时覆盖 --qubits）")
    ap.add_argument("--extra-edges", default=None,
                    help="长程耦合边，逗号分隔的 a-b 对，如 0-7,2-5")
    ap.add_argument("--no-tunable", action="store_true", help="生成不可调比特芯片")
    ap.add_argument("--per-line", type=int, default=2,
                    help="每条读取线的腔数（0 = 按行分组，一行一条线）")
    ap.add_argument("--spacing", type=float, default=1.6, help="比特间距（mm）")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--id", dest="chip_id", default="chip-gen")
    ap.add_argument("--out", default="chip-gen.json")
    args = ap.parse_args()

    chip = generate_chip(num_qubits=args.qubits, topology=args.topology,
                         shape=_parse_shape(args.shape),
                         extra_edges=_parse_edges(args.extra_edges),
                         tunable=not args.no_tunable,
                         cavities_per_line=args.per_line or None,
                         spacing_mm=args.spacing, seed=args.seed, chip_id=args.chip_id)
    from .validate import validate_chip
    p = save_chip(chip, args.out)
    errs, warns = validate_chip(p)
    print(f"已生成 {p}（{args.topology} · {len(chip['qubits'])} 比特 / "
          f"{len(chip['couplers'])} 耦合器） 校验: "
          f"{'PASS' if not errs else 'FAIL errors=' + str(len(errs))} warnings={len(warns)}")


if __name__ == "__main__":
    main()

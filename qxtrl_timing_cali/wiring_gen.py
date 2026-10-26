# -*- coding: utf-8 -*-
"""
wiring_gen.py — 连线延时文件生成函数（JSON · 同结构两种语义）

职责：由芯片文件推导线路集合，生成极简「线路 + delay」文件：
  * generate_wiring(chip, kind=...)  — 构造连线 dict（纯函数，不落盘）；
  * save_wiring(wiring, path)        — 写出 JSON。

设计取舍：只含 line + delay 两项。模拟没有真实连线，通道映射不影响时间校准；
  真实实验时通道信息由测控系统配置（QXtrl cfg.json）承载，不读本文件。

同一函数产出两种语义的文件（由 meta.kind 声明，结构完全一致）：
  * kind="truth"    真实值保留文件 —— delay = 该线初始时刻位置 L（随机生成，
                    模拟设备的隐藏真值，仅供保留与事后核对，校准流程不读不改）；
  * kind="pending"  待测文件       —— delay 全为 0.0，交给校准流水线逐线填入
                    累计右移 d（只增不减）。
两者相加即时序位置 q = L + d。
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import date
from pathlib import Path

from .chip_gen import line_names
from .review.ledger import JOURNAL_NAME

SCHEMA_VERSION = "1.0"
KINDS = ("truth", "pending")

# 初始位置 L 的随机范围（ns）：同批线缆的量级
_L_RANGE = (40.0, 200.0)


def generate_wiring(
    chip: dict,
    kind: str = "pending",
    seed: int | None = None,
    delay_lo: float = _L_RANGE[0],
    delay_hi: float = _L_RANGE[1],
    wiring_id: str | None = None,
    maintainer: str = "QXtrlTimingCali",
    day: str | None = None,
) -> dict:
    """按芯片文件生成连线延时文件（线路集合与芯片严格一致）。

    kind="truth"   → delay 为随机初始位置 L（真实值保留）；
    kind="pending" → delay 全为 0.0（待测，由校准流水线填入）。
    """
    if kind not in KINDS:
        raise ValueError(f"kind 仅支持 {'|'.join(KINDS)}（当前 {kind!r}）")
    if kind == "truth" and delay_lo > delay_hi:
        raise ValueError(f"delay_lo={delay_lo} 不得大于 delay_hi={delay_hi}")
    rng = random.Random(seed)
    names = line_names(chip)

    if kind == "truth":
        entries = [{"line": n, "delay": round(rng.uniform(delay_lo, delay_hi), 1)}
                   for n in names]
    else:
        entries = [{"line": n, "delay": 0.0} for n in names]

    desc = ("真实值保留文件：delay = 各线初始时刻位置 L（模拟隐藏真值，校准流程不读不改）"
            if kind == "truth" else
            "待测文件：delay 初值 0.0，由校准流水线填入累计右移 d（只增不减）")
    return {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "id": wiring_id or f"wiring-{chip['meta']['id']}-{kind}",
            "chip": chip["meta"]["id"],
            "kind": kind,
            "description": desc,
            "date": day or date.today().isoformat(), "maintainer": maintainer,
        },
        "units": {"time": "ns"},
        "lines": entries,
    }


def save_wiring(wiring: dict, path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(wiring, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if (wiring.get("meta") or {}).get("kind") == "pending":
        # 重新生成待测文件 = 开启新一轮校准：作废伴生测量日志（断点续跑的状态），
        # 避免新轮把上一轮的观测错当成本轮已测边（两者内容指纹相同，hash 区分不了）。
        journal = p.with_name(p.stem + "." + JOURNAL_NAME)
        if journal.exists():
            journal.unlink()
    return p


def main() -> None:
    ap = argparse.ArgumentParser(description="由芯片文件生成连线延时文件（line + delay）")
    ap.add_argument("--chip", required=True, help="芯片配置文件路径")
    ap.add_argument("--kind", choices=KINDS, default="pending",
                    help="truth=真实值保留（随机初始位置 L）；pending=待测（初值 0）")
    ap.add_argument("--seed", type=int, default=None, help="truth 模式的随机种子")
    ap.add_argument("--lo", type=float, default=_L_RANGE[0], help="truth 下界（ns）")
    ap.add_argument("--hi", type=float, default=_L_RANGE[1], help="truth 上界（ns）")
    ap.add_argument("--out", default="wiring-gen.json")
    args = ap.parse_args()

    chip = json.loads(Path(args.chip).read_text(encoding="utf-8"))
    wiring = generate_wiring(chip, kind=args.kind, seed=args.seed,
                             delay_lo=args.lo, delay_hi=args.hi)
    p = save_wiring(wiring, args.out)
    from .validate import validate_wiring
    errs, warns = validate_wiring(p, chip=args.chip)
    print(f"已生成 {p}（{len(wiring['lines'])} 条线路 · {args.kind}） 校验: "
          f"{'PASS' if not errs else 'FAIL errors=' + str(len(errs))} warnings={len(warns)}")


if __name__ == "__main__":
    main()

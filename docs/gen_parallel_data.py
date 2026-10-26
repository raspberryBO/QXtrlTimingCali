# -*- coding: utf-8 -*-
"""
gen_parallel_data.py — 并行演示的数据导出（4×4 最近邻网格 · 全可调）

`python docs/gen_parallel_data.py` → docs/parallel-data.json

跑的是**真实分组流水线**（读取线 → 单比特 XY-Z → 比特间/耦合器 → 读取线收尾），
并在第 2、3 组用 --parallel 对应的提交束（默认 4，即每条提交束最多 4 条边一起提交）。
动画里出现的每个数字都能在这份 JSON 里指认：每组的波划分、每条测量落在第几波、
求解与写回事实、以及与"串行逐条测"对比省下的轮次。

芯片为 4×4 最近邻网格：上下、左右相邻比特全部耦合，共 24 个耦合器。
读取线每行一条，多音读取；测量计划按共享线名划分冲突波。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from qxtrl_timing_cali import (__version__, CalibrationSession, DirectExecutor,  # noqa: E402
                               ReviewCenter, SimChip, SimExecutor, Thresholds,
                               StagedCalibrationPipeline, generate_chip, generate_wiring,
                               save_chip, save_wiring)
from qxtrl_timing_cali.review.parallel import ParallelPlan, MeasurementWave

HERE = Path(__file__).resolve().parent
OUT_DEFAULT = HERE / "parallel-data.json"

# 演示芯片：4×4 最近邻网格（shape=(4,4)），每行一条读取线（多音读取），全可调
# day 钉死 ⇒ meta.chip.date 稳定，重跑不漂（缺省会写"今天"，数据就不可复现了）
CHIP = dict(num_qubits=16, topology="grid", shape=(4, 4), cavities_per_line=4,
            tunable=True, seed=11, chip_id="chip-4x4",
            day="2026-09-24")
TRUTH_SEED, DEVICE_SEED = 23, 5        # 固定 ⇒ 数据可复现（tests 钉住）


def build(chip: dict, truth: dict[str, float], pending_path: Path, *, th: Thresholds,
          params: dict, max_parallel: int, write: bool) -> dict:
    """跑一遍分组顺序校准，把波相关的过程事实原样拼成动画数据。"""
    device = SimChip(truth, noise_ns=params["noise_ns"], seed=DEVICE_SEED)
    center = ReviewCenter(
        CalibrationSession.from_pending(pending_path),
        DirectExecutor(SimExecutor(device, seed=DEVICE_SEED)),
        thresholds=th, pending_path=pending_path, write=write,
        params={k: params[k] for k in ("n_shots", "repeats", "noise_ns")})
    pipe = StagedCalibrationPipeline(chip, center, readout_headroom_ns=params.get(
        "readout_headroom_ns", 0.0), max_parallel=max_parallel)
    report = pipe.run(truth)

    stages = []
    for s in report["stages"]:
        waves = s.get("waves") or {}
        stages.append({
            "name": s["name"], "title": s["title"],
            "writeback": s["writeback"], "depends_on": list(s["depends_on"]),
            "why": s["why"], "nodes": list(s["nodes"]),
            "waves": waves,
            "pairs": s["pairs"],
            "measurements": s["measurements"],
            "solve": s["solve"],
            "written": bool(s["written"]),
            "shifts": {k: round(v, 3) for k, v in (s["solve"]["shifts"] if s["solve"] else {}).items()},
            "ledger": s["ledger"],
        })

    # 并行账：每组"串行要几次" vs "分几波"，以及一波最多几条
    parallel = []
    for s in stages:
        waves = s.get("waves") or {}
        plan = ParallelPlan(
            waves=[MeasurementWave(index=w["index"], pairs=w["pairs"])
                   for w in waves.get("waves", [])],
            bundle_size=waves.get("bundle_size", 0))
        parallel.append({
            "stage": s["name"],
            "sequential": len([m for m in s["measurements"] if m["ok"]]) or len(s["pairs"]),
            "wave_count": waves.get("wave_count", 0),
            "wave_sizes": [len(w["pairs"]) for w in waves.get("waves", [])],
            "bundle_size": waves.get("bundle_size", 0),
            "bundle_count": len(plan.bundles()),
        })
    rounds = sum(p["sequential"] for p in parallel)
    waves_total = sum(p["wave_count"] for p in parallel)
    bundles_total = sum(p["bundle_count"] for p in parallel)
    # 从实际三次写回的求解结果还原逐线位置；收回余量是唯一负向位移。
    first = next(s for s in stages if s["name"] == "readout")
    final = next(s for s in stages if s["name"] == "readout_final")
    d1 = (first["solve"] or {}).get("shifts", {}) if first["written"] else {}
    d2 = (report.get("solve") or {}).get("shifts", {}) if report["written"] else {}
    final_solve = final["solve"] or {}
    shifts = final_solve.get("shifts", {}) if final["written"] else {}
    returns = final_solve.get("headroom_return", {}) if final["written"] else {}
    calibration_lines = []
    for name, length in truth.items():
        a, b = d1.get(name, 0.0), d2.get(name, 0.0)
        c = shifts.get(name, 0.0) - returns.get(name, 0.0)
        calibration_lines.append({"name": name, "L": length,
                                  "d1": a, "d2": b, "d3": c,
                                  "q1": length + a + b + c})
    return {
        "meta": {"version": __version__, "chip": chip["meta"], "lines": report["lines"],
                 "noise_ns": params["noise_ns"], "n_shots": params["n_shots"],
                 "repeats": params["repeats"], "max_parallel": max_parallel,
                 "tol_ns": th.tol_ns, "d_max_ns": th.d_max_ns,
                 "readout_headroom_ns": params.get("readout_headroom_ns", 0.0),
                 "rule": "两条边共享线名才冲突（同一读取线的多比特可多音读取，不算冲突）"},
        "chip": chip, "stages": stages, "parallel": parallel,
        "calibration_lines": calibration_lines,
        "summary": {"rounds_sequential": rounds, "rounds_parallel": waves_total,
                    "bundles_total": bundles_total,
                    "wave_reduction_ratio": round(rounds / waves_total, 2) if waves_total else None,
                    "ideal_bundle_speedup": round(rounds / bundles_total, 2) if bundles_total else None,
                    "anchor": report["anchor"], "plan_ns": report["plan_ns"],
                    "residual_ns": (report["residual"] or {}).get("residual_ns"),
                    "aligned": report["aligned"], "aborted": report["aborted"]},
        "report": {"lines": report["lines"], "ledger": report["ledger"],
                   "failed": report["failed"], "isolated": report["isolated"],
                   "converged": report["converged"], "written": report["written"]},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="并行演示数据：真跑分组流水线并导出 JSON")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--parallel", type=int, default=4, help="每条提交束最多几条边（默认 4）")
    ap.add_argument("--headroom", type=float, default=100.0, help="读取线临时余量 ns")
    # 64 条线比 16 条线的演示更吃 SNR：单次噪声降到 1 ns / 400 shots，残差才稳在 tol 内
    ap.add_argument("--noise", type=float, default=1.0)
    ap.add_argument("--shots", type=int, default=400)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--tol", type=float, default=0.5)
    ap.add_argument("--d-max", type=float, default=6000.0)
    ap.add_argument("--dry-run", action="store_true", help="只求解不写回")
    args = ap.parse_args()

    chip = generate_chip(**CHIP)
    truth = {e["line"]: float(e["delay"]) for e in generate_wiring(
        chip, kind="truth", seed=TRUTH_SEED, delay_lo=40.0, delay_hi=200.0)["lines"]}
    work = Path(tempfile.mkdtemp(prefix="qxtrl-par-"))
    pending = work / "pending.json"
    save_chip(chip, work / "chip.json")
    save_wiring(generate_wiring(chip, kind="truth", seed=TRUTH_SEED,
                                delay_lo=40.0, delay_hi=200.0), work / "truth.json")
    save_wiring(generate_wiring(chip, kind="pending"), pending)

    data = build(chip, truth, pending,
                 th=Thresholds(tol_ns=args.tol, d_max_ns=args.d_max),
                 params={"n_shots": args.shots, "repeats": args.repeats,
                         "noise_ns": args.noise, "readout_headroom_ns": args.headroom},
                 max_parallel=args.parallel, write=not args.dry_run)
    out = Path(args.out)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    s = data["summary"]
    print(f"线 {data['meta']['lines']} 条 · 串行 {s['rounds_sequential']} 次 → "
          f"{s['rounds_parallel']} 冲突波 / {s['bundles_total']} 提交束 · "
          f"等时完全并发的理论比值 {s['ideal_bundle_speedup']}×（非实测）· "
          f"残差 {s['residual_ns']} ns · "
          f"aligned={s['aligned']}")
    for p in data["parallel"]:
        print(f"  {p['stage']:14s} {p['sequential']:2d} 次 → {p['wave_count']} 波 "
              f"（{'+'.join(str(x) for x in p['wave_sizes']) or '0'}）")
    print(f"→ {out}")


if __name__ == "__main__":
    main()

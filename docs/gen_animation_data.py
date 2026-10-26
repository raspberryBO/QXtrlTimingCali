# -*- coding: utf-8 -*-
"""动画数据导出：`python docs/gen_animation_data.py` → docs/animation-data.json

跑的是**真实分组流水线**（读取线组 → 单比特 XY-Z 组 → 比特间/耦合器组 → 读取线收尾组），把四组实验的
中间事实原样倒出来给 docs/gen_animation.py 重放。动画里出现的每个数字都能在这份 JSON
里指认，不做任何手写装饰。

分组的位移事实在 `lines` 里逐线留痕：`d1` = 组 1（读取线）**即时写回**的那段
（含 headroom），`d2` = 末尾统一求解后**整批写回**的那段，`d3` = readout_final
收尾的那段（读取线收回 headroom 往左拉回，**可为负**），`q1 = L + d1 + d2 + d3`。

产物字段（JS 侧唯一输入）：
  meta / chip / lines / stages / candidates / tree / measurements / solve / report

用法：
  python docs/gen_animation_data.py                     # 默认 4 比特 grid · L 40–200 ns
  python docs/gen_animation_data.py --qubits 8 --shape 2 4 --delay-hi 1000   # 更大环境
  python docs/gen_animation_data.py --out /tmp/probe.json --no-write-back    # 试运行，不动仓库

默认参数（chip-seed 7 · truth-seed 9 · seed 8 · 每线 2 腔 · 芯片日期戳默认今天）
就是入库那份 `docs/animation-data.json` 的出处：除边上多带的 `kind` 与新增的 `meta.seed`
之外可逐字段复现（tests/test_docs_animation.py 把这条钉住）。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from qxtrl_timing_cali import (__version__, generate_chip, generate_wiring, save_chip,
                               save_wiring, CalibrationSession, DirectExecutor, ReviewCenter,
                               SimChip, SimExecutor, Thresholds, StagedCalibrationPipeline)

HERE = Path(__file__).resolve().parent
OUT_DEFAULT = HERE / "animation-data.json"


def line_group(name: str) -> str:
    """线名 → 分组（画时间轴时按组着色）。"""
    head = name.split(".")[0]
    if head.startswith("CP"):
        return "coupler"
    if head.startswith("RL"):
        return "readout"
    return "qubit"


def build(chip: dict, truth: dict[str, float], pending_path: Path, *, th: Thresholds,
          params: dict, write: bool) -> dict:
    """跑一遍分组顺序校准，把过程事实拼成动画数据（顺序 = 画布上的重放顺序）。

    组 1（读取线 ro/ri）测完**立刻写回**（含 headroom），组 2 / 组 3 只记录；
    末尾把两组观测合在一起做一次全局最小二乘、整批一次写回，最后 readout_final
    收回 headroom 把读取线往左拉回对齐。每条线的位移三段：`d1` + `d2` + `d3`。
    """
    device = SimChip(truth, noise_ns=params["noise_ns"], seed=params["seed"])
    center = ReviewCenter(
        CalibrationSession.from_pending(pending_path),
        DirectExecutor(SimExecutor(device, seed=params["seed"])),
        thresholds=th, pending_path=pending_path, write=write,
        params={k: params[k] for k in ("n_shots", "repeats", "noise_ns")})
    pipe = StagedCalibrationPipeline(chip, center, readout_headroom_ns=params.get("headroom_ns", 0.0))
    report = pipe.run(truth)
    # 按 stage 顺序还原 outcomes（report 里 stages 顺序已经是对的）
    stages = []
    for s in report["stages"]:
        stages.append({
            "name": s["name"], "title": s["title"],
            "writeback": s["writeback"], "depends_on": list(s["depends_on"]),
            "why": s["why"], "nodes": list(s["nodes"]),
            "pairs": s["pairs"],
            "measurements": s["measurements"],
            "solve": s["solve"],
            "written": bool(s["written"]),
            "shifts": {k: round(v, 3) for k, v in (s["solve"]["shifts"] if s["solve"] else {}).items()},
            "headroom_return": {k: round(v, 3)
                                for k, v in ((s["solve"] or {}).get("headroom_return")
                                             if s["solve"] else {}).items()},
            "ledger": s["ledger"],
        })

    # 动画需要知道 headroom 数值，把它挂在 readout stage 上
    readout_stage = next((s for s in stages if s["name"] == "readout"), None)
    if readout_stage:
        readout_stage["headroom_ns"] = round(pipe.readout_headroom_ns, 3)

    solve = report["solve"]

    # 累计位移：d1=readout(含headroom), d2=qubit/coupler全局解,
    # d3=readout_final（净位移 = shifts − headroom_return：读取线收回 headroom
    # 往左拉回对齐，所以 d3 可为负）
    d1 = next((s["shifts"] for s in stages if s["name"] == "readout"), {})
    # qubit/coupler 的解是 deferred 写回：coupler stage 自身的 solve/shifts 恒为空，
    # 真正的解在末尾统一求解的 report["solve"] 里——从 stage 上取只会拿到空字典，
    # 动画里 12 条 qubit/coupler 线就全程不动了。
    d2 = (report.get("solve") or {}).get("shifts") or {}
    fin_stage = next((s for s in stages if s["name"] == "readout_final"), {})
    fin_shifts = fin_stage.get("shifts") or {}
    fin_ret = fin_stage.get("headroom_return") or {}
    d3 = {ln: round(fin_shifts.get(ln, 0.0) - fin_ret.get(ln, 0.0), 3) for ln in fin_shifts}
    d3.update({ln: -round(v, 3) for ln, v in fin_ret.items() if ln not in d3})
    lines = []
    for ln in truth:
        a = round(d1.get(ln, 0.0), 3)
        b = round(d2.get(ln, 0.0), 3)
        c = round(d3.get(ln, 0.0), 3)
        lines.append({"name": ln, "group": line_group(ln), "L": truth[ln],
                      "d1": a, "d2": b, "d3": c,
                      "d": round(a + b + c, 3), "q0": truth[ln],
                      "q1": round(truth[ln] + a + b + c, 3)})

    return {
        "meta": {"version": __version__, "chip": chip["meta"], "lines": report["lines"],
                 "noise_ns": params["noise_ns"], "n_shots": params["n_shots"],
                 "repeats": params["repeats"], "tol_ns": th.tol_ns,
                 "d_max_ns": th.d_max_ns, "seed": params["seed"],
                 "headroom_ns": round(float(params.get("headroom_ns", 0.0)), 3),
                 "stage_names": [s["name"] for s in stages]},
        "chip": {"qubits": [{"id": q["id"], "pos": q["pos"]} for q in chip["qubits"]],
                 "couplers": [{"id": c["id"], "qubits": c["qubits"]} for c in chip["couplers"]],
                 "cavities": [{"id": c["id"], "qubit": c["qubit"], "line": c["line"]}
                              for c in chip["cavities"]],
                 "readout_lines": [{"id": r["id"], "cavities": r["cavities"]}
                                   for r in chip["readout_lines"]]},
        # 动画只显示到 0.001 ns，多出来的位数没有意义（真值/残差另有 report 兜底）
        "lines": lines,
        "stages": stages,
        "candidates": [{**p.to_dict(), "group": p.group} for p in pipe.candidates()],
        "tree": [{**p, "group": s["name"]} for s in stages for p in s["pairs"]],
        "measurements": [{**m, "stage": s["name"]} for s in stages for m in s["measurements"]],
        "solve": solve,
        "report": report,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="跑真实流水线，导出动画重放数据")
    ap.add_argument("--out", default=str(OUT_DEFAULT), help="输出 JSON（默认 docs/animation-data.json）")
    ap.add_argument("--qubits", type=int, default=4)
    ap.add_argument("--shape", type=int, nargs=2, default=(2, 2), metavar=("ROW", "COL"))
    ap.add_argument("--cavities-per-line", type=int, default=2)
    ap.add_argument("--delay-lo", type=float, default=40.0)
    ap.add_argument("--delay-hi", type=float, default=200.0)
    ap.add_argument("--truth-seed", type=int, default=9)
    ap.add_argument("--chip-seed", type=int, default=7)
    ap.add_argument("--chip-id", default="chip-gen")
    ap.add_argument("--chip-day", default=None, help="芯片 meta 日期戳，默认今天（重跑会让该字段变动）")
    ap.add_argument("--seed", type=int, default=8, help="模拟设备 / 测量噪声种子")
    ap.add_argument("--noise", type=float, default=2.0)
    ap.add_argument("--shots", type=int, default=200)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--tol", type=float, default=0.5)
    ap.add_argument("--d-max", type=float, default=5000.0)
    ap.add_argument("--headroom", type=float, default=0.0,
                    help="读取线 ro/ri 对齐后整体右移的 headroom（ns，默认 0；"
                         "典型可设 1000 ns，给 qubit/coupler 留出右移空间）")
    ap.add_argument("--no-write-back", action="store_true",
                    help="只求解不落盘（写回守卫与指纹仍会走一遍内存路径）")
    args = ap.parse_args()

    chip = generate_chip(num_qubits=args.qubits, topology="grid", shape=tuple(args.shape),
                         cavities_per_line=args.cavities_per_line, tunable=True,
                         seed=args.chip_seed, chip_id=args.chip_id, day=args.chip_day)
    truth_file = generate_wiring(chip, kind="truth", seed=args.truth_seed,
                                 delay_lo=args.delay_lo, delay_hi=args.delay_hi)
    truth = {e["line"]: float(e["delay"]) for e in truth_file["lines"]}

    out = Path(args.out)
    work = Path(tempfile.mkdtemp(prefix="qxtrl-anim-"))
    save_chip(chip, work / "chip.json")
    pending = work / "pending.json"
    save_wiring(generate_wiring(chip, kind="pending"), pending)

    th = Thresholds(tol_ns=args.tol, d_max_ns=args.d_max,
                    skew_max_ns=max(1500.0, (args.delay_hi - args.delay_lo) + 600.0))
    data = build(chip, truth, pending, th=th,
                 params={"n_shots": args.shots, "repeats": args.repeats,
                         "noise_ns": args.noise, "seed": args.seed,
                         "headroom_ns": args.headroom},
                 write=not args.no_write_back)

    rep = data["report"]
    res = rep.get("residual") or {}
    per = " / ".join(f"{s['name']}={s['ledger']['records']}" for s in data["stages"])
    print(f"线 {data['meta']['lines']} 条 · 四组测量 {len(data['measurements'])} 次（{per}）"
          f" · 目标锚线 {rep['anchor']}")
    print(f"初始失配 {max(truth.values()) - min(truth.values()):.1f} ns → "
          f"残差 {res.get('residual_ns', float('nan')):.3f} ns · "
          f"末尾总位移 {rep['plan_ns']:.1f} ns · aligned={rep['aligned']} · aborted={rep['aborted']}")
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"→ {out}")


if __name__ == "__main__":
    main()

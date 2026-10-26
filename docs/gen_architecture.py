# -*- coding: utf-8 -*-
"""架构图生成器：`python docs/gen_architecture.py` → docs/architecture-v0.3.3.html

**版本号独立于包版本**：这里的 v0.3.x 只随"图与说明内容"修订而递增，
不跟着 `qxtrl_timing_cali.__version__` 走（历史：v0.2 → v0.3 → v0.3.1 → v0.3.2 → v0.3.3）。

图与说明同源：改坐标即改图，脚本自带几何自检（重叠 / 出界 / 箭头穿节点 / 文字溢出）。
"""
from __future__ import annotations

from math import hypot
from pathlib import Path

W = 1320
RIGHT = 1288          # 内容右边界（各 frame 与节点统一对齐）
T_SIZE, S_SIZE = 12.5, 10.5

C = {
    "gen": ("#eaf2fb", "#2c6cb0"),    # 生成端
    "file": ("#fdf5df", "#a9760a"),   # 配置真源
    "cali": ("#e9f6ec", "#1d7a3c"),   # 校准审查端
    "exec": ("#f2eafb", "#6b3fa0"),   # 执行端
    "ext": ("#fdecea", "#b3261e"),    # 外部软件（未来）
    "art": ("#eef1f4", "#4a5560"),    # 产物
    "plan": ("#f6f7f9", "#7a8590"),   # 规划中
}

nodes, frames, edges = [], [], []


def box(kind, x, y, w, h, title, *lines, dash=False):
    nodes.append(dict(kind=kind, x=x, y=y, w=w, h=h, title=title, lines=list(lines), dash=dash))


def frame(kind, x, y, w, h, label="", dash=False):
    frames.append(dict(kind=kind, x=x, y=y, w=w, h=h, label=label, dash=dash))


def edge(pts, *, kind="cali", dash=False, label=None, lx=None, ly=None, anchor="middle"):
    """保留节点边界连接点，线端退开，避免节点填充吃掉箭头尖。"""
    pts = list(pts)
    ports = (pts[0], pts[-1])
    def inset(a, b, distance):
        length = hypot(b[0] - a[0], b[1] - a[1])
        if length <= distance:
            raise ValueError("连线端段过短，无法为箭头留空")
        return (a[0] + (b[0] - a[0]) * distance / length,
                a[1] + (b[1] - a[1]) * distance / length)
    pts[0] = inset(pts[0], pts[1], 2)
    pts[-1] = inset(pts[-1], pts[-2], 4)
    edges.append(dict(pts=pts, ports=ports, kind=kind, dash=dash, label=label,
                      lx=lx, ly=ly, anchor=anchor))


# ---------------- Band1 生成端（纯函数） ----------------
B1_Y, BH = 74, 74
frame("gen", 32, B1_Y - 24, RIGHT - 32, BH + 40, "")
slots = [40, 360, 680, 1000]
box("gen", slots[0], B1_Y, 280, BH, "chip_gen  芯片文件生成",
    "chain|ring|ladder|grid|chains", "→ pos / couplers / connections")
box("gen", slots[1], B1_Y, 280, BH, "wiring_gen  延时文件生成",
    "kind=truth → 隐藏真值 L（随机）", "kind=pending → 待测 d（全 0）")
box("gen", slots[2], B1_Y, 280, BH, "validate  校验器",
    "结构·唯一·引用 1:1·数值·连通", "connections 双向核对 + 三文件交叉")
box("gen", slots[3], B1_Y, 280, BH, "visualize  可视化排错",
    "自包含 HTML/SVG · 按 pos 布局", "--grid 覆盖自动缩放")

# ---------------- Band2 配置真源（JSON） ----------------
B2_Y = B1_Y + BH + 44
box("file", slots[0], B2_Y, 280, BH, "chip.json  芯片 v1.0",
    "readout_lines / cavities / qubits", "couplers / connections · 无标定值")
box("file", slots[1], B2_Y, 280, BH, "wiring-pending  待测",
    "meta.kind=pending · line+delay", "校准只右移；headroom 可收回")
box("file", slots[2], B2_Y, 280, BH, "wiring-truth  真值",
    "meta.kind=truth · delay = L", "不读不改（仅装载设备·事后对账）")
box("file", slots[3], B2_Y, 280, BH, "runs/ 文件队列",
    "requests ⇄ results（跨软件）", "consumed/ 归档 · rejected/ 隔离")

edge([(180, B1_Y + BH), (180, B2_Y)], label="生成", lx=194, ly=176, anchor="start")
edge([(500, B1_Y + BH), (500, B2_Y)], label="生成", lx=514, ly=176, anchor="start")
edge([(600, B1_Y + BH), (600, B1_Y + BH + 16), (760, B1_Y + BH + 16), (760, B2_Y)])
edge([(820, B1_Y + BH), (820, B2_Y)], kind="file", dash=True,
     label="校验", lx=834, ly=B1_Y + BH + 30, anchor="start")

# ---------------- Band3 校准审查端 ⇄ 执行端 ----------------
B3_Y = B2_Y + BH + 60
FH, NH = 462, 78
L_X, L_W = 32, 576                   # ③ frame 32..608
R_X, R_W = 712, RIGHT - 712          # ④ frame 712..1288
frame("cali", L_X, B3_Y - 26, L_W, FH + 34,
      "③ 校准审查端  review/ + pipeline.py / stages.py（本仓库 · 有状态）")
frame("exec", R_X, B3_Y - 26, R_W, FH + 34,
      "④ 执行端（可整体替换 · 实验由其他软件执行）")

R1Y, R2Y, R3Y, R4Y = B3_Y + 8, B3_Y + 116, B3_Y + 224, B3_Y + 332
LC1, LC2, CW1 = 48, 322, 250         # 左列 48..298，右列 322..572
RC1, RC2 = 736, 1028                 # 736..986 / 1028..1278
edge([(1140, B2_Y + BH), (1140, B2_Y + BH + 12),
      (700, B2_Y + BH + 12), (700, R1Y + 52), (RC1, R1Y + 52)],
     kind="exec", dash=True, label="FileQueue 读写", lx=870, ly=B2_Y + BH + 29)
box("cali", LC1, R1Y, CW1, NH, "stages.py  六步分阶段编排",
    "步1-2 读取线对齐 + headroom → 立刻写回",
    "步3-4 单比特 xy↔z、跨比特 z-z 只记录",
    "步5 统一求解写回 → 步6 读取线挂 qubit.xy")
box("cali", LC2, R1Y, CW1, NH, "center.py  ReviewCenter",
    "① 申请 ② 取结果 ③ 审查 ④ 回写", "commit_solve 数值验收 → 守卫落盘")
box("cali", LC1, R2Y, CW1, NH, "session.py  会话",
    "逐线 d + hash + headroom 授予账簿", "plan 纯函数（先查后写）· 批量无同步块")
box("cali", LC2, R2Y, CW1, NH, "policies.py  策略链",
    "analyze 只出事实；阈值在 Thresholds", "链序 stale→trace→quality", "→budget→align→commit")
box("cali", LC1, R3Y, CW1, NH, "writeback.py  守卫回写",
    "CAS + 只右移 + d_max 预算", "原子替换 · .bak · JSONL 审计")
box("cali", LC2, R3Y, CW1, NH, "contracts.py  五种消息",
    "Request / Result / Assessment", "Verdict / WritePlan · JSON 往返")
box("cali", LC1, R4Y, LC2 + CW1 - LC1, NH + 15, "共用内核：graph · ledger · solver · parallel",
    "graph 选树；ledger 只记测量日志 + reconnect 替补重连",
    "solver.solve_main_component 只解最大分量，其余隔离",
    "parallel 按共享线名分波；ledger 按提交束调用 submit_many",
    "编排器决定写回时机；未收敛禁止应用，headroom 按余额收回")

box("exec", RC1, R1Y, CW1, NH, "queue_io.py  适配器",
    "ExecutorAdapter · DirectExecutor", "FileQueue（requests ⇄ results）")
box("exec", RC2, R1Y, CW1, NH, "sim.py  SimExecutor",
    "n_shots / repeats / noise 采样", "→ skew_ns / sigma_ns / quality")
box("exec", RC1, R2Y, CW1, NH, "sim.py  SimChip  设备替身",
    "q = L + d · headroom 可收回", "按当前 pending 的 d 还原设备")
box("ext", RC2, R2Y, CW1, NH, "QXtrl  未来执行端",
    "QxtrlClient.submit → RecipeResult", "自带 config_hash / cells / quality",
    "hardware_hold 独占锁", dash=True)
box("exec", RC1, R3Y, RC2 + CW1 - RC1, NH, "边界约定（接入 QXtrl 时必须显式落实）",
    "线名 Q0.xy / CP0001.z / RL0.ro ⇄ 板卡通道 XY_CH_ / Z_CH_ / QA_CH_(Probe·Acquire)",
    "映射归 QXtrl 的 Connection，不进 pending 文件；延时的唯一写入者是审查中心")

# ③ 内部
edge([(LC1 + CW1, R1Y + 26), (LC2, R1Y + 26)])
edge([(LC2 + CW1 / 2, R1Y + NH), (LC2 + CW1 / 2, R2Y)],
     label="③ 分析", lx=LC2 + CW1 / 2 + 18, ly=R2Y - 8, anchor="start")
edge([(LC2, R2Y + 26), (LC1 + CW1, R2Y + 26)],
     label="② 建议计划", lx=310, ly=R2Y - 8)
edge([(LC1 + CW1 / 2, R2Y + NH), (LC1 + CW1 / 2, R3Y)], label="④ 执行计划",
     lx=LC1 + CW1 / 2 + 18, ly=R3Y - 8, anchor="start")
edge([(LC2 + CW1 / 2, R2Y + NH), (LC2 + CW1 / 2, R3Y)])

# 跨边界：唯一接口是契约
edge([(LC2 + CW1, R1Y + 12), (RC1, R1Y + 12)], kind="exec",
     label="① Request", lx=(572 + 736) / 2, ly=R1Y + 4)
edge([(RC1, R1Y + 42), (LC2 + CW1, R1Y + 42)], kind="exec",
     label="② Result", lx=(572 + 736) / 2, ly=R1Y + 34)
edge([(RC1 + CW1, R1Y + 26), (RC2, R1Y + 26)])
edge([(RC2 + 40, R1Y + NH), (RC2 + 40, R1Y + NH + 9),
      (1007, R1Y + NH + 9), (1007, R2Y + 20), (RC1 + CW1, R2Y + 20)],
     label="批准计划下发设备", lx=1034, ly=R2Y - 7, anchor="start")
edge([(1278, R1Y + 60), (1304, R1Y + 60), (1304, R2Y + 40), (1278, R2Y + 40)],
     kind="ext", dash=True)

# ---------------- Band4 产物与审计 ----------------
B4_Y = B3_Y + FH + 68
edge([(LC1 + CW1, R3Y + NH / 2), (310, R3Y + NH / 2),
      (310, R4Y - 14), (590, R4Y - 14), (590, B4_Y - 18),
      (180, B4_Y - 18), (180, B4_Y)],
     label="守卫通过或拒绝 → 审计留痕",
     lx=60, ly=B3_Y + FH + 26, anchor="start")
box("art", slots[0], B4_Y, 280, BH, "writeback-audit.jsonl",
    "old/new hash · applied 明细", "拒绝也留痕（CAS/单调/预算）")
box("art", slots[1], B4_Y, 280, BH, "review-history.json",
    "裁决链：request → verdict → written", "attempts 计数支撑 retry/escalate")
box("art", slots[2], B4_Y, 280, BH, "session.json",
    "逐线 d + 指纹快照", "会话与编排器共读")
box("art", slots[3], B4_Y, 280, BH, "pipeline-report.json",
    "tree / measurements / isolated", "solve 诊断 · 残差 ⇔ truth 对账")

# ---------------- Band5 规划 ----------------
B5_Y = B4_Y + BH + 48
box("plan", 40, B5_Y, 400, BH, "后期能力 ⑤（见 README 路线图）",
    "阈值经验值标定 · 按 σ 自适应选边加权", "坏边隔离区续跑 · 多轮收敛 · QXtrl 接入")
box("plan", 460, B5_Y, 400, BH, "借鉴 automation 的调度思路",
    "path→stages 依赖失效：哪些线变了使哪些块作废", "attempt_id=submission_id 超时补查（幂等）")
box("plan", 880, B5_Y, 400, BH, "借鉴 QXtrl 的执行侧思路",
    "Outcome 四态：PASS/FAIL/INCONCLUSIVE/INVALID", "QualityResult + r2 优度 → quality / flags")

H = B5_Y + BH + 24


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def polyline(pts):
    return " ".join(f"{x:.0f},{y:.0f}" for x, y in pts)


svg = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img">']
svg.append('<defs>' + "".join(
    f'<marker id="a-{k}" viewBox="0 0 10 10" refX="10" refY="5" '
    f'markerUnits="userSpaceOnUse" markerWidth="12" markerHeight="12" '
    f'orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{v[1]}"/></marker>'
    for k, v in C.items()) + "</defs>")

for f in frames:
    fill, stroke = C[f["kind"]]
    dash = ' stroke-dasharray="7 5"' if f["dash"] else ""
    svg.append(f'<rect x="{f["x"]}" y="{f["y"]}" width="{f["w"]}" height="{f["h"]}" rx="10" '
               f'fill="{fill}22" stroke="{stroke}" stroke-width="1.4"{dash}/>')
    if f["label"]:
        svg.append(f'<text x="{f["x"] + 14}" y="{f["y"] + 18}" class="cap" '
                   f'fill="{stroke}">{esc(f["label"])}</text>')

for e in edges:
    _, stroke = C[e["kind"]]
    dash = ' stroke-dasharray="6 4"' if e["dash"] else ""
    svg.append(f'<polyline points="{polyline(e["pts"])}" fill="none" stroke="{stroke}" '
               f'stroke-width="1.6"{dash} marker-end="url(#a-{e["kind"]})" opacity="0.9"/>')

for n in nodes:
    fill, stroke = C[n["kind"]]
    dash = ' stroke-dasharray="6 4"' if n["dash"] else ""
    svg.append(f'<rect x="{n["x"]}" y="{n["y"]}" width="{n["w"]}" height="{n["h"]}" rx="7" '
               f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"{dash}/>')
    svg.append(f'<text x="{n["x"] + 12}" y="{n["y"] + 21}" class="t" '
               f'fill="{stroke}">{esc(n["title"])}</text>')
    for i, line in enumerate(n["lines"]):
        svg.append(f'<text x="{n["x"] + 12}" y="{n["y"] + 38 + i * 15}" class="s">'
                   f'{esc(line)}</text>')

for e in edges:                       # 标签最后画 + 白色描边，压在节点之上仍可读
    if not e["label"]:
        continue
    _, stroke = C[e["kind"]]
    pts = e["pts"]
    lx = e["lx"] if e["lx"] is not None else (pts[0][0] + pts[-1][0]) / 2
    ly = e["ly"] if e["ly"] is not None else (pts[0][1] + pts[-1][1]) / 2 - 8
    svg.append(f'<text x="{lx:.0f}" y="{ly:.0f}" class="lbl" text-anchor="{e["anchor"]}" '
               f'fill="{stroke}">{esc(e["label"])}</text>')
svg.append("</svg>")

LEGEND = "".join(
    f'<span class="li"><i style="background:{C[k][0]};border-color:{C[k][1]}"></i>{txt}</span>'
    for k, txt in [("gen", "生成端（纯函数）"), ("file", "配置真源（JSON）"),
                   ("cali", "校准审查端（本仓库）"), ("exec", "执行端（可替换）"),
                   ("ext", "外部软件（未来接入）"), ("plan", "规划中（后期能力）"),
                   ("art", "产物与审计")])

HTML = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>QXtrlTimingCali — 架构 v0.3.3（文档修订号 · 独立于包版本）</title>
<style>
 body{{margin:0;padding:28px 32px;background:#fbfcfd;color:#1f2933;
      font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif}}
 h1{{font-size:20px;margin:0 0 4px}} .sub{{font-size:13px;color:#5c6b7a;margin:0 0 18px}}
 svg{{width:100%;height:auto;background:#fff;border:1px solid #e3e8ee;border-radius:12px}}
 .t{{font-size:{T_SIZE}px;font-weight:600}} .s{{font-size:{S_SIZE}px;fill:#4a5560}}
 .lbl{{font-size:{S_SIZE}px;font-weight:600;paint-order:stroke;stroke:#fff;stroke-width:3.5px;
      stroke-linejoin:round}}
 .cap{{font-size:12.5px;font-weight:700}}
 .legend{{margin:14px 0 26px;font-size:12px;color:#414f5c}}
 .li{{display:inline-flex;align-items:center;margin-right:16px}}
 .li i{{display:inline-block;width:13px;height:13px;border:1.5px solid;border-radius:3px;
       margin-right:6px}}
 h2{{font-size:15px;margin:26px 0 8px;color:#1d4e75}}
 table{{border-collapse:collapse;font-size:12.5px;width:100%}}
 th,td{{border:1px solid #dde3ea;padding:7px 10px;text-align:left;vertical-align:top}}
 th{{background:#eef2f6}} code{{background:#eef2f6;padding:1px 5px;border-radius:4px;font-size:12px}}
 ul{{margin:6px 0 0 18px;padding:0;font-size:13px;line-height:1.8}}
</style></head><body>
<h1>QXtrlTimingCali · 架构 v0.3.3（Phase-2 批量校准 · 四组六步 · 断点续跑已落地）</h1>
<p class="sub">文档修订号 <b>v0.3.3</b>（独立于包版本，只随图与说明的内容修订递增）· 零第三方依赖 ·
数据主线 <b>q = L(truth) + d(pending)</b> · 生成端与校准端以
JSON 契约解耦 · 校准端与执行端以消息契约解耦 ·
<b>读取线对齐 + headroom 即时写回 → 单比特 / 耦合器只记录 → 统一求解整批落盘 →
读取线收回 headroom 拉回对齐</b> · 测量全程留日志，崩溃可断点续跑</p>
<div class="legend">{LEGEND}</div>
{''.join(svg)}
<h2>读图要点</h2>
<ul>
<li><b>唯一跨边界接口是 contracts 里的五种消息</b>：审查端与执行端之间只有
<code>ExperimentRequest</code>（含申请时的 <code>config_hash</code>）→
<code>ExperimentResult</code>，以及批准后的 <code>WritePlan</code> 下发；
换 QXtrl 只换适配器，不改裁决逻辑。</li>
<li><b>分组测量，统一求解</b>：读取线必须先对齐、再加 <code>headroom</code> 整体右移，测完就写回；
单比特与耦合器这两组之间一个字节都不写，所有 skew 描述同一基线；步 5 把这两组观测合并做
一次加权最小二乘（1/σ²）摊到整张图，再用一张 <code>WritePlan</code> 走完四道守卫；
步 6 每条读取线以其<b>首个腔的 <code>qubit.xy</code></b> 为锚收尾：加了 headroom 就把授予的
余量收回、往左拉回对齐（收回量不超出授予余额），未加则仍只允许右移；
读取线已在右侧时不能保证通过右移读取线完成对齐。
这是"误差不再沿链路累加"的根因——同一组输入，旧流程累计位移 33067.5 ns，批量流程只需 2860.1 ns。</li>
<li><b>批量回写单位就是线</b>：手动单对审查仍保留同步块簿记，
批量模式里它被末尾的一次全局求解取代；<code>pending</code> 文件始终只有
<code>{{line, delay}}</code>。</li>
<li><b>四道守卫缺一不落盘</b>：CAS（申请时的 <code>config_hash</code> ⇔ 当前文件指纹）·
校准只右移（<code>delta ≥ 0</code>，仅 <code>headroom_return</code> 可按授予余额收回）· 预算（<code>d ≤ d_max</code>）·
原子替换 + <code>.bak</code> + JSONL 审计；任一失败文件原样不动，但审计留痕。</li>
<li><b>plan_align 是纯函数</b>：先出计划、策略链先检查，通过才落盘——所以 noop/retry/recheck
分支不会留下半成品。</li>
<li><b>truth 文件只装载设备、只参与事后对账</b>：校准流程不读它，残差
<code>max(q) − min(q)</code> 是模拟端独有的硬回归指标。</li>
<li><b>目标锚点取当前最靠右的那条线</b>：只能右移 ⇒ 它既可达、又让总线位移最小，
于是逐线 <code>δ_i = max(o) − o_i ≥ 0</code> 天然满足单调守卫。</li>
<li><b>两条编排主线，一套内核</b>：<code>pipeline.py</code> 两阶段、不分组（一条 CLI 跑完全部线）；
<code>stages.py</code> 四组六步、按物理依赖分组。两者共用 contracts / session / policies /
center / writeback / graph / ledger / solver / parallel。最大分量求解、替补重连与
<code>commit_solve</code> 数值验收共用；解为空或未收敛时，文件、会话和设备都不推进。</li>
<li><b>波与提交束</b>：<code>parallel</code> 只按共享线名判冲突；同波按并发上限切成提交束，
<code>ledger</code> 经 <code>submit_many</code> 提交，收齐一束后推进下一束。
求解、回写和设备下发始终串行；波数不等于提交次数或实测加速。</li>
<li><b>测量日志支持断点续跑</b>：每条边测完即往 pending 伴生文件
<code>&lt;stem&gt;.measurements-audit.jsonl</code> 追加一行（含 <code>config_hash</code>
与 <code>stage_done</code> 事件）。恢复判据由版本纪律直接给出：日志里 <code>config_hash</code>
与当前待测文件一致的合格观测，描述的正是眼前这个世界——重启后按边跳过不重测；
<code>hash</code> 不一致的阶段已随写回生效，按事件整阶段跳过（headroom 授予台账一并重放，
末尾求解是确定性纯计算、重算即可）。重新生成 pending 视为开启新一轮，伴生日志自动作废。
日志不是完整事务 WAL：文件写回、设备下发与阶段事件记录之间仍有崩溃恢复空窗，
不保证三者原子提交。恢复前必须确认设备已按 pending 同步；hash 也不能证明设备未漂移。</li>
</ul>
<h2>接入 QXtrl（执行端 · 相对完善，积极开发中）</h2>
<table><tr><th>我们的契约字段</th><th>QXtrl 侧对应物</th><th>结论</th></tr>
<tr><td><code>config_hash</code></td><td><code>RecipeResult.config_hash</code> /
CFG <code>raw_hash</code></td><td>本仓库 CAS 使用 pending 的 <code>content_hash</code>；
外部 CFG 指纹须作为独立溯源字段，不能直接替代 pending 指纹</td></tr>
<tr><td><code>quality</code> / <code>flags</code></td><td><code>QualityResult(status, score,
metrics)</code> · 拟合 <code>_r2</code> · <code>RecipeCell.warnings</code></td>
<td>映射即可；<code>quality_min</code> 阈值待其 score 口径确认后标定</td></tr>
<tr><td><code>sigma_ns</code> / <code>n_shots</code></td><td><code>RecipeCell.shots</code> ·
step meta</td><td>有 shots；标准误需由我们按 repeats 分组估计（SimExecutor 已是这个口径）</td></tr>
<tr><td><code>artifacts</code></td><td>HDF5 归档 <code>&lt;archive_root&gt;/&lt;task_id&gt;.h5</code>
· VizRedis v2</td><td>只放路径与哈希，符合既有约定（不内联数组）</td></tr>
<tr><td><code>skew_ns</code>（主判据）</td><td><b>未发现</b>现成相对延时测量实验</td>
<td><b>真正的缺口</b>：每条边的相对延时测量需在 QXtrl 侧新增原子实验（最接近的是 Ramsey 扫 delay）；
好消息是批量模式一轮只要 N−1 次实验，且彼此独立，天然适合排队提交</td></tr>
<tr><td>提交与并发</td><td><code>client.submit()</code> 阻塞 / <code>aio</code> 异步 ·
<code>SubmissionSupervisor</code> 队列 · <code>hardware_hold</code> 独占锁 · CLI 批处理先例</td>
<td>同进程用 <code>DirectExecutor</code> 最省；<code>FileQueue</code> 需薄封装申请⇄Atom+CFG
文件对；并发编排必须服从 <code>hardware_hold</code>（设备独占 → 编排"并行"只是排队）</td></tr>
<tr><td>延时写入归属</td><td>其 <code>analysis/act.py</code> 也会原子回写自己的 CFG</td>
<td><b>风险项</b>：两处都写延时会互踩。需明确唯一归属（当前设计：延时归审查中心，
QXtrl 侧只被下发）</td></tr>
</table>
<h2>借鉴 automation（调度核心 · 筹备中，只取思路）</h2>
<ul>
<li><b>stage 五方法协议</b> prepare/analyze/decide/act/recover：正对应我们的
申请/分析/裁决/回写/补救；measure / finish 两阶段已各自成型，将来编排器按此协议注册即可。</li>
<li><b>attempt_id = submission_id 幂等补查</b>：超时不重投而按 id 补取，比现在的
"retry 直接重发"更省设备时间——纳入 <code>resubmit()</code> 的演进方向。</li>
<li><b>Outcome 四态</b> PASS/FAIL/INCONCLUSIVE/INVALID：把"分析出错"与"校准不合格"分开，
比单一 retry 更准，可细化我们的 recheck / retry 分界。</li>
<li><b>path→stages 依赖失效映射</b>：将来动态编排要回答"哪些线的位移使哪些测量边作废"，
这是最合适的表达雏形；作者刻意不自动回溯，与我们把全量编排推迟到末尾统一求解的判断一致。</li>
<li><b>fsync 的四段式 JSONL 条目</b>（request/execution/analysis/decision）：我们的审计行可合并成
一条带全链证据的记录，回放更省事。</li>
<li><b>不照搬</b>：它的线性 stage 列表与"按位置回放"假设与并行编排冲突；我们的回放应按
request 内容匹配，而不是按 stage 顺序。</li>
</ul>
</body></html>
"""

out = Path(__file__).resolve().parent / "architecture-v0.3.3.html"


# ---------------- 几何自检 ----------------
def text_w(s: str, size: float, bold=False) -> float:
    """按字形宽度估算：CJK 与圈号 1 em，大写字母 0.68 em，其余 ASCII 0.56 em。"""
    em = 0.0
    for ch in s:
        o = ord(ch)
        if o >= 0x2E80 or 0x2460 <= o <= 0x24FF or o in (0x21C4, 0x2194):
            em += 1.0
        elif ch in "·… ":
            em += 0.5
        elif "A" <= ch <= "Z":
            em += 0.68
        else:
            em += 0.56
    return em * size * (1.06 if bold else 1.0)


def _rect(item):
    return (item["x"], item["y"], item["x"] + item["w"], item["y"] + item["h"])


def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _segment_hits(a, b, rect):
    """检查整段正交线，包含与节点边框重合的情况，允许单点接触。"""
    x0, y0, x1, y1 = rect
    if a[1] == b[1]:
        return y0 <= a[1] <= y1 and max(min(a[0], b[0]), x0) < min(max(a[0], b[0]), x1)
    if a[0] == b[0]:
        return x0 <= a[0] <= x1 and max(min(a[1], b[1]), y0) < min(max(a[1], b[1]), y1)
    raise ValueError("架构图连线须使用正交折线")


def _label_rect(e):
    width = text_w(e["label"], S_SIZE, True)
    pts = e["pts"]
    x = e["lx"] if e["lx"] is not None else (pts[0][0] + pts[-1][0]) / 2
    y = e["ly"] if e["ly"] is not None else (pts[0][1] + pts[-1][1]) / 2 - 8
    left = x if e["anchor"] == "start" else x - width if e["anchor"] == "end" else x - width / 2
    return (left - 2, y - S_SIZE - 2, left + width + 2, y + 5)


def selfcheck(check_nodes=None, check_edges=None):
    """同时检查节点、整段连线、箭头端段与标签，返回全部问题。"""
    check_nodes = nodes if check_nodes is None else check_nodes
    check_edges = edges if check_edges is None else check_edges
    issues = []
    rectangles = [_rect(n) for n in check_nodes]
    for i, a in enumerate(rectangles):
        for b in rectangles[i + 1:]:
            if _overlap(a, b):
                issues.append(f"节点重叠：{a} / {b}")
    for n, rect in zip(check_nodes, rectangles):
        if not (0 <= rect[0] < rect[2] <= RIGHT + 8 and 0 <= rect[1] < rect[3] <= H):
            issues.append(f"节点出界：{n['title']}")
        for line in [n["title"]] + n["lines"]:
            size, bold = (T_SIZE, True) if line == n["title"] else (S_SIZE, False)
            if text_w(line, size, bold) > n["w"] - 22:
                issues.append(f"文字横向溢出：{n['title']} / {line}")
        if n["lines"] and 38 + (len(n["lines"]) - 1) * 15 + 4 > n["h"]:
            issues.append(f"文字纵向溢出：{n['title']}")
    labels = [(e, _label_rect(e)) for e in check_edges if e["label"]]
    # 大框标题也属于需要避开的文字区域。
    headers = [(f["x"] + 12, f["y"] + 3,
                f["x"] + 16 + text_w(f["label"], 12.5, True), f["y"] + 22)
               for f in frames if f["label"]]
    for e in check_edges:
        name = e["label"] or str(e["ports"])
        for port in e["ports"]:
            if not any((r[0] <= port[0] <= r[2] and port[1] in (r[1], r[3]))
                       or (r[1] <= port[1] <= r[3] and port[0] in (r[0], r[2]))
                       for r in rectangles):
                issues.append(f"连线未连接节点边界：{name} / {port}")
        for point in e["pts"]:
            if not (0 <= point[0] <= W and 0 <= point[1] <= H):
                issues.append(f"连线出界：{name} / {point}")
        for a, b in zip(e["pts"], e["pts"][1:]):
            if a[0] != b[0] and a[1] != b[1]:
                issues.append(f"非正交连线：{name}")
                continue
            for n, rect in zip(check_nodes, rectangles):
                if _segment_hits(a, b, rect):
                    issues.append(f"连线穿过节点或沿边框重合：{name} / {n['title']}")
            for _, rect in labels:
                if _segment_hits(a, b, rect):
                    issues.append(f"连线被标签遮挡：{name}")
            if any(_segment_hits(a, b, rect) for rect in headers):
                issues.append(f"连线穿过大框标题：{name}")
        if hypot(e["pts"][-1][0] - e["pts"][-2][0],
                 e["pts"][-1][1] - e["pts"][-2][1]) < 12:
            issues.append(f"箭头端段不足：{name}")
    for i, (e, rect) in enumerate(labels):
        if not (0 <= rect[0] <= rect[2] <= RIGHT + 8 and 0 <= rect[1] <= rect[3] <= H):
            issues.append(f"标签出界：{e['label']}")
        if any(_overlap(rect, other) for other in rectangles + headers):
            issues.append(f"标签覆盖节点或大框标题：{e['label']}")
        if any(_overlap(rect, other) for _, other in labels[i + 1:]):
            issues.append(f"标签互相覆盖：{e['label']}")
    return issues


if __name__ == "__main__":
    issues = selfcheck()
    if issues:
        raise SystemExit("\n".join(issues))
    out.write_text(HTML, encoding="utf-8")
    print(f"OK nodes={len(nodes)} frames={len(frames)} edges={len(edges)} W={W} H={H} → {out.name}")

# QXtrlTimingCali — 超导量子比特 Timing 校准系统

Phase-0：配置生成 + 校验 + 可视化（架构规格见 `../scq-timing/docs`，v0.10）；
Phase-1：模拟芯片 + 审查中心 + 守卫式回写；
**Phase-2（当前）= 批量校准**：一次性测完全部相对时序 → 全局最小二乘求解 → **整批一次写回**，
替换掉原先"测一对、写回一对"的累积式流程；`stages` 再按真实实验顺序把测量分成**四组六步**
（读取线 `ro ↔ ri` 对齐 + headroom 授予**即时写回** → 单比特 XY-Z → 比特间/耦合器 Z-Z，
后两组只记录 → 末尾统一求解、整批一次写回 → 读取线收回 headroom、拉回对齐再次**即时写回**）。
（架构图版本独立于包版本、只随图与说明的内容修订：`python docs/gen_architecture.py` → `docs/architecture-v0.3.3.html`）
**零第三方依赖**（Python ≥ 3.10 标准库）。

## 模块

| 模块 | 职责 |
| --- | --- |
| `qxtrl_timing_cali.chip_gen` | 芯片文件生成函数：给**构型参数**即推出 pos / 耦合器 / connections，无需逐条描述连接（chain/ring/ladder/grid/**chains 多链**） |
| `qxtrl_timing_cali.wiring_gen` | 连线延时文件生成函数：同一函数产出**真实值保留**（随机初始位置 L）与**待测**（初值 0）两种语义 |
| `qxtrl_timing_cali.validate` | 芯片 / 连线校验器（结构·唯一·引用·数值·图·交叉） |
| `qxtrl_timing_cali.visualize` | 芯片可视化前端（自包含 HTML/SVG · 悬停参数 · 连线表叠加） |
| `qxtrl_timing_cali.review` | **审查中心**：申请实验 → 取结果 → 分析 → 决议 → 守卫式回写（子模块见下） |
| `qxtrl_timing_cali.sim` | 模拟设备替身 `SimChip` + 参考执行方 `SimExecutor`（真实测控软件的占位） |
| `qxtrl_timing_cali.pipeline` | **批量校准流水线**：最小测量集 → 只测量 → 全局求解 → 整批一次写回 |
| `qxtrl_timing_cali.stages` | **分组校准**（四组六步 · 贴合真实实验顺序）：读取线 `ro ↔ ri` 对齐 + headroom 授予**即时写回** → 单比特 XY-Z → 比特间/耦合器 Z-Z（后两组只记录）→ 末尾统一求解、整批一次写回 → 读取线收回 headroom 再次**即时写回**；可整轮跑完，也可 `stage_status()` / `run_group()` / `run_final()` **按组单独驱动**（供上层调度器） |

`review` 包内部：`contracts`（五种消息契约）· `session`（逐线 d 簿记 + 纯计划生成）·
`policies`（analyze 事实 + `quality_problems` 质量判据 + 阈值策略链）· `center`（四项技能）·
`graph`（候选线对 · 最小生成树选边 · 替补边）· `ledger`（**只记录不写回**的测量台账 +
断点续跑测量日志）·
`solver`（加权最小二乘：一堆相对时序 → 逐线右移量）· `writeback`（四道守卫）·
`queue_io`（执行方适配器：同进程 / 文件队列）。

> 批量流程不再使用「同步块」——块只在"逐步合并"的模型里才有意义；
> 现在的合并动作被末尾的一次全局求解取代了。回写单位就是线。

## 快速开始

**新接手代码：打开 `docs/learning-path.md`** —— 一周从"能跑"到"能改"的阅读路线
（分层依赖地图 · 每个模块的关键不变量 · 常见陷阱与钉住它们的测试）。

**想看全流程：打开 `examples/demo_calibration.ipynb` 并 Run All** —— 走通**四组六步顺序实验**的闭环
（生成芯片配置并可视化 → 造 0–1000 ns 随机真值的模拟环境 → 生成待测文件 → 启动审查中心与模拟执行方 →
按物理依赖排四组实验 → **步骤 1-2 读取线 `ro`/`ri` 测完立刻写回**（顺带授予 headroom）→ 步骤 3 单比特 XY-Z、
步骤 4 比特间/耦合器 Z-Z **只记录不写回** → 步骤 5 把两组数据一起求解、一次写回 → 步骤 6 读取线收回
headroom、拉回对齐并立刻写回 → 与隐藏真值对账；
附录演示"步骤 1 就失败 → 后两组根本不开始、文件一个字节不动"）。中间产物落 `runs/demo/`。

**想看并行化方案：打开 `docs/demo-animation-parallel.html`** —— 4×4 最近邻网格（全可调 ·
上下、左右相邻全部连接，24 个耦合器、64 条线）：单比特组 16 条边**同波分束提交**，耦合器组 39 条测量分 7 波，
全程 **63 次测量 → 10 个冲突波 → 18 个提交束**（每束最多 4 条）。
63/10=6.3 是波划分比值；各实验等时、束内完全并发且忽略开销时，
按提交束计算的理论比值为 63/18=3.5，均非实测加速。
左边芯片图按波点亮，右边展示波与提交束；甘特段宽按边数绘制，表示进度而非耗时。
数据真跑：`python docs/gen_parallel_data.py` → `python docs/gen_parallel_animation.py`
（多音读取让同读取线的多比特彼此独立，所以冲突规则只有"共享线名"这一条）。

**想看四组实验怎么跑：打开 `docs/demo-animation.html`** —— 单文件动画（零依赖 · 离线可开 ·
可播放/暂停/单步/变速/点任意一幕跳转）：左边芯片图看**四组的测量边按组点亮**
（读取线绿 → 单比特蓝 → 耦合器紫 → 收尾拉回对齐橙），右边时间轴看 16 条线**分三段右移**：
组 1 读取线的 `d₁` 即时落盘先走，末尾整批的 `d₂` 跟上，收尾的 `d₃` 把读取线拉回对齐，
从 142.7 ns 失配校到 **0.213 ns 残差**。
数据不是编的，是四组流水线真跑出来的：
`python docs/gen_animation_data.py`（真跑流水线导出 `docs/animation-data.json`）→ `python docs/gen_animation.py`
（内嵌成单文件，并顺手做一次几何自检：坐标出界 / 文字溢出 / 量程不覆盖都会打印出来）。
布局尺寸全部由数据推导，换芯片或换延时量级都不会把画裁掉（`tests/test_docs_animation.py` 守这条）。

只想敲命令的话，逐条等价于：

```bash
# 生成 8 比特演示芯片（--id / --seed 给上，命令才能原样复现入库的那份）
python -m qxtrl_timing_cali.chip_gen --id chip-8q --qubits 8 --seed 42 \
       --out examples/chip-8q.json

# 同一函数产出两份同结构文件：真实值保留（隐藏真值 L）+ 待测（初值 0）
python -m qxtrl_timing_cali.wiring_gen --chip examples/chip-8q.json --kind truth \
       --seed 7 --out examples/wiring-8q-truth.json
python -m qxtrl_timing_cali.wiring_gen --chip examples/chip-8q.json --kind pending \
       --out examples/wiring-8q-pending.json

# 换构型：方格 3x4（读取线按行分组）/ 环 / 追加长程边
python -m qxtrl_timing_cali.chip_gen --id chip-grid12 --shape 3x4 --topology grid \
       --per-line 0 --seed 21 --out examples/chip-grid12.json
python -m qxtrl_timing_cali.chip_gen --id chip-ring6 --qubits 6 --topology ring \
       --seed 22 --out examples/chip-ring6.json
python -m qxtrl_timing_cali.chip_gen --id chip-8q-sw --qubits 8 --topology chain \
       --extra-edges 0-7,2-5 --seed 43 --out examples/chip-8q-sw.json

# 可视化（浏览器直接打开单文件；按 pos 二维布局）
python -m qxtrl_timing_cali.visualize --chip examples/chip-8q.json \
        --wiring examples/wiring-8q-truth.json --out examples/chip-8q.html --open
# grid 构型想拉开间距：--grid 给出每毫米像素数，覆盖自动缩放（默认上限 260 px/mm）
python -m qxtrl_timing_cali.visualize --chip examples/chip-grid9.json --grid 280 \
        --out examples/chip-grid9.html

# 独立校验
python -m qxtrl_timing_cali.validate chip   examples/chip-8q.json
python -m qxtrl_timing_cali.validate wiring examples/wiring-8q-truth.json \
        --chip examples/chip-8q.json

# 测试
python -m unittest discover -s tests -v
```

## 构型参数（topology）

| 取值 | 布局 | 耦合边 | 度数 |
| --- | --- | --- | --- |
| `chain` | 一维行 | N−1 | ≤2 |
| `ring` | 圆周等分 | N | 2 |
| `ladder` | 两轨 + 横档（N 为偶数） | 3N/2 − 2 | ≤3 |
| `chains` | 多条独立链（`shape=(链数, 每链比特数)`） | 链数×(每链−1) | ≤2（链内） |
| `grid` | 方格（`shape=rows x cols`，缺省取近方形） | rows(cols−1)+cols(rows−1) | ≤4 |

- `extra_edges` 追加长程边（small-world / 跨链连接），与已有边自动去重；CLI 写作 `--extra-edges 0-7,2-5`
- `chains` 的链间默认不耦合：多链校准时单比特组可放在同一冲突波、按上限分束提交；要连成一张可全局求解的图，用 `--extra-edges` 补几条桥接边即可
- `cavities_per_line`：整数=按比特序号切块；`None`（CLI `--per-line 0`）=一行一条读取线
- 更特殊的点阵（heavy-hex 等）用 `grid` + `extra_edges` 近似，或生成后**手工精修 JSON**——
  校验器会守住 `connections` 与结构的双向一致，手改漏改会直接报错

## 延时文件（一个函数 · 两种语义）

结构完全一致（`line` + `delay`），由 `meta.kind` 声明语义，可逐线直接相加：

| kind | 含义 | delay 取值 | 谁写 / 谁读 |
| --- | --- | --- | --- |
| `truth` | **真实值保留**：模拟设备的隐藏真值 L（初始时刻位置） | 随机 `[--lo, --hi]`（默认 40–200 ns） | 生成时一次写入，事后核对用；校准流程不读不改 |
| `pending` | **待测**：校准流水线的输入与输出载体 | 全 0.0，随后填入累计右移 d | 流水线读当前值、逐线回写（只增不减） |

某线的时序位置 `q = L(truth) + d(pending)`。有了 truth 文件，校准结果可与隐藏真值
对账（残差 = 各线 q 的不一致量），这也是后续噪声与拒绝分支的判据来源。

## 审查中心（自动校准端）

实验由**其他软件**执行，审查中心只负责申请与裁决，两端靠契约解耦：

```
申请 ExperimentRequest → [外部执行] → 结果 ExperimentResult
   → 分析 Assessment → 决议 Verdict → 回写计划 WritePlan → 守卫式落盘（pending 文件）
```

**四项技能**（编排器有两个：`pipeline.py` 两阶段不分组、`stages.py` 四组六步，批量版均已落地；
自适应选边与并行仍是后期能力）：
`request_align()` 申请 · `collect()` 取结果 · `review()` 分析+裁决 · `_execute()` 决议回写。

决议动作：`commit`（按写回计划落盘）· `noop`（偏差在容差内：只合并同步块、省延时预算；
批量模式下极少出现——求解只在"确实需要位移"时才给非零 δ）·
`retry`（质量不达标，重发同一申请）· `recheck`（结果过期 / 守卫拒绝，在新版本上重测）·
`replan`（会话状态不支持该计划，交编排器换路）· `escalate`（用尽重试 / 超预算，转人工）。

结果契约字段分层：主判据 `skew_ns`（q(new) − q(ref)）/ `sigma_ns` / `quality`；
溯源 `request_id` / `config_hash`（测量所依据的待测文件版本）/ `measured_at`；
自报 `flags[]` / `n_shots`；证据 `artifacts{}`（只放路径与哈希，不内联数组）；可选 `repeats[]`。

`flags[]` 分三级（`policies.py`）：`KNOWN_FLAGS`（drift / low_snr / low_contrast / crowded）
只记录不判废，仍由 σ 与 quality 说话；`FATAL_FLAGS`（saturation / fit_failed / hardware_error）
直接判废；名单外一律按异常处理——宁可不写，也不把看不懂的结果落盘。

四道回写守卫（缺一不可，全部通过才落盘）：

| 守卫 | 判据 | 不通过时 |
| --- | --- | --- |
| ① CAS | 当前文件指纹 == 申请时指纹 | 丢弃结果 → `recheck` |
| ② 单调 | 逐线 `delta ≥ 0` | 拒绝写入（只右移） |
| ③ 预算 | 回写后累计 `d ≤ d_max_ns` | 拒绝写入 → `escalate` |
| ④ 原子 | 临时文件 + `os.replace` + `.bak` + JSONL 审计 | 文件保持原样，审计留痕 |

逐步审查模式下回写**单位是同步块**、**存储单位仍是线**（块簿记只在会话与审计里，
pending 文件保持 `line + delay` 极简）；批量模式下回写单位就是线，不再有块。
阈值集中在 `Thresholds`（`tol_ns` 0.5 · `sigma_max_ns` 1.0 · `quality_min` 0.6 ·
`skew_max_ns` 1500 · `spread_max_ns` 2.0 · `d_max_ns` 2000 · `max_attempts` 3），待真实经验值替换。
批量模式下一轮只写一次，`d_max_ns` 只需覆盖「各线到最右线的差距」（≈ 初始位置 L 的极差）；
流水线 CLI 默认给到 **5000 ns**（`--d-max`），范围拉大时才需要再往上调。

对接真实执行软件（`DirectExecutor` 之外的另一端）：`FileQueue("runs")` —— 写 `runs/requests/<id>.json`、
读 `runs/results/<id>.json`，取回后自动归档到 `consumed/`，契约不合格的落 `rejected/` 不阻塞流程。
**接入真实实验的完整操作教程（入口函数 / 结果契约 / 设备同步协议 / 常见坑）：`docs/executor-integration.md`**。

## 批量校准流水线

两条编排主线共用同一套契约、守卫与求解器，区别只在"测哪些边、什么时候写"：

### ① `pipeline.py` —— 两阶段、不分组（一条 CLI 跑完全部线）

旧流程是"测一对 → 裁决一对 → 立刻写回一对"：单步误差沿链路累加，越校越歪。
现在拆成两个互不干扰的阶段：

| 阶段 | 做什么 | 副作用 |
| --- | --- | --- |
| A 测量 | 按最小生成树挑 N−1 条边 → 逐条测相对时序 → 全部记进台账 | **零**：不改文件、不动会话 `d`、不下发设备 |
| B 求解写回 | 全部观测一次加权最小二乘 → 一张 `WritePlan` | 整批一次走完四道守卫 |

于是所有 `skew` 描述的是**同一时刻**的物理状态，随机误差按 1/σ² 分摊到整张图，
而不是沿链路累加。坏边先重试，用尽就找最便宜的替补边重新搭桥，实在接不上才划为隔离区
（本轮不写回，列进报告 `isolated` 交下一轮）。

目标锚点取**当前最靠右的那条线**——只能右移，所以它既可达、又让总线位移最小——
于是逐线位移 `δ_i = max(o) − o_i ≥ 0` 天然成立：**任一条线超预算，整批一个字节都不写**。

```bash
# 全流程：读 truth（设备真值）+ pending（待测），两阶段一次跑完
python -m qxtrl_timing_cali.pipeline --chip examples/chip-8q.json \
        --truth examples/wiring-8q-truth.json --pending runs/wiring-8q-pending.json
# → 线 31 条 · 测量 30 次（恰好 N−1）· 总位移 2860.1 ns · 残差 0.254 ns · aligned=True
# 产物：runs/pipeline-report.json（测量台账 + 求解诊断）、session.json、writeback-audit.jsonl、
#       <pending>.measurements-audit.jsonl（断点续跑用测量日志）、*.bak
```

同一组输入：旧流程要 31 步、累计位移 **33067.5 ns**；批量流程 30 次测量、位移 **2860.1 ns**
——正好等于"各线与最右线的差距之和"，一分不多。省下来的全是逐次累加的无效应移。

报告字段：`tree`（选中的测量边）· `resumed_edges`（从测量日志恢复、未重测的边数）·
`measurements`（逐边 skew 与尝试次数）· `failed` / `isolated` ·
`solve`（相对位置、目标、逐观测残差、迭代次数）· `anchor`（目标锚线）· `plan_ns` / `budget_ns` ·
`residual` · `converged`（无隔离区、解非空且数值收敛）·
`aligned`（收敛、无中止，提供 truth 时还须残差 ≤ `tol_ns`）。
未提供 truth 时 `aligned` 只表示流程判据通过，不代表独立复测已验证；
`written` 单独表示本次是否发生文件写回，dry-run 或已完成阶段的恢复可能为 False。
未收敛的解禁止应用到文件、会话和设备；两套编排器共用数值验收、最大连通分量
求解及替补重连规则。`max_replacements=0` 表示不尝试替补。
批量流程不再使用同步块，报告里也就没有 `blocks` 字段了。

### ② `stages.py` —— 四组六步、按物理依赖分组

真机上不能"所有线一起做优化"：X_π 要靠读取线才读得出信号，单比特门跑通了才谈得上跨比特，
耦合器又要挂在相邻比特上。于是按**顺序执行、数据依赖**切成四组（合称六步）：

| 组 | 步骤 | 实验 | 数据怎么处置 | 依赖 |
| --- | --- | --- | --- | --- |
| 1 `readout` | 步 1–2 | 读取线 `ro ↔ ri` 对齐，再授予 headroom 整体右移 | **测完立刻求解并写回** | — |
| 2 `qubit` | 步 3 | 单比特 `xy ↔ z` | 只记录，末尾统一写 | 组 1 |
| 3 `coupler` | 步 4 | 跨比特 `Qa.z ↔ Qb.z`、耦合器 `CP.z ↔ Q.z`（不可调比特退化为 `Q.xy`） | 只记录，末尾统一写 | 组 2 |
| — | 步 5 | 组 2 + 组 3 观测合并全局最小二乘 | **整批一次写回** | 组 3 |
| 4 `readout_final` | 步 6 | 读取线挂到首腔 `qubit.xy`，收回步 2 的 headroom | **测完立刻写回** | 步 5 |

要点：

- 读取线没对齐，X_π 做出来也读不出信号，所以组 1 的结果**不能攒到最后**；
- 组 2、组 3 之间没有写回，两组 `skew` 同基线，因此步 5 能合成一次求解；
- 组 1 里各条读取线互无基准（组内只有"同线 `ri ↔ ro`"的边），按分量各自定标后就地落盘，
  跨读取线的那部分失配由组 3 的跨比特边在末尾一并解掉；
- 读取线不参与步 5 的解；步 6 用 `readout ↔ 首腔 qubit.xy` 的实测 skew 把"读取线世界"与
  "qubit/coupler 世界"连起来；
- **headroom** 是流水线自己授予的临时余量（记入会话 `headroom` 台账）：步 6 解出的负向位移以
  `WritePlan.headroom_return` 收回，`d` 永远不会低于加 headroom 之前的校准基线；不加 headroom 时
  读取线本来就在左边，照旧只向右拉；
- 任一组拿不到前置基准，编排器按 `depends_on` 停在那里，后面的组不会开始；
- 全程**三次落盘**（组 1、步 5、组 4），同一条线的 `d` 是这三次之和，`d_max_ns` 要按总位移上限给。

```python
from qxtrl_timing_cali import StagedCalibrationPipeline, plan_stages

pipe = StagedCalibrationPipeline(chip, center)
for p in plan_stages(chip, center.session.lines()):
    print(p.name, p.writeback, p.depends_on, len(p.pairs))   # 只看计划，不测量
report = pipe.run(truth)                    # 四组一次跑完（truth 仅用于对账）
```

> **复跑不必重新生成待测文件**：两个 CLI 启动时都会按 pending 当前的 `d` 还原模拟设备，
> 于是"设备状态 == 文件状态"成立——在已落盘的文件上再跑一轮只会测到 ≈0 的 skew、
> `plan_ns = 0`、不再写回（`tests/test_review.py` 的 `test_rerun_after_writeback_does_not_stack` 守住）。
> 只有自己写脚本直接 `SimChip(truth)` 而不调用 `seed_applied_shifts()` 时才会踩叠加的坑。
> 想**开新一轮**（弃掉本轮全部观测）才需要 `wiring_gen --kind pending`，伴生日志会自动作废。

### ③ 分阶段驱动：每次只推进一步（供上层调度器）

真机上校准只是整条实验流程的一环：读完 `ro/ri` 之后还要插 S21 / spectrum / scatter /
power Rabi 等实验，剩下的组什么时候跑由**上层自动化控制中心**决定，不该由本仓库替它决定。
因此除 `run()`（一次跑完，给 demo）外另提供三个入口，**每次只推进一步，绝不顺带往后跑**：

| 入口 | 干什么 |
| --- | --- |
| `pipe.stage_status()` | 只看不跑：每组 `state`（pending / measured / applied）、`ready`、`blocked_by`，外加 `todo` 下一步建议与已授予未收回的 headroom |
| `pipe.run_group(name)` | 只跑一组；依赖不满足就拒绝（文件一个字节不动）；已生效的组直接跳过，不重测 |
| `pipe.run_final(truth)` | 只跑步 5：延后组观测合并求解 → 整批一次写回 |

```python
pipe = StagedCalibrationPipeline(chip, center, readout_headroom_ns=100.0)

st = pipe.stage_status()                    # 先问体检报告
while st["todo"]:                           # 节奏完全由调度器掌握
    if st["todo"][0] == "final":
        pipe.run_final(truth)               #   （中间随时可以插别的实验）
    else:
        pipe.run_group(st["todo"][0])       #   跑一组就停，等下一次召唤
    st = pipe.stage_status()
```

三个入口**跨进程可用**：状态与观测全部来自待测文件指纹和测量日志，步 5 的全局解随
`final_solve` 事件带走、headroom 授予台账按事件重放——所以步 6 在新进程里单独驱动仍能
正确收回 headroom（CLI 每调一次就是一个新进程；`tests/test_stages.py::TestSingleStageDriving`
把"分次驱动 ≡ 一次跑完"钉住了）。

等价的 CLI（同一套语义，方便 shell / 外部调度器直接调）：

```bash
# 只看状态（--json 给程序读）
python -m qxtrl_timing_cali.stages --chip examples/chip-8q.json --pending runs/p.json --status
# 只跑一组：readout | qubit | coupler | readout_final
python -m qxtrl_timing_cali.stages --chip examples/chip-8q.json --pending runs/p.json        --stage readout --headroom 100
# 只跑步 5（延后组合并求解 + 整批一次写回）
python -m qxtrl_timing_cali.stages --chip examples/chip-8q.json --pending runs/p.json --final
```

> 分批驱动的前提是**设备状态与待测文件一致**：CLI 启动时按 pending 当前的 `d` 还原模拟
> 设备（否则新进程从 `q = L` 起算，测出来的是另一个世界）。自己的执行方适配器同理——
> `apply(plan)` 必须把批准并落盘的位移真正下发到设备。

### ④ 并行策略：哪些边可以同时测

省时间的主要手段不是少测，而是**同时测**。每组最少测量集都会附带一份**波划分**：

```
冲突 ⇔ 两条边共享任意一条线名
```

注意"共享读取线"**不算**冲突——同一读取线上的多个腔可以多音读取、分别解调，
各比特在读出环节是独立的。真正不能同时开的是一条线出现在两个时序关系里
（如 `Q0.z↔Q1.z` 与 `CP0001.z↔Q0.z` 都碰 `Q0.z`）。划分就是按代价从低到高往波里塞的贪心装箱：

| 组 | 波数（8 比特 chain / 31 线） | 说明 |
| --- | --- | --- |
| `readout` | 1 波（4） | 各读取线独立 |
| `qubit` | 1 波（8） | 全部比特同时测 |
| `coupler` | 3 波（4+4+6） | 按共享 `Q.z` 分波 |
| `readout_final` | 1 波（4） | 各读取线独立 |

波是**提交提示，不是并发承诺**：执行方可以按顺序跑完一波（真机 `hardware_hold` 独占锁时本就如此），
结果按 `request_id` 归位，上层无感。并行只影响"观测怎么到达"，**不影响**一次全局最小二乘 +
一次整批落盘的语义；求解、写回、设备下发始终串行。

```bash
# 先看波（不测量）
python -m qxtrl_timing_cali.stages --chip examples/chip-8q.json --pending runs/p.json --status

# 真的按波并发提交（每条提交束最多 4 条边）
python -m qxtrl_timing_cali.stages --chip examples/chip-8q.json --pending runs/p.json        --stage coupler --parallel 4 --wait 0.5
python -m qxtrl_timing_cali.pipeline --chip examples/chip-8q.json --pending runs/p.json        --parallel 4 --wait 0.5
```

`--parallel N` 默认 **1**（逐条提交，与历史行为逐位一致）；`--wait` 是一波结果未齐时最多再等几秒
（异步执行方才用得上）。自己写代码则是 `StagedCalibrationPipeline(chip, center, max_parallel=4)`
或 `CalibrationPipeline(chip, center, max_parallel=4)`，报告里的 `waves` 字段带完整波结构。

### 测量日志与断点续跑（两条主线共用）

真机上一条边几分钟、一整批几小时，而批量流程把全部观测攒到末尾才写回，崩溃的爆炸半径最大。
每条边测完即往 pending 伴生文件 `<stem>.measurements-audit.jsonl` **追加一行**
（含 `config_hash` 与 `stage_done` 事件）。恢复判据由版本纪律直接给出：
**日志里 `config_hash` 与当前待测文件一致的合格观测，描述的正是眼前这个世界**——
重启后按边跳过不重测；`hash` 不一致的阶段已随写回生效，按事件整阶段跳过
（headroom 授予台账一并重放，末尾求解是确定性纯计算、重算即可）。
重新生成 pending（`wiring_gen --kind pending`）视为开启新一轮，伴生日志自动作废。

## 约定（芯片沿用 v1.0 规格；延时文件精简为模拟专用）

- 线名推导：`<Q>.xy` / `<Q>.z` · `<CP>.z` · `<RL>.ro` / `<RL>.ri`；可调性 `meta.tunable` 全局声明
- `pos` / `couplers` / `connections` 由构型参数一次性推导，`meta.topology` 记录所用构型；
  与真实芯片有出入时直接改 JSON，`connections` 仍与结构双向核对
- 延时文件只含 **line + delay**：不含 channel——模拟无真实连线，通道映射不影响时间校准；
  真实实验的通道信息由测控配置（QXtrl `cfg.json`）承载
- `delay` 一律 ≥ 0（truth 是物理线长，pending 是只增不减的累计右移）
- 三文件线路集合必须一致（芯片 ↔ truth ↔ pending，`validate` 交叉校验强制）；
  `channel` 若存在仍会校验唯一性（兼容带映射的连线文件）

## 路线图

- [x] Phase-0：配置生成 / 校验 / 可视化
- [x] Phase-1：模拟芯片 + 审查中心 + 守卫式回写（CAS + 单调 + 预算 + 原子写 + `.bak` + JSONL 审计）
- [x] 逐步六阶段流水线原型（对齐 scq-timing，暴露了"逐对写回 → 误差沿链路累加"的问题）
- [x] Phase-2：**批量校准** —— `graph` 最小生成树选边（N 条线只需 N−1 次实验）·
      `ledger` 只读测量台账 · `solver` 加权最小二乘全局求解 · 整批一次写回 · 坏边隔离区
- [x] 测量日志与断点续跑：逐边追加 `<pending>.measurements-audit.jsonl`，按 `config_hash`
      恢复同世界观测 / 按 `stage_done` 事件跳过已生效阶段（headroom 授予随事件重放）
- [ ] 阈值经验值标定（`Thresholds` 数值替换为真实判据）与拒绝分支调参
- [ ] 选边策略升级：按 σ 预估做自适应加权（当前代价是四类静态常量），并在子图上做并行测量
- [ ] 坏边隔离区的续跑：把 `isolated` 线路作为下一轮的独立批次自动接上
- [ ] 多轮收敛：本轮因预算/噪声未 `aligned` 时的自动续批与收敛判据
- [ ] 真实数据接入与 QXtrl 测控适配（`FileQueue` 对接外部执行软件）

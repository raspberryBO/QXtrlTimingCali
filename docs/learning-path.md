# QXtrlTimingCali 代码学习路线

> 面向第一次接触本仓库的工程师。目标：**一周内从"能跑"到"能改"**——理解每层的不变量，
> 能安全地动求解器、守卫和编排器。模块与字段以当前代码为准，包版本 v0.4.1；
> 演示使用 4×4 最近邻网格，架构图修订号与包版本独立。

---

## 0. 开始之前：三句话的心智模型

1. **每条线有一个时序位置 `q = L + d`**。`L` 是设备隐藏真值（truth 文件），`d` 是校准累计右移
   （pending 文件，只增不减——唯一例外是收回 headroom）。校准 = 让所有线的 `q` 对齐。
2. **测量只能得到两线之差**（skew = q(a) − q(b)），永远测不到绝对位置；
   于是校准分两步：测一堆相对时序 → 一次全局最小二乘解出逐线右移量。
3. **实验由外部软件执行，本仓库只申请与裁决**。审查中心与执行方靠五种 JSON 消息解耦，
   回写必须过四道守卫（CAS / 单调 / 预算 / 原子）才落盘。

记住这三句，下面的每个模块都是在回答其中一句话的"怎么做"。

理想无噪声、初始 `d = 0` 且全局连通时，最小非负补偿为
`d_i = max(L) − L_i`，最终所有 `q_i = max(L)`。已有补偿、临时 headroom 与测量噪声
会改变实际求解与回写过程；实测按容差验收，不能把严格相等或 `max(L)` 当成通用回写判据。

---

## 1. 第一天：建立全局观（不写代码）

| 顺序 | 材料 | 看什么 |
| --- | --- | --- |
| 1 | [README.md](../README.md) | 通读。它本身就是架构文档：模块表、四组六步、守卫表、路线图 |
| 2 | [examples/demo_calibration.ipynb](../examples/demo_calibration.ipynb) | **Run All**。走通"生成芯片 → 模拟设备 → 四组六步 → 与真值对账"，把术语变成画面 |
| 3 | [普通流程动画](demo-animation.html) | 浏览器打开。看 16 条线的初始位置与三次净位移；d₃ 在收回 headroom 时可为负 |
| 4 | [并行网格动画](demo-animation-parallel.html) | 4×4 上下、左右最近邻全部耦合：24 个耦合器、64 条线；63 条测量 → 10 波 → 18 束。点击波格或调度波段播放本波，结束自动暂停；全局条形图展示校准位置，台账明细可展开 |
| 5 | [架构图 v0.3.3](architecture-v0.3.3.html) | 确认分层、JSON 契约、设备同步与审计边界 |
| 6 | [6 页项目简报](presentations/QXtrlTimingCali-当前版本简报-网格演示-修订版.pptx) | 复述模块分工、四组六步与后端接入条件；第 5 页链接交互演示。分享时使用[完整演示包](presentations/QXtrlTimingCali-简报与交互演示-修订版.zip)，解压后保持 PPT 与 HTML 同目录 |

自测：能不看资料说出"为什么组 1（读取线）测完要立刻写回，而组 2、组 3 只记录"？
（答：X_π 要靠读取线才读得出信号；且组 2+3 同基线观测才能在末尾合并成一次求解。）

---

## 2. 依赖分层地图

代码按依赖分层，**高层模块依赖低层模块**（层号较大者依赖层号较小者）。
读代码从第 0 层开始，先读被依赖者；下图静态工具的排列表示阅读顺序，不表示调用链：

```
第 0 层  数据模型     review/contracts.py        五种消息（全系统的通用语言）
第 1 层  静态工具     chip_gen / wiring_gen / validate / visualize
                                  （互相独立，只认 JSON 规格）
第 2 层  审查端       review/ 内九个模块（见 §4）
第 3 层  编排器       sim → pipeline → stages   （stages 集大成，最后读）
第 4 层  测试与文档   tests/ · docs/gen_*.py     （可执行规格 + 真跑数据）
```

一个实用提示：**先读 pipeline 再读 stages**。两者共用 `solver.solve_main_component`
（最大分量）、`ledger.reconnect`（替补重连）与 `center.commit_solve`（数值验收后落盘）；
分组依赖、headroom 和报告视角仍各自编排。历史上的连通性过滤只加一边的缺陷，
应由这些共用入口防止再次出现。

---

## 3. 第 0–1 层：数据模型与静态工具（约 1000 行）

### [review/contracts.py](../qxtrl_timing_cali/review/contracts.py)（约 260 行）— 必须逐行读

五种消息是全链路的 JSON 载体，后面所有模块都在传递它们：

| 消息 | 方向 | 关键字段 |
| --- | --- | --- |
| `ExperimentRequest` | 中心 → 执行方 | `targets[0]=new, targets[1]=ref`、`config_hash`（在哪个文件版本上测） |
| `ExperimentResult` | 执行方 → 中心 | 主判据 `skew_ns / sigma_ns / quality`；自报 `flags[]`；`from_dict` 会拒收非有限数值 |
| `Assessment` | 内部 | 阈值无关的事实 + 建议计划 |
| `Verdict` | 内部 | 六种动作：commit / noop / retry / recheck / replan / escalate |
| `WritePlan` | 内部 → 守卫 | `shifts`（恒 ≥ 0）+ `headroom_return`（唯一合法负向通道） |

注意 `skew_ns` 的符号约定：**skew = q(new) − q(ref)**，全仓库的符号推导都建立在它上面。

### [chip_gen.py](../qxtrl_timing_cali/chip_gen.py) + [wiring_gen.py](../qxtrl_timing_cali/wiring_gen.py)

- 线名约定：`<Q>.xy` / `<Q>.z` / `<CP>.z` / `<RL>.ro` / `<RL>.ri`——后面所有模块按后缀分类。
- 同一个 `generate_wiring` 产出两种语义：`kind="truth"`（随机 L，默认 40–200 ns）与
  `kind="pending"`（全 0 的待测载体）。两份文件结构逐行同构，`q = truth + pending` 逐线可加。
- 构型参数（chain/ring/ladder/chains/grid + extra_edges）决定耦合图——它直接决定
  后面"延后组求解"的图是否连通（见 §6 的多链陷阱）。

### [validate.py](../qxtrl_timing_cali/validate.py)

校验规则即规格。重点看它**守什么**：三文件线路集合一致、connections 与结构双向一致、
`delay ≥ 0` 且为有限数。它对坏输入的纪律是"记错误、不崩溃"（`_items` 收集坏条目）。

---

## 4. 第 2 层：审查端 review/（约 1800 行，一次测量的完整生命周期）

推荐阅读顺序 = 一次结果在系统里的旅程：

```
session.new_request ──▶ (queue_io 交给执行方) ──▶ executor 返回 ExperimentResult
       │                                                    │
       │                                            policies.analyze
       │                                                    │
       │                                            策略链 → Verdict
       │                                                    │
       └──────── session 簿记 ◀── writeback.commit_shifts（四道守卫）◀── center._execute
```

| 顺序 | 模块 | 读什么 | 关键不变量 |
| --- | --- | --- | --- |
| 1 | [session.py](../qxtrl_timing_cali/review/session.py) | 逐线 `d` 簿记、`plan_align` 的符号分支、`headroom` 台账 | `apply_plan`：δ≥0；收回量 ≤ 授予余额 |
| 2 | [policies.py](../qxtrl_timing_cali/review/policies.py) | `quality_problems`（测量期就用的判据）与 `analyze`（阈值无关事实）的分工；策略链顺序 | 阈值全在 `Thresholds`，策略只比较数字 |
| 3 | [graph.py](../qxtrl_timing_cali/review/graph.py) | `candidate_pairs` 怎么从芯片结构推导可测边、四种代价、Kruskal | 候选边方向约定（如 a=readout, b=qubit.xy）与 stages 消费端配套 |
| 4 | [solver.py](../qxtrl_timing_cali/review/solver.py) | 加权图拉普拉斯 + 共轭梯度；锚 = 当前最右线 ⇒ δ 天然 ≥ 0 | `solve()` 只吃**单个连通分量**；分量零点不可比 |
| 5 | [writeback.py](../qxtrl_timing_cali/review/writeback.py) | `commit_shifts` 的四道守卫顺序、CAS 复核、原子替换 | 任一守卫不过 → 文件一个字节不动、审计留痕 |
| 6 | [center.py](../qxtrl_timing_cali/review/center.py) | 四项技能 `request_align / collect / review / _execute` | commit → 守卫 → 会话推进 → 指纹刷新 → 设备下发，顺序不能乱 |
| 7 | [ledger.py](../qxtrl_timing_cali/review/ledger.py) | `measure` 的重试循环、journal 逐边追加、`preload` 的 config_hash 恢复判据 | 测量期间世界静止：不改文件、不动 d、不下发设备 |
| 8 | [queue_io.py](../qxtrl_timing_cali/review/queue_io.py) | `DirectExecutor` vs `FileQueue`；半截文件的宽限窗口 | 契约不合格 → rejected/ 留档，永不阻塞流程 |
| 9 | [parallel.py](../qxtrl_timing_cali/review/parallel.py) | 唯一冲突判据（共享线名）、贪心装箱 | 波只是提交提示；求解/写回/下发永远串行 |

**读 solver 前建议手算一遍**：三条线 A/B/C，观测 A−B=−24、C−B=−13，
按 `min Σ w(o_a−o_b−s)²` 解出相对位置，再取最右线为锚算 δ——这 15 分钟
会让你后面看所有"为什么全局解"的论证都变成显然。

### 从网格演示理解选边与分波

两种算法解决不同问题，按顺序阅读：

1. `graph.minimum_spanning_forest` 使用 **Kruskal**，按 `(cost, a, b)` 排序，
   只加入连接不同分量的边；目标是连通所需的最少测量与较低代价。
   当前耦合器组有 16 条 `Q.z` 与 24 条 `CP.z`，共 40 个节点，选出 39 条边：
   15 条比特 Z-Z 与 24 条耦合器 Z-Z。物理耦合器全部保留，测量树并不覆盖所有物理比特间边。
2. `parallel.plan_waves` 同样按 `(cost, a, b)` 排序；每条边放入**当前最空的可兼容波**，
   没有可兼容波才新建波。唯一冲突条件是共享线名，算法不保证任意图上的最少波数。
3. `ParallelPlan.bundles()` 再按 `bundle_size` 切束。当前每束最多 4 条，
   这限制提交粒度，不决定波数，也不保证执行方实际并发。

当前耦合器组分为 **7 波：6、7、7、6、7、3、3 条**。`Q10.z` 参与 7 条测量，
这些测量不能同波，所以当前测量树至少需要 7 波；实际划分达到该下界。
选边还没有联合优化波数，不能据此断言其他测量树也必须 7 波。

全程 63 条测量、10 个冲突波、18 个提交束。`63 / 18 = 3.5` 仅在实验等时、
束内完全并发且忽略开销时成立，是理论比值；模拟后端没有测量真实提速。

---

## 5. 第 3 层：编排器（约 1550 行）

### [sim.py](../qxtrl_timing_cali/sim.py) — 先读，否则看不懂测试

`SimChip`（q = L + d 的设备替身）与 `SimExecutor`（参考执行方）。注意
`seed_applied_shifts`：**新进程的设备必须按 pending 当前的 d 还原**，
否则测到的是另一个世界——这是分阶段跨进程驱动的前提。

模拟后端由 `SimChip / SimExecutor` 提供；真实后端应按 `ExecutorAdapter` 契约完成
实验测量与设备状态同步，通过 `DirectExecutor` 或 `FileQueue` 接入。
实现结果返回、位移下发与恢复核对后，可以复用现有选边、策略、求解和守卫流程开展真实校准；
还需按设备经验配置 `Thresholds`，不能直接把当前占位阈值当作真机验收标准。
接口细节先读 [executor-integration.md](executor-integration.md)，再对照模拟后端实现。

### [pipeline.py](../qxtrl_timing_cali/pipeline.py) — 两阶段、不分组（先读这个）

阶段 A（`measure`：选树、测量、替补重连，零副作用）→ 阶段 B（`finish`：全局求解、
整批一次写回）。注意 `solve()` 里的连通性过滤：只解最大分量，其余进隔离区。

### [stages.py](../qxtrl_timing_cali/stages.py) — 四组六步（全仓库最复杂，按这个顺序攻）

1. `plan_stages` —— 每组的节点/最少测量边/波划分从候选边推导
2. `run` —— 六步一次跑完的主干：`measure`（逐组 `run_stage`）→ `finish`（步 5）→ 步 6
3. `run_stage` —— 单组执行：测量 → 即时组求解落盘 → headroom 授予 → 写 `stage_done` 日志
4. `finish` —— 延后组合并全局求解 + 连通性过滤 + 整批写回 + `final_solve` 事件入日志
5. `_solve_readout_final` / `_pull_back_headroom` —— 步 6 的收尾解与 headroom 收回
6. `stage_status` / `run_group` / `run_final` / `replay_headroom_grants` —— 分阶段驱动四件套

三个最容易懵的点，提前说破：

- **headroom 台账是"整账重算"的**：`replay_headroom_grants` 从日志按事件顺序重放
  授予（`stage_done.headroom`）与收回（`stage_done.headroom_return`），同一份日志
  永远重放出同一份台账——所以跨进程、重复调用都安全。
- **顶层报告的 `solve` 是步 5 视角**（qubit/coupler 全局解），步 6 的收尾解在
  `report["final_solve"]` 与 `stages[-1]["solve"]`。动画数据管线依赖这个约定。
- **dry-run 也下发设备**（`_apply` 的注释）：组 2/3 要站在组 1 的结果上测，
  设备停在旧状态的话后面测到的是另一个世界。

---

## 6. 常见陷阱（都对应真实事故或测试）

| 陷阱 | 一句话解释 | 钉住它的测试 |
| --- | --- | --- |
| `SimChip(truth)` 直接构造不复跑还原 | 设备状态 ≠ 文件状态，测出另一个世界 | `test_stages.py::TestSingleStageDriving` |
| 写回后复用旧观测 | 写回一旦发生，旧观测全部作废（CAS 纪律） | `test_resume.py` 全文件 |
| NaN 穿透判据 | NaN 与任何数比较都是 False，阈值/守卫全失明——契约入口就拒收 | `test_hardening.py::TestNaNRejection` |
| 多链芯片无桥接边 | 延后图不连通，各分量零点不可比 → 只解最大分量，其余进隔离区 | `test_hardening.py::TestDisconnectedFinalSolve` |
| 半截结果文件 | 执行方非原子写 → poll 宽限数秒再收，不急着归档 rejected | `test_hardening.py::TestFileQueueRobustness` |
| 数据集日期漂移 | `generate_chip` 缺省写"今天"——演示数据必须钉 `day=` | `test_docs_parallel.py` 再生测试 |
| 把冲突波当作实际并发或耗时 | 每波可再切束；动画段宽按边数，不代表实验耗时 | `test_docs_parallel.py` 提交束与计数测试 |
| 动画的秒与毫秒混用 | 并行场景用秒，`requestAnimationFrame` 用毫秒；漏换算会瞬间播放完 | `test_docs_playback.py` 时钟与回放测试 |

以及一条元规则：**改任何影响流水线数值行为的代码后，重新生成两份演示数据**
（`python docs/gen_animation_data.py --chip-day <旧日期>` → `gen_animation.py`；
`gen_parallel_data.py` → `gen_parallel_animation.py`），
再生测试会拿新代码逐字段对账——它们是这个仓库"数据不是编的"承诺的执行者。
当前普通演示日期为 `2026-09-23`；并行演示在 `gen_parallel_data.CHIP` 中固定为 `2026-09-24`。
修改架构图生成器后运行 `python docs/gen_architecture.py`；修改动画模板后重新生成对应 HTML。
演示拓扑或数据变化后，同步学习路径、README 与简报中的说明和计数。

---

## 7. 测试当可执行规格用

| 文件 | 它在钉什么 |
| --- | --- |
| [test_stages.py](../tests/test_stages.py) | 四组的顺序/依赖/写回方式；**分次驱动 ≡ 一次跑完** |
| [test_review.py](../tests/test_review.py) | 契约、会话簿记、四道守卫、六种决议、复跑不叠加 |
| [test_resume.py](../tests/test_resume.py) | 断点续跑：按 config_hash 恢复观测、headroom 授予随日志重放 |
| [test_pipeline.py](../tests/test_pipeline.py) | 两阶段批量：隔离区、替补重连、坏边重试 |
| [test_parallel.py](../tests/test_parallel.py) | 波划分不变量：同波无共享线名、覆盖完整 |
| [test_hardening.py](../tests/test_hardening.py) | 守卫与状态机加固：NaN / headroom 台账 / 连通性 / 坏输入健壮性 |
| [test_docs_animation.py](../tests/test_docs_animation.py) / [test_docs_parallel.py](../tests/test_docs_parallel.py) | 演示数据可由真跑流水线逐字段复现；动画几何自检 |
| [test_docs_playback.py](../tests/test_docs_playback.py) | 执行实际动画 JavaScript：时间单位、暂停恢复、分栏边界、点击波次与本波结束暂停；有 Node.js 时运行，项目运行不依赖 Node.js |
| [test_docs_architecture.py](../tests/test_docs_architecture.py) | 连线整段穿框、标签遮挡、连接端口与入库架构图再生一致性 |

改完代码跑 `python -m unittest discover -s tests`（测试数量与耗时以当前运行输出为准）。

---

## 8. 一周节奏建议

| 天 | 内容 | 产出 |
| --- | --- | --- |
| 1 | §1 全局观（README + notebook + 两个动画） | 能讲出四组六步的物理理由 |
| 2 | §3 数据模型 + 静态工具；自己生成一个 grid 芯片并可视化 | 能手写一个新构型的芯片 JSON |
| 3 | §4 前半：session → policies → graph → solver（手算一遍最小二乘） | 能解释 δ 为什么天然 ≥ 0 |
| 4 | §4 后半：writeback → center → ledger → queue_io → parallel | 能画出一次 commit 的完整时序 |
| 5 | §5：sim → pipeline（跑通 CLI，对照报告字段） | 能指出 isolated / waves / resumed 各自含义 |
| 6 | §5：stages 全文 + 分阶段驱动三入口 + headroom 闭环 | 能解释"分次驱动 ≡ 一次跑完"靠什么成立 |
| 7 | §7 测试通读 + 挑一个测试改造（比如加一种构型） | 提交第一个 PR |

之后接真实设备时，[executor-integration.md](executor-integration.md) 是操作手册：
入口函数、结果契约、设备同步协议、常见坑。

/* QXtrlTimingCali 架构设计汇报 PPT 生成器
 * 用法：node docs/gen_pptx.js  → docs/QXtrlTimingCali-架构设计.pptx
 * 视觉母题：分层色带（与 docs/gen_architecture.py 同源层色）+ 卡片左侧色条。
 */
const path = require("path");
const { execSync } = require("child_process");
// 优先本地 node_modules；缺失则回退到全局（npm install -g pptxgenjs），项目无需自带 node_modules。
const pptxgen = (() => {
  try {
    return require("pptxgenjs");
  } catch (e) {
    const gRoot = execSync("npm root -g", { encoding: "utf8" }).toString().trim();
    return require(path.join(gRoot, "pptxgenjs"));
  }
})();

const C = {
  primary: "1F4E79", teal: "2E8B7A", amber: "C8862A", ink: "243340",
  muted: "6B7B8C", line: "D8E0E8", bg: "F4F7FA", card: "FFFFFF",
  bandGen: "2C6CB0", bandFile: "A9760A", bandCali: "1D7A3C",
  bandExec: "6B3FA0", bandArt: "4A5560",
};
const FONT = "Microsoft YaHei", MONO = "Consolas";
const W = 10, H = 5.625, MX = 0.5, CW = W - MX * 2, TITLE_Y = 0.34, BODY_Y = 1.12, FOOT_Y = 5.22;

const pptx = new pptxgen();
pptx.defineLayout({ name: "W16x9", width: W, height: H });
pptx.layout = "W16x9";
pptx.author = "QXtrlTimingCali";
pptx.title = "QXtrlTimingCali 架构设计说明";

function slideBase(title, kicker, n) {
  const s = pptx.addSlide();
  s.background = { color: C.bg };
  s.addShape(pptx.ShapeType.rect, { x: 0, y: 0, w: W, h: 0.055, fill: { color: C.primary } });
  s.addText(title, { x: MX, y: TITLE_Y, w: CW - 1.4, h: 0.5, fontFace: FONT, fontSize: 24,
    bold: true, color: C.primary, align: "left", valign: "middle", margin: 0 });
  if (kicker) s.addText(kicker, { x: MX, y: TITLE_Y + 0.47, w: CW - 1.4, h: 0.28,
    fontFace: FONT, fontSize: 11, color: C.muted, align: "left", valign: "middle", margin: 0 });
  s.addShape(pptx.ShapeType.line, { x: MX, y: FOOT_Y, w: CW, h: 0, line: { color: C.line, width: 1 } });
  s.addText("QXtrlTimingCali · 超导量子比特 Timing 校准系统", { x: MX, y: FOOT_Y + 0.04,
    w: 5.2, h: 0.3, fontFace: FONT, fontSize: 8.5, color: C.muted, valign: "middle", margin: 0 });
  s.addText(String(n).padStart(2, "0"), { x: W - MX - 0.9, y: FOOT_Y + 0.04, w: 0.9, h: 0.3,
    fontFace: MONO, fontSize: 10, bold: true, color: C.primary, align: "right",
    valign: "middle", margin: 0 });
  return s;
}

function card(s, x, y, w, h, accent) {
  s.addShape(pptx.ShapeType.roundRect, { x, y, w, h, rectRadius: 0.05,
    fill: { color: C.card }, line: { color: C.line, width: 1 },
    shadow: { type: "outer", color: "9AAAB8", blur: 6, offset: 1, angle: 90, opacity: 0.15 } });
  if (accent) s.addShape(pptx.ShapeType.rect, { x: x + 0.04, y: y + 0.09, w: 0.05,
    h: h - 0.18, fill: { color: accent } });
}

function ct(s, x, y, w, text, accent, size) {
  s.addText(text, { x: x + 0.18, y, w: w - 0.3, h: 0.3, fontFace: FONT,
    fontSize: size || 12.5, bold: true, color: accent || C.primary, valign: "middle", margin: 0 });
}

function bl(s, x, y, w, h, items, size) {
  const runs = [];
  items.forEach((it) => {
    if (typeof it === "string") {
      runs.push({ text: "▪  " + it, options: { color: C.ink, fontSize: size || 10.5, breakLine: true } });
    } else {
      runs.push({ text: "▪  " + it.h, options: { bold: true, color: C.primary, fontSize: size || 10.5, breakLine: false } });
      runs.push({ text: it.t, options: { color: C.ink, fontSize: size || 10.5, breakLine: true } });
    }
  });
  s.addText(runs, { x, y, w, h, fontFace: FONT, align: "left", valign: "top",
    lineSpacingMultiple: 1.3, paraSpaceAfter: 5, margin: 0 });
}

function chip(s, x, y, w, h, text, color, solid) {
  s.addShape(pptx.ShapeType.roundRect, { x, y, w, h, rectRadius: 0.035,
    fill: { color: solid ? color : "FFFFFF" }, line: { color, width: 1.1 } });
  s.addText(text, { x: x + 0.04, y, w: w - 0.08, h, fontFace: FONT, fontSize: 8.8,
    bold: true, color: solid ? "FFFFFF" : color, align: "center", valign: "middle", margin: 0 });
}

function arrow(s, x, y, w, color) {
  s.addShape(pptx.ShapeType.rightArrow, { x, y, w, h: 0.15, fill: { color }, line: { color, width: 0.5 } });
}

/* ===== P1 封面 ===== */
(function () {
  const s = pptx.addSlide();
  s.background = { color: C.primary };
  for (let i = 0; i < 7; i++) {
    const cx = 6.3 + ((i % 3) * 0.45);
    s.addShape(pptx.ShapeType.rect, { x: cx, y: 1.0 + i * 0.42,
      w: Math.min(2.0 + ((i * 137) % 26) / 10, 9.85 - cx), h: 0.085,
      fill: { color: i % 2 ? "3C7BB0" : "2E8B7A" }, line: { width: 0 } });
  }
  s.addShape(pptx.ShapeType.rect, { x: 0, y: 0, w: 0.12, h: H, fill: { color: C.teal } });
  s.addText("QXtrlTimingCali", { x: 0.75, y: 1.45, w: 6.2, h: 0.9, fontFace: FONT,
    fontSize: 44, bold: true, color: "FFFFFF", valign: "middle", margin: 0, charSpacing: 1 });
  s.addText("超导量子比特 Timing 校准系统", { x: 0.78, y: 2.42, w: 6.2, h: 0.5,
    fontFace: FONT, fontSize: 20, color: "BFD6E8", valign: "middle", margin: 0 });
  s.addShape(pptx.ShapeType.line, { x: 0.8, y: 3.05, w: 2.3, h: 0, line: { color: C.amber, width: 2.5 } });
  s.addText("整体框架设计说明 · 项目汇报 / 团队培训", { x: 0.78, y: 3.2, w: 6.2, h: 0.4,
    fontFace: FONT, fontSize: 13.5, color: "E8EEF4", valign: "middle", margin: 0 });
  s.addText([
    { text: "Phase-2 · 批量校准 · v0.4.0", options: { fontSize: 11, bold: true, color: "FFFFFF", breakLine: true } },
    { text: "四组六步 · 并行波 · Python 标准库 · 零第三方依赖", options: { fontSize: 10, color: "BFD6E8", breakLine: true } },
    { text: "数据主线  q = L(truth) + d(pending)", options: { fontSize: 10, color: "BFD6E8", breakLine: true } },
    { text: "2026-09 · 122 项测试通过", options: { fontSize: 10, color: "8FB0CC" } },
  ], { x: 0.78, y: 3.75, w: 6, h: 1.3, fontFace: FONT, valign: "top", margin: 0 });
  s.addNotes("开场：今天讲这套系统的整体框架设计。目标是让没接触过代码的同事也能理解——它做什么、分几层、数据怎么流、安全怎么保证。全篇零第三方依赖，纯 Python 标准库实现。");
})();

/* ===== P2 导读 ===== */
(function () {
  const s = slideBase("这份说明讲什么", "六个部分，从「为什么」讲到「怎么保证安全」", 2);
  const items = [
    ["01", "要解决的问题", "为什么多条信号线的时间必须对齐", C.bandGen],
    ["02", "技术选型与分层", "四层结构，各层职责与边界", C.bandFile],
    ["03", "模块清单", "每个模块干什么、属于哪一层", C.bandCali],
    ["04", "组件交互与数据流", "五种消息契约，端到端数据走向", C.bandExec],
    ["05", "四组六步校准流程", "核心业务流程：分组测量、统一求解、并行波", C.amber],
    ["06", "安全、接入与路线图", "四道守卫、审计留痕、QXtrl 接入、后期能力", C.bandArt],
  ];
  const cw = (CW - 0.3) / 3, ch = 1.6;
  items.forEach((it, i) => {
    const x = MX + (i % 3) * (cw + 0.15), y = BODY_Y + 0.18 + Math.floor(i / 3) * (ch + 0.2);
    card(s, x, y, cw, ch, it[3]);
    s.addText(it[0], { x: x + 0.2, y: y + 0.12, w: 1, h: 0.4, fontFace: MONO,
      fontSize: 19, bold: true, color: it[3], margin: 0 });
    s.addText(it[1], { x: x + 0.2, y: y + 0.58, w: cw - 0.35, h: 0.35, fontFace: FONT,
      fontSize: 12.5, bold: true, color: C.ink, valign: "middle", margin: 0 });
    s.addText(it[2], { x: x + 0.2, y: y + 0.94, w: cw - 0.35, h: 0.5, fontFace: FONT,
      fontSize: 9.5, color: C.muted, valign: "top", lineSpacingMultiple: 1.2, margin: 0 });
  });
  s.addNotes("导览页。如果只记一件事：整套设计围绕'把多条信号线对齐到同一时间轴'这个唯一目标，其余都是为了这件事的正确性与可追溯性。");
})();

/* ===== P3 问题与价值 ===== */
(function () {
  const s = slideBase("要解决什么问题", "对非技术同事：想象一个多车道站台，火车必须同时到站", 3);
  card(s, MX, BODY_Y, 4.3, 2.45, C.bandGen);
  ct(s, MX, BODY_Y + 0.08, 4.3, "问题：信号线到达时间不齐", C.bandGen);
  ["读取线 RO", "比特 XY", "比特 Z", "耦合器 Z"].forEach((lb, i) => {
    const y = BODY_Y + 0.5 + i * 0.4;
    s.addText(lb, { x: MX + 0.2, y, w: 1.05, h: 0.3, fontFace: FONT, fontSize: 9,
      color: C.muted, valign: "middle", margin: 0 });
    s.addShape(pptx.ShapeType.rect, { x: MX + 1.35, y: y + 0.08, w: 2.7, h: 0.028,
      fill: { color: C.line }, line: { width: 0 } });
    s.addShape(pptx.ShapeType.rect, { x: MX + 1.35 + [0, 0.5, 0.85, 1.2][i], y, w: 0.85,
      h: 0.18, rectRadius: 0.02, fill: { color: C.bandGen }, line: { width: 0 } });
  });
  s.addText("到达时刻参差不齐 → 门操作失真", { x: MX + 0.2, y: BODY_Y + 2.1, w: 3.9,
    h: 0.28, fontFace: FONT, fontSize: 10, bold: true, color: C.amber, valign: "middle", margin: 0 });

  card(s, MX + 4.5, BODY_Y, 4.5, 2.45, C.amber);
  ct(s, MX + 4.5, BODY_Y + 0.08, 4.5, "校准目标：对齐到同一时间轴", C.amber);
  s.addShape(pptx.ShapeType.roundRect, { x: MX + 4.7, y: BODY_Y + 0.5, w: 4.1, h: 0.55,
    rectRadius: 0.04, fill: { color: "EFF5FA" }, line: { color: C.primary, width: 1 } });
  s.addText([
    { text: "q = L + d", options: { fontSize: 16, bold: true, color: C.primary, fontFace: MONO } },
    { text: "  实际 = 固有延时 + 可调右移", options: { fontSize: 9, color: C.muted, fontFace: FONT } },
  ], { x: MX + 4.8, y: BODY_Y + 0.5, w: 3.9, h: 0.55, align: "center", valign: "middle", margin: 0 });
  bl(s, MX + 4.75, BODY_Y + 1.2, 4.0, 1.1, [
    { h: "L（固有延时）：", t: "线缆/器件自带，只读不改" },
    { h: "d（可调右移）：", t: "校准只把线往右挪，只增不减" },
    { h: "评价标准：", t: "看「残差」够不够小，不看 d 大小" },
  ], 10);

  card(s, MX, BODY_Y + 2.62, CW, 1.15, C.teal);
  ct(s, MX, BODY_Y + 2.7, CW, "为什么值得做：误差不再沿链路累加", C.teal);
  [["33067.5 ns", "旧流程累计位移", "测一对写一对 · 31 步"],
   ["2860.1 ns", "批量统一位移", "约 1/11.6"],
   ["0.254 ns", "最终残差", "8 比特 31 线 · 已对齐"]].forEach((st, i) => {
    const x = MX + 0.3 + i * 3.0;
    s.addText(st[0], { x, y: BODY_Y + 3.05, w: 1.7, h: 0.42, fontFace: MONO, fontSize: 18,
      bold: true, color: C.amber, valign: "middle", margin: 0 });
    s.addText([
      { text: st[1], options: { fontSize: 9.5, bold: true, color: C.ink, breakLine: true } },
      { text: st[2], options: { fontSize: 8.5, color: C.muted } },
    ], { x: x + 1.72, y: BODY_Y + 3.03, w: 1.2, h: 0.45, fontFace: FONT, valign: "middle", margin: 0 });
    if (i < 2) s.addShape(pptx.ShapeType.line, { x: x + 2.88, y: BODY_Y + 3.08, w: 0,
      h: 0.4, line: { color: C.line, width: 1 } });
  });
  s.addNotes("非技术页。比喻：多车道站台的火车必须同时到站，否则量子门操作会失真。校准就是把每条线'往右挪一点'让它们对齐。左图红条越参差=问题越严重。右边公式 q=L+d 是全篇的数据主线。下方三个数字是实测值（8 比特 31 线示例）：旧方式挪了 3.3 万纳秒，批量方式只要 2860 纳秒，因为它最后一次性求解，残差 0.254 纳秒。");
})();

/* ===== P4 技术选型与工程原则 ===== */
(function () {
  const s = slideBase("技术选型与工程原则", "零第三方依赖，数据交换全走 JSON", 4);
  card(s, MX, BODY_Y, 4.35, 2.0, C.bandFile);
  ct(s, MX, BODY_Y + 0.08, 4.35, "技术栈", C.bandFile);
  bl(s, MX + 0.2, BODY_Y + 0.5, 4.0, 1.4, [
    { h: "语言：", t: "Python 标准库，零第三方依赖" },
    { h: "数据交换：", t: "JSON（可 diff、可审计、跨语言）" },
    { h: "可视化：", t: "自包含 HTML / SVG，离线可开" },
    { h: "文档产物：", t: "架构图 / 动画 / 本 PPT 均由脚本生成" },
  ], 10);

  card(s, MX + 4.65, BODY_Y, 4.35, 2.0, C.bandCali);
  ct(s, MX + 4.65, BODY_Y + 0.08, 4.35, "工程原则", C.bandCali);
  bl(s, MX + 4.85, BODY_Y + 0.5, 4.0, 1.4, [
    { h: "生成端纯函数：", t: "同输入必同输出，无副作用" },
    { h: "校准端有状态：", t: "阈值、策略、会话归它持有" },
    { h: "执行端可替换：", t: "换实验软件只换适配器" },
    { h: "先查后写：", t: "计划是纯函数，检查通过才落盘" },
  ], 10);

  card(s, MX, BODY_Y + 2.18, CW, 1.5, C.primary);
  ct(s, MX, BODY_Y + 2.28, CW, "三道边界：模块之间靠「契约」而不是靠约定", C.primary);
  const bnd = [
    ["① JSON 契约", "生成端 → 校准端", "chip.json / wiring 文件即接口"],
    ["② 消息契约", "校准端 ⇄ 执行端", "五种消息，全走 JSON 往返"],
    ["③ 单一写入者", "延时字段归属", "只有审查中心能写延时"],
  ];
  bnd.forEach((b, i) => {
    const x = MX + 0.25 + i * 2.95;
    s.addText([
      { text: b[0], options: { fontSize: 11.5, bold: true, color: C.amber, breakLine: true } },
      { text: b[1], options: { fontSize: 9.5, bold: true, color: C.ink, breakLine: true } },
      { text: b[2], options: { fontSize: 9, color: C.muted } },
    ], { x, y: BODY_Y + 2.66, w: 2.7, h: 0.85, fontFace: FONT, valign: "top", margin: 0 });
    if (i < 2) s.addShape(pptx.ShapeType.line, { x: x + 2.8, y: BODY_Y + 2.7, w: 0,
      h: 0.75, line: { color: C.line, width: 1 } });
  });
  s.addNotes("技术选型很克制：纯 Python 标准库、零第三方依赖，意味着没有依赖冲突、装上就能跑。数据交换全用 JSON，好处是可 diff、可审计、跨语言。三条边界是重点——模块之间靠契约而非口头约定，尤其'延时只有一个写入者'这条，是避免系统互踩的关键。");
})();

/* ===== P5 架构分层总览 ===== */
(function () {
  const s = slideBase("架构分层：四层结构", "自上而下：生成 → 存储 → 校准 → 执行，每层职责单一", 5);
  const bands = [
    [C.bandGen, "① 生成端", "纯函数", ["chip_gen 芯片", "wiring_gen 延时", "validate 校验", "visualize 排错"]],
    [C.bandFile, "② 配置真源", "JSON 文件", ["chip.json", "wiring-pending", "wiring-truth", "runs/ 队列"]],
    [C.bandCali, "③ 校准端", "本仓库核心 · 有状态", ["stages / pipeline", "center 审查中心", "solver 求解器", "writeback 守卫回写"]],
    [C.bandExec, "④ 执行端", "可整体替换", ["queue_io 适配器", "sim 设备替身", "FileQueue 队列", "QXtrl（规划）"]],
  ];
  const bh = 0.72, gap = 0.13;
  bands.forEach((b, i) => {
    const y = BODY_Y + 0.05 + i * (bh + gap);
    s.addShape(pptx.ShapeType.roundRect, { x: MX, y, w: CW, h: bh, rectRadius: 0.04,
      fill: { color: b[0] }, line: { color: b[0], width: 1 } });
    s.addText([
      { text: b[1], options: { fontSize: 13, bold: true, color: "FFFFFF", breakLine: true } },
      { text: b[2], options: { fontSize: 8.5, color: "F0F4F8" } },
    ], { x: MX + 0.2, y, w: 2.3, h: bh, fontFace: FONT, valign: "middle", margin: 0 });
    b[3].forEach((m, j) => {
      chip(s, MX + 2.75 + j * 1.58, y + 0.17, 1.45, 0.38, m, b[0]);
    });
    if (i < 3) arrow(s, W / 2 - 0.3, y + bh + 0.005, 0.6, C.muted);
  });
  s.addText("①② 之间是 JSON 契约 · ③④ 之间是五种消息契约 —— 换执行端不改校准逻辑",
    { x: MX, y: BODY_Y + 4 * (bh + gap) + 0.05, w: CW, h: 0.3, fontFace: FONT, fontSize: 10,
      bold: true, color: C.primary, align: "center", valign: "middle", margin: 0 });
  s.addNotes("架构核心页。四层从上到下：生成端只负责造数据（纯函数）；配置真源是 JSON 文件；校准审查端是本仓库核心，有状态、做裁决，里面有两条编排主线（stages 四组六步、pipeline 两阶段不分组）；执行端真正跑实验、可整体替换。每层之间靠契约解耦，所以换实验软件只需要换适配器，不用改校准逻辑。");
})();

/* ===== P6 模块清单 ===== */
(function () {
  const s = slideBase("主要模块清单", "按层归位：每个模块属于哪一层、负责什么", 6);
  const rows = [
    [{ text: "层", options: { bold: true, color: "FFFFFF" } },
     { text: "模块", options: { bold: true, color: "FFFFFF" } },
     { text: "职责", options: { bold: true, color: "FFFFFF" } },
     { text: "关键特性", options: { bold: true, color: "FFFFFF" } }],
    ["① 生成端", "chip_gen", "生成芯片构型（链/环/梯/网格）", "纯函数，无副作用"],
    ["① 生成端", "wiring_gen", "生成真值 L 与待测 d 延时文件", "truth 随机 / pending 全 0"],
    ["① 生成端", "validate / visualize", "结构校验、自包含 HTML 排错", "三文件交叉核对"],
    ["② 真源", "chip.json / wiring-*", "配置与延时的唯一真源", "JSON，可 diff 审计"],
    ["③ 校准端", "stages.py", "四组六步分组编排（核心流程）", "三次落盘 · headroom 闭环"],
    ["③ 校准端", "pipeline.py", "两阶段批量校准：只测量 → 全局求解 → 整批写回", "N−1 次测量，一次写盘"],
    ["③ 校准端", "review/center", "申请→取结果→审查→回写", "持有阈值与策略链"],
    ["③ 校准端", "review/solver", "加权最小二乘全局求解", "按 1/σ² 分摊误差"],
    ["③ 校准端", "review/graph · parallel", "最小生成树选边 · 按共享线名切「波」", "测量次数最少 · 可并行提交"],
    ["③ 校准端", "review/ledger · writeback", "测量台账（只记录不写回）+ 四道守卫原子回写", "断点续跑 · 拒绝也留痕"],
    ["④ 执行端", "queue_io / sim", "适配器与设备替身（含 submit_many 批量提交）", "可整体替换"],
  ];
  s.addTable(rows, {
    x: MX, y: BODY_Y, w: CW, colW: [1.15, 1.85, 3.4, 2.6], rowH: 0.32,
    fontFace: FONT, fontSize: 9.5, color: C.ink, valign: "middle", align: "left",
    border: { type: "solid", color: C.line, pt: 0.75 },
    fill: { color: "FFFFFF" }, margin: 0.06,
  });
  s.addText("按层归位 · ③ 校准端为本仓库核心（stages 四组六步 / pipeline 两阶段，两条编排主线共用同一套契约、守卫与求解器）",
    { x: MX, y: BODY_Y + 3.85, w: CW, h: 0.22, fontFace: FONT, fontSize: 9,
      color: C.muted, align: "center", valign: "middle", margin: 0 });
  s.addNotes("模块清单，按层归位。可以让听众顺着表格看每个模块属于哪一层。重点是③校准端：stages 管四组六步的分组编排，pipeline 是另一条不分组的两阶段主线，center 管审查裁决，solver 管全局求解，graph/parallel 管选边与并行波，ledger/writeback 管台账与安全回写。");
})();

/* ===== P7 组件交互：五种消息契约 ===== */
(function () {
  const s = slideBase("组件之间怎么对话", "唯一跨边界接口是五种消息，全部 JSON 往返", 7);
  const msgs = [
    ["ExperimentRequest", "① 申请", "我要测哪条边\n带申请时的 config_hash", C.bandExec],
    ["ExperimentResult", "② 结果", "实测 skew / sigma\n原样回填 config_hash", C.bandExec],
    ["Assessment", "③ 分析", "出事实与质量判据\n不下最终裁决", C.bandCali],
    ["Verdict", "④ 裁决", "commit / noop / retry\nrecheck / replan / escalate", C.bandCali],
    ["WritePlan", "⑤ 回写", "批准后写哪条线\n经四道守卫落盘", C.amber],
  ];
  const cw = 1.66, gap = 0.19, y = BODY_Y + 0.25;
  msgs.forEach((m, i) => {
    const x = MX + i * (cw + gap);
    card(s, x, y, cw, 1.75, m[3]);
    s.addText(m[1], { x: x + 0.1, y: y + 0.12, w: cw - 0.2, h: 0.28, fontFace: FONT,
      fontSize: 12, bold: true, color: m[3], align: "center", valign: "middle", margin: 0 });
    s.addText(m[0], { x: x + 0.06, y: y + 0.44, w: cw - 0.12, h: 0.5, fontFace: MONO,
      fontSize: 8.5, bold: true, color: C.ink, align: "center", valign: "middle", margin: 0 });
    s.addShape(pptx.ShapeType.line, { x: x + 0.2, y: y + 0.97, w: cw - 0.4, h: 0,
      line: { color: C.line, width: 1 } });
    s.addText(m[2], { x: x + 0.08, y: y + 1.02, w: cw - 0.16, h: 0.65, fontFace: FONT,
      fontSize: 8.5, color: C.muted, align: "center", valign: "top",
      lineSpacingMultiple: 1.15, margin: 0 });
    if (i < 4) arrow(s, x + cw + 0.015, y + 0.8, gap - 0.03, C.muted);
  });
  card(s, MX, BODY_Y + 2.25, CW, 1.4, C.primary);
  ct(s, MX, BODY_Y + 2.35, CW, "对话规则（三条铁律）", C.primary);
  bl(s, MX + 0.2, BODY_Y + 2.75, CW - 0.4, 0.85, [
    { h: "hash 必须原样回填：", t: "Request 里的 config_hash 要在 Result 里原样带回，否则整批观测作废、流程中止（常见接入坑①）" },
    { h: "分析与裁决分离：", t: "analyze 只出事实，decide 才下判断——所以换阈值不用改测量代码" },
    { h: "换执行端不改裁决：", t: "审查端与执行端之间只有这五种消息，换 QXtrl 只换适配器" },
  ], 10);
  s.addNotes("组件交互页。五种消息是系统之间唯一的对话方式：申请→结果→分析→裁决→回写。三条铁律要重点强调，尤其第一条 hash 回填——这是接入真实实验时最容易犯、后果最严重的错误（会丢掉整批数据）。");
})();

/* ===== P8 数据流向 ===== */
(function () {
  const s = slideBase("数据怎么流动", "端到端：从造数据到写回留痕，一条主线贯穿", 8);
  const steps = [
    ["生成", "chip_gen / wiring_gen\n造出芯片与延时文件", C.bandGen],
    ["校验", "validate 三文件\n交叉核对", C.bandGen],
    ["测量", "执行端按「波」跑实验\n产出 skew / sigma", C.bandExec],
    ["裁决", "策略链检查\n好坏与超限", C.bandCali],
    ["求解", "加权最小二乘\n一次算出全部位移", C.bandCali],
    ["写回", "四道守卫\n组1 / 步5 / 步6 三次落盘", C.amber],
  ];
  const cw = 1.32, gap = 0.19, y = BODY_Y + 0.3;
  steps.forEach((st, i) => {
    const x = MX + i * (cw + gap);
    s.addShape(pptx.ShapeType.roundRect, { x, y, w: cw, h: 1.15, rectRadius: 0.04,
      fill: { color: st[2] }, line: { color: st[2], width: 1 } });
    s.addText(st[0], { x, y: y + 0.1, w: cw, h: 0.3, fontFace: FONT, fontSize: 12,
      bold: true, color: "FFFFFF", align: "center", valign: "middle", margin: 0 });
    s.addText(st[1], { x: x + 0.05, y: y + 0.42, w: cw - 0.1, h: 0.65, fontFace: FONT,
      fontSize: 8, color: "F0F4F8", align: "center", valign: "top",
      lineSpacingMultiple: 1.15, margin: 0 });
    if (i < 5) arrow(s, x + cw + 0.02, y + 0.5, gap - 0.04, "FFFFFF");
  });
  card(s, MX, BODY_Y + 1.68, CW, 2.0, C.bandFile);
  ct(s, MX, BODY_Y + 1.78, CW, "贯穿全程的唯一数据主线：q = L + d", C.bandFile);
  const lanes = [
    ["L（固有延时）", "wiring-truth", "只读：只用来装载设备、事后对账，校准流程不读它", C.muted],
    ["d（可调右移）", "wiring-pending", "可写：校准累加的位移，只增不减，是唯一被写回的字段", C.amber],
    ["q（实际位置）", "q = L + d", "计算值：残差 = max(q) − min(q)，越小说明越对齐", C.primary],
  ];
  lanes.forEach((l, i) => {
    const y = BODY_Y + 2.18 + i * 0.46;
    s.addText(l[0], { x: MX + 0.2, y, w: 1.55, h: 0.4, fontFace: MONO, fontSize: 10.5,
      bold: true, color: l[3], valign: "middle", margin: 0 });
    s.addText(l[1], { x: MX + 1.8, y, w: 1.6, h: 0.4, fontFace: MONO, fontSize: 9,
      color: C.ink, valign: "middle", margin: 0 });
    s.addText(l[2], { x: MX + 3.5, y, w: 5.3, h: 0.4, fontFace: FONT, fontSize: 9.5,
      color: C.muted, valign: "middle", margin: 0 });
    if (i < 2) s.addShape(pptx.ShapeType.line, { x: MX + 0.2, y: y + 0.42, w: CW - 0.4,
      h: 0, line: { color: C.line, width: 1 } });
  });
  s.addNotes("数据流页。上方六个步骤是端到端走向；下方是贯穿全程的数据主线。务必讲清三个量的区别：L 是天生的固有延时、只读；d 是校准加的右移量、只增不减、是唯一被写回的；q 是两者之和。我们评价校准好不好，看 q 的离散程度（残差），而不是看 d 有多大。");
})();

/* ===== P9 六步校准流程 ===== */
(function () {
  const s = slideBase("核心流程：四组六步校准", "四组六步 · 三次落盘——误差不再沿链路累加的根因", 9);
  const steps = [
    ["步 1 · 组1", "读取线对齐", "ro ↔ ri 对准", "立刻写回", true],
    ["步 2 · 组1", "授予 headroom", "读取线整体右移留余量", "立刻写回", true],
    ["步 3 · 组2", "单比特校准", "xy ↔ z", "只记录", false],
    ["步 4 · 组3", "跨比特 / 耦合器", "Qa.z↔Qb.z · CP.z↔Q.z", "只记录", false],
    ["步 5", "统一求解", "组 2+3 观测合并求解", "整批写回", true],
    ["步 6 · 组4", "读取线最终对齐", "收回 headroom · 挂 qubit.xy", "立刻写回", true],
  ];
  const cw = 1.4, gap = 0.13, y = BODY_Y + 0.15;
  steps.forEach((st, i) => {
    const x = MX + i * (cw + gap);
    const col = st[4] ? C.amber : C.bandCali;
    s.addShape(pptx.ShapeType.roundRect, { x, y, w: cw, h: 1.9, rectRadius: 0.04,
      fill: { color: C.card }, line: { color: col, width: 1.3 },
      shadow: { type: "outer", color: "9AAAB8", blur: 5, offset: 1, angle: 90, opacity: 0.15 } });
    s.addShape(pptx.ShapeType.rect, { x, y, w: cw, h: 0.34, fill: { color: col }, line: { width: 0 } });
    s.addText(st[0], { x, y: y + 0.02, w: cw, h: 0.3, fontFace: MONO, fontSize: 11.5,
      bold: true, color: "FFFFFF", align: "center", valign: "middle", margin: 0 });
    s.addText(st[1], { x: x + 0.06, y: y + 0.42, w: cw - 0.12, h: 0.5, fontFace: FONT,
      fontSize: 10, bold: true, color: C.ink, align: "center", valign: "middle",
      lineSpacingMultiple: 1.1, margin: 0 });
    s.addText(st[2], { x: x + 0.06, y: y + 0.94, w: cw - 0.12, h: 0.5, fontFace: FONT,
      fontSize: 8.5, color: C.muted, align: "center", valign: "top",
      lineSpacingMultiple: 1.1, margin: 0 });
    s.addShape(pptx.ShapeType.roundRect, { x: x + 0.14, y: y + 1.48, w: cw - 0.28, h: 0.32,
      rectRadius: 0.03, fill: { color: st[4] ? "FBF0DD" : "E9F3EC" }, line: { width: 0 } });
    s.addText(st[3], { x: x + 0.14, y: y + 1.48, w: cw - 0.28, h: 0.32, fontFace: FONT,
      fontSize: 8.5, bold: true, color: col, align: "center", valign: "middle", margin: 0 });
    if (i < 5) arrow(s, x + cw + 0.01, y + 0.85, gap - 0.02, C.muted);
  });
  card(s, MX, BODY_Y + 2.2, CW, 1.45, C.primary);
  ct(s, MX, BODY_Y + 2.3, CW, "为什么这样排顺序", C.primary);
  bl(s, MX + 0.2, BODY_Y + 2.7, CW - 0.4, 0.95, [
    { h: "读取线必须先对齐：", t: "否则读出的信号本身就带着时间偏差，后面的测量全不准" },
    { h: "步 3、步 4 之间一个字节都不写：", t: "两组 skew 描述同一时刻，步 5 才能合并成一次求解" },
    { h: "全程只落盘三次：", t: "组 1 就地写 → 步 5 整批写 → 步 6 收回 headroom 后写" },
    { h: "同组可切「波」并行提交：", t: "按共享线名切波，只影响观测到达顺序，不改变「一次求解 + 一次落盘」的语义" },
  ], 9.5);
  s.addNotes("核心流程页，全篇重点。四组六步的顺序不是随意排的，是物理依赖决定的。要强调步3步4之间'一个字节都不写'——因为只有在同一个基线上测的数据才能合并求解，这是误差不累加的关键。橙色是写回步骤（三次落盘），绿色是只记录步骤。步 2 授予的 headroom 是流水线自己的临时余量，步 6 按额度收回。");
})();

/* ===== P10 分组测量 vs 逐对写回 ===== */
(function () {
  const s = slideBase("为什么不能「测一对写一对」", "同样是校准，两种做法的差距是数量级", 10);
  const rows = [
    [{ text: "对比项", options: { bold: true, color: "FFFFFF", fill: { color: C.primary } } },
     { text: "旧流程：逐对测量逐对写回", options: { bold: true, color: "FFFFFF", fill: { color: C.muted } } },
     { text: "本方案：分组测量统一求解", options: { bold: true, color: "FFFFFF", fill: { color: C.bandCali } } }],
    ["测量次数", "31 次（逐对，8 比特 31 线）", "30 次（最小生成树 N−1）"],
    ["测量时机", "测一对立刻写回", "分组测量，数据先攒着"],
    ["误差累积", "每步误差沿链路累加", "误差按 1/σ² 摊到全图"],
    ["总位移", "33067.5 ns", "2860.1 ns（约 1/11.6）"],
    ["求解次数", "每对一次（≈N 次）", "3 次（组1 + 步5 + 步6）"],
    ["写盘次数", "N−1 次（每次都有风险）", "3 次（组1 / 步5 整批 / 步6）"],
    ["残差量级", "随台阶数不断增长", "只由测量精度 σ 决定（实测 0.254 ns）"],
  ];
  s.addTable(rows, {
    x: MX, y: BODY_Y, w: CW, colW: [1.7, 3.65, 3.65], rowH: 0.36,
    fontFace: FONT, fontSize: 10.5, color: C.ink, valign: "middle", align: "left",
    border: { type: "solid", color: C.line, pt: 0.75 }, margin: 0.08,
  });
  card(s, MX, BODY_Y + 3.0, CW, 0.75, C.amber);
  s.addText([
    { text: "一句话：", options: { fontSize: 11, bold: true, color: C.amber } },
    { text: "把「多次小误差累加」换成「一次全局摊分」——测量 30 次、位移 2860.1 ns（旧流程的 1/11.6）、残差 0.254 ns，结果反而更稳。",
      options: { fontSize: 10.5, bold: true, color: C.ink } },
  ], { x: MX + 0.2, y: BODY_Y + 3.0, w: CW - 0.4, h: 0.75, fontFace: FONT,
    valign: "middle", margin: 0 });
  s.addNotes("对比页，面向所有人。核心结论是最后一行：残差不再随台阶数增长，只由测量精度决定。可以用爬楼梯比喻：旧方式每上一级台阶都要重新校准一次、误差累加；新方式是一次算好所有台阶的高度。33067 对 2860 这个对比最直观——注意测量次数也少了（最小生成树只需 N−1 条边）。");
})();

/* ===== P11 四道守卫 ===== */
(function () {
  const s = slideBase("安全机制：四道守卫", "任一道不过，文件原样不动，但一定留痕", 11);
  const gs = [
    ["① CAS 指纹", "CAS＝比较并交换", "申请时记下的指纹\n必须等于当前指纹\n（变了则整批观测作废）", C.bandGen],
    ["② 只准右移", "单调约束", "delta ≥ 0\n延时只能往右加\n（headroom 收回除外）", C.bandCali],
    ["③ 预算上限", "总量控制", "d ≤ d_max\n总位移不超预算\n（任一线超限→整批不写）", C.bandFile],
    ["④ 原子写入", "落盘安全", "os.replace + .bak\n+ JSONL 审计\n（中断不留半成品）", C.amber],
  ];
  const cw = 2.1, gap = 0.19, y = BODY_Y + 0.15;
  gs.forEach((g, i) => {
    const x = MX + i * (cw + gap);
    card(s, x, y, cw, 2.0, g[3]);
    s.addText(g[0], { x: x + 0.1, y: y + 0.15, w: cw - 0.2, h: 0.32, fontFace: FONT,
      fontSize: 12, bold: true, color: g[3], align: "center", valign: "middle", margin: 0 });
    s.addText(g[1], { x: x + 0.1, y: y + 0.5, w: cw - 0.2, h: 0.26, fontFace: FONT,
      fontSize: 9, color: C.muted, align: "center", valign: "middle", margin: 0 });
    s.addShape(pptx.ShapeType.line, { x: x + 0.2, y: y + 0.82, w: cw - 0.4, h: 0,
      line: { color: C.line, width: 1 } });
    s.addText(g[2], { x: x + 0.1, y: y + 0.9, w: cw - 0.2, h: 1.0, fontFace: FONT,
      fontSize: 9, color: C.ink, align: "center", valign: "top",
      lineSpacingMultiple: 1.2, margin: 0 });
  });
  card(s, MX, BODY_Y + 2.3, CW, 1.35, C.primary);
  ct(s, MX, BODY_Y + 2.4, CW, "两条兜底设计", C.primary);
  bl(s, MX + 0.2, BODY_Y + 2.78, CW - 0.4, 0.8, [
    { h: "先查后写：", t: "计划是纯函数，先算出来给策略链检查，通过才落盘——所以失败分支不会留下半成品" },
    { h: "拒绝也留痕：", t: "四道守卫任一道拒绝，文件一个字节不改，但审计里记下「谁、为什么、被挡在哪一道」" },
  ], 10);
  s.addNotes("安全机制页。四道守卫是数据安全的核心，可以类比银行转账的多重验证。关键是'拒绝也留痕'——即使没写进去，也会记录被挡在哪一道、原因是什么，便于事后追溯。");
})();

/* ===== P12 产物与审计 ===== */
(function () {
  const s = slideBase("产物与审计留痕", "每一次操作都可回放、可追溯", 12);
  const arts = [
    ["pipeline-report.json", "校准报告", "测量集 / 波划分 / 孤立节点\n求解诊断 · 残差与真值对账", C.bandCali],
    ["writeback-audit.jsonl", "写盘审计", "新旧指纹 · 逐线明细\n拒绝也留痕", C.amber],
    ["measurements-\naudit.jsonl", "测量日志", "逐边追加 · 含 config_hash\n断点续跑依据（pending 伴生）", C.bandExec],
    ["session.json", "会话快照", "逐线 d + 指纹快照\n编排器与会话共读", C.bandFile],
  ];
  const cw = 2.1, gap = 0.19, y = BODY_Y + 0.15;
  arts.forEach((a, i) => {
    const x = MX + i * (cw + gap);
    card(s, x, y, cw, 2.0, a[3]);
    s.addText(a[0], { x: x + 0.1, y: y + 0.15, w: cw - 0.2, h: 0.5, fontFace: MONO,
      fontSize: 9.5, bold: true, color: a[3], align: "center", valign: "middle",
      lineSpacingMultiple: 1.1, margin: 0 });
    s.addText(a[1], { x: x + 0.1, y: y + 0.68, w: cw - 0.2, h: 0.28, fontFace: FONT,
      fontSize: 10.5, bold: true, color: C.ink, align: "center", valign: "middle", margin: 0 });
    s.addShape(pptx.ShapeType.line, { x: x + 0.2, y: y + 1.0, w: cw - 0.4, h: 0,
      line: { color: C.line, width: 1 } });
    s.addText(a[2], { x: x + 0.1, y: y + 1.08, w: cw - 0.2, h: 0.85, fontFace: FONT,
      fontSize: 9, color: C.muted, align: "center", valign: "top",
      lineSpacingMultiple: 1.2, margin: 0 });
  });
  card(s, MX, BODY_Y + 2.3, CW, 1.35, C.bandArt);
  ct(s, MX, BODY_Y + 2.4, CW, "可回放：四类问题都能答", C.bandArt);
  bl(s, MX + 0.2, BODY_Y + 2.78, CW - 0.4, 0.85, [
    { h: "出问题时：", t: "这条线是哪一次测的、写盘前后的值是什么" },
    { h: "要复现时：", t: "断点续跑——按边跳过已完成的，只补测剩余" },
    { h: "要审计时：", t: "谁在何时做了什么决定，拒绝也留痕" },
    { h: "要对外讲时：", t: "架构图 / 动画 / 本 PPT 均由脚本生成，代码一改重跑即同步" },
  ], 9.5);
  s.addNotes("产物页。四类文件各有分工：报告讲结果，审计讲动作，测量日志讲断点续跑，会话讲状态。共同点是都可回放。特别提一下断点续跑——跑一半断了，重启后能跳过已完成的继续，不用整个重来；headroom 授予台账也随事件重放，所以换进程继续跑不会错。");
})();

/* ===== P13 接入边界 ===== */
(function () {
  const s = slideBase("怎么接真实实验设备", "执行端可整体替换：换适配器，不改校准逻辑", 13);
  card(s, MX, BODY_Y, 4.3, 2.45, C.bandExec);
  ct(s, MX, BODY_Y + 0.08, 4.3, "接入方式（三选一）+ 教程", C.bandExec);
  bl(s, MX + 0.2, BODY_Y + 0.5, 3.95, 1.85, [
    { h: "同进程：", t: "DirectExecutor，最省事" },
    { h: "跨进程：", t: "FileQueue 文件队列解耦" },
    { h: "接 QXtrl：", t: "实现它的 submit / poll / apply（+ submit_many 批量提交）" },
    { h: "完整教程：", t: "见 docs/executor-integration.md（入口函数 / 结果契约 / 设备同步 / 常见坑）" },
  ], 9.5);
  s.addText("线名映射（Q0.xy ↔ 板卡通道）归执行侧，不进 pending 文件",
    { x: MX + 0.2, y: BODY_Y + 2.0, w: 3.9, h: 0.35, fontFace: FONT, fontSize: 9,
      bold: true, color: C.amber, valign: "middle", lineSpacingMultiple: 1.1, margin: 0 });

  card(s, MX + 4.5, BODY_Y, 4.5, 2.45, C.primary);
  ct(s, MX + 4.5, BODY_Y + 0.08, 4.5, "QXtrl 侧字段对照", C.primary);
  const map = [
    ["config_hash", "RecipeResult.config_hash", "语义一致，可直接沿用"],
    ["quality / flags", "QualityResult + 拟合 r²", "阈值口径待标定"],
    ["sigma_ns", "RecipeCell.shots", "按 repeats 估标准误"],
    ["skew_ns（主判据）", "（QXtrl 侧暂无）", "真正缺口，需新增"],
  ];
  map.forEach((m, i) => {
    const y = BODY_Y + 0.55 + i * 0.44;
    s.addText(m[0], { x: MX + 4.65, y, w: 1.5, h: 0.4, fontFace: MONO, fontSize: 8.5,
      bold: true, color: C.primary, valign: "middle", margin: 0 });
    s.addText(m[1], { x: MX + 6.2, y, w: 1.5, h: 0.4, fontFace: FONT, fontSize: 8.5,
      color: C.ink, valign: "middle", margin: 0 });
    s.addText(m[2], { x: MX + 7.75, y, w: 1.15, h: 0.4, fontFace: FONT, fontSize: 8,
      color: C.muted, valign: "middle", lineSpacingMultiple: 1.1, margin: 0 });
    if (i < 3) s.addShape(pptx.ShapeType.line, { x: MX + 4.65, y: y + 0.42, w: 4.2,
      h: 0, line: { color: C.line, width: 1 } });
  });

  card(s, MX, BODY_Y + 2.62, CW, 1.05, C.amber);
  ct(s, MX, BODY_Y + 2.7, CW, "三个必须提前约定的风险点", C.amber);
  bl(s, MX + 0.2, BODY_Y + 3.05, CW - 0.4, 0.62, [
    { h: "延时写入归属：", t: "QXtrl 也会写延时文件——必须明确只有审查中心能写，否则两处互踩" },
    { h: "设备独占：", t: "hardware_hold 一次只许一个实验占用设备，编排的「波」实际可能降级为排队" },
    { h: "poll 要轮询到齐：", t: "一波结果必须循环 poll 到齐或超时，只调一次会漏结果；σ 相关性由执行侧用 flags 否决" },
  ], 9.5);
  s.addNotes("接入边界页。强调执行端是可整体替换的，换设备只换适配器。右边字段对照表里，skew_ns 是真正的缺口——QXtrl 目前没有现成的相对延时测量实验，这是接入时最需要新开发的部分。下方两个风险点要在项目启动时就和对方约定清楚。");
})();

/* ===== P14 路线图 ===== */
(function () {
  const s = slideBase("后期能力与路线图", "契约、会话、中心、回写四件套不动，只演进编排", 14);
  const road = [
    ["已落地", "Phase-2", ["四组六步分组校准", "分组测量、统一求解", "并行波划分 + submit_many", "分阶段驱动：一次只推一步", "四道守卫 + 审计 + 断点续跑"], C.bandCali],
    ["近期启动", "执行端接入", ["QXtrl 适配 submit / poll / apply", "σ 与 quality 阈值口径标定", "相对延时测量实验", "hardware_hold 独占锁对接"], C.bandExec],
    ["规划中", "智能编排", ["按 σ 自适应加权选边", "坏边隔离区自动续跑", "多轮收敛与收敛判据", "子图并行测量"], C.muted],
  ];
  const cw = 2.9, gap = 0.15, y = BODY_Y + 0.2;
  road.forEach((r, i) => {
    const x = MX + i * (cw + gap);
    card(s, x, y, cw, 2.75, r[3]);
    s.addShape(pptx.ShapeType.roundRect, { x: x + 0.15, y: y + 0.15, w: 1.0, h: 0.3,
      rectRadius: 0.03, fill: { color: r[3] }, line: { width: 0 } });
    s.addText(r[0], { x: x + 0.15, y: y + 0.15, w: 1.0, h: 0.3, fontFace: FONT,
      fontSize: 9, bold: true, color: "FFFFFF", align: "center", valign: "middle", margin: 0 });
    s.addText(r[1], { x: x + 0.15, y: y + 0.52, w: cw - 0.3, h: 0.35, fontFace: FONT,
      fontSize: 13, bold: true, color: r[3], valign: "middle", margin: 0 });
    s.addShape(pptx.ShapeType.line, { x: x + 0.15, y: y + 0.95, w: cw - 0.3, h: 0,
      line: { color: C.line, width: 1 } });
    bl(s, x + 0.15, y + 1.05, cw - 0.3, 1.55, r[2], 9.5);
  });
  card(s, MX, BODY_Y + 3.1, CW, 0.6, C.primary);
  s.addText([
    { text: "演进原则：", options: { fontSize: 11, bold: true, color: C.primary } },
    { text: "契约 / 会话 / 中心 / 回写四件套保持稳定，编排层自由演进——所以升级不影响已有数据与裁决逻辑。",
      options: { fontSize: 10.5, color: C.ink } },
  ], { x: MX + 0.2, y: BODY_Y + 3.1, w: CW - 0.4, h: 0.6, fontFace: FONT, valign: "middle", margin: 0 });
  s.addNotes("路线图页。分三个阶段讲。已落地那一栏是本次更新的重点：四组六步、并行波划分与 submit_many、以及供上层调度器使用的分阶段驱动（stage_status / run_group / run_final，每次只推进一步、绝不自动往后跑）。重点是演进原则：底层四件套（契约、会话、中心、回写）保持稳定，只有编排层在演进。这样升级换代不会动到已有的数据和裁决逻辑，保护既有投入。");
})();

/* ===== P15 总结 ===== */
(function () {
  const s = slideBase("总结：三句话记住这套设计", "架构分层 · 数据主线 · 安全边界", 15);
  const pts = [
    ["四层解耦，靠契约对话", "生成 → 真源 → 校准 → 执行，层间靠契约（JSON 文件 + 五种消息）解耦；换设备只换适配器。", C.bandGen],
    ["一条数据主线，只增不减", "q = L + d：L 只读不改，d 只往右加；评价看残差，不看位移大小。", C.bandFile],
    ["四组六步，统一求解", "组 2、3 不写盘保持同基线，末尾统一求解——位移 2860.1 ns（旧流程 1/11.6），残差 0.254 ns。", C.bandCali],
  ];
  pts.forEach((p, i) => {
    const y = BODY_Y + 0.15 + i * 1.05;
    card(s, MX, y, CW, 0.92, p[2]);
    s.addText(String(i + 1), { x: MX + 0.2, y: y + 0.16, w: 0.6, h: 0.6, fontFace: MONO,
      fontSize: 26, bold: true, color: p[2], align: "center", valign: "middle", margin: 0 });
    s.addText(p[0], { x: MX + 0.95, y: y + 0.1, w: CW - 1.2, h: 0.36, fontFace: FONT,
      fontSize: 14, bold: true, color: C.ink, valign: "middle", margin: 0 });
    s.addText(p[1], { x: MX + 0.95, y: y + 0.46, w: CW - 1.2, h: 0.4, fontFace: FONT,
      fontSize: 10.5, color: C.muted, valign: "middle", margin: 0 });
  });
  s.addText("Q & A", { x: MX, y: BODY_Y + 3.35, w: CW, h: 0.55, fontFace: FONT,
    fontSize: 22, bold: true, color: C.primary, align: "center", valign: "middle",
    charSpacing: 3, margin: 0 });
  s.addNotes("总结页，三句话收尾：四层解耦靠契约、一条数据主线只增不减、四组六步统一求解。数字取自 8 比特 31 线示例（31 条线 / 30 次测量 / 位移 2860.1 ns / 残差 0.254 ns）。讲完可以进入问答。");
})();

pptx.writeFile({ fileName: "docs/QXtrlTimingCali-架构设计.pptx" })
  .then((f) => console.log("OK → " + f))
  .catch((e) => { console.error(e); process.exit(1); });

import React from "react";
import { I } from "./icons.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { wsKey, WsWorks } from "./ws-works.jsx";
import { onRovingTabKeyDown } from "./a11y-tabs.js";
import { WsChapterPlanPanel } from "./ws-snow-chapters.jsx";
import { apiGet, apiPost } from "./lib/client.js";
import { navigateWithViewIntent, setViewIntentTargetReady } from "./ws-view-intents.js";
import { useUndoToast, UndoToast } from "./ws-undo-toast.jsx";
import { dayTimeLabel } from "./lib/ago.js";

/* global React, I */
/* ==========================================================
   WsSnowflake — 构思 · 雪花十步法 (Snowflake Method workbench)

   Built faithfully on Randy Ingermanson's Snowflake Method:
   · the FRACTAL principle — start with one sentence, expand it
     level by level (1句→5句→1页→4页); revising early is cheap,
     so backtracking is encouraged.
   · the STORY SPINE — three escalating disasters + a Moral
     Premise that flips false→true at the midpoint (always shown
     in the right rail).
   · two interwoven TRACKS — plot and character expand in turns.
   · STRUCTURED SCAFFOLDS for the steps where the method's shape
     matters: the 25-word logline meter, the 5-sentence / 3-
     disaster paragraph, the character summary sheet (goal /
     ambition / values / conflict / epiphany), and the scene plan
     (Proactive G-C-S vs Reactive R-D-D).

   Layout: left step list (always visible) · center canvas with
   tabs (编辑/候选/历史/引用) · right live context. Context folds
   into a slide-over drawer on narrow screens.
   ========================================================== */
const { useState: useSS, useEffect: useSE, useRef: useSR } = React;

const S2_STEPS = [
  { key: "audience",  num: "01", name: "读者定位",   blurb: "为谁写、读者期待哪种快感",     state: "done",   essential: true,  track: "orient",    book: "前置 · 定方向",  grow: "锚定目标读者", timebox: "30 分钟" },
  { key: "logline",   num: "02", name: "一句话概括", blurb: "全书核心冲突压成一行",         state: "done",   essential: true,  track: "plot",      book: "第 1 步",        grow: "1 句 · ≤25 词", timebox: "1 小时", from: "读者定位", fromKey: "audience" },
  { key: "paragraph", num: "03", name: "一段话概括", blurb: "五句 = 三幕骨架 + 三大灾难",   state: "done",   essential: true,  track: "plot",      book: "第 2 步",        grow: "1 句 → 5 句",   timebox: "1 小时", from: "一句话概括", fromKey: "logline" },
  { key: "characters",num: "04", name: "角色摘要表", blurb: "目标·抱负·价值观·阻碍·顿悟",   state: "done",   essential: false, track: "character", book: "第 3 步",        grow: "每人 1 张表",   timebox: "每人 1 小时", from: "一段话概括", fromKey: "paragraph" },
  { key: "synopsis",  num: "05", name: "一页梗概",   blurb: "把每一句扩成一段",            state: "done",   essential: false, track: "plot",      book: "第 4 步",        grow: "5 句 → 1 页",   timebox: "1 小时", from: "一段话概括", fromKey: "paragraph", alsoFrom: ["characters"] },
  { key: "backstory", num: "06", name: "角色背景",   blurb: "每个主要角色的来路与伤",       state: "active", essential: false, track: "character", book: "第 5 步",        grow: "每人半页",     timebox: "每人 1 小时", from: "角色摘要表", fromKey: "characters" },
  { key: "outline",   num: "07", name: "长篇大纲",   blurb: "把一页扩成四页",              state: "done",   essential: false, track: "plot",      book: "第 6 步",        grow: "1 页 → 4 页",   timebox: "2 小时", from: "一页梗概", fromKey: "synopsis" },
  { key: "profile",   num: "08", name: "角色全档案", blurb: "生理·心理·环境·性格全维度",    state: "warn",   essential: false, track: "character", book: "第 7 步",        grow: "每人完整档案", timebox: "每人数小时", from: "角色背景", fromKey: "backstory" },
  { key: "scenes",    num: "09", name: "场景列表",   blurb: "一行一场 · 每场都要有冲突",    state: "done",   essential: true,  track: "plot",      book: "第 8 步",        grow: "全书拆成场",   timebox: "几天", from: "长篇大纲", fromKey: "outline", alsoFrom: ["characters"] },
  { key: "planning",  num: "10", name: "场景规划",   blurb: "主动 GCS / 反应 RDD",         state: "done",   essential: true,  track: "plot",      book: "第 9 步",        grow: "每场 5 分钟",   timebox: "每场 5 分钟", from: "场景列表", fromKey: "scenes" },
];

const TRACK_LABEL = { plot: "情节", character: "角色", orient: "定位" };

/* The story's structural spine — three disasters + the moral premise
   that flips at the midpoint. Shown in the right rail on every step. */
const S2_DISASTERS = [
  { id: "灾一", act: "第一幕末", tone: "crimson" },
  { id: "灾二", act: "第二幕中点", tone: "gold" },
  { id: "灾三", act: "第二幕末", tone: "crimson" },
];

/* Universal quality ruler — five dimensions every step's output is judged
   against (distilled from the Snowflake philosophy). The five dimensions
   stay constant across the whole method; a step may sharpen a question. */
const S2_RUBRIC = [
  { k: "分形一致", q: "展开后回头压缩——上一层的概括是否仍然成立？" },
  { k: "因果锁链", q: "每个事件是否锁死下一个走向——更难、更贵、更不可逆？" },
  { k: "角色驱动", q: "是角色的选择在推动情节，还是你在替角色做决定？" },
  { k: "可落场景", q: "拆到最小单元时，每一场都有人想要什么、有什么挡着？" },
  { k: "读者契约", q: "你承诺给读者的那种快感，这一层是否还在兑现？" },
];

/* ---------- 本步诊断引擎 ----------
   把右栏三块（写作指引 / 验收门 / 质量标尺）接到同一套可解释信号上：
   读当前草稿 → 抽取结构信号 → 实时评分 + 机器核验。与控制塔同源，只是放大到单步。 */

// 折叠草稿 / 脚手架为一段纯文本，供信号抽取
function s2Content(draft, scaffold) {
  let text = (draft || "").trim();
  if (!text && scaffold) {
    const out = [];
    const walk = (v) => {
      if (typeof v === "string") out.push(v);
      else if (Array.isArray(v)) v.forEach(walk);
      else if (v && typeof v === "object") Object.values(v).forEach(walk);
    };
    walk(scaffold);
    text = out.join("\n");
  }
  return text;
}

// 从文本抽取结构信号（确定性、可解释）
function s2Signals(text, target) {
  const t = text || "";
  const len = t.replace(/\s+/g, "").length;
  const cov = target ? Math.min(1, len / target) : Math.min(1, len / 80);
  const n = (re) => (t.match(re) || []).length;
  return {
    len, cov,
    causal:    n(/(因为|所以|于是|因此|导致|逼|不得不|只能|被迫|从而|使得|不可逆|代价)/g),
    skeleton:  n(/(灾难?[一二三123]|第[一二三]幕|铺垫|中点|高潮|结局|收束)/g),
    character: n(/(目标|抱负|价值观|动机|想要|渴望|害怕|恐惧|顿悟|信念|挣扎)/g),
    scene:     n(/(场景|地点|S0?\d|POV|视角|GCS|RDD|冲突|进入|离开|证据)/g),
    promise:   n(/(读者|快感|期待|承诺|悬念|揪心|代入|共情|反转)/g),
  };
}

// 五维实时评分：floor + 覆盖度 + 专属标记，clamp 0–100，并附可解释依据
function s2ScoreDims(sig) {
  const cl = (x) => Math.max(0, Math.min(100, Math.round(x)));
  const cap = (k, per, c) => Math.min(c, k * per);
  return {
    "分形一致": { score: cl(50 + 32 * sig.cov + cap(sig.skeleton, 4, 14)), why: `覆盖 ${Math.round(sig.cov * 100)}% · 骨架标记 ${sig.skeleton}` },
    "因果锁链": { score: cl(42 + 16 * sig.cov + cap(sig.causal, 7, 42)), why: `因果词 ${sig.causal} 处` },
    "角色驱动": { score: cl(44 + 16 * sig.cov + cap(sig.character, 7, 40)), why: `动机/价值标记 ${sig.character} 处` },
    "可落场景": { score: cl(40 + 18 * sig.cov + cap(sig.scene, 6, 42)), why: `场景标记 ${sig.scene} 处` },
    "读者契约": { score: cl(48 + 30 * sig.cov + cap(sig.promise, 7, 22)), why: `读者快感标记 ${sig.promise} 处` },
  };
}

// 机器核验项：确定性断言，随草稿实时判定（验收门的「自动」半区）
function s2AutoChecks(sig, target) {
  return [
    { t: "篇幅达标", pass: sig.len >= Math.round((target || 80) * 0.6), val: `${sig.len} / ${target || "—"} 字`, need: `≥ ${Math.round((target || 80) * 0.6)} 字` },
    { t: "结构标记落位", pass: (sig.skeleton + sig.scene + sig.character) >= 2, val: `${sig.skeleton + sig.scene + sig.character} 个标记`, need: "≥ 2 个" },
    { t: "因果链成形", pass: sig.causal >= 1, val: `${sig.causal} 处因果`, need: "≥ 1 处" },
  ];
}

// 09 场景列表专属机器核验：直接读织线 / 节奏结构（与画布上的诊断同源）
function s2SceneAuto(scaffold) {
  const list = (scaffold && scaffold.list) || [];
  const lines = (scaffold && scaffold.lines) || [];
  const pacing = s2PacingRuns(list);
  const stats = s2LineStats(list, lines);
  const noCru = list.filter(s => !(s.crucible || "").trim()).length;
  const tightMax = pacing.tight.length ? Math.max(...pacing.tight.map(r => r.len)) : 0;
  const clustered = stats.filter(s => s.clustered).length;
  const noRefract = stats.filter(s => s.noRefract).length;
  const subUsed = stats.filter(s => s.kind !== "main" && s.count > 0).length;
  return [
    { t: "场场有冲突", pass: list.length > 0 && noCru === 0, val: noCru ? `${noCru} 场缺坩埚` : `${list.length} 场齐`, need: "0 场缺冲突" },
    { t: "节奏无紧绷段", pass: tightMax < 3, val: tightMax >= 3 ? `连续 ${tightMax} 场主动` : "主动 / 反应交替", need: "无连续 ≥3 主动" },
    { t: "支线织入并折射主题", pass: subUsed > 0 && noRefract === 0, val: noRefract ? `${noRefract} 条缺折射` : (subUsed ? `${subUsed} 条支线织入` : "尚无支线"), need: "每条线写折射" },
    { t: "支线穿插不扎堆", pass: clustered === 0, val: clustered ? `${clustered} 条扎堆` : "分布均匀", need: "无扎堆" },
  ];
}

/* ---- 10 场景规划：逐场覆盖与链条核验 ---- */
function s2PlanSlots(plan) { return plan && plan.mode === "reactive" ? ["reaction", "dilemma", "decision"] : ["goal", "conflict", "setback"]; }
// 0 = 未规划 · 1 = 填了一半 · 2 = 三槽齐
function s2PlanState(plan) {
  if (!plan) return 0;
  const slots = s2PlanSlots(plan);
  const n = slots.filter(f => (plan[f] || "").trim()).length;
  return n === slots.length ? 2 : n ? 1 : 0;
}
function s2PlanAuto(scaffold, scenesScaffold) {
  const list = (scenesScaffold && scenesScaffold.list) || [];
  const plans = (scaffold && scaffold.plans) || {};
  const total = list.length;
  const fully = list.filter(s => s2PlanState(plans[s.id]) === 2).length;
  const partial = list.filter(s => s2PlanState(plans[s.id]) === 1).length;
  let seamBad = 0; // 已规划的场，它的上一场却还空着 → 「挫败→反应 / 决定→目标」的链条断在那里
  list.forEach((s, i) => { if (i > 0 && s2PlanState(plans[s.id]) > 0 && s2PlanState(plans[list[i - 1].id]) === 0) seamBad++; });
  return [
    { t: "逐场覆盖", pass: total > 0 && fully + partial === total, val: total ? `${fully + partial}/${total} 场已规划` : "09 还没有场景", need: "每场一份" },
    { t: "三槽填满", pass: total > 0 && fully === total, val: partial ? `${partial} 场只填了一半` : `${fully}/${total} 场三槽齐` , need: "GCS / RDD 三槽齐" },
    { t: "链条衔接", pass: seamBad === 0, val: seamBad ? `${seamBad} 处断链` : "挫败→反应 顺接", need: "上一场也已规划" },
  ];
}


// 分形管线：本步在雪花展开链上的位置（上游 → 本步×倍率 → 下游）
function s2Pipeline(stepKey) {
  const step = S2_STEPS.find(s => s.key === stepKey);
  if (!step) return null;
  const downs = S2_STEPS.filter(s => s.fromKey === stepKey);
  return {
    inName: step.from || "雪花原点", inKey: step.fromKey || null,
    ratio: step.grow,
    outName: downs.length ? downs.map(d => d.name).join(" / ") : "正文初稿",
    outKey: downs.length ? downs[0].key : null,
  };
}

function s2HC(s) { return s >= 80 ? "var(--sage)" : s >= 62 ? "var(--gold)" : "var(--crimson)"; }

/* 09 场景行的 id 就是上行到后端的 row_uid —— 场景计划的不可变身份锚，必须全局不重号。
   旧写法 `"S" + (list.length + 1)` 只看当前长度：删掉中间一场再新增，铸出的号会撞上
   仍然存活的那一场，后端按 row_uid 对位时后者整段覆盖前者的内容（构思侧丢戏）。
   规则改成「已用过的最大编号 + 1」，并兜底跳过任何仍被占用的号。 */
function s2NextSceneRowId(list) {
  const rows = Array.isArray(list) ? list : [];
  const used = new Set(rows.map(row => String((row && row.id) || "")));
  let next = rows.reduce((max, row) => {
    const n = parseInt(String((row && row.id) || "").replace(/^S/, ""), 10);
    return Number.isFinite(n) && n > max ? n : max;
  }, 0) + 1;
  while (used.has("S" + String(next).padStart(2, "0"))) next++;
  return "S" + String(next).padStart(2, "0");
}


/* Per-step content: draft / target / explainer / hints / candidates,
   plus a `scaffold` spec for the four method-critical steps. */
const S2_STEP_DATA = {
  audience: {
    target: 200,
    scaffold: { type: "audience" },
    guide: {
      task: "雪花从定锚开始：你为谁写？她要哪种快感？后面九步每一次展开、每一条取舍，都拿这把尺子量。",
      writing: [
        { k: "类型即承诺", v: "先定类型——文学悬疑、言情、硬推理……类型决定了读者带着什么期待打开书" },
        { k: "一句话快感", v: "用「她读完会觉得 ___」一句话锁定核心快感" },
        { k: "敢于排除", v: "反向定位比正向更有力——写下「我不为谁写」，砍掉犹豫" },
      ],
      checklist: [
        "能一句话说出读者要的核心快感。",
        "明确写下了「不写什么 / 不为谁写」。",
        "后面九步的每一个取舍，都能拿这条来裁决。",
      ],
      note: "Ingermanson：你只有一种读者——你的目标读者。取悦她，忘掉其他人。",
    },
  },
  logline: {
    target: 60,
    meter: { target: 42, note: "原书建议 ≤ 25 个英文词；中文约 40 字内最易记忆，也最像一句宣传语。" },
    guide: {
      task: "雪花的种子：把整部小说压缩成一句话。这是分形的原点——后面所有层都从它长出来。也是你最强的营销工具：让人听完就想说「告诉我更多」。",
      writing: [
        { k: "因果句公式", v: "「一位[有特点的角色]必须[达成目标]，但[核心障碍挡着她]。」——主角、目标、阻力、代价，一句话装下" },
        { k: "悬念不泄底", v: "暗示有赌注，但把结局和最大反转藏住" },
        { k: "能脱口而出", v: "短到你能在电梯里说完——中文 ≤ 40 字" },
      ],
      checklist: [
        "一句因果句里保留了主角、目标、阻力和代价。",
        "结局和反转都藏住了。",
        "大声念一遍，能让人接一句「然后呢？」",
      ],
      note: "这句话既是创作罗盘，也是将来印在书封上的那行字。写不出它，说明故事还没想清楚。",
    },
  },
  paragraph: {
    target: 300,
    scaffold: { type: "beats" },
    guide: {
      task: "第一次分形展开：一句话长成五句话。五句话 = 三幕骨架——铺垫、三个逐级升高的灾难、结局。全书的脊柱在这一步立直。",
      writing: [
        { k: "五句五节点", v: "①背景与主角登场 → ②灾难一：被迫卷入，无法回头 → ③灾难二：世界观被打碎 → ④灾难三：局势失控，逼向终局 → ⑤决战与收尾" },
        { k: "灾难逐级升高", v: "每个灾难都改变主角下一步能做什么——代价更大、退路更少、选择更不可逆" },
        { k: "道德前提翻转", v: "灾难二是中点：主角的错误信念碎掉，正确信念开始生长" },
      ],
      checklist: [
        "三个灾难一个比一个狠，退路逐级收窄。",
        "灾难二处主角的核心信念发生翻转。",
        "结局回应了铺垫埋下的赌注。",
      ],
      note: "脊柱歪了，后面长多少肉都是歪着长。改五句话只要十分钟；改十万字的初稿要十个月。",
    },
  },
  characters: {
    target: 240,
    scaffold: { type: "charsheet" },
    guide: {
      task: "情节与角色交替展开——第一次切到角色轨道。角色的价值观冲突产生你所有的场景冲突。给每人一张摘要表：目标、抱负、价值观、阻碍、顿悟。",
      writing: [
        { k: "目标要具体", v: "写看得见、可验证的东西——不是「寻找自我」，是「查清谁改了档案」" },
        { k: "价值观要碰撞", v: "用「没有什么比 ___ 更重要」写 3 条，互相有张力——主角和对手这句话必须冲突" },
        { k: "反派也是主角", v: "每个角色都是自己故事的主角，包括反派——在她自己的故事里，她也是对的" },
      ],
      checklist: [
        "每个角色都有一个具体的、可验证的目标。",
        "主角和对手的价值观正面对撞。",
        "反派的逻辑在她自己看来说得通。",
      ],
      note: "故事 = 角色被丢进坩埚。坩埚的温度，取决于你把对手写得多认真。",
    },
  },
  synopsis: {
    target: 800,
    scaffold: { type: "synopsisbeats" },
    guide: {
      task: "第二次分形展开：五句话的每一句变成一段，长成约一页梗概。这是故事第一次「填肉」——做法机械但极可靠。",
      writing: [
        { k: "一句变一段", v: "03 第 N 句 → 第 N 段：①世界观与初始冲突 ②触发事件与灾难一 ③挣扎与灾难二的认知翻转 ④升级与灾难三 ⑤高潮走向与收尾" },
        { k: "每段要有画面", v: "别写概括——给具体的时间、地点、在场的人、行动与反应" },
        { k: "结尾留钩", v: "每段结尾制造「必须翻到下一段」的牵引力" },
      ],
      checklist: [
        "每一段都严格对应 03 的一句话。",
        "每一段都有一个看得见的具体场景。",
        "结尾有让人「必须继续」的钩子。",
      ],
      note: "这一页就是你的故事提案。讲不清一页，就讲不清四百页。",
    },
  },
  backstory: {
    target: 800,
    scaffold: { type: "backstory" },
    guide: {
      task: "角色轨道的第二次展开：为每人写半页来路。不是编户口簿——而是找到那件把她变成今天这个人的事。理解角色为何如此，你才能写出真实可信的行动。",
      writing: [
        { k: "信念的起点", v: "哪些关键事件塑造了她的性格？故事开始前她相信什么？" },
        { k: "内心世界", v: "她真正渴望的是什么？为何渴望？最害怕被人发现什么？" },
        { k: "关系与行为", v: "她与其他角色的纠葛；她在压力下会表现出什么行为？" },
      ],
      checklist: [
        "能回答「她为什么变成了现在这个人」。",
        "背景能解释她在正文里的每一个关键选择。",
        "反派的来路写得同样认真。",
      ],
      note: "写不好背景，你就只能让角色听你指挥。写好了，她会自己做决定。",
    },
  },
  outline: {
    target: 600,
    scaffold: { type: "chapters" },
    guide: {
      task: "第三次分形展开：一页梗概的每一段再长成一页，得到四五页的章节大纲。这是最接近实际写作的规划阶段。",
      writing: [
        { k: "一段变一页", v: "05 的每一段 → 这里的一页：加入具体场景设定、角色行动与反应、关键对话要点、情感变化节点" },
        { k: "灾难定位", v: "三个灾难必须落在幕与幕的交界——它们是结构的铰链" },
        { k: "每章推局面", v: "章末的局面必须和章头不同。原地打转的章节要砍" },
      ],
      checklist: [
        "每一章都推动了局面，没有原地打转。",
        "三个灾难在大纲里有明确位置。",
        "章节数量和目标篇幅匹配。",
      ],
      note: "大纲是场景列表的上游：章定不了，场就拆不开。先把骨架立住。",
    },
  },
  profile: {
    target: 700,
    scaffold: { type: "profile" },
    guide: {
      task: "角色的终极展开：为每人建一份「角色圣经」。写完后你应该能替她回答任何问题——因为她在你脑子里活了。这是雪花最深的一层角色挖掘。",
      writing: [
        { k: "四个维度", v: "生理（外貌、习惯）· 心理（恐惧、渴望）· 环境（家庭、工作）· 性格（口头禅、矛盾面）" },
        { k: "矛盾比一致重要", v: "人物的魅力来自矛盾——她嘴上说的和实际做的不一样" },
        { k: "两个版本的她", v: "「别人眼中的她」和「她自己眼中的她」——落差就是人物弧光的起点" },
      ],
      checklist: [
        "四个维度都有具体内容，不只是标签。",
        "至少写出了一个人物内在矛盾。",
        "「别人看她」和「她看自己」之间有落差。",
      ],
      note: "Ingermanson：到这一步你应该对角色了如指掌。找一张「长得像她」的照片贴在桌旁。",
    },
  },
  scenes: {
    target: 500,
    scaffold: { type: "scenelist" },
    guide: {
      task: "分形展开接近底层：把大纲拆成一行一场的清单。场景是小说的基本单位——每个场景必须有冲突，必须是一个完整的缩微故事。",
      writing: [
        { k: "一行一场", v: "编号 · 类型（主动/反应）· POV 角色 · 地点/时间 · 坩埚（困住角色的力量）· 结果/转变" },
        { k: "铁律三条", v: "①每场必须有冲突 ②没有冲突的场景→删除 ③主动与反应交替出现，形成呼吸节奏" },
        { k: "情绪节奏", v: "相邻两场温度要有起伏，不能全程高温也不能全程低温" },
      ],
      checklist: [
        "每一场都有明确冲突。",
        "没有只交代背景的「死场」。",
        "连续读下来，情绪有起有伏。",
      ],
      note: "冲突是让故事跑起来的汽油。场景没冲突，就是一辆抛锚的车——推它不如砍它。",
    },
  },
  planning: {
    target: 400,
    scaffold: { type: "scene" },
    guide: {
      task: "雪花的最后一步：给每场花五分钟画草图。主动场景制造紧张，反应场景让读者喘息并期待——两者交替构成故事的引擎。",
      writing: [
        { k: "主动场景 GCS", v: "目标（具体可拍摄）→ 冲突（多轮受阻）→ 挫败（结尾比开场更糟，迫使翻页）" },
        { k: "反应场景 RDD", v: "反应（情感先于理性，用身体呈现）→ 两难（每个选项都有代价）→ 决定（触发下一场目标）" },
        { k: "交替引擎", v: "主动→反应→主动→反应……挫败接反应，决定接目标——链条不能断" },
      ],
      checklist: [
        "标明了主动 / 反应类型。",
        "三个槽位都填满了。",
        "结尾自然接上下一场的开头。",
      ],
      note: "十步做完，设计阶段结束。从现在起你脑子里只剩一件事：把它写好看。",
    },
  },
};

/* ---- scaffold seeds (structured per-step data) ---- */

/* generic candidate set for steps without a bespoke one */

/* ---- AI candidate generation (后端节点 snowflake_step_candidates，G5) ----
   Gather the confirmed upstream layers as context and let the backend
   template produce 3 divergent candidates. Scaffold data remains editable
   when the LLM is unavailable; generation itself fails closed. */
const S2_ID_LETTERS = ["A", "B", "C", "D"];

// fold one step's content (draft, else scaffold) to a short context line
function s2StepText(key, drafts, scaffolds) {
  const t = s2Content((drafts || {})[key], (scaffolds || {})[key]).replace(/\s+/g, " ").trim();
  return t.length > 180 ? t.slice(0, 180) + "…" : t;
}

// upstream anchor material the model must stay consistent with
function s2UpstreamContext(activeKey, drafts, scaffolds) {
  const idx = S2_STEPS.findIndex(s => s.key === activeKey);
  const lines = [];
  S2_STEPS.forEach((s, i) => {
    if (i >= idx) return;
    const txt = s2StepText(s.key, drafts, scaffolds);
    if (txt) lines.push(`【${s.num} ${s.name}】${txt}`);
  });
  const para = (scaffolds || {}).paragraph || {};
  const pf = (para.premiseF || "").trim(), pt = (para.premiseT || "").trim();
  if (pf || pt) lines.push(`【道德前提·脊柱】错误信念「${pf || "—"}」→ 翻转为「${pt || "—"}」`);
  return lines.join("\n");
}

function s2GenPrompt(active, data, contextStr, currentDraft) {
  const target = data.target || 120;
  const cap = Math.max(40, Math.min(target, 190));
  return [
    "你是雪花写作法（Snowflake Method）的写作助手，正在帮助作者完成长篇小说的构思。",
    `当前步骤：第「${active.num} ${active.name}」步（雪花${active.book}）。`,
    `这一步的任务：${(data.guide && data.guide.task) || active.blurb}`,
    `扩展倍率：${active.grow}。本步整体目标体量约 ${target} 字。`,
    "",
    contextStr ? "已确认的上游材料（人物、冲突、道德前提必须与之严格一致，不得另起炉灶）：\n" + contextStr : "（暂无上游材料，请基于这一步的任务从零提供方向。）",
    "",
    currentDraft && currentDraft.trim() ? "作者当前草稿（可在此基础上改写，也可提出不同方向推翻它）：\n" + currentDraft.trim() : "（作者尚未动笔。）",
    "",
    "请生成 3 条走不同方向的候选草稿（例如：情绪向 / 推进向 / 对照向，或任何贴合本步的差异化角度）。",
    `每条 text 是可直接采纳的正文本身，不要解释或加标题，控制在 ${cap} 字以内。`,
    "三条之间方向要有真实差异，但都要紧扣上游，自洽可用。",
    "只输出一个 JSON 数组，不要任何额外文字、不要代码围栏：",
    '[{"label":"短标签(≤4字)","tag":"一句定位(≤12字)","text":"候选正文","notes":["要点(≤6字)","要点(≤6字)"]}]',
  ].join("\n");
}

function s2ParseCands(raw) {
  if (!raw) throw new Error("空响应");
  let s = String(raw).trim().replace(/```json/gi, "").replace(/```/g, "").trim();
  const a = s.indexOf("["), b = s.lastIndexOf("]");
  if (a >= 0 && b > a) s = s.slice(a, b + 1);
  const arr = JSON.parse(s);
  if (!Array.isArray(arr) || !arr.length) throw new Error("非数组");
  return arr.slice(0, 4).map((c, i) => ({
    id: S2_ID_LETTERS[i] || String(i + 1),
    label: (c.label || `方向 ${i + 1}`).toString().slice(0, 8),
    tag: (c.tag || "AI 候选").toString().slice(0, 16),
    text: (c.text || "").toString().trim(),
    notes: Array.isArray(c.notes) ? c.notes.slice(0, 3).map(n => n.toString().slice(0, 10)) : [],
  })).filter(c => c.text);
}

/* FE→BE 步骤键映射（正源；ws-snow-sync 复用同一份避免漂移） */
const S2_BE_STEPS = [
  ["audience", "book_brief"],
  ["logline", "one_sentence_summary"],
  ["paragraph", "one_paragraph_summary"],
  ["characters", "character_sheets"],
  ["synopsis", "short_synopsis"],
  ["backstory", "character_synopses"],
  ["outline", "long_synopsis"],
  ["profile", "character_bibles"],
  ["scenes", "scene_list"],
  ["planning", "scene_details"],
];
const S2_BE_KEY = Object.fromEntries(S2_BE_STEPS);

/* FE-ALIGN G5：候选生成走后端节点 snowflake_step_candidates（提示词模板在
   config/prompts.yaml）；上下文/草稿折叠文本随请求带入（原型脚手架形状只在
   前端）。LLM 不可用 → 抛引导（默认展示的本地启发式候选不受影响）。 */
async function s2GenerateCands(active, data, drafts, scaffolds) {
  let workId = null;
  try { workId = WsWorks && WsWorks.activeId(); } catch (e) {}
  const beKey = S2_BE_KEY[active.key];
  if (!workId || !beKey) throw new Error("作品尚未就绪，稍后重试");
  let res = null;
  try {
    res = await apiPost(`/api/v2/projects/${workId}/snowflake-workspace/steps/${beKey}/fe-candidates`, {
      context: s2UpstreamContext(active.key, drafts, scaffolds),
      draft: ((drafts || {})[active.key] || ""),
      target_chars: data.target || 120,
    });
  } catch (e) {
    throw new Error("AI 候选生成失败：" + ((e && e.message) || e));
  }
  const cands = (res && res.candidates) || [];
  if (res && res.source === "fallback") {
    throw new Error("AI 候选需要可用的 LLM：请到「系统设置 → 模型与接入」启用后重试（当前展示的是本地启发式候选）。");
  }
  if (!cands.length) throw new Error("未能解析出候选，请重试一次");
  return cands.map((c, i) => ({
    id: S2_ID_LETTERS[i] || String(i + 1),
    label: (c.label || `方向 ${i + 1}`).toString().slice(0, 8),
    tag: (c.tag || "AI 候选").toString().slice(0, 16),
    text: (c.text || "").toString().trim(),
    notes: Array.isArray(c.notes) ? c.notes.slice(0, 3) : [],
  })).filter(c => c.text);
}

/* 「采纳并结构化」等入口统一走视图内的 structuredGenerate（后端 generate 节点：
   每步专用模板 + 权威上游材料 + 压力诊断 + 空字段定向重试；require_llm 保证
   LLM 不可用时诚实报错，绝不落一版启发式草稿冒充）。 */

/* ---- downstream staleness (the fractal method's cheap-backtracking core) ----
   Each step carries a content revision number (revs), bumped whenever its
   folded content changes. When a step is confirmed we snapshot the current
   rev of every upstream ancestor (confirmRevs). A confirmed step is "stale"
   when any ancestor's rev has advanced past the snapshot — i.e. an upstream
   layer was edited after this one was last reviewed. */
function s2Sig(text) {
  const t = text || ""; let h = 0;
  for (let i = 0; i < t.length; i++) { h = (Math.imul(h, 31) + t.charCodeAt(i)) | 0; }
  return h + ":" + t.length;
}
/* 依赖是 DAG 而非单亲链：fromKey 是主展开源，alsoFrom 是跨轨依赖
   （如 05 梗概 / 09 场景列表也依赖 04 角色表：改角色同样触发复核）。 */
function s2Ancestors(key) {
  const out = []; const seen = new Set([key]); let frontier = [key];
  while (frontier.length) {
    const next = [];
    frontier.forEach(k => {
      const s = S2_STEPS.find(x => x.key === k);
      const parents = s ? [s.fromKey, ...(s.alsoFrom || [])].filter(Boolean) : [];
      parents.forEach(p => { if (!seen.has(p)) { seen.add(p); out.push(p); next.push(p); } });
    });
    frontier = next;
  }
  return out;
}
function s2StaleMap(states, revs, confirmRevs) {
  const map = {};
  S2_STEPS.forEach(s => {
    if (states[s.key] !== "done") return;
    const cr = (confirmRevs || {})[s.key] || {};
    const dirty = s2Ancestors(s.key).filter(a => ((revs || {})[a] || 0) > (cr[a] || 0));
    if (dirty.length) map[s.key] = dirty; // nearest dirty ancestor first
  });
  return map;
}
// snapshot of every ancestor's current rev — recorded at confirm/review time
function s2SnapAncestors(key, revs) {
  const snap = {};
  s2Ancestors(key).forEach(a => { snap[a] = (revs || {})[a] || 0; });
  return snap;
}

/* ---- persistence helpers (localStorage, per-work namespaced) ---- */
/* v2：重构前的旧代码未按作品门控种子，曾把 tide 种子原样持久化到其它作品的
   v1 键下（污染）。v2 起换键，并对 v1 做一次性迁移：纯种子拷贝丢弃，
   真·用户创作（与种子有任何差异）才迁移。旧 v1 键保留不删。 */
const s2Key = () => (wsKey ? wsKey("ws_snow_state_v2") : "ws_snow_state_v2");
function s2Load(key) { try { return JSON.parse(localStorage.getItem(key || s2Key())) || {}; } catch (e) { return {}; } }
/* 所有作品（含新建）从空白十步开始 */
function s2BlankScaffolds() {
  return {
    audience: { genre: "", reader: "", pleasure: "", source: "", exclude: "", emotion: "" },
    paragraph: { premiseF: "", premiseT: "", setup: "", d1: "", d2: "", d3: "", resolution: "" },
    characters: { sel: "c1", chars: { c1: { name: "", role: "主角", goal: "", ambition: "", values: "", conflict: "", epiphany: "" } } },
    planning: { sel: "", plans: {} },
    backstory: { sel: "c1", chars: { c1: { name: "", role: "主角", belief: "", wound: "", desire: "", fear: "", relation: "" } } },
    profile: { sel: "c1", chars: { c1: { name: "", role: "主角", physical: "", psych: "", environment: "", personality: "", contradiction: "", views: "" } } },
    scenes: { lines: [], list: [] },
    synopsis: { paras: { setup: "", d1: "", d2: "", d3: "", resolution: "" } },
    outline: { chapters: [] },
  };
}
function s2DefaultDrafts() { return Object.fromEntries(S2_STEPS.map(s => [s.key, ""])); }
function s2DefaultChecks() { return Object.fromEntries(S2_STEPS.map(s => [s.key, (((S2_STEP_DATA[s.key] || {}).guide || {}).checklist || []).map(() => false)])); }
function s2DefaultStates() { return Object.fromEntries(S2_STEPS.map(s => [s.key, "todo"])); }
/* 第 10 步旧数据形状（全书只有一张 GCS/RDD 表）→ 逐场 plans 形状的一次性归一 */
const S2_PLAN_FIELDS = ["goal", "conflict", "setback", "reaction", "dilemma", "decision"];
function s2NormalizePlanning(p) {
  if (!p) return { sel: "", plans: {} };
  const legacyAny = S2_PLAN_FIELDS.some(f => (p[f] || "").trim());
  const out = { sel: p.sel || "", plans: { ...(p.plans || {}) } };
  if (legacyAny && !p.plans) {
    // 纯旧形状：把那张表挂到它标注的场景 id 下（解不出则 S01）
    const m = /S\d+/.exec(p.scene || "");
    const id = m ? m[0] : "S01";
    const plan = { mode: p.mode || "proactive", pov: p.pov || "" };
    S2_PLAN_FIELDS.forEach(f => { plan[f] = p[f] || ""; });
    out.plans[id] = plan;
    out.sel = id;
  }
  if (!out.sel) out.sel = Object.keys(out.plans)[0] || "";
  return out;
}
function s2MergeScaffolds(stored) {
  const base = s2BlankScaffolds();
  if (stored) Object.keys(base).forEach(k => {
    if (!stored[k]) return;
    base[k] = { ...base[k], ...stored[k] };
    if (base[k].chars && stored[k].chars) base[k].chars = { ...base[k].chars, ...stored[k].chars };
    if (k === "planning" && base[k].plans && stored[k].plans) base[k].plans = { ...base[k].plans, ...stored[k].plans };
  });
  base.planning = s2NormalizePlanning(base.planning);
  return base;
}
/* 主页速览：读同一份持久化真相，而非静态拷贝 */
function s2StepSummary() {
  try {
    const saved = s2Load();
    const states = { ...s2DefaultStates(), ...(saved.states || {}) };
    const steps = S2_STEPS.map(s => {
      let v = states[s.key] || "todo";
      if (v === "skip") v = "warn";
      return { name: s.name, s: v };
    });
    const cur = S2_STEPS.find(s => { const v = states[s.key] || "todo"; return v !== "done" && v !== "skip"; });
    return { steps, now: cur ? `${cur.name} · 第 ${cur.num} 步` : "十步已全部确认" };
  } catch (e) { return null; }
}

function s2MergeChecks(stored) {
  const base = s2DefaultChecks();
  if (stored) Object.keys(base).forEach(k => { if (Array.isArray(stored[k]) && stored[k].length === base[k].length) base[k] = stored[k]; });
  return base;
}

/* 把后端水合/结构化导入的稀疏缓存折成视图真正持久化的完整形状。
   同步层和视图层共用这一条边界，避免“刚批准的服务端真相”因为 React 补齐空脚手架
   而被误判为作者编辑，再写回成 pending_review。 */
function s2NormalizeState(saved) {
  const source = saved || {};
  return {
    ...source,
    drafts: { ...s2DefaultDrafts(), ...(source.drafts || {}) },
    scaffolds: s2MergeScaffolds(source.scaffolds),
    checks: s2MergeChecks(source.checks),
    states: { ...s2DefaultStates(), ...(source.states || {}) },
    revs: { ...Object.fromEntries(S2_STEPS.map(s => [s.key, 0])), ...(source.revs || {}) },
    confirmRevs: { ...(source.confirmRevs || {}) },
    history: Array.isArray(source.history) ? source.history : [],
    _t: source._t || Date.now(),
  };
}

/* 场景运行提示词等只读消费者使用：把当前作品的雪花状态物化为独立快照。
   该快照不是项目备份，也不承担跨作品恢复。 */
function s2ExportState() {
  try {
    return s2NormalizeState(s2Load());
  } catch (e) { return null; }
}

function WsSnowflake({ go, initialStep, onOverview }) {
  // freeze this mount's storage key to the work active at mount time, so the
  // unmount flush writes back to the right work even after a switch
  const keyRef = useSR(null);
  if (keyRef.current == null) keyRef.current = s2Key();
  const myKey = keyRef.current;
  const saved = s2Load(myKey);
  const [activeKey, setActiveKey] = useSS(initialStep || "audience");
  /* 页签按步骤记忆：在某步点开「候选」，不该让其它步骤也停在候选页 */
  const [tabByStep, setTabByStep] = useSS({});
  const [drafts, setDrafts] = useSS(() => ({ ...s2DefaultDrafts(), ...(saved.drafts || {}) }));
  const [scaffolds, setScaffolds] = useSS(() => s2MergeScaffolds(saved.scaffolds));
  const [checks, setChecks] = useSS(() => s2MergeChecks(saved.checks));
  const [states, setStates] = useSS(() => ({ ...s2DefaultStates(), ...(saved.states || {}) }));
  const [revs, setRevs] = useSS(() => ({ ...Object.fromEntries(S2_STEPS.map(s => [s.key, 0])), ...(saved.revs || {}) }));
  const [confirmRevs, setConfirmRevs] = useSS(() => ({ ...(saved.confirmRevs || {}) }));
  const [history, setHistory] = useSS(() => saved.history || []);
  const latestRef = useSR();
  latestRef.current = { drafts, scaffolds, checks, states, revs, confirmRevs, history };
  const sigRef = useSR(null);
  const [savedAt, setSavedAt] = useSS(saved._t || null);
  const snowWorkId = String(myKey || "").split("::")[1] || "";
  const [syncState, setSyncState] = useSS(() => {
    try { return (window.SnowSync && window.SnowSync.syncState && window.SnowSync.syncState(snowWorkId)) || { phase: "idle", error: null }; } catch (e) { return { phase: "idle", error: null }; }
  });
  const [syncRetryBusy, setSyncRetryBusy] = useSS(false);
  const { toast, show: pushToast, clear: clearToast } = useUndoToast();
  const [importOpen, setImportOpen] = useSS(false);
  const [importText, setImportText] = useSS("");
  const [importBusy, setImportBusy] = useSS(false);
  const [importError, setImportError] = useSS("");
  const [ctxOpen, setCtxOpen] = useSS(false);
  const [snapDiff, setSnapDiff] = useSS(null);   // 待预览的历史快照条目
  const [genCands, setGenCands] = useSS({});   // ai-generated candidates, keyed by step
  /* busy / 错误也按步骤隔离：全局布尔会让"生成中…"在所有步骤的按钮上亮起，
     并挡住其它步骤发起自己的生成 */
  const [genBusyMap, setGenBusyMap] = useSS({});
  const [genErrMap, setGenErrMap] = useSS({});
  /* 后端 per-step 权威健康（score/status/缺字段/前序闸门）——来自 SnowSync
     （hydrate 全量 + 每次保存后 PATCH 回包增量），与「实时自评」的本地正则估算区分展示 */
  const [beHealth, setBeHealth] = useSS(() => { try { return (window.SnowSync && window.SnowSync.health()) || {}; } catch (e) { return {}; } });
  useSE(() => {
    const refresh = (event) => {
      const detail = (event && event.detail) || {};
      if (detail.workId && detail.workId !== snowWorkId) return;
      try { setSyncState((window.SnowSync && window.SnowSync.syncState && window.SnowSync.syncState(snowWorkId)) || detail.state || { phase: "idle", error: null }); } catch (e) {}
    };
    window.addEventListener("ws:snow-sync-state", refresh);
    window.addEventListener("ws:work-changed", refresh);
    return () => { window.removeEventListener("ws:snow-sync-state", refresh); window.removeEventListener("ws:work-changed", refresh); };
  }, [snowWorkId]);
  useSE(() => {
    const refresh = () => { try { setBeHealth((window.SnowSync && window.SnowSync.health()) || {}); } catch (e) {} };
    window.addEventListener("ws:snow-health", refresh);
    window.addEventListener("ws:snow-hydrated", refresh);
    window.addEventListener("ws:work-changed", refresh);
    return () => { window.removeEventListener("ws:snow-health", refresh); window.removeEventListener("ws:snow-hydrated", refresh); window.removeEventListener("ws:work-changed", refresh); };
  }, []);
  /* 物化后回流：构思 9/10 步领先于目录场景卡的场（SnowSync.resyncStatus，后端真相）。
     pendingCount>0 时顶部横幅给一键「同步到目录」——不同步，写作台/AI 起草台拿到的是旧三拍 */
  const [resyncInfo, setResyncInfo] = useSS(() => { try { return (window.SnowSync && window.SnowSync.resyncStatus()) || { pendingCount: 0, pendingScenes: [] }; } catch (e) { return { pendingCount: 0, pendingScenes: [] }; } });
  const [resyncBusy, setResyncBusy] = useSS(false);
  useSE(() => {
    const refresh = () => { try { setResyncInfo((window.SnowSync && window.SnowSync.resyncStatus()) || { pendingCount: 0, pendingScenes: [] }); } catch (e) {} };
    window.addEventListener("ws:snow-resync", refresh);
    window.addEventListener("ws:snow-hydrated", refresh);
    window.addEventListener("ws:work-changed", refresh);
    return () => { window.removeEventListener("ws:snow-resync", refresh); window.removeEventListener("ws:snow-hydrated", refresh); window.removeEventListener("ws:work-changed", refresh); };
  }, []);
  const doResync = async () => {
    if (resyncBusy || !window.SnowSync || !window.SnowSync.resync) return;
    setResyncBusy(true);
    try {
      const r = await window.SnowSync.resync();
      // 有一部分没能回流时不能报干净的成功——作者会以为目录已经是最新的。
      if (r.notice && r.notice.message) showToast(r.notice.message, "crimson");
      else showToast(`已把 ${r.synced} 场的构思改动同步到目录场景卡`, "sage");
    } catch (e) {
      window.alert("同步到目录失败：" + ((e && e.message) || "请稍后重试"));
    } finally { setResyncBusy(false); }
  };
  /* 「整理为章节结构」= 打开分章预览面板（P2）。
     以前这里是个 window.confirm 加三条互不相同的落库路径（后端物化 / 前端脊柱锚点 /
     只建空壳章），选哪条取决于闸门状态 —— 做得越完整反而掉进最差的那条，而且确认框
     说「并入 12 章」、实际写 1 章。现在只有一条：预览 → 作者确认 → 一次落库。 */
  const [chapterPlanOpen, setChapterPlanOpen] = useSS(false);
  const openChapterPlan = () => setChapterPlanOpen(true);
  const goToMaterializationStep = (beKey) => {
    const pair = S2_BE_STEPS.find(([, candidate]) => candidate === beKey);
    if (!pair) return;
    setChapterPlanOpen(false);
    setActiveKey(pair[0]);
    setTabFor(pair[0], "edit");
  };
  const onChapterPlanDone = (result) => {
    setChapterPlanOpen(false);
    const chapters = (result && result.created_chapter_count) || 0;
    showToast(
      chapters ? `已整理并写入 ${chapters} 章 · 可到章节编排复核` : "章节目录已是最新 · 未重复写入同名章节",
      "sage",
    );
  };
  const active = S2_STEPS.find(s => s.key === activeKey) || S2_STEPS[2];
  const data = S2_STEP_DATA[activeKey] || {};
  /* 当前步骤的页签 / busy / 错误视图（底层都按步骤存） */
  const tab = tabByStep[activeKey] || "edit";
  const setTabFor = (key, v) => setTabByStep(prev => ({ ...prev, [key]: v }));
  const setTab = (v) => setTabFor(activeKey, v);
  const genBusy = !!genBusyMap[activeKey];
  const genErr = genErrMap[activeKey] || null;
  const seedHints = null;
  const draft = drafts[activeKey] || "";
  const setDraft = (v) => setDrafts(prev => ({ ...prev, [activeKey]: typeof v === "function" ? v(prev[activeKey]) : v }));
  const updateScaffold = (updater) => setScaffolds(prev => ({ ...prev, [activeKey]: updater(prev[activeKey]) }));
  const toggleCheck = (i) => setChecks(prev => ({ ...prev, [activeKey]: (prev[activeKey] || []).map((v, j) => j === i ? !v : v) }));
  const gen = genCands[activeKey];
  /* 候选只来自后端 AI 生成通道；未生成时列表为空，不再用本地启发式拼假候选 */
  const cands = (gen && gen.list) || [];
  const candMeta = gen ? { ai: true, at: gen.at } : { ai: false };
  const idx = S2_STEPS.findIndex(s => s.key === activeKey);
  const doneCount = S2_STEPS.filter(s => states[s.key] === "done").length;
  const staleMap = s2StaleMap(states, revs, confirmRevs);
  const staleCount = Object.keys(staleMap).length;
  const curStale = staleMap[activeKey];   // array of dirty ancestor keys, or undefined

  /* 统一回执：UndoToast（ws-undo-toast.jsx）。保留 (label, tone) 旧签名，25+ 调用点不动 */
  const showToast = (label, tone) => pushToast({ text: label, tone: tone || "sage", timeout: 4200 });
  /* 历史时间线：snap 是可回滚的内容快照（只给最近 20 条保留，控制体积） */
  const pushHist = (action, note, who = "我", snap = null) =>
    setHistory(prev => [{ t: Date.now(), who, action, note: note || "", key: activeKey, snap }, ...prev]
      .slice(0, 80)
      .map((h, i) => (i < 20 ? h : (h.snap ? { ...h, snap: null } : h))));
  const snapNow = (key) => {
    try { return JSON.parse(JSON.stringify({ draft: drafts[key] || "", scaffold: scaffolds[key] })); } catch (e) { return null; }
  };
  const restoreSnap = (h) => { if (h && h.snap) setSnapDiff(h); };
  const applySnap = (h) => {
    if (!h || !h.snap) return;
    const st = S2_STEPS.find(s => s.key === h.key); if (!st) return;
    // 回滚前先给当前状态留底，回滚本身也可被撤销
    const backup = snapNow(h.key);
    setDrafts(prev => ({ ...prev, [h.key]: h.snap.draft || "" }));
    if (h.snap.scaffold) setScaffolds(prev => ({ ...prev, [h.key]: JSON.parse(JSON.stringify(h.snap.scaffold)) }));
    setHistory(prev => [{ t: Date.now(), who: "我", action: "回滚快照", note: `${st.num} ${st.name} ← ${new Date(h.t).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}`, key: h.key, snap: backup }, ...prev].slice(0, 80));
    setActiveKey(h.key); setTabFor(h.key, "edit"); setSnapDiff(null);
    showToast(`已回滚 · ${st.name}`, "gold");
  };
  const goStep = (i) => setActiveKey(S2_STEPS[Math.max(0, Math.min(S2_STEPS.length - 1, i))].key);
  const nextUnfinished = (from) => {
    for (let i = 1; i <= S2_STEPS.length; i++) {
      const s = S2_STEPS[(from + i) % S2_STEPS.length];
      if (states[s.key] !== "done" && s.key !== activeKey) return S2_STEPS.findIndex(x => x.key === s.key);
    }
    return Math.min(S2_STEPS.length - 1, from + 1);
  };
  const confirmStep = () => {
    setStates(prev => ({ ...prev, [activeKey]: "done" }));
    setConfirmRevs(prev => ({ ...prev, [activeKey]: s2SnapAncestors(activeKey, revs) }));
    pushHist("确认本步", `${active.num} ${active.name}`, "我", snapNow(activeKey));
    showToast(`已确认 · ${active.name}`, "sage");
    const ni = nextUnfinished(idx); if (ni >= 0) goStep(ni);
  };
  /* re-review a stale step in place: realign its upstream snapshot, stay put */
  const reviewStep = () => {
    setConfirmRevs(prev => ({ ...prev, [activeKey]: s2SnapAncestors(activeKey, revs) }));
    setStates(prev => ({ ...prev, [activeKey]: "done" }));
    pushHist("复核对齐", `${active.num} ${active.name}`, "我", snapNow(activeKey));
    showToast(`已复核 · ${active.name} 与上游重新对齐`, "sage");
  };
  const skipStep = () => {
    setStates(prev => ({ ...prev, [activeKey]: prev[activeKey] === "done" ? "done" : "skip" }));
    pushHist("略过此步", `${active.num} ${active.name}`);
    showToast(`已略过 · ${active.name}`, "slate"); goStep(idx + 1);
  };

  const regenerate = async () => {
    const key = activeKey;
    if (genBusyMap[key]) return;
    setGenErrMap(prev => ({ ...prev, [key]: null }));
    setGenBusyMap(prev => ({ ...prev, [key]: true }));
    try {
      const list = await s2GenerateCands(active, data, drafts, scaffolds);
      setGenCands(prev => ({ ...prev, [key]: { list, at: Date.now() } }));
      pushHist(`生成 ${list.length} 条候选`, `${active.num} ${active.name}`, "Claude");
      showToast(`已生成 ${list.length} 条候选 · 依据上游材料与诊断缺口`, "gold");
    } catch (err) {
      setGenErrMap(prev => ({ ...prev, [key]: (err && err.message) || "生成失败，请稍后重试" }));
    } finally {
      setGenBusyMap(prev => ({ ...prev, [key]: false }));
    }
  };

  /* 编辑区「让 AI 生成候选」→ 跳候选页并（还没有 AI 候选时）触发生成 */
  const openCands = () => { setTab("candidates"); if (!gen && !genBusy) regenerate(); };

  /* 结构化生成通用通道：候选采纳 / 整表生成 / 全部补全 / 单场补全共用——
     后端 generate → 整步规范草稿经 applyServerStep 反推回脚手架，健康评分随回包刷新。
     focusRow 时只回写焦点场的规划（其余场保留本地态，防止未上行编辑被服务端旧值盖掉）。 */
  const [structBusyMap, setStructBusyMap] = useSS({});
  const structBusy = !!structBusyMap[activeKey];
  const structuredGenerate = async ({ direction = null, focus = null, focusRow = null, focusChars = null, focusChar = null, histAction, histNote, doneAction, doneNote, toastOk, toastFail, switchTab = false, fallbackText = null }) => {
    const key = activeKey, step = active;
    if (structBusyMap[key]) return false;
    setGenErrMap(prev => ({ ...prev, [key]: null }));
    setStructBusyMap(prev => ({ ...prev, [key]: true }));
    pushHist(histAction, `${step.num} ${step.name}${histNote ? " · " + histNote : ""} · 生成前留底`, "我", snapNow(key));
    try {
      let workId = null;
      try { workId = WsWorks && WsWorks.activeId(); } catch (e) {}
      const beKey = S2_BE_KEY[key];
      if (!workId || !beKey) throw new Error("作品尚未就绪，稍后重试");
      const body = { require_llm: true, source: focus ? "fe_scene_focus_ai" : focusChars ? "fe_char_focus_ai" : (direction ? "fe_candidate_adopt" : "fe_scaffold_ai") };
      if (direction) body.direction_text = direction;
      if (focus) body.focus_scene_refs = focus;
      if (focusChars) body.focus_character_refs = focusChars;
      /* 本地最新规范草稿随请求带入（与上行 PATCH 同源）：消除「刚加的角色/场
         还没自动保存上行，模型看不到、合并后被丢掉」的竞态 */
      try {
        const dOv = (window.SnowSync && window.SnowSync.pushCanon) ? window.SnowSync.pushCanon(key, { drafts, scaffolds }, workId) : null;
        if (dOv && Object.keys(dOv).length) body.draft_override = dOv;
      } catch (e) {}
      const res = await apiPost(`/api/v2/projects/${workId}/snowflake-workspace/steps/${beKey}/generate`, body);
      if (!res || !res.step) throw new Error("生成回包缺少 step");
      const fe = (window.SnowSync && window.SnowSync.applyServerStep)
        ? window.SnowSync.applyServerStep(workId, key, res.step) : null;
      if (fe && fe.scaffold) {
        if (focusRow && key === "planning") {
          const fePlans = (fe.scaffold || {}).plans || {};
          setScaffolds(prev => {
            const cur = prev[key] || {};
            return { ...prev, [key]: { ...cur, sel: focusRow, plans: { ...(cur.plans || {}), [focusRow]: fePlans[focusRow] || (cur.plans || {})[focusRow] || {} } } };
          });
        } else if (focusChar && (key === "characters" || key === "backstory" || key === "profile")) {
          /* 单角色定向：只把焦点角色的生成结果并回本地，其余角色保持本地态
             （与 planning 的 focusRow 同一防线：未上行编辑不被服务端旧值盖掉） */
          const feChars = (fe.scaffold || {}).chars || {};
          setScaffolds(prev => {
            const cur = prev[key] || {};
            return { ...prev, [key]: { ...cur, sel: focusChar, chars: { ...(cur.chars || {}), [focusChar]: feChars[focusChar] || (cur.chars || {})[focusChar] || {} } } };
          });
        } else {
          setScaffolds(prev => ({ ...prev, [key]: fe.scaffold }));
        }
        setDrafts(prev => ({ ...prev, [key]: "" })); // 脚手架即唯一内容源，避免旧自由草稿盖住它
      } else if (fe && fe.text != null) {
        setDrafts(prev => ({ ...prev, [key]: fe.text }));
      } else if (fallbackText != null) {
        setDrafts(prev => ({ ...prev, [key]: fallbackText })); // 兜底：至少落自由草稿
      }
      if (switchTab) setTabFor(key, "edit");
      /* 分批深化中途失败等半成品：后端把事实放在 health.generation_notice，
         这里必须把绿色的「已生成」降级成警告——否则作者以为整表都做完了 */
      const notice = ((res.step || {}).health || {}).generation_notice;
      const noticeMsg = notice && String(notice.message || "").trim();
      pushHist(doneAction || histAction,
        `${step.num} ${step.name}${doneNote ? " · " + doneNote : ""}${noticeMsg ? " · " + noticeMsg : ""}`, "Claude");
      if (noticeMsg) {
        setGenErrMap(prev => ({ ...prev, [key]: noticeMsg }));
        showToast(noticeMsg.slice(0, 60), "crimson");
      } else {
        showToast(toastOk || "已生成 · 可回滚", "gold");
      }
      return true;
    } catch (err) {
      setGenErrMap(prev => ({ ...prev, [key]: (err && err.message) || "生成失败，请稍后重试" }));
      showToast(toastFail || ("生成失败：" + ((err && err.message) || "稍后重试").slice(0, 40)), "crimson");
      return false;
    } finally {
      setStructBusyMap(prev => ({ ...prev, [key]: false }));
    }
  };

  /* 采纳并结构化：候选正文作为方向蓝本，展开整步 */
  const adoptStructured = (t, id) => structuredGenerate({
    direction: t, switchTab: true, fallbackText: t,
    histAction: `采纳候选 ${id} · 结构化`, histNote: "采纳前留底",
    doneAction: "结构化整步", doneNote: `依候选 ${id} 展开全部字段`,
    toastOk: `候选 ${id} 已结构化写入「${active.name}」· 可回滚`,
    toastFail: "结构化失败 · 候选仍可「仅作草稿」采纳",
  });

  /* 多成员步骤（04/06/08 角色 · 10 场景规划）的增量采纳：候选方向 + 焦点定向组合，
     只更新当前选中的成员，其余保持不动 */
  const candFocus = (() => {
    if (activeKey === "characters" || activeKey === "backstory" || activeKey === "profile") {
      const sc = scaffolds[activeKey] || {};
      const roster = activeKey === "characters" ? (sc.chars || {}) : (((scaffolds.characters || {}).chars) || {});
      const ids = Object.keys(roster);
      if (!ids.length) return null;
      const sel = (sc.sel && (roster[sc.sel] || (sc.chars || {})[sc.sel])) ? sc.sel : ids[0];
      const name = (((roster[sel] || (sc.chars || {})[sel]) || {}).name || "").trim();
      return { kind: "char", id: sel, label: (name || sel).slice(0, 8) };
    }
    if (activeKey === "planning") {
      const sel = ((scaffolds.planning || {}).sel || "").trim();
      if (!sel) return null;
      const row = (((scaffolds.scenes || {}).list) || []).find(s => s.id === sel);
      const title = ((row && (row.event || row.place)) || "").trim();
      return { kind: "scene", id: sel, label: title ? `${sel} ${title}`.slice(0, 12) : sel };
    }
    return null;
  })();
  const adoptStructuredFocused = candFocus ? (t, id) => structuredGenerate({
    direction: t, switchTab: true,
    ...(candFocus.kind === "char" ? { focusChars: [candFocus.id], focusChar: candFocus.id } : { focus: [candFocus.id], focusRow: candFocus.id }),
    histAction: `采纳候选 ${id} · 定向「${candFocus.label}」`, histNote: "采纳前留底",
    doneAction: "定向结构化", doneNote: `依候选 ${id} 只更新「${candFocus.label}」`,
    toastOk: `候选 ${id} 已定向写入「${candFocus.label}」· 其余成员未动 · 可回滚`,
    toastFail: "定向结构化失败 · 可改用整步结构化或仅作草稿",
  }) : null;

  /* 场景分诊（第 10 步）：后端逐场评估 pass/maybe/rewrite + 修复建议/补丁。
     draft_override 带本地最新折叠草稿，免受自动保存节流竞态影响。
     分诊结果随手存档（save_scene_triage）——「重写」场会真实阻挡物化闸门；
     会话内记住 triage_id，复诊时原行更新而不是堆新行。 */
  const [triage, setTriage] = useSS(null);   // { items: rowUid -> item, at, source }
  const [triageBusy, setTriageBusy] = useSS(false);
  const triageIdsRef = useSR({});            // scene_plan_id -> triage_id（会话内复用）
  const runTriage = async () => {
    if (triageBusy) return;
    setTriageBusy(true);
    try {
      let workId = null;
      try { workId = WsWorks && WsWorks.activeId(); } catch (e) {}
      if (!workId) throw new Error("作品尚未就绪");
      const draftOverride = (window.SnowSync && window.SnowSync.canonDraft)
        ? window.SnowSync.canonDraft("planning", { drafts, scaffolds }) : null;
      const res = await apiPost(`/api/v2/projects/${workId}/snowflake-workspace/scene-triage/suggest`,
        draftOverride && (draftOverride.scenes || []).length ? { draft_override: draftOverride } : {});
      const byRow = {};
      (res && res.items || []).forEach(it => { const k = it.row_uid || it.scene_id; if (k) byRow[k] = it; });
      setTriage({ items: byRow, at: Date.now(), source: (res && res.source) || "fallback" });
      pushHist("场景分诊", `10 场景规划 · ${Object.keys(byRow).length} 场`, res && res.source === "llm" ? "Claude" : "规则");
      // 存档为推荐态（不写人工裁定），让「重写场挡物化」的闸门真实生效
      try {
        const saved = await apiPost(`/api/v2/projects/${workId}/snowflake-workspace/scene-triage`, {
          items: (res && res.items || []).map(it => ({
            triage_id: triageIdsRef.current[it.scene_plan_id] || "",
            scene_plan_id: it.scene_plan_id, scene_id: it.scene_id,
            recommended_status: it.status, score: it.score,
            missing_fields: it.missing_fields, fix_steps: it.fix_steps,
            repair_patch: it.repair_patch, notes: it.notes,
          })),
        });
        (saved && saved.items || []).forEach(it => { if (it.scene_plan_id && it.triage_id) triageIdsRef.current[it.scene_plan_id] = it.triage_id; });
        try { window.SnowSync && window.SnowSync.refetch && window.SnowSync.refetch(workId); } catch (e2) {}
      } catch (e2) { /* 存档失败不打断分诊展示；下次分诊重试 */ }
      showToast(res && res.source === "llm" ? "分诊完成 · AI 评估每场压力 · 已存档" : "分诊完成 · 规则诊断（启用 LLM 可得更深评估）· 已存档", "gold");
    } catch (err) {
      showToast("分诊失败：" + ((err && err.message) || "稍后重试").slice(0, 40), "crimson");
    } finally {
      setTriageBusy(false);
    }
  };

  /* 一键应用分诊修复补丁：GCS/RDD 字段进 10 的 plans，坩埚/摘要/地点回写 09 的场景行 */
  const applyTriageRepair = (rowUid, item) => {
    const patch = (item && item.repair_patch) || {};
    if (!Object.keys(patch).length) return;
    pushHist("应用修复补丁", `10 场景规划 · ${rowUid} 修复前留底`, "我", snapNow("planning"));
    const planKeys = ["goal", "conflict", "setback", "reaction", "dilemma", "decision", "cost_requirement"];
    setScaffolds(prev => {
      const cur = prev.planning || {};
      const plan = { ...((cur.plans || {})[rowUid] || {}) };
      planKeys.forEach(k => { if (patch[k]) plan[k] = patch[k]; });
      const next = { ...prev, planning: { ...cur, sel: rowUid, plans: { ...(cur.plans || {}), [rowUid]: plan } } };
      const cru = patch.scene_crucible || patch.crucible;
      if (cru || patch.summary || patch.location) {
        const sc = prev.scenes || {};
        next.scenes = { ...sc, list: (sc.list || []).map(s => s.id !== rowUid ? s : {
          ...s, crucible: cru || s.crucible, event: patch.summary || s.event, place: patch.location || s.place,
        }) };
      }
      return next;
    });
    showToast(`已应用修复补丁 · ${rowUid} · 可回滚`, "gold");
  };

  /* 传给 09/10 脚手架的 AI 工具面 */
  const sceneAI = {
    structBusy, triage, triageBusy, onTriage: runTriage, onApplyRepair: applyTriageRepair,
    onGenerateAll: () => structuredGenerate({
      histAction: "AI 生成场景表", doneAction: "AI 生成场景表",
      doneNote: "依上游大纲与角色生成整表", toastOk: "场景表已生成 · 依上游材料 · 可回滚",
    }),
    onFillAll: () => structuredGenerate({
      histAction: "AI 补全所有场景", doneAction: "AI 补全所有场景",
      doneNote: "逐场补齐 GCS/RDD 与钩子", toastOk: "所有场景已补全 · 可回滚",
    }),
    onFillScene: (rowUid) => structuredGenerate({
      focus: [rowUid], focusRow: rowUid,
      histAction: `AI 补全 ${rowUid}`, doneAction: `AI 补全 ${rowUid}`,
      doneNote: "单场定向生成", toastOk: `${rowUid} 已补全 · 其余场景未动 · 可回滚`,
    }),
  };

  /* 传给 04/06/08 角色编辑器的 AI 工具面：只补全当前选中的角色——
     后端 focus_character_refs 定向生成，其余角色（含名册顺序）保持不动 */
  const charAI = {
    structBusy,
    onFillChar: (charId, charName) => {
      const label = (charName || "").trim() || charId;
      return structuredGenerate({
        focusChars: [charId], focusChar: charId,
        histAction: `AI 补全角色「${label}」`, doneAction: `AI 补全角色「${label}」`,
        doneNote: "单角色定向生成", toastOk: `「${label}」已补全 · 其余角色未动 · 可回滚`,
      });
    },
  };

  /* 驻场教练（snowflake_workspace_assistant）：逐步对话辅导，回合服务端持久化。
     第 10 步自动聚焦当前选中场（row_uid，后端已兼容）；带 draft_override 免竞态。
     candidate_patch 是咨询式补丁：应用时空值不清空、按 id 对位、不删成员。 */
  const [coachHist, setCoachHist] = useSS([]);     // 后端 assistant_history（全步骤，服务端持久化）
  const [coachBusy, setCoachBusy] = useSS(false);
  const coachFocusRow = activeKey === "planning" ? ((scaffolds.planning || {}).sel || "") : "";
  /* 进教练页且本地还没有历史 → 从 workspace 懒加载（跨会话回合可见） */
  useSE(() => {
    if (tab !== "coach" || coachHist.length) return;
    (async () => {
      try {
        const workId = WsWorks && WsWorks.activeId();
        if (!workId) return;
        const ws = await apiGet(`/api/v2/projects/${workId}/snowflake-workspace`);
        if (ws && Array.isArray(ws.assistant_history) && ws.assistant_history.length) setCoachHist(ws.assistant_history);
      } catch (e) {}
    })();
  }, [tab]);
  const sendCoach = async (message) => {
    const msg = String(message || "").trim();
    if (coachBusy || !msg) return;
    setCoachBusy(true);
    const key = activeKey, step = active;
    try {
      let workId = null;
      try { workId = WsWorks && WsWorks.activeId(); } catch (e) {}
      const beKey = S2_BE_KEY[key];
      if (!workId || !beKey) throw new Error("作品尚未就绪，稍后重试");
      const body = { step_key: beKey, message: msg };
      const dOv = (window.SnowSync && window.SnowSync.canonDraft) ? window.SnowSync.canonDraft(key, { drafts, scaffolds }) : null;
      if (dOv && Object.keys(dOv).length) body.draft_override = dOv;
      if (key === "planning" && coachFocusRow) body.focus_scene_id = coachFocusRow;
      const res = await apiPost(`/api/v2/projects/${workId}/snowflake-workspace/assistant`, body);
      setCoachHist((res && res.assistant_history) || []);
      pushHist("教练问答", `${step.num} ${step.name}${body.focus_scene_id ? " · 聚焦 " + body.focus_scene_id : ""}`, res && res.source === "llm" ? "Claude" : "规则");
      if (res && res.source !== "llm") showToast("教练已回复 · 规则建议（启用 LLM 可得更深辅导）", "slate");
    } catch (err) {
      showToast("教练回复失败：" + ((err && err.message) || "稍后重试").slice(0, 40), "crimson");
    } finally {
      setCoachBusy(false);
    }
  };
  /* 应用教练补丁（当前步任意带补丁的回合）：咨询式合并——空值不清空、按 id 对位、不删成员 */
  const applyCoachPatch = (turn) => {
    const patch = turn && turn.candidate_patch;
    if (!patch || !Object.keys(patch).length) return;
    pushHist("应用教练补丁", `${active.num} ${active.name} · 应用前留底`, "我", snapNow(activeKey));
    const fe = (window.SnowSync && window.SnowSync.applyCanonPatch)
      ? window.SnowSync.applyCanonPatch(activeKey, { drafts, scaffolds }, patch, null) : null;
    if (fe && fe.scaffold) setScaffolds(prev => ({ ...prev, [activeKey]: fe.scaffold }));
    else if (fe && fe.text != null) setDraft(fe.text);
    setTab("edit");
    showToast(`已应用「${(turn && turn.candidate_label) || "教练补丁"}」· 可回滚`, "gold");
  };

  useSE(() => {
    const inField = (el) => el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); confirmStep(); return; }
      if (e.key === "Escape" && ctxOpen) { setCtxOpen(false); return; }
      if (e.metaKey || e.ctrlKey || e.altKey || inField(e.target)) return;
      if (e.key === "ArrowLeft") { e.preventDefault(); goStep(idx - 1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); goStep(idx + 1); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [idx, activeKey, states, ctxOpen]);
  /* bump a step's revision whenever its folded content changes (drives staleness) */
  useSE(() => {
    const cur = {};
    S2_STEPS.forEach(s => { cur[s.key] = s2Sig(s2Content(drafts[s.key], scaffolds[s.key])); });
    if (!sigRef.current) { sigRef.current = cur; return; }
    const changed = S2_STEPS.filter(s => cur[s.key] !== sigRef.current[s.key]).map(s => s.key);
    if (changed.length) setRevs(prev => { const n = { ...prev }; changed.forEach(k => { n[k] = (n[k] || 0) + 1; }); return n; });
    sigRef.current = cur;
  }, [drafts, scaffolds]);

  /* persist to localStorage (debounced) */
  useSE(() => {
    const id = setTimeout(() => {
      try {
        localStorage.setItem(myKey, JSON.stringify({ drafts, scaffolds, checks, states, revs, confirmRevs, history, _t: Date.now() }));
        setSavedAt(Date.now());
        window.dispatchEvent(new CustomEvent("ws:snow-saved", { detail: myKey }));
      } catch (e) {
        try { window.SnowSync && window.SnowSync.markLocalFailure && window.SnowSync.markLocalFailure(e, snowWorkId); } catch (ignored) {}
      }
    }, 450);
    return () => clearTimeout(id);
  }, [drafts, scaffolds, checks, states, revs, confirmRevs, history]);

  /* 下一跳握手：分章预览发来 flush 请求时，不等 450ms 防抖，立即把当前内存态落盘
     并触发 SnowSync 上行。这样“确认本步 → 立刻整理”不会读取到上一版后端闸门。 */
  useSE(() => {
    const onFlushLocal = (event) => {
      const requestedWorkId = event && event.detail && event.detail.workId;
      if (requestedWorkId && requestedWorkId !== snowWorkId) return;
      try {
        const now = Date.now();
        localStorage.setItem(myKey, JSON.stringify({ ...latestRef.current, _t: now }));
        setSavedAt(now);
        window.dispatchEvent(new CustomEvent("ws:snow-saved", { detail: myKey }));
      } catch (e) {
        try { window.SnowSync && window.SnowSync.markLocalFailure && window.SnowSync.markLocalFailure(e, snowWorkId); } catch (ignored) {}
      }
    };
    window.addEventListener("ws:snow-flush-local", onFlushLocal);
    return () => window.removeEventListener("ws:snow-flush-local", onFlushLocal);
  }, [myKey, snowWorkId]);

  /* flush latest state on unmount (e.g. leaving for 控制塔总览) so the overview reads fresh truth */
  useSE(() => () => {
    try {
      localStorage.setItem(myKey, JSON.stringify({ ...latestRef.current, _t: Date.now() }));
      window.dispatchEvent(new CustomEvent("ws:snow-saved", { detail: myKey }));
    } catch (e) {
      try { window.SnowSync && window.SnowSync.markLocalFailure && window.SnowSync.markLocalFailure(e, snowWorkId); } catch (ignored) {}
    }
  }, []);

  /* FE-ALIGN F3 授权接缝：后端水合（SnowSync）落盘后重读缓存，刷新本组件状态 */
  useSE(() => {
    const onHyd = (e) => {
      if (!e.detail || myKey !== "ws_snow_state_v2::" + e.detail) return;
      const s = s2Load(myKey);
      setDrafts({ ...s2DefaultDrafts(), ...(s.drafts || {}) });
      setScaffolds(s2MergeScaffolds(s.scaffolds));
      setChecks(s2MergeChecks(s.checks));
      setStates({ ...s2DefaultStates(), ...(s.states || {}) });
      setRevs({ ...Object.fromEntries(S2_STEPS.map(x => [x.key, 0])), ...(s.revs || {}) });
      setConfirmRevs({ ...(s.confirmRevs || {}) });
      setHistory(s.history || []); // G2：跨会话 journal（无 snap 条目天然只读）
      sigRef.current = null;
    };
    window.addEventListener("ws:snow-hydrated", onHyd);
    return () => window.removeEventListener("ws:snow-hydrated", onHyd);
  }, []);

  const resetAll = () => {
    if (!window.confirm("重置会清空本作品全部十步草稿与确认状态，恢复到初始稿。此操作不可撤销，确定继续？")) return;
    try { localStorage.removeItem(myKey); } catch (e) {}
    setDrafts(s2DefaultDrafts());
    setScaffolds(s2MergeScaffolds(null));
    setChecks(s2DefaultChecks());
    setStates(s2DefaultStates());
    setRevs(Object.fromEntries(S2_STEPS.map(s => [s.key, 0])));
    setConfirmRevs({});
    setHistory([]);
    sigRef.current = null;
    showToast("已重置为示例稿", "slate");
  };

  const importCanonicalPlan = async () => {
    if (importBusy) return;
    setImportError("");
    let parsed;
    try { parsed = JSON.parse(importText); }
    catch (e) { setImportError("JSON 格式无效，请检查引号、逗号和括号。"); return; }
    if (!window.SnowSync || !window.SnowSync.importCanonicalPlan) {
      setImportError("雪花同步服务尚未就绪，请刷新页面后重试。");
      return;
    }
    setImportBusy(true);
    try {
      const result = await window.SnowSync.importCanonicalPlan(null, parsed);
      if (!result.readyToMaterialize) throw new Error("十步已导入，但后端物化闸门仍未通过；请检查标为重写的步骤或场景。");
      setImportOpen(false);
      setImportText("");
      showToast("结构化计划已导入 · 后端 10/10 批准", "sage");
    } catch (e) {
      setImportError((e && e.message) || "导入失败，请检查计划内容。");
    } finally { setImportBusy(false); }
  };

  /* export the whole snowflake as a Markdown outline (real download) */
  const exportOutline = () => {
    const workTitle = (() => { try { return WsWorks ? WsWorks.active().title : "未命名作品"; } catch (e) { return "未命名作品"; } })();
    const lines = [`# 雪花大纲 · ${workTitle}`, "", `> 导出于 ${new Date().toLocaleString("zh-CN")} · 已确认 ${doneCount}/10${staleCount ? ` · ${staleCount} 需复核` : ""}`, ""];
    S2_STEPS.forEach(s => {
      const text = s2Content(drafts[s.key], scaffolds[s.key]).trim();
      const st = states[s.key];
      const tag = staleMap[s.key] ? "需复核" : (S2_STATE_LABEL[st] || st);
      lines.push(`## ${s.num} ${s.name}　[${tag}]`);
      lines.push(text || "（本步尚未填写）");
      lines.push("");
    });
    try {
      const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a"); a.href = url; a.download = "雪花大纲.md"; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1500);
      pushHist("导出大纲", "全书 10 步 · Markdown");
      showToast("已导出大纲 · 雪花大纲.md", "sage");
    } catch (e) { showToast("导出失败，请重试", "crimson"); }
  };

  /* respond to command-palette step jumps */
  useSE(() => {
    const onStep = (e) => { if (e.detail) { setActiveKey(e.detail); setTabFor(e.detail, "edit"); } };
    window.addEventListener("ws:snow-step", onStep);
    return () => window.removeEventListener("ws:snow-step", onStep);
  }, []);

  const stStatus = states[activeKey];
  /* 「已确认」区分本地态 vs 后端批准态：beStatus==="approved" 才是后端已批。
     approve 在前序闸门不满足时被 ws-snow-sync 静默跳过，此时本地 done 但后端仍 pending_review——
     以前用户无从分辨，这里显式标注。beHealth 缺失（未同步）时不误判为「未批」。 */
  const beApprovedOf = (k) => { const b = beHealth[k]; return !!(b && b.beStatus === "approved"); };
  const beKnownUnapproved = (k) => { const b = beHealth[k]; return !!(b && b.beStatus && b.beStatus !== "approved" && b.beStatus !== "skipped"); };
  const curChecks = checks[activeKey] || [];
  const checkDone = curChecks.filter(Boolean).length;
  const allChecked = curChecks.length > 0 && checkDone === curChecks.length;
  const syncPhase = (syncState && syncState.phase) || "idle";
  const syncError = syncState && syncState.error;
  const savedLabel = syncPhase === "synced"
    ? `服务器已同步${syncState.lastSyncedAt ? ` · ${new Date(syncState.lastSyncedAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}` : ""}`
    : syncPhase === "syncing"
      ? "本机已保存 · 正在同步服务器…"
      : syncPhase === "error"
        ? (syncError && syncError.scope === "local" ? "本机保存失败 · 请立即导出" : "仅本机已保存 · 服务器同步失败")
        : savedAt
          ? `仅本机已保存 · ${new Date(savedAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`
          : "本机自动保存已开启 · 尚未同步服务器";
  const retrySnowSync = async () => {
    if (syncRetryBusy || !window.SnowSync || !window.SnowSync.retry) return;
    setSyncRetryBusy(true);
    try {
      await window.SnowSync.retry(snowWorkId);
    } catch (error) {
      // SnowSync.retry 会自行记录远端 PATCH / approve 错误。这里的兜底异常
      // 不能被误标成“本机保存失败”，否则会把可重试的服务端故障变成导出告警。
      showToast(`同步重试失败：${error && error.message ? error.message : "请稍后再试"}`, "crimson");
    } finally { setSyncRetryBusy(false); }
  };

  return (
    <div className="snow-page" data-screen-label="snowflake">

      <div className="snow-strip">
        <div className="sf-strip-left">
          <S2Fractal progress={doneCount / S2_STEPS.length} />
          <div>
            <div className="snow-strip-eyebrow">构思 · 雪花十步法</div>
            <h1 className="snow-strip-title">从一句话，长成一部小说</h1>
            <p className="sf-principle">像雪花一样层层展开 —— 每一步都在放大上一步，<b>越早回头修订越省力</b>。</p>
          </div>
        </div>
        <div className="snow-strip-progress">
          <div className="snow-strip-num">
            <span key={doneCount} className="sf-count" style={{ fontSize: 24, fontFamily: "var(--font-serif)", fontWeight: 600 }}>{doneCount}</span>
            <span className="text-muted text-sm"> / 10 已确认</span>
            {staleCount > 0 && (
              <button className="sf-stale-count" onClick={() => { const k = Object.keys(staleMap)[0]; if (k) setActiveKey(k); }} title="跳到第一个需复核的步骤">
                <I.AlertTriangle size={12} /> {staleCount} 需复核
              </button>
            )}
          </div>
          <div className="snow-strip-bar">
            {S2_STEPS.map((s, i) => (
              <button key={i} className={`snow-strip-tick s-${states[s.key]} ${staleMap[s.key] ? "is-stale" : ""}`} title={`${s.num} ${s.name}${staleMap[s.key] ? " · 需复核" : ""}`} onClick={() => setActiveKey(s.key)} />
            ))}
          </div>
          <div className="snow-strip-actions">
            <button className="btn btn-ghost btn-sm" onClick={resetAll} title="清空本地草稿"><I.Refresh size={13} /> 重置</button>
            <button className="btn btn-ghost btn-sm" data-testid="snow-import-open" onClick={() => { setImportError(""); setImportOpen(true); }} title="从已有策划稿导入十步规范 JSON；仍逐步经过后端保存与批准闸门"><I.Download size={13} /> 导入结构</button>
            <button className="btn btn-ghost btn-sm" onClick={exportOutline} title="导出全书大纲为 Markdown"><I.UploadCloud size={13} /> 导出大纲</button>
            {/* 这里曾有个 materializeBusy 忙态（disabled + 「整理中…」）。物化搬进分章面板之后
                没有任何代码再写它，按钮永远可点、文案永远是「整理为章节结构」—— 一个只会误导
                读代码的人的死状态。真正的忙态在面板内部（saving）。 */}
            <button className="btn btn-accent btn-sm" data-testid="snow-materialize-top"
              onClick={openChapterPlan} title="07 章节 + 09 场景 + 10 规划 → 先预览分章，确认后写入章节目录">
              <I.Layout size={13} /> 整理为章节结构
            </button>
          </div>
        </div>
      </div>

      {resyncInfo.pendingCount > 0 && (
        <div className="sf-stale-banner sf-resync-banner">
          <span className="sf-stale-banner-ic"><I.Refresh size={15} /></span>
          <div className="sf-stale-body">
            <div className="sf-stale-title">构思已更新 · {resyncInfo.pendingCount} 场的改动还没同步到章节目录</div>
            <div className="sf-stale-sub">
              物化之后你又修改了这些场的规划
              {resyncInfo.pendingScenes.slice(0, 3).map(s => s.title).filter(Boolean).length
                ? <>（{resyncInfo.pendingScenes.slice(0, 3).map(s => s.title).filter(Boolean).join("、")}{resyncInfo.pendingCount > 3 ? " 等" : ""}）</>
                : null}
              ——不同步的话，写作台和 AI 起草台拿到的还是旧场景卡。
            </div>
          </div>
          <button className="btn btn-accent btn-sm sf-stale-ok" disabled={resyncBusy} onClick={doResync} title="把构思里这些场的最新三拍/POV/题名写回目录场景卡">
            <I.Refresh size={13} className={resyncBusy ? "sf-spin" : ""} /> {resyncBusy ? "同步中…" : "同步到目录"}
          </button>
        </div>
      )}

      <div className="snow-cols" data-ctx={ctxOpen ? "open" : "closed"}>
        {/* left — step list */}
        <aside className="snow-steps">
          <div className="sf-track-legend">
            <span className="sf-trk-chip plot"><span className="sf-trk-dot" />情节</span>
            <span className="sf-trk-chip character"><span className="sf-trk-dot" />角色</span>
            <span className="sf-trk-chip orient"><span className="sf-trk-dot" />定位</span>
            <span className="sf-trk-note">两条线交替展开</span>
          </div>
          {S2_STEPS.map((s) => {
            const st = states[s.key];
            const stale = !!staleMap[s.key];
            return (
              <button key={s.key} data-testid={`snow-step-${s.key}`} className={`snow-step ${activeKey === s.key ? "is-active" : ""} s-${st} ${stale ? "is-stale" : ""}`} onClick={() => setActiveKey(s.key)} title={st === "done" && beKnownUnapproved(s.key) ? "本地已确认 · 后端未批准（前序闸门未满足）" : undefined}>
                <span className={`sf-track-bar trk-${s.track}`} />
                <span className="snow-step-num">{s.num}</span>
                <span className="snow-step-body">
                  <span className="snow-step-name">{s.name}</span>
                  <span className="snow-step-blurb">{stale ? `上游已改 · 需复核` : s.blurb}</span>
                </span>
                <span className="snow-step-mark">
                  {stale ? <I.AlertTriangle size={13} className="sf-stale-ic" />
                    : st === "done" ? <I.Check size={13} style={beKnownUnapproved(s.key) ? { color: "var(--gold)" } : undefined} />
                    : st === "warn" ? <I.AlertTriangle size={13} />
                    : st === "skip" ? <span className="sf-skip-mark">–</span>
                    : (st === "active" && activeKey !== s.key) ? <span className="snow-step-dot" /> : null}
                </span>
              </button>
            );
          })}
        </aside>

        {/* center — canvas */}
        <section className="snow-canvas">
          <div className="sf-canvas-anim" key={activeKey}>
            <header className="snow-canvas-head">
              <div className="flex items-center gap-3">
                <span className="snow-canvas-num">{active.num}</span>
                <div>
                  <h2 className="snow-canvas-title">{active.name}</h2>
                  <div className="sf-head-meta">
                    <span className={`sf-trk-tag trk-${active.track}`}>{TRACK_LABEL[active.track]}</span>
                    <span className="sf-meta-sep">雪花 · {active.book}</span>
                    <span className="sf-meta-sep">{active.grow}</span>
                    <span className="sf-meta-sep">建议 {active.timebox}</span>
                    {active.from && (
                      <button className="sf-lineage" onClick={() => setActiveKey(active.fromKey)} title="回到它展开自的那一步">
                        <I.ArrowRight size={11} style={{ transform: "rotate(180deg)" }} /> 展开自 {active.from}
                      </button>
                    )}
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-2">
                {stStatus === "done" ? (
                  beApprovedOf(activeKey) ? (
                    <span className="pill pill-sage" title="后端已批准本步"><span className="pill-dot" />已批准</span>
                  ) : (
                    <span className="pill pill-gold" title={(beHealth[activeKey] && !beHealth[activeKey].gateSatisfied) ? "本地已确认，后端未批准：前序闸门未满足——补齐上游各步后会自动批准" : "本地已确认 · 后端批准同步中…"}><span className="pill-dot" />本地已确认</span>
                  )
                ) : active.essential ? (
                  <span className="pill pill-crimson"><span className="pill-dot" />必填</span>
                ) : (
                  <span className="pill"><span className="pill-dot" />建议</span>
                )}
                {stStatus === "warn" && <span className="pill pill-gold"><span className="pill-dot" />需补</span>}
                {curStale && <span className="pill pill-gold"><span className="pill-dot" />需复核</span>}
                <button className="btn btn-quiet btn-sm sf-ctx-open" onClick={() => setCtxOpen(true)} title="本步上下文"><I.Info size={14} /></button>
              </div>
            </header>

            {curStale && (
              <div className="sf-stale-banner">
                <span className="sf-stale-banner-ic"><I.AlertTriangle size={15} /></span>
                <div className="sf-stale-body">
                  <div className="sf-stale-title">上游已改动 · 本步需复核一致性</div>
                  <div className="sf-stale-sub">
                    你确认本步之后，
                    {curStale.map((a, i) => { const u = S2_STEPS.find(x => x.key === a); return (
                      <button key={a} className="sf-stale-up" onClick={() => setActiveKey(a)}>{u.num} {u.name}<I.ArrowRight size={10} /></button>
                    ); })}
                    发生了变化。回去核对或重写本步后点“已复核”。
                  </div>
                </div>
                <button className="btn btn-accent btn-sm sf-stale-ok" onClick={reviewStep} title="重新与上游对齐"><I.Check size={13} /> 已复核</button>
              </div>
            )}

            <div className="snow-tabs" role="tablist" aria-label={`${active.name}工作区`}>
              <S2Tab id="edit" cur={tab} on={setTab}>编辑</S2Tab>
              <S2Tab id="candidates" cur={tab} on={setTab}>候选 <span className="cand-tab-num">{cands.length}</span></S2Tab>
              <S2Tab id="coach" cur={tab} on={setTab}>教练{coachHist.filter(t => t.step_key === S2_BE_KEY[activeKey]).length ? <span className="cand-tab-num">{coachHist.filter(t => t.step_key === S2_BE_KEY[activeKey]).length}</span> : null}</S2Tab>
              <S2Tab id="history" cur={tab} on={setTab}>历史</S2Tab>
              <S2Tab id="ref" cur={tab} on={setTab}>引用上下文</S2Tab>
            </div>

            {tab === "edit" && (
              data.scaffold
                ? <React.Fragment>
                    {draft.trim() ? <S2DraftOverride draft={draft} setDraft={setDraft} stepName={active.name} /> : null}
                    <S2Scaffold kind={data.scaffold.type} scaffold={scaffolds[activeKey]} onScaffold={updateScaffold} hints={seedHints} refs={scaffolds} go={setActiveKey}
                      ai={(data.scaffold.type === "scenelist" || data.scaffold.type === "scene") ? sceneAI
                        : (data.scaffold.type === "charsheet" || data.scaffold.type === "backstory" || data.scaffold.type === "profile") ? charAI : undefined} />
                  </React.Fragment>
                : <S2Edit draft={draft} setDraft={setDraft} stepName={active.name} target={data.target} meter={data.meter} hints={seedHints} onAICands={openCands} />
            )}
            {tab === "candidates" && (
              <S2Cands draft={draft} cands={cands} meta={candMeta} busy={genBusy} err={genErr} onRegen={regenerate}
                structBusy={structBusy} onAdoptStructured={adoptStructured}
                onAdoptFocused={adoptStructuredFocused} focusLabel={candFocus ? candFocus.label : null}
                onAdopt={(t, id) => { pushHist(`采纳候选 ${id}`, `${active.num} ${active.name} · 采纳前留底`, "我", snapNow(activeKey)); setDraft(t); setTab("edit"); showToast(`已采纳候选 ${id} · 写入「${active.name}」草稿`, "gold"); }} />
            )}
            {tab === "coach" && (
              <S2Coach active={active} beKey={S2_BE_KEY[activeKey]} history={coachHist} busy={coachBusy}
                focusRow={coachFocusRow} onSend={sendCoach} onApplyPatch={applyCoachPatch} />
            )}
            {tab === "history" && <S2History history={history} go={setActiveKey} onRestore={restoreSnap} />}
            {tab === "ref" && <S2Ref active={active} drafts={drafts} scaffolds={scaffolds} />}
          </div>

          <footer className="snow-canvas-foot">
            <button className="btn btn-ghost" disabled={idx === 0} onClick={() => goStep(idx - 1)}><I.ChevronLeft size={14} /> 上一步</button>
            <div
              className={`sf-sync-state is-${syncPhase}`}
              data-testid="snow-sync-status"
              role="status"
              aria-live="polite"
              title={(syncError && syncError.message) || savedLabel}
            >
              {syncPhase === "error" ? <I.AlertTriangle size={12} /> : syncPhase === "syncing" ? <I.Refresh size={12} className="sf-spin" /> : <I.Check size={12} />}
              <span>{savedLabel}</span>
              {syncPhase === "error" && syncError && <em>{syncError.offline ? "当前离线" : syncError.message}</em>}
              {syncPhase === "error" && syncError && syncError.scope !== "local" && (
                <button type="button" data-testid="snow-sync-retry" disabled={syncRetryBusy} onClick={retrySnowSync}>
                  {syncRetryBusy ? "重试中…" : "重试"}
                </button>
              )}
              {syncPhase === "error" && syncError && syncError.scope === "local" && (
                <button type="button" onClick={exportOutline}>立即导出</button>
              )}
            </div>
            <div className="flex items-center gap-2">
              <button className="btn btn-ghost" onClick={skipStep}>略过此步</button>
              {curChecks.length > 0 && (
                <span className={`sf-foot-checks ${allChecked ? "is-all" : ""}`} title="右栏检查清单进度"><I.CheckCircle size={12} /> 自检 {checkDone}/{curChecks.length}</span>
              )}
              <button className={`btn btn-accent ${allChecked && stStatus !== "done" ? "sf-confirm-ready" : ""}`} onClick={confirmStep} title="确认本步 (⌘↵)"><I.Check size={14} /> 确认本步</button>
              <button className="btn btn-primary" disabled={idx === S2_STEPS.length - 1} onClick={() => goStep(idx + 1)}>下一步 <I.ChevronRight size={14} /></button>
            </div>
          </footer>
        </section>

        {/* right — live context (folds into drawer on narrow) */}
        <aside className="snow-ctx">
          <div className="sf-ctx-drawer-head">
            <span className="fw-600">本步上下文</span>
            <button className="wr-drawer-x" onClick={() => setCtxOpen(false)} title="关闭 (Esc)"><I.X size={16} /></button>
          </div>
          <S2Guide guide={data.guide} rubric={data.rubric || S2_RUBRIC} checks={checks[activeKey] || []} onToggle={toggleCheck}
            stepKey={activeKey} draft={draft} scaffold={scaffolds[activeKey]} target={data.target} go={setActiveKey} refs={scaffolds} health={beHealth[activeKey]} />
          <S2Spine active={active} go={setActiveKey} para={scaffolds.paragraph} />
          <S2Links active={active} states={states} go={setActiveKey} staleMap={staleMap} />
        </aside>
        <div className={`sf-ctx-scrim ${ctxOpen ? "show" : ""}`} onClick={() => setCtxOpen(false)} />
      </div>

      {snapDiff && (
        <S2SnapDiff h={snapDiff} current={{ draft: drafts[snapDiff.key] || "", scaffold: scaffolds[snapDiff.key] }}
          onApply={() => applySnap(snapDiff)} onClose={() => setSnapDiff(null)} />
      )}

      {importOpen && (
        <S2ImportPlanDialog value={importText} busy={importBusy} error={importError}
          onChange={setImportText} onImport={importCanonicalPlan}
          onClose={() => { if (!importBusy) { setImportOpen(false); setImportError(""); } }} />
      )}

      {chapterPlanOpen && (
        <WsChapterPlanPanel onClose={() => setChapterPlanOpen(false)} onDone={onChapterPlanDone}
          onGoToStep={goToMaterializationStep} />
      )}

      <UndoToast toast={toast} onClose={clearToast} />
    </div>
  );
}

/* ====== Fractal progress mark (the namesake snowflake) ====== */
function S2Fractal({ progress }) {
  const arms = 6;
  const lit = Math.round((progress || 0) * arms);
  const pts = [];
  for (let i = 0; i < arms; i++) {
    const a = (i * 60) * Math.PI / 180;
    const ex = Math.cos(a) * 40, ey = Math.sin(a) * 40;
    const bx = Math.cos(a) * 23, by = Math.sin(a) * 23;
    const off = 13;
    pts.push({ i, ex, ey, bx, by,
      l1x: bx + Math.cos(a + 0.55) * off, l1y: by + Math.sin(a + 0.55) * off,
      l2x: bx + Math.cos(a - 0.55) * off, l2y: by + Math.sin(a - 0.55) * off,
      on: i < lit });
  }
  return (
    <svg className="sf-fractal" viewBox="-50 -50 100 100" width="48" height="48" aria-hidden="true">
      {pts.map(p => (
        <g key={p.i} stroke={p.on ? "var(--crimson)" : "var(--line-2)"} strokeWidth="2.4" strokeLinecap="round"
          opacity={p.on ? 1 : 0.55} style={{ transition: "stroke .5s ease, opacity .5s ease" }}>
          <line x1="0" y1="0" x2={p.ex} y2={p.ey} />
          <line x1={p.bx} y1={p.by} x2={p.l1x} y2={p.l1y} />
          <line x1={p.bx} y1={p.by} x2={p.l2x} y2={p.l2y} />
        </g>
      ))}
      <circle cx="0" cy="0" r="4" fill="var(--crimson)" />
    </svg>
  );
}

/* ====== Story spine (derived live from step 03 — single source of truth) ====== */
function S2Spine({ active, go, para }) {
  const isPlot = active && active.track === "plot";
  const p = para || {};
  const clip = (s, n) => { s = (s || "").trim(); return s.length > n ? s.slice(0, n) + "…" : s; };
  const rows = [
    { meta: S2_DISASTERS[0], text: p.d1 },
    { meta: S2_DISASTERS[1], text: p.d2 },
    { meta: S2_DISASTERS[2], text: p.d3 },
  ];
  return (
    <div className={`ctx-block sf-spine-block ${isPlot ? "is-hot" : ""}`}>
      <header className="sfx-h sfx-spine-head"><I.Activity size={13} /><span>故事脊柱 · 三幕三灾难</span></header>
      <div className="sf-premise-mini" title="道德前提：在第二个灾难处，错误信念翻转为正确信念">
        <span className="sf-pm-false">{p.premiseF || "错误信念"}</span>
        <I.ArrowRight size={12} />
        <span className="sf-pm-true">{p.premiseT || "正确信念"}</span>
      </div>
      <div className="sf-spine">
        {rows.map((d, i) => (
          <div key={i} className={`sf-spine-row tone-${d.meta.tone}`}>
            <span className="sf-spine-id">{d.meta.id}</span>
            <div className="sf-spine-body">
              <span className="sf-spine-title">{clip(d.text, 18) || "（待填）"}</span>
              <span className="sf-spine-act">{d.meta.act}</span>
            </div>
          </div>
        ))}
      </div>
      <button className="sf-spine-link" onClick={() => go("paragraph")}>
        {active && active.key === "paragraph"
          ? <><I.Activity size={11} /> 这三行就是你正在编辑的脊柱</>
          : <><I.Edit size={11} /> 在 03 一段话里编辑脊柱</>}
      </button>
    </div>
  );
}

function S2Tab({ id, cur, on, children }) {
  return <button role="tab" aria-selected={cur === id} tabIndex={cur === id ? 0 : -1} onKeyDown={onRovingTabKeyDown}
    className={`snow-tab ${cur === id ? "is-active" : ""}`} onClick={() => on(id)}>{children}</button>;
}

/* ====== Collapsible flat section (shared rail primitive) ====== */
function S2Sec({ label, meta, children, collapsible, defaultOpen = true }) {
  const [open, setOpen] = useSS(defaultOpen);
  return (
    <section className={`sfx-sec ${collapsible ? "is-clp" : ""} ${open ? "is-open" : "is-closed"}`}>
      <header className="sfx-h" onClick={collapsible ? () => setOpen(o => !o) : undefined}>
        <span className="sfx-h-label">{label}</span>
        {meta != null && <span className="sfx-h-meta">{meta}</span>}
        {collapsible && <I.ChevronRight size={13} className="sfx-h-chev" />}
      </header>
      {open && <div className="sfx-sec-body">{children}</div>}
    </section>
  );
}

/* ====== Step diagnostics (pipeline · live rubric · acceptance gate) ====== */
function S2Guide({ guide, rubric, checks, onToggle, stepKey, draft, scaffold, target, go, refs, health }) {
  if (!guide) return null;
  const content = s2Content(draft, scaffold);
  const notStarted = !content.trim();
  const sig = s2Signals(content, target);
  const dims = s2ScoreDims(sig);
  const auto = stepKey === "scenes" ? s2SceneAuto(scaffold)
    : stepKey === "planning" ? s2PlanAuto(scaffold, refs && refs.scenes)
    : s2AutoChecks(sig, target);
  const pipe = s2Pipeline(stepKey);
  const overall = Math.round(rubric.reduce((a, r) => a + (dims[r.k] ? dims[r.k].score : 0), 0) / rubric.length);

  const autoPass = auto.filter(a => a.pass).length;
  const done = checks.filter(Boolean).length;
  const manualAll = checks.length > 0 && done === checks.length;
  const gateOpen = autoPass === auto.length && manualAll;
  const passedTotal = autoPass + done;
  const allTotal = auto.length + checks.length;

  return (
    <div className="sfx-guide">
      <div className="sfx-task">
        <div className="sfx-eyebrow">本步任务</div>
        <p className="sfx-task-text">{guide.task}</p>
      </div>

      <S2Sec label="写作指引" meta="分形算子">
        {pipe && (
          <div className="sfx-pipe">
            <button className="sfx-pipe-node" disabled={!pipe.inKey} onClick={() => pipe.inKey && go(pipe.inKey)} title={pipe.inKey ? "回到上游层" : "雪花原点"}>{pipe.inName}</button>
            <span className="sfx-pipe-arr"><I.ArrowRight size={11} /></span>
            <span className="sfx-pipe-cur">本步<b>{pipe.ratio}</b></span>
            <span className="sfx-pipe-arr"><I.ArrowRight size={11} /></span>
            <button className="sfx-pipe-node" disabled={!pipe.outKey} onClick={() => pipe.outKey && go(pipe.outKey)} title={pipe.outKey ? "进入下游层" : "下游为正文"}>{pipe.outName}</button>
          </div>
        )}
        <ol className="sfx-ops">
          {guide.writing.map((w, i) => (
            <li key={i}>
              <span className="sfx-op-idx">{String(i + 1).padStart(2, "0")}</span>
              <div className="sfx-op-body"><span className="sfx-op-k">{w.k}</span><span className="sfx-op-v">{w.v}</span></div>
            </li>
          ))}
        </ol>
        {guide.note && <p className="sfx-note">{guide.note}</p>}
      </S2Sec>

      <S2Sec label="质量标尺 · 实时自评" meta={notStarted ? <span className="sfx-ruler-overall" style={{ color: "var(--ink-3, #8a8a8a)" }}>未开始</span> : <span className="sfx-ruler-overall" style={{ color: s2HC(overall) }}>{overall}<small> / 100</small></span>}>
        {notStarted ? (
          <p className="sfx-ruler-src"><I.Activity size={10} /> 本步还没动笔——开始写后，这里会随草稿实时估算五维健康度。</p>
        ) : (
        <React.Fragment>
        <p className="sfx-ruler-src"><I.Activity size={10} /> 随草稿实时估算 · 与控制塔同一把尺</p>
        <ul className="sfx-ruler">
          {rubric.map((r, i) => {
            const d = dims[r.k] || { score: 0, why: "" };
            return (
              <li key={i} title={d.why}>
                <div className="sfx-ruler-top">
                  <span className="sfx-rk">{r.k}</span>
                  <span className="sfx-ruler-score" style={{ color: s2HC(d.score) }}>{d.score}</span>
                </div>
                <div className="sfx-ruler-bar"><i style={{ width: d.score + "%", background: s2HC(d.score) }} /></div>
                <p className="sfx-rq">{r.q}</p>
              </li>
            );
          })}
        </ul>
        </React.Fragment>
        )}
      </S2Sec>

      {(() => {
        const be = health || null;
        const hasBe = !!(be && (typeof be.score === "number" || (be.missingFields && be.missingFields.length) || be.beStatus));
        const beTone = be && be.status === "pass" ? "sage" : be && be.status === "rewrite" ? "rose" : "gold";
        const beLabel = { pass: "结构达标", maybe: "可改进", rewrite: "建议重写" };
        return (
          <S2Sec label="后端评估 · 权威" meta={hasBe && typeof be.score === "number"
            ? <span className="sfx-ruler-overall" style={{ color: s2HC(be.score) }}>{be.score}<small> / 100</small></span>
            : <span style={{ color: "var(--ink-3, #8a8a8a)", fontSize: 11 }}>待同步</span>}>
            {!hasBe ? (
              <p className="sfx-ruler-src"><I.Cpu size={10} /> 保存本步后，这里显示后端完备性闸门的权威评定（分数 / 缺字段 / 前序闸门），非本地正则估算。</p>
            ) : (
              <React.Fragment>
                <p className="sfx-ruler-src"><I.Cpu size={10} /> 由后端完备性闸门评定 · 保存时更新（非正则估算）</p>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center", margin: "0 0 6px" }}>
                  {be.status && <span style={{ fontSize: 11, padding: "1px 8px", borderRadius: 10, color: `var(--${beTone})`, border: `1px solid var(--${beTone})` }}>{beLabel[be.status] || be.status}</span>}
                  {typeof be.filled === "number" && typeof be.total === "number" && <span style={{ fontSize: 11, color: "var(--ink-3, #8a8a8a)" }}>字段 {be.filled}/{be.total}</span>}
                  <span style={{ fontSize: 11, display: "inline-flex", alignItems: "center", gap: 3, color: be.gateSatisfied ? "var(--sage)" : "var(--gold)" }}>{be.gateSatisfied ? <><I.Unlock size={10} /> 前序闸门满足</> : <><I.Lock size={10} /> 前序未满足</>}</span>
                </div>
                {be.missingFields && be.missingFields.length > 0 && (
                  <p className="sfx-rq" style={{ margin: "0 0 4px" }}><I.AlertTriangle size={10} /> 缺 {be.missingFields.length} 个字段：{be.missingFields.slice(0, 6).join("、")}</p>
                )}
                {be.nextActions && be.nextActions.length > 0 && (
                  <ul className="sfx-autos">
                    {be.nextActions.slice(0, 4).map((a, i) => (
                      <li key={i}><span className="sfx-auto-ic"><I.ArrowRight size={10} /></span><span className="sfx-auto-t">{a}</span></li>
                    ))}
                  </ul>
                )}
              </React.Fragment>
            )}
          </S2Sec>
        );
      })()}

      <S2Sec label="验收门" meta={<span className={`sfx-gate-meta ${gateOpen ? "is-open" : ""}`}>{gateOpen ? <><I.Unlock size={11} /> 门开</> : <><I.Lock size={11} /> {passedTotal}/{allTotal}</>}</span>}>
        <div className="sfx-gate-grp-h"><I.Cpu size={11} /> 机器核验 · 自动 <span className="sfx-gate-grp-c">{autoPass}/{auto.length}</span></div>
        <ul className="sfx-autos">
          {auto.map((a, i) => (
            <li key={i} className={a.pass ? "is-pass" : "is-fail"}>
              <span className="sfx-auto-ic">{a.pass ? <I.Check size={11} /> : <I.AlertTriangle size={10} />}</span>
              <span className="sfx-auto-t">{a.t}</span>
              <span className="sfx-auto-val">{a.pass ? a.val : <>{a.val} · <em>需{a.need}</em></>}</span>
            </li>
          ))}
        </ul>
        <div className="sfx-gate-grp-h"><I.UserCheck size={11} /> 人工确认 <span className="sfx-gate-grp-c">{done}/{checks.length}</span></div>
        <ul className="sfx-checks">
          {guide.checklist.map((c, i) => (
            <li key={i} className={checks[i] ? "is-done" : ""} onClick={() => onToggle(i)}>
              <span className="sfx-cbox">{checks[i] && <I.Check size={11} />}</span>
              <span className="sfx-ctext">{c}</span>
            </li>
          ))}
        </ul>
        <div className={`sfx-gate-foot ${gateOpen ? "is-open" : ""}`}>
          {gateOpen
            ? <><I.CheckCircle size={12} /> 验收通过 · 可确认本步</>
            : <><I.Lock size={12} /> 还差 {allTotal - passedTotal} 项 · 机器 {autoPass}/{auto.length} · 人工 {done}/{checks.length}</>}
        </div>
      </S2Sec>
    </div>
  );
}

/* ====== Freeform editor (+ optional word meter) ====== */
function S2Edit({ draft, setDraft, stepName, target, meter, hints, onAICands }) {
  return (
    <div className="edit-pane">
      <div className="edit-toolbar">
        <div className="flex items-center gap-2">
          <button className="btn btn-quiet btn-sm" onClick={() => onAICands && onAICands()} title="让 AI 读上游材料与诊断缺口，生成 3 条方向候选">
            <I.Wand size={13} /> 让 AI 生成候选
          </button>
        </div>
        <div className="text-muted text-sm">{draft.length} 字{target ? ` · 目标约 ${target}` : ""}</div>
      </div>
      {meter && <S2Meter len={draft.length} target={meter.target} note={meter.note} />}
      <textarea className="edit-text" value={draft} onChange={(e) => setDraft(e.target.value)} placeholder={`在这里写「${stepName}」…`} />
      <div className="edit-hints">
        {(hints && hints.length ? hints : [{ icon: "Info", tone: "slate", text: "随时可以让 AI 基于上游步骤生成候选，再挑一条采纳。" }]).map((h, i) => (
          <S2Hint key={i} icon={h.icon} tone={h.tone} text={h.text} />
        ))}
      </div>
    </div>
  );
}

function S2Meter({ len, target, note }) {
  const pct = Math.min(100, (len / target) * 100);
  const over = len > target;
  return (
    <div className={`sf-meter ${over ? "is-over" : ""}`}>
      <div className="sf-meter-track"><div className="sf-meter-fill" style={{ width: pct + "%" }} /><div className="sf-meter-cap" style={{ left: "100%" }} /></div>
      <div className="sf-meter-foot">
        <span className="sf-meter-count">{len} / {target} 字{over ? " · 偏长，再砍一刀" : ""}</span>
        <span className="sf-meter-note">{note}</span>
      </div>
    </div>
  );
}

function S2Hint({ icon, tone, text }) {
  const Ic = I[icon] || I.Info;
  return <div className={`hint hint-${tone}`}><Ic size={14} /><span>{text}</span></div>;
}

/* 采纳候选后的自由草稿，在有脚手架的步骤上可见可编可退——
   它会优先于脚手架参与评分 / 引用 / 导出，所以必须明示，不能藏在水面下 */
function S2DraftOverride({ draft, setDraft, stepName }) {
  const clear = () => {
    if (!window.confirm(`清除这段自由草稿？本步将回到结构化脚手架作为唯一内容源。`)) return;
    setDraft("");
  };
  return (
    <div className="sf-dov">
      <div className="sf-dov-head">
        <span className="sf-dov-tag"><I.Wand size={12} /> 自由草稿（采纳候选所得）</span>
        <span className="sf-dov-note">只要这段非空，本步的评分、引用与导出都优先用它，而非下方脚手架。</span>
        <button className="btn btn-quiet btn-sm" onClick={clear} title="清除草稿，回到脚手架"><I.X size={12} /> 清除草稿</button>
      </div>
      <textarea className="sf-dov-text" rows={4} value={draft} onChange={(e) => setDraft(e.target.value)} placeholder={`「${stepName}」的自由草稿…`} />
    </div>
  );
}

/* ====== Structured scaffolds ====== */
function S2Scaffold({ kind, scaffold, onScaffold, hints, refs, go, ai }) {
  return (
    <div className="edit-pane">
      {kind === "beats" && <S2Beats scaffold={scaffold} onScaffold={onScaffold} />}
      {kind === "audience" && <S2Audience scaffold={scaffold} onScaffold={onScaffold} />}
      {kind === "charsheet" && <S2CharSheet scaffold={scaffold} onScaffold={onScaffold} ai={ai} />}
      {kind === "synopsisbeats" && <S2SynopsisBeats scaffold={scaffold} onScaffold={onScaffold} refs={refs} />}
      {kind === "chapters" && <S2ChapterOutline scaffold={scaffold} onScaffold={onScaffold} refs={refs} />}
      {kind === "backstory" && <S2CharDeep scaffold={scaffold} onScaffold={onScaffold} ai={ai} fields={S2_BACKSTORY_FIELDS} roster={(refs && refs.characters) || null} go={go} note={<><b>角色继承自 04 角色摘要表</b>，在这里为每人写半页来路——不是户口簿，是那件把她变成今天的事。</>} icon="BookOpen" />}
      {kind === "profile" && <S2CharDeep scaffold={scaffold} onScaffold={onScaffold} ai={ai} fields={S2_PROFILE_FIELDS} roster={(refs && refs.characters) || null} go={go} note={<><b>角色继承自 04 角色摘要表</b>，为每人建一份「角色圣经」：四维度 + 矛盾 + 两个版本的她。</>} icon="Users" />}
      {kind === "scenelist" && <S2SceneList scaffold={scaffold} onScaffold={onScaffold} refs={refs} ai={ai} />}
      {kind === "scene" && <S2ScenePlan scaffold={scaffold} onScaffold={onScaffold} refs={refs} go={go} ai={ai} />}
      <div className="edit-hints">
        {(hints || []).map((h, i) => <S2Hint key={i} icon={h.icon} tone={h.tone} text={h.text} />)}
      </div>
    </div>
  );
}

const S2_BEATS = [
  { f: "setup",      label: "铺垫",   act: "开场",      desc: "交代背景，引入 1–2 位主角" },
  { f: "d1",         label: "灾难一", act: "第一幕末",  desc: "逼主角入局、做出承诺", tone: "crimson" },
  { f: "d2",         label: "灾难二", act: "第二幕中点", desc: "道德前提翻转：错误信念 → 正确信念", tone: "gold", flip: true },
  { f: "d3",         label: "灾难三", act: "第二幕末",  desc: "逼主角（与反派）走向终局", tone: "crimson" },
  { f: "resolution", label: "结局",   act: "第三幕",    desc: "终极对决 + 收束（喜 / 悲 / 苦甜）" },
];
function S2Beats({ scaffold, onScaffold }) {
  return (
    <div className="sf-scaffold sf-beats">
      <div className="sf-scaffold-note">
        <I.GitBranch size={14} />
        <span>雪花核心：一句话 → 五句话。五句即三幕骨架，<b>三个灾难逐级抬高</b>，第二个灾难把道德前提从错翻成对。</span>
      </div>
      {S2_BEATS.map((b, i) => (
        <div key={b.f} className={`sf-beat ${b.tone ? `tone-${b.tone}` : ""}`}>
          <div className="sf-beat-side">
            <span className="sf-beat-idx">{i + 1}</span>
            <span className="sf-beat-act">{b.act}</span>
          </div>
          <div className="sf-beat-main">
            <div className="sf-beat-label">{b.label}<span className="sf-beat-desc">{b.desc}</span></div>
            <textarea className="sf-beat-text" rows={2} value={scaffold[b.f] || ""}
              onChange={(e) => onScaffold(s => ({ ...s, [b.f]: e.target.value }))} placeholder={`写「${b.label}」…`} />
            {b.flip && (
              <div className="sf-premise-flip">
                <span className="sf-pf-tag">道德前提</span>
                <input className="sf-pf-input is-false" value={scaffold.premiseF || ""} onChange={(e) => onScaffold(s => ({ ...s, premiseF: e.target.value }))} placeholder="错误信念…" />
                <I.ArrowRight size={13} />
                <input className="sf-pf-input is-true" value={scaffold.premiseT || ""} onChange={(e) => onScaffold(s => ({ ...s, premiseT: e.target.value }))} placeholder="正确信念…" />
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

const S2_CHAR_FIELDS = [
  { f: "role",     label: "角色",          hint: "主角 / 对立面 / 导师 / 帮手…", short: true },
  { f: "goal",     label: "目标（具体）",  hint: "这个故事里她要的、看得见的东西" },
  { f: "ambition", label: "抱负（抽象）",  hint: "她对人生说不出口的渴望" },
  { f: "values",   label: "价值观",        hint: "「没有什么比 ___ 更重要」", prefix: "没有什么比", suffix: "更重要" },
  { f: "conflict", label: "阻碍",          hint: "什么挡在她和目标之间" },
  { f: "epiphany", label: "顿悟",          hint: "故事结束时她学到什么（反派常无）" },
];
function S2CharSheet({ scaffold, onScaffold, ai }) {
  const ids = Object.keys(scaffold.chars);
  const sel = scaffold.chars[scaffold.sel] ? scaffold.sel : ids[0];
  const ch = scaffold.chars[sel] || {};
  const addChar = () => onScaffold(s => {
    let n = 1; while (s.chars["c" + n]) n++;
    const id = "c" + n;
    return { ...s, sel: id, chars: { ...s.chars, [id]: { name: "新角色", role: "次要", goal: "", ambition: "", values: "", conflict: "", epiphany: "" } } };
  });
  const delChar = () => {
    if (ids.length <= 1) { window.alert("至少保留一个角色。"); return; }
    if (!window.confirm(`删除角色「${ch.name || "未命名"}」？06 / 08 中她的深档字段会保留但不再展示。`)) return;
    onScaffold(s => {
      const chars = { ...s.chars }; delete chars[sel];
      return { ...s, sel: Object.keys(chars)[0], chars };
    });
  };
  return (
    <div className="sf-scaffold sf-charsheet">
      <div className="sf-scaffold-note">
        <I.Users size={14} />
        <span>雪花第 3 步：每个主要角色一张摘要表。<b>这里是全书角色花名册的唯一真相源</b>——06 背景与 08 全档案的名册都继承自这里。</span>
      </div>
      <div className="sf-char-tabs">
        {ids.map(id => {
          const c = scaffold.chars[id];
          return (
            <button key={id} className={`sf-char-tab ${sel === id ? "is-sel" : ""}`} onClick={() => onScaffold(s => ({ ...s, sel: id }))}>
              <span className="sf-char-av text-serif">{(c.name || "?")[0]}</span>
              <span className="sf-char-tab-body"><span className="sf-char-tab-name">{c.name || "未命名"}</span><span className="sf-char-tab-role">{c.role}</span></span>
            </button>
          );
        })}
        <button className="sf-char-add" onClick={addChar} title="添加角色（06/08 名册同步继承）"><I.Plus size={15} /></button>
      </div>
      <div className="sf-chardeep-head">
        <input className="sf-chardeep-name" value={ch.name || ""} placeholder="角色名"
          onChange={(e) => onScaffold(s => ({ ...s, chars: { ...s.chars, [sel]: { ...s.chars[sel], name: e.target.value } } }))} />
        {ai && (
          <button className="btn btn-quiet btn-sm" disabled={ai.structBusy} onClick={() => ai.onFillChar(sel, ch.name)}
            title="只让 AI 补全当前选中的这个角色——其余角色保持不动（依据上游材料，与其他角色保持一致）">
            <I.Wand size={13} className={ai.structBusy ? "sf-spin" : ""} /> {ai.structBusy ? "生成中…" : "AI 补全此角色"}
          </button>
        )}
        <button className="btn btn-quiet btn-sm" onClick={delChar} title="删除这个角色"><I.X size={13} /> 删除角色</button>
      </div>
      <div className="sf-fields">
        {S2_CHAR_FIELDS.map(fl => (
          <label key={fl.f} className={`sf-field ${fl.short ? "is-short" : ""}`}>
            <span className="sf-field-label">{fl.label}<span className="sf-field-hint">{fl.hint}</span></span>
            {fl.prefix ? (
              <span className="sf-field-affix">
                <span className="sf-affix">{fl.prefix}</span>
                <input className="sf-field-input" value={ch[fl.f] || ""}
                  onChange={(e) => onScaffold(s => ({ ...s, chars: { ...s.chars, [sel]: { ...s.chars[sel], [fl.f]: e.target.value } } }))} />
                <span className="sf-affix">{fl.suffix}</span>
              </span>
            ) : (
              <input className="sf-field-input" value={ch[fl.f] || ""}
                onChange={(e) => onScaffold(s => ({ ...s, chars: { ...s.chars, [sel]: { ...s.chars[sel], [fl.f]: e.target.value } } }))} />
            )}
          </label>
        ))}
      </div>
    </div>
  );
}

/* ---- 06 角色背景 / 08 角色全档案：按角色分栏的深档编辑器（共用） ---- */
const S2_BACKSTORY_FIELDS = [
  { f: "belief",   label: "信念起点",   hint: "故事开始前她相信什么？怎么形成的？" },
  { f: "wound",    label: "第一道裂缝", hint: "哪件事第一次动摇了她——她的旧伤" },
  { f: "desire",   label: "内心渴望",   hint: "她真正渴望的是什么？为何渴望" },
  { f: "fear",     label: "隐秘恐惧",   hint: "最怕被人发现什么——故事将击中的靶心" },
  { f: "relation", label: "关系与行为", hint: "与其他角色的纠葛；压力下她会怎么做" },
];
const S2_PROFILE_FIELDS = [
  { f: "physical",      label: "生理",       hint: "外貌、习惯、标志性细节" },
  { f: "psych",         label: "心理",       hint: "核心恐惧、渴望、创伤" },
  { f: "environment",   label: "环境",       hint: "家庭、工作、人际" },
  { f: "personality",   label: "性格",       hint: "口头禅、矛盾面" },
  { f: "contradiction", label: "内在矛盾",   hint: "嘴上说的 vs 实际做的", accent: true },
  { f: "views",         label: "两个版本的她", hint: "别人眼中的她 ／ 她自己眼中的她", accent: true },
];
const S2_ROLE_TONE = { "主角": "crimson", "对立面": "gold", "次要": "slate", "导师": "slate", "帮手": "sage" };
function S2CharDeep({ scaffold, onScaffold, fields, note, icon, roster, go, ai }) {
  /* 名册的唯一真相源是 04 角色摘要表；本步只存自己这一层的深档字段。
     （本地遗留的、不在 04 名册里的角色仍展示，但标记出来） */
  const rosterChars = (roster && roster.chars) || {};
  const rosterIds = Object.keys(rosterChars);
  const localIds = Object.keys(scaffold.chars || {});
  const legacyIds = localIds.filter(id => !rosterChars[id] && fields.some(fl => ((scaffold.chars[id] || {})[fl.f] || "").trim()));
  const ids = [...rosterIds, ...legacyIds];
  const sel = ids.includes(scaffold.sel) ? scaffold.sel : ids[0];
  const ch = (scaffold.chars || {})[sel] || {};
  const meta = rosterChars[sel] || ch; // name/role 优先取 04
  const Ic = I[icon] || I.Users;
  const setField = (f, v) => onScaffold(s => ({ ...s, chars: { ...s.chars, [sel]: { ...(s.chars[sel] || {}), [f]: v } } }));
  const filledCount = (id) => fields.filter(fl => (((scaffold.chars || {})[id] || {})[fl.f] || "").trim()).length;
  if (!ids.length) {
    return (
      <div className="sf-scaffold sf-chardeep">
        <div className="sf-plan-empty">
          <I.Users size={20} />
          <div>
            <div className="fw-600">名册还是空的</div>
            <div className="text-muted text-sm">角色名册由 04 角色摘要表统一管理——先去那里立人。</div>
          </div>
          <button className="btn btn-primary btn-sm" onClick={() => go && go("characters")}>去 04 · 角色摘要表</button>
        </div>
      </div>
    );
  }
  return (
    <div className="sf-scaffold sf-chardeep">
      <div className="sf-scaffold-note">
        <Ic size={14} />
        <span>{note}</span>
      </div>
      <div className="sf-roster">
        <div className="sf-roster-lead"><I.Users size={12} /> 角色花名册 · {ids.length} 人<span className="sf-roster-src">名册与姓名由 04 统一管理</span></div>
        <div className="sf-char-tabs">
          {ids.map(id => {
            const m = rosterChars[id] || (scaffold.chars || {})[id] || {};
            const fc = filledCount(id);
            return (
              <button key={id} className={`sf-char-tab tone-${S2_ROLE_TONE[m.role] || "slate"} ${sel === id ? "is-sel" : ""}`} onClick={() => onScaffold(s => ({ ...s, sel: id }))}>
                <span className="sf-char-av text-serif">{(m.name || "?")[0]}</span>
                <span className="sf-char-tab-body">
                  <span className="sf-char-tab-name">{m.name || "未命名"}{!rosterChars[id] && <em className="sf-char-legacy" title="这个角色不在 04 名册里（历史数据）">·遗留</em>}</span>
                  <span className="sf-char-tab-role">{m.role || "—"} · {fc}/{fields.length}</span>
                </span>
              </button>
            );
          })}
          <button className="sf-char-add" onClick={() => go && go("characters")} title="名册由 04 管理——去 04 添加角色"><I.Plus size={15} /></button>
        </div>
      </div>
      <div className="sf-chardeep-head">
        <span className="sf-chardeep-name is-ro" title="姓名与定位继承自 04 角色摘要表">{meta.name || "未命名"}</span>
        <span className="sf-chardeep-role is-ro">{meta.role || "—"}</span>
        {ai && (
          <button className="btn btn-quiet btn-sm" disabled={ai.structBusy} onClick={() => ai.onFillChar(sel, meta.name)}
            title="只让 AI 补全当前选中的这个角色——其余角色保持不动（依据上游材料，与其他角色保持一致）">
            <I.Wand size={13} className={ai.structBusy ? "sf-spin" : ""} /> {ai.structBusy ? "生成中…" : "AI 补全此角色"}
          </button>
        )}
        <button className="sf-lineage" onClick={() => go && go("characters")} title="改名 / 改定位 / 增删角色，都在 04">
          <I.ArrowRight size={11} style={{ transform: "rotate(180deg)" }} /> 名册管理在 04
        </button>
      </div>
      <div className="sf-deep-fields">
        {fields.map(fl => (
          <label key={fl.f} className={`sf-deep-field ${fl.accent ? "is-accent" : ""}`}>
            <span className="sf-field-label">{fl.label}<span className="sf-field-hint">{fl.hint}</span></span>
            <textarea className="sf-deep-text" rows={2} value={ch[fl.f] || ""} onChange={(e) => setField(fl.f, e.target.value)} placeholder={`写「${fl.label}」…`} />
          </label>
        ))}
      </div>
    </div>
  );
}

/* ---- 01 读者定位：类型 / 读者画像 / 核心快感 / 来源 / 反向定位 ---- */
const S2_AUD_GENRES = ["文学悬疑", "言情", "硬核推理", "科幻", "奇幻", "历史", "青春", "惊悚"];
const S2_AUD_FIELDS = [
  { f: "reader",   label: "读者画像", hint: "谁？年龄、阅读口味、她为何被这种故事吸引", rows: 2 },
  { f: "pleasure", label: "核心快感", hint: "用「她读完会觉得 ___」一句话锁定", rows: 2, accent: true },
  { f: "source",   label: "快感来源", hint: "这种快感具体从哪来——叙述、主题、节奏？", rows: 2 },
  { f: "emotion",  label: "期待读者情绪", hint: "压力升级中，读者持续感到什么——揪心、压迫、向前的拉力？", rows: 2 },
  { f: "exclude",  label: "反向定位", hint: "「我不为谁写 / 不写什么」——砍掉犹豫", rows: 2, danger: true },
];
function S2Audience({ scaffold, onScaffold }) {
  const set = (f, v) => onScaffold(s => ({ ...s, [f]: v }));
  const filled = ["genre", ...S2_AUD_FIELDS.map(f => f.f)].filter(k => (scaffold[k] || "").trim()).length;
  return (
    <div className="sf-scaffold sf-audience">
      <div className="sf-scaffold-note">
        <I.Target size={14} />
        <span>雪花从<b>定锚</b>开始：先定类型(=承诺)，再用一句话锁定核心快感。这把尺子，后面九步每次取舍都要用。</span>
      </div>

      <div className="sf-aud-genre">
        <div className="sf-field-label">类型<span className="sf-field-hint">类型决定读者带着什么期待打开书</span></div>
        <div className="sf-genre-chips">
          {S2_AUD_GENRES.map(g => (
            <button key={g} className={`sf-genre-chip ${scaffold.genre === g ? "is-sel" : ""}`} onClick={() => set("genre", g)}>{g}</button>
          ))}
          <input className="sf-genre-other" value={S2_AUD_GENRES.includes(scaffold.genre) ? "" : (scaffold.genre || "")} onChange={(e) => set("genre", e.target.value)} placeholder="其他…" />
        </div>
      </div>

      <div className="sf-aud-fields">
        {S2_AUD_FIELDS.map(fl => (
          <label key={fl.f} className={`sf-deep-field ${fl.accent ? "is-accent" : ""} ${fl.danger ? "is-danger" : ""}`}>
            <span className="sf-field-label">{fl.label}<span className="sf-field-hint">{fl.hint}</span></span>
            <textarea className="sf-deep-text" rows={fl.rows} value={scaffold[fl.f] || ""} onChange={(e) => set(fl.f, e.target.value)} placeholder={`写「${fl.label}」…`} />
          </label>
        ))}
      </div>

      <div className="sf-aud-foot">
        <span className={`sf-aud-prog ${filled === S2_AUD_FIELDS.length + 1 ? "is-all" : ""}`}><I.Target size={11} /> 定位完成度 {filled} / {S2_AUD_FIELDS.length + 1}</span>
        {scaffold.genre && scaffold.pleasure ? (
          <span className="sf-aud-seal"><I.Check size={11} /> 已锚定：<b>{scaffold.genre}</b> · 取悦「{(scaffold.pleasure || "").slice(0, 14)}…」的读者</span>
        ) : (
          <span className="sf-aud-seal is-pending">把类型和核心快感都填上，定位才算锚定</span>
        )}
      </div>
    </div>
  );
}

/* ---- 05 一页梗概：五段，每段锚定 03 的一句脊柱节拍（1→5 分形展开可见） ---- */
const S2_SYN_BEATS = [
  { f: "setup",      label: "铺垫",   ref: "setup", tone: "slate",   desc: "世界观与初始处境" },
  { f: "d1",         label: "灾难一", ref: "d1",    tone: "crimson", desc: "触发事件 · 第一幕末" },
  { f: "d2",         label: "灾难二", ref: "d2",    tone: "gold",    desc: "认知翻转 · 中点" },
  { f: "d3",         label: "灾难三", ref: "d3",    tone: "crimson", desc: "升级 · 第二幕末" },
  { f: "resolution", label: "结局",   ref: "resolution", tone: "slate", desc: "高潮走向与收尾" },
];
function S2SynopsisBeats({ scaffold, onScaffold, refs }) {
  const paras = scaffold.paras || {};
  const para03 = (refs && refs.paragraph) || {};
  const setPara = (f, v) => onScaffold(s => ({ ...s, paras: { ...s.paras, [f]: v } }));
  const filled = S2_SYN_BEATS.filter(b => (paras[b.f] || "").trim()).length;
  return (
    <div className="sf-scaffold sf-synopsis">
      <div className="sf-scaffold-note">
        <I.GitBranch size={14} />
        <span><b>五段 = 五句的展开。</b>每段顶部是它要展开的 03 那一句（只读引用），下面把它扩成一段有画面的梗概。</span>
      </div>
      <div className="sf-syn-prog">
        <span className="sf-syn-prog-c"><b>{filled}</b> / 5 段已展开</span>
        <div className="sf-syn-track">{S2_SYN_BEATS.map(b => <span key={b.f} className={`sf-syn-tick tone-${b.tone} ${(paras[b.f] || "").trim() ? "is-on" : ""}`} />)}</div>
      </div>
      {S2_SYN_BEATS.map((b, i) => {
        const src = para03[b.ref] || "";
        const expanded = (paras[b.f] || "");
        const grew = expanded.replace(/\s/g, "").length > src.replace(/\s/g, "").length;
        return (
          <div key={b.f} className={`sf-syn-row tone-${b.tone}`}>
            <div className="sf-syn-side">
              <span className="sf-syn-idx">{i + 1}</span>
              <span className="sf-syn-label">{b.label}</span>
              <span className="sf-syn-desc">{b.desc}</span>
            </div>
            <div className="sf-syn-main">
              <div className="sf-syn-src" title="展开自 03 一段话概括的这一句">
                <span className="sf-syn-src-tag"><I.ArrowRight size={10} style={{ transform: "rotate(180deg)" }} /> 展开自 03</span>
                <span className="sf-syn-src-text">{src || <em className="sf-syn-empty">（03 这一拍还没写）</em>}</span>
              </div>
              <textarea className="sf-syn-text" rows={3} value={expanded} onChange={(e) => setPara(b.f, e.target.value)} placeholder={`把「${b.label}」扩成一段有画面的梗概…`} />
              {expanded.trim() && (
                <div className={`sf-syn-meta ${grew ? "is-ok" : "is-warn"}`}>
                  {grew ? <><I.Check size={10} /> 已展开（{expanded.replace(/\s/g, "").length} 字 &gt; 源句 {src.replace(/\s/g, "").length}）</> : <><I.AlertTriangle size={10} /> 还没比源句长——再填点画面</>}
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ---- 07 长篇大纲：三幕 · 章节表（对应后端 ChapterGoal；章定不了，场就拆不开）---- */
const S2_ACTS = [
  { act: 1, label: "第一幕", desc: "铺垫 → 灾难一", tone: "slate" },
  { act: 2, label: "第二幕", desc: "灾难二（中点翻转）", tone: "gold" },
  { act: 3, label: "第三幕", desc: "灾难三 → 收尾", tone: "crimson" },
];
function S2ChapterOutline({ scaffold, onScaffold, refs }) {
  const chapters = scaffold.chapters || [];
  const setCh = (id, f, v) => onScaffold(s => ({ ...s, chapters: s.chapters.map(c => c.id === id ? { ...c, [f]: v } : c) }));
  const delCh = (id) => onScaffold(s => ({ ...s, chapters: s.chapters.filter(c => c.id !== id) }));
  const addCh = (act) => onScaffold(s => {
    const max = s.chapters.reduce((m, c) => Math.max(m, parseInt(c.id, 10) || 0), 0);
    const nid = String(max + 1).padStart(2, "0");
    return { ...s, chapters: [...s.chapters, { id: nid, act, title: "（待补）", summary: "", spine: "" }] };
  });
  const spineHits = chapters.filter(c => c.spine).length;
  const placeholders = chapters.filter(c => !c.summary.trim() || c.title.includes("待补")).length;
  /* 采用到章节编排 = 打开同一个分章预览面板（P2 路径合一）。
     以前这里和顶部按钮共用 s2AdoptOutline，但那条契约按闸门状态在三种落库路径之间
     分叉，结果同一个动作在不同状态下产出完全不同的章节结构。现在两个入口一条路。 */
  const [adopted, setAdopted] = useSS(null);
  const [planOpen, setPlanOpen] = useSS(false);
  const adoptable = chapters.filter(c => (c.title || "").trim() && !c.title.includes("待补"));
  const adopt = () => setPlanOpen(true);
  /* 并入成功后的第二动线：把已规划好的 todo 场批量送进 AI 起草台（入列后跳转） */
  const goDraft = async () => {
    try {
      if (WsCatalog && WsCatalog.__refresh) await WsCatalog.__refresh();
      const sids = [];
      (WsCatalog ? WsCatalog.get() : []).forEach(c => (c.scenes || []).forEach(s => {
        if (s.sid && s.state !== "done" && (s.goal || "").trim() && !(s.goal || "").includes("待规划")) sids.push(s.sid);
      }));
      if (sids.length) {
        navigateWithViewIntent("scene", "ws:scene-enqueue", { sids: sids.slice(0, 40) });
        return;
      }
    } catch (e) {}
    location.hash = "#scene";
  };
  return (
    <div className="sf-scaffold sf-chapters">
      {planOpen && (
        <WsChapterPlanPanel
          onClose={() => setPlanOpen(false)}
          onDone={(result) => { setPlanOpen(false); setAdopted((result && result.created_chapter_count) || 0); }}
          onGoToStep={(beKey) => {
            const pair = S2_BE_STEPS.find(([, candidate]) => candidate === beKey);
            setPlanOpen(false);
            if (pair) window.dispatchEvent(new CustomEvent("ws:snow-step", { detail: pair[0] }));
          }}
        />
      )}
      <div className="sf-scaffold-note">
        <I.Layers size={14} />
        <span>第三次展开：把一页梗概拆成<b>三幕章节</b>。三个灾难必须落在幕与幕的交界——它们是结构的铰链。</span>
      </div>
      <div className="sf-scene-stats">
        <span className="sf-sstat"><b>{chapters.length}</b> 章</span>
        <span className="sf-sstat tone-gold"><b>{spineHits}</b> 脊柱落点</span>
        <span className={`sf-sstat ${placeholders ? "tone-rose" : "tone-sage"}`}>{placeholders ? <><I.AlertTriangle size={11} /> {placeholders} 章占位待补</> : <><I.Check size={11} /> 骨架已立</>}</span>
        <span style={{ flex: 1 }} />
        {adopted == null ? (
          <button className="btn btn-quiet btn-sm" data-testid="snow-materialize" onClick={adopt} disabled={!adoptable.length} title="把这份大纲落进章节编排 / 写作目录，不用再手工重建">
            <I.Layout size={13} /> 采用到章节编排
          </button>
        ) : (
          <>
            <button className="btn btn-quiet btn-sm" onClick={() => { location.hash = "#author"; }}>
              <I.Check size={13} /> {adopted ? `已并入 ${adopted} 章` : "无新增（同名已存在）"} · 去编排查看
            </button>
            <button className="btn btn-accent btn-sm" data-testid="snow-go-draft" onClick={goDraft} title="把已规划好的场批量送进 AI 起草台排队，按场景卡三拍与雪花上下文起草整场">
              <I.Play size={13} /> 去 AI 起草
            </button>
          </>
        )}
      </div>
      {S2_ACTS.map(a => {
        const list = chapters.filter(c => c.act === a.act);
        return (
          <div key={a.act} className={`sf-act tone-${a.tone}`}>
            <div className="sf-act-head">
              <span className="sf-act-bar" />
              <span className="sf-act-label">{a.label}</span>
              <span className="sf-act-desc">{a.desc}</span>
              <span className="sf-act-count">{list.length} 章</span>
            </div>
            <div className="sf-ch-list">
              {list.map(c => (
                <div key={c.id} className={`sf-ch-row ${c.spine ? "is-spine" : ""} ${(!c.summary.trim() || c.title.includes("待补")) ? "is-ph" : ""}`}>
                  <input className="sc-in sf-ch-id" value={c.id} onChange={(e) => setCh(c.id, "id", e.target.value)} />
                  <div className="sf-ch-body">
                    <input className="sc-in sf-ch-title" value={c.title} onChange={(e) => setCh(c.id, "title", e.target.value)} placeholder="章标题" />
                    <input className="sc-in sf-ch-sum" value={c.summary} onChange={(e) => setCh(c.id, "summary", e.target.value)} placeholder="这一章把局面推到哪——一句话" />
                  </div>
                  <select className="sc-spine sf-ch-spine" value={c.spine} onChange={(e) => setCh(c.id, "spine", e.target.value)} title="绑定脊柱灾难">
                    {S2_SPINE_OPTS.map(o => <option key={o} value={o}>{o || "—"}</option>)}
                  </select>
                  <button className="sc-act sc-act-del" onClick={() => delCh(c.id)} title="删除本章"><I.X size={13} /></button>
                </div>
              ))}
              <button className="sf-ch-add" onClick={() => addCh(a.act)}><I.Plus size={13} /> 添加 {a.label}章节</button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ---- 09 场景列表：结构化场景表（一行一场 · 织线 · 主动/反应节奏） ----
   把雪花的分形原则落到底层：主线与支线在这里编织，主动/反应交替成呼吸。
   两个诊断都与右栏同源——确定性、可解释、可机检。 */
const S2_SPINE_OPTS = ["", "灾一", "灾二", "灾三"];
const S2_LINE_TONES = ["gold", "slate", "sage"];  // 非主线循环配色
const S2_KIND_LABEL = { main: "主线", thread: "线索", sub: "支线" };

// 连续同类型的“跑动”：主动跑太长 = 紧绷；反应跑太长 = 松散
function s2PacingRuns(list) {
  const runs = [];
  (list || []).forEach((s, i) => {
    const t = s.type === "proactive" ? "pro" : "rea";
    const last = runs[runs.length - 1];
    if (last && last.t === t) { last.len++; last.end = i; }
    else runs.push({ t, len: 1, start: i, end: i });
  });
  const tight = runs.filter(r => r.t === "pro" && r.len >= 3);
  const slack = runs.filter(r => r.t === "rea" && r.len >= 3);
  return { runs, tight, slack };
}

// 每条线在全书的分布：出现位置、跨度、是否扎堆、是否缺“折射道德前提”
function s2LineStats(list, lines) {
  const n = (list || []).length || 1;
  return (lines || []).map(ln => {
    const pos = [];
    (list || []).forEach((s, i) => { if ((s.line || "main") === ln.id) pos.push(i); });
    const count = pos.length;
    const span = count ? (pos[count - 1] - pos[0] + 1) : 0;
    const clustered = count >= 2 && span / n < 0.34;            // 像“绕路”而非“编织”
    const noRefract = ln.kind !== "main" && !(ln.refract || "").trim();
    return { ...ln, pos, count, span, clustered, noRefract };
  });
}
/* POV 显示/选择：场景的 pov 可能存的是角色 id（真实项目水合自 pov_character_id，
   形如 <project>_CHAR01）或姓名（演示种子 / 手填）。名册（04 步）以 character_id 为键、
   值含 name。统一解析成显示姓名；下拉选择存回角色 id，与后端 pov_character_id 对齐。 */
function s2RosterList(refs) {
  const chars = ((refs && refs.characters) || {}).chars || {};
  return Object.entries(chars).map(([id, c]) => ({ id, name: ((c && c.name) || "").trim() || id }));
}
function s2PovLabel(pov, roster) {
  if (!pov) return "";
  const hit = (roster || []).find(r => r.id === pov);
  return hit ? hit.name : pov;  // 已是姓名 / 自由文本 → 原样
}
/* 场景显示号：s.id 是不可变身份（真实项目里是 row_<uuid>，不宜直接示人）。
   已是 Sxx / 纯数字则规范化，否则按位置给个友好的 S01 号。 */
function s2SceneNo(id, idx) {
  const s = String(id || "").trim();
  if (/^S\d{1,3}$/i.test(s)) return "S" + s.slice(1).padStart(2, "0");
  if (/^\d{1,3}$/.test(s)) return "S" + s.padStart(2, "0");
  return "S" + String((idx || 0) + 1).padStart(2, "0");
}

/* POV 选择器：名册非空 → 下拉（value=角色id / label=姓名；名册对不上的旧值保留为
   独立项，不丢内容）；名册为空（04 还没建角色）→ 退化成自由文本框，避免卡死作者。 */
function S2PovPick({ value, roster, onChange, className, placeholder }) {
  if (!roster || !roster.length) {
    return <input className={className} value={value || ""} onChange={(e) => onChange(e.target.value)} placeholder={placeholder || "POV"} />;
  }
  const known = roster.find(r => r.id === value) || roster.find(r => r.name === value);
  const unknown = value && !known;
  return (
    <select className={className} value={known ? known.id : (value || "")} onChange={(e) => onChange(e.target.value)} title="选择 POV 视角角色（来自 04 角色名册）">
      <option value="">— POV —</option>
      {roster.map(r => <option key={r.id} value={r.id}>{r.name}</option>)}
      {unknown && <option value={value}>{value}</option>}
    </select>
  );
}

function S2SceneList({ scaffold, onScaffold, refs, ai }) {
  const list = scaffold.list || [];
  const lines = (scaffold.lines && scaffold.lines.length) ? scaffold.lines : [{ id: "main", name: "主线", kind: "main", tone: "crimson", refract: "" }];
  const roster = s2RosterList(refs);
  const placeOpts = [...new Set(list.map(s => (s.place || "").trim()).filter(Boolean))];
  const [hiLine, setHiLine] = useSS(null);
  /* 道德前提读 03 的活数据，不再用静态种子；默认 POV 取 04 名册的主角 */
  const para = (refs && refs.paragraph) || {};
  const premise = { f: (para.premiseF || "").trim(), t: (para.premiseT || "").trim() };
  const mainCharId = (() => {
    const entry = roster.find(r => {
      const chars = ((refs && refs.characters) || {}).chars || {};
      return (chars[r.id] || {}).role === "主角";
    }) || roster[0];
    return (entry && entry.id) || "";
  })();

  const setScene = (i, f, v) => onScaffold(s => ({ ...s, list: s.list.map((sc, j) => j === i ? { ...sc, [f]: v } : sc) }));
  const addScene = () => onScaffold(s => ({
    ...s,
    list: [...s.list, { id: s2NextSceneRowId(s.list), type: "proactive", line: hiLine || "main", pov: mainCharId, place: "", event: "", crucible: "", fn: "", spine: "" }],
  }));
  const delScene = (i) => onScaffold(s => ({ ...s, list: s.list.filter((_, j) => j !== i) }));
  const moveScene = (i, d) => onScaffold(s => {
    const j = i + d; if (j < 0 || j >= s.list.length) return s;
    const l = s.list.slice(); [l[i], l[j]] = [l[j], l[i]]; return { ...s, list: l };
  });

  const setLine = (id, f, v) => onScaffold(s => ({ ...s, lines: (s.lines || []).map(ln => ln.id === id ? { ...ln, [f]: v } : ln) }));
  const addLine = () => onScaffold(s => {
    const subs = (s.lines || []).filter(l => l.kind !== "main").length;
    return { ...s, lines: [...(s.lines || []), { id: "L" + Date.now().toString(36).slice(-4), name: "新支线", kind: "sub", tone: S2_LINE_TONES[subs % S2_LINE_TONES.length], refract: "" }] };
  });
  const delLine = (id) => onScaffold(s => ({
    ...s,
    lines: (s.lines || []).filter(l => l.id !== id),
    list: (s.list || []).map(sc => (sc.line === id ? { ...sc, line: "main" } : sc)),
  }));
  const toneOf = (id) => (lines.find(l => l.id === id) || {}).tone || "slate";

  const pacing = s2PacingRuns(list);
  const lineStats = s2LineStats(list, lines);
  const pro = list.filter(s => s.type === "proactive").length;
  const rea = list.length - pro;
  const noCrucible = list.filter(s => !(s.crucible || "").trim()).length;
  const spineHit = list.filter(s => s.spine).length;
  const tightMax = pacing.tight.length ? Math.max(...pacing.tight.map(r => r.len)) : 0;
  const slackMax = pacing.slack.length ? Math.max(...pacing.slack.map(r => r.len)) : 0;


  return (
    <div className="sf-scaffold sf-scenelist">
      <div className="sf-scaffold-note">
        <I.List size={14} />
        <span>分形展开接近底层：把大纲拆成<b>一行一场</b>。每场都要有坩埚（困住角色的冲突）；主线与支线在此<b>编织</b>，主动 / 反应交替形成<b>呼吸节奏</b>。</span>
      </div>

      <div className="sf-scene-stats">
        <span className="sf-sstat"><b>{list.length}</b> 场</span>
        <span className="sf-sstat tone-crimson"><b>{pro}</b> 主动</span>
        <span className="sf-sstat tone-slate"><b>{rea}</b> 反应</span>
        <span className="sf-sstat tone-gold"><b>{spineHit}</b> 脊柱场</span>
        <span className="sf-sstat"><b>{lines.length}</b> 线</span>
        <span className={`sf-sstat ${noCrucible ? "tone-rose" : "tone-sage"}`}>{noCrucible ? <><I.AlertTriangle size={11} /> {noCrucible} 场缺冲突</> : <><I.Check size={11} /> 场场有冲突</>}</span>
        <span style={{ flex: 1 }} />
        {ai && (
          <button className="btn btn-quiet btn-sm" disabled={ai.structBusy}
            title="让 AI 依已确认的大纲/角色/道德前提生成整份场景表（生成前自动留底，可回滚）"
            onClick={() => {
              if (list.some(s => (s.event || s.crucible || "").trim()) &&
                !window.confirm(`AI 会依上游材料重新生成整份场景表，现有 ${list.length} 场将被整体替换（已留底可回滚）。继续？`)) return;
              ai.onGenerateAll();
            }}>
            <I.Wand size={13} className={ai.structBusy ? "sf-spin" : ""} /> {ai.structBusy ? "生成中…" : "AI 生成整表"}
          </button>
        )}
      </div>

      <div className="sf-weave">
        <div className="sf-weave-head">
          <span className="sf-weave-title"><I.Activity size={13} /> 织线与节奏</span>
          <span className="sf-weave-premise" title="每条线都应折射这条道德前提（源自 03 中点翻转），否则就是闲笔">
            <span className="sf-wp-false">{premise.f || "（03 还没写错误信念）"}</span>
            <I.ArrowRight size={11} />
            <span className="sf-wp-true">{premise.t || "（正确信念）"}</span>
          </span>
        </div>

        <div className="sf-weave-rhythm">
          <span className="sf-weave-axis">节奏</span>
          <div className="sf-rhythm-band">
            {list.map((s, i) => (
              <span key={i}
                className={`sf-rb-cell ${s.type === "proactive" ? "is-pro" : "is-rea"} ${hiLine && (s.line || "main") !== hiLine ? "is-dim" : ""}`}
                title={`${s.id} · ${s.type === "proactive" ? "主动 GCS" : "反应 RDD"}`} />
            ))}
          </div>
          <div className="sf-rhythm-flags">
            {tightMax ? <span className="sf-flag tone-rose"><I.AlertTriangle size={10} /> 连续 {tightMax} 场主动 · 张力紧绷，插一场反应喘息</span> : null}
            {slackMax ? <span className="sf-flag tone-gold"><I.AlertTriangle size={10} /> 连续 {slackMax} 场反应 · 节奏松弛，推进一场主动</span> : null}
            {!tightMax && !slackMax ? <span className="sf-flag tone-sage"><I.Check size={10} /> 主动 / 反应交替均匀</span> : null}
          </div>
        </div>

        <div className="sf-weave-lines">
          {lineStats.map(ln => (
            <div key={ln.id} className={`sf-wl-row tone-${ln.tone} ${hiLine === ln.id ? "is-hi" : ""} ${hiLine && hiLine !== ln.id ? "is-dim" : ""}`}>
              <button className="sf-wl-tab" onClick={() => setHiLine(hiLine === ln.id ? null : ln.id)} title="点击高亮这条线的场景">
                <span className="sf-wl-dot" />
                <input className="sf-wl-name" value={ln.name} onClick={(e) => e.stopPropagation()} onChange={(e) => setLine(ln.id, "name", e.target.value)} />
                <span className="sf-wl-kind">{S2_KIND_LABEL[ln.kind] || "支线"}</span>
              </button>
              <div className="sf-wl-track">
                {list.map((s, i) => <span key={i} className={`sf-wl-cell ${(s.line || "main") === ln.id ? "is-on" : ""}`} title={s.id} />)}
              </div>
              <div className="sf-wl-meta">
                {ln.count ? <span className="sf-wl-count">{ln.count}/{list.length}</span> : <span className="sf-wl-count is-empty">未编入</span>}
                {ln.clustered ? <span className="sf-flag tone-gold" title="该线集中在很窄的一段——像绕路而非编织，考虑分散穿插"><I.AlertTriangle size={10} /> 扎堆</span> : null}
                {ln.noRefract ? <span className="sf-flag tone-rose" title="没写它如何折射道德前提——可能是闲笔"><I.AlertTriangle size={10} /> 缺折射</span> : null}
              </div>
              <input className="sf-wl-refract" value={ln.refract} onClick={(e) => e.stopPropagation()}
                onChange={(e) => setLine(ln.id, "refract", e.target.value)}
                placeholder={ln.kind === "main" ? "主线如何兑现道德前提…" : "这条线如何折射道德前提？（填不出 = 可能是闲笔）"} />
              {ln.kind !== "main"
                ? <button className="sf-wl-del" onClick={() => delLine(ln.id)} title="删除这条线"><I.X size={12} /></button>
                : <span className="sf-wl-del-sp" />}
            </div>
          ))}
          <button className="sf-wl-add" onClick={addLine}><I.Plus size={12} /> 添加支线</button>
        </div>
      </div>

      <div className="sf-scene-table">
        <div className="sf-scene-thead">
          <span className="sc-c-id">#</span><span className="sc-c-type">类型</span><span className="sc-c-line">线</span><span className="sc-c-pov">POV</span>
          <span className="sc-c-place">地点 · 事件</span><span className="sc-c-cru">坩埚（冲突）</span><span className="sc-c-fn">功能</span><span className="sc-c-act"></span>
        </div>
        {list.map((s, i) => {
          const lt = toneOf(s.line || "main");
          const dim = hiLine && (s.line || "main") !== hiLine;
          return (
          <div key={i} className={`sf-scene-row line-${lt} ${s.spine ? "is-spine" : ""} ${!(s.crucible || "").trim() ? "is-nocru" : ""} ${dim ? "is-dim" : ""}`}>
            <span className="sc-c-id"><span className="sc-no" title={s.id}>{s2SceneNo(s.id, i)}</span></span>
            <span className="sc-c-type">
              <button className={`sc-type ${s.type === "proactive" ? "is-pro" : "is-rea"}`} onClick={() => setScene(i, "type", s.type === "proactive" ? "reactive" : "proactive")} title="切换 主动 GCS / 反应 RDD">
                {s.type === "proactive" ? "主动" : "反应"}
              </button>
            </span>
            <span className="sc-c-line">
              <select className={`sc-line tone-${lt}`} value={s.line || "main"} onChange={(e) => setScene(i, "line", e.target.value)} title="这一场服务哪条线">
                {lines.map(ln => <option key={ln.id} value={ln.id}>{ln.name}</option>)}
              </select>
            </span>
            <span className="sc-c-pov"><S2PovPick value={s.pov} roster={roster} onChange={(v) => setScene(i, "pov", v)} className="sc-in sc-pov" /></span>
            <span className="sc-c-place">
              <input className="sc-in sc-in-place" list="s2-place-opts" value={s.place} onChange={(e) => setScene(i, "place", e.target.value)} placeholder="地点" />
              <input className="sc-in sc-in-event" value={s.event} onChange={(e) => setScene(i, "event", e.target.value)} placeholder="发生什么" />
            </span>
            <span className="sc-c-cru"><input className="sc-in" value={s.crucible} onChange={(e) => setScene(i, "crucible", e.target.value)} placeholder="什么困住角色…" /></span>
            <span className="sc-c-fn">
              <input className="sc-in sc-in-fn" value={s.fn} onChange={(e) => setScene(i, "fn", e.target.value)} placeholder="功能" />
              <select className="sc-spine" value={s.spine} onChange={(e) => setScene(i, "spine", e.target.value)} title="绑定脊柱灾难">
                {S2_SPINE_OPTS.map(o => <option key={o} value={o}>{o || "—"}</option>)}
              </select>
            </span>
            <span className="sc-c-act">
              <button className="sc-act" onClick={() => moveScene(i, -1)} disabled={i === 0} title="上移"><I.ChevronRight size={13} style={{ transform: "rotate(-90deg)" }} /></button>
              <button className="sc-act" onClick={() => moveScene(i, 1)} disabled={i === list.length - 1} title="下移"><I.ChevronRight size={13} style={{ transform: "rotate(90deg)" }} /></button>
              <button className="sc-act sc-act-del" onClick={() => delScene(i)} title="删除"><I.X size={13} /></button>
            </span>
          </div>
          );
        })}
      </div>
      <datalist id="s2-place-opts">{placeOpts.map(p => <option key={p} value={p} />)}</datalist>
      <button className="sf-scene-add" onClick={addScene}><I.Plus size={14} /> 添加场景</button>
    </div>
  );
}

const S2_TRIAGE_LABEL = { pass: "可通过", maybe: "需修补", rewrite: "该重写" };

function S2ScenePlan({ scaffold, onScaffold, refs, go, ai }) {
  const list = ((refs && refs.scenes) || {}).list || [];
  const roster = s2RosterList(refs);
  const plans = scaffold.plans || {};
  const selId = list.some(s => s.id === scaffold.sel) ? scaffold.sel : (list[0] ? list[0].id : "");
  const scene = list.find(s => s.id === selId) || null;
  const selIdx = list.findIndex(s => s.id === selId);
  // 类型跟随 09 的真相：主动/反应在场景列表里定，这里不再各说各话
  const proactive = scene ? scene.type !== "reactive" : true;
  const plan = { mode: proactive ? "proactive" : "reactive", pov: (scene && scene.pov) || "", goal: "", conflict: "", setback: "", reaction: "", dilemma: "", decision: "", cost_requirement: "", ...(plans[selId] || {}) };
  plan.mode = proactive ? "proactive" : "reactive";
  const setPlan = (f, v) => onScaffold(s => ({ ...s, sel: selId, plans: { ...(s.plans || {}), [selId]: { ...plan, [f]: v } } }));
  const selScene = (id) => onScaffold(s => ({ ...s, sel: id }));

  if (!list.length) {
    return (
      <div className="sf-scaffold sf-scene">
        <div className="sf-plan-empty">
          <I.List size={20} />
          <div>
            <div className="fw-600">还没有可规划的场景</div>
            <div className="text-muted text-sm">第 10 步逐场画草图——先去 09 把全书拆成一行一场。</div>
          </div>
          <button className="btn btn-primary btn-sm" onClick={() => go && go("scenes")}>去 09 · 场景列表</button>
        </div>
      </div>
    );
  }

  const stateOf = (id) => s2PlanState(plans[id]);
  const fully = list.filter(s => stateOf(s.id) === 2).length;
  const triItems = (ai && ai.triage && ai.triage.items) || null;
  const triOf = (id) => (triItems ? triItems[id] : null);
  const triCount = (st) => (triItems ? list.filter(s => (triOf(s.id) || {}).status === st).length : 0);
  const selTri = triOf(selId);
  const prev = selIdx > 0 ? list[selIdx - 1] : null;
  const prevPlan = prev ? plans[prev.id] : null;
  const prevSeam = prev ? ((prev.type === "reactive" ? (prevPlan || {}).decision : (prevPlan || {}).setback) || "").trim() : "";
  const nextUnplanned = () => { const t = list.find(s => stateOf(s.id) === 0 && s.id !== selId); if (t) selScene(t.id); };

  const triples = proactive
    ? [
        { f: "goal",     label: "目标 · Goal",     desc: "POV 角色进入这场时想要的、具体可达的东西" },
        { f: "conflict", label: "冲突 · Conflict", desc: "一连串挡在目标前的阻碍，逐级升级" },
        { f: "setback",  label: "挫败 · Setback",  desc: "结尾给一记打击——通常是「是的，但…」" },
        { f: "cost_requirement", label: "代价 · Cost", desc: "角色为这个结果具体付出了什么——免费的选择 = 注水" },
      ]
    : [
        { f: "reaction", label: "反应 · Reaction", desc: "对上一场挫败的情绪反应（容许角色崩一下）" },
        { f: "dilemma",  label: "两难 · Dilemma",  desc: "没有好选项，只有两个坏选项" },
        { f: "decision", label: "决定 · Decision", desc: "她选一个坏选项——它成为下一场的目标" },
        { f: "cost_requirement", label: "代价 · Cost", desc: "角色为这个决定具体付出了什么——免费的选择 = 注水" },
      ];
  return (
    <div className="sf-scaffold sf-scene">
      <div className="sf-scaffold-note">
        <I.Play size={13} />
        <span>原书规矩：<b>每场花五分钟</b>画一张草图——主动场＝目标-冲突-挫败；反应场＝反应-两难-决定。逐场过一遍，规划才算完。</span>
      </div>

      {/* AI 工具面：分诊全部场景 / 一键补全（生成前自动留底） */}
      {ai && (
        <div className="sf-plan-ai">
          <button className="btn btn-quiet btn-sm" disabled={ai.triageBusy} onClick={() => ai.onTriage()}
            title="逐场评估压力结构：可通过 / 需修补 / 该重写，并给出修复步骤与补丁">
            <I.Activity size={13} className={ai.triageBusy ? "sf-spin" : ""} /> {ai.triageBusy ? "分诊中…" : "AI 分诊"}
          </button>
          <button className="btn btn-quiet btn-sm" disabled={ai.structBusy} onClick={() => {
            if (!window.confirm(`AI 会逐场补齐 GCS/RDD、坩埚与钩子，已填内容会被深化改写（已留底可回滚）。继续？`)) return;
            ai.onFillAll();
          }} title="让 AI 依上游材料逐场补齐目标/冲突/挫败（或反应/两难/决定）">
            <I.Wand size={13} className={ai.structBusy ? "sf-spin" : ""} /> {ai.structBusy ? "生成中…" : "AI 补全所有场景"}
          </button>
          {triItems && (
            <span className="sf-triage-sum" title={`分诊于 ${new Date(ai.triage.at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })} · ${ai.triage.source === "llm" ? "AI 评估" : "规则诊断"}`}>
              <span className="tri-pass">{triCount("pass")} 过</span>
              <span className="tri-maybe">{triCount("maybe")} 修</span>
              <span className="tri-rewrite">{triCount("rewrite")} 重写</span>
            </span>
          )}
        </div>
      )}

      {/* 覆盖率导航：一格一场，点击切换 */}
      <div className="sf-plan-nav">
        <span className={`sf-plan-cov ${fully === list.length ? "is-all" : ""}`}><I.CheckCircle size={12} /> {fully} / {list.length} 场已规划</span>
        <div className="sf-plan-cells">
          {list.map((s, i) => {
            const st = stateOf(s.id);
            const tri = triOf(s.id);
            return (
              <button key={s.id}
                className={`sf-plan-cell st-${st} ${s.id === selId ? "is-sel" : ""} ${s.type === "reactive" ? "is-rea" : "is-pro"} ${s.spine ? "is-spine" : ""} ${tri ? "tri-" + tri.status : ""}`}
                onClick={() => selScene(s.id)}
                title={`${s2SceneNo(s.id, i)} · ${s.type === "reactive" ? "反应" : "主动"}${s.spine ? " · " + s.spine : ""} · ${st === 2 ? "三槽齐" : st === 1 ? "填了一半" : "未规划"}${tri ? " · 分诊：" + (S2_TRIAGE_LABEL[tri.status] || tri.status) : ""}`}>
                {i + 1}
              </button>
            );
          })}
        </div>
        {fully < list.length && <button className="btn btn-quiet btn-sm" onClick={nextUnplanned}><I.ChevronRight size={13} /> 下一未规划</button>}
      </div>

      <div className="sf-scene-meta">
        <div className="sf-plan-cur">
          <span className="sf-plan-cur-id" title={scene.id}>{s2SceneNo(scene.id, selIdx)}</span>
          <span className="sf-plan-cur-body">
            <span className="sf-plan-cur-title">{scene.event || scene.place || "（未命名场景）"}</span>
            <span className="sf-plan-cur-sub">{scene.place}{scene.spine ? ` · ${scene.spine}` : ""}{scene.fn ? ` · ${scene.fn}` : ""}</span>
          </span>
        </div>
        <label className="sf-field is-short"><span className="sf-field-label">POV 角色</span>
          <S2PovPick value={plan.pov} roster={roster} onChange={(v) => setPlan("pov", v)} className="sf-field-input" placeholder={s2PovLabel(scene.pov, roster) || "POV"} /></label>
        <span className={`sf-plan-type ${proactive ? "is-pro" : "is-rea"}`} title="类型跟随 09 场景列表——要改去 09 切换">
          {proactive ? "主动 · GCS" : "反应 · RDD"}
          <button className="sf-plan-type-go" onClick={() => go && go("scenes")} title="在 09 修改类型">09</button>
        </span>
        {ai && (
          <button className="btn btn-quiet btn-sm" disabled={ai.structBusy} onClick={() => ai.onFillScene(selId)}
            title="只补全这一场的三槽/坩埚/钩子，其余场景不动（生成前自动留底）">
            <I.Wand size={13} className={ai.structBusy ? "sf-spin" : ""} /> {ai.structBusy ? "生成中…" : "AI 补全这一场"}
          </button>
        )}
      </div>

      {/* 本场分诊结果：状态 + 诊断 + 修复步骤 + 一键应用补丁 */}
      {selTri && (
        <div className={`sf-triage tri-${selTri.status}`}>
          <div className="sf-triage-head">
            <span className="sf-triage-badge">{S2_TRIAGE_LABEL[selTri.status] || selTri.status}</span>
            {typeof selTri.score === "number" && <span className="sf-triage-score">{selTri.score} 分</span>}
            <span className="sf-triage-notes">{selTri.notes || ""}</span>
            {Object.keys(selTri.repair_patch || {}).length > 0 && (
              <button className="btn btn-accent btn-sm" disabled={ai.structBusy} onClick={() => ai.onApplyRepair(selId, selTri)}
                title="把分诊给出的修复补丁写进本场三槽/坩埚（应用前自动留底）">
                <I.Check size={13} /> 应用修复补丁
              </button>
            )}
          </div>
          {(selTri.fix_steps || []).length > 0 && (
            <ul className="sf-triage-fixes">
              {(selTri.fix_steps || []).slice(0, 4).map((f, i) => <li key={i}>{f}</li>)}
            </ul>
          )}
        </div>
      )}

      {prev && (
        <div className={`sf-plan-seam ${prevSeam ? "" : "is-empty"}`}>
          <span className="sf-plan-seam-tag"><I.ArrowRight size={10} /> 接上一场 {s2SceneNo(prev.id, selIdx - 1)}</span>
          {prevSeam
            ? <span className="sf-plan-seam-text">{prev.type === "reactive" ? "决定" : "挫败"}：「{prevSeam}」——本场从这里接住。</span>
            : <span className="sf-plan-seam-text">上一场还没写{prev.type === "reactive" ? "决定" : "挫败"}——链条在这里是断的。<button className="sf-plan-seam-go" onClick={() => selScene(prev.id)}>去补 {s2SceneNo(prev.id, selIdx - 1)}</button></span>}
        </div>
      )}

      <div className="sf-gcs" key={selId + plan.mode}>
        {triples.map((t, i) => (
          <div key={t.f} className={`sf-beat ${proactive ? "tone-crimson" : "tone-slate"}`}>
            <div className="sf-beat-side"><span className="sf-beat-idx">{i + 1}</span></div>
            <div className="sf-beat-main">
              <div className="sf-beat-label">{t.label}<span className="sf-beat-desc">{t.desc}</span></div>
              <textarea className="sf-beat-text" rows={2} value={plan[t.f] || ""}
                onChange={(e) => setPlan(t.f, e.target.value)} placeholder={`写「${t.label.split(" · ")[0]}」…`} />
            </div>
          </div>
        ))}
      </div>

      <div className="sf-plan-foot">
        <button className="btn btn-ghost btn-sm" disabled={selIdx <= 0} onClick={() => selScene(list[selIdx - 1].id)}><I.ChevronLeft size={13} /> {selIdx > 0 ? s2SceneNo(list[selIdx - 1].id, selIdx - 1) : "上一场"}</button>
        <span className="text-muted text-sm">{selIdx + 1} / {list.length}</span>
        <button className="btn btn-ghost btn-sm" disabled={selIdx >= list.length - 1} onClick={() => selScene(list[selIdx + 1].id)}>{selIdx < list.length - 1 ? s2SceneNo(list[selIdx + 1].id, selIdx + 1) : "下一场"} <I.ChevronRight size={13} /></button>
      </div>
    </div>
  );
}

/* ====== 驻场教练：逐步对话辅导（回合服务端持久化；第 10 步自动聚焦选中场） ====== */
const S2_COACH_QUICKS = [
  "这一步还缺什么？先告诉我最要命的一个缺口。",
  "帮我把这一步的压力再抬高一档——具体到代价。",
  "请直接给我一版可用的改写（作为补丁）。",
];

function S2Coach({ active, beKey, history, busy, focusRow, onSend, onApplyPatch }) {
  const [input, setInput] = useSS("");
  const turns = (history || []).filter(t => t.step_key === beKey);
  const endRef = useSR(null);
  useSE(() => { try { endRef.current && endRef.current.scrollIntoView({ block: "end" }); } catch (e) {} }, [turns.length, busy]);
  const send = (text) => { const t = (text != null ? text : input).trim(); if (!t || busy) return; onSend(t); setInput(""); };
  return (
    <div className="sf-coach">
      <div className="sf-coach-note">
        <I.Sparkles size={14} />
        <span>驻场教练读得到你<b>本步草稿</b>与已确认的上游材料{focusRow ? <>，当前聚焦场景 <b>{focusRow}</b>（跟随左侧选中）</> : null}。要它直接改写时，回复会附带「补丁」，可一键应用、可回滚。</span>
      </div>
      <div className="sf-coach-log">
        {!turns.length && !busy && (
          <div className="sf-coach-empty">还没有对话。从下面的快捷提问开始，或直接问「{active.name}」这一步的任何问题。</div>
        )}
        {turns.map(t => (
          <div key={t.turn_id} className="sf-coach-turn">
            <div className="sf-coach-q"><span className="sf-coach-who">我</span><span>{t.message || "（生成建议）"}</span></div>
            <div className="sf-coach-a">
              <span className={`sf-coach-who ${t.source === "llm" ? "is-ai" : ""}`}>{t.source === "llm" ? "教练" : "规则"}</span>
              <div className="sf-coach-body">
                <p>{t.reply}</p>
                {(t.suggestions || []).length > 0 && (
                  <ul className="sf-coach-sugs">{(t.suggestions || []).slice(0, 4).map((s, i) => <li key={i}>{s}</li>)}</ul>
                )}
                {t.candidate_patch && Object.keys(t.candidate_patch).length > 0 && (
                  <button className="btn btn-accent btn-sm" onClick={() => onApplyPatch(t)}
                    title="把教练给出的结构化补丁合并进本步（空字段不清空、按角色/场景对位；应用前自动留底）">
                    <I.Check size={13} /> 应用补丁{t.candidate_label ? `「${t.candidate_label}」` : ""}
                  </button>
                )}
                {t.focus_scene_id && <span className="sf-coach-focus">聚焦 {t.focus_scene_id}</span>}
              </div>
            </div>
          </div>
        ))}
        {busy && <div className="sf-coach-busy"><I.Refresh size={13} className="sf-spin" /> 教练正在读你的草稿…</div>}
        <div ref={endRef} />
      </div>
      <div className="sf-coach-quicks">
        {S2_COACH_QUICKS.map((q, i) => (
          <button key={i} className="btn btn-quiet btn-sm" disabled={busy} onClick={() => send(q)}>{q.slice(0, 18)}…</button>
        ))}
      </div>
      <div className="sf-coach-input">
        <textarea rows={2} value={input} disabled={busy} placeholder={`问「${active.name}」这一步的任何问题；让教练“直接给改写”会得到可应用的补丁…`}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); send(); } }} />
        <button className="btn btn-primary" disabled={busy || !input.trim()} onClick={() => send()}>
          {busy ? <I.Refresh size={14} className="sf-spin" /> : <I.ArrowRight size={14} />} 发送
        </button>
      </div>
    </div>
  );
}

function S2Cands({ onAdopt, onAdoptStructured, onAdoptFocused, focusLabel, structBusy, draft, cands, meta, busy, err, onRegen }) {
  const list = cands && cands.length ? cands : [];
  const [sel, setSel] = useSS(list[0] ? list[0].id : "A");
  const [compare, setCompare] = useSS(false);
  useSE(() => { if (list[0] && !list.find(c => c.id === sel)) setSel(list[0].id); }, [cands]);
  const selCand = list.find(c => c.id === sel) || list[0] || { id: "A", text: "", notes: [] };

  useSE(() => {
    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const el = e.target;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) return;
      if (["1", "2", "3"].includes(e.key) && list[+e.key - 1]) { e.preventDefault(); setSel(list[+e.key - 1].id); }
      else if (e.key.toLowerCase() === "c") { e.preventDefault(); setCompare(v => !v); }
      else if (e.key.toLowerCase() === "r") { e.preventDefault(); if (onRegen && !busy) onRegen(); }
      else if (e.key === "Enter") { e.preventDefault(); onAdopt(selCand.text, selCand.id); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [sel, compare, selCand, list, busy]);

  const isAi = meta && meta.ai;
  const stamp = isAi && meta.at ? new Date(meta.at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) : null;
  return (
    <div className="cands">
      <div className="cands-head">
        <div>
          <div className="fw-600">
            {list.length} 条候选 · {isAi ? `AI 生成${stamp ? " · " + stamp : ""}` : "示例候选"}
          </div>
          <div className="text-muted text-sm">
            {isAi ? "依据后端已批准的上游材料与本步诊断缺口生成。「采纳并结构化」会把候选方向展开成本步全部字段。" : "点「AI 生成」让 Claude 读上游各步与诊断缺口，按本步任务重写候选。"}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button className={`btn btn-sm ${compare ? "btn-primary" : "btn-ghost"}`} onClick={() => setCompare(v => !v)}><I.Layout size={13} /> 对比草稿</button>
          <button className={`btn btn-sm ${isAi ? "btn-ghost" : "btn-accent"} sf-regen`} onClick={() => onRegen && onRegen()} disabled={busy} title="基于上游材料生成（R）">
            <I.Refresh size={13} className={busy ? "sf-spin" : ""} /> {busy ? "生成中…" : (isAi ? "重新生成" : "AI 生成")}
          </button>
        </div>
      </div>

      {err && (
        <div className="sf-cand-err">
          <I.AlertTriangle size={13} />
          <span>{err}</span>
          <button className="btn btn-quiet btn-sm" onClick={() => onRegen && onRegen()} disabled={busy}>重试</button>
        </div>
      )}

      {busy && (
        <div className="sf-cand-gen">
          <I.Refresh size={13} className="sf-spin" />
          <span>正在依据上游已确认材料生成候选…</span>
        </div>
      )}

      <div className="sf-cand-tabs">
        {list.map((c, i) => (
          <button key={c.id} className={`sf-cand-tab ${sel === c.id ? "is-sel" : ""}`} onClick={() => setSel(c.id)}>
            <span className="sf-cand-tab-key">{i + 1}</span>
            <span className="sf-cand-tab-id">候选 {c.id}</span>
            <span className="sf-cand-tab-label">{c.label}</span>
          </button>
        ))}
        <span className="sf-cand-hint"><kbd>1</kbd><kbd>2</kbd><kbd>3</kbd> 选 · <kbd>C</kbd> 对比 · <kbd>R</kbd> 生成 · <kbd>↵</kbd> 采纳</span>
      </div>

      {compare ? (
        <div className="sf-compare">
          <div className="sf-compare-col">
            <div className="sf-compare-h"><span className="pill pill-slate text-xs"><span className="pill-dot" />当前草稿</span></div>
            <p className="cand-text">{draft}</p>
          </div>
          <div className="sf-compare-arrow"><I.ArrowRight size={16} /></div>
          <div className="sf-compare-col is-new">
            <div className="sf-compare-h"><span className="pill pill-gold text-xs"><span className="pill-dot" />候选 {selCand.id} · {selCand.label}</span></div>
            <p className="cand-text">{selCand.text}</p>
            <div className="cand-notes">{selCand.notes.map((n, i) => <span key={i} className="pill text-xs">{n}</span>)}</div>
            {isAi && onAdoptStructured ? (
              <React.Fragment>
                <button className="btn btn-accent btn-sm sf-compare-adopt" disabled={structBusy} onClick={() => onAdoptStructured(selCand.text, selCand.id)}
                  title="以候选为方向蓝本，让 AI 把本步全部结构化字段整套填好（可回滚）">
                  {structBusy ? <I.Refresh size={13} className="sf-spin" /> : <I.Check size={13} />} {structBusy ? "结构化中…" : `采纳候选 ${selCand.id} 并结构化整步`}
                </button>
                {onAdoptFocused && focusLabel && (
                  <button className="btn btn-primary btn-sm sf-compare-adopt" disabled={structBusy} onClick={() => onAdoptFocused(selCand.text, selCand.id)}
                    title={`以候选为定向蓝本，只更新当前选中的「${focusLabel}」——其余成员保持不动（可回滚）`}>
                    {structBusy ? "定向中…" : `只更新「${focusLabel}」`}
                  </button>
                )}
                <button className="btn btn-quiet btn-sm sf-compare-adopt" disabled={structBusy} onClick={() => onAdopt(selCand.text, selCand.id)}
                  title="只把候选文本放进自由草稿，不动结构化脚手架">仅作草稿替换</button>
              </React.Fragment>
            ) : (
              <button className="btn btn-accent btn-sm sf-compare-adopt" onClick={() => onAdopt(selCand.text, selCand.id)}><I.Check size={13} /> 采纳候选 {selCand.id}，整体替换草稿</button>
            )}
          </div>
        </div>
      ) : (
        <div className="cands-list">
          {list.map(c => (
            <article key={c.id} className={`cand sf-cand ${sel === c.id ? "is-sel" : ""}`} onClick={() => setSel(c.id)}>
              <header className="cand-head">
                <span className="cand-id">候选 {c.id}</span>
                <div className="flex items-center gap-2">
                  <span className="cand-label">{c.label}</span>
                  <span className="pill"><span className="pill-dot" />{c.tag}</span>
                </div>
                <div className="flex gap-2 cand-actions">
                  <button className="btn btn-quiet btn-sm" onClick={(e) => { e.stopPropagation(); setSel(c.id); setCompare(true); }}>对比</button>
                  {isAi && onAdoptStructured ? (
                    <React.Fragment>
                      <button className="btn btn-quiet btn-sm" disabled={structBusy} title="只把候选文本放进自由草稿，不动结构化脚手架"
                        onClick={(e) => { e.stopPropagation(); onAdopt(c.text, c.id); }}>仅作草稿</button>
                      {onAdoptFocused && focusLabel && (
                        <button className="btn btn-quiet btn-sm" disabled={structBusy} title={`以候选为定向蓝本，只更新当前选中的「${focusLabel}」——其余成员保持不动（可回滚）`}
                          onClick={(e) => { e.stopPropagation(); onAdoptFocused(c.text, c.id); }}>
                          {structBusy ? "定向中…" : `只更新「${focusLabel}」`}
                        </button>
                      )}
                      <button className="btn btn-primary btn-sm" disabled={structBusy} title="以候选为方向蓝本，让 AI 把本步全部结构化字段整套填好（可回滚）"
                        onClick={(e) => { e.stopPropagation(); onAdoptStructured(c.text, c.id); }}>
                        {structBusy ? "结构化中…" : "采纳并结构化"}
                      </button>
                    </React.Fragment>
                  ) : (
                    <button className="btn btn-primary btn-sm" onClick={(e) => { e.stopPropagation(); onAdopt(c.text, c.id); }}>采纳</button>
                  )}
                </div>
              </header>
              <p className="cand-text">{c.text}</p>
              <div className="cand-notes">{c.notes.map((n, i) => <span key={i} className="pill text-xs">{n}</span>)}</div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}

/* 一小时内走相对文案，更早走 lib/ago.js 的天级绝对文案（与成稿中心版本标签同款） */
function s2HistTime(t) {
  const diff = Date.now() - t;
  if (diff < 60000) return "刚刚";
  if (diff < 3600000) return Math.floor(diff / 60000) + " 分钟前";
  return dayTimeLabel(t);
}
function S2History({ history, go, onRestore }) {
  const list = history || [];
  if (!list.length) return (
    <div className="hist-empty">
      <I.Clock size={18} />
      <div>
        <div className="fw-600">还没有操作记录</div>
        <div className="text-muted text-sm">确认步骤、采纳候选、让 AI 生成或复核后，这里会留下时间线；带快照的节点可一键回滚。</div>
      </div>
    </div>
  );
  return (
    <ul className="hist">
      {list.map((h, i) => {
        const st = S2_STEPS.find(s => s.key === h.key);
        return (
          <li key={i} className="hist-row">
            <span className="hist-time">{s2HistTime(h.t)}</span>
            <span className={`hist-who ${h.who === "Claude" ? "is-ai" : ""}`}>{h.who}</span>
            <span className="hist-action">{h.action}</span>
            <span className="hist-note">{h.note}</span>
            {h.snap && onRestore && <button className="btn btn-quiet btn-sm hist-restore" onClick={() => onRestore(h)} title="把这一步回滚到此刻的内容快照"><I.Refresh size={12} /> 回滚</button>}
            {st && go && <button className="btn btn-quiet btn-sm" onClick={() => go(h.key)}>前往</button>}
          </li>
        );
      })}
    </ul>
  );
}

function S2Ref({ active, drafts, scaffolds }) {
  const ancs = s2Ancestors(active.key).slice().reverse(); // root → nearest
  const para = (scaffolds && scaffolds.paragraph) || {};
  const pf = (para.premiseF || "").trim(), pt = (para.premiseT || "").trim();
  const clip = (s, n) => { s = (s || "").replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n) + "…" : s; };
  return (
    <div className="refpane">
      <div className="ref-lead text-muted text-sm">
        {ancs.length ? `本步「${active.name}」展开自下面 ${ancs.length} 层上游——保持一致。` : "这是雪花的原点，没有上游引用。"}
      </div>
      {ancs.map(k => {
        const s = S2_STEPS.find(x => x.key === k);
        const text = clip(s2Content(drafts[k], scaffolds[k]), 160);
        return (
          <div key={k} className="card-flat ref-card">
            <div className="ref-card-h"><span className={`sf-trk-tag trk-${s.track}`}>{s.num}</span><span className="fw-600">{s.name}</span></div>
            <p className="text-serif ref-card-body">{text || <em className="text-muted">（该步尚未填写）</em>}</p>
          </div>
        );
      })}
      {(pf || pt) && (
        <div className="card-flat ref-card ref-spine">
          <div className="ref-card-h"><span className="sf-trk-tag trk-plot">脊柱</span><span className="fw-600">道德前提·中点翻转</span></div>
          <p className="ref-premise"><span className="ref-premise-f">{pf || "—"}</span><I.ArrowRight size={12} /><span className="ref-premise-t">{pt || "—"}</span></p>
        </div>
      )}
      <div className="card-flat ref-card">
        <div className="ref-card-h"><span className="sf-trk-tag trk-orient">风格</span><span className="fw-600">参考画像·冷峻短句</span></div>
        <p className="text-muted text-sm" style={{ lineHeight: 1.6 }}>倾向短句、动词驱动；克制描述抽象情绪，多用具体物件；段落短促，避免华丽形容词堆叠。</p>
      </div>
    </div>
  );
}

/* ====== Links & downstream (portrait + live downstream steps), collapsible ====== */
const S2_STATE_LABEL = { done: "已确认", warn: "需补", active: "进行中", skip: "已略过", todo: "待写", stale: "需复核" };
function S2Links({ active, states, go, staleMap }) {
  const kids = S2_STEPS.filter(s => s.fromKey === active.key);
  return (
    <S2Sec label="关联与影响" meta={kids.length ? `下游 ${kids.length}` : null} collapsible defaultOpen={false}>
      <div className="sfx-links-grp">
        <div className="sfx-links-sub">参考画像</div>
        <div className="sfx-chips">
          <span className="pill pill-gold"><span className="pill-dot" />冷峻短句</span>
          <span className="pill"><span className="pill-dot" />克制叙事</span>
        </div>
        <p className="sfx-note">影响候选生成的节奏与句式。</p>
      </div>
      <div className="sfx-links-grp">
        <div className="sfx-links-sub">本步影响下游</div>
        {kids.length ? kids.map(s => {
          const isStale = staleMap && staleMap[s.key];
          const st = isStale ? "stale" : (states[s.key] || "todo");
          const tone = st === "done" ? "sage" : (st === "warn" || st === "stale") ? "gold" : "slate";
          return (
            <button key={s.key} className="sfx-down" onClick={() => go(s.key)}>
              <span className="sfx-down-name">{s.num} · {s.name}</span>
              <span className={`pill pill-${tone} text-xs`}><span className="pill-dot" />{S2_STATE_LABEL[st] || st}</span>
            </button>
          );
        }) : <p className="sfx-note">本步处在收尾层，暂无直接下游。</p>}
      </div>
    </S2Sec>
  );
}

/* ---- 回滚预览：快照 vs 当前，看清再恢复 ---- */
function S2SnapDiff({ h, current, onApply, onClose }) {
  const st = S2_STEPS.find(s => s.key === h.key) || {};
  const oldText = s2Content(h.snap.draft, h.snap.scaffold).trim();
  const curText = s2Content(current.draft, current.scaffold).trim();
  const same = oldText === curText;
  const cnt = (t) => t.replace(/\s+/g, "").length;
  useSE(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return (
    <div className="sf-sd-scrim" role="dialog" aria-modal="true" onClick={onClose}>
      <div className="sf-sd-card" onClick={(e) => e.stopPropagation()}>
        <header className="sf-sd-head">
          <div>
            <div className="sf-sd-title">回滚预览 · {st.num} {st.name}</div>
            <div className="sf-sd-sub">快照留于 {new Date(h.t).toLocaleString("zh-CN")} · {h.action}{h.note ? ` · ${h.note}` : ""}</div>
          </div>
          <button className="wr-drawer-x" onClick={onClose} title="关闭 (Esc)"><I.X size={16} /></button>
        </header>
        {same ? (
          <div className="sf-sd-same"><I.Check size={14} /> 快照与当前内容完全一致，无需回滚。</div>
        ) : (
          <div className="sf-sd-cols">
            <div className="sf-sd-col is-old">
              <div className="sf-sd-coltag"><I.Clock size={11} /> 快照（将恢复为这版） · {cnt(oldText)} 字</div>
              <pre className="sf-sd-text text-serif">{oldText || "（空）"}</pre>
            </div>
            <div className="sf-sd-col is-cur">
              <div className="sf-sd-coltag"><I.Pen size={11} /> 当前（将被覆盖，会另留底） · {cnt(curText)} 字</div>
              <pre className="sf-sd-text text-serif">{curText || "（空）"}</pre>
            </div>
          </div>
        )}
        <footer className="sf-sd-foot">
          <span className="sf-sd-hint">回滚前会自动给当前内容再留一份快照，回滚本身可撤销</span>
          <div className="flex gap-2">
            <button className="btn btn-quiet btn-sm" onClick={onClose}>取消</button>
            <button className="btn btn-accent btn-sm" onClick={onApply} disabled={same}><I.Refresh size={13} /> 确认回滚</button>
          </div>
        </footer>
      </div>
    </div>
  );
}

function S2ImportPlanDialog({ value, busy, error, onChange, onImport, onClose }) {
  useSE(() => {
    const onKey = (e) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);
  return (
    <div className="sf-sd-scrim" role="dialog" aria-modal="true" aria-label="导入结构化雪花计划" onClick={onClose} data-testid="snow-import-dialog">
      <div className="sf-sd-card sf-import-card" onClick={(e) => e.stopPropagation()}>
        <header className="sf-sd-head">
          <div>
            <div className="sf-sd-title">导入结构化雪花计划</div>
            <div className="sf-sd-sub">粘贴包含 <code>steps</code> 的十步规范 JSON。系统会按依赖顺序逐步保存、批准并保留版本历史。</div>
          </div>
          <button className="wr-drawer-x" onClick={onClose} disabled={busy} title="关闭 (Esc)"><I.X size={16} /></button>
        </header>
        <div className="sf-import-body">
          <div className="sf-import-warning"><I.AlertTriangle size={14} /> 导入会为当前作品创建十步新版本；任一步失败会立即停止，不会伪装成已完成。</div>
          <textarea data-testid="snow-import-json" value={value} disabled={busy} onChange={(e) => onChange(e.target.value)}
            spellCheck="false" placeholder={'{\n  "steps": {\n    "book_brief": { ... },\n    "one_sentence_summary": { ... },\n    ...\n  }\n}'} />
          {error && <div className="sf-cand-err" role="alert"><I.AlertTriangle size={13} /><span>{error}</span></div>}
        </div>
        <footer className="sf-sd-foot">
          <span className="sf-sd-hint">必须包含 book_brief 至 scene_details 全部十步。</span>
          <div className="flex gap-2">
            <button className="btn btn-quiet btn-sm" onClick={onClose} disabled={busy}>取消</button>
            <button className="btn btn-accent btn-sm" data-testid="snow-import-submit" onClick={onImport} disabled={busy || !value.trim()}>
              {busy ? <I.Refresh size={13} className="sf-spin" /> : <I.Download size={13} />} {busy ? "逐步导入中…" : "导入并逐步批准"}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}


/* 构思模块包装：俯视(控制塔) ⇄ 细看(逐步工作台) 两个视图共享同一模块。
   结构总览的 DAG 骨架对所有作品通用——默认进入逐步
   工作台，总览给引导态，避免把别的书的结构图硬塞过来。 */
/* 非潮汐作品的总览引导态：结构图谱会随十步确认逐步点亮 */
function SnowOverviewEmpty({ go, onSteps }) {
  const work = WsWorks ? WsWorks.active() : { title: "这部作品" };
  return (
    <div className="page" data-screen-label="snowflake · overview empty">
      <div style={{ display: "grid", placeItems: "center", minHeight: "70vh", textAlign: "center" }}>
        <div style={{ maxWidth: 460, display: "grid", gap: 14, justifyItems: "center" }}>
          <div style={{ fontFamily: "var(--font-serif)", fontSize: 22, color: "var(--ink-1)" }}>《{work.title}》的结构图谱还没长出来</div>
          <p style={{ color: "var(--ink-3)", fontSize: 14, lineHeight: 1.8, margin: 0 }}>
            结构总览是雪花十步的俯视图：随着每一步被确认，这里会逐层点亮
            「一句话 → 一段话 → 人物 → 大纲 → 场景」的依赖图谱。先把前几步走出来。
          </p>
          <button className="btn btn-accent" onClick={onSteps}><I.Snowflake size={15} /> 进入雪花十步</button>
        </div>
      </div>
    </div>
  );
}

function WsConstruct({ go }) {
  const [mode, setMode] = useSS("steps");
  const [step, setStep] = useSS("paragraph");
  // 深链：从流程图 / 命令面板跳到某一步。优先读挂起目标（避免跨视图挂载竞态），再监听实时事件。
  useSE(() => {
    const apply = (k) => { if (k) { setStep(k); setMode("steps"); } };
    if (window.__snowStepTarget) { const k = window.__snowStepTarget; window.__snowStepTarget = null; apply(k); }
    const onStep = (e) => apply(e.detail);
    window.addEventListener("ws:snow-step", onStep);
    setViewIntentTargetReady("snowflake");
    return () => {
      setViewIntentTargetReady("snowflake", false);
      window.removeEventListener("ws:snow-step", onStep);
    };
  }, []);
  if (mode === "overview") {
    return <SnowOverviewEmpty go={go} onSteps={() => setMode("steps")} />;
  }
  return <WsSnowflake key={step} go={go} initialStep={step} onOverview={() => setMode("overview")} />;
}

/* P2：s2Materialize（前端脊柱锚点物化引擎）与 s2AdoptOutline 已删除 —— 分章算法
   搬到后端 snowflake_chaptering.py，成为唯一实现；「整理为章节结构」只剩
   分章预览面板一条路径。 */
Object.assign(window, { WsSnowflake, WsConstruct, S2_STEPS, S2_BE_STEPS, s2GenerateCands, s2PacingRuns, s2LineStats, s2StepSummary, s2ExportState });

/* ESM 导出（Phase 1 机械追加；window.* 赋值过渡期保留） */
export { WsSnowflake, WsConstruct, S2_STEPS, S2_BE_STEPS, s2PacingRuns, s2LineStats, s2StepSummary, s2ExportState, s2NormalizeState, s2NextSceneRowId };

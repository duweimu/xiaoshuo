import React from "react";
import { I } from "./icons.jsx";
import { S2_STEPS, s2StepSummary } from "./ws-snow.jsx";
import { WsWorks } from "./ws-works.jsx";
import { WsCatalog, useCatalogChapters } from "./ws-catalog.jsx";

/* global React, I, window */
const { useState: useStFM, useEffect: useEfFM, useRef: useRefFM, useMemo: useMemoFM } = React;

/* ==========================================================
   创作流程 — Flow Map (v3 · 接真实数据)
   不再写死：整条流水线从全书的单一数据源派生——
     · 构思  ← S2_STEPS（雪花十步的状态）
     · 场景 / 写作 / 成稿 / 进度脊 ← WsCatalog 目录真相（approved/review/draft/writing/planned）
   数据缺失时回落到静态兜底，永不崩。
   ========================================================== */

/* 工位的展示配置（图标/名称/色调是静态的，数字与状态全部派生） */
const FM_STAGE_CFG = [
  { key: "snowflake",   go: "snowflake",   icon: "Snowflake", name: "构思", sub: "雪花十步",   tone: "gold" },
  { key: "scene",       go: "scene",       icon: "Compass",   name: "场景", sub: "铺场 · 排程", tone: "sage" },
  { key: "writer",      go: "writer",      icon: "Pen",       name: "写作", sub: "章节正文",   tone: "crimson", active: true },
  { key: "manuscripts", go: "manuscripts", icon: "BookOpen",  name: "成稿", sub: "终稿 · 发布", tone: "slate" },
];

/* 连接段色调（静态）；在途条目（chip）派生后塞进来 */
const FM_PIPE_TONES = ["sage", "crimson", "slate"];

/* 进度脊：阶段 → 展示元数据 */
const FM_SPINE_META = {
  idea:  { label: "构思", color: "var(--paper-3)",      text: "var(--ink-3)",   faint: true },
  scene: { label: "场景", color: "var(--gold-soft)",    text: "var(--gold)" },
  draft: { label: "草稿", color: "var(--crimson-soft)", text: "var(--crimson)" },
  final: { label: "定稿", color: "var(--sage)",         text: "var(--sage)" },
};
const FM_SPINE_ORDER = ["final", "draft", "scene", "idea"];
const FM_STAGE_WEIGHT = { final: 1, draft: 0.7, scene: 0.45, idea: 0.2 };

const STAGE_W = 152; // 工位列宽，决定脊与连接段对齐

const pi = (v) => parseInt(v, 10) || 0;
const fmt = (n) => (n || 0).toLocaleString("en-US");

/* ==========================================================
   派生：把全书数据折算成流程模型
   ========================================================== */
function fmDerive() {
  try {
    const work = WsWorks ? WsWorks.active() : null;
    /* 章节目录：单一真相源（与主页 / 写作器 / 成稿中心同源），缺席才回落静态种子 */
    const ARR = WsCatalog ? WsCatalog.get() : null;
    /* 雪花十步：元数据来自 S2_STEPS，状态读构思工作台的持久化真相（按作品） */
    const live = s2StepSummary ? s2StepSummary() : null;
    const STEPS = (S2_STEPS || []).map((s, i) => ({ ...s, state: live && live.steps && live.steps[i] ? live.steps[i].s : s.state }));
    if (!ARR || !STEPS.length) return FM_FALLBACK;

    // 目录是章节进度的唯一真相源。空目录必须保持 0 章，不能用目标章节数
    // 或虚构的第 1 章填充流程地图。
    const total = ARR.length;
    const totalForRatio = Math.max(total, 1);
    const arrByN = {};
    ARR.forEach(c => { arrByN[pi(c.n)] = c; });

    // —— 章节 → 阶段（走到的最远工位）
    const stageOf = (st) =>
      st === "approved" ? "final" :
      (st === "review" || st === "draft" || st === "writing") ? "draft" :
      st === "planned" ? "scene" : "idea";

    const chapters = [];
    for (let n = 1; n <= total; n++) {
      const c = arrByN[n];
      let stage = "idea", front = false;
      if (c) { stage = stageOf(c.state); front = c.state === "writing" || !!c.current; }
      chapters.push({ n, stage, front });
    }
    const spineCounts = chapters.reduce((m, c) => { m[c.stage] = (m[c.stage] || 0) + 1; return m; }, {});

    // —— 各工位分桶
    const approved = ARR.filter(c => c.state === "approved");
    const review   = ARR.filter(c => c.state === "review");
    const draftCh  = ARR.filter(c => c.state === "draft");
    const planned  = ARR.filter(c => c.state === "planned");
    const current  = ARR.find(c => c.state === "writing" || c.current);
    const drafted  = ARR.filter(c => ["writing", "draft", "review", "approved"].includes(c.state));
    const words    = drafted.reduce((a, c) => a + ((c.words && c.words.cur) || 0), 0);
    const curScene = current && (current.scenes || []).find(sc => sc.state === "writing");
    const curTodo  = current ? (current.scenes || []).filter(sc => sc.state === "todo").length : 0;
    const fullDraft = ARR.filter(c => ["review", "approved"].includes(c.state)); // 完整初稿（≥审阅）

    // —— 场景统计（排除「待规划」占位场景）
    const isReal = (sc) => sc && sc.goal !== "—" && sc.title !== "待规划场景";
    let scTotal = 0, scDone = 0, scWriting = 0, scTodo = 0, chWithScenes = 0;
    ARR.forEach(c => {
      const real = (c.scenes || []).filter(isReal);
      if (real.length) {
        chWithScenes++;
        real.forEach(sc => {
          scTotal++;
          if (sc.state === "done") scDone++;
          else if (sc.state === "writing") scWriting++;
          else scTodo++;
        });
      }
    });

    // —— 构思（雪花十步）
    const stepsDone = STEPS.filter(s => s.state === "done");
    const stepsActive = STEPS.filter(s => s.state === "active");
    const stepsWarn = STEPS.filter(s => s.state === "warn");
    const warnStep = stepsWarn[0];

    // —— 整书推进度（按阶段加权，区别于「定稿率」）
    const weighted = chapters.reduce((a, c) => a + (FM_STAGE_WEIGHT[c.stage] || 0), 0);
    const overallPct = total ? Math.round((weighted / total) * 100) : 0;

    const pn = current ? pi(current.n) : 0;
    const firstPlanned = planned[0] ? pi(planned[0].n) : null;
    const lastPlanned  = planned.length ? pi(planned[planned.length - 1].n) : null;

    // ============ 四个工位 ============
    const stages = FM_STAGE_CFG.map(cfg => {
      const base = { ...cfg };
      if (cfg.key === "snowflake") {
        return {
          ...base,
          big: String(stepsDone.length + stepsActive.length),
          unit: `/ ${STEPS.length} 步`,
          pct: Math.round(((stepsDone.length + stepsActive.length * 0.5) / STEPS.length) * 100),
          status: stepsWarn.length ? { label: `${stepsWarn.length} 项需补`, tone: "gold" } : { label: "已就绪", tone: "sage" },
          detail: {
            lead: `把故事从一句话推到可写的骨架。十步里 ${stepsDone.length} 步已确认。`,
            rows: [
              { t: "已确认步骤", s: "done", note: `${stepsDone.length} / ${STEPS.length} 步` },
              ...stepsActive.map(s => ({ t: s.name, s: "active", note: `进行中 · ${s.blurb}` })),
              ...stepsWarn.map(s => ({ t: s.name, s: "warn", note: "待补全" })),
              ...(STEPS.filter(s => (s.key === "scenes" || s.key === "planning") && s.state === "done").length === 2
                ? [{ t: "场景列表 · 场景规划", s: "done", note: "已完成" }] : []),
            ],
            cta: { label: "继续构思", to: "snowflake", step: warnStep ? warnStep.key : null },
          },
        };
      }
      if (cfg.key === "scene") {
        return {
          ...base,
          big: String(scTotal),
          unit: "场",
          pct: scTotal ? Math.round((scDone / scTotal) * 100) : 0,
          status: { label: `${chWithScenes} 章已铺`, tone: "sage" },
          detail: {
            lead: `已规划的 ${scTotal} 场覆盖前 ${chWithScenes} 章，逐场写好目标、阻碍与出口。`,
            rows: [
              { t: "已规划场景", s: "done", note: `${scTotal} 场 · ${chWithScenes} 章` },
              { t: "已完成", s: "done", note: `${scDone} 场` },
              { t: "写作中 / 待写", s: scWriting ? "active" : "todo", note: `写中 ${scWriting} · 待写 ${scTodo}` },
              { t: "待排程章节", s: "todo", note: planned.length && firstPlanned ? `${planned.length} 章（第 ${firstPlanned}–${lastPlanned} 章）` : "—" },
            ],
            cta: { label: "打开 AI 起草台", to: "scene" },
          },
        };
      }
      if (cfg.key === "writer") {
        return {
          ...base,
          big: String(drafted.length),
          unit: `/ ${total} 章`,
          pct: Math.round((drafted.length / totalForRatio) * 100),
          status: current ? { label: `第 ${pn} 章进行中`, tone: "crimson" } : { label: "进行中", tone: "crimson" },
          detail: {
            lead: current ? `当前的创作前线。第 ${pn} 章正在写，焦点落在${curScene ? `「${curScene.title}」` : "本章场景"}。` : "当前的创作前线。",
            rows: [
              { t: "已动笔章节", s: "done", note: `${drafted.length} 章 · ${fmt(words)} 字` },
              { t: "完整初稿", s: "done", note: fullDraft.length ? `第 1–${pi(fullDraft[fullDraft.length - 1].n)} 章` : "—" },
              { t: "正在写", s: "active", note: current ? `第 ${pn} 章《${current.title}》${curScene ? ` · ${curScene.title}` : ""}` : "—" },
              { t: "待动笔章节", s: "todo", note: total ? `第 ${drafted.length + 1}–${total} 章` : "暂无章节" },
            ],
            cta: { label: "回到写作房间", to: "writer" },
          },
        };
      }
      // manuscripts
      return {
        ...base,
        big: String(approved.length),
        unit: `/ ${total} 章`,
        pct: Math.round((approved.length / totalForRatio) * 100),
        status: review.length ? { label: `${review.length} 章审阅中`, tone: "rose" } : { label: "累积中", tone: "slate" },
        detail: {
          lead: "通过质检与批准的章节汇入成稿中心，等待整书发布。",
          rows: [
            { t: "已批准终稿", s: "done", note: approved.length ? `第 1–${pi(approved[approved.length - 1].n)} 章` : "—" },
            { t: "审阅中", s: review.length ? "warn" : "todo", note: review.length ? review.map(c => `第 ${pi(c.n)} 章`).join("、") : "—" },
            { t: "草稿待审", s: "todo", note: draftCh.length ? draftCh.map(c => `第 ${pi(c.n)} 章`).join("、") : "—" },
            { t: "距整书目标", s: "todo", note: total ? `${total} 章 · 已定稿 ${Math.round((approved.length / total) * 100)}%` : "暂无章节" },
          ],
          cta: { label: "打开成稿中心", to: "manuscripts" },
        },
      };
    });

    // ============ 连接段 + 在途条目 ============
    const reviewCh = review[0];
    const pipes = [
      {
        tone: FM_PIPE_TONES[0],
        chip: warnStep ? { tone: "gold", dir: "back", label: warnStep.name, meta: "回流 → 构思", to: "snowflake", step: warnStep.key } : null,
      },
      {
        tone: FM_PIPE_TONES[1],
        chip: planned.length && firstPlanned ? { tone: "sage", dir: "fwd", label: `第 ${firstPlanned}–${lastPlanned} 章铺场`, meta: "场景 → 排程", to: "scene" } : null,
      },
      {
        tone: FM_PIPE_TONES[2],
        chip: reviewCh ? { tone: "slate", dir: "fwd", label: `第 ${pi(reviewCh.n)} 章`, meta: "审阅 → 成稿", to: "manuscripts" } : null,
      },
    ];

    // ============ 流程体检（只来自目录 / 章节 / 场景的真实状态） ============
    const diag = [];
    diag.push({ tone: "crimson", tag: "瓶颈", text: current
      ? `写作是当前节流点（${drafted.length} / ${total} 章）。聚焦第 ${pn} 章收尾，先清空进行中。`
      : `写作是当前节流点（${drafted.length} / ${total} 章）。先开一章，让流水线动起来。`, to: "writer", _w: 2 });
    diag.sort((a, b) => b._w - a._w);
    const diagTop = diag.slice(0, 4);

    // ============ 本周聚焦（派生唯一最优动作）============
    const queue = [];
    if (current) queue.push({ label: curTodo ? `写完第 ${pn} 章余下 ${curTodo} 场` : `推进第 ${pn} 章`, meta: `前线 ·《${current.title}》`, to: "writer" });
    if (reviewCh) queue.push({ label: `审阅第 ${pi(reviewCh.n)} 章《${reviewCh.title}》`, meta: "成稿中心 · 待批准", to: "manuscripts" });
    const focus = warnStep ? {
      tag: "本周聚焦",
      title: `先补全「${warnStep.name}」`,
      body: planned.length && firstPlanned
        ? `这是构思阶段的待补项。补完即可解锁第 ${firstPlanned}–${lastPlanned} 章铺场，让流水线重新单向往前。`
        : "这是构思阶段的待补项，补完后构思工位即可清空回流。",
      impact: planned.length ? `解锁 ${planned.length} 章 · 清掉回流` : "清掉唯一回流",
      cta: { label: "去补全", to: "snowflake", step: warnStep.key },
      queue,
    } : {
      tag: "本周聚焦",
      title: current ? `推进第 ${pn} 章《${current.title}》` : (ARR.length ? "推进当前前线" : "从构思开始"),
      body: current
        ? `当前没有回流，专注把前线章节写完即可。余 ${curTodo} 场未写。`
        : (ARR.length ? "当前没有回流，挑一章动笔即可。" : "目录还是空的——先用雪花十步把骨架立起来，或直接去写作器开第一章。"),
      impact: "保持单向流动",
      cta: ARR.length ? { label: "回到写作房间", to: "writer" } : { label: "开始构思", to: "snowflake" },
      queue: queue.filter(q => q.to !== "writer"),
    };

    return {
      stages,
      pipes,
      chapters,
      spineCounts,
      overall: { pct: overallPct, sub: `定稿 ${approved.length} / ${total} 章 · 阶段加权` },
      focus,
      diag: diagTop,
    };
  } catch (e) {
    return FM_FALLBACK;
  }
}

function WsFlowmap({ go }) {
  go = go || (() => {});
  /* 订阅目录变化：编排 / 写作 / 成稿的改动会重新派生流程模型 */
  const fmCat = useCatalogChapters ? useCatalogChapters() : null;
  const M = useMemoFM(fmDerive, [fmCat]);
  const [sel, setSel] = useStFM("writer");
  const [playing, setPlaying] = useStFM(false);
  const timer = useRefFM(null);
  const frame = useRefFM(null);
  const active = M.stages.find(s => s.key === sel) || M.stages[2];

  // 带上下文跳转：切到工位的同时，定位到具体那一项（目前用于雪花步骤）
  const jump = (to, step) => {
    if (step) window.__snowStepTarget = step;
    go(to, step ? { type: "ws:snow-step", detail: step } : undefined);
  };

  const play = () => {
    setPlaying(false);
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => {
      setPlaying(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setPlaying(false), 3400);
    });
  };
  useEfFM(() => () => {
    cancelAnimationFrame(frame.current);
    clearTimeout(timer.current);
  }, []);

  return (
    <div className="page flow-page" data-screen-label="flowmap">
      <FlowStyles />
      <div className="page-narrow">

        {/* Header */}
        <section className="flow-strip">
          <div className="flow-strip-lead">
            <div className="page-eyebrow">创作流程</div>
            <h1 className="flow-title text-serif">从构思到成稿，一眼看清整本书在哪</h1>
            <p className="flow-sub">四个工位、一条流水线。下面这条脊就是全书 {M.chapters.length} 章 —— 每个工位的数字都对得上它。</p>
          </div>
          <div className="flow-strip-side">
            <div className="flow-overall">
              <span className="flow-overall-num tab-num">{M.overall.pct}<small>%</small></span>
              <span className="flow-overall-lbl">整书推进度</span>
              <span className="flow-overall-meta">{M.overall.sub}</span>
            </div>
            <button className={`btn btn-accent btn-sm flow-play ${playing ? "is-on" : ""}`} onClick={play}>
              <I.Play size={12} /> 演示流动
            </button>
          </div>
        </section>

        {/* ===== 流水线主卡 ===== */}
        <section className="card flow-line-card">
          {/* 在途条目层 —— 与下方连接段对齐 */}
          <div className="flow-chiprow">
            <span className="flow-slot-stage" />
            {M.pipes.map((p, i) => (
              <React.Fragment key={i}>
                <span className="flow-slot-pipe">
                  {p.chip && (
                    <button
                      className={`flow-chip tone-${p.chip.tone} dir-${p.chip.dir}`}
                      onClick={() => jump(p.chip.to, p.chip.step)}
                      title={p.chip.label + " · " + p.chip.meta}
                    >
                      <span className="flow-chip-dir">
                        {p.chip.dir === "back" ? <I.Refresh size={11} /> : <I.ArrowRight size={11} />}
                      </span>
                      <span className="flow-chip-body">
                        <span className="flow-chip-label text-serif">{p.chip.label}</span>
                        <span className="flow-chip-meta">{p.chip.meta}</span>
                      </span>
                    </button>
                  )}
                  <span className="flow-chip-stem" />
                </span>
                <span className="flow-slot-stage" />
              </React.Fragment>
            ))}
          </div>

          {/* 工位 + 连接段 */}
          <div className="flow-line">
            {playing && <span className="flow-token-demo" />}
            {M.stages.map((s, i) => (
              <React.Fragment key={s.key}>
                <StationNode
                  stage={s}
                  selected={sel === s.key}
                  onSelect={() => setSel(s.key)}
                  onOpen={() => go(s.go)}
                />
                {i < M.stages.length - 1 && (
                  <Pipe tone={M.pipes[i].tone} playing={playing} index={i} backflow={M.pipes[i].chip && M.pipes[i].chip.dir === "back"} />
                )}
              </React.Fragment>
            ))}
          </div>

          {/* 进度脊 */}
          <div className="flow-spine">
            <div className="flow-spine-head">
              <span className="ctx-head"><I.BookOpen size={13} /><span>全书 {M.chapters.length} 章进度脊</span></span>
              <div className="flow-spine-legend">
                {FM_SPINE_ORDER.map(k => (
                  <span key={k} className="flow-leg">
                    <span className="flow-leg-sw" style={{ background: FM_SPINE_META[k].color, borderColor: FM_SPINE_META[k].faint ? "var(--line-2)" : "transparent" }} />
                    {FM_SPINE_META[k].label} <b className="tab-num">{M.spineCounts[k] || 0}</b>
                  </span>
                ))}
              </div>
            </div>
            <div className="flow-spine-bar">
              {M.chapters.map(c => {
                const m = FM_SPINE_META[c.stage];
                return (
                  <span
                    key={c.n}
                    className={`flow-seg ${c.front ? "is-front" : ""} ${m.faint ? "is-faint" : ""}`}
                    style={{ background: c.front ? "var(--crimson)" : m.color }}
                    title={`第 ${c.n} 章 · ${m.label}${c.front ? " · 前线" : ""}`}
                  >
                    {c.front && <span className="flow-seg-flag">前线</span>}
                  </span>
                );
              })}
            </div>
          </div>
        </section>

        {/* ===== 本周聚焦 决策条 ===== */}
        <section className="card flow-focus">
          <div className="flow-focus-main">
            <span className="flow-focus-ic"><I.Target size={20} /></span>
            <div className="flow-focus-text">
              <span className="flow-focus-tag"><I.Zap size={11} /> {M.focus.tag}</span>
              <h3 className="flow-focus-title text-serif">{M.focus.title}</h3>
              <p className="flow-focus-body">{M.focus.body}</p>
              <div className="flow-focus-actions">
                <button className="btn btn-accent btn-sm" onClick={() => jump(M.focus.cta.to, M.focus.cta.step)}>
                  {M.focus.cta.label} <I.ArrowRight size={13} />
                </button>
                <span className="flow-focus-impact"><I.Unlock size={12} /> {M.focus.impact}</span>
              </div>
            </div>
          </div>
          <div className="flow-focus-queue">
            <div className="flow-queue-head">接下来排队</div>
            {M.focus.queue.map((q, i) => (
              <button key={i} className="flow-queue-row" onClick={() => go(q.to)}>
                <span className="flow-queue-n tab-num">{i + 1}</span>
                <span className="flow-queue-body">
                  <span className="flow-queue-label">{q.label}</span>
                  <span className="flow-queue-meta">{q.meta}</span>
                </span>
                <I.ChevronRight size={14} />
              </button>
            ))}
          </div>
        </section>

        {/* ===== 详情 + 体检 ===== */}
        <section className="flow-grid">
          <div className="card flow-detail">
            <div className="card-head">
              <div className="flow-detail-head">
                <span className={`flow-detail-ic tone-${active.tone}`}>{ic(active.icon)}</span>
                <div>
                  <div className="card-title">{active.name} · {active.sub}</div>
                  <div className="card-sub">{active.detail.lead}</div>
                </div>
              </div>
              <span className={`pill pill-${active.status.tone}`}><span className="pill-dot" />{active.status.label}</span>
            </div>
            <div className="flow-detail-meter">
              <div className="flow-detail-meter-fill" style={{ width: active.pct + "%", background: `var(--${active.tone})` }} />
            </div>
            <ul className="flow-checks">
              {active.detail.rows.map((r, i) => (
                <li key={i} className={`flow-check s-${r.s}`}>
                  <span className="flow-check-mark">
                    {r.s === "done" && <I.Check size={12} />}
                    {r.s === "active" && <span className="flow-mini-dot" />}
                    {r.s === "warn" && <I.AlertTriangle size={12} />}
                    {r.s === "todo" && <I.Circle size={11} />}
                  </span>
                  <span className="flow-check-text">{r.t}</span>
                  {r.note && <span className="flow-check-note">{r.note}</span>}
                </li>
              ))}
            </ul>
            <div className="flow-detail-foot">
              <button className="btn btn-primary btn-sm" onClick={() => jump(active.detail.cta.to, active.detail.cta.step)}>
                {active.detail.cta.label} <I.ArrowRight size={13} />
              </button>
              <span className="flow-detail-hint">点上方工位可切换查看</span>
            </div>
          </div>

          <div className="card flow-bottle">
            <div className="card-head">
              <div>
                <div className="card-title" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  流程体检
                </div>
                <div className="card-sub">系统看到的回流、瓶颈与风险（按严重度）</div>
              </div>
              <span className="pill pill-crimson"><span className="pill-dot" />{M.diag.length} 处</span>
            </div>
            <ul className="flow-bottle-list">
              {M.diag.map((b, i) => (
                <li key={i} className="flow-bottle-row" onClick={() => go(b.to)}>
                  <span className="flow-bottle-rank tab-num">{i + 1}</span>
                  <span className={`pill pill-${b.tone} text-xs`}><span className="pill-dot" />{b.tag}</span>
                  <span className="flow-bottle-text">{b.text}</span>
                  <I.ChevronRight size={14} />
                </li>
              ))}
            </ul>
          </div>
        </section>
      </div>
    </div>
  );
}

function StationNode({ stage, selected, onSelect, onOpen }) {
  const r = 30, c = 2 * Math.PI * r;
  const dash = (stage.pct / 100) * c;
  return (
    <div className={`flow-node tone-${stage.tone} ${selected ? "is-sel" : ""} ${stage.active ? "is-active" : ""}`}>
      {stage.active && <span className="flow-node-front">前线</span>}
      <button className="flow-node-ring" onClick={onSelect} title={`查看 ${stage.name}`}>
        <svg width="78" height="78" viewBox="0 0 78 78">
          <circle cx="39" cy="39" r={r} fill="none" stroke="var(--line-1)" strokeWidth="5" />
          <circle
            cx="39" cy="39" r={r} fill="none"
            stroke="var(--flow-accent)" strokeWidth="5" strokeLinecap="round"
            strokeDasharray={`${dash} ${c}`} transform="rotate(-90 39 39)"
            style={{ transition: "stroke-dasharray 700ms cubic-bezier(0.16,1,0.3,1)" }}
          />
        </svg>
        <span className="flow-node-ic">{ic(stage.icon)}</span>
        {stage.active && <span className="flow-node-pulse" />}
      </button>
      <div className="flow-node-name text-serif">{stage.name}</div>
      <div className="flow-node-count"><span className="tab-num">{stage.big}</span> <small>{stage.unit}</small></div>
      <span className={`pill pill-${stage.status.tone} text-xs flow-node-pill`}><span className="pill-dot" />{stage.status.label}</span>
      <button className="flow-node-open" onClick={onOpen}>打开 <I.ArrowRight size={11} /></button>
    </div>
  );
}

function Pipe({ tone, playing, index, backflow }) {
  return (
    <div className={`flow-pipe tone-${tone} ${playing ? "is-flowing" : ""} ${backflow ? "has-back" : ""}`} style={{ "--pipe-i": index }}>
      <span className="flow-pipe-fill" />
      {backflow && <span className="flow-pipe-back" title="有内容需要回流补全" />}
    </div>
  );
}

function ic(name) {
  const Ic = I[name] || I.Dot;
  return <Ic size={20} />;
}

/* 静态兜底（数据源缺失时）—— 与派生结果同形 */
const FM_FALLBACK = {
  stages: FM_STAGE_CFG.map(cfg => ({
    ...cfg, big: "—", unit: "", pct: 0,
    status: { label: "加载中", tone: "slate" },
    detail: { lead: "数据加载中…", rows: [], cta: { label: "打开", to: cfg.go } },
  })),
  pipes: FM_PIPE_TONES.map(tone => ({ tone, chip: null })),
  chapters: [],
  spineCounts: {},
  overall: { pct: 0, sub: "—" },
  focus: { tag: "本周聚焦", title: "数据加载中…", body: "", impact: "", cta: { label: "打开", to: "writer" }, queue: [] },
  diag: [],
};

/* ---- self-contained styles ---- */
function FlowStyles() {
  return (
    <style>{`
.flow-page { padding-bottom: 64px; }
.ctx-head { display:inline-flex; align-items:center; gap:7px; font-size:12px; font-weight:600; letter-spacing:0.02em; color:var(--ink-2); }
.ctx-head svg { color:var(--ink-3); }

/* header */
.flow-strip { display:flex; align-items:flex-start; justify-content:space-between; gap:24px; margin-bottom:22px; }
.flow-strip-lead { min-width:0; }
.flow-title { font-size:26px; line-height:1.18; letter-spacing:-0.01em; }
.flow-sub { margin-top:8px; color:var(--ink-3); font-size:13.5px; max-width:62ch; }
.flow-strip-side { display:flex; align-items:center; gap:18px; flex-shrink:0; }
.flow-overall { text-align:right; line-height:1.1; display:flex; flex-direction:column; gap:2px; }
.flow-overall-num { font-family:var(--font-serif); font-weight:600; font-size:34px; color:var(--ink-1); }
.flow-overall-num small { font-size:18px; color:var(--ink-3); margin-left:1px; }
.flow-overall-lbl { font-size:11px; letter-spacing:0.12em; text-transform:uppercase; color:var(--ink-4); white-space:nowrap; }
.flow-overall-meta { font-size:11.5px; color:var(--ink-3); white-space:nowrap; }
.flow-play.is-on { box-shadow:0 0 0 4px var(--crimson-wash); }

/* ===== 流水线主卡 ===== */
.flow-line-card { padding:18px 22px 20px; overflow:hidden; }

/* 在途条目层 —— 与连接段一一对齐的占位结构 */
.flow-chiprow { display:flex; align-items:flex-end; min-height:56px; }
.flow-slot-stage { flex:0 0 ${STAGE_W}px; }
.flow-slot-pipe { flex:1 1 auto; min-width:48px; position:relative; display:flex; justify-content:center; align-items:flex-end; height:56px; }
.flow-chip-stem { position:absolute; bottom:-6px; left:50%; transform:translateX(-50%); width:1px; height:14px; background:var(--line-2); }
.flow-chip {
  display:flex; align-items:center; gap:8px; padding:6px 11px 6px 8px; border-radius:11px;
  background:var(--paper-0); border:1px solid var(--line-2); box-shadow:var(--shadow-sm);
  max-width:172px; cursor:pointer; text-align:left;
  transition:transform 180ms var(--ease-spring,ease), box-shadow 180ms, border-color 140ms;
}
.flow-chip:hover { box-shadow:var(--shadow-md); transform:translateY(-2px); }
.flow-chip-dir { flex:0 0 20px; width:20px; height:20px; border-radius:6px; display:grid; place-items:center; color:#fff; }
.flow-chip.tone-gold .flow-chip-dir{background:var(--gold);} .flow-chip.tone-sage .flow-chip-dir{background:var(--sage);}
.flow-chip.tone-crimson .flow-chip-dir{background:var(--crimson);} .flow-chip.tone-slate .flow-chip-dir{background:var(--slate);}
.flow-chip-body { display:flex; flex-direction:column; line-height:1.2; min-width:0; }
.flow-chip-label { font-size:13px; font-weight:600; color:var(--ink-1); white-space:nowrap; }
.flow-chip-meta { font-size:10.5px; color:var(--ink-3); white-space:nowrap; }
.flow-chip.dir-back { border-color:var(--gold-soft); background:var(--gold-wash); }
.flow-chip.dir-back .flow-chip-meta { color:var(--gold); }
.flow-chip.dir-back .flow-chip-stem { background:var(--gold-soft); }

/* 工位 + 连接段 */
.flow-line { display:flex; align-items:flex-start; position:relative; }
.flow-node {
  --flow-accent: var(--ink-3);
  flex:0 0 ${STAGE_W}px; display:flex; flex-direction:column; align-items:center; text-align:center; gap:7px;
  padding:8px 8px 10px; border-radius:16px; position:relative; z-index:2;
  transition:box-shadow 200ms var(--ease-soft,ease), transform 200ms var(--ease-soft,ease);
}
.flow-node.tone-gold    { --flow-accent: var(--gold); }
.flow-node.tone-sage    { --flow-accent: var(--sage); }
.flow-node.tone-crimson { --flow-accent: var(--crimson); }
.flow-node.tone-slate   { --flow-accent: var(--slate); }
.flow-node.is-sel { background:var(--paper-0); box-shadow:inset 0 0 0 1px var(--line-1), var(--shadow-sm); }
.flow-node.is-active.is-sel { box-shadow:inset 0 0 0 1px var(--crimson-wash), var(--shadow-md); }
.flow-node-front {
  position:absolute; top:0; left:50%; transform:translateX(-50%); z-index:4;
  font-size:9.5px; font-weight:700; letter-spacing:0.14em; color:#fff; background:var(--crimson);
  padding:2px 8px; border-radius:999px; box-shadow:var(--shadow-sm);
}
.flow-node.is-active { padding-top:18px; }
.flow-node-ring {
  position:relative; width:78px; height:78px; border:0; background:transparent; padding:0; cursor:pointer;
  display:grid; place-items:center; border-radius:50%;
  transition:transform 220ms var(--ease-spring,ease);
}
.flow-node-ring:hover { transform:translateY(-2px) scale(1.03); }
.flow-node-ring svg { position:absolute; inset:0; }
.flow-node-ic { color:var(--flow-accent); display:grid; place-items:center; }
.flow-node.is-active .flow-node-ring { transform:scale(1.06); }
.flow-node.is-active .flow-node-ring:hover { transform:translateY(-2px) scale(1.09); }
.flow-node-pulse {
  position:absolute; inset:7px; border-radius:50%; border:2px solid var(--crimson);
  opacity:0; animation:flowPulse 2.4s ease-out infinite;
}
@keyframes flowPulse { 0%{transform:scale(0.86);opacity:0.5;} 70%{transform:scale(1.2);opacity:0;} 100%{opacity:0;} }
.flow-node-name { font-size:16px; font-weight:600; }
.flow-node-count { font-size:13px; color:var(--ink-2); }
.flow-node-count .tab-num { font-family:var(--font-serif); font-weight:600; font-size:19px; color:var(--ink-1); }
.flow-node-count small { color:var(--ink-3); font-size:11.5px; }
.flow-node-pill { margin-top:1px; }
.flow-node-open {
  margin-top:2px; border:0; background:transparent; color:var(--ink-3); font-size:11.5px; font-weight:600;
  display:inline-flex; align-items:center; gap:3px; cursor:pointer; padding:3px 8px; border-radius:999px;
  opacity:0; transform:translateY(-2px); transition:all 180ms var(--ease-soft,ease);
}
.flow-node:hover .flow-node-open, .flow-node.is-sel .flow-node-open { opacity:1; transform:none; }
.flow-node-open:hover { background:var(--paper-2); color:var(--ink-1); }

/* 连接段 */
.flow-pipe {
  --flow-accent: var(--ink-3); position:relative; flex:1 1 auto; height:6px; align-self:flex-start; margin-top:36px;
  background:var(--line-1); border-radius:999px; overflow:visible; min-width:48px;
}
.flow-pipe.tone-gold{--flow-accent:var(--gold);} .flow-pipe.tone-sage{--flow-accent:var(--sage);}
.flow-pipe.tone-crimson{--flow-accent:var(--crimson);} .flow-pipe.tone-slate{--flow-accent:var(--slate);}
.flow-pipe-fill {
  position:absolute; inset:0; border-radius:999px; opacity:0.7;
  background:linear-gradient(90deg, transparent, var(--flow-accent) 50%, transparent);
  background-size:42% 100%; background-repeat:no-repeat; background-position:-60% 0;
  animation:pipeIdle 3.6s linear infinite; animation-delay:calc(var(--pipe-i,0) * -1.1s);
}
@keyframes pipeIdle { 0%{background-position:-60% 0;} 100%{background-position:160% 0;} }
.flow-pipe.is-flowing .flow-pipe-fill { animation-duration:1.1s; opacity:1; }
.flow-pipe.has-back .flow-pipe-fill { animation-direction:reverse; }
.flow-pipe-back {
  position:absolute; top:-6px; left:50%; transform:translateX(-50%);
  width:8px; height:8px; border-radius:50%; background:var(--gold);
  box-shadow:0 0 0 4px var(--gold-wash); animation:flowBack 2.6s ease-in-out infinite;
}
@keyframes flowBack { 0%,100%{transform:translateX(-50%) translateX(7px);} 50%{transform:translateX(-50%) translateX(-7px);} }

/* demo token */
.flow-token-demo {
  position:absolute; top:39px; left:${STAGE_W - 6}px; width:11px; height:11px; border-radius:50%;
  background:var(--crimson); box-shadow:0 0 0 5px var(--crimson-wash), var(--shadow-md); z-index:3;
  animation:flowTravel 3.2s cubic-bezier(0.5,0,0.5,1) forwards;
}
@keyframes flowTravel {
  0%{left:${STAGE_W - 6}px; transform:scale(0.6); opacity:0;}
  8%{opacity:1; transform:scale(1);}
  92%{opacity:1; transform:scale(1);}
  100%{left:calc(100% - ${STAGE_W}px); transform:scale(0.6); opacity:0;}
}

/* 进度脊 */
.flow-spine { margin-top:18px; padding-top:16px; border-top:1px solid var(--line-1); }
.flow-spine-head { display:flex; align-items:center; justify-content:space-between; gap:14px; margin-bottom:10px; flex-wrap:wrap; }
.flow-spine-legend { display:flex; align-items:center; gap:14px; }
.flow-leg { display:inline-flex; align-items:center; gap:5px; font-size:11.5px; color:var(--ink-3); }
.flow-leg-sw { width:11px; height:11px; border-radius:3px; border:1px solid transparent; }
.flow-leg b { color:var(--ink-1); font-weight:600; }
.flow-spine-bar { display:flex; gap:3px; height:30px; }
.flow-seg {
  flex:1 1 0; border-radius:3px; position:relative;
  transition:transform 140ms var(--ease-spring,ease), filter 140ms;
}
.flow-seg.is-faint { box-shadow:inset 0 0 0 1px var(--line-2); }
.flow-seg:hover { transform:translateY(-2px); filter:brightness(1.05); cursor:default; }
.flow-seg.is-front { box-shadow:0 0 0 2px var(--paper-1), 0 0 0 3px var(--crimson); z-index:2; }
.flow-seg-flag {
  position:absolute; top:-19px; left:50%; transform:translateX(-50%);
  font-size:9px; font-weight:700; letter-spacing:0.1em; color:var(--crimson); white-space:nowrap;
}

/* ===== 本周聚焦 ===== */
.flow-focus {
  margin-top:18px; display:grid; grid-template-columns:1fr 280px; gap:0;
  padding:0; overflow:hidden; border-color:var(--line-2);
  background:linear-gradient(180deg, var(--crimson-wash) 0%, transparent 42%), var(--paper-1);
}
.flow-focus-main { display:flex; gap:16px; padding:20px 22px; min-width:0; }
.flow-focus-ic {
  flex:0 0 44px; width:44px; height:44px; border-radius:13px; display:grid; place-items:center;
  background:var(--crimson); color:#fff; box-shadow:var(--shadow-sm);
}
.flow-focus-text { min-width:0; }
.flow-focus-tag {
  display:inline-flex; align-items:center; gap:5px; font-size:11px; font-weight:700; letter-spacing:0.08em;
  color:var(--crimson); text-transform:uppercase; margin-bottom:6px;
}
.flow-focus-title { font-size:20px; line-height:1.25; letter-spacing:-0.01em; }
.flow-focus-body { margin-top:7px; color:var(--ink-2); font-size:13.5px; line-height:1.6; max-width:64ch; text-wrap:pretty; }
.flow-focus-actions { display:flex; align-items:center; gap:14px; margin-top:14px; flex-wrap:wrap; }
.flow-focus-impact { display:inline-flex; align-items:center; gap:5px; font-size:12px; font-weight:600; color:var(--sage); }
.flow-focus-queue {
  border-left:1px solid var(--line-1); padding:18px 18px 16px; background:var(--paper-0);
  display:flex; flex-direction:column; gap:6px;
}
.flow-queue-head { font-size:10.5px; letter-spacing:0.14em; text-transform:uppercase; color:var(--ink-4); margin-bottom:2px; }
.flow-queue-row {
  display:flex; align-items:center; gap:10px; padding:9px 8px; border-radius:9px; border:0; background:transparent;
  width:100%; text-align:left; cursor:pointer; transition:background 140ms;
}
.flow-queue-row:hover { background:var(--paper-2); }
.flow-queue-n {
  flex:0 0 20px; width:20px; height:20px; border-radius:50%; display:grid; place-items:center;
  font-size:11px; font-weight:700; color:var(--ink-3); background:var(--paper-2); border:1px solid var(--line-1);
}
.flow-queue-body { display:flex; flex-direction:column; line-height:1.25; min-width:0; flex:1; }
.flow-queue-label { font-size:13px; font-weight:600; color:var(--ink-1); }
.flow-queue-meta { font-size:11px; color:var(--ink-3); }
.flow-queue-row svg { color:var(--ink-4); flex-shrink:0; }

/* ===== 详情 + 体检 ===== */
.flow-grid { display:grid; grid-template-columns:1.08fr 0.92fr; gap:18px; margin-top:18px; }
.flow-detail-head { display:flex; align-items:flex-start; gap:12px; }
.flow-detail-ic { flex:0 0 40px; width:40px; height:40px; border-radius:11px; display:grid; place-items:center; color:#fff; }
.flow-detail-ic.tone-gold{background:var(--gold);} .flow-detail-ic.tone-sage{background:var(--sage);}
.flow-detail-ic.tone-crimson{background:var(--crimson);} .flow-detail-ic.tone-slate{background:var(--slate);}
.flow-detail-meter { height:5px; border-radius:999px; background:var(--line-1); overflow:hidden; margin:2px 0 6px; }
.flow-detail-meter-fill { height:100%; border-radius:999px; transition:width 700ms cubic-bezier(0.16,1,0.3,1); }
.flow-checks { list-style:none; margin:4px 0 0; padding:0; display:flex; flex-direction:column; }
.flow-check { display:flex; align-items:center; gap:10px; padding:11px 2px; border-bottom:1px solid var(--line-1); }
.flow-check:last-child { border-bottom:0; }
.flow-check-mark { flex:0 0 18px; width:18px; height:18px; border-radius:50%; display:grid; place-items:center; }
.flow-check.s-done .flow-check-mark { background:var(--sage-wash); color:var(--sage); }
.flow-check.s-warn .flow-check-mark { background:var(--gold-wash); color:var(--gold); }
.flow-check.s-active .flow-check-mark { background:var(--crimson-wash); color:var(--crimson); }
.flow-check.s-todo .flow-check-mark { color:var(--ink-4); }
.flow-mini-dot { width:7px; height:7px; border-radius:50%; background:var(--crimson); animation:flowHot 1.6s ease-in-out infinite; }
@keyframes flowHot { 0%,100%{box-shadow:0 0 0 0 var(--crimson-wash);} 50%{box-shadow:0 0 0 6px var(--crimson-wash);} }
.flow-check-text { font-size:13.5px; color:var(--ink-1); }
.flow-check-note { margin-left:auto; font-size:12px; color:var(--ink-3); text-align:right; }
.flow-detail-foot { margin-top:16px; display:flex; align-items:center; justify-content:space-between; gap:12px; }
.flow-detail-hint { font-size:11.5px; color:var(--ink-4); }

.flow-bottle-list { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:4px; }
.flow-bottle-row {
  display:flex; align-items:flex-start; gap:10px; padding:12px 10px; border-radius:10px; cursor:pointer;
  transition:background 140ms;
}
.flow-bottle-row:hover { background:var(--paper-2); }
.flow-bottle-rank {
  flex:0 0 20px; width:20px; height:20px; border-radius:50%; display:grid; place-items:center; margin-top:1px;
  font-size:11px; font-weight:700; color:var(--ink-3); background:var(--paper-2); border:1px solid var(--line-1);
}
.flow-bottle-row .pill { flex-shrink:0; margin-top:1px; }
.flow-bottle-text { font-size:13px; color:var(--ink-2); line-height:1.5; text-wrap:pretty; }
.flow-bottle-row svg { flex-shrink:0; color:var(--ink-4); margin-top:3px; }

@media (max-width:1080px){
  .flow-grid{grid-template-columns:1fr;}
  .flow-focus{grid-template-columns:1fr;}
  .flow-focus-queue{border-left:0; border-top:1px solid var(--line-1);}
}
    `}</style>
  );
}

/* ESM 导出（Phase 1 机械追加；window.* 赋值过渡期保留） */
export { WsFlowmap };

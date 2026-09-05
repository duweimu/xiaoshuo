import React from "react";
import { I } from "./icons.jsx";
import { WsWorks } from "./ws-works.jsx";
import { rvPush } from "./ws-review.jsx";
import { onRovingTabKeyDown } from "./a11y-tabs.js";
import { SrValidation } from "./ws-styleref-val.jsx";
import {
  apiDelete,
  apiGet,
  apiPost,
  buildUrl,
  getOperatorRef,
  getRemoteAccessToken,
} from "./lib/client.js";

/* global React, I */
const { useState: useStSR } = React;

const SR_CLOUD_POLICIES = [
  {
    id: "local_only",
    label: "仅保存在本机",
    badge: "默认 · 最私密",
    detail: "原文不发送给云端模型。可以导入与本地分段，但云端风格抽取会保持关闭。",
  },
  {
    id: "segments_only",
    label: "只发送所需段落",
    badge: "折中",
    detail: "抽取时仅发送任务需要的分段，不上传整本；适合希望使用云端分析又控制出域范围的情况。",
  },
  {
    id: "allow_full_cloud",
    label: "允许全文上云",
    badge: "能力完整",
    detail: "服务可按任务发送更大范围乃至全文给已配置的模型供应商；只应在你确认拥有授权时使用。",
  },
];

/* 导入权属声明（后端 ingest._normalize_rights_declaration §5.9）：
   - analysis_rights：作者确认有权对这本书做风格分析（所有策略都要勾）
   - send_rights：作者确认有权把段落送往云端模型；非 local_only 策略后端强制要求为 true，
     否则 400 STYLE_REFERENCE_SEND_RIGHTS_DECLARATION_REQUIRED。声明缺失绝不由前端默认补上。 */
const SR_RIGHTS_TERMS = {
  analysis: "我确认拥有对这本书进行风格分析的权利，且只用于学习抽象技法，不复刻原文、人物或桥段。",
  send: "我确认拥有这本书的发送权，并授权系统按所选策略把段落发送给已配置的云端模型供应商。",
};

function srPolicyNeedsSendRights(cloudPolicy) {
  return cloudPolicy !== "local_only";
}

/* 声明是否满足所选策略：分析权必勾；云端策略额外要求发送权。 */
function srRightsReady(cloudPolicy, rights) {
  if (!rights || rights.analysis_rights !== true) return false;
  return !srPolicyNeedsSendRights(cloudPolicy) || rights.send_rights === true;
}

/* ==========================================================
   风格参考 — Style Reference Module
   Pipeline: 书库 → 维度矩阵(抽取) → 风格画像 → 注入应用
   Signature: 4 层 × 16 sub-dim DimensionMatrix
   ========================================================== */

/* ---- 4 layers × 4 sub-dims = 16 sub-dimensions ---- */
const SR_LAYERS = [
  {
    id: "language", name: "语言层", abbr: "语", input: "high",
    subs: [
      { id: "sentence_structure", name: "句式结构", conf: "high",   obs: 7, fp: 2, q: 18 },
      { id: "vocabulary",         name: "词汇选择", conf: "high",   obs: 6, fp: 1, q: 15 },
      { id: "rhetoric",           name: "修辞手法", conf: "medium", obs: 4, fp: 3, q: 11 },
      { id: "punctuation",        name: "标点节奏", conf: "high",   obs: 5, fp: 0, q: 9  },
    ],
  },
  {
    id: "narrative", name: "叙事层", abbr: "叙", input: "medium",
    subs: [
      { id: "perspective",         name: "叙事视角", conf: "high",   obs: 5, fp: 1, q: 12 },
      { id: "pacing",              name: "节奏控制", conf: "medium", obs: 4, fp: 2, q: 8  },
      { id: "time_handling",       name: "时间处理", conf: "medium", obs: 3, fp: 1, q: 7  },
      { id: "information_density", name: "信息密度", conf: "low",    obs: 2, fp: 0, q: 4  },
    ],
  },
  {
    id: "scene", name: "场景层", abbr: "景", input: "high",
    subs: [
      { id: "environment",        name: "环境描写", conf: "high",   obs: 6, fp: 1, q: 14 },
      { id: "character_portrayal",name: "人物刻画", conf: "high",   obs: 5, fp: 2, q: 13 },
      { id: "dialogue",           name: "对话写法", conf: "medium", obs: 4, fp: 1, q: 10 },
      { id: "sensory_priority",   name: "感官优先", conf: "medium", obs: 3, fp: 0, q: 6  },
    ],
  },
  {
    id: "theme", name: "主题层", abbr: "题", input: "skip",
    subs: [
      { id: "emotional_tone",      name: "情感基调", conf: "skip", obs: 0, fp: 0, q: 0 },
      { id: "values",              name: "价值取向", conf: "skip", obs: 0, fp: 0, q: 0 },
      { id: "motifs",              name: "母题意象", conf: "skip", obs: 0, fp: 0, q: 0 },
      { id: "narrative_philosophy",name: "叙事哲学", conf: "skip", obs: 0, fp: 0, q: 0 },
    ],
  },
];

/* ---- findings for a couple of sub-dims (rich), with evidence ---- */

/* ---- hard metrics (sample) ---- */
const SR_METRICS = [
  { name: "平均句长",      key: "avg_sentence_length",     mean: 16.8, std: 11.2, unit: "字" },
  { name: "句长标准差",    key: "sentence_length_std",     mean: 11.2, std: 3.1,  unit: "" },
  { name: "短句率(≤10)",   key: "short_sentence_ratio",    mean: 0.41, std: 0.09, unit: "", pct: true },
  { name: "对话占比",      key: "dialogue_ratio",          mean: 0.23, std: 0.07, unit: "", pct: true },
  { name: "比喻密度/千字", key: "metaphor_density_per_1k", mean: 3.2,  std: 1.8,  unit: "" },
  { name: "文言词比率",    key: "classical_word_ratio",    mean: 0.14, std: 0.05, unit: "", pct: true },
  { name: "视觉感官/千字", key: "sensory_visual_per_1k",   mean: 8.1,  std: 2.6,  unit: "" },
  { name: "破折号密度/千", key: "dash_em_density_per_1k",  mean: 2.4,  std: 1.1,  unit: "" },
];


/* ---- books（真相来自后端 style-reference v2；本地不再内置演示样书）---- */
let SR_BOOKS = [];

const SR_STAGES = [
  { id: "overview",   name: "概览",     icon: "Activity" },
  { id: "matrix",     name: "维度矩阵", icon: "Grid" },
  { id: "profile",    name: "风格画像", icon: "Sparkles" },
  { id: "validation", name: "回测校验", icon: "Flask" },
  { id: "apply",      name: "注入应用", icon: "Sliders" },
];


/* ---- extraction cost / coverage trend (last 14 runs) ---- */
const SR_TREND = [3, 5, 4, 8, 6, 9, 12, 10, 14, 11, 13, 16, 12, 16];

/* ==========================================================
   深层页真后端：hook + 真实数据映射器
   有真书(book.real)→ 懒加载并订阅 sr:deep-changed；映射器把
   stats_json / findings 映成各 stage 需要的形状，缺数据返 null → 调用方回退演示。
   ========================================================== */
function useSrDeep(book) {
  const isReal = !!(book && book.real);
  const [deep, setDeep] = React.useState(() => (isReal && window.srDeepFor ? window.srDeepFor(book.id) : null));
  React.useEffect(() => {
    if (!isReal) { setDeep(null); return; }
    const sync = () => setDeep(window.srDeepFor ? window.srDeepFor(book.id) : null);
    sync();
    if (window.srLoadDeep) window.srLoadDeep(book.id);
    window.addEventListener("sr:deep-changed", sync);
    return () => window.removeEventListener("sr:deep-changed", sync);
  }, [isReal, book && book.id]);
  return deep;
}

const SR_PARA_LABEL = {
  narration: "叙述", dialogue: "对话", description_env: "环境", psychology: "心理",
  action: "动作", description_char: "人物", transition: "转场", flashback: "闪回",
};
const SR_INPUT_LABEL = { skip: "语料不足", low: "偏少", medium: "适中", high: "充足" };

function srStatsOf(deep) { return (deep && deep.book && deep.book.stats_json) || null; }

/* stats_json.metrics（26 项）按 SR_METRICS 的展示名/单位取真实 mean/std；缺项跳过 */
function srRealMetrics(stats) {
  const m = (stats && stats.metrics) || {};
  return SR_METRICS.map(d => {
    const real = m[d.key];
    if (!real || real.mean == null) return null;
    return { ...d, mean: Number(real.mean), std: Number(real.std) };
  }).filter(Boolean);
}

/* stats_json.paragraph_type_distribution → [{type,key,v}]（降序） */
function srRealParaDist(stats) {
  const dist = (stats && stats.paragraph_type_distribution) || {};
  return Object.entries(dist)
    .map(([key, v]) => ({ type: SR_PARA_LABEL[key] || key, key, v: Number(v) || 0 }))
    .sort((a, b) => b.v - a.v);
}

function WsStyleRef({ go }) {
  const [bookId, setBookId] = useStSR("b1");
  const [stage, setStage] = useStSR("matrix");
  const [headerBusy, setHeaderBusy] = useStSR(null);
  const [delBusy, setDelBusy] = useStSR(null);
  const [importOpen, setImportOpen] = useStSR(false);
  const book = SR_BOOKS.find(b => b.id === bookId) || SR_BOOKS[0];

  /* FE-ALIGN F5 授权接缝：书库由后端背书，变化时重渲染 */
  const [, setSrPing] = useStSR(0);
  React.useEffect(() => {
    const f = () => setSrPing(p => p + 1);
    window.addEventListener("sr:books-changed", f);
    return () => window.removeEventListener("sr:books-changed", f);
  }, []);

  const busyRef = React.useRef(null);
  const runHeaderAction = (id) => {
    if (headerBusy) return;
    setHeaderBusy(id);
    /* 真实书走后端动作（LLM 未启用时弹明确引导）；演示书保留原模拟节奏 */
    if (book && book.real && window.srBookAction) {
      window.srBookAction(id, book.id).finally(() => setHeaderBusy(null));
      return;
    }
    clearTimeout(busyRef.current);
    busyRef.current = setTimeout(() => setHeaderBusy(null), 1400);
  };
  React.useEffect(() => () => clearTimeout(busyRef.current), []);

  /* 删除参考书（仅真实书）：confirm → DELETE 端点（级联清除全部衍生数据）→ 刷新书库。
     删的若是当前选中书，切到刷新后剩余的第一本（srDeleteBook 内部已 srSyncBooks 重置 SR_BOOKS；
     删光最后一本时书库回落演示书，next 即演示书首项）。 */
  const onDeleteBook = async (b) => {
    if (delBusy || !b || !window.srDeleteBook) return;
    const ok = window.confirm(
      `确认删除参考书《${b.title}》？\n\n` +
      "将一并清除它的全部衍生数据（抽取 findings、证据引文、风格画像、注入绑定、回测报告），此操作不可恢复。"
    );
    if (!ok) return;
    // 删的若是当前选中书，删后切到原位置的邻居（srDeleteBook 内部已 srSyncBooks 重置 SR_BOOKS）。
    const wasActive = bookId === b.id;
    const idx = SR_BOOKS.findIndex(x => x.id === b.id);
    setDelBusy(b.id);
    try {
      await window.srDeleteBook(b.id);
      if (wasActive && SR_BOOKS.length) {
        const next = SR_BOOKS[Math.min(Math.max(idx, 0), SR_BOOKS.length - 1)];
        if (next) setBookId(next.id);
      }
    } catch (e) {
      window.alert("删除失败：" + ((e && e.message) || e));
    } finally {
      setDelBusy(null);
    }
  };

  return (
    <div className="sr-page" data-screen-label="styleref">
      <div className="sr-cols">
        {/* Left: books */}
        <aside className="sr-books">
          <header className="sr-books-head">
            <div>
              <div className="page-eyebrow" style={{margin:0, display:"flex", alignItems:"center", gap:8}}>风格参考</div>
              <h2 className="text-serif" style={{fontSize:18, margin:"4px 0 0"}}>参考书库</h2>
            </div>
            <button className="btn btn-accent btn-sm" aria-label="导入参考书" onClick={() => setImportOpen(true)}><I.Plus size={13} /></button>
          </header>

          <ul className="sr-book-list">
            {SR_BOOKS.map(b => (
              <li key={b.id} className="sr-book-item">
                <button className={`sr-book ${bookId === b.id ? "is-active" : ""}`} onClick={() => setBookId(b.id)}>
                  <span className={`sr-book-spine spine-${b.color}`} />
                  <span className="sr-book-body">
                    <span className="sr-book-title text-serif">{b.title}</span>
                    <span className="sr-book-author">{b.author} · {(b.chars/10000).toFixed(1)} 万字</span>
                    <span className="sr-book-run">{b.run}</span>
                  </span>
                  <SrBookState s={b.status} />
                </button>
                {b.real && (
                  <button
                    type="button"
                    className={`sr-book-del${delBusy === b.id ? " is-busy" : ""}`}
                    data-sr-del={b.id}
                    title="删除这本参考书"
                    aria-label={`删除参考书《${b.title}》`}
                    disabled={!!delBusy}
                    onClick={() => onDeleteBook(b)}
                  >
                    {delBusy === b.id
                      ? <span className="sr-spin" style={{ display: "inline-flex" }}><I.Refresh size={13} /></span>
                      : <I.Trash size={13} />}
                  </button>
                )}
              </li>
            ))}
          </ul>

          <button type="button" className="sr-books-import" onClick={() => setImportOpen(true)}>
            <I.FileInput size={16} />
            <div>
              <div className="fw-600 text-sm">导入参考书</div>
              <div className="text-xs text-muted">epub · docx · txt · md · 先选择隐私边界</div>
            </div>
          </button>
          <p className="sr-safe-note">
            <I.ShieldCheck size={12} />
            <span>不限内置作者；从每次导入的任意作品动态学习抽象风格，不复刻原文表达、人物或桥段。</span>
          </p>
        </aside>

        {/* Right: stage workspace（书库为空时给空状态；删光最后一本后 SR_BOOKS 为 []，
           book 为 undefined，若继续解引用 book.color/book.author 会抛错整页白屏——
           演示样书已下线，不再有「回落演示书」兜底，必须显式处理空库） */}
        {book ? (
        <section className="sr-stage">
          <header className="sr-stage-head">
            <div className="flex items-center gap-3">
              <div className={`sr-stage-mark spine-${book.color}`}>{book.author[0]}</div>
              <div>
                <h1 className="sr-stage-title text-serif">{book.title}</h1>
                <div className="text-muted text-sm">{book.author} · {book.chars.toLocaleString()} 字 · {book.run}</div>
              </div>
            </div>
            <div className="flex gap-2 items-center">
              <button className="btn btn-quiet btn-sm" disabled={!!headerBusy} onClick={() => runHeaderAction("reclassify")}>
                <span className={headerBusy === "reclassify" ? "sr-spin" : ""} style={{ display: "inline-flex" }}><I.Refresh size={13} /></span> {headerBusy === "reclassify" ? "重新分类中…" : "重新分类"}
              </button>
              <button className="btn btn-ghost btn-sm" disabled={!!headerBusy} onClick={() => runHeaderAction("rerun")}>
                {headerBusy === "rerun" ? <><span className="sr-spin" style={{ display: "inline-flex" }}><I.Refresh size={13} /></span> 重跑抽取中…</> : "重跑抽取"}
              </button>
            </div>
          </header>

          <nav className="sr-stepper" aria-label="风格参考流水线">
            {SR_STAGES.map((s, i) => {
              const Ic = I[s.icon] || I.Dot;
              const active = stage === s.id;
              const idx = SR_STAGES.findIndex(x => x.id === stage);
              const done = i < idx;
              return (
                <React.Fragment key={s.id}>
                  {i > 0 && <span className={`sr-step-line ${i <= idx ? "is-done" : ""}`} aria-hidden="true" />}
                  <button
                    className={`sr-step ${active ? "is-active" : ""} ${done ? "is-done" : ""}`}
                    onClick={() => setStage(s.id)}
                    aria-current={active ? "step" : undefined}
                  >
                    <span className="sr-step-mark">
                      {done ? <I.Check size={14} /> : <Ic size={15} />}
                    </span>
                    <span className="sr-step-text">
                      <span className="sr-step-idx">0{i + 1}</span>
                      <span className="sr-step-name">{s.name}</span>
                    </span>
                  </button>
                </React.Fragment>
              );
            })}
          </nav>

          <div className="sr-stage-body">
            {stage === "overview"   && <SrOverview book={book} go={setStage} />}
            {stage === "matrix"     && <SrMatrix go={setStage} book={book} />}
            {stage === "profile"    && <SrProfile book={book} go={setStage} />}
            {stage === "validation" && (
              <SrValidation
                book={book}
                go={setStage}
                deepFor={srDeepFor}
                loadDeep={srLoadDeep}
              />
            )}
            {stage === "apply"      && <SrApply go={setStage} book={book} />}
          </div>
        </section>
        ) : (
        <section className="sr-stage sr-stage-empty">
          <div style={{ minHeight: "60vh", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: "40px 24px", textAlign: "center" }}>
            <I.BookOpen size={40} />
            <h1 className="text-serif" style={{ fontSize: 20, margin: "14px 0 6px" }}>参考书库还是空的</h1>
            <p className="text-muted text-sm" style={{ maxWidth: 380, lineHeight: 1.7 }}>
              导入一本参考书后，这里会展示它的维度矩阵、风格画像、回测校验与注入应用。系统只学习抽象风格画像，不复刻原文表达、人物或桥段。
            </p>
            <button className="btn btn-accent" style={{ marginTop: 18 }} onClick={() => setImportOpen(true)}>
              <I.FileInput size={14} /> 导入第一本参考书
            </button>
          </div>
        </section>
        )}
      </div>

      <SrImportDialog
        open={importOpen}
        onClose={() => setImportOpen(false)}
        onChoose={(policy, rights) => {
          setImportOpen(false);
          if (window.srImportBook) window.srImportBook(policy, rights);
        }}
      />
    </div>
  );
};

function SrImportDialog({ open, onClose, onChoose }) {
  const [policy, setPolicy] = useStSR("local_only");
  const [analysisRights, setAnalysisRights] = useStSR(false);
  const [sendRights, setSendRights] = useStSR(false);
  const dialogRef = React.useRef(null);
  const returnFocusRef = React.useRef(null);
  const needsSend = srPolicyNeedsSendRights(policy);
  // 发送权只在云端策略下有意义；切回 local_only 时声明里恒为 false，不带走多余授权。
  const declaration = { declared: true, analysis_rights: analysisRights, send_rights: needsSend && sendRights };
  const rightsReady = srRightsReady(policy, declaration);

  React.useEffect(() => {
    if (!open) return undefined;
    setPolicy("local_only");
    setAnalysisRights(false);
    setSendRights(false);
    returnFocusRef.current = document.activeElement;
    const focusTimer = setTimeout(() => {
      const first = dialogRef.current && dialogRef.current.querySelector("input, button");
      if (first) first.focus();
    }, 0);
    const onKeyDown = (event) => {
      if (event.key === "Escape") { event.preventDefault(); onClose(); return; }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll('input:not([disabled]), button:not([disabled])'));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      clearTimeout(focusTimer);
      document.removeEventListener("keydown", onKeyDown);
      const target = returnFocusRef.current;
      if (target && target.focus) target.focus();
    };
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="sr-import-scrim" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section ref={dialogRef} className="sr-import-dialog" role="dialog" aria-modal="true" aria-labelledby="sr-import-title" aria-describedby="sr-import-desc">
        <header className="sr-import-head">
          <div>
            <div className="page-eyebrow">导入前先定边界</div>
            <h2 id="sr-import-title" className="text-serif">这本参考书可以离开本机吗？</h2>
          </div>
          <button type="button" className="sr-import-x" aria-label="关闭导入设置" onClick={onClose}><I.X size={16} /></button>
        </header>
        <p id="sr-import-desc" className="sr-import-desc">
          选择会随参考书保存，并约束后续抽取。默认不让原文出域；系统只学习抽象技法，不复刻人物、桥段或原句。
        </p>
        <fieldset className="sr-policy-list">
          <legend className="sr-policy-legend">原文数据使用范围</legend>
          {SR_CLOUD_POLICIES.map((item) => (
            <label key={item.id} className={`sr-policy ${policy === item.id ? "is-selected" : ""}`}>
              <input type="radio" name="sr-cloud-policy" value={item.id} checked={policy === item.id} onChange={() => setPolicy(item.id)} />
              <span className="sr-policy-mark" aria-hidden="true" />
              <span className="sr-policy-copy">
                <span className="sr-policy-title">{item.label}<em>{item.badge}</em></span>
                <span className="sr-policy-detail">{item.detail}</span>
              </span>
            </label>
          ))}
        </fieldset>
        <fieldset className="sr-rights-list">
          <legend className="sr-policy-legend">权属声明</legend>
          <label className={`sr-rights ${analysisRights ? "is-checked" : ""}`}>
            <input type="checkbox" data-testid="sr-rights-analysis" checked={analysisRights} onChange={(event) => setAnalysisRights(event.target.checked)} />
            <span>{SR_RIGHTS_TERMS.analysis}</span>
          </label>
          {needsSend && (
            <label className={`sr-rights ${sendRights ? "is-checked" : ""}`}>
              <input type="checkbox" data-testid="sr-rights-send" checked={sendRights} onChange={(event) => setSendRights(event.target.checked)} />
              <span>{SR_RIGHTS_TERMS.send}</span>
            </label>
          )}
          {!rightsReady && (
            <p className="sr-rights-hint" data-testid="sr-rights-hint">
              {needsSend ? "云端策略需要同时确认分析权与发送权，后端不接受未声明的上云导入；未获授权请改选「仅保存在本机」。" : "请先确认分析权，再选择文件。"}
            </p>
          )}
        </fieldset>
        <div className="sr-import-notice"><I.ShieldCheck size={14} /><span>“仅本机”不会静默降级为上云；需要云端抽取时，必须重新以更开放的策略导入。</span></div>
        <footer className="sr-import-actions">
          <button type="button" className="btn btn-ghost" onClick={onClose}>取消</button>
          <button
            type="button"
            className="btn btn-accent"
            data-testid="sr-import-choose-file"
            disabled={!rightsReady}
            onClick={() => { if (rightsReady) onChoose(policy, declaration); }}
          ><I.FileInput size={14} /> 选择文件</button>
        </footer>
      </section>
    </div>
  );
}

function SrBookState({ s }) {
  const map = {
    ready:      { tone: "sage",  label: "已就绪" },
    extracting: { tone: "gold",  label: "抽取中" },
    pending:    { tone: "slate", label: "等待" },
  };
  const m = map[s] || map.pending;
  return <span className={`pill pill-${m.tone} text-xs`}><span className="pill-dot" />{m.label}</span>;
}

/* ============ Stage: Overview ============ */
function SrOverview({ book, go }) {
  const deep = useSrDeep(book);
  const stats = srStatsOf(deep);
  /* 真实书绝不回退演示数据:缺数据显示空态 */
  const isRealBook = !!(book && book.real);
  const realMetricsArr = stats ? srRealMetrics(stats) : [];
  const metrics = realMetricsArr;
  const realInput = stats && stats.input_assessment;
  const inputRows = SR_LAYERS.map(l => ({ id: l.id, name: l.name, level: (realInput && realInput[l.id]) || "low" }));
  const realDistArr = stats ? srRealParaDist(stats) : [];
  const dist = realDistArr;
  const distMax = Math.max(...dist.map(d => d.v), 0.01);
  const calib = (stats && stats.classifier_calibration) || null;
  const isReal = !!stats;
  const dimCovered = deep && deep.dimCounts ? Object.keys(deep.dimCounts).length : 0;
  const runStatus = deep && deep.run ? deep.run.status : null;
  const runProgress = (deep && deep.run && deep.run.coverage_json && deep.run.coverage_json.progress) || null;

  return (
    <div className="sr-overview">
      <div className="sr-ov-grid">
        <div className="card sr-ov-metrics">
          <div className="card-head">
            <div><div className="card-title">内部统计基线</div><div className="card-sub">全文计算 · {metrics.length} 项 · 用于校准与回测，不作为生成配额</div></div>
            <span className={`pill ${isReal ? "pill-sage" : ""}`}><span className="pill-dot" />{isReal ? "实时" : "MetricsEngine"}</span>
          </div>
          <div className="sr-metric-grid">
            {metrics.map(m => (
              <div key={m.key} className="sr-metric">
                <div className="sr-metric-name">{m.name}</div>
                <div className="sr-metric-val tab-num">
                  {m.pct ? (m.mean*100).toFixed(0) + "%" : (Math.round(m.mean * 10) / 10)}
                  {m.unit && !m.pct && <span className="sr-metric-unit"> {m.unit}</span>}
                </div>
                <div className="sr-metric-std tab-num">σ {Math.round(m.std * 10) / 10}</div>
              </div>
            ))}
          </div>
          {metrics.length === 0 && (
            <div className="text-xs text-muted" style={{padding:"10px 2px"}}>统计基线加载中——若长时间为空，重进本页或重新导入。</div>
          )}
        </div>

        <div className="card">
          <div className="card-head"><div><div className="card-title">输入量评估</div><div className="card-sub">按层设阈值</div></div></div>
          <div className="sr-input-list">
            {inputRows.map(l => (
              <div key={l.id} className="sr-input-row">
                <span className="sr-input-name">{l.name}</span>
                <span className={`sr-input-level lv-${l.level}`}>
                  {SR_INPUT_LABEL[l.level] || l.level}
                </span>
              </div>
            ))}
          </div>
          <div className="sr-calib">
            <div className="ctx-head" style={{marginBottom: 8}}><I.Target size={13} /><span>分类器校准</span></div>
            <ul className="meta-rows">
              <li><span>锚定集</span><strong>{calib ? `前 ${calib.anchor_size} 段 · 强模型` : "前 200 段 · 强模型"}</strong></li>
              <li><span>快模型一致率</span><strong className="tab-num">{calib && calib.fast_model_agreement != null ? Number(calib.fast_model_agreement).toFixed(2) : "—"}</strong></li>
              <li><span>是否降级</span><strong style={{color: calib && calib.fallback_to_strong ? "var(--gold)" : "var(--sage)"}}>{calib ? (calib.fallback_to_strong ? "是" : "否") : "否"}</strong></li>
            </ul>
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-head"><div><div className="card-title">段落类型分布</div><div className="card-sub">8 类 · {isReal ? "本书实测" : "LLM 分类器 + 锚定校准"}</div></div></div>
        <div className="sr-dist">
          {dist.map(d => (
            <div key={d.key} className="sr-dist-row">
              <span className="sr-dist-label">{d.type}</span>
              <div className="sr-dist-bar">
                <div className="sr-dist-fill" style={{width: (d.v / distMax * 100) + "%"}} />
              </div>
              <span className="sr-dist-val tab-num">{(d.v*100).toFixed(0)}%</span>
            </div>
          ))}
          {dist.length === 0 && (
            <div className="text-xs text-muted" style={{padding:"6px 2px"}}>暂无段型分布——导入后自动分类，启用 LLM 后可在右上「重新分类」提升精度。</div>
          )}
        </div>
      </div>

      {/* 演示书:趋势/重试链示意图。真实书:最近一次抽取 run 的真实进展。 */}
      {!isRealBook ? (
        <div className="sr-ov-grid">
          <div className="card">
            <div className="card-head"><div><div className="card-title">抽取进度趋势（示意）</div><div className="card-sub">近 14 次 run 的 sub-dim 覆盖增长</div></div>
              <span className="pill pill-sage"><span className="pill-dot" />16/16 已覆盖</span>
            </div>
            <SrTrendChart data={SR_TREND} />
          </div>

          <div className="card">
            <div className="card-head"><div><div className="card-title">Evidence 重试链（示意）</div><div className="card-sub">两级重试 · 成本审计</div></div></div>
            <ul className="sr-retry">
              <li><span className="sr-retry-dot ok" /><span className="flex-1">初次抽取通过</span><b className="tab-num">47</b></li>
              <li><span className="sr-retry-dot l1" /><span className="flex-1">第一级定向补抽</span><b className="tab-num">9</b></li>
              <li><span className="sr-retry-dot l2" /><span className="flex-1">第二级整维重抽</span><b className="tab-num">2</b></li>
              <li><span className="sr-retry-dot drop" /><span className="flex-1">证据不足丢弃</span><b className="tab-num">1</b></li>
            </ul>
            <p className="text-xs text-muted mt-3">补抽成本 ≤ 完整抽取 30% · 全部 finding 强制 ≥2 证据。</p>
          </div>
        </div>
      ) : (
        <div className="card">
          <div className="card-head">
            <div><div className="card-title">抽取进展</div><div className="card-sub">最近一次抽取 run</div></div>
            <span className={`pill ${runStatus === "done" ? "pill-sage" : runStatus === "running" ? "pill-gold" : ""}`}>
              <span className="pill-dot" />
              {runStatus === "done" ? "抽取完成" : runStatus === "running" ? "抽取中" : runStatus === "failed" ? "抽取失败" : "尚未抽取"}
            </span>
          </div>
          {runStatus ? (
            <ul className="meta-rows">
              <li><span>覆盖维度</span><strong className="tab-num">{dimCovered} / 16</strong></li>
              {runProgress && <li><span>层进度</span><strong className="tab-num">{runProgress.layers_done ?? 0} / {runProgress.layers_total ?? 4}{runProgress.current_layer ? ` · ${runProgress.current_layer}` : ""}</strong></li>}
            </ul>
          ) : (
            <p className="text-sm text-muted" style={{margin:0}}>
              这本书还没有抽取记录——点右上「重跑抽取」启动后台抽取（需启用 LLM），完成后「维度矩阵」即显示真实 findings。
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function SrTrendChart({ data }) {
  const max = Math.max(...data, 1);
  return (
    <div className="sr-trend">
      {data.map((v, i) => (
        <div key={i} className="sr-trend-col">
          <div className="sr-trend-bar" style={{height: (v / max * 100) + "%"}} />
        </div>
      ))}
    </div>
  );
}

/* ============ Stage: Dimension Matrix ============ */
/* 真实 finding(后端形状)→ FindingCard 期望形状 */
function srAdaptFinding(f) {
  return {
    id: f.finding_id,
    conf: f.confidence,
    statement: f.statement,
    review: f.status || "pending",
    vote: f.user_vote || null,   // 立项 B — 回显当前用户已投的票(跨刷新持久)
    evidence: (f.evidence || []).map(e => ({
      p: e.paragraph_id || null,
      quote: e.quote_text || "",
      kind: e.anchor_kind,
      synthetic: !!e.is_synthetic,
      note: null,
      dims: null,
    })),
  };
}

function SrMatrix({ go, book }) {
  const deep = useSrDeep(book);
  const isRealBook = !!(book && book.real);
  const realInput = (deep && deep.book && deep.book.stats_json && deep.book.stats_json.input_assessment) || null;
  const realMode = !!(deep && deep.runId && deep.dimCounts && Object.keys(deep.dimCounts).length > 0);
  /* 真实书但尚无抽取产物:显示真实空态(全 0 + 引导),绝不回退演示 findings */
  const realEmpty = isRealBook && !realMode;
  const [cell, setCell] = useStSR("language.sentence_structure");
  const [kindFilter, setKindFilter] = useStSR("all");
  const [hover, setHover] = useStSR(null);
  const [synthBusy, setSynthBusy] = useStSR(false);

  // 有效单元数据：真模式叠加 dimCounts + input_assessment(skip)
  const cellData = (layerId, sub) => {
    const path = `${layerId}.${sub.id}`;
    if (realEmpty) {
      const skip = !!(realInput && realInput[layerId] === "skip");
      return { path, name: sub.name, conf: skip ? "skip" : "low", obs: 0, fp: 0, q: 0, skip };
    }
    if (realInput && realInput[layerId] === "skip") return { path, name: sub.name, conf: "skip", obs: 0, fp: 0, q: 0, skip: true };
    const dc = deep.dimCounts[path];
    if (!dc) return { path, name: sub.name, conf: "low", obs: 0, fp: 0, q: 0, skip: false };
    return { path, name: sub.name, conf: dc.conf, obs: dc.obs, fp: dc.fp, q: dc.q, skip: false };
  };
  const cellsByLayer = SR_LAYERS.map(l => ({ layer: l, cells: l.subs.map(s => cellData(l.id, s)) }));

  // 抽屉 findings：真模式取 deep.findingsByDim[cell] 适配；未抽取 → null(空态)
  const realGroup = realMode ? deep.findingsByDim[cell] : null;
  const findings = realMode
    ? (realGroup ? { observations: realGroup.observations.map(srAdaptFinding), forbidden_patterns: realGroup.forbidden_patterns.map(srAdaptFinding) } : null)
    : null;
  const onReviewFinding = realMode
    ? (findingId, decision) => { if (window.srReviewFinding) window.srReviewFinding(findingId, decision, book.id).catch(() => {}); }
    : null;
  const onVoteFinding = realMode
    ? (findingId, vote) => { if (window.srFindingFeedback) window.srFindingFeedback(findingId, vote, book.id).catch(() => {}); }
    : null;

  // 2D keyboard navigation across the matrix (skip cells are not selectable)
  const grid = cellsByLayer.map(row => row.cells.map(c => ({ path: c.path, skip: c.skip })));
  React.useEffect(() => {
    const inField = (el) => el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
    const findRC = (p) => {
      for (let r = 0; r < grid.length; r++) for (let c = 0; c < grid[r].length; c++) if (grid[r][c].path === p) return { r, c };
      return { r: 0, c: 0 };
    };
    const move = (dr, dc) => {
      let { r, c } = findRC(cell);
      for (let i = 0; i < 6; i++) {
        r += dr; c += dc;
        if (r < 0 || r >= grid.length || c < 0 || c >= grid[r].length) return;
        if (!grid[r][c].skip) { setCell(grid[r][c].path); return; }
      }
    };
    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey || inField(e.target)) return;
      if (e.key === "ArrowRight") { e.preventDefault(); move(0, 1); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); move(0, -1); }
      else if (e.key === "ArrowDown") { e.preventDefault(); move(1, 0); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1, 0); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [cell, realMode]);

  const cellMeta = (() => {
    for (const row of cellsByLayer) for (const c of row.cells) if (c.path === cell) return { layer: row.layer, cell: c };
    return null;
  })();

  const totals = realMode
    ? Object.values(deep.dimCounts).reduce((a, d) => ({ obs: a.obs + d.obs, fp: a.fp + d.fp, q: a.q + d.q }), { obs: 0, fp: 0, q: 0 })
    : (realEmpty ? { obs: 0, fp: 0, q: 0 } : { obs: 52, fp: 14, q: 140 });
  const hasProfile = !!(deep && deep.profileId);

  const onSynth = async () => {
    if (realEmpty) return;
    if (!realMode || !deep.runId || hasProfile) { go && go("profile"); return; }
    if (synthBusy) return;
    setSynthBusy(true);
    try {
      await window.srSynthesize(deep.runId, book.id);
      go && go("profile");
    } catch (e) {
      if (e && (e.code === "STYLE_REFERENCE_LLM_REQUIRED" || e.code === "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED")) {
        window.alert("合成风格画像需要启用 LLM（系统设置 → 模型与接入）。");
      } else { window.alert("合成失败：" + ((e && e.message) || e)); }
    } finally { setSynthBusy(false); }
  };

  return (
    <div className="sr-matrix-wrap">
      <style>{`
        .sr-matrix-kbd { margin-left: auto; display: inline-flex; align-items: center; gap: 3px; font-size: 11px; color: var(--ink-4); }
        .sr-matrix-kbd kbd { font-family: var(--font-mono); font-size: 10px; padding: 1px 5px; border-radius: 4px; background: var(--paper-2); border: 1px solid var(--line-1); color: var(--ink-3); }
        .sr-matrix-cells .sr-cell { transition: transform 180ms var(--ease-spring, ease), box-shadow 180ms var(--ease-soft, ease), border-color var(--t-fast), background var(--t-fast); }
        .sr-matrix-cells .sr-cell.is-active { transform: translateY(-2px); box-shadow: 0 0 0 2px var(--crimson), var(--shadow-md); }
        .sr-findings-scroll { animation: srDrawerIn 300ms var(--ease-out, ease) both; }
        @keyframes srDrawerIn { from { transform: translateX(12px); } to { transform: none; } }
      `}</style>
      <div className="sr-matrix-side">
        {realEmpty && (
          <div className="sr-fewshot-warn" style={{marginBottom: 12}}>
            <I.Info size={13} />
            <span>这本书还没有抽取产物——点右上「重跑抽取」启动后台抽取（需启用 LLM）。完成后此矩阵按真实 findings 点亮。</span>
          </div>
        )}
        <div className="sr-matrix-legend">
          <span className="text-xs text-muted">置信度</span>
          <span className="sr-lg sr-lg-high">高</span>
          <span className="sr-lg sr-lg-medium">中</span>
          <span className="sr-lg sr-lg-low">低</span>
          <span className="sr-lg sr-lg-skip">不足</span>
          <span className="sr-matrix-kbd"><kbd>←</kbd><kbd>↑</kbd><kbd>↓</kbd><kbd>→</kbd> 选维度</span>
        </div>

        <div className="sr-matrix">
          {cellsByLayer.map(({ layer: l, cells }) => (
            <div key={l.id} className="sr-matrix-row">
              <div className="sr-matrix-rowhead">
                <span className="sr-matrix-abbr">{l.abbr}</span>
                <span className="sr-matrix-layer">{l.name}</span>
              </div>
              <div className="sr-matrix-cells">
                {cells.map(c => {
                  const confLabel = c.conf === "high" ? "高置信" : c.conf === "medium" ? "中置信" : "低置信";
                  return (
                    <button
                      key={c.path}
                      className={`sr-cell conf-${c.conf} ${cell === c.path ? "is-active" : ""}`}
                      onClick={() => !c.skip && setCell(c.path)}
                      onMouseEnter={() => !c.skip && setHover(c.path)}
                      onMouseLeave={() => setHover(null)}
                      disabled={c.skip}
                    >
                      <span className="sr-cell-name">{c.name}</span>
                      {c.skip ? (
                        <span className="sr-cell-skip">语料不足</span>
                      ) : (
                        <span className="sr-cell-stats">
                          <span className="sr-cell-stat"><b>{c.obs}</b>观察</span>
                          <span className="sr-cell-stat"><b>{c.q}</b>引文</span>
                          {c.fp > 0 && <span className="sr-cell-stat fp"><b>{c.fp}</b>禁忌</span>}
                        </span>
                      )}
                      {hover === c.path && !c.skip && (
                        <span className="sr-cell-tip">
                          <span className="sr-cell-tip-conf">
                            <span className={`conf-dot conf-${c.conf}`} />
                            {confLabel} · {c.obs} 观察 / {c.q} 引文 / {c.fp} 禁忌
                          </span>
                          <span className="sr-cell-tip-hint">点击查看全部证据 →</span>
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
        </div>

        <div className="sr-matrix-foot">
          <div className="sr-matrix-foot-stat"><b className="tab-num">{totals.obs}</b> 观察</div>
          <div className="sr-matrix-foot-stat"><b className="tab-num">{totals.fp}</b> 禁忌模式</div>
          <div className="sr-matrix-foot-stat"><b className="tab-num">{totals.q}</b> 引文样本</div>
          <div className="flex-1" />
          <button className="btn btn-accent btn-sm" disabled={synthBusy || realEmpty} title={realEmpty ? "先完成抽取再合成画像" : undefined} onClick={onSynth}>
            {synthBusy ? <><span className="sr-spin" style={{display:"inline-flex"}}><I.Refresh size={13} /></span> 合成中…</>
              : <><I.Sparkles size={13} /> {hasProfile ? "查看风格画像" : "合成风格画像"}</>}
          </button>
        </div>
      </div>

      {/* Findings drawer */}
      <aside className="sr-findings">
        {cellMeta && (
          <header className="sr-findings-head">
            <div>
              <div className="text-muted text-xs" style={{letterSpacing:"0.12em"}}>{cellMeta.layer.name} · {cellMeta.cell.name}</div>
              <h3 className="text-serif" style={{fontSize:17, margin:"3px 0 0"}}>{cellMeta.cell.name}</h3>
            </div>
            <span className={`pill pill-${cellMeta.cell.conf === "high" ? "sage" : cellMeta.cell.conf === "medium" ? "gold" : "slate"} text-xs`}>
              <span className="pill-dot" />{cellMeta.cell.conf === "high" ? "高置信" : cellMeta.cell.conf === "medium" ? "中置信" : "低置信"}
            </span>
          </header>
        )}

        <div className="sr-findings-filter">
          <button className={`sr-ff ${kindFilter === "all" ? "is-active" : ""}`} onClick={() => setKindFilter("all")}>全部</button>
          <button className={`sr-ff ${kindFilter === "obs" ? "is-active" : ""}`} onClick={() => setKindFilter("obs")}>
            <I.Check size={12} /> 观察 {findings?.observations.length || 0}
          </button>
          <button className={`sr-ff ${kindFilter === "fp" ? "is-active" : ""}`} onClick={() => setKindFilter("fp")}>
            <I.Ban size={12} /> 禁忌 {findings?.forbidden_patterns.length || 0}
          </button>
        </div>

        <div className="sr-findings-scroll" key={cell}>
          {!findings && (
            <div className="empty-state" style={{padding: 30}}>
              <I.Quote size={24} />
              <div className="mt-2 text-muted text-sm">
                {realEmpty ? "尚未抽取——启动「重跑抽取」后，这里显示该维度的真实观察与证据。"
                  : realMode ? "该维度暂无 finding（语料不足或尚未抽出）。" : "该维度暂无展开数据"}
              </div>
            </div>
          )}
          {findings && (kindFilter === "all" || kindFilter === "obs") && findings.observations.map(o => (
            <FindingCard key={o.id} kind="obs" finding={o} onReview={onReviewFinding ? (d) => onReviewFinding(o.id, d) : null} onVote={onVoteFinding ? (v) => onVoteFinding(o.id, v) : null} />
          ))}
          {findings && (kindFilter === "all" || kindFilter === "fp") && findings.forbidden_patterns.map(f => (
            <FindingCard key={f.id} kind="fp" finding={f} onReview={onReviewFinding ? (d) => onReviewFinding(f.id, d) : null} onVote={onVoteFinding ? (v) => onVoteFinding(f.id, v) : null} />
          ))}
        </div>
      </aside>
    </div>
  );
}

function FindingCard({ kind, finding, onReview, onVote }) {
  const isFp = kind === "fp";
  const [review, setReview] = useStSR(finding.review || "pending");
  const [vote, setVote] = useStSR(finding.vote || null);
  // deep 重载后 finding.review / vote 变化 → 同步(同 key 实例不会重跑 initializer)
  React.useEffect(() => { setReview(finding.review || "pending"); }, [finding.review]);
  React.useEffect(() => { setVote(finding.vote || null); }, [finding.vote]);
  const setReviewBoth = (next) => { setReview(next); if (onReview) onReview(next); };
  // 立项 B — 投票:真模式发 up/down(无 un-vote 语义,可改向,后端幂等),演示模式本地 toggle。
  const castVote = (v) => {
    if (onVote) { setVote(v); onVote(v); }
    else { setVote(vote === v ? null : v); }
  };
  return (
    <article className={`sr-finding ${isFp ? "is-fp" : ""} rev-${review}`}>
      <header className="sr-finding-head">
        <span className={`sr-finding-tag ${isFp ? "tag-fp" : "tag-obs"}`}>
          {isFp ? <I.Ban size={11} /> : <I.Check size={11} />}
          {isFp ? "禁忌模式" : "观察"}
        </span>
        {!isFp && finding.conf && (
          <span className={`pill text-xs pill-${finding.conf === "high" ? "sage" : "gold"}`}><span className="pill-dot" />{finding.conf === "high" ? "高" : "中"}</span>
        )}
        <span className={`sr-rev-state st-${review}`}>
          {review === "approved" ? "已通过" : review === "rejected" ? "已驳回" : "待审"}
        </span>
        <div className="flex gap-1" style={{marginLeft:"auto"}}>
          <button className={`sr-rev-btn ${review==="approved"?"on-ok":""}`} title="通过" onClick={()=>setReviewBoth(review==="approved"?"pending":"approved")}><I.Check size={13} /></button>
          <button className={`sr-rev-btn ${review==="rejected"?"on-no":""}`} title="驳回" onClick={()=>setReviewBoth(review==="rejected"?"pending":"rejected")}><I.X size={13} /></button>
        </div>
      </header>
      <p className="sr-finding-statement text-serif">{finding.statement}</p>
      <div className="sr-finding-evidence">
        <div className="sr-finding-evidence-label">证据 · {finding.evidence.length}{finding.evidence.length >= 2 ? " · 已满足 ≥2" : " · 不足"}</div>
        {finding.evidence.map((e, i) => (
          <div key={i} className="sr-ev">
            <div className="sr-ev-mark">
              {e.kind === "counter_example" || e.synthetic ? <span className="sr-ev-badge syn">合成反例</span>
                : e.kind === "author_avoidance" ? <span className="sr-ev-badge avoid">负空间</span>
                : <span className="sr-ev-badge quote">{e.p || "引文"}</span>}
            </div>
            {e.quote && <p className="sr-ev-quote text-serif">{e.quote}</p>}
            {e.note && <p className="sr-ev-note">{e.note}</p>}
            {e.dims && (
              <div className="sr-ev-dims">
                {e.dims.map(d => <span key={d} className="sr-ev-dim">{d}</span>)}
              </div>
            )}
          </div>
        ))}
      </div>
      <footer className="sr-finding-foot">
        <span className="text-xs text-muted">这条画像准吗？</span>
        <div className="sr-vote">
          <button className={`sr-vote-btn ${vote==="up"?"on":""}`} onClick={()=>castVote("up")} aria-label="赞">👍</button>
          <button className={`sr-vote-btn ${vote==="down"?"on":""}`} onClick={()=>castVote("down")} aria-label="踩">👎</button>
        </div>
        <span className="text-xs text-subtle" style={{marginLeft:"auto"}}>反馈聚合后更新 confidence</span>
      </footer>
    </article>
  );
}

/* ============ Stage: Profile ============ */
/* 真实书缺产物时的空态引导(矩阵/画像/回测/注入共用形态) */
function SrRealEmpty({ title, sub, actionLabel, onAction }) {
  return (
    <div className="card" style={{padding: "44px 24px", textAlign: "center"}}>
      <I.Sparkles size={26} style={{color: "var(--ink-3)"}} />
      <h3 className="text-serif" style={{fontSize: 17, margin: "10px 0 6px"}}>{title}</h3>
      <p className="text-muted text-sm" style={{margin: "0 auto 16px", maxWidth: 420, lineHeight: 1.7}}>{sub}</p>
      {actionLabel && <button className="btn btn-accent btn-sm" onClick={onAction}>{actionLabel}</button>}
    </div>
  );
}

function SrProfile({ book, go }) {
  const [tab, setTab] = useStSR("summary");
  const deep = useSrDeep(book);
  const isRealBook = !!(book && book.real);
  const profile = deep && deep.profile;
  const pj = (profile && profile.profile_json) || null;
  const real = !!pj;
  const dimMeta = (path) => { for (const l of SR_LAYERS) for (const s of l.subs) if (`${l.id}.${s.id}` === path) return { abbr: l.abbr, name: s.name }; return { abbr: "·", name: path }; };
  const cov = (profile && profile.coverage_json) || {};
  const subDims = (pj && pj.sub_dimensions) || null;
  const realDimRows = subDims ? Object.entries(subDims).map(([path, d]) => ({ path, ...dimMeta(path), conf: (d && d.confidence) || "low", obs: (d && d.observation_count) || 0, fp: (d && d.forbidden_pattern_count) || 0, q: (d && d.quote_count) || 0 })) : null;
  const baseline = (pj && pj.metrics_baseline) || null;
  const realBaseline = baseline ? SR_METRICS.map(m => { const b = baseline[m.key]; if (!b || b.mean == null) return null; return { ...m, mean: Number(b.mean), std: Number(b.std) }; }).filter(Boolean).slice(0, 6) : null;
  const sampleIdx = (pj && pj.scene_samples_index) || null;
  const features = (pj && pj.style_features) || [];
  const dimRows = realDimRows || [];

  /* 真实书还没有画像:显示真实空态,不再把演示画像(冷峻克制白描…)当成这本书的 */
  if (isRealBook && !real) {
    const hasRun = !!(deep && deep.runId);
    return (
      <SrRealEmpty
        title="还没有风格画像"
        sub={hasRun
          ? "抽取已有产物——回「维度矩阵」点「合成风格画像」聚合 16 维 findings（需启用 LLM）。"
          : "先在「维度矩阵」启动抽取，完成后再合成风格画像。"}
        actionLabel="去维度矩阵"
        onAction={() => go && go("matrix")}
      />
    );
  }

  return (
    <div className="sr-profile">
      <div className="sr-profile-main">
        <div className="card">
          <div className="card-head">
            <div>
              <div className="card-title">{real ? (profile.title || `${book.author}风格画像`) : `${book.author}风格画像 · v3`}</div>
              <div className="card-sub">{real ? `${cov.findings_count || 0} finding / ${cov.quotes_count || 0} 引文聚合 · ${cov.sub_dim_count || 0} 维` : "由 52 观察 / 14 禁忌 / 140 引文聚合 · 12 维有效"}</div>
            </div>
            <span className={`pill ${real && profile.status !== "active" ? "pill-gold" : "pill-sage"}`}><span className="pill-dot" />{real ? (profile.status === "active" ? "已就绪" : profile.status) : "已就绪"}</span>
          </div>
          <p className="sr-profile-summary text-serif">
            {real
              ? (pj.qualitative_summary || pj.narrative_summary || "（该画像尚无叙述性概述。）")
              : "冷峻、克制、白描见长。短句为骨，逗号顿连推进节奏，少用关联词与抒情排比。比喻具象、取自乡土器物，喻体之后即收，不作解释。叙述者多为限知的「我」，与事件保持冷静距离；对话后接动作而非心理剖白。整体以白描叠加细节制造反讽与悲悯，不依赖形容词堆砌。"}
          </p>
          {real && features.length > 0 && (
            <div style={{margin: "2px 0 4px"}}>
              {features.slice(0, 8).map((f, i) => (
                <span key={i} className="sr-pd-path" style={{display:"inline-block", margin:"2px 6px 2px 0", padding:"2px 8px", background:"var(--paper-2)", borderRadius:6, fontSize:12}}>{f}</span>
              ))}
            </div>
          )}

          <div className="sr-profile-tabs" role="tablist" aria-label="风格画像视图">
            <button role="tab" aria-selected={tab === "summary"} tabIndex={tab === "summary" ? 0 : -1} onKeyDown={onRovingTabKeyDown}
              className={`sr-pt ${tab === "summary" ? "is-active" : ""}`} onClick={() => setTab("summary")}>维度摘要</button>
            <button role="tab" aria-selected={tab === "preview"} tabIndex={tab === "preview" ? 0 : -1} onKeyDown={onRovingTabKeyDown}
              className={`sr-pt ${tab === "preview" ? "is-active" : ""}`} onClick={() => setTab("preview")}>预览示例</button>
          </div>

          {tab === "summary" && (
            <div className="sr-profile-dims">
              {dimRows.map(row => (
                <div key={row.path} className="sr-pd-row">
                  <span className="sr-pd-path">{row.abbr} · {row.name}</span>
                  <span className={`sr-pd-conf conf-dot conf-${row.conf}`} />
                  <span className="sr-pd-counts">{row.obs} 观察 · {row.fp} 禁忌 · {row.q} 引文</span>
                </div>
              ))}
              {realDimRows && realDimRows.length === 0 && <div className="text-xs text-muted" style={{padding:"8px 2px"}}>画像暂无维度摘要。</div>}
            </div>
          )}

          {tab === "preview" && <SrPreview profileId={real ? profile.profile_id : null} />}
        </div>
      </div>

      <aside className="sr-profile-side">
        <div className="card-flat">
          <div className="ctx-head" style={{marginBottom: 10}}><I.Target size={13} /><span>指标基线</span></div>
          <div className="sr-baseline">
            {(realBaseline || []).map(m => (
              <div key={m.key} className="sr-baseline-row">
                <span className="text-sm">{m.name}</span>
                <span className="tab-num fw-600">{m.pct ? (m.mean*100).toFixed(0)+"%" : (Math.round(m.mean*10)/10)} <span className="text-muted text-xs">±{Math.round(m.std*10)/10}</span></span>
              </div>
            ))}
          </div>
          <p className="text-xs text-muted mt-3">回测阈值 = max(σ × 1.25, 绝对下限)，自适应于本作。</p>
        </div>

        <div className="card-flat">
          <div className="ctx-head" style={{marginBottom: 10}}><I.Quote size={13} /><span>场景样例索引</span></div>
          <ul className="sr-sample-index">
            {sampleIdx ? (
              Object.entries(sampleIdx).filter(([, ids]) => (ids || []).length).length === 0
                ? <li><span className="text-muted text-xs">暂无样例索引</span></li>
                : Object.entries(sampleIdx).filter(([, ids]) => (ids || []).length).map(([t, ids]) => (
                    <li key={t}><span>{SR_PARA_LABEL[t] || t}</span><b className="tab-num">{(ids || []).length}</b></li>
                  ))
            ) : (
              <>
                <li><span>对话</span><b className="tab-num">10</b></li>
                <li><span>动作</span><b className="tab-num">6</b></li>
                <li><span>心理</span><b className="tab-num">8</b></li>
                <li><span>环境</span><b className="tab-num">14</b></li>
              </>
            )}
          </ul>
          <p className="text-xs text-muted mt-2">Few-shot 注入 O(1) 直读，不绕段落表。</p>
        </div>

        <button className="btn btn-accent btn-lg" style={{width:"100%"}} onClick={() => go && go("validation")}><I.ArrowRight size={15} /> 进入回测校验</button>
      </aside>
    </div>
  );
}

function SrPreview({ profileId }) {
  const demo = [
    { kind: "对话", verdict: "pass", text: "「茴香豆的茴字，怎样写的？」他显出极高兴的样子，将两个指头的长指甲敲着柜台。" },
    { kind: "环境", verdict: "partial", text: "灰白的天压在屋檐上，巷子空着，只有风把一张旧报纸卷起来，又放下。" },
    { kind: "动作", verdict: "pass", text: "他没有应声，弯下腰，把那枚铜板从砖缝里抠出来，又用袖子擦了擦。" },
  ];
  const [samples, setSamples] = useStSR(null);
  const [loading, setLoading] = useStSR(false);
  const [err, setErr] = useStSR(null);
  const run = React.useCallback(() => {
    if (!profileId) return;
    setLoading(true); setErr(null);
    window.srPreviewSamples(profileId)
      .then(r => setSamples((r && r.samples) || []))
      .catch(e => setErr(e && (e.code === "STYLE_REFERENCE_LLM_REQUIRED" || e.code === "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED")
        ? "预览生成需要启用 LLM（系统设置 → 模型与接入）。"
        : ((e && e.message) || "预览生成失败")))
      .finally(() => setLoading(false));
  }, [profileId]);
  React.useEffect(() => { if (profileId) run(); }, [profileId, run]);

  const list = profileId
    ? (samples || []).map(s => ({ kind: SR_PARA_LABEL[s.paragraph_type] || s.paragraph_type, verdict: s.verdict || "partial", text: s.sample_text || "", error: s.error }))
    : demo;

  return (
    <div className="sr-preview">
      <div className="sr-preview-head">
        <span className="text-muted text-sm">{profileId ? "生成 3 段示例 + 自跑回测（sync_only）" : "apply 前自动生成 3 段示例 + 自跑回测"}</span>
        <button className="btn btn-quiet btn-sm" disabled={loading || !profileId} onClick={run}>
          {loading ? <><span className="sr-spin" style={{display:"inline-flex"}}><I.Refresh size={13} /></span> 生成中…</> : <><I.Refresh size={13} /> 重新生成</>}
        </button>
      </div>
      {err && <div className="sr-fewshot-warn"><I.Info size={13} /><span>{err}</span></div>}
      {profileId && !loading && !err && samples && samples.length === 0 && (
        <div className="text-xs text-muted" style={{padding:"10px 2px"}}>暂无预览样例。</div>
      )}
      {list.map((s, i) => (
        <article key={i} className="sr-pv-card">
          <header className="sr-pv-head">
            <span className="pill text-xs"><span className="pill-dot" />{s.kind}</span>
            {s.error ? <span className="pill pill-rose text-xs"><span className="pill-dot" />生成失败</span> : <VerdictPill v={s.verdict} />}
          </header>
          {s.text && <p className="sr-pv-text text-serif">{s.text}</p>}
        </article>
      ))}
    </div>
  );
}

function VerdictPill({ v }) {
  const map = {
    pass:       { tone: "sage",    label: "PASS" },
    partial:    { tone: "gold",    label: "PARTIAL" },
    fail:       { tone: "rose",    label: "FAIL" },
    plagiarism: { tone: "crimson", label: "抄袭" },
  };
  const m = map[v] || map.partial;
  return <span className={`pill pill-${m.tone} text-xs`} style={{fontFamily:"var(--font-mono)", letterSpacing:"0.05em"}}><span className="pill-dot" />{m.label}</span>;
}

/* ============ Stage: Apply / Inject ============ */
const SR_TASKS = [
  { id: "project_init", name: "项目初始化", def: "A", refresh: 0 },
  { id: "scene_generation", name: "场景生成", def: "mixed", refresh: 0 },
  { id: "fine_tuning", name: "精修小改", def: "B", refresh: 0 },
  { id: "key_chapter", name: "关键章节", def: "C", refresh: 2000 },
];

/* layered injection stack — base(project/global) ∪ character(pov+onstage) ∪ scene */
const SR_LAYER_STACK = [
  { rank: 3, scope: "global",    label: "全局基底", target: "默认风格", weight: 1, tokens: 320, tone: "slate",   frags: 2 },
  { rank: 2, scope: "project",   label: "项目层",   target: "示例项目", weight: 2, tokens: 540, tone: "crimson", frags: 4 },
  { rank: 1, scope: "character", label: "角色层 · POV", target: "示例角色", weight: 3, tokens: 680, tone: "gold",    frags: 3, onstage: ["配角"] },
  { rank: 0, scope: "scene",     label: "场景层",   target: "CH08·SC01", weight: 4, tokens: 880, tone: "sage",    frags: 3 },
];

const SR_FEWSHOT = {
  dialogue:    { id: "q_001", text: "「示例对白。」", note: "对话 · 单句定身份" },
  action:      { id: "q_067", text: "他从口袋里摸出几枚硬币，放在桌上。", note: "动作 · 不解释心理" },
  description_env: { id: "q_203", text: "灰白的天底下，远处横着几处安静的屋舍。", note: "环境 · 冷色白描" },
};

const SR_BANNED_INIT = [
  { term: "文笔优美", hint: "改具体描写", scope: "generation", source: "preset" },
  { term: "震撼人心", hint: "用动作呈现", scope: "generation", source: "preset" },
  { term: "示例专名", hint: "源书专名", scope: "extraction", source: "user" },
];

function SrApply({ go, book }) {
  const [sub, setSub] = useStSR("strategy");
  const [strategy, setStrategy] = useStSR("mixed");
  const [taskType, setTaskType] = useStSR("scene_generation");
  const [applied, setApplied] = useStSR(null); // 已创建的审核条目描述
  const [intensity, setIntensity] = useStSR(80);
  const [scope, setScope] = useStSR("project");
  const [scopeRefId, setScopeRefId] = useStSR(null);   // 立项 A — scene/character 级绑定目标 id
  const [scopeOpts, setScopeOpts] = useStSR({ scene: [], character: [] });
  const [banned, setBanned] = useStSR(SR_BANNED_INIT);
  const [bannedInput, setBannedInput] = useStSR("");
  const [bannedScope, setBannedScope] = useStSR("generation");
  const [selectedDims, setSelectedDims] = useStSR(() => {
    const all = [];
    SR_LAYERS.forEach(l => l.input !== "skip" && l.subs.forEach(s => s.conf !== "skip" && all.push(`${l.id}.${s.id}`)));
    return all;
  });

  /* ---- 真后端深层数据：有真画像则注入应用走真后端，否则回退演示 ---- */
  const isRealBook = !!(book && book.real);
  const [deep, setDeep] = useStSR(() => (isRealBook ? srDeepFor(book.id) : null));
  React.useEffect(() => {
    if (!isRealBook) { setDeep(null); return; }
    const sync = () => setDeep(window.srDeepFor ? window.srDeepFor(book.id) : null);
    sync();
    if (window.srLoadDeep) window.srLoadDeep(book.id);
    window.addEventListener("sr:deep-changed", sync);
    return () => window.removeEventListener("sr:deep-changed", sync);
  }, [isRealBook, book && book.id]);
  const realProfileId = deep && deep.profileId;
  const realMode = !!realProfileId;
  const realBindings = (deep && deep.bindings) || [];
  // 立项 A — 当前活动项目 id(空安全:works 列表为空时 active() 可能 undefined)
  const activeProjId = (WsWorks && WsWorks.active && WsWorks.active() && WsWorks.active().id) || null;
  // 立项 A — scope 切换时清空已选目标(避免把 A scope 的目标误用到 B scope)
  React.useEffect(() => { setScopeRefId(null); }, [scope]);
  // 立项 A — 真模式按当前活动项目加载场景/角色选项(直取后端,不依赖 catalog 缓存状态)。
  // 依赖含 activeProjId:切换活动项目时刷新选项,避免跨项目数据陈旧。
  React.useEffect(() => {
    if (!realMode || !activeProjId) { setScopeOpts({ scene: [], character: [] }); return; }
    const pid = activeProjId;
    let alive = true;
    (async () => {
      try {
        const [cat, lib] = await Promise.all([
          apiGet(`/api/v2/projects/${pid}/catalog`).catch(() => null),
          apiGet(`/api/v2/projects/${pid}/library`).catch(() => null),
        ]);
        if (!alive) return;
        const scenes = [];
        ((cat && cat.chapters) || []).forEach(c => (c.scenes || []).forEach(s => {
          if (s && s.scene_id) scenes.push({ id: s.scene_id, label: `${c.no ? c.no + "章·" : ""}${s.title || s.scene_id}` });
        }));
        const chars = ((lib && lib.characters) || []).map(c => ({
          id: c.character_id || c.id, label: c.name || c.display_name || c.character_id,
        })).filter(c => c.id);
        setScopeOpts({ scene: scenes, character: chars });
      } catch { if (alive) setScopeOpts({ scene: [], character: [] }); }
    })();
    return () => { alive = false; };
  }, [realMode, activeProjId]);

  /* ---- 真注入预览（dryrun，不写盘，debounce 350ms）---- */
  const [preview, setPreview] = useStSR(null);
  const [previewErr, setPreviewErr] = useStSR(null);
  const [previewNonce, setPreviewNonce] = useStSR(0); // 禁用词增删后强制刷新预览
  const previewTimer = React.useRef(null);
  React.useEffect(() => {
    if (!realMode) { setPreview(null); setPreviewErr(null); return; }
    clearTimeout(previewTimer.current);
    previewTimer.current = setTimeout(() => {
      window.srInjectionPreview(realProfileId, {
        strategy, task_type: taskType, intensity,
        sub_dimensions: selectedDims,
        include_positive: true, include_forbidden: true, include_metric: strategy !== "C",
      }).then(r => { setPreview(r); setPreviewErr(null); })
        .catch(e => { setPreview(null); setPreviewErr((e && e.message) || "注入预览失败"); });
    }, 350);
    return () => clearTimeout(previewTimer.current);
  }, [realMode, realProfileId, strategy, taskType, intensity, selectedDims, previewNonce]);

  /* ---- 禁用词（真模式接 profile 级后端;演示书保留本地演示列表）---- */
  const [realBanned, setRealBanned] = useStSR(null);
  const [bannedBusy, setBannedBusy] = useStSR(false);
  const loadRealBanned = React.useCallback(async () => {
    if (!realProfileId) return;
    try {
      const r = await apiGet(`/api/v2/style-reference/profiles/${realProfileId}/banned-terms`);
      setRealBanned((r && r.terms) || []);
    } catch { setRealBanned([]); }
  }, [realProfileId]);
  React.useEffect(() => { if (realMode) loadRealBanned(); else setRealBanned(null); }, [realMode, loadRealBanned]);
  const addBannedReal = async () => {
    const t = bannedInput.trim();
    if (!t || bannedBusy || !realProfileId) return;
    setBannedBusy(true);
    try {
      await apiPost(`/api/v2/style-reference/profiles/${realProfileId}/banned-terms`, { term: t, scope: bannedScope });
      setBannedInput("");
      await loadRealBanned();
      setPreviewNonce(n => n + 1); // generation 词进红线段,预览需刷新
    } catch (e) { window.alert("添加禁用词失败：" + ((e && e.message) || e)); }
    finally { setBannedBusy(false); }
  };
  const removeBannedReal = async (termId) => {
    if (bannedBusy) return;
    setBannedBusy(true);
    try {
      await apiDelete(`/api/v2/style-reference/banned-terms/${termId}`);
      await loadRealBanned();
      setPreviewNonce(n => n + 1);
    } catch (e) { window.alert("删除禁用词失败：" + ((e && e.message) || e)); }
    finally { setBannedBusy(false); }
  };

  /* ---- 任务默认表（真源 /injection/task-defaults：默认策略 + 续写刷新周期；失败回退静态值）---- */
  const [taskDefaults, setTaskDefaults] = useStSR(null);
  React.useEffect(() => {
    if (!realMode) { setTaskDefaults(null); return; }
    let alive = true;
    (async () => {
      try {
        const r = await apiGet("/api/v2/style-reference/injection/task-defaults");
        if (alive) setTaskDefaults((r && r.tasks) || null);
      } catch { /* 静态默认兜底 */ }
    })();
    return () => { alive = false; };
  }, [realMode]);
  const tasks = SR_TASKS.map(t => {
    const d = (taskDefaults || []).find(x => x.task_type === t.id);
    return d ? { ...t, def: d.default_strategy, refresh: d.refresh_every_chars } : t;
  });

  /* ---- 叠加注入层（真源 /injection/layers：resolve_binding_layers 命中层 + 预算分配）
       上下文带本画像已绑定的场景/角色 id，使 scene/character 层能在预览中亮起 ---- */
  const [layerStack, setLayerStack] = useStSR(null);
  const [layerErr, setLayerErr] = useStSR(null);
  React.useEffect(() => {
    if (!realMode || sub !== "layers" || !activeProjId) { setLayerStack(null); setLayerErr(null); return; }
    let alive = true;
    (async () => {
      try {
        const qs = new URLSearchParams({ project_id: activeProjId, task_type: taskType });
        const sceneB = realBindings.find(b => b.scope === "scene" && b.scope_ref_id);
        if (sceneB) qs.set("scene_id", sceneB.scope_ref_id);
        const charRefs = realBindings.filter(b => b.scope === "character" && b.scope_ref_id).map(b => b.scope_ref_id);
        if (charRefs.length) qs.set("character_ids", charRefs.join(","));
        const r = await apiGet(`/api/v2/style-reference/injection/layers?${qs.toString()}`);
        if (alive) { setLayerStack(r); setLayerErr(null); }
      } catch (e) { if (alive) { setLayerStack(null); setLayerErr((e && e.message) || "叠层加载失败"); } }
    })();
    return () => { alive = false; };
  }, [realMode, sub, activeProjId, taskType, realBindings.length]);

  const toggleDim = (path) => setSelectedDims(prev => prev.includes(path) ? prev.filter(p => p !== path) : [...prev, path]);
  const task = tasks.find(t => t.id === taskType) || tasks[1];
  const obsCount = Math.round((intensity / 100) * 6 * (selectedDims.length / 12));
  const totalTokens = SR_LAYER_STACK.reduce((s, l) => s + l.tokens, 0);

  const addBanned = () => {
    const t = bannedInput.trim();
    if (!t) return;
    setBanned(prev => [...prev, { term: t, hint: "", scope: bannedScope, source: "user" }]);
    setBannedInput("");
  };

  /* 真实书还没有画像:注入应用无对象,给真实空态引导(演示书保留完整演示流程) */
  if (isRealBook && !realMode) {
    return (
      <SrRealEmpty
        title="还没有可应用的画像"
        sub="注入应用消费「风格画像」——先在「维度矩阵」完成抽取并合成画像，回到这里即可配置策略/强度并应用到项目、场景或角色。"
        actionLabel="去维度矩阵"
        onAction={() => go && go("matrix")}
      />
    );
  }

  return (
    <div className="sr-apply">
      <div className="sr-apply-main">
        <nav className="sr-apply-subtabs">
          <button className={`sr-ast ${sub==="strategy"?"is-active":""}`} onClick={()=>setSub("strategy")}><I.Sliders size={13} /> 策略与维度</button>
          <button className={`sr-ast ${sub==="layers"?"is-active":""}`} onClick={()=>setSub("layers")}><I.Layers size={13} /> 叠加层</button>
          <button className={`sr-ast ${sub==="fewshot"?"is-active":""}`} onClick={()=>setSub("fewshot")}><I.Quote size={13} /> Few-shot</button>
          <button className={`sr-ast ${sub==="banned"?"is-active":""}`} onClick={()=>setSub("banned")}><I.Ban size={13} /> 禁用词 <span className="sr-ast-count">{realMode ? (realBanned || []).length : banned.length}</span></button>
        </nav>

        {sub === "strategy" && (
          <>
            <div className="card">
              <div className="card-head"><div><div className="card-title">注入策略</div><div className="card-sub">按任务类型选择 A / B / C / 混合，TaskType 自带默认</div></div></div>
              <div className="sr-task-row">
                {tasks.map(t => (
                  <button key={t.id} className={`sr-task ${taskType === t.id ? "is-active" : ""}`} onClick={() => { setTaskType(t.id); setStrategy(t.def); }}>
                    <span className="sr-task-name">{t.name}</span>
                    <span className="sr-task-def">默认 {t.def}{t.refresh > 0 ? ` · 每${t.refresh}字刷新` : ""}</span>
                  </button>
                ))}
              </div>
              <div className="sr-strat-row">
                <StratCard id="A" cur={strategy} on={setStrategy} title="System Prompt" desc="把观察 + 禁忌写进系统提示" />
                <StratCard id="B" cur={strategy} on={setStrategy} title="Few-shot" desc="从样例索引直读示范段落" />
                <StratCard id="C" cur={strategy} on={setStrategy} title="RAG" desc="三粒度向量召回（Phase 3）" />
                <StratCard id="mixed" cur={strategy} on={setStrategy} title="混合" desc="A+B 组合，预算分配" />
              </div>
            </div>

            <div className="card">
              <div className="card-head"><div><div className="card-title">风格强度</div><div className="card-sub">控制注入的观察数量与约束力度</div></div>
                <span className="sr-intensity-val tab-num">{intensity}%</span>
              </div>
              <input type="range" min="0" max="100" value={intensity} onChange={e=>setIntensity(parseInt(e.target.value))} className="sr-range" />
              <div className="sr-intensity-ticks"><span>轻微借鉴</span><span>均衡</span><span>强烈复刻</span></div>
              <div className="sr-intensity-readout">
                <I.Sparkles size={13} />
                <span>当前将注入约 <b>{Math.max(2, obsCount)}</b> 条观察 · <b>{selectedDims.length}</b> 个维度 · 禁忌红线 <b>全量</b> 固定保留</span>
              </div>
            </div>

            <div className="card">
              <div className="card-head">
                <div><div className="card-title">注入维度</div><div className="card-sub">勾选要参与注入的 sub-dim（{selectedDims.length} / 12 已选）</div></div>
                <button className="btn btn-quiet btn-sm" onClick={() => {
                  const all = [];
                  SR_LAYERS.forEach(l => l.input !== "skip" && l.subs.forEach(s => s.conf !== "skip" && all.push(`${l.id}.${s.id}`)));
                  setSelectedDims(selectedDims.length === all.length ? [] : all);
                }}>{selectedDims.length === 12 ? "全不选" : "全选"}</button>
              </div>
              <div className="sr-dimselect">
                {SR_LAYERS.map(l => (
                  <div key={l.id} className="sr-ds-layer">
                    <div className="sr-ds-layer-name">{l.name}</div>
                    <div className="sr-ds-cells">
                      {l.subs.map(s => {
                        const path = `${l.id}.${s.id}`;
                        const disabled = s.conf === "skip";
                        const on = selectedDims.includes(path);
                        return (
                          <button key={s.id} className={`sr-ds-cell ${on ? "is-on" : ""} ${disabled ? "is-disabled" : ""}`}
                            onClick={() => !disabled && toggleDim(path)} disabled={disabled}>
                            {on && <I.Check size={11} />}{s.name}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </>
        )}

        {sub === "layers" && (realMode ? (
          <SrLayersReal stack={layerStack} err={layerErr} />
        ) : isRealBook ? (
          <div className="card">
            <div className="card-head">
              <div><div className="card-title">叠加注入层</div><div className="card-sub">scene &gt; character &gt; project &gt; global 逐层加权</div></div>
            </div>
            <div className="sr-stack-note">
              <I.Info size={13} />
              <span>尚未合成风格画像——完成抽取并「合成风格画像」后，这里显示该书的真实注入层与预算分配。</span>
            </div>
          </div>
        ) : (
          <div className="card">
            <div className="card-head">
              <div><div className="card-title">叠加注入层</div><div className="card-sub">由泛到具体加权全叠：scene &gt; character &gt; project &gt; global，越具体预算越大</div></div>
              <span className="pill"><span className="pill-dot" />{SR_LAYER_STACK.length} 层 · {totalTokens} tok</span>
            </div>

            <div className="sr-stack">
              {SR_LAYER_STACK.map((l, i) => (
                <div key={l.scope} className="sr-stack-layer">
                  <div className={`sr-stack-rank rank-${l.tone}`}>rank {l.rank}</div>
                  <div className="sr-stack-body">
                    <div className="sr-stack-top">
                      <span className={`pill pill-${l.tone} text-xs`}><span className="pill-dot" />{l.label}</span>
                      <span className="sr-stack-target text-serif">{l.scope === "project" && WsWorks ? WsWorks.active().title : l.target}</span>
                      {l.onstage && <span className="sr-stack-onstage">+ 在场 {l.onstage.join("、")}</span>}
                      <span className="sr-stack-frags">{l.frags} fragments</span>
                    </div>
                    <div className="sr-stack-budget">
                      <div className="sr-stack-budget-track">
                        <div className={`sr-stack-budget-fill fill-${l.tone}`} style={{width: (l.tokens / 880 * 100) + "%"}} />
                      </div>
                      <span className="sr-stack-weight">权重 ×{l.weight}</span>
                      <span className="sr-stack-tokens tab-num">{l.tokens} tok</span>
                    </div>
                  </div>
                </div>
              ))}
            </div>

            <div className="sr-stack-note">
              <I.Info size={13} />
              <span>合并规则：base + 最具体增量逐层叠加；forbidden 行级去重；同一 metric 取最具体层；token 按 <b>weights=range(1,n+1)</b> 加权分配（scene 最大）。qc gate 单选时走 <code>resolve_active_binding</code> 透明返回合并后的单一 fragments。</span>
            </div>
          </div>
        ))}

        {sub === "fewshot" && (
          <div className="card">
            <div className="card-head">
              <div><div className="card-title">Few-shot 示例（策略 B / 混合）</div><div className="card-sub">从 profile.scene_samples_index 按段类型 O(1) 直读，不绕段落表</div></div>
              {!realMode && <span className="pill pill-gold"><span className="pill-dot" />k = 5</span>}
            </div>
            {strategy === "A" && (
              <div className="sr-fewshot-warn"><I.Info size={13} /><span>当前策略为 A（System Prompt），不注入 few-shot。切到 B 或 混合 以启用示例。</span></div>
            )}
            {strategy === "C" && (
              <div className="sr-fewshot-warn"><I.Info size={13} /><span>当前策略为 C（RAG），示例由三粒度向量召回提供（rag_block），不走 few-shot 直读。</span></div>
            )}
            {realMode ? (
              (() => {
                const fs = String((preview && preview.fragments && preview.fragments.few_shot_block) || "").trim();
                if (!fs) {
                  return (strategy === "B" || strategy === "mixed")
                    ? <div className="text-xs text-muted" style={{padding:"8px 2px"}}>该画像暂无可注入样例——样例索引为空（重跑抽取后重新合成画像可补齐）。</div>
                    : <div className="text-xs text-muted" style={{padding:"8px 2px"}}>切到 B 或 混合 策略后，这里显示实际注入的样例引文。</div>;
                }
                const lines = fs.split("\n").filter(l => l.trim());
                return (
                  <div className="sr-fewshot-list">
                    {lines.map((l, i) => (
                      i === 0
                        ? <p key={i} className="text-xs text-muted" style={{margin:"0 0 4px"}}>{l}</p>
                        : <div key={i} className="sr-fewshot-item"><p className="sr-fewshot-text text-serif" style={{margin:0}}>{l.replace(/^-\s*/, "")}</p></div>
                    ))}
                  </div>
                );
              })()
            ) : (
              <>
                <div className="sr-fewshot-list">
                  {Object.entries(SR_FEWSHOT).map(([k, v]) => (
                    <div key={k} className="sr-fewshot-item">
                      <div className="sr-fewshot-meta">
                        <span className="pill text-xs"><span className="pill-dot" />{v.note}</span>
                        <span className="sr-fewshot-id">{v.id}</span>
                      </div>
                      <p className="sr-fewshot-text text-serif">{v.text}</p>
                    </div>
                  ))}
                </div>
                <p className="text-xs text-muted mt-3">每 sub-dim 索引：对话 10 · 动作 6 · 心理 8 · 环境 14（见画像页样例索引）。</p>
              </>
            )}
          </div>
        )}

        {sub === "banned" && (
          <div className="card">
            <div className="card-head">
              <div><div className="card-title">禁用词编辑</div><div className="card-sub">generation = 生成红线段禁用 · extraction = 重跑抽取时跳过含此词的段落</div></div>
              {realMode && <span className="pill pill-sage text-xs"><span className="pill-dot" />画像级 · 已接后端</span>}
            </div>
            <div className="sr-banned-add">
              <input className="input" placeholder="添加禁用词…" value={bannedInput} disabled={bannedBusy}
                onChange={e=>setBannedInput(e.target.value)} onKeyDown={e=>e.key==="Enter"&&(realMode ? addBannedReal() : addBanned())} />
              <div className="seg">
                <button className={`seg-btn ${bannedScope==="generation"?"is-active":""}`} onClick={()=>setBannedScope("generation")}>generation</button>
                <button className={`seg-btn ${bannedScope==="extraction"?"is-active":""}`} onClick={()=>setBannedScope("extraction")}>extraction</button>
              </div>
              <button className="btn btn-primary btn-sm" disabled={bannedBusy} onClick={() => (realMode ? addBannedReal() : addBanned())}><I.Plus size={13} /> 添加</button>
            </div>
            {realMode ? (
              <>
                <ul className="sr-banned-list">
                  {(realBanned || []).map((b) => (
                    <li key={b.term_id} className="sr-banned-item">
                      <span className={`sr-banned-scope sc-${b.scope}`}>{b.scope === "generation" ? "生成" : "抽取"}</span>
                      <span className="sr-banned-term text-serif">{b.term}</span>
                      {b.replacement_hint && <span className="sr-banned-hint">→ {b.replacement_hint}</span>}
                      {b.source === "preset" && <span className="sr-banned-preset">预置</span>}
                      {b.source !== "preset" && (
                        <button className="btn btn-quiet btn-sm" disabled={bannedBusy} onClick={()=>removeBannedReal(b.term_id)}><I.X size={13} /></button>
                      )}
                    </li>
                  ))}
                </ul>
                {realBanned && realBanned.length === 0 && (
                  <p className="text-xs text-muted" style={{padding:"6px 2px"}}>暂无禁用词。generation 词会立即进入右侧红线段；extraction 词在下次重跑抽取时过滤段落。</p>
                )}
                {!realBanned && <p className="text-xs text-muted" style={{padding:"6px 2px"}}>加载中…</p>}
              </>
            ) : (
              <ul className="sr-banned-list">
                {banned.map((b, i) => (
                  <li key={i} className="sr-banned-item">
                    <span className={`sr-banned-scope sc-${b.scope}`}>{b.scope === "generation" ? "生成" : "抽取"}</span>
                    <span className="sr-banned-term text-serif">{b.term}</span>
                    {b.hint && <span className="sr-banned-hint">→ {b.hint}</span>}
                    {b.source === "preset" && <span className="sr-banned-preset">预置</span>}
                    <button className="btn btn-quiet btn-sm" onClick={()=>setBanned(prev=>prev.filter((_,j)=>j!==i))}><I.X size={13} /></button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>

      {/* Right: bundle preview + bindings */}
      <aside className="sr-apply-side">
        <div className="card-flat sr-bundle">
          <div className="ctx-head" style={{marginBottom: 10}}><I.FileText size={13} /><span>SystemPromptFragments · {realMode ? "实时预览" : "有序"}</span></div>
          {realMode ? (
            <SrBundleReal preview={preview} previewErr={previewErr} />
          ) : (
            <>
              <div className="sr-bundle-frag">
                <div className="sr-frag-label"><span className="sr-frag-ord">1</span> 风格定性概述</div>
                <p className="sr-frag-text">冷峻克制白描，短句为骨，逗号顿连…</p>
              </div>
              <div className="sr-bundle-frag">
                <div className="sr-frag-label danger"><span className="sr-frag-ord">2</span><I.Ban size={11} /> banned_pattern_block</div>
                <p className="sr-frag-text">禁：排比抒情长句 · 陈词滥调比喻 · 比喻后解释…</p>
              </div>
              <div className="sr-bundle-frag">
                <div className="sr-frag-label"><span className="sr-frag-ord">3</span> observations_by_dim · {Math.max(2, obsCount)} 条</div>
                <p className="sr-frag-text">句式：短句独立成段 / 词汇：乡土具象…</p>
              </div>
              <div className="sr-bundle-frag fixed">
                <div className="sr-frag-label lock"><span className="sr-frag-ord">4</span><I.ShieldCheck size={11} /> anti_plagiarism_block · 固定</div>
                <p className="sr-frag-text">严禁复制原文表达、人物、桥段与标志性意象。</p>
              </div>
              <div className="sr-budget-bar">
                <div className="sr-budget-track">
                  <div className="sr-budget-fill" style={{width: (totalTokens / 3000 * 100) + "%"}} />
                </div>
                <div className="sr-budget-legend">
                  <span className="tab-num">{totalTokens}</span> / 3000 tok 预算
                </div>
              </div>
            </>
          )}
          <div className="sr-bundle-meta">
            <span>策略 {strategy === "mixed" ? "A+B" : strategy}</span>
            <span>·</span>
            <span>{task.refresh > 0 ? `续写每 ${task.refresh} 字刷新` : "一次性注入"}</span>
          </div>
        </div>

        {task.refresh > 0 && (
          <div className="card-flat sr-drift">
            <div className="ctx-head" style={{marginBottom: 10}}><I.Refresh size={13} /><span>长文防漂移</span></div>
            <div className="sr-drift-track">
              {[0,1,2,3].map(i => (
                <div key={i} className="sr-drift-seg">
                  <div className="sr-drift-bar" />
                  {i < 3 && <div className="sr-drift-tick"><I.Refresh size={10} /></div>}
                </div>
              ))}
            </div>
            <p className="text-xs text-muted mt-2">每生成 {task.refresh} 字带最新 context 重调注入，5000+ 字续写 inject ≥3 次，防止回归 base 腔调。</p>
          </div>
        )}

        <div className="card-flat">
          <div className="ctx-head" style={{marginBottom: 10}}><I.GitBranch size={13} /><span>应用范围</span></div>
          <div className="sr-scope">
            {[["project","项目"],["scene","场景"],["character","角色"]].map(([id, name]) => (
              <button key={id}
                className={`sr-scope-btn ${scope === id ? "is-active" : ""}`}
                onClick={() => setScope(id)}>{name}</button>
            ))}
          </div>
          {/* 立项 A — scene/character 级目标选择器(真模式):选中 id 作为 effect.scope_ref_id */}
          {realMode && scope !== "project" && (
            <div className="sr-scope-target" style={{marginTop: 8}}>
              <select className="sr-select" value={scopeRefId || ""}
                onChange={(e) => setScopeRefId(e.target.value || null)}
                style={{width:"100%", padding:"6px 8px"}}>
                <option value="">{`选择${scope === "scene" ? "场景" : "角色"}…`}</option>
                {(scopeOpts[scope] || []).map(o => (
                  <option key={o.id} value={o.id}>{o.label}</option>
                ))}
              </select>
              {(scopeOpts[scope] || []).length === 0 && (
                <p className="text-xs text-muted" style={{marginTop:4}}>
                  {scope === "scene" ? "当前项目暂无场景(先在目录/构思生成场景)" : "当前项目暂无角色(先在构思补充角色)"}
                </p>
              )}
            </div>
          )}
          {realMode ? (
            <ul className="sr-bindings">
              {realBindings.length === 0 && (
                <li className="text-xs text-muted" style={{padding:"6px 2px", display:"block"}}>暂无已批准的绑定 · 应用并在收件箱批准后出现在此。</li>
              )}
              {realBindings.map(b => {
                const tone = b.scope === "project" ? "crimson" : b.scope === "character" ? "gold" : b.scope === "scene" ? "sage" : "slate";
                const sname = b.scope === "project" ? "项目" : b.scope === "character" ? "角色" : b.scope === "scene" ? "场景" : b.scope;
                return (
                  <li key={b.binding_id}>
                    <span className={`pill pill-${tone} text-xs`}><span className="pill-dot" />{sname}</span>
                    <span className="text-sm">{b.scope_ref_id || "—"} · {b.strategy === "mixed" ? "A+B" : b.strategy}</span>
                    <button className="btn btn-quiet btn-sm" onClick={() => {
                      window.srUnbind && window.srUnbind(b.binding_id, book.id).catch(e => window.alert("解绑失败：" + ((e && e.message) || e)));
                    }}>解绑</button>
                  </li>
                );
              })}
            </ul>
          ) : isRealBook ? (
            <ul className="sr-bindings">
              <li><span className="text-sm" style={{opacity:.7}}>尚无绑定——合成风格画像后，在上方「立项应用」为项目 / 角色 / 场景创建绑定。</span></li>
            </ul>
          ) : (
            <ul className="sr-bindings">
              <li><span className="pill pill-crimson text-xs"><span className="pill-dot" />项目</span><span className="text-sm">示例项目 · 全局</span><I.Check size={13} style={{color:"var(--sage)"}} /></li>
              <li><span className="pill pill-gold text-xs"><span className="pill-dot" />角色</span><span className="text-sm">示例角色 POV</span><button className="btn btn-quiet btn-sm" disabled title="演示绑定不可修改">演示绑定</button></li>
              <li><span className="pill pill-sage text-xs"><span className="pill-dot" />场景</span><span className="text-sm">CH08 · SC01</span><button className="btn btn-quiet btn-sm" disabled title="演示绑定不可修改">演示绑定</button></li>
            </ul>
          )}
        </div>

        <button className="btn btn-accent btn-lg" style={{width:"100%"}}
          disabled={!!applied || (realMode && scope !== "project" && !scopeRefId)} onClick={() => {
          if (!rvPush) return;
          const _act = (WsWorks && WsWorks.active && WsWorks.active()) || null;
          const workTitle = (_act && _act.title) || "当前作品";
          const projId = activeProjId;
          const selOpt = scope !== "project" ? (scopeOpts[scope] || []).find(o => o.id === scopeRefId) : null;
          const selLabel = selOpt ? selOpt.label : (scopeRefId || "");
          const scopeName = scope === "project" ? `项目《${workTitle}》`
            : scope === "scene" ? (realMode ? `场景 ${selLabel}` : (isRealBook ? "场景" : "场景 CH08 · SC01"))
            : (realMode ? `角色 ${selLabel}` : (isRealBook ? "角色" : "角色 示例角色 POV"));
          // 立项 A — scope_ref_id:项目级用 project_id,场景/角色级用所选目标 id(显式传,不靠后端回退)
          const effScopeRefId = scope === "project" ? projId : scopeRefId;
          if (realMode) {
            const profileTitle = (deep && deep.profile && deep.profile.title) || `${book.author || "参考"}风格画像`;
            rvPush({
              kind: "decision", priority: 1,
              title: `参考画像「${profileTitle}」应用到${scopeName}`,
              where: "风格参考 · 注入应用", source: "风格参考",
              detail: `策略 ${strategy === "mixed" ? "A+B 混合" : strategy} · 强度 ${intensity}% · ${selectedDims.length} 维。批准后画像绑定到该范围、作为生成期默认润色基线，可随时回风格参考解绑。`,
              dedupe_key: `style-apply:${realProfileId}:${scope}:${effScopeRefId || "_"}:${strategy}`,
              actions: [
                { label: "批准应用", intent: "primary", op: "resolve",
                  effect: {
                    type: "bind_style_profile",
                    profile_id: realProfileId,
                    scope, scope_ref_id: effScopeRefId, task_type: taskType, strategy, intensity,
                    sub_dimensions: selectedDims,
                    include_positive: true, include_forbidden: true, include_metric: strategy !== "C",
                  } },
                { label: "回风格参考调整", intent: "ghost", op: "nav", to: "styleref" },
                { label: "丢弃", intent: "quiet", op: "resolve" },
              ],
            });
          } else {
            rvPush({
              kind: "decision", priority: 1,
              title: `参考画像「冷峻短句」应用到${scopeName}`,
              where: "风格参考 · 注入应用", source: "风格参考",
              detail: `策略 ${strategy === "mixed" ? "A+B 混合" : strategy} · 强度 ${intensity}% · 注入预算 ${totalTokens} tok。（演示画像：导入真实参考书并合成画像后，应用将创建携带配置的真实绑定决策。）`,
              actions: [
                { label: "批准应用", intent: "primary", op: "resolve" },
                { label: "回风格参考调整", intent: "ghost", op: "nav", to: "styleref" },
                { label: "丢弃", intent: "quiet", op: "resolve" },
              ],
            });
          }
          setApplied(scopeName);
        }}>
          <I.Check size={15} /> {applied ? "已进入审核" : `应用到${scope === "project" ? "项目" : scope === "scene" ? "场景" : "角色"} · 进审核`}
        </button>
        {applied ? (
          <p className="text-xs" style={{textAlign:"center", color:"var(--sage)", fontWeight:600}}>
            已为{applied}创建审核条目 · <a href="#review" style={{color:"inherit"}}>去待办收件箱拍板 →</a>
          </p>
        ) : (
          <p className="text-xs text-muted" style={{textAlign:"center"}}>应用后在「待办收件箱」创建决策条目，批准后才作为运行时规则生效。</p>
        )}
      </aside>
    </div>
  );
}

/* 叠加注入层 — 真数据渲染（GET /injection/layers：命中层 + 权重/预算 + 合并概要） */
const SR_SCOPE_TONE = { scene: "sage", character: "gold", project: "crimson", global: "slate" };
const SR_SCOPE_LABEL = { scene: "场景层", character: "角色层", project: "项目层", global: "全局基底" };

function SrLayersReal({ stack, err }) {
  if (err) {
    return <div className="card"><div className="sr-fewshot-warn"><I.Info size={13} /><span>叠加注入层：{err}</span></div></div>;
  }
  if (!stack) {
    return <div className="card"><div className="text-xs text-muted" style={{padding:"10px 2px"}}>正在解析当前项目的注入叠层…</div></div>;
  }
  const layers = stack.layers || [];
  const total = stack.budget_total || 800;
  const merged = stack.merged;
  if (layers.length === 0) {
    return (
      <div className="card" style={{padding: "32px 20px", textAlign: "center"}}>
        <I.Layers size={24} style={{color: "var(--ink-3)"}} />
        <div className="mt-2 text-muted text-sm">当前项目没有已批准的激活绑定——先在「策略与维度」应用画像并在收件箱批准，这里会显示真实的注入叠层。</div>
      </div>
    );
  }
  const maxBudget = Math.max(...layers.map(l => l.budget_chars || 0), 1);
  return (
    <div className="card">
      <div className="card-head">
        <div><div className="card-title">叠加注入层</div><div className="card-sub">由泛到具体加权全叠：scene &gt; character &gt; project &gt; global，越具体预算越大</div></div>
        <span className="pill pill-sage"><span className="pill-dot" />{layers.length} 层 · 预算 {total} 字</span>
      </div>

      <div className="sr-stack">
        {layers.map(l => {
          const tone = SR_SCOPE_TONE[l.scope] || "slate";
          return (
            <div key={l.binding_id} className="sr-stack-layer">
              <div className={`sr-stack-rank rank-${tone}`}>rank {l.rank}</div>
              <div className="sr-stack-body">
                <div className="sr-stack-top">
                  <span className={`pill pill-${tone} text-xs`}><span className="pill-dot" />{SR_SCOPE_LABEL[l.scope] || l.scope}</span>
                  <span className="sr-stack-target text-serif">{l.profile_title || l.profile_id}</span>
                  <span className="text-xs text-muted">{l.scope_ref_id || "—"} · 策略 {l.strategy === "mixed" ? "A+B" : l.strategy}</span>
                  <span className="sr-stack-frags">{l.fragment_count} fragments</span>
                </div>
                <div className="sr-stack-budget">
                  <div className="sr-stack-budget-track">
                    <div className={`sr-stack-budget-fill fill-${tone}`} style={{width: ((l.budget_chars || 0) / maxBudget * 100) + "%"}} />
                  </div>
                  <span className="sr-stack-weight">权重 ×{l.weight}</span>
                  <span className="sr-stack-tokens tab-num">{l.budget_chars} 字</span>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <div className="sr-stack-note">
        <I.Info size={13} />
        <span>
          {merged && merged.layer_count > 1
            ? <>合并产物：{merged.layer_count} 层 → 单一 fragments（策略取最具体层 {merged.strategy === "mixed" ? "A+B" : merged.strategy}），最终注入前缀 <b className="tab-num">{merged.prefix_chars}</b> 字。forbidden 行级去重、metric 取最具体层、few-shot/RAG 不参与叠加。</>
            : <>单层路径：strategy 全语义直出（不做叠加截断），最终注入前缀 <b className="tab-num">{merged ? merged.prefix_chars : 0}</b> 字。</>}
        </span>
      </div>
    </div>
  );
}

function StratCard({ id, cur, on, title, desc }) {
  return (
    <button className={`sr-strat ${cur === id ? "is-active" : ""}`} onClick={() => on(id)}>
      <span className="sr-strat-badge">{id === "mixed" ? "A+B" : id}</span>
      <span className="sr-strat-title">{title}</span>
      <span className="sr-strat-desc">{desc}</span>
    </button>
  );
}

/* 真实注入预览（dryrun）的 SystemPromptFragments 渲染 */
function SrBundleReal({ preview, previewErr }) {
  if (previewErr) {
    return <div className="sr-fewshot-warn"><I.Info size={13} /><span>注入预览：{previewErr}</span></div>;
  }
  if (!preview) {
    return <div className="text-xs text-muted" style={{padding:"10px 2px"}}>正在生成注入预览…</div>;
  }
  const f = preview.fragments || {};
  const clip = (t) => { const s = String(t || "").replace(/\n+/g, " · ").trim(); return s.length > 130 ? s.slice(0, 130) + "…" : s; };
  const ordered = [
    ["positive_block", "narrative / observations", false],
    ["forbidden_block", "banned_pattern_block", true],
    ["metric_anchor_block", "metric_anchor_block", false],
    ["few_shot_block", "few_shot_block", false],
  ];
  const present = ordered.filter(([k]) => f[k] && String(f[k]).trim());
  const hasAnti = !!(f.anti_plagiarism_block && String(f.anti_plagiarism_block).trim());
  const prefixLen = (preview.prefix || "").length;
  if (present.length === 0 && !hasAnti) {
    return <div className="text-xs text-muted" style={{padding:"10px 2px"}}>该画像暂无可注入内容——需先抽取并合成出观察后再应用。</div>;
  }
  return (
    <>
      {present.map(([k, label, danger], i) => (
        <div key={k} className="sr-bundle-frag">
          <div className={`sr-frag-label ${danger ? "danger" : ""}`}><span className="sr-frag-ord">{i + 1}</span>{danger && <I.Ban size={11} />} {label}</div>
          <p className="sr-frag-text">{clip(f[k])}</p>
        </div>
      ))}
      <div className="sr-bundle-frag fixed">
        <div className="sr-frag-label lock"><span className="sr-frag-ord">★</span><I.ShieldCheck size={11} /> anti_plagiarism_block · 固定</div>
        <p className="sr-frag-text">{clip(f.anti_plagiarism_block || "严禁复制原文表达、人物、桥段与标志性意象。")}</p>
      </div>
      <div className="sr-budget-bar">
        <div className="sr-budget-track">
          <div className="sr-budget-fill" style={{width: Math.min(100, prefixLen / 800 * 100) + "%"}} />
        </div>
        <div className="sr-budget-legend">
          <span className="tab-num">{prefixLen}</span> / 800 字 注入预算
        </div>
      </div>
    </>
  );
}


/* ==========================================================
   FE-ALIGN F5：参考书库接 style_reference v2。
   - 书库列表/导入/删除/重跑/重分类走真实 API；
   - 列表一律以后端为准，后端为空即空态引导导入。
   ========================================================== */
let SR_REAL = false;

function srMapStatus(s) {
  if (s === "ready") return "ready";
  if (/extract|run/i.test(s || "")) return "extracting";
  return "pending";
}

async function srSyncBooks() {
  let rows = [];
  try {
    rows = ((await apiGet("/api/v2/style-reference/books")) || {}).books || [];
  } catch (e) { return; }
  if (!rows.length) {
    if (SR_REAL || SR_BOOKS.length) { SR_BOOKS = []; SR_REAL = false; window.dispatchEvent(new CustomEvent("sr:books-changed")); }
    return;
  }
  const colors = ["crimson", "gold", "slate", "sage"];
  SR_BOOKS = rows.map((b, i) => ({
    id: b.book_id,
    title: b.title,
    author: b.author_label || "未署名",
    chars: b.total_chars || 0,
    status: srMapStatus(b.status),
    profiles: 0,
    run: b.status === "ready" ? "已导入 · 待抽取" : b.status,
    color: colors[i % colors.length],
    real: true,
  }));
  SR_REAL = true;
  window.dispatchEvent(new CustomEvent("sr:books-changed"));
}

/* 把对话框收集的声明整理成后端 rights_declaration 的形状；local_only 且未声明时返回 null（后端记 declared=false）。 */
function srBuildRightsDeclaration(cloudPolicy, rights) {
  if (!rights || rights.declared === false) return null;
  return {
    declared: true,
    analysis_rights: rights.analysis_rights === true,
    send_rights: srPolicyNeedsSendRights(cloudPolicy) && rights.send_rights === true,
    declared_by: getOperatorRef(),
  };
}

/* 导入参考书：文件选择 → POST import-upload（multipart，带幂等键 + 权属声明） */
function srImportBook(cloudPolicy = "local_only", rights = null) {
  if (!SR_CLOUD_POLICIES.some((item) => item.id === cloudPolicy)) {
    throw new Error("未知的参考书数据策略");
  }
  const rightsDeclaration = srBuildRightsDeclaration(cloudPolicy, rights);
  // 与后端同一条红线：云端策略没有 send_rights=true 就不打开文件选择器，更不会发请求。
  if (srPolicyNeedsSendRights(cloudPolicy) && !(rightsDeclaration && rightsDeclaration.send_rights)) {
    throw new Error("云端策略需要作者先确认发送权声明；未获授权请改用「仅保存在本机」。");
  }
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".txt,.md,.epub,.docx";
  input.onchange = async () => {
    const f = input.files && input.files[0];
    if (!f) return;
    const title = (window.prompt("书名（用于书库显示）", f.name.replace(/\.[^.]+$/, "")) || "").trim();
    if (!title) return;
    try {
      const fd = new FormData();
      fd.append("file", f, f.name);
      fd.append("title", title);
      // 策略必须来自作者在导入前的显式选择；默认 local_only，绝不静默放宽出域范围。
      fd.append("cloud_policy", cloudPolicy);
      // 后端以 JSON 串的 Form 字段接收声明（api/routes/style_reference.py import_book_upload）。
      if (rightsDeclaration) fd.append("rights_declaration", JSON.stringify(rightsDeclaration));
      const headers = {
        "X-Idempotency-Key": "sr-import-" + Date.now().toString(36),
        "X-Operator-Ref": getOperatorRef(),
      };
      const accessToken = getRemoteAccessToken();
      if (accessToken) headers["X-Novel-Access-Token"] = accessToken;
      const res = await fetch(buildUrl("/api/v2/style-reference/books/import-upload"), {
        method: "POST",
        headers,
        body: fd,
      });
      const body = await res.json();
      if (!body.ok) {
        // 原样透出后端信封里的 message（含 STYLE_REFERENCE_SEND_RIGHTS_* 的引导语），附 code 便于对照日志。
        const err = (body && body.error) || {};
        const message = err.message || `导入失败（HTTP ${res.status}）`;
        throw new Error(err.code ? `${message}［${err.code}］` : message);
      }
      await srSyncBooks();
      window.alert(`已导入《${title}》（${(((body.data || {}).book || {}).total_chars || 0).toLocaleString()} 字）。`);
    } catch (e) { window.alert("导入失败：" + (e.message || e)); }
  };
  input.click();
}

/* 头部动作（真实书）：重跑抽取 / 重新分类。LLM 未启用时给明确引导。 */
async function srBookAction(action, bookId, opts = {}) {
  try {
    if (action === "rerun") {
      // 后台模式：立即返回 run_id，按 coverage_json.progress 轮询(2.5s),
      // 全 16 维抽取可达数分钟,同步等待会撞 HTTP 超时
      const res = await apiPost(`/api/v2/style-reference/books/${bookId}/runs`, { background: true, force: !!opts.force });
      const runId = res && res.run_id;
      window.alert("抽取已在后台启动（按层推进），完成后会提示。");
      if (runId) srPollRun(runId);
    } else if (action === "reclassify") {
      await apiPost(`/api/v2/style-reference/books/${bookId}/reclassify`, {});
      window.alert("已重新分类段落。");
    }
  } catch (e) {
    if (e && e.code === "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED") {
      window.alert("这本书的云端策略是「仅本地」，风格抽取需要把段落送 LLM 分析。请删除后以「按段落送云」策略重新导入。");
    } else if (e && e.code === "STYLE_REFERENCE_INPUT_TOO_SMALL" && !opts.force) {
      // §6.4 输入量门槛：全部分析层被评估为 skip。给一键强制重试（明知样本少仍要抽）
      const goOn = window.confirm(
        "这本书字数太少，按输入量门槛所有分析层都被评估为「跳过」，抽取不会执行。\n\n" +
        "建议补足语料后重新导入；也可以点「确定」强制抽取（样本过少时画像可信度很低）。"
      );
      if (goOn) return srBookAction("rerun", bookId, { force: true });
    } else if (e && (e.code === "STYLE_REFERENCE_LLM_REQUIRED" || /llm/i.test(e.code || ""))) {
      window.alert("风格抽取需要先启用 LLM：请到「系统设置 → 模型与接入」配置并开启后重试。");
    } else {
      window.alert("操作失败：" + (e.message || e));
    }
  }
  await srSyncBooks();
}

const srPollRegistry = window.__srStylePollRegistry instanceof Map
  ? window.__srStylePollRegistry
  : new Map();
window.__srStylePollRegistry = srPollRegistry;

function srStopPoll(runId, token) {
  const current = srPollRegistry.get(runId);
  if (!current || (token && current.token !== token)) return;
  clearTimeout(current.timer);
  srPollRegistry.delete(runId);
}

/* 后台抽取轮询：层粒度进度，完成/失败时提示并刷新书库。最长轮询 20 分钟。 */
async function srPollRun(runId) {
  if (!runId) return;
  srStopPoll(runId);
  const startedAt = Date.now();
  const token = Symbol(runId);
  const record = { token, timer: null };
  srPollRegistry.set(runId, record);
  const schedule = () => {
    if (srPollRegistry.get(runId)?.token !== token) return;
    record.timer = setTimeout(tick, 2500);
  };
  const tick = async () => {
    if (srPollRegistry.get(runId)?.token !== token) return;
    if (Date.now() - startedAt > 20 * 60 * 1000) { srStopPoll(runId, token); return; }
    let run = null;
    try { run = ((await apiGet(`/api/v2/style-reference/runs/${runId}`)) || {}).run || null; } catch (e) { /* 网络抖动下一轮再试 */ }
    if (srPollRegistry.get(runId)?.token !== token) return;
    const status = run && run.status;
    if (status === "done") {
      srStopPoll(runId, token);
      await srSyncBooks();
      window.alert("风格抽取完成，维度矩阵已可查看。");
      return;
    }
    if (status === "failed" || status === "cancelled") {
      srStopPoll(runId, token);
      await srSyncBooks();
      window.alert(status === "failed" ? "风格抽取失败，可重试或查看系统日志。" : "风格抽取已取消。");
      return;
    }
    schedule();
  };
  schedule();
}

async function srDeleteBook(bookId) {
  const headers = {
    "X-Idempotency-Key": `sr-del-${bookId}-${Date.now().toString(36)}`,
    "X-Operator-Ref": getOperatorRef(),
  };
  const accessToken = getRemoteAccessToken();
  if (accessToken) headers["X-Novel-Access-Token"] = accessToken;
  const res = await fetch(buildUrl(`/api/v2/style-reference/books/${bookId}`), {
    method: "DELETE",
    // book_id 由内容 checksum 决定（同内容重导=同 id），删除键必须带熵，
    // 否则幂等层会重放上一次的成功响应而不真正执行
    headers,
  });
  const body = await res.json();
  if (!body.ok) throw new Error((body.error && body.error.message) || "删除失败");
  await srSyncBooks();
  return true;
}

/* ==========================================================
   深层页真后端 store（按 book 懒加载 profile + bindings）
   有真画像 → 注入应用走真后端；无（演示书 / 未合成）→ 回退演示。
   范式同 srSyncBooks：内存缓存 + 懒加载 + 防重 + window 事件广播。
   ========================================================== */
const SR_DEEP = {};            // bookId -> { profileId, profile, bindings, loaded, error }
const SR_DEEP_FETCHING = {};

function srDeepFor(bookId) { return SR_DEEP[bookId] || null; }

async function srLoadDeep(bookId, { force = false } = {}) {
  if (!bookId) return null;
  if (!force && SR_DEEP[bookId]) return SR_DEEP[bookId];
  if (SR_DEEP_FETCHING[bookId]) return SR_DEEP_FETCHING[bookId];
  SR_DEEP_FETCHING[bookId] = (async () => {
    const out = {
      book: null, runId: null, run: null,
      findingsByDim: {}, dimCounts: {},
      profileId: null, profile: null, bindings: [],
      loaded: true, error: null,
    };
    try {
      // 1. 书详情（stats_json：metrics / input_assessment / 段型分布 / 分类器校准）
      try {
        const r = await apiGet(`/api/v2/style-reference/books/${encodeURIComponent(bookId)}`);
        out.book = (r && r.book) || null;
      } catch (e) { /* 详情失败不致命 */ }
      // 2. 最新 run（优先 done，否则最新一条）
      try {
        const rr = await apiGet(`/api/v2/style-reference/books/${encodeURIComponent(bookId)}/runs`);
        const runs = (rr && rr.runs) || [];
        out.run = runs.find(r => r.status === "done") || runs[0] || null;
        out.runId = out.run ? out.run.run_id : null;
      } catch (e) { /* 无 run 列表则矩阵走演示 */ }
      // 3. 该 run 的 findings（含证据）→ 按 sub_dim 分组 + 计数
      if (out.runId) {
        try {
          const fr = await apiGet(`/api/v2/style-reference/runs/${out.runId}/findings?include=evidence`);
          for (const f of (fr && fr.findings) || []) {
            const dim = f.sub_dimension;
            if (!out.findingsByDim[dim]) out.findingsByDim[dim] = { observations: [], forbidden_patterns: [] };
            (f.finding_kind === "forbidden_pattern" ? out.findingsByDim[dim].forbidden_patterns : out.findingsByDim[dim].observations).push(f);
          }
          for (const [dim, g] of Object.entries(out.findingsByDim)) {
            const confs = g.observations.map(o => o.confidence);
            const conf = confs.includes("high") ? "high" : confs.includes("medium") ? "medium" : "low";
            const q = [...g.observations, ...g.forbidden_patterns].reduce((s, f) => s + ((f.evidence || []).length), 0);
            out.dimCounts[dim] = { obs: g.observations.length, fp: g.forbidden_patterns.length, q, conf };
          }
        } catch (e) { /* findings 失败则矩阵走演示 */ }
      }
      // 4. profile + bindings
      try {
        const pr = await apiGet(`/api/v2/style-reference/profiles?book_id=${encodeURIComponent(bookId)}`);
        const profiles = (pr && pr.profiles) || [];
        const chosen = profiles.find(p => p.status === "active") || profiles[profiles.length - 1] || null;
        out.profileId = chosen ? chosen.profile_id : null;
        out.profile = chosen;
        if (chosen) {
          try {
            const b = await apiGet(`/api/v2/style-reference/profiles/${chosen.profile_id}/bindings`);
            out.bindings = (b && b.bindings) || [];
          } catch (e) { /* 绑定拉取失败不致命 */ }
        }
      } catch (e) { /* profile 失败则画像/应用走演示 */ }
    } catch (e) {
      out.error = (e && e.message) || String(e);
    } finally {
      SR_DEEP[bookId] = out;
      delete SR_DEEP_FETCHING[bookId];
      window.dispatchEvent(new CustomEvent("sr:deep-changed"));
    }
    return SR_DEEP[bookId];
  })();
  return SR_DEEP_FETCHING[bookId];
}

/* dryrun 注入预览（不写盘）：返回真实 fragments + prefix。失败抛 ApiRequestError。 */
async function srInjectionPreview(profileId, body) {
  return apiPost(`/api/v2/style-reference/profiles/${profileId}/injection-preview`, body);
}

/* 解绑：DELETE binding 后强制重载该 book 的深层数据。 */
async function srUnbind(bindingId, bookId) {
  await apiDelete(`/api/v2/style-reference/bindings/${bindingId}`);
  await srLoadDeep(bookId, { force: true });
  return true;
}

/* 合成画像：POST synthesize（需 LLM）后强制重载。LLM 未启用时抛 ApiRequestError(409)。 */
async function srSynthesize(runId, bookId) {
  const r = await apiPost(`/api/v2/style-reference/runs/${runId}/synthesize`, {});
  await srLoadDeep(bookId, { force: true });
  return r;
}

/* finding 审核（approved / rejected / pending）后强制重载。 */
async function srReviewFinding(findingId, decision, bookId) {
  await apiPost(`/api/v2/style-reference/findings/${findingId}/review`, { decision });
  await srLoadDeep(bookId, { force: true });
  return true;
}

/* 立项 B — finding 用户反馈(👍/👎):聚合后按阈值调档 confidence,强制重载使 deep 体现。 */
async function srFindingFeedback(findingId, vote, bookId) {
  await apiPost(`/api/v2/style-reference/findings/${findingId}/user-feedback`, { vote });
  await srLoadDeep(bookId, { force: true });
  return true;
}

/* 画像预览：生成 3 段示例 + 自跑回测（需 LLM）。 */
async function srPreviewSamples(profileId) {
  return apiPost(`/api/v2/style-reference/profiles/${profileId}/preview`, {});
}

if (window.__srStyleGlobalHandlers) {
  clearTimeout(window.__srStyleGlobalHandlers.hydrateTimer);
  window.removeEventListener("hashchange", window.__srStyleGlobalHandlers.hashchange);
}
const srHydrateTimer = setTimeout(() => srSyncBooks(), 800); // 启动水合
const srHashChange = () => {
  if ((location.hash || "").indexOf("styleref") >= 0) srSyncBooks();
};
window.addEventListener("hashchange", srHashChange);
window.__srStyleGlobalHandlers = { hydrateTimer: srHydrateTimer, hashchange: srHashChange };

Object.assign(window, {
  WsStyleRef, srSyncBooks, srImportBook, srBookAction, srDeleteBook,
  srLoadDeep, srDeepFor, srInjectionPreview, srUnbind,
  srSynthesize, srReviewFinding, srFindingFeedback, srPreviewSamples,
});

/* ESM 导出（Phase 1 机械追加；window.* 赋值过渡期保留） */
export { WsStyleRef, SrImportDialog, SR_CLOUD_POLICIES, SR_RIGHTS_TERMS, srImportBook, srRightsReady };

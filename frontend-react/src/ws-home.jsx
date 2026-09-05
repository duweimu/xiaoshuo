import React from "react";
import { I } from "./icons.jsx";
import { WsWorks, useActiveWork, useWorksStatus, wsKey } from "./ws-works.jsx";
import { WsCatalog, useCatalogChapters } from "./ws-catalog.jsx";
import { RV_KINDS, rvOpenItems, rvMarkResolved } from "./ws-review.jsx";
import { WsAiProviders, useAiProviders } from "./ws-ai-providers.jsx";

/* global React, I, useActiveWork, useCatalogChapters */
/* ==========================================================
   WsHome — 项目主页 (calm orienting command center)
   One question, answered the moment you arrive:
   「此刻该写哪一幕?」 — everything else is quiet context.
   Layout: masthead (identity + book progress) → focus hero
   (the one scene + last lines you wrote) → 雪花/待办 → 最近章节.
   ----------------------------------------------------------
   现在所有身份与进度数据都来自「当前作品」(useActiveWork)。
   一部刚新建的空白作品会显示引导性空状态，而不是空指标。
   ========================================================== */

const HOME_CHAP_ST = { approved: "定稿", review: "送审", draft: "草稿", writing: "在写", planned: "规划" };

function WsHome({ go }) {
  const work = useActiveWork();
  const remote = useWorksStatus(work && work.id);
  const chapters = useCatalogChapters ? useCatalogChapters() : [];
  const isBlank = chapters.length === 0;

  if (!work.id && remote.projects.phase === "ready") return <WsHomeNoWorks remote={remote} />;
  if (isBlank) return <WsHomeBlank work={work} go={go} remote={remote} />;
  return <WsHomeFull work={work} go={go} chapters={chapters} remote={remote} />;
}

function WsHomeDataNotice({ remote, workId }) {
  const dashboardError = remote && remote.dashboard && remote.dashboard.error;
  const projectsError = remote && remote.projects && remote.projects.error;
  const error = dashboardError || projectsError;
  if (!error) return null;
  const scope = dashboardError ? "dashboard" : "projects";
  const phase = scope === "dashboard" ? remote.dashboard.phase : remote.projects.phase;
  return (
    <div className="hm-data-notice" role="status" aria-live="polite">
      <span className="hm-data-notice-ic"><I.AlertTriangle size={15} /></span>
      <div>
        <strong>{error.offline ? "当前离线 · 正在使用本地缓存" : "远端数据暂时没有更新"}</strong>
        <span>{error.message || "你的本地内容仍可继续使用。"}</span>
      </div>
      <button type="button" className="btn btn-ghost btn-sm" disabled={phase === "loading"} onClick={() => { void WsWorks.retry(scope, workId); }}>
        <I.Refresh size={13} /> {phase === "loading" ? "重试中…" : "重新连接"}
      </button>
    </div>
  );
}

/* ===== full home — a work with momentum =====
   结构性数据（章节 / 当前场景 / GMC / 进度）全部派生自 WsCatalog；
   雪花进度读构思工作台的持久化状态；待办读收件箱 store。
   「上次写到这里」来自服务端 dashboard 缓存，浏览器正文只在更新时覆盖它。 */
function WsHomeFull({ work: p, go, chapters, remote }) {
  const home = p.home || {};

  /* —— 当前章 / 当前场（单一真相源）—— */
  const cur = chapters.find(c => c.current) || chapters.find(c => c.state === "writing") || chapters[chapters.length - 1];
  const wIdx = cur ? cur.scenes.findIndex(s => s.state === "writing") : -1;
  const sIdx = wIdx >= 0 ? wIdx : 0;
  const curScene = cur && cur.scenes[sIdx] ? cur.scenes[sIdx] : null;
  const slug = cur && curScene
    ? `CH ${cur.n} · SC ${String(sIdx + 1).padStart(2, "0")} · ${(curScene.kind || "主动")}场景`
    : (home.slug || "");
  const sceneTitle = curScene ? curScene.title : (home.scene || "—");
  const gos = curScene ? [
    { k: "目标", tone: "sage", v: curScene.goal || "（本场目标待规划）" },
    { k: "阻碍", tone: "gold", v: curScene.obstacle || "（阻碍待规划）" },
    { k: "挫折", tone: "crimson", v: curScene.turn || "（挫折待规划）" },
  ] : (home.gos || []);

  /* —— 进度（与切换器 / 成稿中心同源）—— */
  const totals = WsCatalog ? WsCatalog.totals() : { words: p.wordsTotal, written: p.chaptersWritten, planned: chapters.length };
  const pct = Math.min(100, Math.round((totals.words / Math.max(1, p.wordsTarget)) * 100));
  const dayPct = Math.min(100, Math.round((p.wordsToday / Math.max(1, p.wordsTargetDay)) * 100));

  /* —— 雪花：读构思工作台的持久化真相，而非静态拷贝 —— */
  const snowLive = window.s2StepSummary ? window.s2StepSummary() : null;
  const snow = snowLive ? snowLive.steps : (home.snow || []);
  const snowNow = snowLive ? snowLive.now : (home.snowNow || "—");
  const snowDone = snow.filter(s => s.s === "done").length;

  const dashboardResume = home.resume || {};
  /* —— 「上次写到这里」：优先读写作器落盘的真实正文（取末两段），服务端 dashboard 作兜底 —— */
  const liveLines = (() => {
    try {
      if (!curScene || !curScene.sid) return null;
      const raw = localStorage.getItem(wsKey ? wsKey("wr-doc:" + curScene.sid) : "wr-doc:" + curScene.sid);
      if (raw == null) return null;
      const div = document.createElement("div");
      div.innerHTML = raw;
      const paras = Array.from(div.querySelectorAll("p, li")).map(x => (x.textContent || "").trim()).filter(Boolean);
      if (!paras.length || (paras.length === 1 && /^在这里开始写/.test(paras[0]))) return null;
      return paras.slice(-2);
    } catch (e) { return null; }
  })();
  const resume = {
    ch: cur ? cur.n : (dashboardResume.ch || "01"),
    lines: liveLines || dashboardResume.lines || [],
    sceneWords: curScene && typeof curScene.words === "number" ? curScene.words : (dashboardResume.sceneWords || 0),
    pausedAgo: liveLines ? "" : (dashboardResume.pausedAgo || ""),
  };

  const chaps = chapters.slice(-5).map(c => ({
    n: c.n, t: c.title, s: c.state,
    pct: c.words && c.words.target ? Math.min(100, Math.round(((c.words.cur || 0) / c.words.target) * 100)) : 0,
    active: !!(c.current || c.state === "writing"),
  }));

  // 与「待办收件箱」同源（store）：取优先级最高的几条，主页只做速览；
  // 在这里「标记处理」会真实落盘，徽标与收件箱同步消失。
  const RK = RV_KINDS || {};
  const [todos, setTodos] = React.useState(() =>
    (rvOpenItems ? rvOpenItems() : [])
      .slice().sort((a, b) => a.priority - b.priority).slice(0, 3)
      .map(it => ({ id: it.id, kind: it.kind, title: it.title, where: it.where }))
  );
  const dismissTodo = (id) => {
    if (rvMarkResolved) { try { rvMarkResolved([id]); } catch (e) {} }
    setTodos(prev => prev.filter(x => x.id !== id));
  };
  const decisionsLeft = todos.filter(t => t.kind === "decision").length;

  return (
    <div className="ws-page ws-view hm" data-screen-label="主页">
      <WsHomeDataNotice remote={remote} workId={p.id} />
      <WsAiSetupNotice go={go} />
      {/* ===== masthead — identity + book progress ===== */}
      <header className="hm-top">
        <div className="hm-id">
          <div className="hm-greet"><span className="hm-greet-dot" /> {p.greet || "继续写作"}</div>
          <h1 className="hm-title">{p.title}</h1>
          <p className="hm-logline">{p.sub}</p>
        </div>
        <div className="hm-book" role="group" aria-label="全书进度">
          <HomeRing pct={pct} size={66} />
          <div className="hm-book-meta">
            <div className="hm-book-lbl">全书进度</div>
            <div className="hm-book-val"><b>{totals.written}</b> / {totals.planned} 章</div>
            <div className="hm-book-sub">{(totals.words / 10000).toFixed(1)} 万 / {(p.wordsTarget / 10000).toFixed(0)} 万字</div>
          </div>
        </div>
      </header>

      {/* ===== focus hero — the one scene to write now ===== */}
      <section className="hm-hero">
        <div className="hm-hero-main">
          <div className="hm-hero-bar">
            <span className="hm-eyebrow"><I.Compass size={13} /> 此刻 · 继续写作</span>
            <span className="hm-today" title="今日写作目标">
              <span className="hm-today-txt">今日 <b>{p.wordsToday.toLocaleString()}</b> / {p.wordsTargetDay.toLocaleString()} 字</span>
              <span className="hm-today-bar"><i style={{ width: dayPct + "%" }} /></span>
              <span className="hm-today-streak"><I.Activity size={12} /> {p.streak} 天连续</span>
            </span>
          </div>
          <div className="hm-slug">{slug}</div>
          <h2 className="hm-scene">{sceneTitle}</h2>
          <div className="hm-gos">
            {gos.map(g => (
              <div className="hm-gos-row" key={g.k}>
                <span className={`hm-gos-k t-${g.tone}`}>{g.k}</span>
                <span className="hm-gos-v">{g.v}</span>
              </div>
            ))}
          </div>
          <div className="hm-hero-actions">
            <button className="btn btn-accent btn-lg" onClick={() => go("writer")}><I.Pen size={16} /> 进入写作房间</button>
            <button className="btn btn-ghost btn-lg" onClick={() => go("snowflake")}><I.Snowflake size={16} /> 回到构思</button>
          </div>
        </div>

        <button className="hm-resume" onClick={() => go("writer")} title="回到上次中断处">
          <span className="hm-resume-tab">CH {resume.ch}</span>
          <div className="hm-resume-head"><I.Quote size={12} /> 上次写到这里</div>
          <div className="hm-resume-body">
            {resume.lines.length === 0 && <p>这一场还没有正文——进去写下第一段，它会出现在这里。<span className="hm-caret" /></p>}
            {resume.lines.map((ln, i) => (
              <p key={i}>{ln}{i === resume.lines.length - 1 ? <span className="hm-caret" /> : null}</p>
            ))}
          </div>
          <div className="hm-resume-foot">
            <span className="hm-resume-words">本场景 {resume.sceneWords.toLocaleString()} 字</span>
            {resume.pausedAgo ? <span className="hm-resume-ago"><I.Clock size={11} /> 暂停于 {resume.pausedAgo}</span> : null}
          </div>
        </button>
      </section>

      {/* ===== secondary: snowflake + todo ===== */}
      <section className="home-row">
        <button className="home-card" onClick={() => go("snowflake")}>
          <div className="home-card-head">
            <div className="home-card-title"><span className="ic"><I.Snowflake size={17} /></span> 构思 · 雪花十步</div>
            <span className="home-card-go">打开 <I.ArrowRight size={13} /></span>
          </div>
          <div className="home-snow-track">
            {snow.map((s, i) => <span key={i} className={`home-snow-tick s-${s.s}`} title={s.name} />)}
          </div>
          <div className="home-snow-now">
            <div>
              <div className="home-snow-now-label">当前焦点</div>
              <div className="home-snow-now-name">{snowNow || "—"}</div>
            </div>
            <div className="home-snow-count"><b>{snowDone}</b> <span className="text-muted text-sm">/ {snow.length} 已确认</span></div>
          </div>
        </button>

        <div className="home-card is-static">
          <div className="home-card-head">
            <div className="home-card-title"><span className="ic"><I.Inbox size={17} /></span> 待办收件箱</div>
            <button type="button" className="home-card-go home-card-go-btn" onClick={() => go("review")}>全部 <I.ArrowRight size={13} /></button>
          </div>
          <div className="home-todo-list">
            {todos.length === 0 && (
              <div className="home-todo-empty"><I.CheckCircle size={18} /> 待办都处理完了，回去继续写吧。</div>
            )}
            {todos.map(it => {
              const m = RK[it.kind] || { tone: "slate", label: "待办" };
              const isDecision = it.kind === "decision";
              return (
                <button className="home-todo" key={it.id} onClick={() => go("review")} title="去待办收件箱处理">
                  <span className={`pill pill-${m.tone} text-xs`}><span className="pill-dot" />{m.label}</span>
                  <span className="home-todo-text">{it.title}</span>
                  <span className="home-todo-go" title={isDecision ? "需要拍板 · 去收件箱选一个选项" : "标记处理"}
                    onClick={(e) => { e.stopPropagation(); if (isDecision) { go("review"); } else { dismissTodo(it.id); } }}>
                    {isDecision ? <I.ArrowRight size={14} /> : <I.Check size={14} />}
                  </span>
                </button>
              );
            })}
            {todos.length > 0 && (
              <div className="home-todo-foot">
                共 <b>{todos.length}</b> 条速览{decisionsLeft ? <> · <b>{decisionsLeft}</b> 条需决策</> : ""}，到收件箱看全部。
              </div>
            )}
          </div>
        </div>
      </section>

      {/* ===== recent chapters ===== */}
      <section className="hm-chaps">
        <div className="hm-chaps-head">
          <div className="hm-chaps-title">最近章节</div>
          <button className="btn btn-quiet btn-sm" onClick={() => go("writer")}>全部章节 <I.ArrowRight size={13} /></button>
        </div>
        <div className="hm-chap-track">
          {chaps.map(c => (
            <button key={c.n} className={`hm-chap s-${c.s} ${c.active ? "is-active" : ""}`} onClick={() => go("writer")}>
              <div className="hm-chap-top">
                <span className="hm-chap-n">CH {c.n}</span>
                <span className={`hm-chap-st st-${c.s}`}>{HOME_CHAP_ST[c.s] || "草稿"}</span>
              </div>
              <div className="hm-chap-t">{c.t}</div>
              <div className="hm-chap-bar"><i style={{ width: c.pct + "%" }} /></div>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}

/* ===== blank home — a brand-new work, still a blank page ===== */
const HOME_START_STEPS = [
  { icon: "Snowflake", view: "snowflake", title: "雪花构思", desc: "从一句话故事开始，逐步长出人物与大纲。", cta: "开始十步" },
  { icon: "Library", view: "library", title: "建立资料库", desc: "登记人物、地点与设定，随写随查。", cta: "打开资料" },
  { icon: "Beaker", view: "styleref", title: "设定风格基调", desc: "给这部作品定一个叙述声音与语感。", cta: "去设定" },
];

function WsHomeNoWorks({ remote }) {
  const openNewWork = () => window.dispatchEvent(new CustomEvent("ws:new-work"));
  return (
    <div className="ws-page ws-view hm" data-screen-label="主页 · 空书架">
      <WsHomeDataNotice remote={remote} workId="" />
      <section className="hm-empty">
        <div className="hm-empty-mark" data-accent="slate">新</div>
        <h1 className="hm-empty-title">书架还是空的</h1>
        <p className="hm-empty-sub">先创建第一部作品，构思、资料、章节和正文才会有清晰且彼此隔离的归属。</p>
        <div className="hm-empty-actions">
          <button className="btn btn-accent btn-lg" data-testid="empty-create-work" onClick={openNewWork}>
            <I.Plus size={16} /> 创建第一部作品
          </button>
        </div>
      </section>
    </div>
  );
}

/* 模型未就绪时在主页提前说明，避免作者走到 AI 动作后才遇到 409/502。 */
function WsAiSetupNotice({ go }) {
  const ai = useAiProviders();
  React.useEffect(() => {
    if (!ai.loaded && !ai.loading && !ai.error) {
      void WsAiProviders.refresh().catch(() => {});
    }
  }, [ai.loaded, ai.loading, ai.error]);
  const readiness = ai.overview && ai.overview.readiness;
  const globallyDisabled = ai.overview && ai.overview.api_snapshot && ai.overview.api_snapshot.enabled === false;
  if (!ai.loaded || !readiness || (readiness.ready === true && !globallyDisabled)) return null;
  return (
    <div className="hm-data-notice" role="status" aria-live="polite">
      <span className="hm-data-notice-ic"><I.Sparkles size={15} /></span>
      <div>
        <strong>AI 尚未就绪</strong>
        <span>开始生成前，请先在系统设置中配置并启用一个可用模型。</span>
      </div>
      <button type="button" className="btn btn-ghost btn-sm" onClick={() => go && go("settings")}>
        <I.Settings size={13} /> 去配置
      </button>
    </div>
  );
}

function WsHomeBlank({ work: p, go, remote }) {
  return (
    <div className="ws-page ws-view hm" data-screen-label="主页 · 新作品">
      <WsHomeDataNotice remote={remote} workId={p.id} />
      <WsAiSetupNotice go={go} />
      <header className="hm-top">
        <div className="hm-id">
          <div className="hm-greet"><span className="hm-greet-dot" /> {p.greet || "新的开始"}</div>
          <h1 className="hm-title">{p.title}</h1>
          <p className="hm-logline">{p.sub || "还没有简介——可以先用一句话，说清这部作品是关于什么的。"}</p>
        </div>
        <div className="hm-book" role="group" aria-label="全书进度">
          <HomeRing pct={0} size={66} />
          <div className="hm-book-meta">
            <div className="hm-book-lbl">全书进度</div>
            <div className="hm-book-val"><b>0</b> 章</div>
            <div className="hm-book-sub">目标 {(p.wordsTarget / 10000).toFixed(0)} 万字</div>
          </div>
        </div>
      </header>

      <section className="hm-empty">
        <div className="hm-empty-mark" data-accent={p.accent}>{p.mark}</div>
        <h2 className="hm-empty-title">这部作品还是一张白纸</h2>
        <p className="hm-empty-sub">先把念头落成结构，再开始写。<br />不知道从哪起步的话，雪花十步会一步步带着你走。</p>
        <div className="hm-empty-actions">
          <button className="btn btn-accent btn-lg" onClick={() => go("snowflake")}><I.Snowflake size={16} /> 开始雪花构思</button>
          <button className="btn btn-ghost btn-lg" onClick={() => go("writer")}><I.Pen size={16} /> 直接进入写作</button>
        </div>
      </section>

      <section className="hm-start">
        <div className="hm-chaps-head"><div className="hm-chaps-title">起步清单</div></div>
        <div className="hm-start-grid">
          {HOME_START_STEPS.map(s => {
            const Ic = I[s.icon] || I.Dot;
            return (
              <button key={s.view} className="hm-start-card" onClick={() => go(s.view)}>
                <span className="hm-start-ic"><Ic size={20} /></span>
                <div className="hm-start-title">{s.title}</div>
                <div className="hm-start-desc">{s.desc}</div>
                <span className="hm-start-cta">{s.cta} <I.ArrowRight size={13} /></span>
              </button>
            );
          })}
        </div>
      </section>
    </div>
  );
}

function HomeRing({ pct, size = 132 }) {
  const sw = Math.max(6, Math.round(size * 0.105));
  const r = (size - sw) / 2 - 1, c = 2 * Math.PI * r, dash = (pct / 100) * c, cx = size / 2;
  return (
    <svg className="home-ring hm-ring" width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      <defs>
        <linearGradient id="wsRing" x1="0" x2="1" y1="0" y2="1">
          <stop offset="0%" stopColor="var(--crimson)" />
          <stop offset="100%" stopColor="var(--gold)" />
        </linearGradient>
      </defs>
      <circle cx={cx} cy={cx} r={r} fill="none" stroke="var(--line-1)" strokeWidth={sw} />
      <circle cx={cx} cy={cx} r={r} fill="none" stroke="url(#wsRing)" strokeWidth={sw} strokeLinecap="round"
        strokeDasharray={c} strokeDashoffset={c - dash} transform={`rotate(-90 ${cx} ${cx})`} />
      <text x={cx} y={cx + size * 0.055} textAnchor="middle"
        style={{ fontSize: size * 0.3, fontWeight: 600, fill: "var(--ink-1)", fontFamily: "var(--font-serif)" }}>
        {pct}<tspan fontSize={size * 0.155} dy={-size * 0.04}>%</tspan>
      </text>
    </svg>
  );
}

export { WsHome };

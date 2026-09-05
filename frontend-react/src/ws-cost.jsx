import React from "react";
import { apiGet } from "./lib/client.js";
import { useStoreTick } from "./lib/store-utils.js";
import { StatCard } from "./ws-quality-ui.jsx";

/* global React, I */
/* ==========================================================
   WsCost — 成本看板（结果闭环治理 §5.8/§6.3/§10）
   数据源（均只读）：
     GET /api/v2/projects/{id}/cost-dashboard?days=N
         —— 项目级一读聚合：summary + 逐日趋势 + 模型/节点/章节
            构成 + Top 调用 + 全局额度快照
     GET /api/v2/projects/{id}/cost-summary?chapter_id= / ?scene_id=
         —— 章节 / 场景下钻（编排信号面板同口径）
   口径纪律（§5.8）：跨 provider 的 token 不直接相加（分词器不同，
   汇总以费用为准）；三口径 estimate / provider_actual /
   budget_charged；价格来自 config/pricing.yaml 快照，占位价全部
   is_estimate——接入真实计费后替换单价即可。
   跟随当前作品：视图挂载/切换作品时自动加载，无需手输项目 ID。
   ========================================================== */

let csState = {
  projectId: null,
  level: "project",   // project | chapter | scene
  days: 30,
  dashboard: null,    // 项目级一读聚合（含 summary/trend/by_*/top_calls）
  summary: null,      // 当前层 summary（project 时 = dashboard.summary；下钻时为下钻结果）
  quota: null,
  loading: false,
  error: null,
};

function csSnapshot() { return csState; }
function csEmit() { try { window.dispatchEvent(new CustomEvent("ws:cost-changed")); } catch (e) {} }

const PHASE_LABEL = {
  candidate_generation: "候选生成",
  quality_check: "质检 QC",
  revision: "修订",
  review: "评审",
  other: "其他",
};
const COST_WINDOWS = [7, 30, 90];

async function costLoad(projectId, opts = {}) {
  if (!projectId) return null;
  const days = Number(opts.days) > 0 ? Math.floor(Number(opts.days)) : csState.days;
  const level = opts.sceneId ? "scene" : opts.chapterId ? "chapter" : "project";
  csState = { ...csState, projectId, level, days, loading: true, error: null };
  csEmit();
  try {
    const base = `/api/v2/projects/${encodeURIComponent(projectId)}`;
    let data;
    if (level === "project") {
      data = await apiGet(`${base}/cost-dashboard?days=${days}`);
      csState = {
        ...csState,
        dashboard: data || null,
        summary: (data && data.summary) || null,
        quota: (data && data.quota) || null,
        loading: false,
      };
    } else {
      const qs = opts.sceneId
        ? `scene_id=${encodeURIComponent(opts.sceneId)}`
        : `chapter_id=${encodeURIComponent(opts.chapterId)}`;
      data = await apiGet(`${base}/cost-summary?${qs}`);
      csState = {
        ...csState,
        level: (data && data.level) || level,
        summary: (data && data.summary) || null,
        quota: (data && data.quota) || csState.quota,
        loading: false,
      };
    }
    csEmit();
    return data;
  } catch (e) {
    csState = { ...csState, loading: false, error: (e && e.message) || "成本加载失败。" };
    csEmit();
    return null;
  }
}

/* 下钻返回：dashboard 还在缓存里就地还原，不重发请求 */
function costBack() {
  if (csState.dashboard) {
    csState = { ...csState, level: "project", summary: csState.dashboard.summary || null, error: null };
    csEmit();
    return;
  }
  if (csState.projectId) costLoad(csState.projectId);
}

function useCostState() {
  const [, force] = React.useReducer((x) => x + 1, 0);
  React.useEffect(() => {
    const h = () => force();
    window.addEventListener("ws:cost-changed", h);
    return () => window.removeEventListener("ws:cost-changed", h);
  }, []);
  return csSnapshot();
}

/* 当前作品（window.WsWorks 全局，惰性访问——不 ES import，避免拖动书架副作用） */
function useCostActiveWork() {
  useStoreTick((bump) => {
    window.addEventListener("ws:work-changed", bump);
    let un = null;
    try { un = window.WsWorks && window.WsWorks.subscribe && window.WsWorks.subscribe(bump); } catch (e) {}
    return () => {
      window.removeEventListener("ws:work-changed", bump);
      if (typeof un === "function") { try { un(); } catch (e) {} }
    };
  });
  try {
    const w = window.WsWorks && window.WsWorks.active && window.WsWorks.active();
    return w && w.id !== "__loading__" ? w : null;
  } catch (e) { return null; }
}

/* ---- 格式化 ---- */
function fmtMoney(v, cur) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  return `${n.toFixed(n >= 100 ? 2 : 4)} ${cur || "USD"}`;
}
function fmtInt(v) { return v === null || v === undefined ? "—" : Number(v).toLocaleString(); }
function fmtPct(v) { return v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`; }
function fmtDay(d) { return (d || "").slice(5); }
function fmtTime(iso) { return (iso || "").replace("T", " ").slice(5, 16); }

function chapterLabel(chapterId) {
  if (!chapterId) return "未关联章节";
  try {
    const list = window.WsCatalog && window.WsCatalog.get && window.WsCatalog.get();
    const ch = Array.isArray(list) ? list.find((c) => c && c.id === chapterId) : null;
    if (ch) return `第${ch.n || "?"}章 · ${ch.title || chapterId}`;
  } catch (e) {}
  return chapterId;
}

/* ==========================================================
   小部件
   ========================================================== */

function EstimatePill({ on }) {
  if (!on) return null;
  return <span className="pill pill-gold text-xs" title="价格来自 config/pricing.yaml 的占位估算单价，非权威计费"><span className="pill-dot" />估算价</span>;
}

/* 逐日费用柱状图：后端稠密补零序列，纯 SVG，无第三方依赖 */
function TrendChart({ trend, currency }) {
  const series = (trend && trend.series) || [];
  if (!series.length) return null;
  const W = 720, H = 110, PADX = 2;
  const slot = (W - PADX * 2) / series.length;
  const bw = Math.max(2, slot - 2);
  const maxCost = Math.max(...series.map((d) => d.cost || 0));
  const scale = maxCost > 0 ? (H - 12) / maxCost : 0;
  const activeDays = series.filter((d) => (d.call_count || 0) > 0).length;
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none"
           role="img" aria-label={`近 ${series.length} 天逐日成本柱状图`}
           style={{ display: "block", background: "var(--paper-2, #f7f5ef)", borderRadius: 8 }}>
        {series.map((d, i) => {
          const h = (d.cost || 0) > 0 ? Math.max(2, d.cost * scale) : 1;
          return (
            <rect key={d.date} x={PADX + i * slot} y={H - h} width={bw} height={h} rx={1}
                  fill={(d.call_count || 0) > 0 ? "var(--acc, #667a64)" : "var(--line-2, #dcd8ce)"}>
              <title>{`${d.date} · ${fmtMoney(d.cost, currency)} · ${fmtInt(d.tokens)} tok · ${fmtInt(d.call_count)} 次`}</title>
            </rect>
          );
        })}
      </svg>
      <div className="text-xs" style={{ display: "flex", justifyContent: "space-between", gap: 8, marginTop: 4, color: "var(--ink-3)" }}>
        <span>{fmtDay(series[0].date)}</span>
        <span>
          窗口合计 {fmtMoney(trend.window_cost, currency)} · {fmtInt(trend.window_tokens)} tok · {fmtInt(trend.window_call_count)} 次
          {activeDays === 0 ? " · 窗口内暂无调用" : ""}
        </span>
        <span>{fmtDay(series[series.length - 1].date)}</span>
      </div>
    </div>
  );
}

function MeterBar({ ratio, danger }) {
  const pct = Math.round(Math.min(1, Math.max(0, ratio || 0)) * 100);
  return (
    <div style={{ height: 8, borderRadius: 5, background: "var(--line-1, #e7e4dc)", overflow: "hidden" }}>
      <div style={{ width: `${pct}%`, height: "100%", background: danger ? "var(--crimson, #a64b3c)" : "var(--acc, #667a64)" }} />
    </div>
  );
}

function PhaseBars({ breakdown, currency }) {
  const rows = Object.keys(PHASE_LABEL)
    .map((k) => ({ k, tokens: 0, cost: 0, share: 0, call_count: 0, ...((breakdown || {})[k] || {}) }))
    .filter((r) => (r.call_count || 0) > 0);
  if (!rows.length) return <div className="text-xs" style={{ color: "var(--ink-3)" }}>暂无阶段成本。</div>;
  return (
    <div style={{ display: "grid", gap: 8 }}>
      {rows.map((r) => (
        <div key={r.k}>
          <div className="text-xs" style={{ display: "flex", justifyContent: "space-between", gap: 8, marginBottom: 2 }}>
            <span>{PHASE_LABEL[r.k]}</span>
            <span style={{ color: "var(--ink-3)" }}>{fmtMoney(r.cost, currency)} · {fmtPct(r.share)} · {fmtInt(r.call_count)} 次</span>
          </div>
          <div role="progressbar" aria-label={`${PHASE_LABEL[r.k]}占比`} aria-valuemin="0" aria-valuemax="100" aria-valuenow={Math.round((r.share || 0) * 100)}>
            <MeterBar ratio={r.share} />
          </div>
        </div>
      ))}
    </div>
  );
}

function ModelTable({ byModel, currency }) {
  if (!byModel || !byModel.length) return <div className="text-xs" style={{ color: "var(--ink-3)" }}>暂无模型调用。</div>;
  return (
    <div style={{ overflowX: "auto" }}>
      <table className="lib-table" aria-label="按模型的成本构成">
        <thead>
          <tr><th>提供商</th><th>模型</th><th>费用</th><th>Token</th><th>调用</th><th>价源</th></tr>
        </thead>
        <tbody>
          {byModel.map((m) => (
            <tr key={`${m.provider}:${m.model}`}>
              <td>{m.provider}</td>
              <td>{m.model}</td>
              <td>{fmtMoney(m.cost, currency)}</td>
              <td>{fmtInt(m.tokens)}</td>
              <td>{fmtInt(m.call_count)}</td>
              <td>{m.is_estimate
                ? <span className="pill pill-gold text-xs"><span className="pill-dot" />估算</span>
                : <span className="pill pill-slate text-xs"><span className="pill-dot" />实际</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function NodeBars({ byNode, currency }) {
  const top = (byNode && byNode.top) || [];
  if (!top.length) return <div className="text-xs" style={{ color: "var(--ink-3)" }}>暂无节点成本。</div>;
  const max = Math.max(...top.map((n) => n.cost || 0), 1e-12);
  const remainder = byNode.remainder;
  return (
    <div style={{ display: "grid", gap: 8 }}>
      {top.map((n) => (
        <div key={n.node_id}>
          <div className="text-xs" style={{ display: "flex", justifyContent: "space-between", gap: 8, marginBottom: 2 }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6, minWidth: 0 }}>
              <code style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{n.node_id}</code>
              <span className="pill pill-slate text-xs"><span className="pill-dot" />{PHASE_LABEL[n.phase] || n.phase}</span>
            </span>
            <span style={{ color: "var(--ink-3)", whiteSpace: "nowrap" }}>{fmtMoney(n.cost, currency)} · {fmtInt(n.call_count)} 次</span>
          </div>
          <MeterBar ratio={(n.cost || 0) / max} />
        </div>
      ))}
      {remainder && (
        <div className="text-xs" style={{ color: "var(--ink-3)" }}>
          其余 {fmtInt(remainder.node_count)} 个节点合计 {fmtMoney(remainder.cost, currency)} · {fmtInt(remainder.call_count)} 次
        </div>
      )}
    </div>
  );
}

function ChapterTable({ byChapter, currency, onDrill, loading }) {
  if (!byChapter || !byChapter.length) return <div className="text-xs" style={{ color: "var(--ink-3)" }}>暂无章节成本——生成过草稿的章节会出现在这里。</div>;
  return (
    <div style={{ overflowX: "auto" }}>
      <table className="lib-table" aria-label="按章节的成本构成">
        <thead>
          <tr><th>章节</th><th>费用</th><th>Token</th><th>调用</th><th>涉及场景</th><th></th></tr>
        </thead>
        <tbody>
          {byChapter.map((c) => (
            <tr key={c.chapter_id || "__none__"}>
              <td>{chapterLabel(c.chapter_id)}</td>
              <td>{fmtMoney(c.cost, currency)}</td>
              <td>{fmtInt(c.tokens)}</td>
              <td>{fmtInt(c.call_count)}</td>
              <td>{fmtInt(c.scene_count)}</td>
              <td>
                {c.chapter_id && (
                  <button className="btn btn-ghost btn-sm" disabled={loading} onClick={() => onDrill(c.chapter_id)}>
                    下钻 {I && I.ChevronRight && <I.ChevronRight size={12} />}
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TopCallsTable({ topCalls, currency, onDrillScene, loading }) {
  if (!topCalls || !topCalls.length) return <div className="text-xs" style={{ color: "var(--ink-3)" }}>暂无调用明细。</div>;
  return (
    <div style={{ overflowX: "auto" }}>
      <table className="lib-table" aria-label="费用最高的调用明细">
        <thead>
          <tr><th>时间</th><th>节点</th><th>模型</th><th>Token</th><th>费用</th><th>延迟</th><th>状态</th><th>场景</th></tr>
        </thead>
        <tbody>
          {topCalls.map((c) => (
            <tr key={c.llm_call_id}>
              <td style={{ whiteSpace: "nowrap" }}>{fmtTime(c.created_at)}</td>
              <td>
                <code>{c.node_id || "—"}</code>{" "}
                <span className="pill pill-slate text-xs"><span className="pill-dot" />{PHASE_LABEL[c.phase] || c.phase}</span>
              </td>
              <td style={{ whiteSpace: "nowrap" }}>{c.provider || "—"} / {c.model || "—"}</td>
              <td>{fmtInt(c.total_tokens)}</td>
              <td>{fmtMoney(c.cost, c.currency || currency)}</td>
              <td>{c.latency_ms === null || c.latency_ms === undefined ? "—" : `${fmtInt(c.latency_ms)} ms`}</td>
              <td>
                {c.error_code
                  ? <span className="pill pill-crimson text-xs" title={c.error_code}><span className="pill-dot" />失败</span>
                  : <span className="pill pill-slate text-xs"><span className="pill-dot" />{c.accounting_status || "—"}</span>}
              </td>
              <td>
                {c.scene_id
                  ? <button className="btn btn-ghost btn-sm" disabled={loading} onClick={() => onDrillScene(c.scene_id)}>{c.scene_id}</button>
                  : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const QUOTA_METER_KEYS = [
  "daily_tokens",
  "monthly_tokens",
  "project_daily_tokens",
  "daily_requests",
  "concurrent_requests",
  "daily_cost_usd",
];

// 只认 limit：后端仅在闸门关闭时把 limit 置 null。不要拿 enforced 当判据——
// 缺该字段的载荷（旧后端、下钻时保留的旧 quota）会被判成「未设限」，在闸门
// 其实已启用时谎报无上限。安全展示必须朝「有上限」的方向失败。
function quotaArmed(meter) {
  return !!meter && meter.limit !== null && meter.limit !== undefined;
}

function QuotaRow({ label, meter, suffix = "token", decimals = 0, armedOnly = false }) {
  if (!meter) return null;
  const armed = quotaArmed(meter);
  if (armedOnly && !armed) return null;
  const used = Number(meter.used || 0);
  const fmt = (v) => (decimals > 0 ? Number(v).toFixed(decimals) : Number(v).toLocaleString());
  // 额度关闭时仍然报用量，只是没有可填的进度条。
  if (!armed) {
    return (
      <div className="text-xs" style={{ display: "flex", justifyContent: "space-between", gap: 12, marginBottom: 8 }}>
        <span>{label}</span>
        <span style={{ color: "var(--ink-3)" }}>{fmt(used)} {suffix} · 未设限</span>
      </div>
    );
  }
  const limit = Number(meter.limit);
  const ratio = limit > 0 ? Math.min(1, used / limit) : 0;
  return (
    <div style={{ marginBottom: 8 }}>
      <div className="text-xs" style={{ display: "flex", justifyContent: "space-between", gap: 12, marginBottom: 2 }}>
        <span>{label}</span>
        <span style={{ color: ratio >= 0.9 ? "var(--crimson, #a64b3c)" : "var(--ink-3)" }}>
          {fmt(used)} / {fmt(limit)} {suffix} · {Math.round(ratio * 100)}%
        </span>
      </div>
      <div role="progressbar" aria-label={label} aria-valuemin="0" aria-valuemax={limit} aria-valuenow={used}>
        <MeterBar ratio={ratio} danger={ratio >= 0.9} />
      </div>
    </div>
  );
}

function QuotaSection({ quota }) {
  if (!quota) return null;
  // 同样不信任 any_enforced：由各 meter 自行推导，缺字段时判为「已武装」。
  const armed = QUOTA_METER_KEYS.some((key) => quotaArmed(quota[key]));
  const tz = quota.period_timezone || "UTC";
  return (
    <section className="card" aria-label={armed ? "全局模型额度" : "全局模型用量"} style={{ padding: "12px 14px", borderRadius: 12, border: "1px solid var(--line, #e5e2dc)" }}>
      <h3 className="text-serif" style={{ margin: "0 0 10px", fontSize: 15 }}>
        {I && I.ShieldCheck && <I.ShieldCheck size={14} />} {armed ? "全局硬额度" : "全局用量"}
      </h3>
      <QuotaRow label="今日总量" meter={quota.daily_tokens} />
      <QuotaRow label="本月总量" meter={quota.monthly_tokens} />
      <QuotaRow label="本项目今日" meter={quota.project_daily_tokens} />
      <QuotaRow label="今日请求" meter={quota.daily_requests} suffix="次" />
      <QuotaRow label="并发请求" meter={quota.concurrent_requests} suffix="路" />
      {/* 金额只在闸门启用时出现：它按 env 单价计价，与本页其余 pricing.yaml 口径
          不同，未启用时恒为 0，摆出来会和上方「全书累计成本」自相矛盾。 */}
      <QuotaRow label="今日金额" meter={quota.daily_cost_usd} suffix="USD" decimals={4} armedOnly />
      <small style={{ color: "var(--ink-3)" }}>
        {armed
          ? `额度按 ${tz} 结算；达到上限会在请求发出前拒绝，不产生模型费用。`
          : `按 ${tz} 结算的用量读数。当前未设任何硬额度，生成不会被额度拦下；需要上限时在后端设置对应的额度环境变量（如 NOVEL_SYSTEM_LLM_DAILY_TOKEN_LIMIT、NOVEL_SYSTEM_LLM_MAX_CONCURRENT_REQUESTS）并重启后端。`}
        {armed && !quotaArmed(quota.daily_cost_usd)
          ? " 金额上限未启用：需同时设置 NOVEL_SYSTEM_LLM_DAILY_COST_LIMIT_USD 与模型单价。"
          : ""}
      </small>
    </section>
  );
}

/* 三口径 + 尝试可观测性 + 独立性（默认收起的审计注脚） */
function CalibersDetails({ summary }) {
  const cal = summary && summary.calibers;
  const obs = summary && summary.attempt_observability;
  if (!cal && !obs) return null;
  const CAL_LABEL = { estimate: "估算口径", provider_actual: "供应商实际", budget_charged: "预算计费" };
  return (
    <details className="card" style={{ padding: "10px 14px", borderRadius: 12, border: "1px solid var(--line, #e5e2dc)" }}>
      <summary className="text-xs" style={{ cursor: "pointer", color: "var(--ink-2)" }}>口径与可观测性（审计注脚）</summary>
      <div style={{ display: "grid", gap: 10, marginTop: 10 }}>
        {summary.cross_provider && (
          <div className="text-xs" style={{ color: "var(--gold-deep, #a67c00)" }}>
            跨 provider：不同分词器的 token 不可直接相加，本页汇总以费用为准。
            {summary.tokens_by_provider && (
              <> 分列：{Object.entries(summary.tokens_by_provider).map(([p, t]) => `${p} ${fmtInt(t)}`).join(" · ")}</>
            )}
          </div>
        )}
        {cal && (
          <div className="text-xs" style={{ display: "grid", gap: 4 }}>
            {Object.entries(CAL_LABEL).map(([k, label]) => cal[k] && (
              <div key={k} style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
                <span>{label}</span>
                <span style={{ color: "var(--ink-3)" }}>{fmtInt(cal[k].tokens)} tok <code style={{ opacity: 0.7 }}>{cal[k].source}</code></span>
              </div>
            ))}
          </div>
        )}
        {obs && (
          <div className="text-xs" style={{ color: "var(--ink-3)" }}>
            物理尝试 {fmtInt(obs.physical_attempt_count)} · 重试 {fmtInt(obs.retry_attempt_count)}
            （传输 {fmtInt(obs.transport_retry_attempt_count)} / 解析 {fmtInt(obs.response_parse_retry_attempt_count)}）
            · 降级 {fmtInt(obs.degrade_attempt_count)} · 异常 {fmtInt(obs.exception_count)}
            · 用量为估算 {fmtInt(obs.usage_estimate_count)}
            {obs.legacy_parent_without_attempt_count > 0 && (
              <span style={{ color: "var(--gold-deep, #a67c00)" }}>
                {" "}· 含 {fmtInt(obs.legacy_parent_without_attempt_count)} 条旧账（{fmtInt(obs.legacy_unreconstructable_tokens)} tok 不可重构）
              </span>
            )}
          </div>
        )}
      </div>
    </details>
  );
}

/* ---- 下钻面板（章节 / 场景） ---- */
function DrillPanel({ st }) {
  const s = st.summary;
  if (!s) return null;
  const isScene = st.level === "scene";
  const budget = s.budget;
  const extra = s.extra_cost;
  return (
    <section style={{ display: "grid", gap: 12 }}>
      <div className="rv-toolbar" style={{ gap: 8 }}>
        <button className="btn btn-ghost btn-sm" onClick={costBack}>
          {I && I.ChevronLeft && <I.ChevronLeft size={13} />} 返回全书
        </button>
        <h2 className="text-serif" style={{ margin: 0, fontSize: 17 }}>
          {isScene ? `场景成本 · ${s.scene_id}` : `${chapterLabel(s.chapter_id)} · 章节成本`}
        </h2>
        <EstimatePill on={s.is_estimate} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10 }}>
        <StatCard label="总成本" value={fmtMoney(s.total_cost, s.currency)} />
        <StatCard label="Token" value={fmtInt(s.total_tokens)} hint={s.cross_provider ? "跨 provider，以费用为准" : null} />
        <StatCard label="调用数" value={fmtInt(s.call_count)} />
        {!isScene && <StatCard label="已归档场景" value={fmtInt(s.archived_scene_count)} hint={s.tokens_per_archived_scene ? `场均 ${fmtInt(s.tokens_per_archived_scene)} tok` : null} />}
        {isScene && budget && budget.budget !== null && budget.budget !== undefined && (
          <StatCard label="场景预算（5× 基线）" value={`${fmtInt(budget.used)} / ${fmtInt(budget.budget)}`}
                    hint={budget.over_budget ? "已超预算" : `使用率 ${fmtPct(budget.usage_ratio)}`} />
        )}
      </div>

      {isScene && budget && budget.budget !== null && budget.budget !== undefined && (
        <div role="progressbar" aria-label="场景 token 预算使用率" aria-valuemin="0" aria-valuemax={budget.budget} aria-valuenow={budget.used}>
          <MeterBar ratio={budget.usage_ratio} danger={!!budget.over_budget || (budget.usage_ratio || 0) >= 0.9} />
        </div>
      )}

      <div className="card" style={{ padding: "12px 14px", borderRadius: 12, border: "1px solid var(--line, #e5e2dc)" }}>
        <h3 className="text-serif" style={{ margin: "0 0 10px", fontSize: 15 }}>各阶段占比</h3>
        <PhaseBars breakdown={s.phase_breakdown} currency={s.currency} />
      </div>

      {extra && (
        <div className="text-xs" style={{ color: "var(--ink-2)" }}>
          额外成本：失败重试 {fmtMoney(extra.failed_call_cost, s.currency)} · 重复 QC {fmtMoney(extra.repeat_qc_cost, s.currency)} ·
          低分散补候选 {fmtMoney(extra.low_dispersion_topup_cost, s.currency)}（占总成本 {fmtPct(extra.retry_cost_ratio)}）
        </div>
      )}

      <CalibersDetails summary={s} />
    </section>
  );
}

/* ==========================================================
   主视图
   ========================================================== */
function WsCost() {
  const st = useCostState();
  const work = useCostActiveWork();
  const activeId = work ? work.id : null;

  // 跟随当前作品：挂载 / 切书自动加载；已有同项目缓存则不重复请求
  React.useEffect(() => {
    if (!activeId) return;
    const cur = csSnapshot();
    if (cur.projectId !== activeId || (!cur.dashboard && !cur.loading)) costLoad(activeId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  const dash = st.dashboard;
  const s = st.level === "project" ? (dash && dash.summary) : null;
  const currency = (s && s.currency) || (st.summary && st.summary.currency) || "USD";
  const projectId = st.projectId || activeId;
  const empty = s && !s.call_count;

  return (
    <div className="ws-page ws-view q-cost" data-screen-label="cost">
      <header className="rv-head">
        <div className="rv-eyebrow" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {I && I.Coins && <I.Coins size={13} />} 运维 · 结果闭环
        </div>
        <h1 className="rv-title">成本看板</h1>
        <p className="rv-sub">
          每一次候选、重试、批判、补丁与 QC 都计入账本；本页解释钱花在哪、趋势如何、用量走到了哪一步。
          价格为 <code>config/pricing.yaml</code> 快照——占位估算价会标「估算」，接入真实计费后替换单价即可。
        </p>
      </header>

      <div className="rv-toolbar" style={{ flexWrap: "wrap", gap: 8 }}>
        <span className="pill pill-slate text-xs" title={projectId || ""}>
          <span className="pill-dot" />{work ? work.title : projectId || "未选择作品"}
        </span>
        <span style={{ flex: 1 }} />
        {st.level === "project" && COST_WINDOWS.map((n) => (
          <button key={n} className={`btn btn-sm ${st.days === n ? "btn-accent" : "btn-ghost"}`}
                  disabled={st.loading || !projectId} onClick={() => costLoad(projectId, { days: n })}>
            近 {n} 天
          </button>
        ))}
        <button className="btn btn-ghost btn-sm" disabled={st.loading || !projectId}
                onClick={() => costLoad(projectId, st.level === "project" ? { days: st.days } : undefined)}>
          {I && I.Refresh && <I.Refresh size={13} />} {st.loading ? "加载中…" : "刷新"}
        </button>
      </div>

      {st.error && <div className="rv-none" style={{ color: "var(--crimson, #b00)" }}>成本加载出错：{st.error}</div>}

      {!projectId && !st.loading && (
        <div className="rv-none">先在书架选择一部作品——成本看板会自动跟随当前作品。</div>
      )}

      {projectId && st.level !== "project" && <DrillPanel st={st} />}

      {projectId && st.level === "project" && dash && (
        <div style={{ display: "grid", gap: 14 }}>
          {empty ? (
            <div className="rv-none">这部作品还没有任何模型调用——生成一次草稿或跑一次 QC 后，这里会出现成本账。</div>
          ) : (
            <>
              {/* 总览卡片 */}
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10 }}>
                <StatCard label="全书累计成本" value={fmtMoney(s.total_cost, currency)}>
                  <EstimatePill on={s.is_estimate} />
                </StatCard>
                <StatCard label={`近 ${st.days} 天成本`} value={fmtMoney(dash.trend && dash.trend.window_cost, currency)}
                          hint={dash.trend ? `${fmtInt(dash.trend.window_call_count)} 次调用` : null} />
                <StatCard label="累计 Token" value={fmtInt(s.total_tokens)}
                          hint={s.cross_provider ? "跨 provider，以费用为准" : null} />
                <StatCard label="累计调用" value={fmtInt(s.call_count)} />
                <StatCard label="归档章均成本" value={s.cost_per_archived_chapter === null || s.cost_per_archived_chapter === undefined ? "—" : fmtMoney(s.cost_per_archived_chapter, currency)}
                          hint={`已归档 ${fmtInt(s.archived_chapter_count)} / ${fmtInt(s.chapter_count)} 章`} />
                <StatCard label="归档场均 Token" value={s.tokens_per_archived_scene === null || s.tokens_per_archived_scene === undefined ? "—" : fmtInt(s.tokens_per_archived_scene)}
                          hint={`已归档 ${fmtInt(s.archived_scene_count)} / ${fmtInt(s.scene_count)} 场`} />
              </div>

              {/* 趋势 */}
              <section className="card" style={{ padding: "12px 14px", borderRadius: 12, border: "1px solid var(--line, #e5e2dc)" }}>
                <h3 className="text-serif" style={{ margin: "0 0 10px", fontSize: 15 }}>
                  {I && I.Activity && <I.Activity size={14} />} 逐日成本（近 {st.days} 天 · UTC）
                </h3>
                <TrendChart trend={dash.trend} currency={currency} />
              </section>

              {/* 构成：阶段 | 节点 */}
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: 12 }}>
                <section className="card" style={{ padding: "12px 14px", borderRadius: 12, border: "1px solid var(--line, #e5e2dc)" }}>
                  <h3 className="text-serif" style={{ margin: "0 0 10px", fontSize: 15 }}>各阶段占比</h3>
                  <PhaseBars breakdown={s.phase_breakdown} currency={currency} />
                </section>
                <section className="card" style={{ padding: "12px 14px", borderRadius: 12, border: "1px solid var(--line, #e5e2dc)" }}>
                  <h3 className="text-serif" style={{ margin: "0 0 10px", fontSize: 15 }}>成本最高的节点</h3>
                  <NodeBars byNode={dash.by_node} currency={currency} />
                </section>
              </div>

              {/* 模型构成 */}
              <section className="card" style={{ padding: "12px 14px", borderRadius: 12, border: "1px solid var(--line, #e5e2dc)" }}>
                <h3 className="text-serif" style={{ margin: "0 0 10px", fontSize: 15 }}>按模型</h3>
                <ModelTable byModel={dash.by_model} currency={currency} />
              </section>

              {/* 章节构成（可下钻） */}
              <section className="card" style={{ padding: "12px 14px", borderRadius: 12, border: "1px solid var(--line, #e5e2dc)" }}>
                <h3 className="text-serif" style={{ margin: "0 0 10px", fontSize: 15 }}>按章节</h3>
                <ChapterTable byChapter={dash.by_chapter} currency={currency} loading={st.loading}
                              onDrill={(cid) => costLoad(projectId, { chapterId: cid })} />
              </section>

              {/* Top 调用明细 */}
              <section className="card" style={{ padding: "12px 14px", borderRadius: 12, border: "1px solid var(--line, #e5e2dc)" }}>
                <h3 className="text-serif" style={{ margin: "0 0 10px", fontSize: 15 }}>费用最高的调用</h3>
                <TopCallsTable topCalls={dash.top_calls} currency={currency} loading={st.loading}
                               onDrillScene={(sid) => costLoad(projectId, { sceneId: sid })} />
              </section>

              <CalibersDetails summary={s} />
            </>
          )}

          <QuotaSection quota={st.quota} />
        </div>
      )}

      {projectId && st.level === "project" && !dash && st.loading && (
        <div className="rv-none">正在核账…</div>
      )}
    </div>
  );
}

export { WsCost, costLoad, costBack, csSnapshot, useCostState, PHASE_LABEL, QuotaSection, quotaArmed };

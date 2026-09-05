import React from "react";
import { I } from "./icons.jsx";
import { wsKey, WsWorks } from "./ws-works.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { WrDocVersions, WrRecovery } from "./wr-doc-store.jsx";
import { apiGet, apiPost, cancelRunJob, getLatestSceneRunJob } from "./lib/client.js";

/* global React, I */
/* ==========================================================
   AI 起草台 — 真实运行引擎（catalog-sourced scenes）
   ----------------------------------------------------------
   把演示流水线的「预检 → 起草 → 质检 → 裁决 → 归档」落到实处：
   · 上下文：雪花构思（一句话 / 道德前提 / 读者定位 / 角色表）+ 章节卡
   · 起草：后端 scenes run 管线（run/jobs 投递 + 轮询，FE-ALIGN F6）
   · 质检：确定性、可解释——短句率 / 句式重复 / 超长句标红，不装神弄鬼
   · 归档：写入写作器正文文档（wr-doc:sid）+ 字数回写 + 场景卡置 done
   · 持久化：每场的运行结果存 scn-run:sid（按作品隔离），刷新不丢
   ========================================================== */

const SCN_RUN_FIELDS = ["state", "draft", "metrics", "alignment", "verdict", "log", "attempts", "attempt", "at", "words", "gate", "budgetBlock", "authorNote"];
const scnRunKey = (sid) => (wsKey ? wsKey("scn-run:" + sid) : "scn-run:" + sid);
const scnQueueKey = () => (wsKey ? wsKey("scn-queue:v1") : "scn-queue:v1");
const scnDismissKey = () => (wsKey ? wsKey("scn-queue-dismissed:v1") : "scn-queue-dismissed:v1");

const RUN_JOB_POLLING_STATUSES = new Set(["queued", "running", "cancel_requested"]);
const RUN_JOB_CANCELABLE_STATUSES = new Set(["queued", "running"]);
const RUN_JOB_TERMINAL_STATUSES = new Set(["cancelled", "completed", "failed", "blocked"]);
const RUN_JOB_STATUS_LABELS = {
  queued: "排队中",
  running: "运行中",
  cancel_requested: "正在取消",
  cancelled: "已取消",
  completed: "已完成",
  failed: "运行失败",
  blocked: "已阻断",
};

function runJobErrorText(error) {
  const code = error && error.code ? String(error.code) : "REQUEST_FAILED";
  const message = error && error.message ? String(error.message) : "请求失败";
  const details = error && error.details && typeof error.details === "object"
    ? Object.entries(error.details)
      .filter(([, value]) => value !== null && value !== undefined && value !== "")
      .map(([key, value]) => `${key}: ${typeof value === "object" ? JSON.stringify(value) : value}`)
      .join(" · ")
    : "";
  return `${code} · ${message}${details ? ` · ${details}` : ""}`;
}

function isRunJobStateRegression(currentJob, nextJob) {
  if (!currentJob || !nextJob || currentJob.job_id !== nextJob.job_id) return false;
  if (RUN_JOB_TERMINAL_STATUSES.has(currentJob.status)) {
    return currentJob.status !== nextJob.status;
  }
  if (currentJob.status === "cancel_requested") {
    return nextJob.status === "queued" || nextJob.status === "running";
  }
  return currentJob.status === "running" && nextJob.status === "queued";
}

function SceneRunJobControl({
  sceneId,
  observedJob = null,
  onJobChange = null,
  pollIntervalMs = 2000,
  refreshSignal = 0,
}) {
  const [job, setJob] = React.useState(null);
  const [loading, setLoading] = React.useState(Boolean(sceneId));
  const [cancelling, setCancelling] = React.useState(false);
  const [errorText, setErrorText] = React.useState("");
  const jobRef = React.useRef(null);
  const sceneRef = React.useRef(sceneId || "");
  const epochRef = React.useRef(0);
  const requestVersionRef = React.useRef(0);
  const cancelInFlightRef = React.useRef(false);
  const refreshInFlightRef = React.useRef(false);
  const refreshAbortRef = React.useRef(null);
  const cancelAbortRef = React.useRef(null);
  const onJobChangeRef = React.useRef(onJobChange);

  React.useEffect(() => {
    onJobChangeRef.current = onJobChange;
  }, [onJobChange]);

  const publishJob = React.useCallback((nextJob, epoch = epochRef.current) => {
    if (epoch !== epochRef.current) return false;
    const expectedSceneId = sceneRef.current;
    if (
      nextJob
      && nextJob.scene_id
      && expectedSceneId
      && String(nextJob.scene_id) !== String(expectedSceneId)
    ) {
      return false;
    }
    if (isRunJobStateRegression(jobRef.current, nextJob)) return false;
    jobRef.current = nextJob || null;
    setJob(nextJob || null);
    if (onJobChangeRef.current) onJobChangeRef.current(nextJob || null);
    return true;
  }, []);

  const refreshLatest = React.useCallback(async ({ silent = false, epoch = epochRef.current, force = false } = {}) => {
    const targetSceneId = sceneRef.current;
    if (!targetSceneId || epoch !== epochRef.current || (refreshInFlightRef.current && !force)) return null;
    if (force && refreshAbortRef.current) refreshAbortRef.current.abort();
    const requestVersion = requestVersionRef.current + 1;
    requestVersionRef.current = requestVersion;
    const controller = new AbortController();
    refreshAbortRef.current = controller;
    refreshInFlightRef.current = true;
    if (!silent) setLoading(true);
    try {
      const latest = await getLatestSceneRunJob(targetSceneId, { signal: controller.signal });
      if (requestVersion !== requestVersionRef.current) return null;
      if (publishJob(latest, epoch) && !silent) setErrorText("");
      return latest;
    } catch (error) {
      if (epoch !== epochRef.current || requestVersion !== requestVersionRef.current) return null;
      if (error && (error.status === 404 || error.code === "RUN_JOB_NOT_FOUND")) {
        publishJob(null, epoch);
        if (!silent) setErrorText("");
        return null;
      }
      if (!silent) setErrorText(runJobErrorText(error));
      return null;
    } finally {
      if (refreshAbortRef.current === controller) refreshAbortRef.current = null;
      if (epoch === epochRef.current && requestVersion === requestVersionRef.current) {
        refreshInFlightRef.current = false;
        if (!silent) setLoading(false);
      }
    }
  }, [publishJob]);

  React.useEffect(() => {
    const epoch = epochRef.current + 1;
    epochRef.current = epoch;
    requestVersionRef.current += 1;
    if (refreshAbortRef.current) refreshAbortRef.current.abort();
    if (cancelAbortRef.current) cancelAbortRef.current.abort();
    refreshAbortRef.current = null;
    cancelAbortRef.current = null;
    sceneRef.current = sceneId || "";
    refreshInFlightRef.current = false;
    cancelInFlightRef.current = false;
    jobRef.current = null;
    setCancelling(false);
    setErrorText("");
    publishJob(null, epoch);
    if (!sceneId) {
      setLoading(false);
      return () => {
        if (epochRef.current === epoch) epochRef.current += 1;
      };
    }
    void refreshLatest({ epoch });
    return () => {
      if (epochRef.current === epoch) epochRef.current += 1;
      if (refreshAbortRef.current) refreshAbortRef.current.abort();
      if (cancelAbortRef.current) cancelAbortRef.current.abort();
      refreshAbortRef.current = null;
      cancelAbortRef.current = null;
      refreshInFlightRef.current = false;
      cancelInFlightRef.current = false;
    };
  }, [sceneId, publishJob, refreshLatest]);

  React.useEffect(() => {
    if (!observedJob || !sceneId) return;
    // A POST response is newer than any latest request already in flight.
    requestVersionRef.current += 1;
    if (refreshAbortRef.current) refreshAbortRef.current.abort();
    refreshAbortRef.current = null;
    refreshInFlightRef.current = false;
    publishJob(observedJob);
  }, [observedJob, sceneId, publishJob]);

  /* 归档等页面动作后由父组件递增 refreshSignal：终态 job 不轮询，
     不刷新的话横幅会停留在旧暂停点（如 awaiting_candidate_selection）。 */
  React.useEffect(() => {
    if (!sceneId || !refreshSignal) return;
    void refreshLatest({ silent: true, force: true });
  }, [refreshSignal, sceneId, refreshLatest]);

  React.useEffect(() => {
    if (!sceneId || !job || !RUN_JOB_POLLING_STATUSES.has(job.status)) return undefined;
    const timer = window.setInterval(() => {
      void refreshLatest({ silent: true });
    }, Math.max(1, pollIntervalMs));
    return () => window.clearInterval(timer);
  }, [sceneId, job && job.job_id, job && job.status, pollIntervalMs, refreshLatest]);

  const requestCancellation = React.useCallback(async () => {
    const currentJob = job;
    if (
      !currentJob
      || !RUN_JOB_CANCELABLE_STATUSES.has(currentJob.status)
      || cancelInFlightRef.current
    ) {
      return;
    }
    const epoch = epochRef.current;
    requestVersionRef.current += 1;
    if (refreshAbortRef.current) refreshAbortRef.current.abort();
    refreshAbortRef.current = null;
    refreshInFlightRef.current = false;
    cancelInFlightRef.current = true;
    const controller = new AbortController();
    cancelAbortRef.current = controller;
    setCancelling(true);
    setErrorText("");
    try {
      const nextJob = await cancelRunJob(currentJob.job_id, { signal: controller.signal });
      if (epoch !== epochRef.current) return;
      if (
        !jobRef.current
        || jobRef.current.job_id !== currentJob.job_id
      ) {
        await refreshLatest({ silent: true, epoch, force: true });
        return;
      }
      requestVersionRef.current += 1;
      refreshInFlightRef.current = false;
      publishJob(nextJob, epoch);
    } catch (error) {
      if (epoch !== epochRef.current) return;
      if (
        !jobRef.current
        || jobRef.current.job_id !== currentJob.job_id
        || !RUN_JOB_CANCELABLE_STATUSES.has(jobRef.current.status)
      ) {
        await refreshLatest({ silent: true, epoch, force: true });
        return;
      }
      setErrorText(runJobErrorText(error));
      if (error && error.status === 409) {
        await refreshLatest({ silent: true, epoch, force: true });
      }
    } finally {
      if (cancelAbortRef.current === controller) cancelAbortRef.current = null;
      if (epoch === epochRef.current) {
        cancelInFlightRef.current = false;
        setCancelling(false);
      }
    }
  }, [job, publishJob, refreshLatest]);

  if (!sceneId) return null;

  const status = job && job.status ? job.status : "none";
  const statusLabel = loading && !job
    ? "正在恢复运行任务"
    : (RUN_JOB_STATUS_LABELS[status] || (job ? status : "暂无运行任务"));
  const showCancel = Boolean(job && RUN_JOB_CANCELABLE_STATUSES.has(status));
  const showCancelling = Boolean(job && status === "cancel_requested");

  return (
    <div
      className="scn2-decide is-wait"
      data-testid="scene-run-job-control"
      data-status={status}
      data-job-id={(job && job.job_id) || ""}
    >
      <div
        className="scn2-decide-sum"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {RUN_JOB_POLLING_STATUSES.has(status) && <span className="scn2-spin" aria-hidden="true" />}
        <span>
          运行任务 · {statusLabel}
          {job && job.current_step ? ` · ${job.current_step}` : ""}
        </span>
      </div>
      <div className="scn2-decide-acts">
        {errorText && <span role="alert" data-testid="scene-run-cancel-error">{errorText}</span>}
        {showCancel && (
          <button
            type="button"
            className="btn btn-accent btn-sm"
            data-testid="scene-run-cancel-button"
            disabled={cancelling}
            aria-disabled={cancelling ? "true" : "false"}
            onClick={requestCancellation}
          >
            {cancelling ? "正在提交取消…" : "取消运行"}
          </button>
        )}
        {showCancelling && (
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            data-testid="scene-run-cancel-button"
            disabled
            aria-disabled="true"
          >
            取消处理中
          </button>
        )}
      </div>
    </div>
  );
}

function scnRunLoad(sid) {
  try { return JSON.parse(localStorage.getItem(scnRunKey(sid))) || null; } catch (e) { return null; }
}
function scnRunSave(sid, run) {
  try {
    const slim = {}; SCN_RUN_FIELDS.forEach(f => { if (run[f] !== undefined) slim[f] = run[f]; });
    localStorage.setItem(scnRunKey(sid), JSON.stringify(slim));
  } catch (e) {}
}
function scnQueueLoad() {
  try { return JSON.parse(localStorage.getItem(scnQueueKey())) || []; } catch (e) { return []; }
}
function scnQueueSave(sids) {
  try { localStorage.setItem(scnQueueKey(), JSON.stringify(sids.slice(0, 40))); } catch (e) {}
}
/* 移出队列的场：本地队列只是「在办清单」，但队列成员也会从后端
   scene-run-states 恢复（换浏览器/后台跑完的场不该消失）。若不记下作者的移出意图，
   下次进页面这一场又会被恢复回来——删除就成了假动作。这里按作品持久化移出名单，
   重新入列时销名。 */
function scnQueueDismissLoad() {
  try {
    const raw = JSON.parse(localStorage.getItem(scnDismissKey()));
    return Array.isArray(raw) ? raw : [];
  } catch (e) { return []; }
}
const SCN_DISMISS_CAP = 200;
function scnQueueDismissSave(sids) {
  /* 留**最近**的 200 条。新移出的场追加在尾部，所以 slice(0, 200) 恰好把它们整批丢掉：
     名单一满，「移出」就退化成假动作 —— 界面上那一场消失了，下次进页面
     scnBackendQueueSids 的恢复过滤名单里根本没有它，它又原样回到队列里。
     这正是这份名单当初被引入要防的那种「删除是假动作」。 */
  const kept = (sids || []).slice(-SCN_DISMISS_CAP);
  try { localStorage.setItem(scnDismissKey(), JSON.stringify(kept)); } catch (e) {}
  return kept;
}
function scnQueueDismissAdd(sids) {
  const next = scnQueueDismissLoad();
  (sids || []).forEach((sid) => { if (sid && !next.includes(sid)) next.push(sid); });
  /* 返回真正落盘的那份，而不是截断前的 next —— 调用方拿它当「现在的移出名单」用。 */
  return scnQueueDismissSave(next);
}
function scnQueueDismissClear(sids) {
  const drop = new Set(sids || []);
  if (!drop.size) return scnQueueDismissLoad();
  const next = scnQueueDismissLoad().filter((sid) => !drop.has(sid));
  return scnQueueDismissSave(next);
}

/* ---- 确定性质检：可解释、可复算 ---- */
function scnSentencesOf(text) {
  return text.split(/(?<=[。！？；…])/).map(s => s.trim()).filter(s => s.length > 1);
}
/* ---- 质检阈值：从 Tweaks 面板读，改动对已生成稿实时重算 ---- */
let scnQcThresholds = {};

function scnSetQcThresholds(value) {
  scnQcThresholds = { ...(value || {}) };
}

function scnQcTh() {
  const t = scnQcThresholds;
  return { short: t.short || 55, repeat: t.repeat || 30, long: t.long || 64 };
}

function scnQC(paras, reactive) {
  const th = scnQcTh();
  const all = paras.map(p => p.text).join("");
  const sents = paras.flatMap(p => scnSentencesOf(p.text));
  const n = sents.length || 1;
  const shortRate = Math.round(100 * sents.filter(s => s.length <= 20).length / n);
  const openers = {};
  sents.forEach(s => { const k = s.slice(0, 2); openers[k] = (openers[k] || 0) + 1; });
  /* 句式重复：同一起手出现 ≥ 3 次才计（限知视角里「她」起句是正常的） */
  const repeated = Object.values(openers).filter(c => c >= 3).reduce((a, c) => a + c, 0);
  const repeatRate = Math.round(100 * repeated / n);
  const longs = sents.filter(s => s.length > th.long);

  // 把风险句标进段落 parts（写作台同款高亮）
  const risks = [];
  const draft = paras.map(p => {
    const parts = [];
    let rest = p.text;
    scnSentencesOf(p.text).forEach(s => {
      const at = rest.indexOf(s);
      if (at < 0) return;
      const isLong = s.length > th.long;
      const isRep = openers[s.slice(0, 2)] > 2;
      if (isLong || isRep) {
        if (at > 0) parts.push({ text: rest.slice(0, at) });
        const tip = isLong ? `超长句（${s.length} 字 > 阈值 ${th.long}）：考虑拆成两到三句` : `句首「${s.slice(0, 2)}」重复 ${openers[s.slice(0, 2)]} 次：换个起手`;
        parts.push({ risk: isLong ? "pace" : "repeat", sev: isLong ? "mid" : "low", text: s, tip });
        risks.push({ sev: isLong ? "mid" : "low" });
        rest = rest.slice(at + s.length);
      }
    });
    if (rest) parts.push({ text: rest });
    return { id: p.id, beat: p.beat, parts: parts.length ? parts : [{ text: p.text }] };
  });

  const metrics = [
    { label: "短句率",   pct: shortRate,  target: th.short,  val: shortRate + "%", tone: shortRate >= th.short ? "ok" : "warn" },
    { label: "句式重复", pct: repeatRate, target: th.repeat, val: repeatRate + "%", tone: repeatRate <= th.repeat ? "ok" : "warn" },
    { label: "超长句",   pct: Math.min(100, longs.length * 20), target: 20, val: longs.length + " 句", tone: longs.length <= 1 ? "ok" : "warn" },
  ];
  const beats = reactive ? ["goal", "conflict", "exit"] : ["goal", "conflict", "setback", "exit"];
  const noteOf = reactive
    ? { goal: "反应拍", conflict: "两难拍", exit: "决定拍" }
    : { goal: "目标拍", conflict: "冲突拍", setback: "挫败拍", exit: "出口拍" };
  const alignment = beats.map(b => {
    const p = paras.find(x => x.beat === b);
    return { beat: b, para: p ? p.id : null, status: p ? "ok" : "pend", note: p ? `${noteOf[b]}落在 ${p.id}` : `模型未标注${noteOf[b]}` };
  });
  const alignOk = alignment.filter(a => a.status === "ok").length;
  const warns = metrics.filter(m => m.tone === "warn").length + (risks.length ? 1 : 0);
  const words = all.replace(/\s/g, "").length;
  return {
    draft, metrics, alignment, words,
    verdict: {
      qc: warns ? "通过 · 有风险" : "通过",
      risks: risks.length ? `${risks.length} 处风险句` : "无风险句",
      align: `戏剧卡 ${alignOk}/${beats.length} 对齐`,
      words,
    },
  };
}

/* ---- 作者可见状态门（Wave 2 · 治理 §5.3/§5.4）----
   从 workbench/status 的 author_state 投影提取「无法继续 vs 有稿建议修改」：
   · hard_blocked（verified Q0/Q1）→ 不可归档，正文保留可接管
   · quality_warning（Q2/Q3）→ 有稿可归档，警告随行
   gate 随运行记录持久化，裁决条据此分开展示、归档前先拦。 ---- */
function scnGateFrom(src) {
  const a = src && src.author_state;
  if (!a || typeof a !== "object") return null;
  return {
    authorState: a.author_state || null,
    blocking: Array.isArray(a.blocking_findings) ? a.blocking_findings : [],
    warnings: Array.isArray(a.quality_warnings) ? a.quality_warnings : [],
    recommended: Array.isArray(a.recommended_actions) ? a.recommended_actions : [],
    canArchive: a.can_archive !== false,
  };
}

function scnGateLog(gate, tm) {
  if (!gate) return null;
  if (gate.authorState === "hard_blocked") {
    const keys = gate.blocking.map(f => f.issue_key || f.kind).filter(Boolean).join("、");
    return { t: tm, who: "pipeline", text: `无法继续：存在已证实的硬问题（Q0/Q1${keys ? "：" + keys : ""}）——正文已保留，处理后可续跑；此稿暂不可归档` };
  }
  if (gate.authorState === "quality_warning") {
    return { t: tm, who: "pipeline", text: `已有稿，建议修改：${gate.warnings.length} 条质量建议（Q2/Q3）随稿附上——可直接采纳归档，也可按建议改后重跑` };
  }
  return null;
}

/* ---- 完整一跑：后端 scenes run 管线（FE-ALIGN F6）----
   投递 run job（POST run/jobs）→ 轮询 run-jobs/{id} → workbench 取产出
   → 本地确定性复检。失败/阻塞给明确引导（执行契约缺字段 / LLM 未启用 /
   预检不过），不装假进度。提示词由后端 config/prompts.yaml 组装。 */
function scnFriendly(e) {
  const code = (e && e.code) || "";
  const msg = (e && e.message) || String(e || "");
  if (code === "SCENE_EXECUTION_CONTRACT_BLOCKED") {
    const miss = (((e && e.details) || {}).missing_fields || []).join("、");
    return new Error(`这一场的执行契约还缺关键字段${miss ? `（${miss}）` : ""}——先在章节编排把场景卡补全，或走「构思 → 物化」主路径生成完整场景卡。`);
  }
  if (code === "VOICE_PROFILE_MISSING" || code === "RELATION_PROFILE_MISSING") {
    // Fix C：缺声线/关系卡现可一键补齐最小卡解阻（scnCreateCards → /preflight/create-cards）
    const what = code === "VOICE_PROFILE_MISSING" ? "POV 声线卡" : "同场角色关系卡";
    const err = new Error(`这一场缺少可用的${what}，暂不能起草——可点「补齐声线卡并重试」一键生成后自动续跑，或在声线/关系工作台细化。`);
    err.code = code;
    err.canCreateCards = true; // 起草台据此在阻断态显示「补齐声线卡并重试」按钮
    return err;
  }
  if (/LLM/i.test(code) || /llm|provider|api.?key/i.test(msg)) {
    return new Error("AI 起草需要可用的 LLM：请到「系统设置 → 模型与接入」配置并启用后重试。原始信息：" + msg);
  }
  return new Error("起草失败：" + msg);
}

/* Fix C：一键补齐当前场景缺失的最小 voice/relation 卡(active)，解阻 run 预检。
   返回 { created, run_preflight }。这是 create_minimal_voice_card 预检动作的真实执行入口。 */
async function scnCreateCards(sid) { // eslint-disable-line no-unused-vars
  const sceneId = WsCatalog && WsCatalog.__backendSceneId ? await WsCatalog.__backendSceneId(sid) : null;
  if (!sceneId) throw new Error("这一场还没同步到后端目录——稍候片刻或刷新后重试。");
  return apiPost(`/api/v1/scenes/${sceneId}/preflight/create-cards`, {});
}

/* 一份质检摘要里的改写指令条目。后端 workbench 已把 rewrite_brief 摊平成字符串列表
   （api/routes/scenes.py `_extract_rewrite_brief`）；qc-reports 明细路径还会带原始
   rewrite_brief_json 条目（{instruction} / {carry_note_text}），同样按后端规则取字段，
   不让对象条目拼成 "[object Object]"。 */
function scnRewriteBriefEntries(report) {
  if (!report || typeof report !== "object" || !Array.isArray(report.rewrite_brief)) return [];
  return report.rewrite_brief
    .map(entry => {
      if (typeof entry === "string") return entry.trim();
      if (entry && typeof entry === "object") return String(entry.instruction || entry.carry_note_text || "").trim();
      return "";
    })
    .filter(Boolean);
}

function scnRewriteBriefFrom(src) {
  const wb = src && typeof src === "object" ? src : {};
  /* GET /scenes/{id}/workbench 以 hard_qc_summary / soft_qc_summary 透出最近一次硬/软质检
     （api/routes/scenes.py `_serialize_qc_summary`）。此前这里只读 hard_qc / soft_qc /
     latest_qc——那是早期契约名，run-jobs 视图的 latest_qc 也不带 rewrite_brief——于是质检
     给出的具体改法从未落到运行记录，「按硬问题重写」只能退到 issue_key 拼接。
     顺序：硬质检先于软质检（硬是阻断级重写，软只是修补建议）；同类里服务端键名优先，
     旧键名仅兜底。 */
  const reports = [
    wb.hard_qc_summary, wb.hard_qc,
    wb.soft_qc_summary, wb.soft_qc,
    wb.latest_qc,
  ];
  for (const report of reports) {
    const brief = scnRewriteBriefEntries(report);
    if (brief.length) return brief.join("；");
  }
  const projection = scnGateFrom(src);
  return ((projection && projection.blocking) || [])
    .map(f => f.human_readable_reason || f.message || f.issue_key || f.kind)
    .filter(Boolean)
    .join("；");
}

const SCN_LIFECYCLE_BUDGET_CODES = new Set([
  "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
  "LLM_BUSINESS_ATTEMPT_BUDGET_EXHAUSTED",
  "LLM_PROVIDER_ATTEMPT_BUDGET_EXHAUSTED",
]);

function scnBudgetBlock(job, workbench) {
  const code = String((job && job.error_code) || "");
  if (!SCN_LIFECYCLE_BUDGET_CODES.has(code)) return null;
  const lifecycle = (workbench && workbench.scene_run_state && workbench.scene_run_state.lifecycle_budget) || {};
  let topup;
  let label;
  if (code === "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED") {
    const suggested = Number(lifecycle.recommended_topup_tokens || lifecycle.baseline_tokens || 6400);
    topup = { extra_tokens: Math.max(1, Math.trunc(suggested)) };
    label = "本场 token 生命周期预算已到派发边界";
  } else if (code === "LLM_BUSINESS_ATTEMPT_BUDGET_EXHAUSTED") {
    topup = { extra_attempts: 1 };
    label = "本场业务尝试预算已用完";
  } else {
    topup = { extra_provider_attempts: 1 };
    label = "本场 provider 尝试预算已用完";
  }
  return {
    code,
    label,
    message: String((job && job.error_text) || "生命周期预算耗尽；已有正文已保留"),
    currentStep: String((job && job.current_step) || "blocked"),
    lifecycle,
    topup,
  };
}

async function scnTopupBudget(sid, budgetBlock) { // eslint-disable-line no-unused-vars
  const sceneId = WsCatalog && WsCatalog.__backendSceneId ? await WsCatalog.__backendSceneId(sid) : null;
  if (!sceneId) throw new Error("这一场还没同步到后端目录——稍候片刻或刷新后重试。");
  const raw = budgetBlock && budgetBlock.topup && typeof budgetBlock.topup === "object"
    ? budgetBlock.topup
    : {};
  const topup = Object.fromEntries(Object.entries(raw).filter(([, value]) => Number.isInteger(value) && value > 0));
  if (!Object.keys(topup).length) throw new Error("没有可执行的生命周期预算追加量。");
  return apiPost(`/api/v1/scenes/${sceneId}/budget/topup`, {
    ...topup,
    reason: "作者在起草台确认追加生命周期预算并从持久化检查点继续",
  });
}

function scnRunUiAbortError() {
  const error = new Error("scene run UI tracking stopped");
  error.code = "SCENE_RUN_UI_ABORTED";
  return error;
}

function scnThrowIfAborted(signal) {
  if (signal && signal.aborted) throw scnRunUiAbortError();
}

function scnPollDelay(delayMs, signal) {
  scnThrowIfAborted(signal);
  if (!signal) return new Promise(resolve => setTimeout(resolve, delayMs));
  return new Promise((resolve, reject) => {
    const finish = () => {
      signal.removeEventListener("abort", abort);
      resolve();
    };
    const abort = () => {
      clearTimeout(timer);
      reject(scnRunUiAbortError());
    };
    const timer = setTimeout(finish, delayMs);
    signal.addEventListener("abort", abort, { once: true });
  });
}

async function scnRun(item, note, prevText, lifecycle = {}) { // eslint-disable-line no-unused-vars
  const signal = lifecycle && lifecycle.signal;
  const trackedGet = (path) => signal ? apiGet(path, { signal }) : apiGet(path);
  const trackedPost = (path, body) => signal ? apiPost(path, body, { signal }) : apiPost(path, body);
  scnThrowIfAborted(signal);
  const sceneId = WsCatalog && WsCatalog.__backendSceneId
    ? await WsCatalog.__backendSceneId(item.sid)
    : null;
  scnThrowIfAborted(signal);
  if (!sceneId) throw new Error("这一场还没同步到后端目录——稍候片刻或刷新后重试。");
  const t0 = Date.now();
  let job;
  // G3：作者改写指令随任务下发（后端注入风格生成阶段的提示词）
  // 起草台是作者在场的交互式工作流：严格模式把 Q2 建议停在可采纳态，
  // 由“采纳并归档”留下明确接受记录；无 Q2 时后端仍可按契约自动完成。
  const body = { run_policy: (lifecycle && lifecycle.runPolicy) || "strict" };
  const authorNote = note == null ? "" : String(note).trim();
  if (Array.from(authorNote).length > 2000) {
    const error = new Error("作者改写指令不能超过 2000 个字符，请精简后重试；系统没有截断或提交这段指令。");
    error.code = "AUTHOR_NOTE_TOO_LONG";
    throw error;
  }
  if (authorNote) body.author_note = authorNote;
  if (lifecycle && lifecycle.resumeBudget === true) body.resume_budget = true;
  try {
    job = await trackedPost(`/api/v1/scenes/${sceneId}/run/jobs`, body);
  } catch (e) {
    scnThrowIfAborted(signal);
    throw scnFriendly(e);
  }
  scnThrowIfAborted(signal);
  try {
    if (lifecycle && typeof lifecycle.onJobCreated === "function") {
      lifecycle.onJobCreated(job, sceneId);
    }
  } catch (e) {}
  const TERMINAL = ["completed", "blocked", "failed", "cancelled"];
  let last = job;
  const deadline = Date.now() + 5 * 60 * 1000;
  while (!TERMINAL.includes(last.status)) {
    if (Date.now() > deadline) throw new Error("起草超时（5 分钟）——后台任务可能仍在运行，稍后可在质检台查看产出。");
    await scnPollDelay(2000, signal);
    scnThrowIfAborted(signal);
    try {
      last = await trackedGet(`/api/v1/run-jobs/${job.job_id}`);
    } catch (e) {
      scnThrowIfAborted(signal);
    }
  }
  scnThrowIfAborted(signal);
  /* 终态后先看产出：需人工审阅的 blocked 也可能已有草稿，照实呈现 */
  let wb = null;
  try { wb = await trackedGet(`/api/v1/scenes/${sceneId}/workbench`); } catch (e) {}
  scnThrowIfAborted(signal);
  const content = (wb && ((wb.final_scene && wb.final_scene.content)
    || (wb.style_draft && wb.style_draft.content)
    || (wb.neutral_draft && wb.neutral_draft.content))) || "";
  const budgetBlock = scnBudgetBlock(last, wb);
  if (!content.trim()) {
    if (budgetBlock) {
      const error = new Error(`${budgetBlock.label}——可显式追加后从持久化检查点继续。`);
      error.code = budgetBlock.code;
      error.budgetBlock = budgetBlock;
      throw error;
    }
    // Fix A：异步任务现透出结构化 missing_fields（与同步 run/full 同源）→ 引导能点名缺哪些字段
    throw scnFriendly({ code: last.error_code || "", message: last.error_text || `任务以「${last.status}」结束且没有产出正文（${last.current_step || "—"}）`, details: { missing_fields: last.missing_fields || [] } });
  }
  const paras = content.split(/\n{2,}|\n/).map((x, i) => ({ id: "p" + (i + 1), beat: null, text: x.trim() })).filter(p => p.text);
  const hit = item.sid && WsCatalog ? WsCatalog.sceneById(item.sid) : null;
  const reactive = ((hit && hit.scene.kind) || item.kind || "").includes("反应");
  const qc = scnQC(paras, reactive);
  const secs = Math.round((Date.now() - t0) / 1000);
  const tm = (off) => new Date(t0 + off * 1000).toTimeString().slice(0, 8);
  const pipeState = wb && wb.scene_run_state ? wb.scene_run_state.scene_status : last.status;
  // Wave 2：提取作者可见状态门（无法继续 vs 有稿建议修改），随运行记录持久化
  qc.gate = scnGateFrom(wb);
  qc.rewriteBrief = scnRewriteBriefFrom(wb);
  qc.authorNote = authorNote;
  // reliable/无警告路径可能已经由后端原子归档。不能把 author_state=archived
  // 的 can_archive=false 误渲染成 Q0/Q1 阻断，也不能再展示待裁决按钮。
  qc.state = pipeState === "archived" ? "archived" : "ready";
  qc.budgetBlock = budgetBlock;
  if (budgetBlock) {
    qc.gate = {
      ...(qc.gate || { authorState: null, blocking: [], warnings: [], recommended: [] }),
      canArchive: false,
      blockReason: "lifecycle_budget",
    };
  }
  qc.log = [
    { t: tm(0), who: "system", text: "已投递后端起草任务（scenes run 管线：预检 → 蓝图 → 起草 → 硬/软双层质检）" },
    note ? { t: tm(0), who: "system", text: "改写指令已随任务下发（注入风格生成阶段，优先级最高）" } : null,
    { t: tm(secs), who: "pipeline", text: `管线结束 · 任务 ${last.status} · 场景状态 ${pipeState} · ${qc.words} 字 · 用时 ${secs}s` },
    budgetBlock
      ? { t: tm(secs), who: "pipeline", text: `${budgetBlock.label}；已有正文与恢复点均已保留，需作者显式追加预算后续跑` }
      : scnGateLog(qc.gate, tm(secs)),
    { t: tm(secs + 1), who: "qc", text: `本地复检：短句率 ${qc.metrics[0].val} · 句式重复 ${qc.metrics[1].val} · ${qc.verdict.risks}` },
  ].filter(Boolean);
  qc.cost = [
    { k: "起草", v: `后端管线 · ${secs}s` },
    { k: "质检", v: "硬/软双层 + 本地复检" },
    { k: "字数", v: String(qc.words), mono: true },
  ];
  return qc;
}

/* ---- 后端水合：本地没有运行记录（换浏览器 / 页面关闭前没取回）时，
   从 scenes workbench 恢复这一场的最新产出为一条可裁决的运行。
   队列/运行记录此前只活在 localStorage，后端 SceneRunState 才是管线真相——
   这是「起草台各自为战」的补缝。目录场景卡已 done 的按已归档呈现。 ---- */
async function scnHydrateFromBackend(sid, { signal, terminalJob } = {}) {
  scnThrowIfAborted(signal);
  const sceneId = WsCatalog && WsCatalog.__backendSceneId ? await WsCatalog.__backendSceneId(sid) : null;
  scnThrowIfAborted(signal);
  if (!sceneId) return null;
  let wb = null;
  try {
    wb = signal
      ? await apiGet(`/api/v1/scenes/${sceneId}/workbench`, { signal })
      : await apiGet(`/api/v1/scenes/${sceneId}/workbench`);
  } catch (e) {
    scnThrowIfAborted(signal);
    return null;
  }
  const content = (wb && ((wb.final_scene && wb.final_scene.content)
    || (wb.style_draft && wb.style_draft.content)
    || (wb.neutral_draft && wb.neutral_draft.content))) || "";
  const budgetBlock = scnBudgetBlock(terminalJob, wb);
  const authorNote = terminalJob && typeof terminalJob.author_note === "string"
    ? terminalJob.author_note
    : "";
  const pipeState = (wb && wb.scene_run_state && wb.scene_run_state.scene_status) || "";
  if (!content.trim()) {
    // Fresh browsers have no local run cache. A budget-blocked job can stop
    // before producing even a neutral draft, but its durable checkpoint and
    // author instruction are still resumable. Return that recovery state
    // instead of erasing the only UI path to an explicit top-up.
    if (!budgetBlock) return null;
    const now = new Date().toTimeString().slice(0, 8);
    return {
      state: "queued",
      progress: 0,
      draft: [],
      metrics: [],
      alignment: [],
      attempts: [{
        n: 1,
        time: "后端恢复",
        result: "等待追加预算",
        tone: "gold",
        note: authorNote ? "原作者指令已从任务恢复" : "持久化检查点可续跑",
      }],
      attempt: 1,
      at: Date.now(),
      words: 0,
      gate: {
        ...(scnGateFrom(wb) || { authorState: null, blocking: [], warnings: [], recommended: [] }),
        canArchive: false,
        blockReason: "lifecycle_budget",
      },
      budgetBlock,
      authorNote,
      error: `${budgetBlock.label}；尚未产出正文，检查点与原作者指令已保留。请追加预算后继续。`,
      log: [{
        t: now,
        who: "pipeline",
        text: `${budgetBlock.label}；尚未产出正文，检查点与原作者指令已从后端恢复`,
      }],
      cost: [],
      recoveredWithoutDraft: true,
      pipeState,
    };
  }
  const paras = content.split(/\n{2,}|\n/).map((x, i) => ({ id: "p" + (i + 1), beat: null, text: x.trim() })).filter(p => p.text);
  if (!paras.length) return null;
  const hit = WsCatalog ? WsCatalog.sceneById(sid) : null;
  const reactive = ((hit && hit.scene && hit.scene.kind) || "").includes("反应");
  const qc = scnQC(paras, reactive);
  const done = !!(hit && hit.scene && hit.scene.state === "done") || pipeState === "archived";
  const now = new Date().toTimeString().slice(0, 8);
  qc.state = done ? "archived" : "ready";
  qc.attempt = 1;
  qc.at = Date.now();
  qc.gate = scnGateFrom(wb);
  qc.rewriteBrief = scnRewriteBriefFrom(wb);
  qc.authorNote = authorNote;
  qc.budgetBlock = budgetBlock;
  if (qc.budgetBlock) {
    qc.gate = {
      ...(qc.gate || { authorState: null, blocking: [], warnings: [], recommended: [] }),
      canArchive: false,
      blockReason: "lifecycle_budget",
    };
  }
  qc.attempts = [{ n: 1, time: "后端恢复", result: done ? "已归档" : "待裁决", tone: done ? "sage" : "gold", note: "从后端管线取回的最新产出" }];
  qc.log = [
    { t: now, who: "system", text: `已从后端恢复这一场的最新产出（场景状态 ${pipeState || "—"}）——运行在别处完成或页面关闭前未取回` },
    scnGateLog(qc.gate, now),
    { t: now, who: "qc", text: `本地复检：短句率 ${qc.metrics[0].val} · 句式重复 ${qc.metrics[1].val} · ${qc.verdict.risks}` },
  ].filter(Boolean);
  qc.cost = [
    { k: "起草", v: "后端管线 · 已恢复" },
    { k: "质检", v: "硬/软双层 + 本地复检" },
    { k: "字数", v: String(qc.words), mono: true },
  ];
  return qc;
}

/* ---- 队列成员的后端派生（贯通轮遗留 ①）：项目内进过管线的场
   （GET /scene-run-states，scene_status 已离开 ready）→ sid 列表。
   队列的 localStorage 从此退化为这份管线真相的读缓存——换浏览器时
   队列成员可恢复，各场产出再经 scnHydrateFromBackend 逐场取回。 ---- */
async function scnBackendQueueSids() {
  const workId = WsWorks ? WsWorks.activeId() : null;
  if (!workId || workId === "__loading__") return [];
  let data = null;
  try { data = await apiGet(`/api/v1/scene-run-states?project_id=${encodeURIComponent(workId)}`); } catch (e) { return []; }
  const items = (data && data.items) || [];
  if (!items.length) return [];
  try {
    if (WsCatalog && !WsCatalog.get().length && WsCatalog.__refresh) await WsCatalog.__refresh(workId);
  } catch (e) {}
  const bySceneId = {};
  try {
    (WsCatalog ? WsCatalog.get() : []).forEach(c => (c.scenes || []).forEach(s => { if (s.backendId) bySceneId[s.backendId] = s.sid; }));
  } catch (e) {}
  // 端点按 updated_at 倒序返回：最近有动静的场排前面
  return items.map(it => bySceneId[it.scene_id]).filter(Boolean);
}

/* ---- 候选终选（Wave 3 · 治理 §5.5）----
   关键场景管线暂停在 awaiting_author_choice：盲化候选（后端 blinded_order
   随机序、默认无分数）→ 作者整稿选择 → resume 从批判修订/QC 续跑到归档。
   终选一次写入：改选须显式 reopen（后端锁定，SELECTION_LOCKED 上抛）。 ---- */
async function scnBackendIdOf(sid) {
  const sceneId = WsCatalog && WsCatalog.__backendSceneId ? await WsCatalog.__backendSceneId(sid) : null;
  if (!sceneId) throw new Error("这一场还没同步到后端目录——稍候片刻或刷新后重试。");
  return sceneId;
}
async function scnCandidates(sid) {
  const sceneId = await scnBackendIdOf(sid);
  return apiGet(`/api/v1/scenes/${sceneId}/style-candidates`);
}
async function scnSelectCandidate(sid, rowId, opts) {
  const sceneId = await scnBackendIdOf(sid);
  return apiPost(`/api/v1/scenes/${sceneId}/style-candidates/${encodeURIComponent(rowId)}/select`, opts || {});
}
async function scnResumeAfterSelection(sid) {
  const sceneId = await scnBackendIdOf(sid);
  return apiPost(`/api/v1/scenes/${sceneId}/resume-after-selection`, {});
}

/* ---- 归档（治理 §5.2 归档单入口）----
   「完成」的真值在后端：POST adopt-current 携带浏览器当前正文和作者稿
   base revision，服务端在同一事务内保存并提升精确修订。成功响应后只吸收
   权威修订到写作器缓存、回写字数、
   目录卡置 done——done 只由服务端 archived 响应映射，不再先本地置位。
   后端拒绝（无稿 NO_VALID_DRAFT / 来源安全 SOURCE_SAFETY_BLOCKED）时
   不动本地任何状态，faithful 返回失败原因。 ---- */
function scnEscape(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function scnDraftHTML(draft) {
  return (draft || []).map(p => "<p>" + scnEscape((p.parts || []).map(x => x.text).join("")) + "</p>").join("");
}
function scnHTMLParas(raw) {
  if (!raw) return [];
  const node = document.createElement("div");
  node.innerHTML = raw;
  const items = [...node.querySelectorAll("p, li")].map(el => (el.textContent || "").trim()).filter(Boolean);
  return items.length ? items : ((node.textContent || "").trim() ? [(node.textContent || "").trim()] : []);
}
function scnAdoptionPreview(sid, draft) {
  const html = scnDraftHTML(draft);
  const key = wsKey ? wsKey("wr-doc:" + sid) : "wr-doc:" + sid;
  let existing = "";
  try { existing = (WrRecovery && WrRecovery.current(sid)) || localStorage.getItem(key) || ""; } catch (e) {}
  const hasReal = existing && existing.replace(/<[^>]+>/g, "").replace(/\s/g, "").length > 0 && !existing.includes("在这里开始写这一场");
  return {
    sid,
    html,
    existing,
    hasReal: !!hasReal,
    diff: WrDocVersions.diff(scnHTMLParas(existing), scnHTMLParas(html)),
  };
}
async function scnPrepareAdoption(sid, draft) {
  try { if (window.WrDocs && window.WrDocs.hydrate) await window.WrDocs.hydrate(sid); } catch (e) {
    throw Object.assign(new Error("无法核对服务器上的作者稿，已停止采用；请检查网络后重试"), {
      code: "AUTHOR_DRAFT_PREFLIGHT_FAILED",
      cause: e,
    });
  }
  return scnAdoptionPreview(sid, draft);
}
async function scnAdoptToDoc(sid, draft, gate, options = {}) {
  if (!sid || !WsCatalog) return { ok: false, reason: "没有场景卡" };
  // Wave 2（治理 §5.4）：只有真实 Q0/Q1 阻断归档——gate 前置拦截给即时反馈，
  // 后端 adopt-current 的 HARD_BLOCKED 409 仍是权威裁决（绕过前端也拦得住）。
  if (gate && gate.canArchive === false) {
    const keys = (gate.blocking || []).map(f => f.issue_key || f.kind).filter(Boolean).join("、");
    return { ok: false, reason: `存在已证实的硬问题（Q0/Q1${keys ? "：" + keys : ""}），暂不能归档——正文已保留，处理或重跑后再采纳` };
  }
  const preview = await scnPrepareAdoption(sid, draft);
  const html = preview.html;
  const text = (draft || []).map(p => p.parts.map(x => x.text).join("")).join("");
  const key = wsKey ? wsKey("wr-doc:" + sid) : "wr-doc:" + sid;
  // API 层也采用安全默认：任何未声明模式的调用，只要检测到作者正文，
  // 都先保存为候选。显式 overwrite 才可能进入覆盖路径，避免未来新增入口
  // 绕过页面对话框后又退回旧的 confirm/直接覆盖行为。
  const requestedMode = options.mode;
  if (requestedMode && !["candidate", "overwrite", "legacy"].includes(requestedMode)) {
    return { ok: false, reason: "未知的采用模式，已停止以保护作者稿" };
  }
  const mode = requestedMode || (preview.hasReal ? "candidate" : "overwrite");
  if (mode === "candidate") {
    const candidate = WrRecovery.createCandidate(sid, html, "AI 起草台候选；未覆盖作者当前正文，也未归档");
    return {
      ok: true,
      archived: false,
      mode: "candidate",
      candidate,
      warning: candidate.durable === false ? "浏览器空间不足，候选仅保留在本次会话，请立即导出" : null,
    };
  }
  if (preview.hasReal && mode === "overwrite" && options.confirmed !== true) {
    return {
      ok: false,
      reason: "需要先查看差异并明确确认覆盖；作者稿没有被改动",
      confirmationRequired: true,
    };
  }
  if (preview.hasReal && mode === "legacy" && !window.confirm("这一场在写作器里已有正文。继续会先自动备份作者稿，再用 AI 稿覆盖并归档。确定继续？")) {
    return { ok: false, reason: "已取消" };
  }
  let authorBackup = null;
  if (preview.hasReal) {
    try {
      const currentWorkId = WsWorks ? WsWorks.activeId() : "";
      authorBackup = options.authorBackupId
        ? WrRecovery.list().find(item => (
            item.id === options.authorBackupId
            && item.type === "backup"
            && item.source === "author"
            && item.sid === sid
            && item.workId === currentWorkId
            && item.html === preview.existing
            && item.durable !== false
          )) || null
        : null;
      if (!authorBackup) {
        authorBackup = WrRecovery.createBackup(sid, preview.existing, "AI 稿确认覆盖前自动备份作者正文");
      }
    } catch (error) {
      return { ok: false, reason: (error && error.message) || "作者稿备份失败，已停止覆盖", backupFailed: true };
    }
  }
  // 1) 后端归档单入口：确切 HTML + 作者稿 revision + 当前 FinalScene 指针
  // 在一个事务中完成保存与提升，不再让服务端自行猜测浏览器选中了哪份稿。
  let sceneId = null;
  try { sceneId = WsCatalog.__backendSceneId ? await WsCatalog.__backendSceneId(sid) : null; } catch (e) {}
  if (!sceneId) return { ok: false, reason: "这一场还没同步到后端目录——稍候片刻或刷新后重试" };
  const docs = window.WrDocs;
  const docState = docs && docs.state ? docs.state(sid) : null;
  if (!docs || !docs.acceptCanonical || !docState || !docState.draftId || !Number.isInteger(docState.revision) || docState.revision < 1) {
    return { ok: false, reason: "无法取得服务器作者稿修订，已停止归档以避免正文错位" };
  }
  let adoption = null;
  try {
    adoption = await apiPost(`/api/v1/scenes/${sceneId}/adopt-current`, {
      accepted_warning_codes: Array.isArray(options.acceptedWarningCodes)
        ? options.acceptedWarningCodes
        : [],
      exact_author_draft: {
        draft_id: docState.draftId,
        base_revision_no: docState.revision,
        expected_current_final_scene_row_id: docState.currentFinalSceneRowId || null,
        content: html,
      },
    });
  } catch (e) {
    const code = (e && e.code) || "";
    const msg = (e && e.message) || String(e || "");
    return { ok: false, reason: `后端归档未通过（${code || "网络错误"}）：${msg}`, error: e, authorBackup };
  }
  // 2) 服务端已经保存并归档同一修订；这里只吸收回包，不再 PATCH 新修订。
  let cacheWarning = null;
  try {
    const synced = docs.acceptCanonical(sid, html, adoption);
    if (synced && synced.localDurable === false) cacheWarning = "正文已安全归档到服务器，但浏览器缓存写入失败；刷新后可从服务器恢复";
  } catch (e) {
    cacheWarning = "正文已安全归档到服务器，但本地状态同步失败；请刷新页面从服务器恢复";
  }
  const hit = WsCatalog.sceneById(sid);
  const prev = hit && typeof hit.scene.words === "number" ? hit.scene.words : 0;
  const count = text.replace(/\s/g, "").length;
  try { WsCatalog.recordSceneWords(sid, count, prev); } catch (e) {}
  try {
    WsCatalog.set(WsCatalog.get().map(c => ({
      ...c, scenes: (c.scenes || []).map(s => s.sid === sid ? { ...s, state: "done" } : s),
    })));
  } catch (e) {}
  // 3) 治理设计项 4：归档后重新拉服务端状态（起草台运行记录与管线真相收敛）
  try {
    const status = await apiGet(`/api/v1/scenes/${sceneId}/status`);
    return { ok: true, archived: true, words: count, authorBackup, cacheWarning, contentHash: adoption && adoption.content_hash, serverStatus: (status && status.scene_status) || "archived", authorState: status && status.author_state };
  } catch (e) {
    return { ok: true, archived: true, words: count, authorBackup, cacheWarning, contentHash: adoption && adoption.content_hash, serverStatus: "archived" };
  }
}

/* 已生成稿件的实时重算：阈值改动后，风险标记 / 指标 / 判词跟着变 */
function scnReQC(draft, kind) {
  try {
    const paras = (draft || []).map(p => ({ id: p.id, beat: p.beat, text: p.parts.map(x => x.text).join("") }));
    if (!paras.length) return null;
    return scnQC(paras, (kind || "").includes("反应"));
  } catch (e) { return null; }
}

/* ---- 选场器数据：目录里可入列的场 ---- */
function scnPickList(queuedSids) {
  const q = new Set(queuedSids || []);
  try {
    return (WsCatalog ? WsCatalog.get() : []).map(c => ({
      id: c.id, n: c.n, title: c.title,
      scenes: (c.scenes || []).map(s => ({
        sid: s.sid, title: s.title, kind: s.kind, state: s.state,
        ready: !!((s.goal || "").trim() && !(s.goal || "").includes("待规划")),
        queued: q.has(s.sid),
        hasDraft: !!scnRunLoad(s.sid),
      })),
    })).filter(c => c.scenes.length);
  } catch (e) { return []; }
}

/* 场景工作台只通过显式 ESM 导出连接，不再写入 window 全局命名空间。 */
export { SceneRunJobControl, scnRun, scnCreateCards, scnTopupBudget, scnAdoptToDoc, scnAdoptionPreview, scnPrepareAdoption, scnPickList, scnRunLoad, scnRunSave, scnQueueLoad, scnQueueSave, scnQueueDismissLoad, scnQueueDismissAdd, scnQueueDismissClear, scnQC, scnReQC, scnSetQcThresholds, scnHydrateFromBackend, scnBackendQueueSids, scnGateFrom, scnRewriteBriefFrom, scnCandidates, scnSelectCandidate, scnResumeAfterSelection };

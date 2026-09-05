import React from "react";
import ReactDOM from "react-dom";
import { I } from "./icons.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { SceneRunJobControl, scnQueueLoad, scnRunLoad, scnQueueSave, scnQueueDismissLoad, scnQueueDismissAdd, scnQueueDismissClear, scnReQC, scnSetQcThresholds, scnRun, scnCreateCards, scnTopupBudget, scnRunSave, scnAdoptToDoc, scnPrepareAdoption, scnPickList, scnHydrateFromBackend, scnBackendQueueSids, scnCandidates, scnSelectCandidate, scnResumeAfterSelection } from "./ws-scene-run.jsx";
import { ContentSafetyReviewDialog, contentSafetyReviewFromError } from "./wr-content-safety-review.jsx";
import { UndoToast, useUndoToast } from "./ws-undo-toast.jsx";
import { WsWorks } from "./ws-works.jsx";
import { SceneTweaks } from "./ws-shell-tweaks.jsx";
import { setViewIntentTargetReady } from "./ws-view-intents.js";

/* global React, I */
const { useState: useSt8, useEffect: useEf8, useRef: useRef8, useMemo: useMemo8 } = React;
const scnPortal = ReactDOM.createPortal;

/* ==========================================================
   场景工作台 — Scene Workbench  (refactor v2)
   One-screen closed loop:  流程在上 · 正文居中 · 裁决在下
   ┌──────────┬─────────────────────────────────┬──────────┐
   │ 运行队列  │  Pipeline strip (live)          │ 证据      │
   │          │  ─────────────────────────       │ · 质检    │
   │          │  正文 / 起草过程 (reading stage) │ · 戏剧卡  │
   │          │  ─────────────────────────       │ · 开销    │
   │          │  裁决条 (accept / send-back / dx)│ · 归档    │
   └──────────┴─────────────────────────────────┴──────────┘
   The centre re-skins by the picked scene's state:
     queued → 预检   running → 起草直播   ready → 复核裁决   archived → 定稿
   ========================================================== */

const SC_STAGES = [
  { id: "preflight", name: "预检" },
  { id: "draft",     name: "起草" },
  { id: "qc",        name: "质检" },
  { id: "rewrite",   name: "二改" },
  { id: "verify",    name: "校核" },
  { id: "archive",   name: "归档" },
];

/* ---- Drama-beat tints, used to colour the inline highlights ---- */
const BEAT_META = {
  goal:     { label: "Goal · 目标",     tone: "sage"    },
  conflict: { label: "Conflict · 冲突", tone: "crimson" },
  setback:  { label: "Setback · 挫折",  tone: "gold"    },
  exit:     { label: "Exit · 出口",     tone: "slate"   },
};

/* ===================== Scene content ===================== */


const STATE_LABEL = { running: "运行中", queued: "排队", ready: "待复核", archived: "已归档" };
const STATE_TONE  = { running: "crimson", queued: "slate", ready: "gold", archived: "sage" };

/* 「CH 08 · SC 03」→ 历史场景 id ch08s3（跳写作台深改用） */
function scnSidOf(n) {
  const m = /CH\s*(\d+)[^]*SC\s*(\d+)/.exec(n || "");
  return m ? `ch${m[1]}s${parseInt(m[2], 10)}` : null;
}

/* 章节编排「交给 AI」入列：从目录场景卡派生一条队列项 */
function scnFromCatalog(sid) {
  if (!sid || !WsCatalog) return null;
  const hit = WsCatalog.sceneById(sid);
  if (!hit) return null;
  const { chapter: c, scene: s, index } = hit;
  const beats = [];
  if (s.goal) beats.push({ beat: "goal", text: s.goal });
  if (s.obstacle) beats.push({ beat: "conflict", text: s.obstacle });
  if (s.turn) beats.push({ beat: "exit", text: s.turn });
  return {
    id: "cq-" + sid, sid, fromCard: true,
    n: `CH ${c.n} · SC ${String(index + 1).padStart(2, "0")}`,
    title: s.title, kind: (s.kind || "主动") + "场景",
    state: "queued", progress: 0, stageIdx: 0, attempt: 0,
    targetWords: "1500–1800",
    brief: beats, draft: [], metrics: [], alignment: [], cost: [], log: [],
  };
}

/* ============================ Main ============================ */

function WsSceneBoard({ go, t }) {
  const tw = t || {};
  /* 初始化：持久化队列（按作品）+ 编排送来的入列请求 + 每场已持久化的运行结果 */
  const initRef = useRef8(null);
  if (!initRef.current) {
    const sids = (scnQueueLoad ? scnQueueLoad() : []).slice();
    const items = sids.map(sid => scnFromCatalog(sid)).filter(Boolean);
    const runs0 = {};
    items.forEach(it => { const r = scnRunLoad ? scnRunLoad(it.sid) : null; if (r) runs0[it.id] = r; });
    if (scnQueueSave) scnQueueSave(items.map(i => i.sid));
    initRef.current = { items, runs0 };
  }
  const [extras, setExtras] = useSt8(initRef.current.items);
  const [runs, setRuns] = useSt8(initRef.current.runs0);
  const [picker, setPicker] = useSt8(false);
  const [qSelMode, setQSelMode] = useSt8(false);          // 队列多选态（会话内，不持久化）
  const [qSel, setQSel] = useSt8(() => new Set());
  const { toast, show: showNotice, clear: clearNotice } = useUndoToast();
  const runSeq = useRef8({});
  const runAbortControllers = useRef8({});
  const runProgressTimers = useRef8({});
  const [pickedId, setPicked] = useSt8(() => (initRef.current.items[0] ? initRef.current.items[0].id : null));
  const [activeBeat, setActiveBeat] = useSt8(null);     // highlighted beat in draft
  const [logOpen, setLogOpen] = useSt8(false);
  const [compare, setCompare] = useSt8(null);           // attempt object being compared
  const [adoptionDecision, setAdoptionDecision] = useSt8(null); // 作者稿存在时的安全采用决策
  const [adoptionBusy, setAdoptionBusy] = useSt8("");
  const [adoptionMessage, setAdoptionMessage] = useSt8("");
  const [contentSafetyReview, setContentSafetyReview] = useSt8(null);
  const [contentSafetyError, setContentSafetyError] = useSt8("");
  const [dxDone, setDxDone] = useSt8(() => ({ ...(window.__sceneDxDone || {}) }));  // scene n → adopted-issue count (深改回传)
  const pickedIdRef = useRef8(pickedId);
  pickedIdRef.current = pickedId;
  const archiveUiEpoch = useRef8(0);
  const archivePreviewLocks = useRef8(new Set());
  const archiveCommitLocks = useRef8(new Set());
  const [archivePreviewBusy, setArchivePreviewBusy] = useSt8(false);
  const scenePageMounted = useRef8(true);

  useEf8(() => {
    scenePageMounted.current = true;
    return () => {
      scenePageMounted.current = false;
      archiveUiEpoch.current += 1;
    };
  }, []);

  /* 场景切换或页面卸载只停止前端跟踪，不伪造后端取消；回到场景后由 latest 恢复。 */
  useEf8(() => () => {
    const ids = new Set([
      ...Object.keys(runAbortControllers.current),
      ...Object.keys(runProgressTimers.current),
    ]);
    ids.forEach((id) => {
      const controller = runAbortControllers.current[id];
      if (controller) controller.abort();
      const timer = runProgressTimers.current[id];
      if (timer) clearInterval(timer);
      runSeq.current[id] = (runSeq.current[id] || 0) + 1;
    });
    runAbortControllers.current = {};
    runProgressTimers.current = {};
  }, [pickedId]);

  const enqueueSid = (sid) => {
    const it = scnFromCatalog(sid);
    if (!it) return;
    if (scnQueueDismissClear) scnQueueDismissClear([sid]);   // 重新入列 = 撤销之前的移出
    setExtras(x => {
      if (x.some(y => y.id === it.id)) return x;
      const nx = [it, ...x];
      if (scnQueueSave) scnQueueSave(nx.map(i => i.sid));
      return nx;
    });
    setQSel(prev => (prev.size ? new Set([...prev].filter(id => id !== "cq-" + sid)) : prev));
    const r = scnRunLoad ? scnRunLoad(sid) : null;
    if (r) setRuns(m => ({ ...m, ["cq-" + sid]: r }));
    else if (scnHydrateFromBackend) {
      // 本地无记录：尝试从后端 workbench 恢复既有产出（不覆盖期间跑起来的运行）
      scnHydrateFromBackend(sid)
        .then(hr => {
          if (hr && scenePageMounted.current) {
            setRuns(m => (m["cq-" + sid] ? m : { ...m, ["cq-" + sid]: hr }));
            if (scnRunSave) scnRunSave(sid, hr);
          }
        })
        .catch(() => {});
    }
    setPicked("cq-" + sid);
  };

  /* 移出队列：只把这一场从「在办清单」里拿掉——场景卡、已生成的 AI 稿和后端运行记录
     一概不动（重新入列即原样回来，所以这里说「移出」而不是「删除」；要删场景卡请去章节编排）。
     正因为不破坏任何东西，这里不拦一道确认弹窗，而是移完给一条带「撤销」的回执：
     动作即时、后悔成本近乎为零，比「先弹窗问一遍」顺手得多。
     运行中的场仍然拦下：后端任务还在跑，悄悄丢掉跟踪会让作者以为已经停了。 */
  const removeFromQueue = (ids) => {
    const wanted = new Set(ids || []);
    const targets = extras.filter(x => wanted.has(x.id));
    if (!targets.length) return;
    const stateOf = (item) => (runs[item.id] && runs[item.id].state) || item.state || "queued";
    const running = targets.filter(x => stateOf(x) === "running");
    if (running.length) {
      showNotice({ tone: "warn", text: `有 ${running.length} 场正在运行，不能移出队列——请先「中止」或等它跑完。` });
      return;
    }
    const removeIds = new Set(targets.map(x => x.id));
    const removedSids = targets.map(x => x.sid);
    const snapshot = extras;                       // 撤销 = 整体还原这份快照（runs 全程没动，稿还在）
    const prevPicked = pickedIdRef.current;
    if (scnQueueDismissAdd) scnQueueDismissAdd(removedSids);
    const nx = extras.filter(x => !removeIds.has(x.id));
    setExtras(nx);
    if (scnQueueSave) scnQueueSave(nx.map(i => i.sid));
    if (removeIds.has(prevPicked)) setPicked(nx.length ? nx[0].id : null);
    setQSel(prev => (prev.size ? new Set([...prev].filter(id => !removeIds.has(id))) : prev));
    showNotice({
      text: targets.length === 1
        ? `已把「${targets[0].title}」移出队列 · 场景卡与 AI 稿都保留`
        : `已把 ${targets.length} 场移出队列 · 场景卡与 AI 稿都保留`,
      actionLabel: "撤销",
      onAction: () => {
        if (scnQueueDismissClear) scnQueueDismissClear(removedSids);
        setExtras(snapshot);
        if (scnQueueSave) scnQueueSave(snapshot.map(i => i.sid));
        setPicked(prevPicked);
      },
    });
  };

  /* FE 补缝：本地没有 scn-run 记录的入列场，从后端 workbench 恢复运行态——
     换浏览器 / 后台完成的运行不再「消失」；已有本地记录或期间跑起来的不覆盖 */
  useEf8(() => {
    if (!scnHydrateFromBackend) return;
    let alive = true;
    (async () => {
      for (const it of initRef.current.items) {
        if (initRef.current.runs0[it.id]) continue;
        let r = null;
        try { r = await scnHydrateFromBackend(it.sid); } catch (e) {}
        if (!alive) return;
        if (!r) continue;
        setRuns(m => (m[it.id] ? m : { ...m, [it.id]: r }));
        if (scnRunSave) scnRunSave(it.sid, r);
      }
    })();
    return () => { alive = false; };
  }, []);

  /* 队列成员的后端恢复（贯通轮遗留 ①）：进过管线的场（scene-run-states）
     并入队列——本地队列在前、后端恢复在后；localStorage 队列由此退化为
     管线真相的读缓存，换浏览器队列成员不再是空的 */
  useEf8(() => {
    if (!scnBackendQueueSids) return;
    let alive = true;
    (async () => {
      let sids = [];
      try { sids = await scnBackendQueueSids(); } catch (e) {}
      /* 作者移出过的场不再从后端恢复：管线里仍有它的运行记录，但队列是「在办清单」，
         恢复它等于把删除撤销掉。重新入列（加入场景 / 交给 AI）会销名。 */
      const dismissed = new Set(scnQueueDismissLoad ? scnQueueDismissLoad() : []);
      sids = sids.filter(sid => !dismissed.has(sid));
      if (!alive || !sids.length) return;
      const restored = sids.map(sid => scnFromCatalog(sid)).filter(Boolean);
      setExtras(prev => {
        const have = new Set(prev.map(i => i.sid));
        const add = restored.filter(item => !have.has(item.sid));
        if (!add.length) return prev;
        const nx = [...prev, ...add];
        if (scnQueueSave) scnQueueSave(nx.map(i => i.sid));
        return nx;
      });
      /* 空本地队列从后端恢复时必须选中首场，才能挂载 latest 控件。 */
      if (restored.length) setPicked(current => current || restored[0].id);
      /* 新并入的场恢复运行态；已在初始队列里的由上面的水合 effect 负责 */
      const fresh = sids.filter(sid => !initRef.current.items.some(i => i.sid === sid));
      for (const sid of fresh) {
        const id = "cq-" + sid;
        const local = scnRunLoad ? scnRunLoad(sid) : null;
        if (local) { setRuns(m => (m[id] ? m : { ...m, [id]: local })); continue; }
        if (!scnHydrateFromBackend) continue;
        let hr = null;
        try { hr = await scnHydrateFromBackend(sid); } catch (e) {}
        if (!alive) return;
        if (hr) { setRuns(m => (m[id] ? m : { ...m, [id]: hr })); if (scnRunSave) scnRunSave(sid, hr); }
      }
    })();
    return () => { alive = false; };
  }, []);

  useEf8(() => {
    const onDx = (e) => { const d = e.detail || {}; if (d.n) setDxDone(m => ({ ...m, [d.n]: d.count || 0 })); };
    window.addEventListener("ws:scene-deepdesk-done", onDx);
    const onEnq = (e) => {
      const detail = e.detail || {};
      if (Array.isArray(detail.sids)) detail.sids.slice().reverse().forEach(enqueueSid);
      if (detail.sid) enqueueSid(detail.sid);
    };
    window.addEventListener("ws:scene-enqueue", onEnq);
    setViewIntentTargetReady("scene");
    return () => {
      setViewIntentTargetReady("scene", false);
      window.removeEventListener("ws:scene-deepdesk-done", onDx);
      window.removeEventListener("ws:scene-enqueue", onEnq);
    };
  }, []);

  /* 队列：目录来的场叠加运行态 */
  const baseQueue = useMemo8(() => {
    const ext = extras.map(x => {
      const r = runs[x.id];
      return r ? { ...x, state: r.state || "queued", progress: r.state === "running" ? (r.progress || 0) : (r.state === "queued" ? 0 : 1) } : x;
    });
    return ext;
  }, [extras, runs]);
  const sceneOfX = (id) => extras.find(x => x.id === id) || null;
  const selectedCardScene = extras.find(x => x.id === pickedId) || null;
  const [activeBackendScene, setActiveBackendScene] = useSt8(null);
  const [observedRunJob, setObservedRunJob] = useSt8(null);
  const [authoritativeRunJob, setAuthoritativeRunJob] = useSt8(null);
  /* 归档成功后递增：让终态运行任务横幅重取 latest（后端视图已收敛为 archived）。 */
  const [runJobRefreshTick, setRunJobRefreshTick] = useSt8(0);

  /* Task 8：scene 变更时重新解析后端 scene id；旧解析和旧 latest 响应都不得覆盖新场景。 */
  useEf8(() => {
    let alive = true;
    setActiveBackendScene(null);
    setObservedRunJob(null);
    setAuthoritativeRunJob(null);
    const sid = selectedCardScene && selectedCardScene.sid;
    if (!sid || !WsCatalog || !WsCatalog.__backendSceneId) return () => { alive = false; };
    Promise.resolve(WsCatalog.__backendSceneId(sid))
      .then(sceneId => { if (alive) setActiveBackendScene(sceneId ? { sid, sceneId } : null); })
      .catch(() => { if (alive) setActiveBackendScene(null); });
    return () => { alive = false; };
  }, [pickedId, selectedCardScene && selectedCardScene.sid]);
  const activeBackendSceneId = (
    activeBackendScene
    && selectedCardScene
    && activeBackendScene.sid === selectedCardScene.sid
  ) ? activeBackendScene.sceneId : "";

  /* 质检阈值随 Tweaks 即时生效，通过模块接口同步给运行引擎。 */
  scnSetQcThresholds({ short: tw.scnShort || 55, repeat: tw.scnRepeat || 30, long: tw.scnLong || 64 });

  const currentAuthoritativeJob = (
    authoritativeRunJob
    && activeBackendSceneId
    && authoritativeRunJob.sceneId === activeBackendSceneId
  ) ? authoritativeRunJob.job : null;
  const authoritativeStatus = currentAuthoritativeJob && currentAuthoritativeJob.status;

  /* latest 终态负责把切场景时遗留的本地 running 收敛回可继续状态。 */
  useEf8(() => {
    const terminal = ["cancelled", "completed", "failed", "blocked"].includes(authoritativeStatus);
    if (!terminal || !selectedCardScene || !activeBackendSceneId || !currentAuthoritativeJob) return undefined;
    const id = selectedCardScene.id;
    const sid = selectedCardScene.sid;
    const expectedSeq = runSeq.current[id] || 0;
    const controller = new AbortController();
    let alive = true;
    const hasUsableLocalResult = (record) => (
      ["ready", "archived"].includes(record && record.state)
      && Array.isArray(record && record.draft)
      && record.draft.length > 0
    );
    const commit = (produce) => {
      if (!alive || controller.signal.aborted || (runSeq.current[id] || 0) !== expectedSeq) return;
      setRuns(current => {
        if (!alive || controller.signal.aborted || (runSeq.current[id] || 0) !== expectedSeq) return current;
        const next = produce(current[id] || {});
        if (scnRunSave) scnRunSave(sid, next);
        return { ...current, [id]: next };
      });
    };

    if (authoritativeStatus === "completed" || authoritativeStatus === "blocked") {
      commit(previous => hasUsableLocalResult(previous) ? previous : ({
          ...previous,
          state: "queued",
          progress: 0,
          error: authoritativeStatus === "blocked" ? "任务已阻断，正在恢复可审阅产出…" : "任务已完成，正在恢复产出…",
          needsCards: false,
        }));
      (async () => {
        let hydrated = null;
        try { hydrated = await scnHydrateFromBackend(sid, { signal: controller.signal, terminalJob: currentAuthoritativeJob }); } catch (e) {
          if (e && e.code === "SCENE_RUN_UI_ABORTED") return;
        }
        if (hydrated) {
          commit(previous => ({
            ...previous,
            ...hydrated,
            // A no-draft budget checkpoint needs an explicit explanation next
            // to its top-up action. Ordinary recovered manuscripts clear stale
            // errors as before.
            error: hydrated.recoveredWithoutDraft ? hydrated.error : null,
            needsCards: false,
          }));
          return;
        }
        const message = authoritativeStatus === "blocked"
          ? "任务已阻断，尚未取得可审阅草稿，请检查阻断原因后重试"
          : "任务已完成，但暂未取回草稿，请稍后重试";
        commit(previous => hasUsableLocalResult(previous)
          ? previous
          : ({ ...previous, state: "queued", progress: 0, error: message, needsCards: false }));
      })();
    } else {
      const message = authoritativeStatus === "cancelled"
        ? "任务已取消，可重新起草"
        : "任务运行失败，请检查任务详情后重试";
      commit(previous => ({ ...previous, state: "queued", progress: 0, error: message, needsCards: false }));
    }
    return () => {
      alive = false;
      controller.abort();
    };
  }, [activeBackendSceneId, authoritativeStatus, currentAuthoritativeJob && currentAuthoritativeJob.job_id, selectedCardScene && selectedCardScene.id, selectedCardScene && selectedCardScene.sid]);

  const authoritativeQueueState = authoritativeStatus === "queued"
    ? "queued"
    : (["running", "cancel_requested"].includes(authoritativeStatus) ? "running" : null);
  const queue = useMemo8(() => baseQueue.map(item => (
    item.id === pickedId && authoritativeQueueState
      ? { ...item, state: authoritativeQueueState, progress: authoritativeQueueState === "queued" ? 0 : (item.progress || 0.06) }
      : item
  )), [baseQueue, pickedId, authoritativeQueueState]);
  const rawState = queue.find(q => q.id === pickedId)?.state;
  const effState = rawState;
  const renderState = authoritativeStatus === "queued"
    ? "queued"
    : (["running", "cancel_requested"].includes(authoritativeStatus) ? "running" : effState);
  const scene = useMemo8(() => {
    const base = sceneOfX(pickedId);
    if (!base) return null;
    if (base.fromCard) {
      const r = runs[base.id];
      if (!r) return base;
      /* 阈值变动时对已生成稿实时重算质检（风险标记 / 指标 / 判词） */
      const reqc = (r.state === "ready" || r.state === "archived") && r.draft && scnReQC ? scnReQC(r.draft, base.kind) : null;
      const merged = {
        ...base, ...r, ...(reqc || {}),
        stageIdx: r.state === "running" ? 1 : r.state === "ready" ? 4 : r.state === "archived" ? 5 : 0,
        model: "Claude · 实时起草",
        elapsed: r.state === "running" ? "进行中" : "—",
        eta: r.state === "running" ? "片刻" : null,
        attempt: r.attempt || 1,
      };
      if (r.state === "running") merged.runBanner = { t: "起草进行中", s: `Claude · 第 ${r.attempt || 1} 次尝试 · 整稿返回后过质检` };
      return merged;
    }
    const dx = dxDone[base.n];
    return dx != null ? { ...base, dxCount: dx } : base;
  }, [pickedId, dxDone, extras, runs, tw.scnShort, tw.scnRepeat, tw.scnLong]);

  useEf8(() => {
    archiveUiEpoch.current += 1;
    setArchivePreviewBusy(archivePreviewLocks.current.has(pickedId));
    setAdoptionBusy(archiveCommitLocks.current.has(pickedId) ? "archive" : "");
    setActiveBeat(null);
    setLogOpen(rawState === "running" && tw.scnLog !== false);
    setCompare(null);
    setAdoptionDecision(null);
    setAdoptionMessage("");
    setContentSafetyReview(null);
    setContentSafetyError("");
  }, [pickedId]);

  const counts = useMemo8(() => {
    const c = { running: 0, queued: 0, ready: 0, archived: 0 };
    queue.forEach(q => { c[q.state || "queued"]++; });
    return c;
  }, [queue]);

  /* —— 真·运行：起草 / 退回重写（同一条路，带指令） —— */
  const startRun = async (sc, note, options = {}) => {
    if (!sc || !sc.fromCard) return;
    const id = sc.id;
    if (runAbortControllers.current[id]) runAbortControllers.current[id].abort();
    if (runProgressTimers.current[id]) clearInterval(runProgressTimers.current[id]);
    const controller = new AbortController();
    runAbortControllers.current[id] = controller;
    const token = (runSeq.current[id] || 0) + 1; runSeq.current[id] = token;
    const normalizedNote = note == null ? "" : String(note).trim();
    if (Array.from(normalizedNote).length > 2000) {
      setRuns(m => ({ ...m, [id]: { ...(m[id] || {}), error: "作者改写指令不能超过 2000 个字符，请精简后重试；内容没有被静默截断。" } }));
      return;
    }
    const attempt = ((runs[id] && runs[id].attempt) || 0) + 1;
    const prevText = runs[id] && runs[id].draft ? runs[id].draft.map(p => p.parts.map(x => x.text).join("")).join("\n") : "";
    const t0 = new Date().toTimeString().slice(0, 8);
    setRuns(m => ({ ...m, [id]: { ...(m[id] || {}), state: "running", progress: 0.06, attempt, authorNote: normalizedNote, error: null, needsCards: false, budgetBlock: null,
      log: [{ t: t0, who: "system", text: `预检通过 · 第 ${attempt} 次尝试${normalizedNote ? " · 改写指令已附" : ""}` }, { t: t0, who: "sonnet", text: "起草进行中……整稿返回后过质检" }] } }));
    const tick = setInterval(() => setRuns(m => {
      const cur = m[id];
      if (!cur || cur.state !== "running") { clearInterval(tick); return m; }
      return { ...m, [id]: { ...cur, progress: Math.min(0.92, (cur.progress || 0) + 0.045) } };
    }), 700);
    runProgressTimers.current[id] = tick;
    const stopProgress = () => {
      clearInterval(tick);
      if (runProgressTimers.current[id] === tick) delete runProgressTimers.current[id];
    };
    try {
      const res = await scnRun(sc, normalizedNote, normalizedNote ? prevText : "", {
        signal: controller.signal,
        resumeBudget: options.resumeBudget === true,
        onJobCreated: (job, sceneId) => setObservedRunJob({ job, sceneId }),
      });
      stopProgress();
      if (runSeq.current[id] !== token) return;
      setRuns(m => {
        const stamp = new Date().toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
        const prevAtt = ((m[id] && m[id].attempts) || []).map(a => a.time && a.time.startsWith("本次") ? { ...a, time: stamp, result: "退回重写", tone: "slate" } : a);
        const attempts = [{ n: attempt, time: "本次 · 待裁决", result: "待裁决", tone: "gold", note: normalizedNote ? "按指令改写" : "初稿", cmp: normalizedNote ? { verdict: "作者改写指令：" + normalizedNote } : undefined }, ...prevAtt].slice(0, 8);
        const nr = { ...(m[id] || {}), ...res, authorNote: normalizedNote, state: res.state === "archived" ? "archived" : "ready", progress: 1, attempt, attempts, at: Date.now() };
        if (scnRunSave) scnRunSave(sc.sid, nr);
        return { ...m, [id]: nr };
      });
    } catch (e) {
      stopProgress();
      if (runSeq.current[id] !== token) return;
      if (e && e.code === "SCENE_RUN_UI_ABORTED") return;
      // Fix C：缺声线/关系卡的阻断带 canCreateCards 标记 → 起草台据此显示「补齐声线卡并重试」
      setRuns(m => ({ ...m, [id]: { ...(m[id] || {}), state: "queued", progress: 0, error: (e && e.message) || "起草失败，请重试", needsCards: !!(e && e.canCreateCards), budgetBlock: (e && e.budgetBlock) || null } }));
    } finally {
      if (runAbortControllers.current[id] === controller) delete runAbortControllers.current[id];
    }
  };
  const startSelectedRun = (note, options = {}) => startRun(sceneOfX(pickedId), note, options);
  // Fix C：一键补齐缺失的最小声线/关系卡(active)解阻预检，成功后自动续跑起草
  const createCards = async () => {
    const sc = sceneOfX(pickedId);
    if (!sc || !sc.fromCard) return;
    const id = sc.id;
    const t0 = new Date().toTimeString().slice(0, 8);
    setRuns(m => ({ ...m, [id]: { ...(m[id] || {}), state: "running", progress: 0.04, error: null, needsCards: false,
      log: [{ t: t0, who: "system", text: "正在补齐最小声线/关系卡……" }] } }));
    try {
      const res = await scnCreateCards(sc.sid);
      const made = ((res && res.created) || []).map(c => c.dependency_type).join("、") || "(已就绪)";
      setRuns(m => ({ ...m, [id]: { ...(m[id] || {}), log: [...((m[id] || {}).log || []), { t: new Date().toTimeString().slice(0, 8), who: "system", text: `已补齐：${made} · 自动续跑起草` }] } }));
      await startRun(sc, "");
    } catch (e) {
      setRuns(m => ({ ...m, [id]: { ...(m[id] || {}), state: "queued", progress: 0, error: (e && e.message) || "补齐声线卡失败，请重试", needsCards: false } }));
    }
  };
  const topupBudget = async () => {
    const sc = sceneOfX(pickedId);
    const current = sc && sc.fromCard ? runs[sc.id] : null;
    if (!sc || !current || !current.budgetBlock) return;
    const id = sc.id;
    setRuns(m => ({ ...m, [id]: { ...(m[id] || {}), state: "running", progress: 0.03, error: null } }));
    try {
      await scnTopupBudget(sc.sid, current.budgetBlock);
      if (current.budgetBlock.resumeMode === "selection") {
        const resumed = await scnResumeAfterSelection(sc.sid);
        const block = resumed && resumed.lifecycle_budget_block;
        const fresh = await scnHydrateFromBackend(sc.sid, block ? {
          terminalJob: { error_code: block.code, error_text: block.message },
        } : undefined);
        if (!fresh) throw new Error("终选续跑后未取得可审阅正文。");
        if (block && fresh.budgetBlock) fresh.budgetBlock = { ...fresh.budgetBlock, resumeMode: "selection" };
        setRuns(m => {
          const next = { ...(m[id] || {}), ...fresh, error: null };
          if (scnRunSave) scnRunSave(sc.sid, next);
          return { ...m, [id]: next };
        });
      } else {
        await startRun(sc, current.authorNote || "", { resumeBudget: true });
      }
    } catch (e) {
      setRuns(m => ({ ...m, [id]: { ...(m[id] || {}), state: current.draft && current.draft.length ? "ready" : "queued", progress: 0, error: (e && e.message) || "追加预算失败，请重试", budgetBlock: current.budgetBlock } }));
    }
  };
  const commitAdoption = async (sc, r, mode = "overwrite", options = {}) => {
    if (!sc || archiveCommitLocks.current.has(sc.id)) return;
    archiveCommitLocks.current.add(sc.id);
    const epoch = archiveUiEpoch.current;
    const isCurrentTarget = () => scenePageMounted.current && archiveUiEpoch.current === epoch && pickedIdRef.current === sc.id;
    if (isCurrentTarget()) {
      setAdoptionBusy(mode);
      setAdoptionMessage("");
    }
    try {
      // 归档单入口仍由后端裁决；若作者稿存在，overwrite 路径会先落一份持久本地备份。
      const res = await scnAdoptToDoc(sc.sid, r.draft, r.gate, { mode, ...options });
      if (!res.ok) {
        const review = contentSafetyReviewFromError(res.error);
        if (review) {
          if (isCurrentTarget()) {
            setAdoptionDecision(null);
            setAdoptionMessage("");
            setContentSafetyError("");
            setContentSafetyReview({
              review,
              sc,
              r,
              mode,
              options: {
                ...options,
                ...(res.authorBackup && res.authorBackup.id
                  ? { authorBackupId: res.authorBackup.id }
                  : {}),
              },
            });
          }
          return;
        }
        if (isCurrentTarget()) setAdoptionMessage(`采用未完成：${res.reason || "请稍后重试"}`);
        return;
      }
      if (res.archived === false) {
        if (isCurrentTarget()) {
          setAdoptionDecision(null);
          setAdoptionMessage(res.warning
            ? `AI 稿没有覆盖作者正文；${res.warning}。请打开右下角“同步与恢复”。`
            : "AI 稿已保存为候选，作者正文没有被改动。可在右下角“同步与恢复”继续比较或恢复。");
        }
        return;
      }
      const nr = { ...r, state: "archived", justArchived: true, archivedAt: new Date().toLocaleString("zh-CN") };
      if (scenePageMounted.current) setRuns(m => ({ ...m, [sc.id]: nr }));
      if (scnRunSave) scnRunSave(sc.sid, nr);
      if (isCurrentTarget()) {
        setRunJobRefreshTick(t => t + 1);
        setAdoptionDecision(null);
        setContentSafetyReview(null);
        setContentSafetyError("");
        setAdoptionMessage(res.authorBackup
          ? "已归档；覆盖前的作者稿已自动放入“同步与恢复”。"
          : "已归档并写入正文文档。");
      }
    } catch (error) {
      const review = contentSafetyReviewFromError(error);
      if (review) {
        if (isCurrentTarget()) {
          setAdoptionDecision(null);
          setAdoptionMessage("");
          setContentSafetyError("");
          setContentSafetyReview({ review, sc, r, mode, options });
        }
      } else {
        if (isCurrentTarget()) setAdoptionMessage(`采用未完成：${(error && error.message) || "请稍后重试"}`);
      }
    } finally {
      archiveCommitLocks.current.delete(sc.id);
      if (scenePageMounted.current && pickedIdRef.current === sc.id) setAdoptionBusy("");
    }
  };
  const onArchive = async () => {
    const sc = sceneOfX(pickedId);
    if (!sc || !sc.fromCard) return;
    const r = runs[sc.id];
    if (!r || !r.draft || r.state !== "ready") return;
    if (archivePreviewLocks.current.has(sc.id) || archiveCommitLocks.current.has(sc.id)) return;
    const epoch = archiveUiEpoch.current;
    const isCurrentTarget = () => scenePageMounted.current && archiveUiEpoch.current === epoch && pickedIdRef.current === sc.id;
    archivePreviewLocks.current.add(sc.id);
    if (isCurrentTarget()) setArchivePreviewBusy(true);
    try {
      const preview = await scnPrepareAdoption(sc.sid, r.draft);
      if (!isCurrentTarget()) return;
      if (preview.hasReal) {
        setAdoptionDecision({ sc, r, preview });
        return;
      }
      await commitAdoption(sc, r, "overwrite");
    } catch (error) {
      if (isCurrentTarget()) setAdoptionMessage((error && error.message) || "无法核对作者稿，已停止采用");
    } finally {
      archivePreviewLocks.current.delete(sc.id);
      if (scenePageMounted.current && pickedIdRef.current === sc.id) setArchivePreviewBusy(false);
    }
  };

  /* 空队列（非演示作品）：引导入列 */
  if (!queue.length || !scene) {
    return (
      <div className="scn2" data-screen-label="scene" data-density={tw.scnDensity || "cozy"} style={{ "--scn-font": (tw.scnFont || 16) + "px" }}>
        <div style={{ gridColumn: "1 / -1", display: "grid", placeItems: "center", minHeight: "70vh", textAlign: "center" }}>
          <div style={{ maxWidth: 440, display: "grid", gap: 14, justifyItems: "center" }}>
            <I.Play size={26} style={{ color: "var(--ink-3)" }} />
            <div style={{ fontFamily: "var(--font-serif)", fontSize: 21, color: "var(--ink-1)" }}>运行队列还是空的</div>
            <p style={{ color: "var(--ink-3)", fontSize: 13.5, lineHeight: 1.9, margin: 0 }}>从章节目录挑一场入列，AI 会按场景卡（目标 / 阻碍 / 转折）和雪花构思起草，过质检后由你裁决。</p>
            <button className="btn btn-accent" data-testid="scene-add" onClick={() => setPicker(true)}><I.Plus size={14} /> 加入场景</button>
          </div>
        </div>
        {picker && <ScenePicker queued={extras.map(x => x.sid)} onPick={(sid) => { enqueueSid(sid); setPicker(false); }} onClose={() => setPicker(false)} />}
        <UndoToast toast={toast} onClose={clearNotice} />
      </div>
    );
  }

  return (
    <div className="scn2" data-screen-label="scene"
      data-density={tw.scnDensity || "cozy"}
      data-beats={tw.scnBeats === false ? "off" : "on"}
      style={{ "--scn-font": (tw.scnFont || 16) + "px" }}>
      <SceneQueue
        queue={queue} sceneOfX={sceneOfX}
        pickedId={pickedId} setPicked={setPicked} counts={counts} dxDone={dxDone}
        onAdd={() => setPicker(true)}
        onRemove={(id) => removeFromQueue([id])}
        select={{
          mode: qSelMode,
          has: (id) => qSel.has(id),
          count: qSel.size,
          onToggleMode: () => { setQSelMode(v => !v); setQSel(new Set()); },
          onToggle: (id) => setQSel(prev => {
            const nextSel = new Set(prev);
            if (nextSel.has(id)) nextSel.delete(id); else nextSel.add(id);
            return nextSel;
          }),
          onSelectAll: () => setQSel(prev => (prev.size === queue.length ? new Set() : new Set(queue.map(q => q.id)))),
          onRemoveSelected: () => { removeFromQueue([...qSel]); setQSelMode(false); },
        }}
      />

      <section className="scn2-stage" key={pickedId}>
        <SceneHead scene={scene} state={renderState} hideAbort={scene.fromCard} onRerun={scene.fromCard ? () => startSelectedRun("") : null} />
        {scene.fromCard && activeBackendSceneId && (
          <SceneRunJobControl
            sceneId={activeBackendSceneId}
            observedJob={observedRunJob && observedRunJob.sceneId === activeBackendSceneId ? observedRunJob.job : null}
            onJobChange={(job) => setAuthoritativeRunJob({ sceneId: activeBackendSceneId, job })}
            refreshSignal={runJobRefreshTick}
          />
        )}
        <Pipeline scene={scene} state={renderState} />
        <div className="scn2-stage-body">
          {renderState === "queued"   && <Preflight scene={scene} />}
          {renderState === "running"  && <RunningStage scene={scene} activeBeat={activeBeat} setActiveBeat={setActiveBeat} logOpen={logOpen} setLogOpen={setLogOpen} />}
          {renderState === "ready" && scene.fromCard && scene.gate && scene.gate.authorState === "awaiting_author_choice"
            ? <CandidatePicker sid={scene.sid} onDone={async (resumed) => {
                const block = resumed && resumed.lifecycle_budget_block;
                const fresh = await scnHydrateFromBackend(scene.sid, block ? {
                  terminalJob: { error_code: block.code, error_text: block.message },
                } : undefined);
                if (fresh) {
                  if (block && fresh.budgetBlock) fresh.budgetBlock = { ...fresh.budgetBlock, resumeMode: "selection" };
                  setRuns(m => ({ ...m, [scene.id]: { ...(m[scene.id] || {}), ...fresh } }));
                  if (scnRunSave) scnRunSave(scene.sid, { ...(runs[scene.id] || {}), ...fresh });
                }
              }} />
            : renderState === "ready" && <ReviewStage scene={scene} activeBeat={activeBeat} setActiveBeat={setActiveBeat} />}
          {renderState === "archived" && <ArchivedStage scene={scene} activeBeat={activeBeat} setActiveBeat={setActiveBeat} />}
        </div>
        {adoptionMessage && (
          <div className="scn2-adoption-message" role="status" aria-live="polite">
            <I.ShieldCheck size={14} /> <span>{adoptionMessage}</span>
            <button type="button" onClick={() => setAdoptionMessage("")} aria-label="关闭采用提示"><I.X size={12} /></button>
          </div>
        )}
        <DecisionBar scene={scene} state={renderState} runJobStatus={authoritativeStatus} go={go} onArchive={onArchive} onRun={startSelectedRun} onCreateCards={createCards} onBudgetTopup={topupBudget} archiveBusy={archivePreviewBusy || Boolean(adoptionBusy)} />
        {compare && <AttemptCompare attempt={compare} scene={scene} onClose={() => setCompare(null)}
          onRewrite={scene.fromCard ? () => {
            const attemptNo = compare.n || compare.attempt || "所选";
            setCompare(null);
            startSelectedRun(`参考第 ${attemptNo} 次尝试的复盘意见重写；不恢复该版正文，以当前稿为输入修正当前质检问题。`);
          } : null} />}
      </section>

      <Evidence scene={scene} state={renderState} activeBeat={activeBeat} setActiveBeat={setActiveBeat} onView={setCompare} />
      {picker && <ScenePicker queued={extras.map(x => x.sid)} onPick={(sid) => { enqueueSid(sid); setPicker(false); }} onClose={() => setPicker(false)} />}
      <UndoToast toast={toast} onClose={clearNotice} />
      {adoptionDecision && scnPortal(
        <AdoptionProtectDialog
          decision={adoptionDecision}
          busy={adoptionBusy}
          message={adoptionMessage}
          onClose={() => { if (!adoptionBusy) setAdoptionDecision(null); }}
          onCandidate={() => commitAdoption(adoptionDecision.sc, adoptionDecision.r, "candidate")}
          onOverwrite={() => commitAdoption(adoptionDecision.sc, adoptionDecision.r, "overwrite", { confirmed: true })}
        />,
        document.body,
      )}
      {contentSafetyReview && (
        <ContentSafetyReviewDialog
          review={contentSafetyReview.review}
          busy={Boolean(adoptionBusy)}
          error={contentSafetyError}
          onCancel={() => {
            if (!adoptionBusy) {
              setContentSafetyReview(null);
              setContentSafetyError("");
            }
          }}
          onConfirm={async (acceptedWarningCodes) => {
            const pending = contentSafetyReview;
            if (!pending) return;
            const expected = pending.review.findings.map(item => item.code);
            if (acceptedWarningCodes.length !== expected.length || expected.some(code => !acceptedWarningCodes.includes(code))) {
              setContentSafetyError("请逐项核对当前服务端返回的全部风险提示后再继续。");
              return;
            }
            setContentSafetyError("");
            await commitAdoption(pending.sc, pending.r, pending.mode, {
              ...pending.options,
              confirmed: true,
              acceptedWarningCodes,
            });
          }}
        />
      )}
    </div>
  );
}

function AdoptionProtectDialog({ decision, busy, message, onClose, onCandidate, onOverwrite }) {
  const [confirmed, setConfirmed] = useSt8(false);
  const dialogRef = useRef8(null);
  const safeRef = useRef8(null);
  const previousFocus = useRef8(null);
  const busyRef = useRef8(busy);
  busyRef.current = busy;
  const preview = decision.preview || {};
  const diff = preview.diff || { paras: [], adds: 0, dels: 0 };

  useEf8(() => {
    previousFocus.current = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    requestAnimationFrame(() => safeRef.current?.focus());
    const onKey = (event) => {
      if (event.key === "Escape" && !busyRef.current) { event.preventDefault(); onClose(); return; }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const nodes = [...dialogRef.current.querySelectorAll('button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])')];
      if (!nodes.length) return;
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKey);
      previousFocus.current?.focus?.();
    };
  }, []);

  return (
    <div className="scn-adopt-scrim" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
      <section ref={dialogRef} className="scn-adopt-dialog" role="dialog" aria-modal="true" aria-labelledby="scn-adopt-title">
        <header className="scn-adopt-head">
          <span className="scn-adopt-shield"><I.ShieldCheck size={19} /></span>
          <div>
            <div className="scn-adopt-eyebrow">AUTHOR COPY PROTECTION · 作者稿保护</div>
            <h2 id="scn-adopt-title">写作器里已有作者正文</h2>
            <p>AI 稿不会直接覆盖。先看差异，再选择把它保留为候选，或明确替换并归档。</p>
          </div>
          <button type="button" className="scn-adopt-close" onClick={onClose} disabled={!!busy} aria-label="关闭作者稿保护对话框"><I.X size={18} /></button>
        </header>

        <div className="scn-adopt-summary">
          <span>对象 <b>{decision.sc.sid}</b></span>
          <span className="is-del">作者稿将替换 {diff.dels} 句</span>
          <span className="is-add">AI 稿新增 {diff.adds} 句</span>
        </div>
        <div className="scn-adopt-diff" aria-label="作者正文与 AI 稿差异">
          <div className="scn-adopt-legend"><span className="is-del">作者当前稿</span><span className="is-add">AI 候选稿</span></div>
          {diff.paras.length ? diff.paras.map((para, index) => (
            <p key={`${para.p}-${index}`}>
              {para.segs.map((seg, segIndex) => <span key={`${seg.t}-${segIndex}`} className={`is-${seg.t}`}>{seg.text}</span>)}
            </p>
          )) : <div className="scn-adopt-no-diff">两份正文内容一致；仍需由你决定是否归档。</div>}
        </div>

        <div className="scn-adopt-choices">
          <section className="scn-adopt-choice is-safe">
            <div><span className="scn-adopt-choice-mark"><I.FileText size={16} /></span><strong>保存为候选</strong><em>推荐 · 不改作者稿</em></div>
            <p>AI 稿进入“同步与恢复”，之后可以继续比较、复制或恢复；当前正文与服务端归档状态都不变。</p>
            <button ref={safeRef} type="button" className="btn btn-accent" onClick={onCandidate} disabled={!!busy} data-testid="scene-save-candidate">
              <I.Save size={14} /> {busy === "candidate" ? "保存中…" : "保存为候选（推荐）"}
            </button>
          </section>
          <section className="scn-adopt-choice is-overwrite">
            <div><span className="scn-adopt-choice-mark"><I.AlertTriangle size={16} /></span><strong>替换并归档</strong><em>会改写作者稿</em></div>
            <p>系统会先自动备份当前作者稿，再调用后端归档并写入 AI 稿。备份失败时覆盖会被阻止。</p>
            <label className="scn-adopt-confirm"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /> <span>我已查看差异，确认用 AI 稿替换当前正文</span></label>
            <button type="button" className="btn btn-ghost scn-adopt-overwrite" onClick={onOverwrite} disabled={!confirmed || !!busy} data-testid="scene-confirm-overwrite">
              <I.Check size={14} /> {busy === "overwrite" ? "备份并归档中…" : "确认覆盖并归档"}
            </button>
          </section>
        </div>
        <div className="scn-adopt-live" role="status" aria-live="polite">{message || "默认安全选项是保存为候选。"}</div>
      </section>
    </div>
  );
}

/* ============================ Queue ============================ */

function SceneQueue({ queue, sceneOfX, pickedId, setPicked, counts, dxDone, onAdd, onRemove, select }) {
  const sel = select || {};
  const selectMode = !!sel.mode;
  const selectedCount = sel.count || 0;
  return (
    <aside className="scn2-queue">
      <header className="scn2-queue-head">
        <div className="page-eyebrow" style={{ margin: 0, display: "flex", alignItems: "center", gap: 8 }}>AI 起草台</div>
        <h2 className="text-serif scn2-queue-title">运行队列</h2>
        <p className="scn2-queue-sub">从章节编排的场景卡入列 · 一场一裁</p>
      </header>

      <div className="scn2-stats">
        <QStat n={counts.running} label="运行" tone="crimson" />
        <QStat n={counts.queued}  label="排队" tone="slate" />
        <QStat n={counts.ready}   label="待审" tone="gold" />
        <QStat n={counts.archived} label="归档" tone="sage" />
      </div>

      {selectMode && (
        <div className="scn2-queue-batch" role="toolbar" aria-label="队列批量操作">
          <div className="scn2-queue-batch-top">
            <span className="scn2-queue-batch-n">已选 <strong>{selectedCount}</strong> / {queue.length}</span>
            <button className="btn btn-quiet btn-sm" onClick={sel.onSelectAll}>
              {selectedCount === queue.length && queue.length ? "取消全选" : "全选"}
            </button>
          </div>
          <div className="scn2-queue-batch-top">
            <button className="btn btn-danger btn-sm" data-testid="scene-queue-batch-remove" style={{ flex: 1 }}
              disabled={!selectedCount} onClick={sel.onRemoveSelected}>
              <I.Trash size={12} /> 移出所选
            </button>
            <button className="btn btn-ghost btn-sm" data-testid="scene-queue-select-exit" onClick={sel.onToggleMode}>完成</button>
          </div>
        </div>
      )}

      <ul className="scn2-queue-list">
        {queue.map(q => {
          const s = sceneOfX(q.id);
          const st = q.state;
          const active = pickedId === q.id;
          const checked = !!(sel.has && sel.has(q.id));
          return (
            <li key={q.id} className={`scn2-qrow-wrap ${checked ? "is-selected" : ""}`}>
              {selectMode && (
                <label className="scn2-qrow-check" title="选中后可批量移出队列">
                  <input type="checkbox" checked={checked} aria-label={`选择 ${s.n} ${s.title}`} onChange={() => sel.onToggle(q.id)} />
                </label>
              )}
              <button className={`scn2-qrow ${active ? "is-active" : ""} s-${st}`} data-testid="scene-queue-item" data-scene-sid={s.sid || ""}
                onClick={() => (selectMode ? sel.onToggle(q.id) : setPicked(q.id))}>
                <span className={`scn2-qrow-spine s-${st}`} />
                <div className="scn2-qrow-main">
                  <div className="scn2-qrow-top">
                    <span className="scn2-qrow-num">{s.n}</span>
                    <span className={`scn2-chip s-${st}`}>{st === "running" && <span className="scn2-chip-pulse" />}{STATE_LABEL[st]}</span>
                  </div>
                  <div className="scn2-qrow-title text-serif">{s.title}</div>
                  {dxDone && dxDone[s.n] != null && <span className="scn2-qrow-dx"><I.Microscope size={11} /> 已深改 · {dxDone[s.n]} 处</span>}
                  <div className="scn2-qrow-bar">
                    <div className={`scn2-qrow-fill s-${st}`} style={{ width: (st === "running" ? q.progress * 100 : 100) + "%" }} />
                  </div>
                </div>
              </button>
              {!selectMode && (
                <button className="scn2-qrow-x" data-testid="scene-queue-remove" aria-label={`把 ${s.title} 移出队列`}
                  title="移出队列（保留场景卡与已生成的 AI 稿，可撤销）"
                  onClick={(e) => { e.stopPropagation(); onRemove && onRemove(q.id); }}><I.Trash size={12} /></button>
              )}
            </li>
          );
        })}
      </ul>

      {!selectMode && (
        <div className="scn2-queue-foot">
          <button className="btn btn-accent btn-sm" data-testid="scene-add" style={{ flex: 1 }} onClick={onAdd}><I.Plus size={13} /> 加入场景</button>
          <button className="btn btn-quiet btn-sm" data-testid="scene-queue-select-mode"
            disabled={!queue.length} title="多选后可一次移出多场" onClick={sel.onToggleMode}>
            <I.Check size={13} /> 多选
          </button>
        </div>
      )}
    </aside>
  );
}

function QStat({ n, label, tone }) {
  return (
    <div className={`scn2-stat tone-${tone}`}>
      <div className="scn2-stat-num tab-num">{n}</div>
      <div className="scn2-stat-label">{label}</div>
    </div>
  );
}

/* ============================ Head ============================ */

function SceneHead({ scene, state, onAbort, onRerun, hideAbort = false }) {
  const stateLabel = scene.fromCard
    ? STATE_LABEL[state]
    : ({ running: "运行样例", queued: "排队样例", ready: "待审样例", archived: "归档样例" })[state];
  return (
    <header className="scn2-head">
      <div className="scn2-head-l">
        <div className="scn2-head-meta">
          <span className="scn2-head-num">{scene.n}</span>
          <span className="scn2-head-dot">·</span>
          <span>{scene.kind}</span>
          <span className={`scn2-state-tag tone-${STATE_TONE[state]}`}>
            {state === "running" && <span className="scn2-chip-pulse" />}
            {stateLabel}
          </span>
        </div>
        <h1 className="scn2-head-title text-serif">{scene.title}</h1>
        <div className="scn2-head-sub">
          {state === "running" && <span>第 {scene.attempt} 次尝试 · 用时 {scene.elapsed} · 预计 {scene.eta} 后可裁决</span>}
          {state === "ready"   && <span>{scene.fromCard ? `第 ${scene.attempt} 次尝试 · ${scene.verdict?.words} 字 · ${scene.model}` : `演示待复核稿 · ${scene.verdict?.words || "—"} 字 · 只读`}</span>}
          {state === "queued"  && <span>预检就绪 · 目标 {scene.targetWords} 字</span>}
          {state === "archived" && <span>{scene.fromCard ? (scene.justArchived ? "刚刚写回章节场景卡" : "已写回 · " + (scene.archivedAt || "")) : "演示归档状态 · 只读 · 未写入真实作品"}</span>}
        </div>
      </div>
      <div className="scn2-head-r">
        {!scene.fromCard && <button className="btn btn-quiet btn-sm" disabled title="演示场没有可编辑的真实场景卡"><I.FileText size={13} /> 演示戏剧卡</button>}
        {state === "running" && !hideAbort && (onAbort ? <button className="btn btn-ghost btn-sm" onClick={onAbort}>中止</button> : <button className="btn btn-ghost btn-sm" disabled title="演示运行不可中止">演示运行</button>)}
        {(state === "running" || state === "ready") && (onRerun
          ? (state === "ready" && <button className="btn btn-ghost btn-sm" onClick={onRerun}><I.Refresh size={13} /> 重跑</button>)
          : <button className="btn btn-ghost btn-sm" disabled title="演示稿不可提交真实重跑"><I.Refresh size={13} /> 演示稿</button>)}
      </div>
    </header>
  );
}

/* ============================ Pipeline ============================ */

function Pipeline({ scene, state }) {
  const liveProgress = state === "running" ? scene.progress : 1;
  return (
    <div className="scn2-pipe">
      {SC_STAGES.map((stg, i) => {
        let st = "todo";
        if (i < scene.stageIdx) st = "done";
        else if (i === scene.stageIdx) st = state === "running" ? "active" : (state === "archived" || state === "ready" ? "done" : "active");
        if (state === "archived") st = "done";
        if (state === "queued" && i === 0) st = "active";
        if (state === "queued" && i > 0) st = "todo";
        return (
          <React.Fragment key={stg.id}>
            <div className={`scn2-pstep s-${st}`}>
              <span className="scn2-pmark">
                {st === "done" && <I.Check size={12} />}
                {st === "active" && (state === "running" ? <span className="scn2-spin" /> : <span className="scn2-pdot" />)}
                {st === "todo" && <span className="scn2-pidx">{i + 1}</span>}
              </span>
              <span className="scn2-pname">{stg.name}</span>
            </div>
            {i < SC_STAGES.length - 1 && (
              <span className={`scn2-pline ${i < scene.stageIdx ? "is-done" : ""}`}>
                {i === scene.stageIdx - 1 && state === "running" && <span className="scn2-pline-go" style={{ width: (liveProgress * 100) + "%" }} />}
              </span>
            )}
          </React.Fragment>
        );
      })}
    </div>
  );
}

/* ============================ Draft renderer ============================ */

function Draft({ scene, activeBeat, setActiveBeat, typing }) {
  return (
    <article className="scn2-draft text-serif">
      {scene.draft.map((p, i) => {
        const isBeat = !!p.beat;
        const isActive = activeBeat && p.beat === activeBeat;
        const last = typing && i === scene.draft.length - 1;
        return (
          <p
            key={p.id}
            className={`scn2-para ${isBeat ? "has-beat tone-" + BEAT_META[p.beat].tone : ""} ${isActive ? "is-lit" : ""}`}
            onMouseEnter={() => isBeat && setActiveBeat(p.beat)}
            onMouseLeave={() => isBeat && setActiveBeat(null)}
          >
            {isBeat && <span className={`scn2-beat-tab tone-${BEAT_META[p.beat].tone}`}>{BEAT_META[p.beat].label.split(" ")[0]}</span>}
            {p.parts.map((part, j) =>
              part.risk
                ? <mark key={j} className={`scn2-risk sev-${part.sev}`} data-tip={part.tip}>{part.text}</mark>
                : <span key={j}>{part.text}</span>
            )}
            {last && <span className="scn2-caret" />}
          </p>
        );
      })}
    </article>
  );
}

/* ============================ Preflight (queued) ============================ */

function Preflight({ scene }) {
  const briefBeats = (scene.brief || []).map(b => b.beat);
  const checks = scene.fromCard
    ? [
        { ok: briefBeats.includes("goal"),     text: "场景卡 · 目标已填" },
        { ok: briefBeats.includes("conflict"), text: "场景卡 · 阻碍已填" },
        { ok: briefBeats.includes("exit"),     text: "场景卡 · 出口已填" },
        { ok: false, text: "参考画像未绑定 · 可选" },
      ]
    : [
        { ok: true,  text: "戏剧卡完整 · 6/6 字段" },
        { ok: true,  text: "出场角色已绑定 · 3 位" },
        { ok: true,  text: "上一场出口已对齐入口" },
        { ok: false, text: "参考画像未绑定 · 可选" },
      ];
  return (
    <div className="scn2-pre scn2-scroll">
      <div className="scn2-pre-card">
        {scene.fromCard && (
          <div className="scn2-archived-note" style={{ marginBottom: 12 }}>
            <I.ArrowRight size={14} /> 由章节编排「交给 AI」入列 · 预检校验的就是这张场景卡
          </div>
        )}
        <div className="scn2-pre-eyebrow"><I.ShieldCheck size={14} /> 预检清单</div>
        <ul className="scn2-pre-list">
          {checks.map((c, i) => (
            <li key={i} className={c.ok ? "ok" : "opt"}>
              {c.ok ? <I.Check size={14} /> : <I.Circle size={13} />}
              <span>{c.text}</span>
            </li>
          ))}
        </ul>
        <div className="scn2-pre-brief">
          <div className="scn2-pre-eyebrow"><I.Compass size={14} /> 本场戏剧卡{scene.fromCard ? " · 与章节编排同一张" : ""}</div>
          <ul className="scn2-brief-list">
            {scene.brief.map((b, i) => (
              <li key={i}>
                <span className={`scn2-brief-tag tone-${BEAT_META[b.beat].tone}`}>{BEAT_META[b.beat].label.split(" ")[0]}</span>
                <span className="scn2-brief-text">{b.text}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}

/* ============================ Running (live) ============================ */

function RunningStage({ scene, activeBeat, setActiveBeat, logOpen, setLogOpen }) {
  const banner = scene.runBanner || { t: "二次改写进行中", s: `针对 2 项中风险 · Haiku · 预计 ${scene.eta}` };
  const reduce = useMemo8(() => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches, []);
  const [shown, setShown] = useSt8(reduce ? scene.log.length : 1);
  const scRef = useRef8(null);

  useEf8(() => {
    if (reduce) { setShown(scene.log.length); return; }
    setShown(1);
    let i = 1;
    const id = setInterval(() => {
      i += 1;
      setShown(Math.min(i, scene.log.length));
      if (i >= scene.log.length) clearInterval(id);
    }, 900);
    return () => clearInterval(id);
  }, [scene.id]);

  useEf8(() => {
    if (logOpen && scRef.current) scRef.current.scrollTop = scRef.current.scrollHeight;
  }, [shown, logOpen]);

  return (
    <div className="scn2-run">
      <div className="scn2-run-doc scn2-scroll">
        <div className="scn2-run-banner">
          <span className="scn2-spin scn2-spin-lg" />
          <div>
            <div className="scn2-run-banner-t">{banner.t}</div>
            <div className="scn2-run-banner-s">{banner.s}</div>
          </div>
          <div className="scn2-run-pct tab-num">{Math.round(scene.progress * 100)}%</div>
        </div>
        {scene.draft.length === 0 && (
          <p className="scn2-para" style={{ color: "var(--ink-3)" }}>Claude 正在按场景卡起草……整稿返回后先过本地质检（短句率 / 句式重复 / 超长句），再交给你裁决。</p>
        )}
        <Draft scene={scene} activeBeat={activeBeat} setActiveBeat={setActiveBeat} typing />
        <p className="scn2-draft-foot">起草稿 · {scene.targetWords} 字{scene.fromCard ? " · 整稿返回后过质检" : " · 正在按风险项改写…"}</p>
      </div>

      <div className={`scn2-console ${logOpen ? "is-open" : ""}`}>
        <button className="scn2-console-bar" onClick={() => setLogOpen(o => !o)}>
          <I.Activity size={13} />
          <span>运行日志</span>
          <span className="scn2-console-live"><span className="scn2-chip-pulse" />直播</span>
          <I.ChevronRight size={14} className="scn2-console-caret" />
        </button>
        {logOpen && (
          <ul className="scn2-log scn2-scroll" ref={scRef}>
            {scene.log.slice(0, shown).map((l, i) => (
              <li key={i} className="scn2-log-row">
                <span className="scn2-log-t">{l.t}</span>
                <span className={`scn2-log-who w-${l.who}`}>{l.who}</span>
                <span className="scn2-log-text">{l.text}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/* ============================ Candidate terminal selection (Wave 3 §5.5) ============================ */

function CandidatePicker({ sid, onDone }) {
  const [st, setSt] = useSt8({ loading: true, error: null, candidates: [], picking: null, resuming: false });
  const [styleMatchReason, setStyleMatchReason] = useSt8(false);
  const openedAtRef = useRef8(Date.now());
  useEf8(() => {
    let on = true;
    openedAtRef.current = Date.now();
    setStyleMatchReason(false);
    setSt({ loading: true, error: null, candidates: [], picking: null, resuming: false });
    (async () => {
      try {
        const data = await scnCandidates(sid);
        if (!on) return;
        setSt(s => ({ ...s, loading: false, candidates: (data && data.candidates) || [] }));
      } catch (e) {
        if (on) setSt(s => ({ ...s, loading: false, error: (e && e.message) || "候选拉取失败，请重试" }));
      }
    })();
    return () => { on = false; };
  }, [sid]);

  const choose = async (rowId, tie) => {
    setSt(s => ({ ...s, picking: rowId, error: null }));
    try {
      const selection = {
        no_clear_difference: !!tie,
        duration_ms: Math.max(0, Date.now() - openedAtRef.current),
        preference_tags: styleMatchReason && !tie ? ["style_match"] : [],
      };
      await scnSelectCandidate(sid, rowId, selection);
      setSt(s => ({ ...s, resuming: true }));
      const resumed = await scnResumeAfterSelection(sid);
      onDone && (await onDone(resumed));
    } catch (e) {
      setSt(s => ({ ...s, picking: null, resuming: false, error: (e && e.message) || "终选失败，请重试" }));
    }
  };

  return (
    <div className="scn2-review scn2-scroll">
      <div className="scn2-decide is-wait" style={{ marginBottom: 10 }}>
        <div className="scn2-decide-sum">
          <I.Users size={14} /> 关键场景 · 匿名候选终选：全文读完再选——顺序已随机化，机器分数不展示，选中后管线自动续跑（批判修订 → 质检 → 归档）
        </div>
      </div>
      {st.error && (
        <div className="scn2-decide is-wait" style={{ borderColor: "var(--crimson)", marginBottom: 10 }}>
          <div className="scn2-decide-sum"><I.AlertTriangle size={14} style={{ color: "var(--crimson)" }} /> {st.error}</div>
        </div>
      )}
      {st.loading && <p className="scn2-draft-foot">候选拉取中……</p>}
      {!st.loading && !st.candidates.length && !st.error && (
        <p className="scn2-draft-foot">没有可选候选——可退回重写重新生成。</p>
      )}
      {st.candidates.map((c, i) => (
        <article key={c.row_id} style={{ border: "1px solid var(--line-1)", borderRadius: 10, padding: "14px 16px", marginBottom: 12 }}>
          <header style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
            <span className="pill pill-slate text-xs"><span className="pill-dot" />候选 {String.fromCharCode(65 + i)}</span>
            <span style={{ flex: 1 }} />
            <button
              className="btn btn-accent btn-sm"
              data-testid="scene-candidate-select"
              data-candidate-row-id={c.row_id}
              disabled={!!st.picking || st.resuming}
              onClick={() => choose(c.row_id, false)}
            >
              <I.Check size={13} /> {st.picking === c.row_id ? (st.resuming ? "续跑中…" : "提交中…") : "选这稿"}
            </button>
          </header>
          <div style={{ whiteSpace: "pre-wrap", lineHeight: 1.9, fontSize: "var(--scn-font)" }}>{c.content}</div>
        </article>
      ))}
      {!st.loading && st.candidates.length > 1 && (
        <div style={{ display: "flex", alignItems: "center", gap: 12, justifyContent: "space-between", marginBottom: 8 }}>
          <label style={{ display: "inline-flex", alignItems: "center", gap: 7, cursor: "pointer", fontSize: 12, color: "var(--ink-2)" }}>
            <input
              type="checkbox"
              data-testid="scene-candidate-style-reason"
              checked={styleMatchReason}
              disabled={!!st.picking || st.resuming}
              onChange={e => setStyleMatchReason(e.target.checked)}
            />
            我主要按“更贴近参考风格”来选
          </label>
          <button className="btn btn-quiet btn-sm" data-testid="scene-candidate-tie" disabled={!!st.picking || st.resuming}
            title="记录「无明显差异」并采用候选 A（终选耗时与平局照实入档）"
            onClick={() => choose(st.candidates[0].row_id, true)}>
            两稿无明显差异 · 用候选 A
          </button>
        </div>
      )}
      <p className="scn2-draft-foot">终选一次写入：提交后改选需在后端显式重开（留审计）。</p>
    </div>
  );
}

/* ============================ Review (ready) ============================ */

function ReviewStage({ scene, activeBeat, setActiveBeat }) {
  return (
    <div className="scn2-review scn2-scroll">
      <Draft scene={scene} activeBeat={activeBeat} setActiveBeat={setActiveBeat} />
      <p className="scn2-draft-foot">复核稿 · {scene.verdict?.words} 字 · 悬停高亮处查看风险，点右侧戏剧卡定位段落</p>
    </div>
  );
}

/* ============================ Archived ============================ */

function ArchivedStage({ scene, activeBeat, setActiveBeat }) {
  return (
    <div className="scn2-review scn2-scroll">
      {!scene.fromCard && (
        <div className="scn2-archived-note">
          <I.ShieldCheck size={15} /> 演示归档样例 · 仅展示终态界面，不代表已写入任何真实作品或正文
        </div>
      )}
      {scene.justArchived && (
        <div className="scn2-archived-note">
          <I.Check size={15} /> {scene.fromCard
            ? <>已写入 <strong>{scene.n}</strong> 的正文文档（{scene.verdict ? scene.verdict.words : "—"} 字）· 场景卡已置「完成」· 字数已回写目录</>
            : <>已写回 <strong>{scene.n}</strong> 场景卡 · writer_brief_json 已更新</>}
        </div>
      )}
      <Draft scene={scene} activeBeat={activeBeat} setActiveBeat={setActiveBeat} />
      <p className="scn2-draft-foot">{scene.fromCard ? "定稿 · 只读 · 如需局部打磨请送写作台深改" : "演示归档快照 · 完全只读 · 不可归档、重写或送深改"}</p>
    </div>
  );
}

/* ============================ Decision bar ============================ */

function DecisionBar({ scene, state, runJobStatus, go, onArchive, onRun, onCreateCards, onBudgetTopup, archiveBusy = false }) {
  const [rework, setRework] = useSt8(false);
  const [note, setNote] = useSt8("");
  const normalizedNoteLength = Array.from(note.trim()).length;
  const noteTooLong = normalizedNoteLength > 2000;
  useEf8(() => { setRework(false); setNote(""); }, [scene.id]);

  const toWriterDeep = () => {
    const sid = scene.sid || scnSidOf(scene.n);
    go("writer");
    setTimeout(() => {
      if (sid) window.dispatchEvent(new CustomEvent("ws:writer-scene", { detail: sid }));
      window.dispatchEvent(new CustomEvent("ws:writer-posture", { detail: "deep" }));
    }, 80);
  };

  if (state === "queued") {
    if (scene.fromCard) {
      if (runJobStatus === "queued") {
        return (
          <div className="scn2-decide is-wait">
            <div className="scn2-decide-sum"><I.Clock size={14} /> 任务已排队 · 等待 worker 接管</div>
            <div className="scn2-decide-acts">
              <button className="btn btn-ghost btn-sm" type="button" disabled>等待运行</button>
            </div>
          </div>
        );
      }
      return (
        <div className="scn2-decide">
          <div className="scn2-decide-sum">
            {scene.error
              ? <><I.AlertTriangle size={14} style={{ color: "var(--crimson)" }} /> {scene.error}</>
              : <><I.Clock size={14} /> 预检就绪 · 会把雪花构思与场景卡一起喂给 Claude</>}
          </div>
          <div className="scn2-decide-acts">
            <button className="btn btn-quiet btn-sm" onClick={() => go("author")} title="场景卡在章节编排里维护">编辑场景卡</button>
            {scene.budgetBlock && onBudgetTopup
              ? <button className="btn btn-accent" data-testid="scene-budget-topup" onClick={() => onBudgetTopup()}><I.Plus size={13} /> 追加预算并继续</button>
              : scene.needsCards && onCreateCards
              ? <button className="btn btn-accent" data-testid="scene-create-cards" onClick={() => onCreateCards()} title="确定性建出最小 active 声线/关系卡解阻，再自动续跑起草"><I.Refresh size={13} /> 补齐声线卡并重试</button>
              : <button className="btn btn-accent" data-testid="scene-start" onClick={() => onRun && onRun("")}><I.Play size={13} /> 开始起草</button>}
          </div>
        </div>
      );
    }
    return (
      <div className="scn2-decide">
        <div className="scn2-decide-sum"><I.Clock size={14} /> 预检就绪，可立即起草</div>
        <div className="scn2-decide-acts">
          <button className="btn btn-quiet btn-sm" disabled title="演示场没有真实场景卡">演示戏剧卡</button>
          <button className="btn btn-accent" disabled title="这是只读演示样例；请从自己的目录加入场景"><I.Play size={13} /> 演示样例 · 只读</button>
        </div>
      </div>
    );
  }

  if (state === "running") {
    return (
      <div className="scn2-decide is-wait">
        <div className="scn2-decide-sum"><span className="scn2-spin" /> 运行中 · 完成校核后开放裁决</div>
        <div className="scn2-decide-acts">
          <button className="btn btn-ghost btn-sm" disabled>采纳并归档</button>
        </div>
      </div>
    );
  }

  if (state === "archived") {
    return (
      <div className="scn2-decide is-done">
        <div className="scn2-decide-sum"><I.Database size={14} /> {scene.fromCard ? "已写入正文文档 · 场景卡置「完成」" : "已归档至章节场景卡"}</div>
        <div className="scn2-decide-acts">
          {scene.fromCard && (
            <button className="btn btn-quiet btn-sm" onClick={() => go("writer", { type: "ws:writer-scene", detail: scene.sid })}><I.Pen size={13} /> 在写作器打开</button>
          )}
          <button className="btn btn-quiet btn-sm" onClick={() => go("manuscripts")}>在成稿中心查看</button>
          <button className="btn btn-ghost btn-sm" onClick={toWriterDeep}><I.Microscope size={13} /> 送写作台深改</button>
        </div>
      </div>
    );
  }

  // ready → the real decision moment
  const v = scene.verdict || {};
  /* Wave 2（治理 §5.3/§5.4）：「无法继续」与「已有稿但建议修改」分开展示。
     hard_blocked = 已证实 Q0/Q1 → 归档禁用（正文保留可接管）；
     quality_warning = Q2/Q3 建议 → 照常可归档，建议随行。 */
  const gate = scene.gate || null;
  const budgetBlocked = !!scene.budgetBlock;
  const gateBlocked = !!(gate && gate.canArchive === false && !budgetBlocked);
  const gateWarn = !!(gate && !gateBlocked && gate.authorState === "quality_warning");
  const gateKeys = (list) => (list || []).map(f => f.issue_key || f.kind).filter(Boolean).slice(0, 4).join("、");
  return (
    <div className="scn2-decide-wrap">
      {budgetBlocked && (
        <div className="scn2-decide is-wait" style={{ borderColor: "var(--gold)", marginBottom: 8 }}>
          <div className="scn2-decide-sum">
            <I.AlertTriangle size={14} style={{ color: "var(--gold)" }} />
            {scene.budgetBlock.label} · 已有正文与持久化恢复点均保留
          </div>
          <div className="scn2-decide-acts">
            <button className="btn btn-accent" data-testid="scene-budget-topup" onClick={() => onBudgetTopup && onBudgetTopup()}><I.Plus size={13} /> 追加预算并继续</button>
          </div>
        </div>
      )}
      {gateBlocked && (
        <div className="scn2-decide is-wait" style={{ borderColor: "var(--crimson)", marginBottom: 8 }}>
          <div className="scn2-decide-sum">
            <I.AlertTriangle size={14} style={{ color: "var(--crimson)" }} />
            无法继续：已证实的硬问题（Q0/Q1）{gateKeys(gate.blocking) ? `：${gateKeys(gate.blocking)}` : ""} · 正文已保留，处理或重跑后再归档
          </div>
          <div className="scn2-decide-acts">
            <button
              className="btn btn-accent"
              data-testid="scene-hard-rewrite"
              disabled={!onRun}
              onClick={() => onRun && onRun(
                scene.rewriteBrief
                || (gate.blocking || []).map(f => f.human_readable_reason || f.message || f.issue_key || f.kind).filter(Boolean).join("；")
                || "按已证实的 Q0/Q1 硬问题修正正文，逐项满足场景卡约束后重新复检。"
              )}
            ><I.Refresh size={13} /> 按硬问题重写并复检</button>
          </div>
        </div>
      )}
      {gateWarn && (
        <div className="scn2-decide is-wait" style={{ borderColor: "var(--gold)", marginBottom: 8 }}>
          <div className="scn2-decide-sum">
            <I.AlertTriangle size={14} style={{ color: "var(--gold)" }} />
            已有稿，建议修改：{(gate.warnings || []).length} 条质量建议（Q2/Q3）{gateKeys(gate.warnings) ? `：${gateKeys(gate.warnings)}` : ""} · 可直接归档，警告随稿留痕
          </div>
        </div>
      )}
      {rework && (
        <div className="scn2-rework">
          <div className="scn2-rework-head">
            <I.Refresh size={13} /><span>退回重写 · 给改写指令</span>
            <button className="scn2-rework-x" onClick={() => setRework(false)}><I.X size={13} /></button>
          </div>
          <div className="scn2-rework-chips">
            {["收一点结尾的比喻", "Setback 往后挪", "增强环境声细节", "压一压短句率"].map(c => (
              <button key={c} className="scn2-rework-chip" onClick={() => setNote(n => n ? n + "；" + c : c)}>{c}</button>
            ))}
          </div>
          <textarea
            className="scn2-rework-input" rows={2}
            placeholder="写给模型的具体改写指令，例如：保留第 5 段的节奏，但把最后一句的比喻收得更克制…"
            value={note} onChange={e => setNote(e.target.value)}
            aria-invalid={noteTooLong ? "true" : undefined}
            aria-describedby="scene-author-note-limit"
          />
          <div className="scn2-rework-foot">
            <span
              id="scene-author-note-limit"
              className="scn2-rework-hint"
              role={noteTooLong ? "alert" : undefined}
            >
              {noteTooLong
                ? `作者指令 ${normalizedNoteLength} / 2000 字；请精简，系统不会静默截断。`
                : `作者指令 ${normalizedNoteLength} / 2000 字 · 将保留为第 ${scene.attempt + 1} 次尝试`}
            </span>
            <button className="btn btn-accent btn-sm" onClick={() => { if (scene.fromCard && onRun) { onRun(note); setRework(false); } }} disabled={(scene.fromCard && !note.trim()) || noteTooLong}><I.Refresh size={13} /> 确认退回重写</button>
          </div>
        </div>
      )}
      <div className="scn2-decide is-ready">
        <div className="scn2-decide-verdict">
          <span className="scn2-verdict-badge"><I.ShieldCheck size={14} /> {v.qc}</span>
          <span className="scn2-verdict-meta">{v.align} · {v.risks}</span>
        </div>
        <div className="scn2-decide-acts">
          <button className="btn btn-ghost btn-sm" onClick={toWriterDeep}><I.Microscope size={13} /> 送写作台深改</button>
          <button className={`btn btn-quiet btn-sm ${rework ? "is-on" : ""}`} onClick={() => setRework(r => !r)}><I.Refresh size={13} /> 退回重写</button>
          <button
            className="btn btn-accent"
            data-testid="scene-archive"
            onClick={onArchive}
            disabled={gateBlocked || budgetBlocked || archiveBusy}
            aria-busy={archiveBusy ? "true" : undefined}
            title={archiveBusy ? "正在核对作者正文与归档状态，请稍候" : (budgetBlocked ? "生命周期预算阻断尚未解除，需显式追加后续跑" : (gateBlocked ? "存在已证实的 Q0/Q1 硬问题，暂不能归档（正文已保留）" : undefined))}
          ><I.Check size={14} /> {archiveBusy ? "正在核对作者稿…" : (budgetBlocked ? "需续跑完成" : (gateBlocked ? "需处理硬问题" : "采纳并归档"))}</button>
        </div>
      </div>
    </div>
  );
}

/* ============================ Evidence ============================ */

function Evidence({ scene, state, activeBeat, setActiveBeat, onView }) {
  const hasMetrics = scene.metrics && scene.metrics.length > 0;
  return (
    <aside className="scn2-evi scn2-scroll">
      {hasMetrics && (
        <section className="scn2-evi-block">
          <h3 className="scn2-evi-h"><I.ShieldCheck size={13} /> 质检指标</h3>
          <div className="scn2-meters">
            {scene.metrics.map((m, i) => (
              <div key={i} className="scn2-meter">
                <div className="scn2-meter-top">
                  <span className="scn2-meter-label">{m.label}</span>
                  <span className={`scn2-meter-val tab-num tone-${m.tone}`}>{m.val}</span>
                </div>
                <div className="scn2-meter-track">
                  <div className={`scn2-meter-fill tone-${m.tone}`} style={{ width: m.pct + "%" }} />
                  <span className="scn2-meter-target" style={{ left: m.target + "%" }} title={`目标 ${m.target}%`} />
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {scene.alignment && scene.alignment.length > 0 && (
        <section className="scn2-evi-block">
          <h3 className="scn2-evi-h"><I.Compass size={13} /> 戏剧卡对齐</h3>
          <ul className="scn2-align">
            {scene.alignment.map((a, i) => {
              const meta = BEAT_META[a.beat];
              const lit = activeBeat === a.beat;
              const clickable = !!a.para;
              return (
                <li key={i}>
                  <button
                    type="button"
                    className={`scn2-align-row st-${a.status} ${lit ? "is-lit" : ""} ${clickable ? "" : "is-static"}`}
                    disabled={!clickable}
                    aria-pressed={clickable ? lit : undefined}
                    title={clickable ? "定位并高亮正文证据" : "暂无可定位的正文证据"}
                    onClick={() => clickable && setActiveBeat(lit ? null : a.beat)}
                    onMouseEnter={() => clickable && setActiveBeat(a.beat)}
                    onMouseLeave={() => clickable && setActiveBeat(null)}
                  >
                    <span className={`scn2-align-dot tone-${meta.tone}`} />
                    <span className="scn2-align-body">
                      <span className="scn2-align-beat">{meta.label}</span>
                      <span className="scn2-align-note">{a.note}</span>
                    </span>
                    <span className={`scn2-align-mark st-${a.status}`}>
                      {a.status === "ok" && <I.Check size={12} />}
                      {a.status === "warn" && <I.AlertTriangle size={12} />}
                      {a.status === "pend" && "…"}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {scene.attempts && scene.attempts.length > 0 && (
        <section className="scn2-evi-block">
          <h3 className="scn2-evi-h"><I.Clock size={13} /> 尝试历史 · {scene.attempts.length}</h3>
          <ul className="scn2-tries">
            {scene.attempts.map((a, i) => (
              <li key={i} className={`scn2-try ${i === 0 ? "is-current" : ""}`}>
                <span className="scn2-try-n tab-num">#{a.n}</span>
                <span className="scn2-try-body">
                  <span className="scn2-try-top">
                    <span className="scn2-try-time">{a.time}</span>
                    <span className={`scn2-try-tag tone-${a.tone}`}>{a.result === "running" ? "进行中" : a.result}</span>
                  </span>
                  <span className="scn2-try-note">{a.note}</span>
                </span>
                {i !== 0 && <button className="scn2-try-view" onClick={() => onView(a)}>对比</button>}
              </li>
            ))}
          </ul>
        </section>
      )}

      {scene.cost && scene.cost.length > 0 && (
        <section className="scn2-evi-block">
          <h3 className="scn2-evi-h"><I.Coins size={13} /> 本次开销</h3>
          <ul className="scn2-rows">
            {scene.cost.map((c, i) => (
              <li key={i}><span>{c.k}</span><strong className={c.mono ? "tab-num" : ""}>{c.v}</strong></li>
            ))}
          </ul>
        </section>
      )}

      {scene.dxCount != null && (
        <section className="scn2-evi-block">
          <h3 className="scn2-evi-h"><I.Microscope size={13} /> 深改记录</h3>
          <div className="scn2-dx">
            <div className="scn2-dx-top"><I.Check size={13} /> 写作台深改已采纳 {scene.dxCount} 处建议并回传</div>
            <ul className="scn2-rows">
              <li><span>逐句采纳</span><strong>{scene.dxCount} 处</strong></li>
              <li><span>低风险项</span><strong className="scn2-dx-ok">已清零</strong></li>
              <li><span>状态</span><strong>已同步本场质检</strong></li>
            </ul>
          </div>
        </section>
      )}

      <section className="scn2-evi-block">
        <h3 className="scn2-evi-h"><I.Database size={13} /> 归档去向</h3>
        <ul className="scn2-rows">
          <li><span>章节</span><strong>{scene.n?.split(" · ")[0]}</strong></li>
          <li><span>场景卡</span><strong>{scene.n?.split(" · ")[1]} · {scene.kind}</strong></li>
          {scene.fromCard ? (
            <React.Fragment>
              <li><span>写入</span><strong className="tab-num">wr-doc · 写作器正文</strong></li>
              <li><span>连带</span><strong>字数回写 + 场景卡置完成</strong></li>
            </React.Fragment>
          ) : (
            <li><span>数据</span><strong>演示快照 · 未写入真实作品</strong></li>
          )}
          <li><span>策略</span><strong>{scene.fromCard ? (state === "archived" ? "已写入" : "裁决通过后写入") : "只读预览 · 无持久化操作"}</strong></li>
        </ul>
      </section>
    </aside>
  );
}

function AttemptCompare({ attempt, scene, onClose, onRewrite }) {
  const cmp = attempt.cmp || {};
  return (
    <div className="scn2-cmp" role="dialog" aria-modal="true">
      <div className="scn2-cmp-card">
        <header className="scn2-cmp-head">
          <div className="scn2-cmp-title">
            <span className="scn2-cmp-n tab-num">尝试 #{attempt.n}</span>
            <span className={`scn2-try-tag tone-${attempt.tone}`}>{attempt.result === "running" ? "进行中" : attempt.result}</span>
            <span className="scn2-cmp-time">{attempt.time}</span>
          </div>
          <button className="scn2-cmp-x" onClick={onClose} aria-label="关闭"><I.X size={16} /></button>
        </header>

        <div className="scn2-cmp-body scn2-scroll">
          <div className="scn2-cmp-verdict">
            <span className="scn2-cmp-vdot tone-slate" />
            <p>这里只保留该次尝试的复盘摘要，不包含可恢复的历史正文快照。重写始终以当前稿为输入，仅把这份复盘意见加入作者指令。</p>
          </div>
          {cmp.verdict && (
            <div className="scn2-cmp-verdict">
              <span className={`scn2-cmp-vdot tone-${attempt.tone}`} />
              <p>{cmp.verdict}</p>
            </div>
          )}

          {cmp.metrics && cmp.metrics.length > 0 && (
            <div className="scn2-cmp-metrics">
              <div className="scn2-cmp-sub">指标变化 · 该版 → 本次</div>
              <div className="scn2-cmp-mgrid">
                {cmp.metrics.map((m, i) => (
                  <div key={i} className="scn2-cmp-metric">
                    <span className="scn2-cmp-mlabel">{m.label}</span>
                    <span className="scn2-cmp-mflow">
                      <span className="scn2-cmp-was tab-num">{m.was}</span>
                      <span className={`scn2-cmp-arrow ${m.better ? "good" : "bad"}`}>{m.better ? "↓" : "↑"}</span>
                      <span className="scn2-cmp-now tab-num">{m.now}</span>
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {cmp.before && (
            <div className="scn2-cmp-diff">
              <div className="scn2-cmp-col is-before">
                <div className="scn2-cmp-coltag">该版留存的问题段摘要 · #{attempt.n}</div>
                <p className="text-serif scn2-cmp-text">{cmp.before.text}</p>
                {cmp.before.risk && <div className="scn2-cmp-risk"><I.AlertTriangle size={12} /> {cmp.before.risk}</div>}
              </div>
              <div className="scn2-cmp-arrow-col"><I.ArrowRight size={16} /></div>
              <div className="scn2-cmp-col is-after">
                <div className="scn2-cmp-coltag">本次 · 当前版</div>
                <p className="text-serif scn2-cmp-text">{cmp.after.text}</p>
                <div className="scn2-cmp-fixed"><I.Check size={12} /> 已修正</div>
              </div>
            </div>
          )}

          {!cmp.metrics && !cmp.before && (
            <div className="scn2-cmp-empty">该版未保留逐段记录，仅留结论与起草日志。</div>
          )}
        </div>

        <footer className="scn2-cmp-foot">
          <span className="scn2-cmp-hint">参考该版复盘意见重写；不会恢复或把历史版本当作真实 base</span>
          <div className="flex gap-2">
            <button className="btn btn-quiet btn-sm" onClick={onClose}>关闭</button>
            <button className="btn btn-ghost btn-sm" data-testid="scene-attempt-rewrite" onClick={onRewrite} disabled={!onRewrite}
              title={onRewrite ? "仅把该版复盘意见写入作者指令；真实输入仍是当前稿" : "演示尝试不可提交真实重跑"}>
              <I.Refresh size={13} /> {onRewrite ? "参考该版复盘意见重写" : "演示版本 · 只读"}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}

/* ============================ 选场入列 ============================ */
function ScenePicker({ queued, onPick, onClose }) {
  const chs = scnPickList ? scnPickList(queued) : [];
  const stLabel = { done: "已完成", writing: "在写", todo: "待写" };
  const batchOf = (c) => c.scenes.filter(s => !s.queued && s.sid && s.state !== "done");
  return (
    <div className="scn2-cmp" role="dialog" aria-modal="true">
      <div className="scn2-cmp-card" style={{ maxWidth: 620 }}>
        <header className="scn2-cmp-head">
          <div className="scn2-cmp-title">
            <span className="scn2-cmp-n">加入场景</span>
            <span className="scn2-cmp-time">从章节目录挑一场交给 AI 起草</span>
          </div>
          <button className="scn2-cmp-x" onClick={onClose} aria-label="关闭"><I.X size={16} /></button>
        </header>
        <div className="scn2-cmp-body scn2-scroll" style={{ display: "grid", gap: 14 }}>
          {!chs.length && (
            <div className="scn2-cmp-empty">章节目录还是空的——先在构思的「下游交付」把雪花整理成章节结构，或去章节编排建章。</div>
          )}
          {chs.map(c => {
            const batch = batchOf(c);
            return (
            <div key={c.id}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                <span style={{ fontSize: 12, fontWeight: 700, color: "var(--ink-3)", flex: 1 }}>第 {c.n} 章 · {c.title}</span>
                {batch.length > 1 && (
                  <button className="btn btn-quiet btn-sm" onClick={() => batch.forEach(s => onPick(s.sid, true))} title="把本章未完成的场全部入列（不自动起草，逐场点开始）">
                    <I.Plus size={12} /> 整章入列 · {batch.length} 场
                  </button>
                )}
              </div>
              <div style={{ display: "grid", gap: 6 }}>
                {c.scenes.map(s => (
                  <button key={s.sid} className="scn2-pick-row" disabled={s.queued} onClick={() => onPick(s.sid)}>
                    <span className={`scn2-pick-kind ${s.kind === "反应" ? "is-rea" : "is-pro"}`}>{s.kind || "主动"}</span>
                    <span className="scn2-pick-title">{s.title}</span>
                    {s.hasDraft && <span className="scn2-pick-draft" title="这一场已有 AI 稿（入列后可直接裁决或重跑）"><I.Sparkles size={11} /> 有 AI 稿</span>}
                    {!s.ready && <span className="scn2-pick-warn" title="场景卡的目标还是占位——起草质量会打折"><I.AlertTriangle size={11} /> 卡未填全</span>}
                    <span className="scn2-pick-st">{s.queued ? "已在队列" : (stLabel[s.state] || s.state || "—")}</span>
                  </button>
                ))}
              </div>
            </div>
            );
          })}
        </div>
        <footer className="scn2-cmp-foot">
          <span className="scn2-cmp-hint">入列后点「开始起草」：Claude 读雪花构思 + 场景卡起草，过本地质检后由你裁决</span>
          <button className="btn btn-quiet btn-sm" onClick={onClose}>关闭</button>
        </footer>
      </div>
      <style>{`
.scn2-pick-row { display: flex; align-items: center; gap: 10px; width: 100%; text-align: left; padding: 9px 12px; border: 1px solid var(--line-1, #ddd); border-radius: 10px; background: var(--paper-0, #fff); cursor: pointer; font: inherit; }
.scn2-pick-row:hover:not([disabled]) { border-color: var(--ink-3); }
.scn2-pick-row[disabled] { opacity: 0.55; cursor: default; }
.scn2-pick-kind { flex: 0 0 auto; font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 999px; }
.scn2-pick-kind.is-pro { background: var(--crimson-wash); color: var(--crimson); }
.scn2-pick-kind.is-rea { background: var(--paper-1, #f3f2ef); color: var(--ink-2); }
.scn2-pick-title { flex: 1; min-width: 0; font-size: 13px; color: var(--ink-1); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.scn2-pick-warn { display: inline-flex; align-items: center; gap: 3px; font-size: 10.5px; font-weight: 700; color: var(--gold); flex: 0 0 auto; }
.scn2-pick-draft { display: inline-flex; align-items: center; gap: 3px; font-size: 10.5px; font-weight: 700; color: var(--sage); flex: 0 0 auto; }
.scn2-pick-st { flex: 0 0 auto; font-size: 11px; color: var(--ink-3); }
      `}</style>
    </div>
  );
}

/* 运行队列：从章节目录加场入列——真实起草：读雪花构思 + 场景卡，质检后写回正文。 */
function WsScene(props) {
  const hasCatalog = (() => { try { return WsCatalog && WsCatalog.get().length > 0; } catch (e) { return false; } })();
  if (hasCatalog) return <WsSceneBoard {...props} />;
  const work = WsWorks ? WsWorks.active() : { title: "这部作品" };
  return (
    <div className="page" data-screen-label="scene · empty">
      <div style={{ display: "grid", placeItems: "center", minHeight: "70vh", textAlign: "center" }}>
        <div style={{ maxWidth: 460, display: "grid", gap: 14, justifyItems: "center" }}>
          <div style={{ fontFamily: "var(--font-serif)", fontSize: 22, color: "var(--ink-1)" }}>《{work.title}》还没有章节目录</div>
          <p style={{ color: "var(--ink-3)", fontSize: 14, lineHeight: 1.9, margin: 0 }}>
            AI 起草台按场景卡逐场起草：预检 → 起草 → 质检 → 裁决 → 写回正文。
            先在构思里把雪花「整理成章节结构」，或去章节编排建章、填好场景卡，再回来入列。
          </p>
          <div style={{ display: "flex", gap: 10 }}>
            <button className="btn btn-accent" onClick={() => props.go && props.go("snowflake")}><I.Layout size={15} /> 去构思·下游交付</button>
            <button className="btn btn-ghost" onClick={() => props.go && props.go("author")}>去章节编排</button>
          </div>
        </div>
      </div>
    </div>
  );
}

/* 场景工作台只通过显式 ESM 导出。 */
export { WsScene, SceneTweaks };

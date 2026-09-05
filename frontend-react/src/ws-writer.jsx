import React from "react";
import { I } from "./icons.jsx";
import { storeAlert } from "./lib/store-utils.js";
import { WsCatalog, WsTrashStore } from "./ws-catalog.jsx";
import { WrDocs } from "./wr-doc-store.jsx";
import { WrCanonicalControl } from "./wr-canonical-control.jsx";
import { ContentSafetyReviewDialog, contentSafetyReviewFromError } from "./wr-content-safety-review.jsx";
import { wsKey, WsWorks } from "./ws-works.jsx";
import {
  wrDeepUnmark, wrDeepScan, wrDxLog, wrDeepMark, wrDeepAdopt,
  wrDxPushLog, wrDxAddSkip, wrDxClearSkips, wrDxSnapshot,
  wrDxApplyPreferences, wrDxMergePreferences, wrDxLoadPreferences, wrDxSavePreferences, WrDeepDrawer,
} from "./ws-deep.jsx";
import { UndoToast, useUndoToast } from "./ws-undo-toast.jsx";
import { onRovingTabKeyDown } from "./a11y-tabs.js";
import { apiGet, apiPatch, apiPost } from "./lib/client.js";
import { WriterTweaks, WRITER_TWEAK_DEFAULTS } from "./ws-shell-tweaks.jsx";
import { navigateWithViewIntent, setViewIntentTargetReady } from "./ws-view-intents.js";
import { sanitizeManuscriptHTML } from "./manuscript-html.js";
import { wrPickedText, wrPlainText, wrSentences } from "./writer-candidates.js";

/* global React, I */
/* ==========================================================
   WriterRoom — 写作房间 (reusable component)
   Refactored from the standalone prototype into a component that
   reads tweak values from props (t, setTweak). Exposed on window
   so both the standalone bootstrap and the unified shell can use it.
   ========================================================== */
const { useState: useWS, useEffect: useWE, useRef: useWR, useCallback: useWC } = React;

/* mm:ss session clock */
function fmtElapsed(s) {
  const m = Math.floor(s / 60), ss = s % 60;
  return `${m}:${String(ss).padStart(2, "0")}`;
}

/* small goal-progress ring shown in the top stat cluster */
function GoalRing({ pct }) {
  const r = 9, c = 2 * Math.PI * r;
  const off = c * (1 - Math.min(100, pct) / 100);
  return (
    <svg className="wr-ring" width="22" height="22" viewBox="0 0 22 22" aria-hidden="true">
      <circle className="wr-ring-bg" cx="11" cy="11" r={r} strokeWidth="2.5" />
      <circle className={`wr-ring-fill ${pct >= 100 ? "is-full" : ""}`} cx="11" cy="11" r={r} strokeWidth="2.5"
        strokeDasharray={c} strokeDashoffset={off} />
    </svg>
  );
}

function wrLoadRail() {
  try { const s = JSON.parse(localStorage.getItem("wr-rail")); if (s && s.l && s.r) return { l: s.l, r: s.r }; } catch (e) {}
  return { l: 224, r: 264 };
}


/* ---- 正文持久化：每个场景一份文档，按作品隔离 ---- */
const wrDocKey = (sid) => (wsKey ? wsKey("wr-doc:" + sid) : "wr-doc:" + sid);
const WR_DOC_PLACEHOLDER = "<p>在这里开始写这一场……</p>";
function wrSeedHTML(sid) {
  return WR_DOC_PLACEHOLDER;
}
function wrCountOf(el) { return el ? el.innerText.replace(/\s/g, "").length : 0; }
/* 落盘前去掉深改姿态的诊断标注，保证 wr-doc 始终是干净正文 */
function wrCleanHTML(el) {
  if (!el) return "";
  if (!el.querySelector || !el.querySelector("mark.wr-dx, .wr-dx-para")) return el.innerHTML;
  const clone = el.cloneNode(true);
  if (wrDeepUnmark) wrDeepUnmark(clone);
  return clone.innerHTML;
}

/* 目录 → 大纲形状（写作器本地渲染用） */
function wrFromCatalog() {
  const cat = WsCatalog ? WsCatalog.get() : null;
  if (!cat) return [];
  return cat.map(c => ({
    id: c.id, n: c.n, title: c.title, state: c.state,
    expanded: !!(c.current || c.state === "writing"),
    scenes: (c.scenes || []).map(s => ({ id: s.sid, title: s.title, state: s.state === "writing" ? "active" : (s.state || "todo") })),
  }));
}
function wrInitialScene() {
  if (WsCatalog) { const w = WsCatalog.writingScene(); return w ? w.scene.sid : null; }
  return null;
}

/* ==========================================================
   档案实体高亮 — 把正文中已登记的人物/地点/术语标出，点击直达档案。
   匹配档案名 + 少量别名（正文里的口语指代）。运行时读取 window.LIB_*。
   ========================================================== */
function wrEntityMatchers() {
  /* 与资料库同源：种子按作品门控 + 用户新建 + 编辑覆盖 */
  const live = window.LIB_live ? window.LIB_live() : { entries: window.LIB_ENTRIES || [], byId: window.LIB_BY_ID || {} };
  const ents = live.entries;
  const byId = live.byId;
  const idOf = {};
  ents.forEach(e => { if (e.name && e.name.length >= 2 && !(e.name in idOf)) idOf[e.name] = e.id; });
  return idOf;
}
function wrHighlightEntities(root) {
  if (!root) return;
  const idOf = wrEntityMatchers();
  const names = Object.keys(idOf).sort((a, b) => b.length - a.length);
  if (!names.length) return;
  const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const rx = new RegExp("(" + names.map(esc).join("|") + ")", "g");
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
      let p = node.parentElement;
      while (p && p !== root) {
        if (p.classList && (p.classList.contains("wr-entity") || p.classList.contains("wr-anno"))) return NodeFilter.FILTER_REJECT;
        p = p.parentElement;
      }
      rx.lastIndex = 0;
      return rx.test(node.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });
  const targets = []; let n;
  while ((n = walker.nextNode())) targets.push(n);
  targets.forEach(node => {
    rx.lastIndex = 0;
    const txt = node.nodeValue;
    const frag = document.createDocumentFragment();
    let last = 0, m;
    while ((m = rx.exec(txt))) {
      if (m.index > last) frag.appendChild(document.createTextNode(txt.slice(last, m.index)));
      const span = document.createElement("span");
      span.className = "wr-entity";
      span.setAttribute("data-lib-id", idOf[m[0]]);
      span.textContent = m[0];
      frag.appendChild(span);
      last = m.index + m[0].length;
    }
    if (last < txt.length) frag.appendChild(document.createTextNode(txt.slice(last)));
    node.parentNode.replaceChild(frag, node);
  });
}

/* AI 续写候选的展示外观（真实候选无模型自评的"手法标签"，仅用于视觉区分/轮换） */
const WR_CAND_TONES = ["slate", "crimson", "gold"];

/* ==========================================================
   Sentence-level adoption — split a candidate into sentences so the
   writer can cherry-pick just the lines they want. Marks (<mark>) are
   preserved per-sentence; adopted text is stripped to plain prose.
   ========================================================== */

function wrSceneIsApproved(sceneId) {
  if (!sceneId || !WsCatalog) return false;
  try {
    const hit = WsCatalog.sceneById(sceneId);
    return !!(hit && hit.chapter && hit.chapter.state === "approved");
  } catch (e) {
    return false;
  }
}

function canonicalPromotionErrorMessage(error) {
  const code = error && error.code;
  if (code === "CANONICAL_BASE_CONFLICT" || code === "AUTHOR_DRAFT_CONFLICT") {
    return "草稿或权威正文已在别处更新。请刷新、比较最新版本后再提升。";
  }
  if (code === "CANONICAL_NARRATIVE_RECONCILIATION_REQUIRED") {
    return "这次修改涉及故事事实，必须先核对叙事事件，系统不会静默沿用旧事实。";
  }
  if (code === "CHAPTER_APPROVED_LOCKED") {
    return "该章节已批准锁定，需要先明确重开章节，才能更新权威正文。";
  }
  if (code === "CONTENT_SAFETY_REVIEW_REQUIRED") {
    return "服务端要求内容风险复核，但没有返回可逐项确认的项目。系统已停止提升，请刷新后重试。";
  }
  return "权威正文提升失败，草稿仍安全保留。请稍后重试。";
}
function WrCandText({ html, picked, onToggle }) {
  const sents = wrSentences(html);
  return (
    <p className="wr-cand-text">
      {sents.map((sen, si) => (
        <span key={si}
          className={`wr-sen ${picked.includes(si) ? "is-pick" : ""}`}
          onClick={(e) => { e.stopPropagation(); onToggle(si); }}
          dangerouslySetInnerHTML={{ __html: sen }} />
      ))}
    </p>
  );
}

/* ==========================================================
   Per-scene context — the right rail reads from this so 戏剧弧 / QC /
   画像 update as the writer moves between scenes in the outline.
   Scenes without rich data fall back to a sensible skeleton.
   ========================================================== */
function wrCtx(sceneId) {
  const hit = WsCatalog ? WsCatalog.sceneById(sceneId) : null;
  if (hit) {
    const c = hit.chapter, s = hit.scene;
    return {
      pov: c.pov ? c.pov + "（限知）" : "—", time: c.time || "—", place: c.place || "—", type: (s.kind || "主动") + "场景",
      gmc: { goal: s.goal || "（本场目标待规划）", conflict: s.obstacle || "（阻碍待规划）", setback: s.turn || "（挑战待规划）" },
      cast: c.pov ? [[c.pov, "POV"]] : [],
      risks: [],
      portrait: [["短句率", 0, 70, false], ["动词驱动", 0, 65, false], ["具象意象", 0, 80, false]],
    };
  }
  return {
    pov: "—", time: "—", place: "—", type: "主动场景",
    gmc: { goal: "（本场目标待规划）", conflict: "（阻碍待规划）", setback: "（挫折待规划）" },
    cast: [],
    risks: [],
    portrait: [["短句率", 0, 70, false], ["动词驱动", 0, 65, false], ["具象意象", 0, 80, false]],
  };
}

/* ==========================================================
   WriterRoom
   ========================================================== */
function WriterRoom({ t, setTweak, onExit, go }) {
  const tw = { ...WRITER_TWEAK_DEFAULTS, ...(t || {}) };

  const [activeScene, setActiveScene] = useWS(wrInitialScene);
  const { toast, show: showNotice, clear: clearNotice } = useUndoToast();
  /* FE-ALIGN G4 授权接缝：当前在写场景镜像到模块级（inline rewrite 的后端定位） */
  useWE(() => { WR_ACTIVE_SID = activeScene; return () => { WR_ACTIVE_SID = null; }; }, [activeScene]);
  const [entityPop, setEntityPop] = useWS(null);
  const [mention, setMention] = useWS(null);     /* @ 档案选择器 { query, x, y } */
  const [mentionIdx, setMentionIdx] = useWS(0);
  const mentionCtx = useWR(null);
  const [chapters, setChapters] = useWS(wrFromCatalog);
  const activeChapter = chapters.find((chapter) => (chapter.scenes || []).some((scene) => scene.id === activeScene));
  const approvedLocked = !!(activeChapter && activeChapter.state === "approved");
  const catalogLoadError = WsCatalog && WsCatalog.loadError ? WsCatalog.loadError() : null;
  const catalogUnavailable = !!(WsCatalog && WsCatalog.ready && !WsCatalog.ready() && catalogLoadError);
  const catalogLoading = !!(WsCatalog && WsCatalog.ready && !WsCatalog.ready() && !catalogLoadError);

  /* FE-ALIGN P3：目录改为后端异步装载 —— 冷启动直达写作器（刷新停在 #writer）时
     activeScene 可能初始化为 null；目录就绪后自动选中在写场景并刷新章节树 */
  useWE(() => {
    if (!WsCatalog) return;
    const sync = () => {
      setChapters(wrFromCatalog());
      setActiveScene(prev => {
        // 切换作品/目录重载时，旧作品的 scene id 绝不能继续留在编辑器里。
        if (prev && WsCatalog.sceneById(prev)) return prev;
        const w = WsCatalog.writingScene();
        return w && w.scene ? w.scene.sid : null;
      });
    };
    const un = WsCatalog.subscribe(sync);
    if (!activeScene) sync();
    return un;
  }, []); // eslint-disable-line
  const deskWideInit = () => tw.wrLayout !== "immersive" && typeof window !== "undefined" && !window.matchMedia("(max-width: 999px)").matches;
  const [leftOpen, setLeftOpen] = useWS(deskWideInit);
  const [rightOpen, setRightOpen] = useWS(deskWideInit);
  const [rightTab, setRightTab] = useWS("scene");
  const [trayOpen, setTrayOpen] = useWS(false);
  const [immersion, setImmersion] = useWS(false);
  const [chrome, setChrome] = useWS(true);
  const [wordCount, setWordCount] = useWS(0);
  const [session, setSession] = useWS(2140);
  const [saved, setSaved] = useWS("草稿已保存");
  const [savedAt, setSavedAt] = useWS(null);
  const [canonicalStatus, setCanonicalStatus] = useWS("unknown");
  const [contentSafetyReview, setContentSafetyReview] = useWS(null);
  const [contentSafetyBusy, setContentSafetyBusy] = useWS(false);
  const [contentSafetyError, setContentSafetyError] = useWS("");
  const [nextCue, setNextCue] = useWS(false);
  const [nextDismissed, setNextDismissed] = useWS(false);
  const [snowSrc, setSnowSrc] = useWS(null);
  const [elapsed, setElapsed] = useWS(0);
  const [narrow, setNarrow] = useWS(() => typeof window !== "undefined" && window.matchMedia("(max-width: 999px)").matches);

  /* 姿态：起草 / 深改（原独立深改台并入后的第二姿态） */
  const [posture, setPosture] = useWS("draft");
  const [dxIssues, setDxIssues] = useWS([]);
  const [dxActive, setDxActive] = useWS(null);
  const [dxLog, setDxLog] = useWS([]);
  const [dxPersistenceStatus, setDxPersistenceStatus] = useWS("idle");
  const dxPreferencesRef = useWR({ sceneId: null, revisionNo: 0, saveChain: Promise.resolve() });
  const dxUndoRef = useWR(null);   // { sid, html } 采纳前的快照，一步撤销
  useWE(() => {
    if (approvedLocked && posture === "deep") setPosture("draft");
  }, [approvedLocked, posture]);

  const isDesk = tw.wrLayout === "desk";
  const coexist = isDesk && !narrow; // both rails may sit open side-by-side

  const [railL, setRailL] = useWS(() => wrLoadRail().l);
  const [railR, setRailR] = useWS(() => wrLoadRail().r);
  const [dragging, setDragging] = useWS(false);
  const dragRef = useWR(null);
  const onRailMove = useWC((e) => {
    const d = dragRef.current; if (!d) return;
    const dx = e.clientX - d.startX;
    if (d.side === "l") setRailL(Math.max(200, Math.min(360, d.startL + dx)));
    else setRailR(Math.max(200, Math.min(360, d.startR - dx)));
  }, []);
  const onRailUp = useWC(() => {
    dragRef.current = null; setDragging(false);
    window.removeEventListener("pointermove", onRailMove);
    window.removeEventListener("pointerup", onRailUp);
    document.body.style.cursor = ""; document.body.style.userSelect = "";
  }, [onRailMove]);
  const startRail = (side) => (e) => {
    e.preventDefault();
    dragRef.current = { side, startX: e.clientX, startL: railL, startR: railR };
    setDragging(true);
    window.addEventListener("pointermove", onRailMove);
    window.addEventListener("pointerup", onRailUp);
    document.body.style.cursor = "col-resize"; document.body.style.userSelect = "none";
  };
  const resetRail = (side) => (side === "l" ? setRailL(224) : setRailR(264));
  useWE(() => { try { localStorage.setItem("wr-rail", JSON.stringify({ l: railL, r: railR })); } catch (e) {} }, [railL, railR]);

  const editorRef = useWR(null);
  const scrollRef = useWR(null);
  const saveTimer = useWR(null);
  const hideTimer = useWR(null);
  const baselineRef = useWR(0);   // 场景载入时的字数基线，增量回写用
  const dirtyRef = useWR(false);  // 有未落盘的改动
  const editVersionRef = useWR(0);
  const mountedRef = useWR(false);
  const activeSceneRef = useWR(activeScene);
  activeSceneRef.current = activeScene;
  useWE(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  /* 真·自动保存：正文落盘 + 字数增量回写目录/作品 */
  const persistDoc = useWC(async () => {
    const el = editorRef.current;
    if (!el || !activeScene) return false;
    if (wrSceneIsApproved(activeScene)) {
      dirtyRef.current = false;
      setSaved("终稿已锁定");
      return false;
    }
    const sceneId = activeScene;
    const html = wrCleanHTML(el);
    const count = wrCountOf(el);
    const editVersion = editVersionRef.current;
    const baseline = baselineRef.current;
    /* FE-ALIGN P3：正文落 author-drafts 主路径（WrDocs 缓存写通 + PATCH），
       字数 rollup 由保存响应回流目录/统计 */
    try {
      await WrDocs.save(sceneId, html);
      if (WsCatalog) { try { WsCatalog.recordSceneWords(sceneId, count, baseline); } catch (e) {} }
      const stillCurrent = mountedRef.current && activeSceneRef.current === sceneId;
      if (stillCurrent) baselineRef.current = count;
      if (stillCurrent && editVersionRef.current === editVersion) {
        dirtyRef.current = false;
        setSaved("草稿已保存"); setSavedAt(Date.now());
        const state = WrDocs.state(sceneId);
        setCanonicalStatus(state && state.canonicalDirty === false ? "current" : "dirty");
      }
      return true;
    } catch (e) {
      const stillCurrent = mountedRef.current && activeSceneRef.current === sceneId;
      if (stillCurrent && editVersionRef.current === editVersion) {
        dirtyRef.current = true;
        setSaved("草稿保存失败");
      }
      return false;
    }
  }, [activeScene]);
  const schedulePersist = useWC(() => {
    if (wrSceneIsApproved(activeScene)) return;
    setSaved("正在保存草稿…");
    setCanonicalStatus("dirty");
    dirtyRef.current = true;
    editVersionRef.current += 1;
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => { void persistDoc(); }, 900);
  }, [persistDoc]);
  /* 离开场景 / 卸载前把未落盘的改动冲掉 */
  const persistRef = useWR(persistDoc);
  persistRef.current = persistDoc;
  useWE(() => () => { clearTimeout(saveTimer.current); }, []);

  const promoteCanonical = useWC(async () => {
    if (!activeScene) return;
    if (wrSceneIsApproved(activeScene)) {
      storeAlert(null, "本场属于已批准终稿。请先到成稿中心重新打开章节，再修改正文。");
      return;
    }
    clearTimeout(saveTimer.current);
    if (dirtyRef.current) {
      setSaved("正在保存草稿…");
      const savedOk = await persistDoc();
      if (!savedOk) {
        storeAlert(null, "草稿尚未保存到服务端，不能提升为权威正文。请先重试保存。");
        return;
      }
    }
    let confirmed = false;
    try {
      confirmed = window.confirm(
        "请确认：本次修改只调整文字表达，没有改变人物、时间、地点、物品或剧情结果等故事事实。\n\n" +
        "若事实有变化，请取消并先完成事实核对；当前版本不会静默沿用旧事件。",
      );
    } catch (e) { confirmed = false; }
    if (!confirmed) return;

    setCanonicalStatus("promoting");
    try {
      await WrDocs.promote(activeScene, { narrativeEffect: "facts_unchanged" });
      setCanonicalStatus("current");
      storeAlert(null, "草稿已提升为权威正文，场景记忆与章节汇总已同步重建。");
    } catch (e) {
      const code = e && e.code;
      if (code === "CONTENT_SAFETY_REVIEW_REQUIRED") {
        const review = contentSafetyReviewFromError(e);
        if (review) {
          setContentSafetyReview({ ...review, sid: activeScene, acceptedCodes: [] });
          setContentSafetyError("");
          setCanonicalStatus("review");
          return;
        }
      }
      setCanonicalStatus(code === "CANONICAL_NARRATIVE_RECONCILIATION_REQUIRED" ? "reconcile" : "error");
      try { window.alert(canonicalPromotionErrorMessage(e)); } catch (ignored) {}
    }
  }, [activeScene, persistDoc]);

  const cancelContentSafetyReview = useWC(() => {
    if (contentSafetyBusy) return;
    setContentSafetyReview(null);
    setContentSafetyError("");
    setCanonicalStatus("dirty");
  }, [contentSafetyBusy]);

  const confirmContentSafetyReview = useWC(async (acceptedCodes) => {
    const review = contentSafetyReview;
    if (!review || contentSafetyBusy) return;
    const exactCodes = review.findings.map(item => item.code);
    const received = Array.isArray(acceptedCodes) ? acceptedCodes : [];
    if (received.length !== exactCodes.length || exactCodes.some((code, index) => received[index] !== code)) {
      setContentSafetyError("确认项与服务端返回的风险代码不一致，已停止提升。");
      return;
    }
    setContentSafetyBusy(true);
    setContentSafetyError("");
    setCanonicalStatus("promoting");
    const acceptedForRetry = [...new Set([...(review.acceptedCodes || []), ...exactCodes])];
    try {
      await WrDocs.promote(review.sid, {
        narrativeEffect: "facts_unchanged",
        acceptedWarningCodes: acceptedForRetry,
      });
      setContentSafetyReview(null);
      setCanonicalStatus("current");
      storeAlert(null, "已按你的逐项确认重新校验；草稿已提升为权威正文。");
    } catch (error) {
      if (error && error.code === "CONTENT_SAFETY_REVIEW_REQUIRED") {
        const nextReview = contentSafetyReviewFromError(error);
        if (nextReview) {
          setContentSafetyReview({ ...nextReview, sid: review.sid, acceptedCodes: acceptedForRetry });
          setContentSafetyError("服务端返回了仍需复核的项目。系统没有沿用上次勾选，请重新逐项核对。");
          setCanonicalStatus("review");
          return;
        }
      }
      if (error && (error.retryable || error.code === "NETWORK_ERROR" || Number(error.status) >= 500)) {
        setContentSafetyError(`${canonicalPromotionErrorMessage(error)} 你可以保持本页并重试。`);
        setCanonicalStatus("review");
      } else {
        setContentSafetyReview(null);
        setCanonicalStatus(error && error.code === "CANONICAL_NARRATIVE_RECONCILIATION_REQUIRED" ? "reconcile" : "error");
        try { window.alert(canonicalPromotionErrorMessage(error)); } catch (ignored) {}
      }
    } finally {
      setContentSafetyBusy(false);
    }
  }, [contentSafetyReview, contentSafetyBusy]);

  useWE(() => {
    const inField = (el) => el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA");
    const onKey = (e) => {
      const meta = e.metaKey || e.ctrlKey;
      if (meta && e.key.toLowerCase() === "j") { e.preventDefault(); openAI(); }
      else if (meta && e.key === ".") { e.preventDefault(); setImmersion(v => !v); }
      else if (meta && e.key === "1") { e.preventDefault(); setLeftOpen(v => !v); if (!coexist) setRightOpen(false); }
      else if (meta && e.key === "2") { e.preventDefault(); setRightOpen(v => !v); if (!coexist) setLeftOpen(false); }
      else if (e.key === "Escape") {
        if (trayOpen) setTrayOpen(false);
        else if (leftOpen || rightOpen) { setLeftOpen(false); setRightOpen(false); }
        else if (immersion) setImmersion(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [trayOpen, leftOpen, rightOpen, immersion, coexist]);

  // session clock — ticks for the life of the writing session
  useWE(() => { const id = setInterval(() => setElapsed(e => e + 1), 1000); return () => clearInterval(id); }, []);

  // responsive: below 880px the desk rails fall back to summon-and-dissolve overlays
  useWE(() => {
    const mq = window.matchMedia("(max-width: 999px)");
    const on = () => setNarrow(mq.matches);
    on();
    mq.addEventListener ? mq.addEventListener("change", on) : mq.addListener(on);
    return () => { mq.removeEventListener ? mq.removeEventListener("change", on) : mq.removeListener(on); };
  }, []);

  // desk layout on a wide screen keeps both rails docked open; otherwise collapse them
  useWE(() => {
    if (isDesk && !narrow) { setLeftOpen(true); setRightOpen(true); }
    else { setLeftOpen(false); setRightOpen(false); }
  }, [isDesk, narrow]);

  useWE(() => {
    if (!immersion) { setChrome(true); clearTimeout(hideTimer.current); return; }
    const reveal = () => { setChrome(true); clearTimeout(hideTimer.current); hideTimer.current = setTimeout(() => setChrome(false), 2600); };
    reveal();
    window.addEventListener("mousemove", reveal);
    window.addEventListener("keydown", reveal);
    return () => { window.removeEventListener("mousemove", reveal); window.removeEventListener("keydown", reveal); clearTimeout(hideTimer.current); };
  }, [immersion]);

  useWE(() => {
    const el = editorRef.current;
    if (!el) return;
    if (!activeScene) { el.innerHTML = ""; setWordCount(0); setCanonicalStatus("unknown"); return; }
    editVersionRef.current += 1;
    dirtyRef.current = false;
    setSaved("草稿已加载");
    const initialState = WrDocs.state(activeScene);
    setCanonicalStatus(initialState && initialState.canonicalDirty === false ? "current" : "dirty");
    /* FE-ALIGN P3：同步读 WrDocs 缓存（兼容旧 wr-doc 本地键），后台水合服务端草稿 */
    let stored = null;
    try { stored = WrDocs.load(activeScene); } catch (e) {}
    el.innerHTML = sanitizeManuscriptHTML(stored != null ? stored : wrSeedHTML(activeScene));
    wrHighlightEntities(el);
    baselineRef.current = wrCountOf(el);
    recount();
    const activeFrame = requestAnimationFrame(updateActive);
    let locateFrame = null;
    if (pendingEntity.current) {
      const id = pendingEntity.current; pendingEntity.current = null;
      locateFrame = requestAnimationFrame(() => locateEntity(id));
    }
    /* 服务端草稿水合完成且本地无未保存改动 → 回填编辑器（跨浏览器以服务端为准） */
    const onDocLoaded = (e) => {
      if (!e || e.detail !== activeScene || dirtyRef.current) return;
      let fresh = null;
      try { fresh = WrDocs.load(activeScene); } catch (e2) {}
      if (fresh != null && el.innerHTML !== fresh) {
        el.innerHTML = sanitizeManuscriptHTML(fresh);
        wrHighlightEntities(el);
        baselineRef.current = wrCountOf(el);
        recount();
      }
      setSaved("草稿已保存");
      const state = WrDocs.state(activeScene);
      setCanonicalStatus(state && state.canonicalDirty === false ? "current" : "dirty");
    };
    const onDocState = (e) => {
      if (!e || !e.detail || e.detail.sid !== activeScene) return;
      if (e.detail.lastSaveError) setSaved("草稿保存失败");
      else if (!e.detail.dirty) setSaved("草稿已保存");
      setCanonicalStatus(e.detail.canonicalDirty === false ? "current" : "dirty");
    };
    window.addEventListener("ws:wr-doc-loaded", onDocLoaded);
    window.addEventListener("ws:wr-doc-state", onDocState);
    /* 离开这个场景（或卸载）时，把未落盘的改动用「当时的」场景 id 冲掉 */
    const sid = activeScene;
    return () => {
      window.removeEventListener("ws:wr-doc-loaded", onDocLoaded);
      window.removeEventListener("ws:wr-doc-state", onDocState);
      cancelAnimationFrame(activeFrame);
      if (locateFrame != null) cancelAnimationFrame(locateFrame);
      clearTimeout(saveTimer.current);
      if (dirtyRef.current && el && sid && !wrSceneIsApproved(sid)) {
        try { void WrDocs.save(sid, wrCleanHTML(el)).catch(() => {}); } catch (e) {}
        try { WsCatalog && WsCatalog.recordSceneWords(sid, wrCountOf(el), baselineRef.current); } catch (e) {}
        dirtyRef.current = false;
      }
    };
  }, [activeScene]);

  /* 反向链路：从档案「在正文中定位」跳来，滚动并高亮对应实体 */
  useWE(() => {
    const tryLocate = (id) => {
      if (!id) return;
      if (locateEntity(id)) return;
      pendingEntity.current = id;
      setActiveScene(prev => {
        const w = WsCatalog ? WsCatalog.writingScene() : null;
        const tgt = w ? w.scene.sid : "ch08s3";
        return prev === tgt ? prev : tgt;
      });
    };
    const h = (e) => tryLocate(e.detail);
    window.addEventListener("ws:writer-locate", h);
    return () => window.removeEventListener("ws:writer-locate", h);
  }, []);

  /* 实体高亮交互：悬停看摘要，点击直达档案 */
  const pendingEntity = useWR(null);
  const locateEntity = useWC((id) => {
    const el = editorRef.current, st = scrollRef.current;
    if (!el || !st) return false;
    const sp = el.querySelector('.wr-entity[data-lib-id="' + id + '"]');
    if (!sp) return false;
    const r = sp.getBoundingClientRect(), sr = st.getBoundingClientRect();
    st.scrollTo({ top: Math.max(0, st.scrollTop + (r.top - sr.top) - sr.height / 2 + r.height / 2), behavior: "smooth" });
    sp.classList.add("wr-entity-flash");
    setTimeout(() => sp.classList.remove("wr-entity-flash"), 1500);
    return true;
  }, []);
  const openDossier = (id) => {
    if (!id) return;
    setEntityPop(null);
    navigateWithViewIntent("library", "ws:lib-open", id);
  };
  const onEditorOver = (e) => {
    const sp = e.target.closest && e.target.closest(".wr-entity");
    if (sp) { const r = sp.getBoundingClientRect(); setEntityPop({ id: sp.getAttribute("data-lib-id"), x: r.left + r.width / 2, top: r.top, bottom: r.bottom }); }
  };
  const onEditorOut = (e) => {
    const sp = e.target.closest && e.target.closest(".wr-entity");
    if (sp) { const to = e.relatedTarget; if (!to || !to.closest || !to.closest(".wr-entity")) setEntityPop(null); }
  };
  const onEditorClick = (e) => {
    const sp = e.target.closest && e.target.closest(".wr-entity");
    if (sp) { e.preventDefault(); openDossier(sp.getAttribute("data-lib-id")); }
  };

  /* ---- @ 唤档案：检测、筛选、插入引用 ---- */
  const detectMention = useWC(() => {
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount) { mentionCtx.current = null; setMention(null); return; }
    const node = sel.anchorNode;
    if (!node || node.nodeType !== 3) { mentionCtx.current = null; setMention(null); return; }
    const before = node.nodeValue.slice(0, sel.anchorOffset);
    const m = /@([^@\s]{0,12})$/.exec(before);
    if (!m) { mentionCtx.current = null; setMention(null); return; }
    mentionCtx.current = { node, start: m.index, end: sel.anchorOffset };
    const rect = sel.getRangeAt(0).cloneRange().getBoundingClientRect();
    setMention({ query: m[1], x: rect.left || rect.right, y: rect.bottom || rect.top });
    setMentionIdx(0);
  }, []);
  const closeMention = () => { mentionCtx.current = null; setMention(null); };
  const insertMention = (entry) => {
    const ctx = mentionCtx.current, el = editorRef.current;
    if (!ctx || !el) return;
    const range = document.createRange();
    range.setStart(ctx.node, ctx.start);
    range.setEnd(ctx.node, ctx.end);
    range.deleteContents();
    const span = document.createElement("span");
    span.className = "wr-entity"; span.setAttribute("data-lib-id", entry.id); span.textContent = entry.name;
    range.insertNode(span);
    const space = document.createTextNode("\u00A0");
    span.after(space);
    const r2 = document.createRange(); r2.setStartAfter(space); r2.collapse(true);
    const s = window.getSelection(); s.removeAllRanges(); s.addRange(r2);
    closeMention(); recount(); el.focus();
  };
  const mentionList = mention
    ? ((window.LIB_live ? window.LIB_live().entries : window.LIB_ENTRIES) || []).filter(e => {
        const q = (mention.query || "").toLowerCase();
        if (!q) return true;
        return (e.name + " " + (e.summary || "") + " " + (e.kind || "") + " " + (e.tags || []).join(" ")).toLowerCase().includes(q);
      }).slice(0, 8)
    : [];
  const onEditorKeyDown = (e) => {
    if (!mention) return;
    if (e.key === "ArrowDown") { e.preventDefault(); setMentionIdx(i => Math.min(Math.max(0, mentionList.length - 1), i + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setMentionIdx(i => Math.max(0, i - 1)); }
    else if (e.key === "Enter") { if (mentionList[mentionIdx]) { e.preventDefault(); insertMention(mentionList[mentionIdx]); } }
    else if (e.key === "Escape") { e.preventDefault(); closeMention(); }
  };

  const recount = () => { const el = editorRef.current; if (el) setWordCount(el.innerText.replace(/\s/g, "").length); };

  const updateActive = useWC(() => {
    const el = editorRef.current;
    if (!el) return;
    const sel = window.getSelection();
    let node = sel && sel.anchorNode;
    if (!node || !el.contains(node)) return;
    while (node && node.parentNode !== el) node = node.parentNode;
    Array.from(el.children).forEach(c => c.classList.toggle("is-active", c === node));
    const st = scrollRef.current;
    if (!st || !node) return;
    const typewriter = tw.typewriter;
    if (!immersion && !typewriter) return;
    const sr = st.getBoundingClientRect();
    // typewriter pins the caret line itself; immersion centres the paragraph
    let r = null;
    if (typewriter && sel.rangeCount) {
      const rect = sel.getRangeAt(0).cloneRange().getBoundingClientRect();
      if (rect && rect.height) r = rect;
    }
    if (!r) r = node.getBoundingClientRect();
    const factor = typewriter ? 0.42 : 0.5;
    const target = st.scrollTop + (r.top - sr.top) - sr.height * factor + r.height / 2;
    st.scrollTo({ top: Math.max(0, target), behavior: typewriter ? "auto" : "smooth" });
  }, [immersion, tw.typewriter]);

  const onInput = () => {
    if (approvedLocked) return;
    recount(); schedulePersist();
    // once the writer touches a merged draft paragraph, it becomes their own
    const el = editorRef.current;
    if (el) { const m = el.querySelector("p.is-merge"); if (m && m.contains(window.getSelection().anchorNode)) m.classList.remove("is-merge"); }
    updateActive();
  };

  const commitEdit = () => {
    if (approvedLocked) return;
    recount(); schedulePersist();
  };

  const openAI = () => {
    if (approvedLocked) return;
    if (tw.aiPlace === "drawer") { setRightTab("ai"); setRightOpen(true); if (!coexist) setLeftOpen(false); }
    else setTrayOpen(true);
  };

  const adoptText = (text) => {
    if (approvedLocked) return;
    const el = editorRef.current;
    if (!el || !text) return;
    const p = document.createElement("p");
    p.className = "is-fresh";
    p.textContent = text;
    el.appendChild(p);
    setTrayOpen(false); setRightOpen(false); recount(); schedulePersist();
    setSession(s => s + text.length);
    requestAnimationFrame(() => {
      const st = scrollRef.current;
      if (st) st.scrollTo({ top: st.scrollHeight, behavior: "smooth" });
      setTimeout(() => p.classList.remove("is-fresh"), 1600);
    });
  };

  const adopt = (cand) => {
    if (approvedLocked) return;
    const el = editorRef.current;
    if (!el) return;
    const p = document.createElement("p");
    p.className = "is-fresh";
    p.innerHTML = sanitizeManuscriptHTML(cand.html.replace(/<\/?mark>/g, ""));
    el.appendChild(p);
    setTrayOpen(false); recount(); schedulePersist();
    setSession(s => s + p.innerText.length);
    requestAnimationFrame(() => {
      const st = scrollRef.current;
      if (st) st.scrollTo({ top: st.scrollHeight, behavior: "smooth" });
      setTimeout(() => p.classList.remove("is-fresh"), 1600);
    });
  };

  /* 融合 — insert as an editable draft paragraph, focus it so the writer
     weaves it into their own words instead of accepting verbatim. */
  const merge = (cand) => {
    if (approvedLocked) return;
    const el = editorRef.current;
    if (!el) return;
    const p = document.createElement("p");
    p.className = "is-merge";
    p.innerHTML = sanitizeManuscriptHTML(cand.html.replace(/<\/?mark>/g, ""));
    el.appendChild(p);
    setTrayOpen(false); setRightOpen(false); recount(); schedulePersist();
    requestAnimationFrame(() => {
      const st = scrollRef.current;
      if (st) st.scrollTo({ top: st.scrollHeight, behavior: "smooth" });
      // place caret at end of the merged paragraph
      const range = document.createRange();
      range.selectNodeContents(p); range.collapse(false);
      const selc = window.getSelection(); selc.removeAllRanges(); selc.addRange(range);
      el.focus(); updateActive();
    });
  };

  const goalPct = Math.min(100, Math.round((wordCount / 1500) * 100));
  useWE(() => { if (goalPct >= 100 && !nextDismissed) setNextCue(true); }, [goalPct, nextDismissed]);

  /* ==================== 深改姿态 ==================== */
  const dxRescan = useWC(() => {
    const el = editorRef.current;
    if (!el || !activeScene || !wrDeepScan) return;
    const issues = wrDeepScan(el, activeScene);
    setDxIssues(issues);
    setDxActive(a => issues.some(i => i.key === a) ? a : (issues[0] ? issues[0].key : null));
  }, [activeScene]);

  const queueDxPreferencesPersist = useWC((sceneId) => {
    if (!sceneId) return;
    const queuedSnapshot = wrDxSnapshot(sceneId);
    let state = dxPreferencesRef.current;
    if (state.sceneId !== sceneId) {
      state = { sceneId, revisionNo: 0, saveChain: Promise.resolve() };
      dxPreferencesRef.current = state;
    }
    setDxPersistenceStatus("saving");
    state.saveChain = state.saveChain
      .catch(() => undefined)
      .then(async () => {
        let snapshot = queuedSnapshot;
        let result;
        try {
          result = await wrDxSavePreferences(sceneId, snapshot, state.revisionNo);
        } catch (error) {
          if (error?.code !== "SCENE_DEEP_REVIEW_PREFERENCES_CONFLICT") throw error;
          const remote = await wrDxLoadPreferences(sceneId);
          snapshot = wrDxMergePreferences(remote, queuedSnapshot, { localIgnoredAuthoritative: true });
          wrDxApplyPreferences(sceneId, snapshot);
          result = await wrDxSavePreferences(sceneId, snapshot, remote.revision_no);
        }
        state.revisionNo = result.revision_no;
        if (dxPreferencesRef.current === state && activeSceneRef.current === sceneId) {
          wrDxApplyPreferences(sceneId, result);
          setDxLog(result.decision_log || []);
          setDxPersistenceStatus("synced");
        }
      })
      .catch(() => {
        if (dxPreferencesRef.current === state && activeSceneRef.current === sceneId) {
          setDxPersistenceStatus("local");
        }
      });
  }, []);

  /* 进入深改：先冲掉未落盘改动再诊断；退出：清标注 */
  useWE(() => {
    const el = editorRef.current;
    if (!el) return;
    let cancelled = false;
    if (posture === "deep" && activeScene) {
      persistDoc();
      const sceneId = activeScene;
      const local = wrDxSnapshot(sceneId);
      const state = { sceneId, revisionNo: 0, saveChain: Promise.resolve() };
      dxPreferencesRef.current = state;
      setDxLog(local.decision_log);
      setDxPersistenceStatus("loading");
      dxRescan();
      setRightOpen(true);
      wrDxLoadPreferences(sceneId)
        .then((remote) => {
          if (cancelled || dxPreferencesRef.current !== state) return;
          state.revisionNo = remote.revision_no;
          const merged = wrDxMergePreferences(remote, local);
          wrDxApplyPreferences(sceneId, merged);
          setDxLog(merged.decision_log);
          dxRescan();
          const remoteSnapshot = {
            decision_log: remote.decision_log || [],
            ignored_issue_keys: remote.ignored_issue_keys || [],
          };
          if (JSON.stringify(merged) !== JSON.stringify(remoteSnapshot)) queueDxPreferencesPersist(sceneId);
          else setDxPersistenceStatus("synced");
        })
        .catch(() => {
          if (!cancelled && dxPreferencesRef.current === state) setDxPersistenceStatus("local");
        });
    } else {
      if (wrDeepUnmark) wrDeepUnmark(el);
      setDxIssues([]);
      setDxPersistenceStatus("idle");
    }
    return () => { cancelled = true; };
  }, [posture, activeScene]); // eslint-disable-line

  /* 诊断结果 / 选中项变化 → 重画正文标注 */
  useWE(() => {
    const el = editorRef.current;
    if (!el) return;
    if (posture === "deep" && wrDeepMark) wrDeepMark(el, dxIssues, dxActive);
  }, [dxIssues, dxActive, posture]);

  const locateDxPara = (pid, flash) => {
    const el = editorRef.current, sc = scrollRef.current;
    if (!el || !sc) return;
    const p = el.querySelectorAll("p, blockquote")[pid];
    if (!p) return;
    const top = p.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop - 140;
    sc.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
    if (flash) { p.classList.add("is-fresh"); setTimeout(() => p.classList.remove("is-fresh"), 1600); }
  };
  const dxPick = (key) => {
    setDxActive(key);
    const it = dxIssues.find(x => x.key === key);
    if (it) locateDxPara(it.pid, false);
  };
  const dxAdopt = (issue, cand) => {
    if (approvedLocked) return;
    const el = editorRef.current;
    if (!el || !wrDeepAdopt) return;
    dxUndoRef.current = { sid: activeScene, html: wrCleanHTML(el) };
    if (!wrDeepAdopt(el, issue, cand)) { dxUndoRef.current = null; return; }
    wrHighlightEntities(el);
    recount();
    persistDoc();
    setDxLog(wrDxPushLog(activeScene, `采纳「${cand.label}」 · ${issue.title}`));
    queueDxPreferencesPersist(activeScene);
    dxRescan();
  };
  const dxIgnore = (issue) => {
    if (wrDxAddSkip) wrDxAddSkip(activeScene, issue.key);
    setDxLog(wrDxPushLog(activeScene, `忽略 · ${issue.title}`));
    queueDxPreferencesPersist(activeScene);
    dxRescan();
  };
  const dxUndo = () => {
    if (approvedLocked) return;
    const u = dxUndoRef.current, el = editorRef.current;
    if (!u || !el || u.sid !== activeScene) return;
    if (wrDeepUnmark) wrDeepUnmark(el);
    el.innerHTML = sanitizeManuscriptHTML(u.html);
    wrHighlightEntities(el);
    dxUndoRef.current = null;
    recount(); persistDoc();
    setDxLog(wrDxPushLog(activeScene, "撤销上一次采纳"));
    queueDxPreferencesPersist(activeScene);
    dxRescan();
  };
  const dxRescanAll = () => {
    if (wrDxClearSkips) wrDxClearSkips(activeScene);
    queueDxPreferencesPersist(activeScene);
    dxRescan();
  };
  const dxEditDraft = (issue) => { if (!approvedLocked) { setPosture("draft"); setTimeout(() => locateDxPara(issue.pid, true), 80); } };
  /* ================== /深改姿态 ================== */


  /* respond to command-palette jumps / actions */
  useWE(() => {
    const onScene = (e) => {
      if (e.detail) {
        activeSceneRef.current = e.detail;
        setActiveScene(e.detail);
        setLeftOpen(false);
        setRightOpen(false);
      }
    };
    const onSnowSrc = (e) => { if (e.detail) setSnowSrc(e.detail); };
    const onAction = (e) => {
      if (e.detail === "ai") openAI();
      else if (e.detail === "immersion") setImmersion(v => !v);
      else if (e.detail === "deep" && activeSceneRef.current && !approvedLocked) setPosture("deep");
    };
    const onPosture = (e) => setPosture(e.detail === "deep" && activeSceneRef.current && !approvedLocked ? "deep" : "draft");
    window.addEventListener("ws:writer-scene", onScene);
    window.addEventListener("ws:snow-source", onSnowSrc);
    window.addEventListener("ws:writer-action", onAction);
    window.addEventListener("ws:writer-posture", onPosture);
    setViewIntentTargetReady("writer");
    return () => {
      setViewIntentTargetReady("writer", false);
      window.removeEventListener("ws:writer-scene", onScene);
      window.removeEventListener("ws:snow-source", onSnowSrc);
      window.removeEventListener("ws:writer-action", onAction);
      window.removeEventListener("ws:writer-posture", onPosture);
    };
  }, [tw.aiPlace, approvedLocked]);

  /* 大纲结构操作 → 全部写穿 WsCatalog（单一真相源），再刷新本地映射 */
  const refreshChapters = () => setChapters(wrFromCatalog());
  const chapterLocked = (chId) => chapters.some((chapter) => chapter.id === chId && chapter.state === "approved");
  const reorderScene = (chId, from, to) => {
    if (chapterLocked(chId)) return;
    if (WsCatalog) { WsCatalog.moveScene(chId, from, to); refreshChapters(); return; }
    setChapters(prev => prev.map(c => {
      if (c.id !== chId) return c;
      const scenes = c.scenes.slice();
      const [m] = scenes.splice(from, 1);
      scenes.splice(to, 0, m);
      return { ...c, scenes };
    }));
  };
  const renameScene = (chId, sid, title) => {
    if (chapterLocked(chId)) return;
    if (WsCatalog) { WsCatalog.renameScene(chId, sid, title); refreshChapters(); return; }
    setChapters(prev => prev.map(c => c.id !== chId ? c : { ...c, scenes: c.scenes.map(s => s.id === sid ? { ...s, title } : s) }));
  };
  /* 删除后把光标放到还活着的第一场（正文文档留在存储里，随回收站恢复一并回来），
     并给一条回执说明东西去了哪——删完一片安静是最容易让人以为「丢了」的时刻。 */
  const settleAfterDelete = (removedSceneIds, noticeText) => {
    const next = wrFromCatalog();
    setChapters(next);
    if (removedSceneIds.has(activeScene)) {
      const flat = next.flatMap(c => c.scenes);
      setActiveScene(flat.length ? flat[0].id : null);
    }
    if (noticeText) {
      showNotice({
        text: noticeText,
        actionLabel: "打开回收站",
        onAction: () => { if (go) go("trash"); else location.hash = "#trash"; },
      });
    }
  };
  const deleteScene = (chId, sid) => {
    if (chapterLocked(chId)) return;
    if (WsCatalog) {
      // 进回收站（正文文档保留在存储里，恢复时一并回来；彻底删除时由回收站清理）
      const hit = WsCatalog.sceneById(sid);
      if (hit && WsTrashStore) {
        WsTrashStore.push({
          kind: "场景",
          title: `CH ${hit.chapter.n} · ${hit.scene.title}`,
          payload: { type: "scene", chId: hit.chapter.id, index: hit.index, scene: hit.scene },
        });
      }
      const title = (hit && hit.scene.title) || "未命名场景";
      WsCatalog.removeScene(chId, sid);
      settleAfterDelete(new Set([sid]), `已把场景「${title}」移入回收站`);
      return;
    }
    if (activeScene === sid) {
      const flat = chapters.flatMap(c => c.scenes).filter(s => s.id !== sid);
      if (flat.length) setActiveScene(flat[0].id);
    }
    setChapters(prev => prev.map(c => c.id !== chId ? c : { ...c, scenes: c.scenes.filter(s => s.id !== sid) }));
  };
  const addScene = (chId) => {
    if (chapterLocked(chId)) return;
    if (WsCatalog) { WsCatalog.addScene(chId, "新场景"); refreshChapters(); return; }
    const nid = "sc" + Date.now();
    setChapters(prev => prev.map(c => c.id !== chId ? c : { ...c, scenes: [...c.scenes, { id: nid, title: "新场景", state: "todo" }] }));
  };
  const deleteChapter = (chId) => {
    if (!WsCatalog || chapterLocked(chId)) return;
    const target = chapters.find(c => c.id === chId);
    if (!target) return;
    const scenes = target.scenes.length;
    if (!window.confirm(`把第 ${target.n} 章「${target.title}」移入回收站？${scenes ? `连同章下 ${scenes} 个场景。` : ""}`)) return;
    WsCatalog.removeChapters([chId]);
    settleAfterDelete(new Set(target.scenes.map(s => s.id)), `已把第 ${target.n} 章移入回收站${scenes ? `（含 ${scenes} 场）` : ""}`);
  };
  /* 批量删除：章 + 场一次提交（被删章下的场不再单独进场景桶，
     否则后端会以「章下已有单独回收的场景」为由挡下整章删除） */
  const deleteBatch = (chapterIds, sceneIds) => {
    if (!WsCatalog) return;
    const chDrop = new Set(chapterIds.filter(id => !chapterLocked(id)));
    const scDrop = new Set();
    const removedScenes = new Set();
    chapters.forEach((c) => {
      c.scenes.forEach((s) => {
        if (chDrop.has(c.id)) { removedScenes.add(s.id); return; }
        if (sceneIds.includes(s.id) && c.state !== "approved") { scDrop.add(s.id); removedScenes.add(s.id); }
      });
    });
    if (!chDrop.size && !scDrop.size) return;
    const parts = [];
    if (chDrop.size) parts.push(`${chDrop.size} 章`);
    if (scDrop.size) parts.push(`${scDrop.size} 场`);
    if (!window.confirm(`把所选 ${parts.join(" · ")} 移入回收站？共 ${removedScenes.size} 个场景受影响。`)) return;
    WsCatalog.removeMixed([...chDrop], [...scDrop]);
    settleAfterDelete(removedScenes, `已把 ${parts.join(" · ")} 移入回收站`);
  };
  /* 空白作品：创建第一章 + 开场，立刻可写 */
  const createFirstChapter = () => {
    if (!WsCatalog) return;
    WsCatalog.addChapter();
    const next = wrFromCatalog();
    setChapters(next);
    const w = WsCatalog.writingScene();
    if (w) setActiveScene(w.scene.sid);
  };
  // active-scene meta derived from live chapter order (stamps update on reorder)
  const sceneMeta = (id) => {
    if (!id) return { stamp: "", title: "", type: "", goal: "" };
    const hit = WsCatalog ? WsCatalog.sceneById(id) : null;
    for (const c of chapters) {
      const i = c.scenes.findIndex(s => s.id === id);
      if (i >= 0) {
        const s = c.scenes[i];
        return {
          stamp: `CH ${c.n} · SC ${String(i + 1).padStart(2, "0")}`,
          title: s.title,
          type: hit && hit.scene.kind ? hit.scene.kind + "场景" : (s.state === "active" ? "主动场景" : (i % 2 === 0 ? "主动场景" : "反应场景")),
          goal: (hit && hit.scene.goal) || "（本场目标待规划）",
          card: hit ? { kind: hit.scene.kind || "主动", goal: hit.scene.goal || "", obstacle: hit.scene.obstacle || "", turn: hit.scene.turn || "" } : null,
        };
      }
    }
    return {};
  };
  const am = sceneMeta(activeScene);
  const flatScenes = chapters.flatMap(c => c.scenes);
  const sceneAt = (off) => {
    const i = flatScenes.findIndex(s => s.id === activeScene);
    if (i < 0) return null;
    const t = flatScenes[i + off];
    return t ? t.id : null;
  };

  const drawerOpen = leftOpen || rightOpen;

  return (
    <div
      className="wr-root"
      data-screen-label="writer"
      data-focus={tw.focus}
      data-ambient={tw.ambient ? "on" : "off"}
      data-motion={t && t.motion ? t.motion : "standard"}
      data-texture={t && t.texture === false ? "off" : "on"}
      data-immersion={immersion ? "on" : "off"}
      data-chrome={chrome ? "show" : "hidden"}
      data-layout={isDesk ? "desk" : "immersive"}
      data-posture={posture}
      data-typewriter={tw.typewriter ? "on" : "off"}
      data-left={leftOpen ? "on" : "off"}
      data-right={rightOpen ? "on" : "off"}
      data-dragging={dragging ? "on" : "off"}
      style={{ "--measure": tw.measure + "px", "--ms-fz": tw.fontSize + "px", "--ms-lh": String(tw.lineHeight), "--rail-l": railL + "px", "--rail-r": railR + "px" }}
    >
      <div className="wr-room-bg" />
      <div className="wr-room">
        <div className={`wr-progress ${goalPct >= 100 ? "is-full" : ""}`} style={{ width: goalPct + "%" }} />
        <div className="wr-top">
          {onExit && (
            <button className="wr-loc wr-home-btn" onClick={onExit} title="返回工作台">
              <I.ChevronLeft size={15} /><span className="wr-loc-name" style={{ fontSize: 13 }}>工作台</span>
            </button>
          )}
          <button className="wr-loc" onClick={() => { setLeftOpen(true); setRightOpen(false); }} title="章节大纲 ⌘1">
            <span className="wr-loc-num">{am.stamp}</span>
            <span className="wr-loc-name">{am.title}</span>
            <span className="wr-loc-chev"><I.ChevronDown size={14} /></span>
          </button>
          <div className="wr-posture" role="tablist" aria-label="写作姿态">
            <button role="tab" aria-selected={posture === "draft"} tabIndex={posture === "draft" ? 0 : -1} onKeyDown={onRovingTabKeyDown}
              className={posture === "draft" ? "is-on" : ""} onClick={() => setPosture("draft")} title="起草 · 编辑正文"><I.Pen size={12} /> 起草</button>
            <button role="tab" aria-selected={posture === "deep"} tabIndex={posture === "deep" ? 0 : -1} onKeyDown={onRovingTabKeyDown}
              className={posture === "deep" ? "is-on" : ""} disabled={!activeScene || approvedLocked} onClick={() => setPosture("deep")} title={approvedLocked ? "终稿已锁定，请先重新打开章节" : "深改 · 句段诊断与改写"}>
              <I.Microscope size={12} /> 深改{posture === "deep" && dxIssues.length > 0 ? <span className="wr-posture-n">{dxIssues.length}</span> : null}
            </button>
          </div>
          <div className="wr-top-spacer" />
          <div className="wr-stat">
            <WrCanonicalControl
              saveStatus={saved}
              canonicalStatus={canonicalStatus}
              disabled={!activeScene || approvedLocked || !!contentSafetyReview}
              onPromote={promoteCanonical}
            />
            <span className="wr-stat-time" title="本节写作时长"><I.Clock size={13} /><b>{fmtElapsed(elapsed)}</b></span>
            <span className="wr-count-wrap" title="本场字数 / 目标">
              <GoalRing pct={goalPct} />
              <span className="wr-count"><b>{wordCount}</b> / 1500</span>
            </span>
          </div>
        </div>

        {snowSrc && (
          <div className="wr-snow-anchor" data-form={snowSrc.form}>
            <span className="wr-snow-ic"><I.Activity size={13} /></span>
            <span className="wr-snow-text">
              本场来自<b>构思</b>
              <span className="wr-snow-sep">·</span>{snowSrc.chapter}
              <span className="wr-snow-id">{snowSrc.id}</span>
              <span className="wr-snow-form">{snowSrc.form === "proactive" ? "主动 · GCS" : "反应 · RDD"}</span>
              {snowSrc.spine && <span className="wr-snow-spine"><I.Activity size={9} />{snowSrc.spine}</span>}
            </span>
            <span className="wr-snow-brief">{snowSrc.brief}</span>
            <button className="wr-snow-back" onClick={() => { if (onExit) onExit(); setTimeout(() => { location.hash = "#snowflake"; }, 0); }} title="返回构思控制塔">回到构思 <I.ArrowRight size={12} /></button>
            <button className="wr-snow-x" onClick={() => setSnowSrc(null)} title="收起锚点">×</button>
          </div>
        )}

        <div className="wr-scroll" ref={scrollRef} onClick={updateActive}>
          {!activeScene && (catalogLoading ? (
            <div className="wr-blank" style={{ display: "grid", placeItems: "center", minHeight: "60vh", textAlign: "center", color: "var(--ink-3)" }} role="status">
              正在从服务端加载章节与正文…
            </div>
          ) : catalogUnavailable ? (
            <div className="wr-blank" style={{ display: "grid", placeItems: "center", minHeight: "60vh", textAlign: "center" }} role="status">
              <div style={{ maxWidth: 440, display: "grid", gap: 14, justifyItems: "center" }}>
                <div style={{ fontFamily: "var(--font-serif)", fontSize: 22, color: "var(--ink-1)" }}>章节目录加载失败</div>
                <p style={{ color: "var(--ink-3)", fontSize: 14, lineHeight: 1.8, margin: 0 }}>系统不会把网络失败当成空作品。恢复连接后重试，正文与目录都不会被本地空状态覆盖。</p>
                <button className="btn btn-ghost" onClick={() => WsCatalog.__refresh && WsCatalog.__refresh()}><I.Refresh size={14} /> 重试加载</button>
              </div>
            </div>
          ) : (
            <div className="wr-blank" style={{ display: "grid", placeItems: "center", minHeight: "60vh", textAlign: "center" }}>
              <div style={{ maxWidth: 420, display: "grid", gap: 14, justifyItems: "center" }}>
                <div style={{ fontFamily: "var(--font-serif)", fontSize: 22, color: "var(--ink-1)" }}>这部作品还没有章节</div>
                <p style={{ color: "var(--ink-3)", fontSize: 14, lineHeight: 1.8, margin: 0 }}>建立第一章和开场场景就能动笔；也可以先去雪花构思把结构长出来。</p>
                <div style={{ display: "flex", gap: 10 }}>
                  <button className="btn btn-accent" onClick={createFirstChapter}><I.Plus size={15} /> 创建第一章 · 开场</button>
                  <button className="btn btn-ghost" onClick={() => { if (onExit) onExit(); setTimeout(() => { location.hash = "#snowflake"; }, 0); }}>去构思</button>
                </div>
              </div>
            </div>
          ))}
          <div className="wr-measure" style={!activeScene ? { display: "none" } : undefined}>
            <header className="wr-scene-head">
              <div className="wr-stamp">{am.stamp} · {am.type}</div>
              <h1 className="wr-scene-title">{am.title}</h1>
              {am.card
                ? <WrSceneCard card={am.card} onEdit={go ? () => go("author") : null} />
                : <div className="wr-goal"><span className="wr-goal-k">本场目标</span><span className="wr-goal-v">{am.goal}</span></div>}
              {approvedLocked && <div className="wr-final-lock" role="status"><I.Lock size={13} /> 已批准终稿只读。需要改写时，请先到成稿中心重新打开本章。</div>}
              {posture === "deep" && <div className="wr-deep-note"><span className="dot" />深改姿态 · 正文只读 · 点击高亮句直达诊断</div>}
            </header>
            <div className="wr-editor" ref={editorRef} role="textbox" aria-multiline="true"
              aria-label={`${am.title || "当前场景"}正文编辑区`} aria-readonly={posture === "deep" || approvedLocked} tabIndex={0}
              contentEditable={posture !== "deep" && !approvedLocked} suppressContentEditableWarning spellCheck={false} onInput={() => { onInput(); detectMention(); }} onKeyDown={onEditorKeyDown} onKeyUp={() => { updateActive(); detectMention(); }} onMouseOver={onEditorOver} onMouseOut={onEditorOut} onClick={(e) => {
              if (posture === "deep") {
                const dx = e.target.closest && e.target.closest("[data-dx]");
                if (dx) { setDxActive(dx.getAttribute("data-dx")); return; }
              }
              onEditorClick(e);
            }} />
            <footer className="wr-scene-foot">
              <span className="wr-foot-meta">{approvedLocked ? "终稿锁定 · 只读" : (savedAt ? `自动保存 · ${new Date(savedAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}` : "自动保存已开启")}</span>
              <div className="flex gap-2">
                <button className="btn btn-ghost btn-sm" disabled={!sceneAt(-1)} onClick={() => { const p = sceneAt(-1); if (p) setActiveScene(p); }}>回到上一场</button>
                <button className="btn btn-primary btn-sm" disabled={approvedLocked} title={approvedLocked ? "终稿已锁定" : "仅打开下一场提示，不会伪造服务端完成状态"} onClick={() => { setNextCue(true); setNextDismissed(false); }}>准备下一场 <I.ArrowRight size={13} /></button>
              </div>
            </footer>
          </div>
        </div>

        <button className="wr-edge left" onClick={() => { setLeftOpen(true); setRightOpen(false); }} title="章节大纲 ⌘1"><span className="wr-edge-label">大纲</span></button>
        <button className="wr-edge right" onClick={() => { setRightOpen(true); setLeftOpen(false); }} title="场景上下文 ⌘2"><span className="wr-edge-label">上下文</span></button>

        <div className="wr-dock">
          {onExit && (<><button className="wr-dock-btn wr-dock-icon" onClick={onExit} title="返回工作台"><I.Home size={16} /></button><div className="wr-dock-sep" /></>)}
          <button className={`wr-dock-btn ${leftOpen ? "is-on" : ""}`} onClick={() => { setLeftOpen(v => !v); if (!coexist) setRightOpen(false); }} title="大纲 ⌘1"><I.BookOpen size={16} /> 大纲</button>
          <button className={`wr-dock-btn ${rightOpen ? "is-on" : ""}`} onClick={() => { setRightOpen(v => !v); if (!coexist) setLeftOpen(false); }} title="上下文 ⌘2"><I.Compass size={16} /> 上下文</button>
          <div className="wr-dock-sep" />
          <button className="wr-dock-btn accent" disabled={approvedLocked} onClick={openAI} title={approvedLocked ? "终稿已锁定，请先重新打开章节" : "AI 续写 / 改写 ⌘J"}><I.Sparkles size={16} /> AI 续写 <kbd>⌘J</kbd></button>
          <div className="wr-dock-sep" />
          <button className={`wr-dock-btn wr-dock-icon ${isDesk ? "wr-layout-on" : ""}`} onClick={() => setTweak && setTweak("wrLayout", isDesk ? "immersive" : "desk")} title={isDesk ? "切换到沉浸稿纸" : "切换到书桌三栏"}><I.PanelLeft size={16} /></button>
          <button className={`wr-dock-btn wr-dock-icon ${immersion ? "is-on" : ""}`} onClick={() => setImmersion(v => !v)} title="沉浸写作 ⌘."><I.Eye size={16} /></button>
        </div>

        {coexist && leftOpen && <div className="wr-resize wr-resize-l" onPointerDown={startRail("l")} onDoubleClick={() => resetRail("l")} title="拖拽调整大纲栏宽 · 双击重置" />}
        {coexist && rightOpen && <div className="wr-resize wr-resize-r" onPointerDown={startRail("r")} onDoubleClick={() => resetRail("r")} title="拖拽调整上下文栏宽 · 双击重置" />}

        <WrNextCue show={nextCue && (coexist || !drawerOpen)} onGo={() => { const n = sceneAt(1); if (n) setActiveScene(n); setNextCue(false); }} onDismiss={() => { setNextCue(false); setNextDismissed(true); }} />
      </div>

      <div className={`wr-scrim wr-scrim-drawer ${drawerOpen ? "show" : ""}`} onClick={() => { setLeftOpen(false); setRightOpen(false); }} />
      <WrOutline open={leftOpen} activeScene={activeScene} chapters={chapters} onReorder={reorderScene} onRename={renameScene} onDelete={deleteScene} onDeleteChapter={deleteChapter} onDeleteBatch={deleteBatch} onAdd={addScene} onPick={(id) => { setActiveScene(id); if (!coexist) setLeftOpen(false); }} onClose={() => setLeftOpen(false)} />
      {posture === "deep" && WrDeepDrawer
        ? <WrDeepDrawer open={rightOpen} issues={dxIssues} activeKey={dxActive} onPick={dxPick} onAdopt={dxAdopt} onIgnore={dxIgnore} onRescan={dxRescanAll} onEditDraft={dxEditDraft} onUndo={dxUndo} canUndo={!!(dxUndoRef.current && dxUndoRef.current.sid === activeScene)} log={dxLog} persistenceStatus={dxPersistenceStatus} onClose={() => setRightOpen(false)} />
        : <WrContext open={rightOpen} tab={rightTab} setTab={setRightTab} onClose={() => setRightOpen(false)} onOpenAI={() => { setRightOpen(false); setTrayOpen(true); }} place={tw.aiPlace} onAdopt={adopt} onMerge={merge} onAdoptText={adoptText} scene={activeScene} editorRef={editorRef} />}
      <div className={`wr-scrim wr-scrim-tray ${trayOpen ? "show" : ""}`} onClick={() => setTrayOpen(false)} />
      <WrTray open={trayOpen} onClose={() => setTrayOpen(false)} onAdopt={adopt} onMerge={merge} onAdoptText={adoptText}
        sceneLabel={am.stamp ? am.stamp + (am.title ? " · " + am.title : "") : null}
        pov={wrCtx(activeScene).pov !== "—" ? wrCtx(activeScene).pov : null} />
      <UndoToast toast={toast} onClose={clearNotice} />
      <WrInlineRewrite editorRef={editorRef} onCommit={commitEdit} />
      <WrEntityPop pop={entityPop} onOpen={openDossier} />
      <WrMentionPicker mention={mention} list={mentionList} idx={mentionIdx} onPick={insertMention} onHover={setMentionIdx} />
      {contentSafetyReview && (
        <ContentSafetyReviewDialog
          review={contentSafetyReview}
          busy={contentSafetyBusy}
          error={contentSafetyError}
          onCancel={cancelContentSafetyReview}
          onConfirm={confirmContentSafetyReview}
        />
      )}
    </div>
  );
}

/* ---- @ mention picker (insert a library reference) ---- */
function WrMentionPicker({ mention, list, idx, onPick, onHover }) {
  if (!mention) return null;
  const cats = (window.LIB_CATS || []).reduce((m, c) => { m[c.id] = c; return m; }, {});
  const flip = mention.y > (typeof window !== "undefined" ? window.innerHeight - 300 : 9999);
  const style = { left: mention.x, top: flip ? mention.y - 26 : mention.y + 6, transform: flip ? "translateY(-100%)" : "none" };
  return (
    <div className="wr-mention" style={style}>
      <div className="wr-mention-head"><I.Library size={12} /> 插入档案引用{mention.query ? <span className="wr-mention-q">「{mention.query}」</span> : null}</div>
      {list.length === 0 ? (
        <div className="wr-mention-empty">没有匹配的档案</div>
      ) : (
        <ul className="wr-mention-list">
          {list.map((e, i) => (
            <li key={e.id}
              className={`wr-mention-item acc-${e.accent} ${i === idx ? "is-sel" : ""}`}
              onMouseEnter={() => onHover(i)}
              onMouseDown={(ev) => { ev.preventDefault(); onPick(e); }}>
              <span className="wr-mention-glyph">{e.glyph}</span>
              <span className="wr-mention-main">
                <span className="wr-mention-name">{e.name}</span>
                <span className="wr-mention-sub">{(cats[e.cat] || {}).label} · {e.summary || e.kind}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
      <div className="wr-mention-foot"><kbd style={WR_KBD}>↑↓</kbd>选择 <kbd style={WR_KBD}>⏎</kbd>插入 <kbd style={WR_KBD}>Esc</kbd>取消</div>
    </div>
  );
}

/* ---- entity hover card (from library) ---- */
function WrEntityPop({ pop, onOpen }) {
  if (!pop) return null;
  const byId = window.LIB_live ? window.LIB_live().byId : (window.LIB_BY_ID || {});
  const e = byId[pop.id];
  if (!e) return null;
  const cats = (window.LIB_CATS || []).reduce((m, c) => { m[c.id] = c; return m; }, {});
  const above = pop.top > 180;
  const style = {
    left: pop.x,
    top: above ? pop.top - 10 : pop.bottom + 10,
    transform: above ? "translate(-50%, -100%)" : "translate(-50%, 0)",
  };
  return (
    <div className={`wr-entpop acc-${e.accent}`} style={style}>
      <div className="wr-entpop-head">
        <span className="wr-entpop-glyph">{e.glyph}</span>
        <div className="wr-entpop-main">
          <div className="wr-entpop-name">{e.name}</div>
          <div className="wr-entpop-kind">{(cats[e.cat] || {}).label} · {e.kind}</div>
        </div>
      </div>
      {e.summary && <div className="wr-entpop-sum">{e.summary}</div>}
      <div className="wr-entpop-foot"><I.BookOpen size={12} /> 点击打开档案</div>
    </div>
  );
}

/* ---- 场景卡随行：目录里的 GMC 卡常驻正文上方，对着规划写 ---- */
function WrSceneCard({ card, onEdit }) {
  const isReact = card.kind === "反应";
  const fields = isReact
    ? [["反应", card.goal], ["困境", card.obstacle], ["决定", card.turn]]
    : [["目标", card.goal], ["阻碍", card.obstacle], ["出口", card.turn]];
  return (
    <div className="wr-scard">
      <div className="wr-scard-top">
        <span className={`wr-scard-kind ${isReact ? "is-react" : ""}`}>{card.kind}场景</span>
        <span className="wr-scard-src">本场的卡 · 来自章节编排</span>
        {onEdit && <button className="wr-scard-edit" onClick={onEdit} title="到章节编排修改这张卡">编辑卡 ↗</button>}
      </div>
      <div className="wr-scard-grid">
        {fields.map(([k, v]) => (
          <div className="wr-scard-f" key={k}>
            <span className="wr-scard-k">{k}</span>
            <span className={`wr-scard-v ${v && !v.includes("（待") ? "" : "is-empty"}`}>{v && !v.includes("（待") ? v : "（待规划）"}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ---- next cue ---- */
function WrNextCue({ show, onGo, onDismiss }) {
  return (
    <div className={`wr-next ${show ? "show" : ""}`}>
      <span className="wr-next-text">本场已达目标字数 · 下一场 <b>馆长出现</b></span>
      <button className="wr-next-go" onClick={onGo}>写下一场 <I.ArrowRight size={13} /></button>
      <button className="wr-next-dismiss" onClick={onDismiss} title="稍后"><I.X size={14} /></button>
    </div>
  );
}

/* ---- outline drawer ---- */
function WrOutline({ open, activeScene, chapters, onReorder, onRename, onDelete, onDeleteChapter, onDeleteBatch, onAdd, onPick, onClose }) {
  const list = chapters || [];
  const allScenes = list.flatMap(c => c.scenes);
  const doneCount = allScenes.filter(s => s.state === "done").length;
  const pct = allScenes.length ? Math.round((doneCount / allScenes.length) * 100) : 0;
  /* 多选态只活在抽屉里（不落 localStorage）：退出多选或删除完成即清空——
     勾选是瞬时意图，跨会话留着只会让作者对着一份看不见来路的勾选下手 */
  const [selectMode, setSelectMode] = useWS(false);
  const [selCh, setSelCh] = useWS(() => new Set());
  const [selSc, setSelSc] = useWS(() => new Set());
  const openable = list.filter(c => c.state !== "approved");
  const selectableScenes = openable.flatMap(c => c.scenes);
  /* 勾了整章就等于勾了它名下所有场：单独再算一遍会把「3 章 + 7 场」这种
     数字报成作者根本没做过的选择，删除范围也跟着看不懂。这里只统计
     「不在已选章名下的散场」，并让那些场在行上显示为随章带走。 */
  const scenesUnderSelectedChapters = new Set(
    list.filter(c => selCh.has(c.id)).flatMap(c => c.scenes.map(s => s.id))
  );
  const looseScenes = [...selSc].filter(id => !scenesUnderSelectedChapters.has(id));
  const selectedCount = selCh.size + looseScenes.length;
  const exitSelect = () => { setSelectMode(false); setSelCh(new Set()); setSelSc(new Set()); };
  const toggle = (setter) => (id) => setter(prev => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const allSelected = !!(openable.length || selectableScenes.length)
    && selCh.size === openable.length && selSc.size === selectableScenes.length;
  return (
    <aside className={`wr-drawer left ${open ? "show" : ""}`}>
      <header className="wr-drawer-head">
        <I.BookOpen size={16} /><span className="wr-drawer-title">{selectMode ? "选择要删除的内容" : "章节大纲"}</span>
        <button className={`wr-drawer-sel ${selectMode ? "is-on" : ""}`} title={selectMode ? "退出多选" : "多选章节 / 场景后可批量删除"}
          data-testid="writer-outline-select-mode"
          onClick={() => (selectMode ? exitSelect() : setSelectMode(true))}>
          <I.Check size={13} />{selectMode ? "完成" : "多选"}
        </button>
        <button className="wr-drawer-x" onClick={onClose} title="收起 (Esc)"><I.X size={16} /></button>
      </header>
      <div className="wr-drawer-body">
        <div className="wr-rail-progress">
          <div className="wr-rail-progress-top">
            <span className="wr-rail-progress-k">全书进度</span>
            <span className="wr-rail-progress-v">{doneCount}/{allScenes.length} 场 · {pct}%</span>
          </div>
          <div className="wr-rail-progress-bar"><div className="wr-rail-progress-fill" style={{ width: pct + "%" }} /></div>
        </div>
        {list.map(c => (
          <WrChapter key={c.id} ch={c} activeScene={activeScene} onPick={onPick} onReorder={onReorder} onRename={onRename}
            onDelete={onDelete} onDeleteChapter={onDeleteChapter} onAdd={onAdd}
            selectMode={selectMode} chChecked={selCh.has(c.id)}
            scChecked={(id) => selSc.has(id) || scenesUnderSelectedChapters.has(id)}
            scFollowsChapter={selCh.has(c.id)}
            onToggleCh={toggle(setSelCh)} onToggleSc={toggle(setSelSc)} />
        ))}
      </div>
      {selectMode && (
        <footer className="wr-outline-batch" role="toolbar" aria-label="大纲批量操作">
          <span className="wr-outline-batch-n">
            {selectedCount
              ? <>已选 <strong>{selCh.size}</strong> 章 · <strong>{looseScenes.length}</strong> 场{scenesUnderSelectedChapters.size ? `（另含随章带走 ${scenesUnderSelectedChapters.size} 场）` : ""}</>
              : "勾选章或场；点场景行也可选中"}
          </span>
          <button className="btn btn-quiet btn-sm" onClick={() => {
            if (allSelected) { setSelCh(new Set()); setSelSc(new Set()); return; }
            setSelCh(new Set(openable.map(c => c.id)));
            setSelSc(new Set(selectableScenes.map(s => s.id)));
          }}>{allSelected ? "取消全选" : "全选"}</button>
          <button className="btn btn-danger btn-sm" data-testid="writer-outline-batch-delete" disabled={!selectedCount}
            onClick={() => { onDeleteBatch && onDeleteBatch([...selCh], [...selSc]); exitSelect(); }}>
            <I.Trash size={13} /> 删除所选
          </button>
        </footer>
      )}
    </aside>
  );
}
function WrChapter({ ch, activeScene, onPick, onReorder, onRename, onDelete, onDeleteChapter, onAdd, selectMode, chChecked, scChecked, scFollowsChapter, onToggleCh, onToggleSc }) {
  const [open, setOpen] = useWS(ch.expanded || ch.scenes.some(s => s.id === activeScene));
  const [over, setOver] = useWS(null);
  const [editing, setEditing] = useWS(null);
  const [editVal, setEditVal] = useWS("");
  const dragFrom = useWR(null);
  const tone = { approved: "sage", draft: "slate", writing: "crimson" }[ch.state] || "slate";
  const label = { approved: "已批准", draft: "草稿", writing: "进行中" }[ch.state] || ch.state;
  const locked = ch.state === "approved";
  const startEdit = (s) => { if (!locked) { setEditing(s.id); setEditVal(s.title); } };
  const commit = () => { if (editing && onRename) onRename(ch.id, editing, editVal.trim() || "未命名"); setEditing(null); };
  return (
    <div className="wr-ch">
      <div className={`wr-ch-head ${chChecked ? "is-selected" : ""}`}>
        {selectMode && (
          <label className="wr-ch-check" title={locked ? "已批准终稿不可删除——请先在成稿中心重新打开" : "选中本章（含章下场景）"}>
            <input type="checkbox" checked={!!chChecked} disabled={locked} aria-label={`选择第 ${ch.n} 章 · ${ch.title}`}
              onChange={() => onToggleCh && onToggleCh(ch.id)} />
          </label>
        )}
        <button className="wr-ch-row" onClick={() => setOpen(v => !v)}>
          <span className="wr-ch-chev" style={{ transform: open ? "rotate(90deg)" : "none" }}><I.ChevronRight size={13} /></span>
          <span className="wr-ch-num">{ch.n}</span>
          <span className="wr-ch-title">{ch.title}</span>
          <span className={`pill pill-${tone} text-xs`} style={{ padding: "0 6px" }}><span className="pill-dot" />{label}</span>
        </button>
        {!selectMode && (
          <button className="wr-sc-actbtn danger wr-ch-del" disabled={locked}
            title={locked ? "终稿已锁定" : "删除本章（含章下场景）"}
            onClick={(e) => { e.stopPropagation(); onDeleteChapter && onDeleteChapter(ch.id); }}><I.Trash size={12} /></button>
        )}
      </div>
      {open && (
        <ul className="wr-sc-list">
          {ch.scenes.map((s, i) => (
            <li key={s.id}
              draggable={!locked && !selectMode && editing !== s.id}
              onDragStart={(e) => { dragFrom.current = i; if (e.dataTransfer) e.dataTransfer.effectAllowed = "move"; }}
              onDragOver={(e) => { e.preventDefault(); if (over !== i) setOver(i); }}
              onDrop={(e) => { e.preventDefault(); const from = dragFrom.current; if (from != null && from !== i && onReorder) onReorder(ch.id, from, i); dragFrom.current = null; setOver(null); }}
              onDragEnd={() => { dragFrom.current = null; setOver(null); }}>
              <div className={`wr-sc ${activeScene === s.id ? "is-active" : ""} ${over === i ? "is-over" : ""} ${editing === s.id ? "is-editing" : ""} ${selectMode && scChecked && scChecked(s.id) ? "is-selected" : ""}`}
                role="button" tabIndex={0}
                onClick={() => { if (selectMode) { if (!locked && !scFollowsChapter) onToggleSc && onToggleSc(s.id); return; } if (editing !== s.id) onPick(s.id); }}>
                {selectMode ? (
                  <label className="wr-sc-check" onClick={(e) => e.stopPropagation()}
                    title={scFollowsChapter ? "本章已选中，这一场随章一起删除" : undefined}>
                    <input type="checkbox" checked={!!(scChecked && scChecked(s.id))} disabled={locked || scFollowsChapter}
                      aria-label={`选择场景 ${s.title}`} onChange={() => onToggleSc && onToggleSc(s.id)} />
                  </label>
                ) : (
                  <span className="wr-sc-grip" title={locked ? "终稿已锁定" : "拖拽重排"}><I.GripVertical size={13} /></span>
                )}
                <span className={`wr-sc-mark s-${s.state}`}>
                  {s.state === "done" && <I.Check size={11} />}
                  {s.state === "active" && <span className="wr-sc-dot" />}
                  {s.state === "todo" && <I.Circle size={11} />}
                </span>
                {editing === s.id ? (
                  <input className="wr-sc-edit" autoFocus value={editVal}
                    onClick={(e) => e.stopPropagation()}
                    onChange={(e) => setEditVal(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } else if (e.key === "Escape") { e.preventDefault(); setEditing(null); } }}
                    onBlur={commit} />
                ) : (
                  <span className="wr-sc-name" onDoubleClick={(e) => { e.stopPropagation(); startEdit(s); }}>{s.title}</span>
                )}
                {editing !== s.id && !selectMode && (
                  <span className="wr-sc-act">
                    <button className="wr-sc-actbtn" disabled={locked} title={locked ? "终稿已锁定" : "重命名"} onClick={(e) => { e.stopPropagation(); startEdit(s); }}><I.Edit size={12} /></button>
                    <button className="wr-sc-actbtn danger" disabled={locked} title={locked ? "终稿已锁定" : "删除场景"} onClick={(e) => { e.stopPropagation(); onDelete && onDelete(ch.id, s.id); }}><I.Trash size={12} /></button>
                  </span>
                )}
              </div>
            </li>
          ))}
          {!selectMode && <li className="wr-sc-add"><button className="wr-sc-addbtn" disabled={locked} title={locked ? "终稿已锁定" : undefined} onClick={() => onAdd && onAdd(ch.id)}><I.Plus size={12} /> 添加场景</button></li>}
        </ul>
      )}
    </div>
  );
}

/* ---- context drawer ---- */
function WrContext({ open, tab, setTab, onClose, onOpenAI, place, onAdopt, onMerge, onAdoptText, scene, editorRef }) {
  const riskN = wrCtx(scene).risks.length;
  const [annoTick, setAnnoTick] = useWS(0);
  useWE(() => { const h = () => setAnnoTick(t => t + 1); window.addEventListener("ws:anno-change", h); return () => window.removeEventListener("ws:anno-change", h); }, []);
  const annoCount = (editorRef && editorRef.current) ? editorRef.current.querySelectorAll("mark.wr-anno").length : 0;
  return (
    <aside className={`wr-drawer right ${open ? "show" : ""}`}>
      <header className="wr-drawer-head">
        <I.Compass size={16} /><span className="wr-drawer-title">场景上下文</span>
        <button className="wr-drawer-x" onClick={onClose} title="收起 (Esc)"><I.X size={16} /></button>
      </header>
      <div className="wr-tabs" role="tablist" aria-label="场景上下文分类">
        <WrTabBtn id="scene" cur={tab} on={setTab} icon="Compass" label="戏剧" />
        <WrTabBtn id="ai" cur={tab} on={setTab} icon="Sparkles" label="AI" />
        <WrTabBtn id="qc" cur={tab} on={setTab} icon="ShieldCheck" label="QC" badge={riskN ? String(riskN) : null} />
        <WrTabBtn id="anno" cur={tab} on={setTab} icon="Edit" label="批注" badge={annoCount ? String(annoCount) : null} />
        <WrTabBtn id="notes" cur={tab} on={setTab} icon="FileText" label="笔记" />
      </div>
      <div className="wr-drawer-body">
        {tab === "scene" && <WrCtxScene scene={scene} />}
        {tab === "ai" && <WrCtxAI place={place} onOpenAI={onOpenAI} onAdopt={onAdopt} onMerge={onMerge} onAdoptText={onAdoptText} />}
        {tab === "qc" && <WrCtxQC scene={scene} />}
        {tab === "anno" && <WrAnnoList editorRef={editorRef} tick={annoTick} />}
        {tab === "notes" && <WrCtxNotes scene={scene} />}
      </div>
    </aside>
  );
}
function WrAnnoList({ editorRef, tick }) {
  const marks = (editorRef && editorRef.current) ? Array.from(editorRef.current.querySelectorAll("mark.wr-anno")) : [];
  const jump = (el) => {
    const st = editorRef.current && editorRef.current.closest(".wr-scroll");
    if (!st || !el) return;
    const r = el.getBoundingClientRect(); const sr = st.getBoundingClientRect();
    const target = st.scrollTop + (r.top - sr.top) - sr.height / 2 + r.height / 2;
    st.scrollTo({ top: Math.max(0, target), behavior: "smooth" });
    el.classList.add("wr-anno-flash");
    setTimeout(() => el.classList.remove("wr-anno-flash"), 1200);
  };
  if (!marks.length) {
    return (
      <div className="wr-anno-empty">
        <I.Edit size={20} />
        <div>本场还没有批注。</div>
        <div className="text-sm" style={{ color: "var(--ink-4)" }}>选中正文 · 点「批注」即可划线标注。</div>
      </div>
    );
  }
  return (
    <div className="wr-anno-list">
      <div className="wr-block-h" style={{ marginBottom: 4 }}>本场批注（{marks.length}）</div>
      {marks.map((m, i) => (
        <button key={i} className="wr-anno-item" onClick={() => jump(m)}>
          <span className="wr-anno-item-q">{m.textContent}</span>
          <span className="wr-anno-item-n">{m.getAttribute("data-note") || "（无批注内容）"}</span>
        </button>
      ))}
    </div>
  );
}
function WrTabBtn({ id, cur, on, icon, label, badge }) {
  const Ic = I[icon] || I.Dot;
  return (<button role="tab" aria-selected={cur === id} tabIndex={cur === id ? 0 : -1} onKeyDown={onRovingTabKeyDown}
    className={`wr-tab ${cur === id ? "is-active" : ""}`} onClick={() => on(id)}><Ic size={14} /> {label}{badge && <span className="wr-tab-badge">{badge}</span>}</button>);
}
function WrCtxScene({ scene }) {
  const c = wrCtx(scene);
  /* 写作台 → AI 起草台的直达动线：这一场在目录里存在时，单场入列并跳转
     （此前整场起草只能绕道章节编排的「交给 AI」，三个台子像各自为战） */
  const inCatalog = (() => { try { return !!(WsCatalog && WsCatalog.sceneById(scene)); } catch (e) { return false; } })();
  const forkAI = () => {
    navigateWithViewIntent("scene", "ws:scene-enqueue", { sid: scene });
  };
  return (
    <>
      <section className="wr-block">
        <div className="wr-block-h">场景定位</div>
        <ul className="wr-meta">
          <li><span>POV</span><strong>{c.pov}</strong></li>
          <li><span>时间</span><strong>{c.time}</strong></li>
          <li><span>地点</span><strong>{c.place}</strong></li>
          <li><span>类型</span><strong className="text-serif">{c.type}</strong></li>
        </ul>
      </section>
      <section className="wr-block">
        <div className="wr-block-h">戏剧弧 · GMC</div>
        <ul className="wr-gmc">
          <li><div className="k">Goal · 目标</div><div className="v">{c.gmc.goal}</div></li>
          <li><div className="k">Conflict · 阻碍</div><div className="v">{c.gmc.conflict}</div></li>
          <li><div className="k">Setback · 挫折</div><div className="v">{c.gmc.setback}</div></li>
        </ul>
        {inCatalog && (
          <button className="btn btn-quiet btn-sm" style={{ marginTop: 10, width: "100%", justifyContent: "center" }} onClick={forkAI}
            title="把这一场送进 AI 起草台排队：按上面的三拍与雪花上下文起草整场，归档后写回这里的正文">
            <I.Play size={13} /> 交给 AI 起草整场
          </button>
        )}
      </section>
      <section className="wr-block">
        <div className="wr-block-h">出场角色</div>
        <div className="wr-chips">
          {c.cast.map(([name, role]) => (
            <span key={name} className="pill"><span className="pill-dot" />{name}（{role}）</span>
          ))}
        </div>
      </section>
    </>
  );
}
function WrCtxAI({ place, onOpenAI, onAdopt, onMerge, onAdoptText }) {
  const [running, setRunning] = useWS(false);
  const [cands, setCands] = useWS(place === "drawer" ? [] : null);
  const [picks, setPicks] = useWS({});
  const [err, setErr] = useWS("");
  const run = (instr) => {
    setRunning(true); setCands([]); setPicks({}); setErr("");
    wrContinueMulti(instr)
      .then(list => { setRunning(false); setCands(list); })
      .catch(e => {
        setRunning(false); setCands([]);
        setErr(e && e.code === "no-model" ? "AI 续写需要可用的 LLM：请到「系统设置 → 模型与接入」配置并启用后重试。" : "生成失败，请稍后重试。");
      });
  };
  const toggle = (id, si) => setPicks(p => { const cur = p[id] || []; return { ...p, [id]: cur.includes(si) ? cur.filter(x => x !== si) : [...cur, si] }; });
  return (
    <>
      {place === "tray" ? (
        <button className="btn btn-accent" style={{ width: "100%", justifyContent: "center" }} onClick={onOpenAI}><I.Sparkles size={15} /> 打开续写托盘 · ⌘J</button>
      ) : (
        <button className="btn btn-accent" style={{ width: "100%", justifyContent: "center" }} onClick={() => run()}><I.Wand size={14} /> {cands && cands.length ? "重新生成三条" : "生成三条候选"}</button>
      )}
      <div className="wr-block mt-4">
        <div className="wr-block-h">快捷动作</div>
        <div className="wr-chips">
          {["续写下一段", "让节奏更紧", "加感官细节", "删冗余", "改成对话推进"].map(x => (
            <button key={x} className="pill" style={{ cursor: "pointer" }} onClick={() => place === "drawer" ? run(x) : onOpenAI()}>{x}</button>
          ))}
        </div>
      </div>
      {place === "drawer" && err && (
        <div className="wr-block mt-4"><div className="wr-cand-note is-err">{err}</div></div>
      )}
      {place === "drawer" && (running || (cands && cands.length > 0)) && (
        <div className="wr-block mt-4">
          <div className="wr-block-h">三条候选 · 点句挑选</div>
          {running
            ? [0, 1, 2].map(i => (<div key={i} className="wr-cand wr-skel" style={{ marginBottom: 10, animation: "none", opacity: 1, transform: "none" }}><div className="sk" /><div className="sk" /><div className="sk short" /></div>))
            : cands.map((c, i) => {
              const picked = picks[c.id] || [];
              const sents = wrSentences(c.html);
              return (
                <article key={c.id} className="wr-cand" style={{ marginBottom: 10, animationDelay: i * 70 + "ms" }}>
                  <div className="wr-cand-head"><span className="wr-cand-key">{i + 1}</span><span className={`pill pill-${c.tone} text-xs`}><span className="pill-dot" />{c.approach}</span></div>
                  <WrCandText html={c.html} picked={picked} onToggle={(si) => toggle(c.id, si)} />
                  {c.note && <p className="wr-cand-note">{c.note}</p>}
                  <div className="wr-cand-act">
                    <button className="btn btn-quiet btn-sm" onClick={() => onMerge && onMerge(c)} title="作为可编辑草稿插入">融合</button>
                    {picked.length > 0
                      ? <button className="btn btn-accent btn-sm" onClick={() => onAdoptText && onAdoptText(wrPickedText(sents, picked))}>采纳选中 {picked.length} 句</button>
                      : <button className="btn btn-accent btn-sm" onClick={() => onAdopt(c)}>采纳全部</button>}
                  </div>
                </article>
              );
            })}
        </div>
      )}
    </>
  );
}
function WrCtxQC({ scene }) {
  const c = wrCtx(scene);
  const written = c.portrait.some(p => p[1] > 0);
  return (
    <>
      <section className="wr-block">
        <div className="wr-block-h">本场风险（{c.risks.length}）</div>
        {c.risks.length === 0
          ? (<div className="wr-qc" style={{ cursor: "default" }}><span className="pill pill-sage text-xs"><span className="pill-dot" />通过</span><span className="wr-qc-text">本场暂无质检风险。</span></div>)
          : c.risks.map(([tone, tag, text], i) => (
            <div key={i} className="wr-qc"><span className={`pill pill-${tone} text-xs`}><span className="pill-dot" />{tag}</span><span className="wr-qc-text">{text}</span></div>
          ))}
      </section>
      <section className="wr-block">
        <div className="wr-block-h">文学画像贴合</div>
        {written
          ? c.portrait.map(([label, pct, target, warn]) => (
            <div key={label} className="wr-bar-row"><span className="wr-bar-label">{label}</span><WrBar pct={pct} target={target} warn={warn} /><span className="wr-bar-val">{pct}%</span></div>
          ))
          : (<div className="text-sm" style={{ color: "var(--ink-4)", lineHeight: 1.6 }}>本场尚未起笔，画像贴合待生成。</div>)}
      </section>
    </>
  );
}
function WrBar({ pct, target, warn }) {
  return (<div className="wr-bar"><div className={`wr-bar-fill ${warn ? "warn" : ""}`} style={{ width: pct + "%" }} /><div className="wr-bar-target" style={{ left: target + "%" }} /></div>);
}
function wrNotesKey(scene) { return wsKey ? wsKey("wr-notes:" + scene) : "wr-notes:" + scene; }
function wrNotesPendingKey(scene) { return wsKey ? wsKey("wr-notes-pending:" + scene) : "wr-notes-pending:" + scene; }
const wrNotesSeed = () => "";
function WrCtxNotes({ scene }) {
  const [val, setVal] = useWS("");
  const [status, setStatus] = useWS("loading");
  const timer = useWR(null);
  const sceneState = useWR(null);
  const valueRef = useWR("");

  const persist = useWC((value, requestedState = sceneState.current) => {
    const state = requestedState;
    if (!state || !state.active) return Promise.resolve();
    const version = ++state.saveVersion;
    const persistedEditVersion = state.editVersion;
    if (sceneState.current === state) setStatus("saving");
    state.chain = state.chain.then(async () => {
      if (!state.active) return;
      try {
        const sceneId = state.backendSceneId || await WsCatalog.__backendSceneId(state.scene);
        if (!state.active) return;
        if (!sceneId) throw Object.assign(new Error("场景尚未同步到服务器"), { code: "SCENE_NOT_READY" });
        state.backendSceneId = sceneId;
        const data = await apiPatch(`/api/v1/scenes/${sceneId}/author-notes`, {
          notes: value,
          base_revision_no: state.revision,
        });
        const remoteRevision = Number(data && data.revision_no);
        state.revision = Number.isFinite(remoteRevision)
          ? Math.max(state.revision, remoteRevision)
          : state.revision + 1;
        if (version === state.saveVersion && persistedEditVersion === state.editVersion) {
          try { localStorage.removeItem(wrNotesPendingKey(state.scene)); } catch (e) {}
        }
        if (
          sceneState.current === state
          && state.active
          && version === state.saveVersion
          && persistedEditVersion === state.editVersion
        ) setStatus("saved");
      } catch (error) {
        if (!state.active) return;
        if (error && error.code === "SCENE_AUTHOR_NOTES_CONFLICT" && state.backendSceneId) {
          try {
            const current = await apiGet(`/api/v1/scenes/${state.backendSceneId}/author-notes`);
            const remoteRevision = Number(current && current.revision_no);
            if (Number.isFinite(remoteRevision)) state.revision = Math.max(state.revision, remoteRevision);
          } catch (e) {}
          if (sceneState.current === state && state.active && version === state.saveVersion) setStatus("conflict");
        } else if (sceneState.current === state && state.active && version === state.saveVersion) {
          setStatus("local");
        }
      }
    });
    return state.chain;
  }, []);

  useWE(() => {
    const state = {
      scene,
      active: true,
      backendSceneId: null,
      revision: 0,
      saveVersion: 0,
      editVersion: 0,
      chain: Promise.resolve(),
    };
    sceneState.current = state;
    let stored = null;
    let pending = false;
    try { stored = localStorage.getItem(wrNotesKey(scene)); } catch (e) {}
    try { pending = localStorage.getItem(wrNotesPendingKey(scene)) != null; } catch (e) {}
    const initial = stored != null ? stored : wrNotesSeed(scene);
    valueRef.current = initial;
    setVal(initial);
    setStatus(pending ? "local" : "loading");
    const loadEditVersion = state.editVersion;
    void (async () => {
      try {
        const sceneId = await WsCatalog.__backendSceneId(scene);
        if (!sceneId || !state.active || sceneState.current !== state) return;
        state.backendSceneId = sceneId;
        const data = await apiGet(`/api/v1/scenes/${sceneId}/author-notes`);
        if (!state.active || sceneState.current !== state) return;
        const remoteRevision = Number(data && data.revision_no);
        if (Number.isFinite(remoteRevision)) state.revision = Math.max(state.revision, remoteRevision);
        if (state.editVersion !== loadEditVersion) return;
        const serverValue = String((data && data.notes) || "");
        let currentStored = null;
        let currentPending = false;
        try { currentStored = localStorage.getItem(wrNotesKey(scene)); } catch (e) {}
        try { currentPending = localStorage.getItem(wrNotesPendingKey(scene)) != null; } catch (e) {}
        if (currentPending && currentStored != null && currentStored !== serverValue) {
          setStatus("conflict");
          return;
        }
        valueRef.current = serverValue;
        setVal(serverValue);
        try {
          localStorage.setItem(wrNotesKey(scene), serverValue);
          localStorage.removeItem(wrNotesPendingKey(scene));
        } catch (e) {}
        setStatus("saved");
      } catch (e) {
        if (state.active && sceneState.current === state && state.editVersion === loadEditVersion) {
          setStatus(stored != null ? "local" : "error");
        }
      }
    })();
    return () => {
      state.active = false;
      if (sceneState.current === state) sceneState.current = null;
      clearTimeout(timer.current);
    };
  }, [scene]);

  const onChange = (e) => {
    const v = e.target.value;
    const state = sceneState.current;
    if (state && state.scene === scene) state.editVersion += 1;
    valueRef.current = v;
    setVal(v);
    setStatus("saving");
    clearTimeout(timer.current);
    try {
      localStorage.setItem(wrNotesKey(scene), v);
      localStorage.setItem(wrNotesPendingKey(scene), String(Date.now()));
    } catch (e) {
      setStatus("error");
    }
    timer.current = setTimeout(() => { void persist(v, state); }, 500);
  };
  const statusText = {
    loading: "读取服务器…",
    saving: "保存中…",
    saved: "已存服务器",
    local: "未同步（已留本机）",
    conflict: "与其他设备冲突",
    error: "保存失败，请复制留底",
  }[status] || "保存状态未知";
  const healthy = status === "saved";
  return (
    <>
      <div className="wr-notes-bar">
        <span className="wr-notes-cap">本场笔记</span>
        <span className={`wr-notes-state ${healthy ? "" : "saving"}`}>
          <span className="wr-notes-dot" />{statusText}
          {(status === "local" || status === "conflict" || status === "error") && (
            <button type="button" className="btn btn-quiet btn-sm" onClick={() => { clearTimeout(timer.current); void persist(valueRef.current, sceneState.current); }}>重试</button>
          )}
        </span>
      </div>
      <textarea className="wr-notes" value={val} onChange={onChange} placeholder="只跟这一场有关的提醒、伏笔、待回收……（自动保存到服务器，按场独立）" />
    </>
  );
}

/* ---- AI tray ---- */
function WrTray({ open, onClose, onAdopt, onMerge, onAdoptText, sceneLabel, pov }) {
  const [prompt, setPrompt] = useWS("续写下一段，自然承接当前正文");
  const [phase, setPhase] = useWS("ready"); // ready | loading | result | error
  const [cands, setCands] = useWS([]);
  const [errMsg, setErrMsg] = useWS("");
  const [sel, setSel] = useWS(0);
  const [seed, setSeed] = useWS(0);
  const [picks, setPicks] = useWS({});
  const run = () => {
    setPhase("loading"); setPicks({}); setErrMsg("");
    wrContinueMulti(prompt)
      .then(list => { setCands(list); setPhase("result"); setSel(0); })
      .catch(e => {
        setCands([]);
        setErrMsg(e && e.code === "no-model" ? "AI 续写需要可用的 LLM：请到「系统设置 → 模型与接入」配置并启用后重试。" : "生成失败，请稍后重试。");
        setPhase("error");
      });
  };
  const toggle = (id, si) => setPicks(p => { const cur = p[id] || []; return { ...p, [id]: cur.includes(si) ? cur.filter(x => x !== si) : [...cur, si] }; });
  useWE(() => { if (open) { setSeed(s => s + 1); run(); } else setPhase("ready"); }, [open]); // eslint-disable-line
  useWE(() => {
    if (!open) return;
    const onKey = (e) => {
      if (phase !== "result") return;
      if (["1", "2", "3"].includes(e.key)) { e.preventDefault(); setSel(+e.key - 1); }
      else if (e.key === "Enter" && !e.shiftKey && document.activeElement.tagName !== "TEXTAREA") {
        e.preventDefault();
        const c = cands[sel]; if (!c) return;
        const p = picks[c.id] || [];
        if (p.length && onAdoptText) onAdoptText(wrPickedText(wrSentences(c.html), p));
        else onAdopt(c);
      }
      else if (e.key.toLowerCase() === "r" && document.activeElement.tagName !== "TEXTAREA") { setSeed(s => s + 1); run(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, phase, sel, picks, cands]); // eslint-disable-line
  return (
    <div className={`wr-tray ${open ? "show" : ""}`}>
      <div className="wr-tray-grip" />
      <header className="wr-tray-head">
        <span className="wr-tray-spark"><I.Sparkles size={16} /></span>
        <div><div className="wr-tray-title">AI 续写 · 三条候选</div><div className="wr-tray-sub">{sceneLabel || "—"}</div></div>
        <button className="wr-tray-x" onClick={onClose} title="关闭 (Esc)"><I.X size={16} /></button>
      </header>
      <div className="wr-tray-prompt">
        <textarea className="wr-prompt-in" rows={2} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
        <div className="wr-prompt-side">
          {pov && <div className="wr-prompt-chips"><span className="pill text-xs"><span className="pill-dot" />{pov}</span></div>}
          <button className="btn btn-primary btn-sm" onClick={() => { setSeed(s => s + 1); run(); }}><I.Wand size={13} /> {phase === "result" || phase === "error" ? "重新生成" : "生成"}</button>
        </div>
      </div>
      <div className="wr-cands" key={seed}>
        {phase === "loading"
          ? [0, 1, 2].map(i => (<div key={i} className="wr-cand wr-skel" style={{ animation: "none", opacity: 1, transform: "none" }}><div className="sk" /><div className="sk" /><div className="sk" /><div className="sk short" /></div>))
          : phase === "error"
          ? (<div className="wr-cand-note is-err">{errMsg}</div>)
          : phase === "result" && cands.map((c, i) => {
            const picked = picks[c.id] || [];
            const sents = wrSentences(c.html);
            return (
            <article key={c.id} className={`wr-cand ${sel === i ? "is-sel" : ""}`} style={{ animationDelay: i * 70 + "ms" }} onMouseEnter={() => setSel(i)} onClick={() => setSel(i)}>
              <div className="wr-cand-head"><span className="wr-cand-key">{i + 1}</span><span className={`pill pill-${c.tone} text-xs`}><span className="pill-dot" />{c.approach}</span>{picked.length > 0 && <span className="wr-cand-pickn">{picked.length} 句已选</span>}</div>
              <WrCandText html={c.html} picked={picked} onToggle={(si) => toggle(c.id, si)} />
              {c.note && <p className="wr-cand-note">{c.note}</p>}
              <div className="wr-cand-act">
                <button className="btn btn-quiet btn-sm" onClick={(e) => { e.stopPropagation(); onMerge && onMerge(c); }} title="作为可编辑草稿插入，自己揉合">融合</button>
                {picked.length > 0
                  ? <button className="btn btn-accent btn-sm" onClick={(e) => { e.stopPropagation(); onAdoptText && onAdoptText(wrPickedText(sents, picked)); }}>采纳选中 {picked.length} 句</button>
                  : <button className="btn btn-accent btn-sm" onClick={(e) => { e.stopPropagation(); onAdopt(c); }}>采纳全段</button>}
              </div>
            </article>
            );
          })}
      </div>
      <footer className="wr-tray-foot">
        <span><kbd style={WR_KBD}>1</kbd><kbd style={WR_KBD}>2</kbd><kbd style={WR_KBD}>3</kbd> 预览 · <kbd style={WR_KBD}>⏎</kbd> 采纳 · <kbd style={WR_KBD}>R</kbd> 重生 · <kbd style={WR_KBD}>Esc</kbd> 关闭</span>
        <span>点击句子可逐句挑选</span>
      </footer>
    </div>
  );
}
const WR_KBD = { fontFamily: "var(--font-mono)", fontSize: "10px", padding: "1px 5px", borderRadius: "4px", background: "var(--paper-3)", color: "var(--ink-3)", border: "1px solid var(--line-1)", marginRight: "3px" };

/* ==========================================================
   Inline rewrite — select prose, summon a toolbar, rewrite the
   selection in-place via the backend passage-patch pipeline (G4).
   ========================================================== */
const WR_RW_ACTIONS = [
  { id: "polish",   label: "润色",   instr: "在不改变原意与人称的前提下润色这段文字，使其更精炼、更有文学质感" },
  { id: "shorter",  label: "更凝练", instr: "把这段文字改写得更凝练简短，删去冗余与可省的修饰，保留关键意象" },
  { id: "concrete", label: "更具象", instr: "把这段文字改写得更具象可感，增加克制的细节与动作，避免空泛与抽象" },
  { id: "dialogue", label: "对话化", instr: "把这段叙述改写为以对话推进的形式，符合人物口吻，保留必要的动作提示" },
];
function wrToneInstr(t) {
  const lean = (v, lo, hi) => v <= 22 ? ("明显更" + lo) : v <= 42 ? ("略" + lo) : v >= 78 ? ("明显更" + hi) : v >= 58 ? ("略" + hi) : null;
  const parts = [
    lean(t.warm, "冷峻克制", "温情柔软"),
    lean(t.expand, "凝练简短", "铺陈细腻"),
    lean(t.direct, "含蓄留白", "直白有力"),
  ].filter(Boolean);
  if (!parts.length) return "在保持原意与人称的前提下，做一次自然的文学性润色";
  return "调整文字的语气，使其" + parts.join("、") + "；保持原意与人称不变";
}
function WrToneSlider({ label, lo, hi, value, onChange }) {
  return (
    <div className="wr-tune-row">
      <div className="wr-tune-poles"><span>{lo}</span><span className="wr-tune-label">{label}</span><span>{hi}</span></div>
      <input type="range" min="0" max="100" value={value} className="wr-tune-range" onChange={(e) => onChange(+e.target.value)} />
    </div>
  );
}
function wrParseVariants(out) {
  let parts = (out || "").split(/\n?\s*~~~+\s*\n?/).map(s => s.trim()).filter(Boolean);
  if (parts.length < 2) parts = (out || "").split(/\n{2,}/).map(s => s.trim()).filter(Boolean);
  parts = parts.map(s => s
    .replace(/^\s*(版本\s*[一二三四1234][：:、.\)]?|[1234][.\)、])\s*/, "")
    .replace(/^["「『]+|["」』]+$/g, "")
    .trim());
  return parts.filter(Boolean).slice(0, 3);
}
/* FE-ALIGN G4：内联改写接后端 passages/patch-candidates（writer_passage_patch
   节点；提示词由 config/prompts.yaml 组装，指令/语气走 issue_dimension 自由文本）。
   采纳/弃用经 accept/reject 回传——这是作者偏好画像的学习闭环。 */
let WR_ACTIVE_SID = null;   // 当前在写场景（WriterRoom 镜像）
let wrPatchLast = null;     // { patchId, options } —— 待裁决的最近一次候选

async function wrRewriteMulti(text, instr) {
  let sceneId = null;
  try {
    const sid = WR_ACTIVE_SID || (((WsCatalog && WsCatalog.writingScene()) || {}).scene || {}).sid;
    sceneId = sid && WsCatalog && WsCatalog.__backendSceneId ? await WsCatalog.__backendSceneId(sid) : null;
  } catch (e) {}
  if (!sceneId) { const err = new Error("no-scene"); err.code = "no-model"; throw err; }
  let cand = null;
  try {
    const data = await apiPost("/api/v1/passages/patch-candidates", {
      object_type: "scene",
      object_id: sceneId,
      scene_id: sceneId,
      source_excerpt: String(text || "").slice(0, 2000),
      issue_dimension: instr,
    });
    cand = data && data.candidate;
  } catch (e) {
    const err = new Error("no-model"); err.code = "no-model"; err.detail = e && e.message; throw err;
  }
  const options = (cand && cand.replacement_options) || [];
  // 离线兜底产物是确定性占位改写——按「模型不可用」如实处理，不冒充真实改写
  if (!options.length || /offline deterministic/i.test((cand && cand.rationale) || "")) {
    const err = new Error("no-model"); err.code = "no-model"; throw err;
  }
  wrPatchLast = { patchId: cand.patch_id, options };
  return options.slice(0, 3).map(o => String(o.replacement_text || "").trim()).filter(Boolean);
}

/* 候选裁决回传（替换=accept 选中项 / 关闭未采纳=reject）；幂等：决一次即清 */
function wrPatchDecide(pickIdx, accepted) {
  const last = wrPatchLast;
  if (!last) return;
  wrPatchLast = null;
  const opt = last.options[pickIdx] || last.options[0] || {};
  if (accepted) apiPost(`/api/v1/passage-patch-candidates/${last.patchId}/accept`, { selected_option_id: opt.option_id || "" }).catch(() => {});
  else apiPost(`/api/v1/passage-patch-candidates/${last.patchId}/reject`, {}).catch(() => {});
}

/* ==========================================================
   AI 续写 — 接后端 author-drafts 的 proposals/generate（continuation
   类型："只推进下一拍，不改写作者现有正文"，见 config/prompts.yaml
   author_proposal_generate 模板）。一次 generate-set 业务意图在服务端生成
   action / relationship / suspense 三个明确标槽的续写；复用 wrRewriteMulti
   同一套 WR_ACTIVE_SID → WrDocs.draftId 映射。
   离线兜底（LLM 未启用）与内联改写同一约定：rationale 命中
   /offline deterministic/i 即按"模型不可用"处理，不混入候选列表。
   ========================================================== */
function wrCandEscape(s) { return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function wrCandTidy(s) { return String(s || "").replace(/\s*\n\s*/g, "").trim(); }

async function wrContinueMulti(instruction) {
  const sid = WR_ACTIVE_SID || (((WsCatalog && WsCatalog.writingScene()) || {}).scene || {}).sid;
  if (!sid) { const err = new Error("no-scene"); err.code = "no-model"; throw err; }
  let draftId = null;
  try { draftId = await WrDocs.draftId(sid); } catch (e) {}
  if (!draftId) { const err = new Error("no-draft"); err.code = "no-model"; throw err; }
  const generated = await apiPost(`/api/v1/author-drafts/${draftId}/proposals/generate-set`, {
    mode: "continuation_variants",
    instruction: instruction || "续写下一段，自然承接当前正文",
  });
  const cands = [];
  const proposals = Array.isArray(generated && generated.proposals) ? generated.proposals : [];
  proposals.forEach((p) => {
    const text = wrCandTidy(p && p.content);
    if (!text) return;
    // 离线兜底产物是确定性占位续写——按「模型不可用」如实处理，不混进候选里
    if (/offline deterministic/i.test((p && p.rationale) || "")) return;
    cands.push({
      id: (p && p.proposal_id) || ("cand" + cands.length),
      approach: `候选 ${cands.length + 1}`,
      tone: WR_CAND_TONES[cands.length % WR_CAND_TONES.length],
      note: (p && p.rationale) || "",
      html: wrCandEscape(text),
    });
  });
  if (!cands.length) { const err = new Error("no-model"); err.code = "no-model"; throw err; }
  return cands;
}

function WrInlineRewrite({ editorRef, onCommit }) {
  const [rect, setRect] = useWS(null);
  const [phase, setPhase] = useWS("idle"); // idle | custom | loading | result | error
  const [results, setResults] = useWS([]);
  const [pick, setPick] = useWS(0);
  const [errMsg, setErrMsg] = useWS("");
  const [custom, setCustom] = useWS("");
  const lastInstr = useWR(null);
  const rangeRef = useWR(null);
  const selRef = useWR("");
  const popRef = useWR(null);
  const [popTop, setPopTop] = useWS(null);
  const [annoText, setAnnoText] = useWS("");
  const [annoNew, setAnnoNew] = useWS(false);
  const [tone, setTone] = useWS({ warm: 50, expand: 50, direct: 50 });
  const annoElRef = useWR(null);
  const revElRef = useWR(null);

  useWE(() => {
    const onSel = () => {
      if (phase !== "idle") return;
      const ed = editorRef.current;
      const sel = window.getSelection();
      if (!ed || !sel || sel.isCollapsed || sel.rangeCount === 0) { setRect(null); return; }
      const range = sel.getRangeAt(0);
      if (!ed.contains(range.commonAncestorContainer)) { setRect(null); return; }
      const text = sel.toString();
      if (text.trim().length < 2) { setRect(null); return; }
      rangeRef.current = range.cloneRange();
      selRef.current = text;
      const r = range.getBoundingClientRect();
      setRect({ top: r.top, bottom: r.bottom, left: r.left + r.width / 2 });
    };
    document.addEventListener("selectionchange", onSel);
    return () => document.removeEventListener("selectionchange", onSel);
  }, [phase]);

  useWE(() => {
    const onKey = (e) => { if (e.key === "Escape" && (rect || phase !== "idle")) { setPhase("idle"); setRect(null); } };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [rect, phase]);

  // measure the popover and clamp it inside the viewport (prevents header clipping)
  useWE(() => {
    if (!rect || phase === "idle") { setPopTop(null); return; }
    const el = popRef.current; if (!el) return;
    const h = el.offsetHeight;
    const prefBelow = rect.bottom < window.innerHeight * 0.5;
    let t = prefBelow ? rect.bottom + 10 : rect.top - 10 - h;
    t = Math.min(Math.max(12, t), window.innerHeight - h - 12);
    setPopTop(t);
  }, [rect, phase, results, errMsg, custom, annoText, tone]);

  // open an existing annotation when its highlight is clicked
  useWE(() => {
    const onClick = (e) => {
      const ed = editorRef.current; if (!ed || !e.target.closest) return;
      const rev = e.target.closest(".wr-rev");
      if (rev && ed.contains(rev)) {
        const sel = window.getSelection();
        if (!sel || sel.isCollapsed) {
          e.preventDefault();
          revElRef.current = rev;
          const r = rev.getBoundingClientRect();
          setRect({ top: r.top, bottom: r.bottom, left: r.left + r.width / 2 });
          setPhase("rev");
          return;
        }
      }
      const m = e.target.closest(".wr-anno");
      if (m && ed.contains(m)) {
        const sel = window.getSelection();
        if (!sel || sel.isCollapsed) {
          e.preventDefault();
          annoElRef.current = m;
          setAnnoText(m.getAttribute("data-note") || "");
          setAnnoNew(false);
          const r = m.getBoundingClientRect();
          setRect({ top: r.top, bottom: r.bottom, left: r.left + r.width / 2 });
          setPhase("anno");
        }
      }
    };
    document.addEventListener("click", onClick, true);
    return () => document.removeEventListener("click", onClick, true);
  }, []);

  const run = async (instr) => {
    lastInstr.current = instr;
    setPhase("loading");
    try { const arr = await wrRewriteMulti(selRef.current, instr); setResults(arr); setPick(0); setPhase("result"); }
    catch (err) { setErrMsg(err && err.code === "no-model" ? "实时改写需要可用的 LLM：请到「系统设置 → 模型与接入」启用后重试。" : "改写失败，请稍后重试。"); setPhase("error"); }
  };
  const doReplace = () => {
    const range = rangeRef.current;
    const chosen = results[pick];
    if (range && chosen) {
      try {
        const span = document.createElement("span");
        span.className = "wr-rev";
        span.setAttribute("data-orig", selRef.current);
        span.textContent = chosen;
        range.deleteContents(); range.insertNode(span);
      } catch (e) {}
      const sel = window.getSelection(); if (sel) sel.removeAllRanges();
      onCommit && onCommit();
      wrPatchDecide(pick, true); // G4：采纳回传（学习偏好）
    }
    close();
  };
  const close = () => { wrPatchDecide(0, false); /* G4：未采纳即弃用回传（已裁决则 no-op） */ setPhase("idle"); setRect(null); setResults([]); setPick(0); setErrMsg(""); setCustom(""); setAnnoText(""); annoElRef.current = null; revElRef.current = null; };

  const unwrapAnno = (m) => {
    const parent = m && m.parentNode; if (!parent) return;
    while (m.firstChild) parent.insertBefore(m.firstChild, m);
    parent.removeChild(m);
    if (parent.normalize) parent.normalize();
  };
  const startAnno = () => {
    const range = rangeRef.current; if (!range) return;
    const m = document.createElement("mark");
    m.className = "wr-anno";
    try { range.surroundContents(m); }
    catch (e) { try { m.appendChild(range.extractContents()); range.insertNode(m); } catch (_) { return; } }
    annoElRef.current = m;
    setAnnoText(""); setAnnoNew(true);
    const r = m.getBoundingClientRect();
    setRect({ top: r.top, bottom: r.bottom, left: r.left + r.width / 2 });
    setPhase("anno");
    const sel = window.getSelection(); if (sel) sel.removeAllRanges();
  };
  const saveAnno = () => {
    const m = annoElRef.current; const t = annoText.trim();
    if (m) {
      if (!t) unwrapAnno(m);
      else { m.setAttribute("data-note", t); m.setAttribute("title", t); }
    }
    onCommit && onCommit();
    window.dispatchEvent(new CustomEvent("ws:anno-change"));
    close();
  };
  const deleteAnno = () => { if (annoElRef.current) unwrapAnno(annoElRef.current); onCommit && onCommit(); window.dispatchEvent(new CustomEvent("ws:anno-change")); close(); };
  const revertRev = () => {
    const span = revElRef.current;
    if (span && span.parentNode) { const parent = span.parentNode; parent.replaceChild(document.createTextNode(span.getAttribute("data-orig") || ""), span); if (parent.normalize) parent.normalize(); }
    onCommit && onCommit(); close();
  };
  const acceptRev = () => { if (revElRef.current) unwrapAnno(revElRef.current); onCommit && onCommit(); close(); };
  const cancelAnno = () => { if (annoNew && annoElRef.current) unwrapAnno(annoElRef.current); close(); };

  if (!rect) return null;
  const below = rect.top < 170;
  const pos = (w) => ({
    top: below ? rect.bottom + 8 : rect.top - 8,
    left: Math.min(Math.max(rect.left, w / 2 + 12), window.innerWidth - w / 2 - 12),
    transform: below ? "translate(-50%, 0)" : "translate(-50%, -100%)",
  });

  if (phase === "idle") {
    return (
      <div className="wr-irw-bar" style={pos(448)} onMouseDown={(e) => e.preventDefault()}>
        <span className="wr-irw-spark"><I.Sparkles size={13} /></span>
        {WR_RW_ACTIONS.map(a => (
          <button key={a.id} className="wr-irw-btn" onClick={() => run(a.instr)}>{a.label}</button>
        ))}
        <span className="wr-irw-sep" />
        <button className="wr-irw-btn" onClick={startAnno}>批注</button>
        <button className="wr-irw-btn" onClick={() => setPhase("tune")}>调音</button>
        <button className="wr-irw-btn accent" onClick={() => setPhase("custom")}>自定义…</button>
      </div>
    );
  }
  if (phase === "rev") {
    const span = revElRef.current;
    const orig = span ? (span.getAttribute("data-orig") || "") : "";
    const now = span ? span.textContent : "";
    return (
      <div className="wr-irw-pop" ref={popRef}
        style={{ top: popTop != null ? popTop : (rect.bottom + 10), left: Math.min(Math.max(rect.left, 192), window.innerWidth - 192), transform: "translateX(-50%)" }}
        onMouseDown={(e) => e.stopPropagation()}>
        <div className="wr-irw-head"><I.Sparkles size={14} /> AI 改动 <span className="sp">可还原</span></div>
        <div className="wr-irw-body">
          <div className="wr-rev-row"><span className="wr-rev-tag">原文</span><div className="wr-irw-orig" style={{ margin: 0, flex: 1 }}>{orig}</div></div>
          <div className="wr-rev-row" style={{ marginTop: 10 }}><span className="wr-rev-tag now">改写</span><div className="wr-irw-new" style={{ fontSize: 14, flex: 1 }}>{now}</div></div>
        </div>
        <div className="wr-irw-foot">
          <button className="btn btn-quiet btn-sm" onClick={close}>关闭</button>
          <button className="btn btn-ghost btn-sm" onClick={revertRev}>还原原文</button>
          <button className="btn btn-accent btn-sm" onClick={acceptRev}>接受改动</button>
        </div>
      </div>
    );
  }
  if (phase === "anno") {
    return (
      <div className="wr-irw-pop" ref={popRef}
        style={{ top: popTop != null ? popTop : (rect.bottom + 10), left: Math.min(Math.max(rect.left, 192), window.innerWidth - 192), transform: "translateX(-50%)" }}
        onMouseDown={(e) => e.stopPropagation()}>
        <div className="wr-irw-head"><I.FileText size={14} /> 批注 <span className="sp">{annoNew ? "新建" : "已锚定"}</span></div>
        <div className="wr-anno-note">
          <textarea autoFocus value={annoText} placeholder="写下对这段文字的批注、疑问或待办…" onChange={(e) => setAnnoText(e.target.value)} />
        </div>
        <div className="wr-irw-foot">
          <button className="btn btn-quiet btn-sm" onClick={cancelAnno}>{annoNew ? "取消" : "关闭"}</button>
          {!annoNew && <button className="btn btn-ghost btn-sm" onClick={deleteAnno}>删除批注</button>}
          <button className="btn btn-accent btn-sm" onClick={saveAnno}>保存</button>
        </div>
      </div>
    );
  }
  return (
    <div className="wr-irw-pop" ref={popRef}
      style={{ top: popTop != null ? popTop : (rect.bottom + 10), left: Math.min(Math.max(rect.left, 192), window.innerWidth - 192), transform: "translateX(-50%)" }}
      onMouseDown={(e) => e.stopPropagation()}>
      <div className="wr-irw-head"><I.Sparkles size={14} /> AI 内联改写{phase === "result" && results.length > 1 ? ` · ${results.length} 版` : ""} <span className="sp">{selRef.current.length} 字</span></div>
      {phase === "custom" && (
        <div className="wr-irw-custom">
          <input className="wr-irw-input" autoFocus value={custom} placeholder="如：更冷一点、删掉比喻、加一个动作…"
            onChange={(e) => setCustom(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && custom.trim()) { e.preventDefault(); run(custom.trim()); } }} />
          <button className="btn btn-accent btn-sm" disabled={!custom.trim()} onClick={() => custom.trim() && run(custom.trim())}>改写</button>
        </div>
      )}
      {phase === "tune" && (
        <>
          <div className="wr-tune">
            <WrToneSlider label="语气" lo="冷峻" hi="温情" value={tone.warm} onChange={(v) => setTone(t => ({ ...t, warm: v }))} />
            <WrToneSlider label="繁简" lo="凝练" hi="铺陈" value={tone.expand} onChange={(v) => setTone(t => ({ ...t, expand: v }))} />
            <WrToneSlider label="显隐" lo="含蓄" hi="直白" value={tone.direct} onChange={(v) => setTone(t => ({ ...t, direct: v }))} />
            <div className="wr-tune-prev">{wrToneInstr(tone)}</div>
          </div>
          <div className="wr-irw-foot">
            <button className="btn btn-quiet btn-sm" onClick={close}>取消</button>
            <button className="btn btn-accent btn-sm" onClick={() => run(wrToneInstr(tone))}>按此生成三版</button>
          </div>
        </>
      )}
      {phase === "loading" && (<div className="wr-irw-load"><span className="wr-irw-spin" /> AI 改写中 · 生成多个版本…</div>)}
      {phase === "error" && (
        <>
          <div className="wr-irw-body"><div className="wr-irw-new is-err">{errMsg}</div></div>
          <div className="wr-irw-foot"><button className="btn btn-quiet btn-sm" onClick={close}>取消</button></div>
        </>
      )}
      {phase === "result" && (
        <>
          <div className="wr-irw-body">
            <div className="wr-irw-orig">{selRef.current}</div>
            <div className="wr-irw-cands">
              {results.map((r, i) => (
                <button key={i} className={`wr-irw-cand ${pick === i ? "is-sel" : ""}`} onClick={() => setPick(i)}>
                  <span className="wr-irw-cand-k">{i + 1}</span>
                  <span className="wr-irw-cand-t">{r}</span>
                </button>
              ))}
            </div>
          </div>
          <div className="wr-irw-foot">
            <button className="btn btn-quiet btn-sm" onClick={close}>取消</button>
            <button className="btn btn-ghost btn-sm" onClick={() => run(lastInstr.current || WR_RW_ACTIONS[0].instr)}>重写</button>
            <button className="btn btn-accent btn-sm" onClick={doReplace}>替换为第 {pick + 1} 版</button>
          </div>
        </>
      )}
    </div>
  );
}


/* ESM 导出（Phase 1 机械追加；window.* 赋值过渡期保留） */
export {
  WriterRoom,
  WriterTweaks,
  WRITER_TWEAK_DEFAULTS,
  WrCtxNotes,
  wrSeedHTML,
  wrNotesSeed,
  wrContinueMulti,
  wrPickedText,
  wrPlainText,
  wrSentences,
};

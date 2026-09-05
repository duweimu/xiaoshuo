import React from "react";
import { I } from "./icons.jsx";
import { WsWorks } from "./ws-works.jsx";
import { WsCatalog } from "./ws-catalog.jsx";

/* global React, I */
/* ==========================================================
   WsPalette — 全局命令面板 (⌘K)
   Calm, keyboard-first. Fuzzy-jump to any scene / step, or run
   an action. The connective tissue of the whole workspace.
   ========================================================== */
const { useState: usePS, useEffect: usePE, useRef: usePR, useMemo: usePM } = React;

/* ---- jumpable data (mirrors the rest of the app) ---- */
const PAL_SCENES = [];  // 场景跳转真相来自 WsCatalog；无目录时列表为空
const PAL_STEPS = [
  { key: "audience", num: "01", name: "读者定位" }, { key: "logline", num: "02", name: "一句话概括" },
  { key: "paragraph", num: "03", name: "一段话概括" }, { key: "characters", num: "04", name: "角色摘要表" },
  { key: "synopsis", num: "05", name: "一页梗概" }, { key: "backstory", num: "06", name: "角色背景" },
  { key: "outline", num: "07", name: "长篇大纲" }, { key: "profile", num: "08", name: "角色全档案" },
  { key: "scenes", num: "09", name: "场景列表" }, { key: "planning", num: "10", name: "场景规划" },
];

/* ---- fuzzy subsequence match w/ light scoring ---- */
function fuzzy(q, text) {
  if (!q) return { ok: true, score: 0 };
  q = q.toLowerCase(); const t = text.toLowerCase();
  if (t.includes(q)) return { ok: true, score: 100 - t.indexOf(q) };
  let qi = 0, score = 0, prev = -2;
  for (let i = 0; i < t.length && qi < q.length; i++) {
    if (t[i] === q[qi]) { score += (i === prev + 1 ? 5 : 1); prev = i; qi++; }
  }
  return { ok: qi === q.length, score };
}

function WsPalette({ open, onClose, run, theme }) {
  const [q, setQ] = usePS("");
  const [sel, setSel] = usePS(0);
  const inputRef = usePR(null);
  const listRef = usePR(null);

  usePE(() => {
    if (!open) return undefined;
    setQ("");
    setSel(0);
    const timer = window.setTimeout(() => inputRef.current?.focus(), 30);
    return () => window.clearTimeout(timer);
  }, [open]);

  /* build command set */
  const all = usePM(() => {
    const cmds = [];

    // 作品 — 切换 / 新建（数据来自 WsWorks store）
    const works = (WsWorks ? WsWorks.list() : []);
    const activeId = (WsWorks ? WsWorks.activeId() : null);
    works.forEach(w => {
      if (w.id === activeId) return;
      cmds.push({ g: "作品", icon: "BookOpen", label: `切换到《${w.title}》`, hint: w.genre,
        kw: `work zuopin qiehuan ${w.title} ${w.genre}`, run: () => run({ type: "work", workId: w.id }) });
    });
    cmds.push({ g: "作品", icon: "Plus", label: "新建作品", hint: "新书",
      kw: "new work xinjian zuopin xinshu", run: () => run({ type: "new-work" }) });

    cmds.push({ g: "导航", icon: "Home", label: "回到主页", hint: "主页", kw: "home zhuye shouye", run: () => run({ type: "go", view: "home" }) });
    cmds.push({ g: "导航", icon: "GitBranch", label: "创作流程地图", hint: "流程", kw: "flowmap liucheng pipeline", run: () => run({ type: "go", view: "flowmap" }) });
    cmds.push({ g: "导航", icon: "Snowflake", label: "打开构思 · 雪花十步", hint: "构思", kw: "snowflake gousi xuehua", run: () => run({ type: "go", view: "snowflake" }) });
    cmds.push({ g: "导航", icon: "Pen", label: "进入写作房间", hint: "写作", kw: "writer xiezuo", run: () => run({ type: "go", view: "writer" }) });
    cmds.push({ g: "导航", icon: "Beaker", label: "风格参考 · 维度矩阵", hint: "风格", kw: "styleref fengge canzhao", run: () => run({ type: "go", view: "styleref" }) });
    cmds.push({ g: "导航", icon: "Inbox", label: "查看待办收件箱", hint: "待办", kw: "review daiban shoujianxiang", run: () => run({ type: "go", view: "review" }) });
    cmds.push({ g: "导航", icon: "Library", label: "资料库 · 人物 / 设定 / 知识", hint: "资料", kw: "library ziliao renwu sheding", run: () => run({ type: "go", view: "library" }) });

    cmds.push({ g: "导航 · 生产", icon: "Layout", label: "章节编排", hint: "高级", kw: "author zhangjie bianpai", run: () => run({ type: "go", view: "author" }) });
    cmds.push({ g: "导航 · 生产", icon: "Play", label: "AI 起草台", hint: "高级", kw: "scene ai qicao changjing gongzuotai", run: () => run({ type: "go", view: "scene" }) });
    cmds.push({ g: "导航 · 生产", icon: "BookOpen", label: "成稿中心", hint: "高级", kw: "manuscripts chenggao", run: () => run({ type: "go", view: "manuscripts" }) });
    cmds.push({ g: "导航 · 生产", icon: "Microscope", label: "写作台 · 深改姿态", hint: "原深改台", kw: "deepdesk shengai shenxiu", run: () => run({ type: "writer-action", action: "deep" }) });
    // 长篇控制塔已并入「章节编排」的「全书编排」（故事弧线 / 线索织布机 / 节奏镜头 / 全书体检）。
    cmds.push({ g: "导航 · 运维", icon: "UploadCloud", label: "发布索引", hint: "高级", kw: "index fabu suoyin", run: () => run({ type: "go", view: "index" }) });
    cmds.push({ g: "导航 · 运维", icon: "FileInput", label: "互操作与导出", hint: "高级", kw: "interop hucaozuo daoru daochu", run: () => run({ type: "go", view: "interop" }) });
    cmds.push({ g: "导航 · 系统", icon: "Settings", label: "系统设置", hint: "设置", kw: "settings shezhi", run: () => run({ type: "go", view: "settings" }) });
    cmds.push({ g: "导航 · 系统", icon: "Trash", label: "回收站", hint: "系统", kw: "trash huishouzhan", run: () => run({ type: "go", view: "trash" }) });

    cmds.push({ g: "动作", icon: "Sparkles", label: "AI 续写当前场景", hint: "⌘J", kw: "ai xuxie sparkles", run: () => run({ type: "writer-action", action: "ai" }) });
    cmds.push({ g: "动作", icon: "Eye", label: "进入沉浸写作", hint: "⌘.", kw: "immersion chenjin zhuanzhu", run: () => run({ type: "writer-action", action: "immersion" }) });
    cmds.push({ g: "动作", icon: "Sliders", label: "调节舒适度 · 打开 Tweaks", kw: "tweaks shezhi shushidu", run: () => run({ type: "tweaks" }) });
    cmds.push({ g: "动作", icon: theme === "night" ? "Sun" : "Moon", label: theme === "night" ? "切换到 白昼主题" : "切换到 夜灯主题", kw: "theme zhuti yejian baizhou", run: () => run({ type: "theme", value: theme === "night" ? "day" : "night" }) });
    cmds.push({ g: "动作", icon: "Type", label: "切换到 暮色主题", kw: "theme dusk muse", run: () => run({ type: "theme", value: "dusk" }) });

    /* 场景跳转：派生自 WsCatalog（与大纲 / 主页同源），缺席时回退静态表 */
    const palScenes = (() => {
      try {
        if (!WsCatalog) return PAL_SCENES;
        const out = [];
        WsCatalog.get().forEach(c => (c.scenes || []).forEach(s => {
          out.push({ ch: c.n, chTitle: c.title, id: s.sid, title: s.title, state: s.state === "writing" ? "active" : (s.state || "todo") });
        }));
        // 在写的场景排最前，最多 12 条，避免淹没命令列表
        out.sort((a, b) => (a.state === "active" ? -1 : 0) - (b.state === "active" ? -1 : 0));
        return out.slice(0, 12);
      } catch (e) { return PAL_SCENES; }
    })();
    palScenes.forEach(s => cmds.push({
      g: "跳转 · 场景", icon: "FileText", label: s.title, hint: `CH ${s.ch} · ${s.chTitle}`, state: s.state,
      kw: `${s.title} ${s.chTitle} ch${s.ch}`, run: () => run({ type: "scene", sceneId: s.id })
    }));
    PAL_STEPS.forEach(s => cmds.push({
      g: "跳转 · 构思", icon: "Compass", label: `${s.num} · ${s.name}`, hint: "雪花",
      kw: `${s.name} ${s.num} xuehua`, run: () => run({ type: "step", key: s.key })
    }));
    return cmds;
  }, [run, theme]);

  const results = usePM(() => {
    const scored = all.map(c => {
      const m = fuzzy(q, c.label + " " + (c.kw || "") + " " + (c.hint || ""));
      return { c, ...m };
    }).filter(x => x.ok).sort((a, b) => b.score - a.score);
    return q ? scored.map(x => x.c) : all;
  }, [q, all]);

  /* group while preserving order */
  const groups = usePM(() => {
    const order = []; const map = {};
    results.forEach(c => { if (!map[c.g]) { map[c.g] = []; order.push(c.g); } map[c.g].push(c); });
    return order.map(g => ({ g, items: map[g] }));
  }, [results]);

  const flat = usePM(() => groups.flatMap(gr => gr.items), [groups]);

  usePE(() => { if (sel >= flat.length) setSel(Math.max(0, flat.length - 1)); }, [flat.length]);

  usePE(() => {
    if (!open) return;
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); }
      else if (e.key === "ArrowDown") { e.preventDefault(); setSel(s => Math.min(flat.length - 1, s + 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); setSel(s => Math.max(0, s - 1)); }
      else if (e.key === "Enter") { e.preventDefault(); const c = flat[sel]; if (c) { c.run(); onClose(); } }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [open, flat, sel, onClose]);

  usePE(() => {
    if (!open || !listRef.current) return;
    const el = listRef.current.querySelector(`[data-i="${sel}"]`);
    if (el) el.scrollIntoView ? el.scrollIntoView({ block: "nearest" }) : null;
  }, [sel, open]);

  if (!open) return null;

  let running = -1;
  return (
    <div className="pal-wrap" onMouseDown={onClose}>
      <div className="pal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="pal-search">
          <I.Search size={18} />
          <input ref={inputRef} className="pal-input" value={q} placeholder="跳转到场景 / 构思步骤，或输入命令…"
            onChange={(e) => { setQ(e.target.value); setSel(0); }} spellCheck={false} />
          <kbd className="pal-esc">Esc</kbd>
        </div>

        <div className="pal-list" ref={listRef}>
          {groups.length === 0 && (
            <div className="pal-empty"><I.Search size={22} /><span>没有匹配「{q}」的结果</span></div>
          )}
          {groups.map(gr => (
            <div className="pal-group" key={gr.g}>
              <div className="pal-group-h">{gr.g}</div>
              {gr.items.map(c => {
                running++;
                const i = running;
                const Ic = I[c.icon] || I.Dot;
                return (
                  <button key={i} data-i={i} className={`pal-item ${sel === i ? "is-sel" : ""}`}
                    onMouseEnter={() => setSel(i)} onClick={() => { c.run(); onClose(); }}>
                    <span className="pal-item-ic"><Ic size={17} /></span>
                    <span className="pal-item-label">{c.label}</span>
                    {c.state && <span className={`pal-dot s-${c.state}`} />}
                    {c.hint && <span className="pal-item-hint">{c.hint}</span>}
                    {sel === i && <span className="pal-enter"><I.ArrowRight size={13} /></span>}
                  </button>
                );
              })}
            </div>
          ))}
        </div>

        <div className="pal-foot">
          <span><kbd>↑</kbd><kbd>↓</kbd> 选择</span>
          <span><kbd>↵</kbd> 打开</span>
          <span><kbd>esc</kbd> 关闭</span>
          <span className="pal-foot-spacer" />
          <span className="pal-foot-tip">随时按 <kbd>⌘K</kbd> 唤出</span>
        </div>
      </div>
    </div>
  );
}

export { WsPalette };

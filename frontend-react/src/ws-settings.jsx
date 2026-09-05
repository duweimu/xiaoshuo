import React from "react";
import { I } from "./icons.jsx";
import { WsWorks, useActiveWork } from "./ws-works.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { AISettings } from "./ws-settings-ai.jsx";
import { Section, Row, Toggle, Segmented, usePref } from "./ws-settings-shared.jsx";

/* global React, I */
const { useState: useSt6 } = React;

/* ==========================================================
   Settings — 设置
   Tabs: 项目 · 写作偏好 · AI 模型 · 外观 · 数据
   ----------------------------------------------------------
   · 项目页读写 WsWorks（当前作品），状态由 WsCatalog 实时汇总
   · 写作偏好 / AI 偏好持久化到全局 ws_prefs_v1
   · 外观直接接 tweaks（与「调节舒适度」同源）
   · 数据页：导入导出跳转真实模块；危险区是真实操作
   ========================================================== */

const S_TABS = [
  { id: "project",  label: "项目", icon: "Folder" },
  { id: "writing",  label: "写作偏好", icon: "Pen" },
  { id: "ai",       label: "AI 模型", icon: "Sparkles" },
  { id: "appear",   label: "外观",  icon: "Type" },
  { id: "data",     label: "数据 & 安全", icon: "ShieldCheck" },
];

function WsSettings({ go, t, setTweak }) {
  const [tab, setTab] = useSt6("project");

  return (
    <div className="page" data-screen-label="settings">
      <div className="page-narrow">
        <header className="page-header">
          <div>
            <div className="page-eyebrow">设置</div>
            <h1 className="page-title">让系统配合你，不是你配合系统</h1>
            <p className="page-subtitle">「项目」只影响当前作品；其余偏好是全局的。所有修改即改即存。</p>
          </div>
        </header>

        <div className="settings-cols">
          <aside className="settings-nav">
            {S_TABS.map(t => {
              const Ic = I[t.icon] || I.Dot;
              return (
                <button key={t.id} className={`settings-nav-btn ${tab === t.id ? "is-active" : ""}`} onClick={() => setTab(t.id)}>
                  <Ic size={15} /><span>{t.label}</span>
                </button>
              );
            })}
          </aside>

          <section className="settings-body">
            {tab === "project" && <ProjectSettings />}
            {tab === "writing" && <WritingSettings />}
            {tab === "ai" && <AISettings />}
            {tab === "appear" && <AppearSettings t={t} setTweak={setTweak} />}
            {tab === "data" && <DataSettings go={go} />}
          </section>
        </div>
      </div>
    </div>
  );
};

/* ===== 项目 — 读写当前作品（WsWorks），状态来自目录汇总 ===== */
function ProjectSettings() {
  const work = useActiveWork ? useActiveWork() : { id: "", title: "", genre: "", sub: "", wordsTarget: 0, wordsTargetDay: 0, streak: 0 };
  const totals = WsCatalog ? WsCatalog.totals() : { words: 0, written: 0, planned: 0, today: 0 };
  const save = (patch) => { if (WsWorks && work.id) WsWorks.update(work.id, patch); };
  const num = (e) => { const n = parseInt(e.target.value, 10); return Number.isFinite(n) && n > 0 ? n : null; };
  const pct = work.wordsTarget ? Math.min(100, Math.round((totals.words / work.wordsTarget) * 100)) : 0;

  return (
    <>
      <Section title="项目信息" desc={`当前作品《${work.title}》。改完即存，左上角书架与主页同步更新。`}>
        <Row label="项目名" hint="将出现在导航、主页与导出。">
          <input className="input" key={work.id + ":t"} defaultValue={work.title}
            onBlur={(e) => { const v = e.target.value.trim(); if (v && v !== work.title) save({ title: v }); }} />
        </Row>
        <Row label="题材">
          <input className="input" key={work.id + ":g"} defaultValue={work.genre}
            onBlur={(e) => { const v = e.target.value.trim(); if (v !== work.genre) save({ genre: v }); }} />
        </Row>
        <Row label="一句话简介">
          <input className="input" key={work.id + ":s"} defaultValue={work.sub}
            onBlur={(e) => { const v = e.target.value.trim(); if (v !== work.sub) save({ sub: v }); }} />
        </Row>
        <Row label="目标字数">
          <input className="input" type="number" step="10000" key={work.id + ":w"} defaultValue={work.wordsTarget}
            onBlur={(e) => { const n = num(e); if (n) save({ wordsTarget: n }); }} />
        </Row>
        <Row label="每日目标" hint="主页「今日」进度条的分母。">
          <input className="input" type="number" step="100" key={work.id + ":d"} defaultValue={work.wordsTargetDay}
            onBlur={(e) => { const n = num(e); if (n) save({ wordsTargetDay: n }); }} />
        </Row>
      </Section>
      <Section title="项目状态" desc="由章节目录实时汇总，与主页 / 书架同源。">
        <Row label="完成度"><div className="set-readonly">{pct}% · {totals.written} / {totals.planned} 章 · {totals.words.toLocaleString()} 字</div></Row>
        <Row label="今日已写"><div className="set-readonly">{(totals.today || 0).toLocaleString()} 字 · 连续 {work.streak || 0} 天</div></Row>
      </Section>
    </>
  );
}

/* ===== 写作偏好 — 全局持久化 ===== */
function WritingSettings() {
  const [autosave, setAutosave] = usePref("autosave", true);
  const [indent, setIndent] = usePref("indent", "em");
  const [spell, setSpell] = usePref("spell", "name");
  const [diction, setDiction] = usePref("diction", "zh-mainland");
  const [punct, setPunct] = usePref("punct", "typographic");
  return (
    <>
      <Section title="写作习惯" desc="影响写作房间的编辑体验。">
        <Row label="自动保存" hint="改动后 3 秒静止自动保存。"><Toggle on={autosave} onChange={setAutosave} /></Row>
        <Row label="行首缩进">
          <Segmented options={[
            { value: "none",   label: "不缩进" },
            { value: "two",    label: "两空格" },
            { value: "em",     label: "全角两字" },
          ]} value={indent} onChange={setIndent} />
        </Row>
        <Row label="拼写检查">
          <Segmented options={[
            { value: "off",  label: "关闭" },
            { value: "name", label: "只查人名地名" },
            { value: "all",  label: "全部" },
          ]} value={spell} onChange={setSpell} />
        </Row>
      </Section>

      <Section title="文本规范" desc="保存与导出时统一执行。">
        <Row label="行文规范">
          <Segmented options={[
            { value: "zh-mainland", label: "大陆" },
            { value: "zh-tw",       label: "繁体" },
            { value: "literary",    label: "文学体" },
          ]} value={diction} onChange={setDiction} />
        </Row>
        <Row label="标点">
          <Segmented options={[
            { value: "typographic", label: "全角" },
            { value: "ascii",       label: "半角" },
            { value: "mixed",       label: "混排" },
          ]} value={punct} onChange={setPunct} />
        </Row>
      </Section>
    </>
  );
}

/* ===== AI 模型 — 真实模型接入(ws-settings-ai.jsx,FE 模型接入重建) ===== */

/* ===== 外观 — 直接接 tweaks（与「调节舒适度」同一份状态） ===== */
function AppearSettings({ t, setTweak }) {
  const theme = t ? t.theme : "day";
  const fontSize = (t && t.fontSize) || 18;
  const lh = (t && t.lineHeight) || 2.05;
  const lhVal = lh <= 1.9 ? "snug" : lh >= 2.25 ? "airy" : "normal";
  const set = (k, v) => setTweak && setTweak(k, v);
  return (
    <>
      <Section title="主题" desc="全局生效，与左下角昼夜切换同源。">
        <Row label="模式">
          <Segmented options={[
            { value: "day",   label: "白昼" },
            { value: "dusk",  label: "暮色" },
            { value: "night", label: "夜灯" },
          ]} value={theme} onChange={(v) => set("theme", v)} />
        </Row>
        <Row label="稿纸纹理"><Toggle on={!!(t && t.texture)} onChange={(v) => set("texture", v)} /></Row>
      </Section>

      <Section title="正文排版" desc="影响写作房间的手感，与「调节舒适度」同一份设置。">
        <Row label="字号" hint={`当前 ${fontSize}px`}>
          <input type="range" min="14" max="22" value={fontSize} onChange={(e) => set("fontSize", parseInt(e.target.value, 10))} className="range" />
        </Row>
        <Row label="行距">
          <Segmented options={[
            { value: "snug",  label: "紧凑" },
            { value: "normal",label: "标准" },
            { value: "airy",  label: "宽松" },
          ]} value={lhVal} onChange={(v) => set("lineHeight", v === "snug" ? 1.8 : v === "airy" ? 2.3 : 2.05)} />
        </Row>
      </Section>
    </>
  );
}

/* ===== 数据 & 安全 — 真实动作 ===== */
function DataSettings({ go }) {
  const work = WsWorks ? WsWorks.active() : { id: "", title: "—" };
  const worksN = WsWorks ? WsWorks.list().length : 1;

  const clearLocalCache = () => {
    if (!window.confirm(
      `清除《${work.title}》的本机缓存？\n` +
      "不会删除服务端作品、章节或正文；刷新后会重新从服务端读取。" +
      "\n未同步的本地恢复记录会被删除且无法撤销——请先手工复制仍需保留的内容。"
    )) return;
    try {
      const suffix = "::" + work.id;
      const doomed = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && k.slice(-suffix.length) === suffix) doomed.push(k);
      }
      doomed.forEach(k => localStorage.removeItem(k));
    } catch (e) {}
    location.reload();
  };

  const deleteWork = () => {
    if (!window.confirm(`删除《${work.title}》？\n这部作品会连同全部数据进入回收站，可在「回收站」里整体恢复。`)) return;
    WsWorks.remove(work.id);
    if (go) go("home");
  };

  return (
    <>
      <Section title="导出与备份" desc="成稿导出、本机缓存快照和服务端数据库备份是三种不同能力。">
        <Row label="导出服务端成稿" hint="逐章核验服务端权威正文，可导出 Markdown、TXT 或 Word。">
          <button className="btn btn-ghost" onClick={() => go && go("manuscripts")}>去成稿中心 <I.ArrowRight size={13} /></button>
        </Row>
        <Row label="浏览器缓存快照" hint="仅供诊断或人工取证；不含服务端数据库，不能迁移或恢复项目。">
          <span className="text-muted text-xs">可在同步与恢复中心查看</span>
        </Row>
        <Row label="完整数据库备份" hint="由运维执行带完整性、外键和 SHA-256 校验的停机恢复演练。">
          <span className="text-muted text-xs">不在浏览器内执行</span>
        </Row>
      </Section>
      <Section title="危险区" desc="谨慎操作。清缓存不改服务端数据；删除可从回收站恢复。">
        <Row label="清除本机缓存" hint="删除当前作品的浏览器缓存与未同步恢复记录，随后从服务端重载。">
          <button className="btn btn-ghost" style={{ borderColor: "var(--rose)", color: "var(--rose)" }} onClick={clearLocalCache}>清除…</button>
        </Row>
        <Row label="删除本作品" hint={worksN <= 1 ? "至少保留一部作品。" : "整部进入回收站，可恢复。"}>
          <button className="btn btn-ghost" disabled={worksN <= 1}
            style={{ borderColor: "var(--rose)", color: "var(--rose)", opacity: worksN <= 1 ? 0.45 : 1 }}
            onClick={deleteWork}>删除…</button>
        </Row>
      </Section>
    </>
  );
}

/* Section/Row/Toggle/Segmented/usePref 供 ws-settings-ai.jsx 复用。 */
export { WsSettings, DataSettings, Section, Row, Toggle, Segmented, usePref };

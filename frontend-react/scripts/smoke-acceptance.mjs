// FE-ALIGN 全局验收冒烟：新建空白书全链路 + 跨会话持久 + 回收站往返。
// （验收①②：构思暂存本地→物化后全部业务数据进后端；清缓存重载后一切都在）
// 运行：cd frontend && node ../frontend-react/scripts/smoke-acceptance.mjs
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const BASE = process.argv[2] || "http://127.0.0.1:5174/";
const API = process.argv[3] || "http://127.0.0.1:8009";
let failed = 0;
const errors = [];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
page.setDefaultTimeout(20_000);
page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
page.on("dialog", (d) => d.accept());

async function check(label, fn) {
  try { await fn(); console.log("ok:", label); }
  catch (e) { failed++; console.log("FAIL:", label, "—", e.message.split("\n")[0]); }
}

const api = async (p) => (await page.evaluate(async (u) => (await fetch(u)).json(), API + p)).data;
const TITLE = "验收之书-" + Date.now().toString(36); // 每轮唯一，避免撞上一轮残留

await page.goto(BASE);
await page.evaluate((apiBase) => {
  localStorage.clear();
  localStorage.setItem("novel-system-api-base", apiBase);
  localStorage.setItem("novel-system-api-base-default", "http://127.0.0.1:8000");
}, API);
await page.reload();
await page.waitForSelector(".ws-app");
await page.waitForTimeout(2000);

let pid = null;

await check("① 新建空白书 → 落库", async () => {
  pid = await page.evaluate(async (t) => {
    window.WsWorks.create({ title: t, mark: "验", accent: "slate" });
    await new Promise(r => setTimeout(r, 1500));
    return window.WsWorks.activeId();
  }, TITLE);
  const items = (await api("/api/v2/projects")).items;
  if (!items.some(w => w.title === TITLE)) throw new Error("project not in backend");
  pid = items.find(w => w.title === TITLE).project_id;
});

await check("② 编排：建章建场 → 后端目录", async () => {
  await page.evaluate(async () => {
    const chs = window.WsCatalog.get();
    window.WsCatalog.set([...chs, { id: "tmp1", n: "01", title: "验收第一章", state: "writing", scenes: [
      { title: "开场", kind: "主动", state: "writing", goal: "目标", obstacle: "阻碍", turn: "挫折" },
    ] }]);
    await new Promise(r => setTimeout(r, 2500));
  });
  const tree = await api(`/api/v2/projects/${pid}/catalog`);
  if (tree.chapters.length !== 1) throw new Error(`chapters: ${tree.chapters.length}`);
  if (tree.chapters[0].title !== "验收第一章") throw new Error("title wrong");
  if (tree.chapters[0].scenes[0].title !== "开场") throw new Error("scene wrong");
});

let wordsAfterWrite = 0;
await check("③ 写作：正文写穿 author-drafts → 统计上涨", async () => {
  await page.evaluate(async () => {
    const sid = window.WsCatalog.get()[0].scenes[0].sid;
    window.WrDocs.save(sid, "<p>验收正文：潮水在夜里退去，露出一行脚印。这是真实保存到后端的一段话。</p>");
    await new Promise(r => setTimeout(r, 2000));
  });
  const stats = await api(`/api/v2/projects/${pid}/writing-stats`);
  // 统计按保存增量记账（新场景的占位正文计入基线），只验「涨了且连更=1」
  if (!(stats.words_total > 0)) throw new Error(`words_total: ${stats.words_total}`);
  if (stats.streak_days !== 1) throw new Error(`streak: ${stats.streak_days}`);
  wordsAfterWrite = stats.words_total;
});

await check("④ 待办：投递卡 → effect 改目录（后端事务闭环）", async () => {
  const tree = await api(`/api/v2/projects/${pid}/catalog`);
  const cid = tree.chapters[0].chapter_id;
  await page.evaluate(async (args) => {
    await fetch(`${args.api}/api/v1/review-items`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Idempotency-Key": "acc-card-" + Date.now() },
      body: JSON.stringify({
        project_id: args.pid, kind: "decision", priority: 1, title: "验收：定章题",
        dedupe_key: "acc:rename", options: ["验收·定稿章题"],
        actions: [{ label: "用这个", intent: "primary", op: "resolve", effect: { type: "rename_chapter", chapter_id: args.cid, title: "验收·定稿章题" } }],
      }),
    });
  }, { api: API, pid, cid });
  await page.evaluate(() => { location.hash = "#review"; });
  await page.waitForTimeout(1800);
  await page.click('.rv-item:has-text("验收：定章题")');
  await page.waitForTimeout(400);
  await page.click('button:has-text("用这个")');
  await page.waitForTimeout(2000);
  const after = await api(`/api/v2/projects/${pid}/catalog`);
  if (after.chapters[0].title !== "验收·定稿章题") throw new Error(`title: ${after.chapters[0].title}`);
});

await check("⑥ 跨会话：清缓存重载 → 目录/正文/统计/章题都在", async () => {
  await page.evaluate((args) => {
    localStorage.clear();
    localStorage.setItem("novel-system-api-base", args.api);
    localStorage.setItem("novel-system-api-base-default", "http://127.0.0.1:8000");
    localStorage.setItem("ws_active_work_v1", args.pid);
  }, { api: API, pid });
  await page.reload();
  await page.waitForSelector(".ws-app");
  await page.waitForTimeout(2500);
  const snap = await page.evaluate(async () => {
    const chs = window.WsCatalog.get();
    const sid = chs[0] && chs[0].scenes[0] ? chs[0].scenes[0].sid : null;
    let doc = sid ? window.WrDocs.load(sid) : "";
    if (!doc) { await new Promise(r => setTimeout(r, 1500)); doc = window.WrDocs.load(sid); }
    return { title: chs[0] && chs[0].title, doc: doc || "", count: chs.length };
  });
  if (snap.title !== "验收·定稿章题") throw new Error(`title: ${snap.title}`);
  if (!snap.doc.includes("潮水在夜里退去")) throw new Error("doc not hydrated");
});

await check("⑦ 回收站：删整部 → 恢复 → 数据无损", async () => {
  await page.evaluate(async (p) => {
    window.WsWorks.remove(p);
    await new Promise(r => setTimeout(r, 1500));
  }, pid);
  let items = (await api("/api/v2/projects")).items;
  if (items.some(w => w.project_id === pid)) throw new Error("still listed after remove");
  await page.evaluate(async (args) => {
    await fetch(`${args.api}/api/v2/projects/${args.pid}/restore`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-Idempotency-Key": "acc-restore-" + Date.now() }, body: "{}",
    });
  }, { api: API, pid });
  items = (await api("/api/v2/projects")).items;
  const row = items.find(w => w.project_id === pid);
  if (!row) throw new Error("not restored");
  if (row.stats.words_total !== wordsAfterWrite) throw new Error(`stats lost: ${row.stats.words_total} != ${wordsAfterWrite}`);
  const tree = await api(`/api/v2/projects/${pid}/catalog`);
  if (tree.chapters[0].title !== "验收·定稿章题") throw new Error("catalog lost");
});


// 清理：验收书不留库（软删 + 回收站彻底清除）
try {
  await fetch(`${API}/api/v2/projects/${pid}`, { method: "DELETE", headers: { "X-Idempotency-Key": "acc-clean-" + pid } });
  await fetch(`${API}/api/v2/trash/${encodeURIComponent("work:" + pid)}`, { method: "DELETE", headers: { "X-Idempotency-Key": "acc-purge-" + pid } });
} catch (e) {}

await browser.close();
const uniq = [...new Set(errors)];
if (uniq.length) { console.log(`\n${uniq.length} page errors:`); uniq.slice(0, 10).forEach(e => console.log(" -", e.slice(0, 300))); }
process.exitCode = failed || uniq.length ? 1 : 0;
console.log(failed ? `\n${failed} checks failed` : "\nall checks passed");

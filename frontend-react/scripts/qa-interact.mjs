// QA Round 1b — 非破坏性深度交互探针。
// 只做：Tab 切换 / 打开弹窗后取消 / 渲染观察。捕获 console/pageerror + 截图。
// 不触发 LLM 生成、不增删后端数据。
// 运行：cd frontend && node ../frontend-react/scripts/qa-interact.mjs [BASE] [API] [OUTDIR]
import path from "node:path";
import fs from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const BASE = process.argv[2] || "http://127.0.0.1:5174/";
const API = process.argv[3] || "http://127.0.0.1:8000";
const OUT = process.argv[4] || path.resolve("../.codex-run/qa-round1");
fs.mkdirSync(path.join(OUT, "shots-interact"), { recursive: true });

const findings = [];
let ctx = "";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
page.setDefaultTimeout(15000);
page.on("console", (m) => { if (m.type() === "error") findings.push({ ctx, kind: "console", detail: m.text().slice(0, 300) }); });
page.on("pageerror", (e) => findings.push({ ctx, kind: "pageerror", detail: e.message.slice(0, 300) }));

async function waitApp() {
  await page.waitForSelector(".ws-app", { state: "attached" });
  try { await page.evaluate(() => document.fonts.ready); } catch {}
  await page.waitForTimeout(400);
}
async function gotoView(work, view) {
  await page.evaluate((w) => localStorage.setItem("ws_active_work_v1", w), work);
  await page.evaluate((v) => { location.hash = "#" + v; }, view);
  await page.reload();
  await waitApp();
  await page.waitForTimeout(900);
}
function note(kind, detail) { findings.push({ ctx, kind, detail: String(detail).slice(0, 300) }); }
async function shot(name) { try { await page.screenshot({ path: path.join(OUT, "shots-interact", name + ".png") }); } catch {} }
async function clickText(t, opts = {}) {
  const loc = page.locator(`text=${t}`).first();
  if (await loc.count()) { await loc.click(opts).catch((e) => note("click-fail", `${t}: ${e.message.split("\n")[0]}`)); return true; }
  return false;
}

await page.addInitScript((api) => {
  localStorage.setItem("novel-system-api-base", api);
  localStorage.setItem("novel-system-api-base-default", "http://127.0.0.1:8000");
}, API);
await page.goto(BASE);
await waitApp();

// ---- A) Author 三镜头 Tab，单章 vs 多章 ----
for (const work of ["work-a", "PRJ_1C88DEFF3D"]) {
  ctx = `author/${work}`;
  await gotoView(work, "author");
  await shot(`author-${work}-1`);
  for (const tab of ["故事弧线", "线索织布机", "节奏镜头"]) {
    const before = findings.length;
    const ok = await clickText(tab);
    if (!ok) { note("tab-missing", tab); continue; }
    await page.waitForTimeout(900);
    await shot(`author-${work}-${tab}`);
    console.log(`[${ctx}] tab=${tab} (+${findings.length - before})`);
  }
  // 切到“章节详情”视图
  if (await clickText("章节详情")) { await page.waitForTimeout(800); await shot(`author-${work}-detail`); }
}

// ---- B) Snowflake 10 步导航（只观察，不点生成）----
for (const work of ["work-a"]) {
  ctx = `snowflake/${work}`;
  await gotoView(work, "snowflake");
  await shot(`snow-${work}-load`);
  // 逐步点击左侧步骤条（sf-step / 数字 01..10）
  for (let i = 1; i <= 10; i++) {
    const num = String(i).padStart(2, "0");
    const before = findings.length;
    const loc = page.locator(`.sf-rail, .ct-rail, nav`).locator(`text=${num}`).first();
    if (await loc.count()) { await loc.click().catch(() => {}); await page.waitForTimeout(500); }
    if (findings.length > before) console.log(`[${ctx}] step ${num} (+${findings.length - before})`);
  }
  await shot(`snow-${work}-stepped`);
  // “整理为章节结构”使用原生确认框；预先挂一次性 dismiss，确保探针绝不物化。
  page.once("dialog", (dialog) => dialog.dismiss().catch(() => {}));
  if (await clickText("整理为章节结构")) {
    await page.waitForTimeout(500);
    await shot(`snow-${work}-materialize-cancelled`);
  }
}

// ---- C) Style reference：打开导入弹窗 → 取消 ----
ctx = "styleref/tide";
await gotoView("work-a", "styleref");
await shot("styleref-load");
for (const t of ["导入参考书", "导入", "上传", "新建", "添加参考"]) {
  if (await clickText(t)) { await page.waitForTimeout(700); await shot(`styleref-modal-${t}`); await page.keyboard.press("Escape").catch(() => {}); break; }
}

// ---- D) Settings 子页轮巡 ----
ctx = "settings/tide";
await gotoView("work-a", "settings");
await page.waitForTimeout(600);
const navBtns = await page.locator(".settings-nav-btn").allTextContents().catch(() => []);
note("settings-tabs", `nav=[${navBtns.join(" | ")}]`);
for (const label of navBtns) {
  const before = findings.length;
  await page.locator(`.settings-nav-btn:has-text("${label.trim()}")`).first().click().catch(() => {});
  await page.waitForTimeout(800);
  await shot(`settings-${label.trim().slice(0, 8)}`);
  if (findings.length > before) console.log(`[settings] ${label.trim()} (+${findings.length - before})`);
}

// ---- E) Quality / Longform 渲染 + 主按钮存在性 ----
for (const view of ["quality"]) {
  ctx = `${view}/tide`;
  await gotoView("work-a", view);
  await page.waitForTimeout(900);
  await shot(`${view}-tide`);
  const txtLen = await page.evaluate(() => (document.querySelector(".ws-content")?.innerText || "").length);
  note("render-len", `${view} content len=${txtLen}`);
}

await browser.close();

// 去重 + 汇总
const seen = new Set();
const uniq = findings.filter(f => { const k = `${f.ctx}|${f.kind}|${f.detail}`; if (seen.has(k)) return false; seen.add(k); return true; });
const byKind = {};
for (const f of uniq) byKind[f.kind] = (byKind[f.kind] || 0) + 1;
fs.writeFileSync(path.join(OUT, "interact-findings.json"), JSON.stringify({ total: findings.length, unique: uniq.length, byKind, findings: uniq }, null, 2));
console.log("\n==== interact 汇总 ====");
console.log(JSON.stringify(byKind, null, 2));
for (const f of uniq.filter(f => ["console", "pageerror", "click-fail", "tab-missing"].includes(f.kind))) console.log(` - [${f.ctx}] ${f.kind}: ${f.detail}`);
console.log(`unique=${uniq.length} -> ${path.join(OUT, "interact-findings.json")}`);

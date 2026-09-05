// QA3 深度真实用户走查 — 全 16 视图，重交互（切 tab/开关弹窗/展开/点条目），非破坏。
// 捕获 console error / pageerror / 4xx-5xx / 非 GET 写 / 可见错误态 + 截图。
// 不触发 LLM 生成、不物化、不删除、不送审/批准/导出。
// 运行：cd frontend && node ../frontend-react/scripts/qa3-walk.mjs [BASE] [API]
import path from "node:path";
import fs from "node:fs";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const BASE = process.argv[2] || "http://127.0.0.1:5174/";
const API = process.argv[3] || "http://127.0.0.1:8000";
const OUT = path.resolve("../.codex-run/qa3");
fs.mkdirSync(path.join(OUT, "shots"), { recursive: true });

const findings = [];
const net = [];
let ctx = "";
function rec(kind, detail) { findings.push({ ctx, kind, detail: String(detail).slice(0, 400) }); }
const ERR_KW = ["出错", "加载失败", "无法加载", "未能", "失败了", "undefined", "NaN", "[object Object]", "Cannot read", "TypeError", "is not a function", "Something went wrong", "页面崩溃"];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1680, height: 1050 } });
page.setDefaultTimeout(20000);
page.on("console", (m) => { if (m.type() === "error") rec("console", m.text()); });
page.on("pageerror", (e) => rec("pageerror", e.message + (e.stack ? " | " + (e.stack.split("\n")[1] || "") : "")));
page.on("requestfailed", (r) => { const u = r.url(); if (u.includes("/api/")) rec("requestfailed", `${r.method()} ${u.replace(API, "")} :: ${r.failure()?.errorText}`); });
page.on("response", (r) => {
  const u = r.url(); if (!u.includes("/api/")) return;
  const m = r.request().method(), s = r.status();
  if (m !== "GET" || s >= 400) net.push({ ctx, m, s, url: u.replace(API, "") });
  if (s >= 400) rec(s >= 500 ? "http5xx" : "http4xx", `${s} ${m} ${u.replace(API, "")}`);
});

await page.addInitScript((api) => {
  localStorage.setItem("novel-system-api-base", api);
  localStorage.setItem("novel-system-api-base-default", "http://127.0.0.1:8000");
  localStorage.setItem("ws_tweaks_v1", JSON.stringify({ mode: "advanced" })); // 解锁高级组
}, API);

async function waitApp() { await page.waitForSelector(".ws-app", { state: "attached" }); try { await page.evaluate(() => document.fonts.ready); } catch {} await page.waitForTimeout(450); }
async function go(work, view) {
  await page.evaluate((w) => localStorage.setItem("ws_active_work_v1", w), work);
  await page.evaluate((v) => { location.hash = "#" + v; }, view);
  await page.reload(); await waitApp(); await page.waitForTimeout(900);
}
async function shot(n) { try { await page.screenshot({ path: path.join(OUT, "shots", n + ".png"), fullPage: false }); } catch {} }
async function probe(label) {
  const r = await page.evaluate((kw) => {
    const c = document.querySelector(".ws-content") || document.body;
    const text = (c.innerText || "").trim();
    return { len: text.length, hits: kw.filter(k => text.includes(k)), sample: text.slice(0, 120) };
  }, ERR_KW);
  if (r.len < 40) rec("blank-or-thin", `${label} len=${r.len} "${r.sample}"`);
  if (r.hits.length) rec("visible-error-text", `${label} kw=[${r.hits.join(",")}] "${r.sample}"`);
  return r;
}
async function clickText(t, opts = {}) {
  const loc = page.locator(`text=${t}`).first();
  if (await loc.count()) { await loc.click({ timeout: 6000, ...opts }).catch((e) => rec("click-fail", `${t}: ${e.message.split("\n")[0]}`)); return true; }
  return false;
}
async function escAll() { for (let i = 0; i < 2; i++) { await page.keyboard.press("Escape").catch(() => {}); await page.waitForTimeout(200); } }

const WORK = process.env.QA_WORK || "work-a";
await page.goto(BASE); await waitApp();

// ---------- 1) home ----------
ctx = "home"; await go(WORK, "home"); await probe("home"); await shot("01-home");
// 列出 home 上的主行动按钮（仅记录文案，不点危险项）
const homeBtns = await page.locator(".ws-content button").allTextContents().catch(() => []);
rec("buttons", `home=[${homeBtns.map(s => s.trim()).filter(Boolean).slice(0, 12).join(" | ")}]`);

// ---------- 2) flowmap ----------
ctx = "flowmap"; await go(WORK, "flowmap"); await probe("flowmap"); await shot("02-flowmap");

// ---------- 3) snowflake ----------
ctx = "snowflake"; await go(WORK, "snowflake"); await probe("snowflake");
await clickText("总览"); await page.waitForTimeout(400); await shot("03-snow-overview");
await clickText("逐步"); await page.waitForTimeout(500); await shot("03-snow-stepwise");
// 逐步点 step rail（只读观察）
for (let i = 1; i <= 10; i++) {
  const num = String(i).padStart(2, "0");
  const loc = page.locator(`.sf-rail, .ct-rail, nav, aside`).locator(`text=${num}`).first();
  if (await loc.count()) { await loc.click().catch(() => {}); await page.waitForTimeout(250); }
}
await shot("03-snow-stepped");
if (await clickText("整理成章节结构") || await clickText("整理为章节结构")) {
  await page.waitForTimeout(700); await shot("03-snow-materialize-modal"); await escAll();
}

// ---------- 4) writer ----------
ctx = "writer"; await go(WORK, "writer"); await probe("writer"); await shot("04-writer");

// ---------- 5) styleref ----------
ctx = "styleref"; await go(WORK, "styleref"); await probe("styleref"); await shot("05-styleref");
for (const t of ["导入参考书", "导入", "上传", "添加参考书", "新建"]) {
  if (await clickText(t)) { await page.waitForTimeout(700); await shot(`05-styleref-modal`); await escAll(); break; }
}

// ---------- 6) review ----------
ctx = "review"; await go(WORK, "review"); await probe("review"); await shot("06-review");
// 点筛选 chip / 第一条待办（不 resolve）
const reviewCards = page.locator(".ws-content [class*='card'], .ws-content li, .ws-content [role='listitem']");
if (await reviewCards.count()) { await reviewCards.first().click().catch(() => {}); await page.waitForTimeout(500); await shot("06-review-detail"); }

// ---------- 7) library ----------
ctx = "library"; await go(WORK, "library"); await probe("library");
for (const t of ["总览", "图谱", "关系", "时间线", "人物", "实体"]) { if (await clickText(t)) await page.waitForTimeout(450); }
await shot("07-library");

// ---------- 8) author (advanced) ----------
ctx = "author"; await go(WORK, "author"); await probe("author");
for (const t of ["故事弧线", "线索织布机", "节奏镜头", "章节详情"]) { if (await clickText(t)) { await page.waitForTimeout(700); await shot(`08-author-${t}`); } }

// ---------- 9) scene workbench (advanced) ----------
ctx = "scene"; await go(WORK, "scene"); await probe("scene"); await shot("09-scene");
const sceneBtns = await page.locator(".ws-content button").allTextContents().catch(() => []);
rec("buttons", `scene=[${sceneBtns.map(s => s.trim()).filter(Boolean).slice(0, 14).join(" | ")}]`);

// ---------- 10) manuscripts (advanced) ----------
ctx = "manuscripts"; await go(WORK, "manuscripts"); await probe("manuscripts"); await shot("10-manuscripts");
// 点章节列表项（只读阅读，不送审/批准）
const chap = page.locator(".ws-content [class*='chapter'], .ws-content li").first();
if (await chap.count()) { await chap.click().catch(() => {}); await page.waitForTimeout(600); await shot("10-manuscripts-read"); }

// ---------- 12) quality ----------
ctx = "quality"; await go(WORK, "quality"); await probe("quality");
const qTabs = await page.locator(".ws-content [role='tab'], .ws-content button").allTextContents().catch(() => []);
for (const t of ["总览", "扫描", "章组", "复审", "文本"]) { if (await clickText(t)) await page.waitForTimeout(450); }
await shot("12-quality");

// ---------- 15) settings ----------
ctx = "settings"; await go(WORK, "settings"); await probe("settings");
const navBtns = await page.locator(".settings-nav-btn").allTextContents().catch(() => []);
rec("settings-tabs", `[${navBtns.map(s => s.trim()).join(" | ")}]`);
for (const label of navBtns) {
  await page.locator(`.settings-nav-btn:has-text("${label.trim()}")`).first().click().catch(() => {});
  await page.waitForTimeout(600); await shot(`15-settings-${label.trim().slice(0, 6)}`);
}

// ---------- 16) trash ----------
ctx = "trash"; await go(WORK, "trash"); await probe("trash"); await shot("16-trash");

await browser.close();

// 汇总
const seen = new Set();
const uniq = findings.filter(f => { const k = `${f.ctx}|${f.kind}|${f.detail}`; if (seen.has(k)) return false; seen.add(k); return true; });
const byKind = {};
for (const f of uniq) byKind[f.kind] = (byKind[f.kind] || 0) + 1;
fs.writeFileSync(path.join(OUT, "walk-findings.json"), JSON.stringify({ base: BASE, api: API, work: WORK, total: findings.length, unique: uniq.length, byKind, findings: uniq, net }, null, 2));
console.log("==== QA3 walk 汇总 (work=" + WORK + ") ====");
console.log(JSON.stringify(byKind, null, 2));
for (const f of uniq.filter(f => !["buttons", "settings-tabs"].includes(f.kind))) console.log(` - [${f.ctx}] ${f.kind}: ${f.detail}`);
console.log("--- 非 GET / 4xx-5xx 网络 ---");
for (const n of net) console.log(`   ${n.s} ${n.m} ${n.url}  (@${n.ctx})`);
console.log(`unique=${uniq.length} -> ${path.join(OUT, "walk-findings.json")}`);

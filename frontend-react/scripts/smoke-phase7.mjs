// Phase 7 验收冒烟：长篇审计与当前产品界面的真实链路
// （审计发现 → 待办收件箱 / 服务端 dedupe / 契约归档写回）。
// 运行：cd frontend && node ../frontend-react/scripts/smoke-phase7.mjs
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

await page.goto(BASE);
await page.evaluate((apiBase) => {
  localStorage.clear();
  localStorage.setItem("novel-system-api-base", apiBase);
  localStorage.setItem("novel-system-api-base-default", "http://127.0.0.1:8000");
  localStorage.setItem("ws_active_work_v1", "work-a");
}, API);
await page.reload();
await page.waitForSelector(".ws-app");
await page.waitForTimeout(2500);

await check("onceTask：同一事项重复触发只有一张卡", async () => {
  const create = () => page.evaluate(async (apiBase) => {
    const response = await fetch(`${apiBase}/api/v1/review-items`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Idempotency-Key": `p7-task-${crypto.randomUUID()}`,
      },
      body: JSON.stringify({
        project_id: "work-a",
        kind: "qc",
        priority: 2,
        title: "补铺垫：第二组脚印",
        source: "章节编排",
        where: "第 9 章",
        dedupe_key: "task:backfill:ch09:脚印",
      }),
    });
    return { status: response.status, body: await response.json() };
  }, API);
  const first = await create();
  const second = await create();
  if (first.status !== 200 || second.status !== 200) throw new Error(`create status: ${first.status}/${second.status}`);
  if (first.body.data.deduped !== false || second.body.data.deduped !== true) {
    throw new Error(`dedupe flags: ${first.body.data.deduped}/${second.body.data.deduped}`);
  }
  const items = (await api("/api/v1/review-items?state=open&project_id=work-a")).items;
  if (items.filter(i => i.dedupe_key === "task:backfill:ch09:脚印").length !== 1) throw new Error("duplicate task cards");
});

await browser.close();
const uniq = [...new Set(errors)];
if (uniq.length) { console.log(`\n${uniq.length} page errors:`); uniq.slice(0, 10).forEach(e => console.log(" -", e.slice(0, 300))); }
process.exitCode = failed || uniq.length ? 1 : 0;
console.log(failed ? `\n${failed} checks failed` : "\nall checks passed");

export function domainChunk(moduleId) {
  const id = String(moduleId || "").replaceAll("\\", "/");
  if (id.includes("/node_modules/")) return "vendor";

  const marker = "/frontend-react/src/";
  const source = id.includes(marker) ? id.slice(id.indexOf(marker) + marker.length) : "";
  if (!source) return undefined;
  if (/^(ws-snow|ws-chapter-plan)/.test(source)) return "domain-snowflake";
  if (/^ws-styleref/.test(source)) return "domain-style-reference";
  if (/^(ws-writer|ws-deep|ws-signals|wr-canonical-control|wr-content-safety-review)/.test(source)) return "domain-writer";
  if (/^ws-scene/.test(source)) return "domain-scene";
  if (/^(ws-author|ws-chapter-run)/.test(source)) return "domain-author";
  if (/^ws-library/.test(source)) return "domain-library";
  if (/^ws-manuscripts/.test(source)) return "domain-manuscripts";
  if (/^(ws-quality|ws-cost)/.test(source)) return "domain-quality";
  if (/^(ws-settings|ws-ai-providers)/.test(source)) return "domain-settings";
  return undefined;
}

// 生产构建只固定第三方依赖。业务页面的边界由 React.lazy 动态入口决定；
// 强行把相互调用的业务模块塞进手工 chunk 会制造循环依赖并把它们重新拉回首屏。
export function productionChunk(moduleId) {
  const id = String(moduleId || "").replaceAll("\\", "/");
  return id.includes("/node_modules/") ? "vendor" : undefined;
}

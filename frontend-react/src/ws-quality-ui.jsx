import React from "react";

/* ==========================================================
   ws-quality-ui — 质量域共享小部件
   成本看板（ws-cost）等视图共用的统计卡。
   纯展示组件，无模块级副作用。
   ========================================================== */

function StatCard({ label, value, hint, children }) {
  return (
    <div className="card" style={{ padding: "10px 12px", borderRadius: 12, border: "1px solid var(--line, #e5e2dc)" }}>
      <div className="text-xs" style={{ color: "var(--ink-3)" }}>{label}</div>
      <div className="text-serif" style={{ fontSize: 20, lineHeight: 1.3, display: "flex", alignItems: "baseline", gap: 6, flexWrap: "wrap" }}>
        {value}{children}
      </div>
      {hint && <div className="text-xs" style={{ color: "var(--ink-3)", marginTop: 2 }}>{hint}</div>}
    </div>
  );
}

export { StatCard };

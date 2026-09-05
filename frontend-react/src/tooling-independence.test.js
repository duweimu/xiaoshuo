import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = path.resolve(frontendRoot, "..");
const scriptsDir = path.join(frontendRoot, "scripts");
const srcDir = path.join(frontendRoot, "src");

function sourceModules() {
  const modules = [];
  const walk = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name);
      if (entry.isDirectory()) walk(absolute);
      else if (/\.(js|jsx)$/.test(entry.name) && !entry.name.includes(".test.")) modules.push(absolute);
    }
  };
  walk(srcDir);
  return modules;
}

function resolveRelativeModule(importer, specifier, knownModules) {
  const base = path.resolve(path.dirname(importer), specifier);
  return [base, `${base}.js`, `${base}.jsx`, path.join(base, "index.js"), path.join(base, "index.jsx")]
    .find((candidate) => knownModules.has(path.normalize(candidate))) || null;
}

describe("React 工具链独立性", () => {
  it("由本包直接、精确锁定 Playwright", () => {
    const pkg = JSON.parse(fs.readFileSync(path.join(frontendRoot, "package.json"), "utf8"));
    expect(pkg.devDependencies.playwright).toMatch(/^\d+\.\d+\.\d+$/);
    expect(fs.existsSync(path.join(frontendRoot, "node_modules", "playwright", "package.json"))).toBe(true);
  });

  it("浏览器脚本按自身位置解析依赖，不依赖调用者工作目录", () => {
    const browserScripts = fs.readdirSync(scriptsDir)
      .filter((name) => name.endsWith(".mjs"))
      .map((name) => [name, fs.readFileSync(path.join(scriptsDir, name), "utf8")])
      .filter(([, source]) => source.includes('require("playwright")'));

    expect(browserScripts.length).toBeGreaterThan(0);
    for (const [name, source] of browserScripts) {
      expect(source, name).toContain("createRequire(import.meta.url)");
      expect(source, name).not.toContain('createRequire(path.join(process.cwd(), "package.json"))');
    }
  });

  it("QA2 uses the live project-list contract to discover its single-chapter fixture", () => {
    const source = fs.readFileSync(path.join(scriptsDir, "qa2-ui.mjs"), "utf8");
    expect(source).toContain("${API}/api/v1/projects");
    expect(source).not.toContain("${API}/api/v2/projects`");
    expect(source).toContain("catalog.chapters.length === 1");
  });

  it("静态 ESM 依赖图没有循环", () => {
    const modules = sourceModules();
    const knownModules = new Set(modules.map((file) => path.normalize(file)));
    const graph = new Map();
    for (const file of modules) {
      const source = fs.readFileSync(file, "utf8");
      const dependencies = [...source.matchAll(/(?:import|export)\s+(?:[^'\"]*?\s+from\s+)?['\"](\.[^'\"]+)['\"]/g)]
        .map((match) => resolveRelativeModule(file, match[1], knownModules))
        .filter(Boolean);
      graph.set(path.normalize(file), dependencies);
    }

    const state = new Map();
    const stack = [];
    const cycles = [];
    const visit = (file) => {
      state.set(file, "visiting");
      stack.push(file);
      for (const dependency of graph.get(file) || []) {
        if (!state.has(dependency)) visit(dependency);
        else if (state.get(dependency) === "visiting") {
          const start = stack.indexOf(dependency);
          cycles.push([...stack.slice(start), dependency].map((item) => path.relative(srcDir, item)));
        }
      }
      stack.pop();
      state.set(file, "visited");
    };
    for (const file of graph.keys()) if (!state.has(file)) visit(file);

    expect(cycles).toEqual([]);
  });

  it("新的 ESM-only 模块不再写入 window 全局命名空间", () => {
    const esmOnly = [
      "icons.jsx", "tweaks-panel.jsx", "ws-ai-providers.jsx", "ws-chapter-plan.jsx",
      "ws-cost.jsx", "ws-deep.jsx", "ws-home.jsx",
      "ws-palette.jsx", "ws-quality.jsx", "ws-settings.jsx", "ws-settings-ai.jsx",
      "ws-settings-shared.jsx", "ws-styleref-val.jsx", "ws-scene-run.jsx",
      "ws-author-data.jsx", "ws-author-doctor.jsx", "ws-author-loom.jsx",
      "ws-author-pacing.jsx", "ws-author-plan.jsx", "ws-library-derive.jsx",
      "ws-library-graph.jsx", "ws-library-overview.jsx", "ws-library-timeline.jsx",
      "ws-manuscripts-store.jsx", "ws-manuscripts.jsx", "ws-author.jsx",
      "ws-scene.jsx", "ws-library.jsx", "ws-writer.jsx",
    ];
    for (const name of esmOnly) {
      const source = fs.readFileSync(path.join(srcDir, name), "utf8");
      expect(source, name).not.toMatch(/Object\.assign\(window|window\.[A-Za-z_$][A-Za-z0-9_$]*\s*=/);
    }

    const remainingTransitionalModules = sourceModules().filter((file) => {
      const source = fs.readFileSync(file, "utf8");
      return /Object\.assign\(window|window\.[A-Za-z_$][A-Za-z0-9_$]*\s*=/.test(source);
    });
    expect(remainingTransitionalModules.length).toBeLessThanOrEqual(10);
  });
});

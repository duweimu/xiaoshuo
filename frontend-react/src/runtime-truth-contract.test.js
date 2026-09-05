import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const srcRoot = path.dirname(fileURLToPath(import.meta.url));

describe("正式运行时文案与行为一致", () => {
  it("设置页不会把仅清理浏览器缓存冒充为重置服务端作品", () => {
    const source = fs.readFileSync(path.join(srcRoot, "ws-settings.jsx"), "utf8");
    expect(source).toContain("不会删除服务端作品、章节或正文");
    expect(source).toContain('label="清除本机缓存"');
    expect(source).not.toContain("重置本作品");
    expect(source).not.toContain("回到示例种子状态");
  });

  it("主页待办没有退役演示种子的回退入口", () => {
    const source = fs.readFileSync(path.join(srcRoot, "ws-home.jsx"), "utf8");
    expect(source).not.toContain("window.RV_SEED");
  });
});

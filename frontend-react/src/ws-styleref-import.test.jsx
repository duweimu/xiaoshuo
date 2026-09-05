import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./ws-catalog.jsx", () => ({ WsDemoTag: () => null }));
vi.mock("./ws-works.jsx", () => ({ WsWorks: { activeId: () => "new-book" } }));
vi.mock("./ws-review.jsx", () => ({ rvPush: vi.fn() }));

import {
  SrImportDialog, SR_CLOUD_POLICIES, SR_RIGHTS_TERMS, srImportBook, srRightsReady,
} from "./ws-styleref.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const mounted = [];

async function renderDialog(props) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(<SrImportDialog {...props} />));
  await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
  return host;
}

const chooseBtn = (host) => host.querySelector('[data-testid="sr-import-choose-file"]');
/* 导入成功后 srSyncBooks 会经真实 client 再打 fetch 拉书库，只挑 import-upload 那一次。 */
const uploadCalls = (fetchMock) => fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/books/import-upload"));
const tick = (host, testId) => act(async () => host.querySelector(`[data-testid="${testId}"]`).click());

/* 桩掉文件选择器 + 书名 prompt + alert + fetch，返回 fetch mock；
   createElement 只劫持 "input"（srImportBook 用它造隐藏 file input），其余照常。 */
function stubImportPipeline({ response } = {}) {
  const originalCreate = document.createElement.bind(document);
  const fileInput = {
    type: "", accept: "", files: [new File(["片段"], "参考.md", { type: "text/markdown" })],
    onchange: null,
    click() { return this.onchange(); },
  };
  const createSpy = vi.spyOn(document, "createElement").mockImplementation((tag, options) => (
    tag === "input" ? fileInput : originalCreate(tag, options)
  ));
  vi.spyOn(window, "prompt").mockReturnValue("参考书");
  vi.spyOn(window, "alert").mockImplementation(() => {});
  const fetchMock = vi.fn().mockResolvedValue(response || {
    ok: true,
    status: 200,
    headers: new Headers(),
    json: async () => ({ ok: true, data: { book: { total_chars: 2 } }, books: [] }),
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, createSpy };
}

afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("参考书导入的数据出域选择", () => {
  it("展示三档策略并默认仅本机，作者可显式选择按段落送云", async () => {
    const onChoose = vi.fn();
    const host = await renderDialog({ open: true, onClose: vi.fn(), onChoose });

    expect(SR_CLOUD_POLICIES.map((item) => item.id)).toEqual([
      "local_only", "segments_only", "allow_full_cloud",
    ]);
    expect(host.textContent).toContain("仅保存在本机");
    expect(host.textContent).toContain("只发送所需段落");
    expect(host.textContent).toContain("允许全文上云");
    expect(host.querySelector('input[value="local_only"]').checked).toBe(true);

    await act(async () => host.querySelector('input[value="segments_only"]').click());
    await tick(host, "sr-rights-analysis");
    await tick(host, "sr-rights-send");
    await act(async () => chooseBtn(host).click());

    expect(onChoose).toHaveBeenCalledWith("segments_only", {
      declared: true, analysis_rights: true, send_rights: true,
    });
  });

  it("云端策略未勾发送权时「选择文件」保持禁用，勾满才放行", async () => {
    const onChoose = vi.fn();
    const host = await renderDialog({ open: true, onClose: vi.fn(), onChoose });

    // 默认 local_only：只要求分析权，不出现发送权勾选框
    expect(host.querySelector('[data-testid="sr-rights-send"]')).toBeNull();
    expect(host.textContent).toContain(SR_RIGHTS_TERMS.analysis);
    expect(chooseBtn(host).disabled).toBe(true);

    await act(async () => host.querySelector('input[value="allow_full_cloud"]').click());
    expect(host.querySelector('[data-testid="sr-rights-send"]')).toBeTruthy();
    expect(host.textContent).toContain(SR_RIGHTS_TERMS.send);
    expect(chooseBtn(host).disabled).toBe(true);

    // 只勾分析权：云端策略仍不放行，并给出说明
    await tick(host, "sr-rights-analysis");
    expect(chooseBtn(host).disabled).toBe(true);
    expect(host.querySelector('[data-testid="sr-rights-hint"]').textContent).toContain("发送权");
    await act(async () => chooseBtn(host).click());
    expect(onChoose).not.toHaveBeenCalled();

    await tick(host, "sr-rights-send");
    expect(chooseBtn(host).disabled).toBe(false);
    expect(host.querySelector('[data-testid="sr-rights-hint"]')).toBeNull();
    await act(async () => chooseBtn(host).click());
    expect(onChoose).toHaveBeenCalledWith("allow_full_cloud", {
      declared: true, analysis_rights: true, send_rights: true,
    });
  });

  it("切回仅本机后声明里的发送权恒为 false，不带走多余授权", async () => {
    const onChoose = vi.fn();
    const host = await renderDialog({ open: true, onClose: vi.fn(), onChoose });

    await act(async () => host.querySelector('input[value="segments_only"]').click());
    await tick(host, "sr-rights-analysis");
    await tick(host, "sr-rights-send");
    await act(async () => host.querySelector('input[value="local_only"]').click());

    expect(host.querySelector('[data-testid="sr-rights-send"]')).toBeNull();
    expect(chooseBtn(host).disabled).toBe(false);
    await act(async () => chooseBtn(host).click());
    expect(onChoose).toHaveBeenCalledWith("local_only", {
      declared: true, analysis_rights: true, send_rights: false,
    });
  });

  it("srRightsReady 与后端 _normalize_rights_declaration 的红线一致", () => {
    expect(srRightsReady("local_only", null)).toBe(false);
    expect(srRightsReady("local_only", { analysis_rights: true, send_rights: false })).toBe(true);
    expect(srRightsReady("segments_only", { analysis_rights: true, send_rights: false })).toBe(false);
    expect(srRightsReady("segments_only", { analysis_rights: true, send_rights: true })).toBe(true);
    expect(srRightsReady("allow_full_cloud", { analysis_rights: false, send_rights: true })).toBe(false);
  });

  it("Escape 关闭并把焦点还给打开它的按钮", async () => {
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();
    const onClose = vi.fn();
    const host = await renderDialog({ open: true, onClose, onChoose: vi.fn() });
    expect(host.querySelector('[role="dialog"]')).toBeTruthy();

    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(onClose).toHaveBeenCalledTimes(1);

    const { root } = mounted[mounted.length - 1];
    await act(async () => root.render(<SrImportDialog open={false} onClose={onClose} onChoose={vi.fn()} />));
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });
});

describe("srImportBook 上传表单的权属声明", () => {
  it("把作者所选策略原样写入上传表单，不再硬编码 segments_only", async () => {
    const { fetchMock } = stubImportPipeline();

    srImportBook("allow_full_cloud", { declared: true, analysis_rights: true, send_rights: true });
    await vi.waitFor(() => expect(uploadCalls(fetchMock)).toHaveLength(1));
    await vi.waitFor(() => expect(window.alert).toHaveBeenCalledWith(expect.stringContaining("已导入")));

    const request = uploadCalls(fetchMock)[0][1];
    expect(request.body).toBeInstanceOf(FormData);
    expect(request.body.get("cloud_policy")).toBe("allow_full_cloud");
  });

  it("segments_only 的表单带 rights_declaration JSON，send_rights=true 且署名 operator", async () => {
    const { fetchMock } = stubImportPipeline();

    srImportBook("segments_only", { declared: true, analysis_rights: true, send_rights: true });
    await vi.waitFor(() => expect(uploadCalls(fetchMock)).toHaveLength(1));
    await vi.waitFor(() => expect(window.alert).toHaveBeenCalledWith(expect.stringContaining("已导入")));

    const request = uploadCalls(fetchMock)[0][1];
    expect(request.method).toBe("POST");
    expect(request.headers["X-Idempotency-Key"]).toMatch(/^sr-import-/);
    expect(request.headers["X-Operator-Ref"]).toBe("operator");
    const raw = request.body.get("rights_declaration");
    expect(typeof raw).toBe("string");
    expect(JSON.parse(raw)).toEqual({
      declared: true,
      analysis_rights: true,
      send_rights: true,
      declared_by: "operator",
    });
  });

  it("仅本机且已确认分析权：声明写入 send_rights=false；未声明则不附字段", async () => {
    const { fetchMock } = stubImportPipeline();

    srImportBook("local_only", { declared: true, analysis_rights: true, send_rights: false });
    await vi.waitFor(() => expect(uploadCalls(fetchMock)).toHaveLength(1));
    await vi.waitFor(() => expect(window.alert).toHaveBeenCalledTimes(1));
    expect(JSON.parse(uploadCalls(fetchMock)[0][1].body.get("rights_declaration"))).toEqual({
      declared: true, analysis_rights: true, send_rights: false, declared_by: "operator",
    });

    srImportBook("local_only");
    await vi.waitFor(() => expect(uploadCalls(fetchMock)).toHaveLength(2));
    await vi.waitFor(() => expect(window.alert).toHaveBeenCalledTimes(2));
    const undeclared = uploadCalls(fetchMock)[1][1].body;
    expect(undeclared.get("cloud_policy")).toBe("local_only");
    expect(undeclared.has("rights_declaration")).toBe(false);
  });

  it("云端策略没有发送权声明：同步抛错，不开文件选择器、不发请求", () => {
    const { fetchMock, createSpy } = stubImportPipeline();

    expect(() => srImportBook("segments_only")).toThrow(/发送权/);
    expect(() => srImportBook("allow_full_cloud", { declared: true, analysis_rights: true, send_rights: false }))
      .toThrow(/发送权/);
    expect(createSpy).not.toHaveBeenCalledWith("input");
    expect(window.prompt).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("后端仍拒绝时原样透出信封里的 message 与 code", async () => {
    const { fetchMock } = stubImportPipeline({
      response: {
        ok: false,
        status: 400,
        headers: new Headers(),
        json: async () => ({
          ok: false,
          data: null,
          error: {
            code: "STYLE_REFERENCE_SEND_RIGHTS_DECLARATION_REQUIRED",
            message: "云端策略需要用户显式声明发送权；请确认声明或改用 local_only。",
            details: {},
          },
          request_id: "req_test",
        }),
      },
    });

    srImportBook("segments_only", { declared: true, analysis_rights: true, send_rights: true });
    await vi.waitFor(() => expect(uploadCalls(fetchMock)).toHaveLength(1));
    await vi.waitFor(() => expect(window.alert).toHaveBeenCalledTimes(1));
    const shown = window.alert.mock.calls[0][0];
    expect(shown).toContain("导入失败");
    expect(shown).toContain("云端策略需要用户显式声明发送权；请确认声明或改用 local_only。");
    expect(shown).toContain("STYLE_REFERENCE_SEND_RIGHTS_DECLARATION_REQUIRED");
  });
});

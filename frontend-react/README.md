# frontend-react — 正式创作工作台

Vite + React 18 前端，默认端口 `5174`。仓库根目录的 `start-dev.cmd` 默认只启动后端与本工程；旧 `frontend/` 仅用于兼容回归。

## 当前产品入口

默认路由是 `#home`。作家模式显示主页、流程、构思、写作、风格、待办、资料、设置和回收站；高级模式另外显示章节编排、AI 起草台、成稿中心、文学质量和成本看板。

界面能力边界：

- 新建作品、雪花十步、结构物化、逐场 AI 起草、写作草稿同步和权威正文提升连接真实 API。
- `潮汐档案` 的结构控制塔及部分正文装饰是演示数据；非演示作品显示自身数据或明确空态。
- 高级章节编排中的 `运行本章` 连接持久化章节任务，防重复启动并轮询进度；模型未配置、阻断和失败都会原样呈现，不会冒充成功或自动运行离线演示。
- 成稿中心按“通读当前正文并绑定哈希 → 项目级终稿批准”两步执行；目录 API 不能直接伪造批准。重新打开终稿需要原因，并由服务端撤销该章及其后的批准链。
- 写作房间的内容安全复核会按当前响应中的精确发现码重新确认；旧确认不会自动覆盖新发现。

## 命令

```powershell
cd frontend-react
npm install
npm run dev        # http://127.0.0.1:5174
npm run test       # Vitest
npm run build      # dist/
```

需要旧 Vue 界面时，在仓库根目录显式运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\dev.ps1 -Action start
```

## API 配置

- `VITE_NOVEL_SYSTEM_API_BASE`：后端地址，默认 `http://127.0.0.1:8000`。
- `VITE_NOVEL_SYSTEM_ACCESS_TOKEN`：远程模式的共享访问令牌默认值。
- `setRemoteAccessToken()`：集成层可写入当前标签页会话的令牌；值保存在 `sessionStorage`，不会写入 `localStorage`。

所有公共 API 请求会在存在令牌时发送 `X-Novel-Access-Token`。Vite 环境变量会进入浏览器构建产物，因此该令牌只能作为受控环境的共享门槛，不能替代用户认证或被当成浏览器端秘密。

## 同步与恢复

写作草稿会携带服务端当前权威版本标识，避免在版本已变化时静默覆盖。离线、`409` 冲突、配额失败或 AI 覆盖风险会留下浏览器本地恢复记录；全局 `同步与恢复中心` 支持差异查看、导出、重试、恢复和删除。

这些记录局限于当前浏览器配置文件和站点存储，不是跨设备备份。清理站点数据、无痕会话结束或存储失败后可能丢失。

## 结构约定

- `src/` 是持续维护的正式源代码；早期设计原型和一次性迁移脚本已经退役，不要从历史提交重新生成或覆盖现有实现。
- `src/main.jsx` 的样式导入顺序具有层叠语义，调整时必须做视觉回归。
- `window.*` 与部分同步 store 是现有运行时兼容接缝，新增代码优先使用模块导出；若要移除接缝，必须先补齐对应的 store 与跨视图回归。
- API 错误统一为 `ApiRequestError`；界面应优先使用稳定 `code` 和 `details`，不要解析错误文案。
- 新项目不得回退到 `潮汐档案` 的人物、剧情或候选种子。

## 关键文件与回归

- 壳层与导航：`src/ws-app.jsx`
- 作品与远端状态：`src/ws-works.jsx`
- 雪花主线：`src/ws-snow.jsx`、`src/ws-snow-sync.jsx`
- 写作与恢复：`src/ws-writer.jsx`、`src/wr-doc-store.jsx`、`src/wr-recovery-center.jsx`
- AI 起草台：`src/ws-scene.jsx`、`src/ws-scene-run.jsx`
- 章节运行与成稿：`src/ws-chapter-run.jsx`、`src/ws-manuscripts.jsx`、`src/ws-manuscripts-store.jsx`
- API 客户端：`src/lib/client.js`

核心回归至少包括：

```powershell
npx vitest run src/ws-works.test.jsx src/ws-snow.test.jsx src/ws-snow-sync.test.jsx
npx vitest run src/wr-doc-store.test.jsx src/wr-recovery-center.test.jsx src/ws-writer-content-safety.test.jsx
npx vitest run src/ws-scene-run.test.jsx src/lib/client.test.js
npx vitest run src/ws-chapter-run.test.jsx src/ws-manuscripts.test.jsx src/ws-manuscripts-flow.test.jsx
npm run build
```

仓库级 React 契约 E2E 由根目录脚本启动隔离后端、前端并自动清理。请从仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify_react_e2e.ps1
```

运行安全细节见 [`../docs/runtime-safety.md`](../docs/runtime-safety.md)，长篇最终状态见 [`../docs/longform-runtime-contract.md`](../docs/longform-runtime-contract.md)。

# AI 小说创作系统

这是一个面向单机、单作者工作流的长篇小说创作系统。正式界面是 `frontend-react/` 的 React 工作台。

当前产品主线是：新建作品 → 雪花十步构思 → 物化章节与场景 → 逐场 AI 起草或人工写作 → 作者复核并提升权威正文。系统提供带正文证据的正史事实链、连续性检查、内容与来源安全提示、LLM 用量记账和后台任务恢复，但不应被理解为多用户 SaaS、生产级高可用服务或自动出版裁决器。2026-09 的减法已经移除结果治理/盲评实验、知识提升与索引发布台、长篇控制塔、互操作导出台、旧 Vue 前端以及一批只有旧界面才能触达的 v1 接口。

## 当前界面与能力边界

启动后默认进入 `主页`，不是雪花页。作家模式的日常入口包括：

- `主页`、`流程`：查看当前作品和下一步。
- `构思`：真实项目创建、雪花十步、场景急救、结构物化与回流。
- `写作`：人工编辑、AI 候选、草稿保存、内容安全复核和权威正文提升。
- `风格`：上传参考书并学习抽象风格画像。
- `待办`、`资料`：处理人工决策与故事资料。

切到高级模式后会显示 `章节编排`、`AI 起草台`、`成稿中心`、`文学质量` 和 `成本看板`。

当前需要特别区分：

- 新建作品、雪花步骤保存/批准、结构物化、逐场 AI 起草、草稿同步和权威正文提升都有真实后端链路。
- 演示作品与假生成已退役：不再有内置演示作品（原 `潮汐档案`/`盐镇来信`）或离线确定性桩生成。所有生成节点一律 fail-closed——未配置可用 LLM 时返回 409/502 与 `author_action` 引导，绝不返回罐头文本。
- 高级 `章节编排` 的 `运行本章` 会启动持久化章节任务、轮询真实进度，并明确展示阻断、失败与模型未配置状态。
- `成稿中心` 的终稿批准必须先完成当前终稿的正史事实复核，再显式确认已通读当前服务端正文，随后依次写入正文哈希确认和项目级 `approve-final`。未接受的模型候选不会进入后续提示词；目录不能直接伪造 `approved`，重开终稿必须填写原因，并由服务端级联撤销受影响的后续批准。

## 本地快速启动

依赖：Python 3.12、Node.js/npm。后端的传递依赖已锁定并带下载哈希；不要用仓库根目录安装 npm 包（根目录不是 Node 项目）。

```powershell
cd backend
uv sync --locked --extra dev
cd ..\frontend-react
npm ci
cd ..
.\start-dev.cmd
```

默认安装使用内存向量后端，不包含 Chroma。需要在 WSL/Linux 中执行真实 Chroma 兼容验证时，显式安装可选能力：

```bash
cd backend
UV_PROJECT_ENVIRONMENT=.venv-wsl uv sync --locked --extra dev --extra chroma
```

项目只支持嵌入式 `PersistentClient` 用法，不应启动或对外暴露 Chroma 自带的 HTTP/FastAPI 服务。

需要主动升级 Python 依赖时，修改 `backend/pyproject.toml` 后使用同一版 uv 重新生成并审查 `uv.lock` 和两份带哈希的导出锁文件：

```powershell
cd backend
uv lock --python 3.12
uv export --locked --extra dev --no-emit-project --format requirements-txt --output-file requirements.lock
uv export --locked --extra dev --extra chroma --no-emit-project --format requirements-txt --output-file requirements-chroma.lock
```

启动脚本会先执行 `alembic upgrade head`，随后启动后端与 React 前端；生产启动链路不会注入测试夹具或演示作品。默认地址：

- React：`http://127.0.0.1:5174`
- 后端：`http://127.0.0.1:8000`
- 存活检查：`http://127.0.0.1:8000/live`
- 就绪检查：`http://127.0.0.1:8000/ready`

若端口被占用，脚本会选择可用端口，并把后端地址写入 `.codex-run/backend.url`。

```powershell
.\stop-dev.cmd
.\restart-dev.cmd
```

## 推荐创作路径

1. 从作品切换器选择 `新建作品`，填写标题、题材、目标字数/章节数和起始大纲。
2. 进入 `构思`，逐步生成、编辑、保存并确认雪花十步。
3. 完成场景列表与场景规划后运行场景急救，处理 `合格 / 需修改 / 废除重写`。
4. 使用 `整理成章节结构` 将已确认内容物化为 `ChapterGoal` 与 `SceneCard`。
5. 直接进入 `写作`、在 `AI 起草台` 逐场生成，或在高级 `章节编排` 中运行当前整章并观察持久化进度。
6. 人工审阅 AI 候选；提升权威正文时按准确的内容安全发现码逐项确认。
7. 在 `成稿中心` 的“正史”页签核对正文事实候选，完成每场提交后再通读当前服务端正文并批准终稿；需要重开时填写可审计原因。文学质量提示始终需要作者判断。

雪花步骤允许带原因跳过，但读者定位、一句话概括、一段话概括、场景列表和场景规划是结构物化前的硬检查项。
完整十步依次为：读者定位、一句话概括、一段话概括、角色摘要表、一页梗概、角色背景故事、长篇大纲、角色全档案、场景列表、场景规划。

## 数据库与迁移

当前代码要求唯一 Alembic head `20260904_0083`（该版本删除已退役功能的数据表，不可回退，升级前请先备份）。`20260802_0077` 合并了曾发布的
`20260717_0074 -> 20260717_0075` real-only 分支与
`20260722_0074 -> 20260725_0076` 雪花分章分支；旧分支数据库可直接执行
`alembic upgrade head`，不要手工修改 `alembic_version`。

```powershell
cd backend
.\.venv\Scripts\python.exe -m alembic current
.\.venv\Scripts\python.exe -m alembic heads
.\.venv\Scripts\python.exe -m alembic upgrade head
```

`0073` 会把历史 LLM 审计中的提示词、草稿、模型输出和供应商错误正文改写为有界指纹；正文仍保留在各自权威业务表中。该脱敏不可逆，升级已有数据库前应先备份。

`/live` 只表示进程存活；`/ready` 还会检查数据库连接、迁移版本和必需结构，部署探针应使用两者的不同语义。

## 网络与令牌

后端默认 `NOVEL_SYSTEM_LOCAL_ONLY=true`，只接受回环请求并拒绝转发头。若明确需要远程访问，必须同时设置：

```powershell
$env:NOVEL_SYSTEM_LOCAL_ONLY = "false"
$env:NOVEL_SYSTEM_REMOTE_ACCESS_TOKEN = "使用足够长的随机值"
$env:NOVEL_SYSTEM_CORS_ORIGINS = "https://你的前端域名"
```

`start-dev.cmd` 仍只监听 `127.0.0.1`；以上变量不会自动把端口暴露到网络。远程部署还需要单独配置监听地址或可信反向代理，并遵守运行安全文档中的边界。

浏览器客户端通过 `X-Novel-Access-Token` 发送令牌；可用 `VITE_NOVEL_SYSTEM_ACCESS_TOKEN` 注入默认值，运行时值只保存在 `sessionStorage`。这是共享访问令牌，不是用户登录、RBAC 或租户隔离；构建进前端的值也不能视为对浏览器用户保密。

完整的网络、额度、内容复核、路径导入和恢复边界见 [运行安全与资源边界](docs/runtime-safety.md)。长篇冻结与最终状态语义见 [长篇运行时契约与终稿状态](docs/longform-runtime-contract.md)，正文事实的候选、复核和提交规则见 [正史连续性与长篇记忆](docs/canon-continuity.md)。本轮整改与残余盲区见 [系统整改记录（2026-07-16）](docs/system-remediation-2026-07-16.md)。

完整文档入口、维护状态和历史资料边界见 [文档导航](docs/README.md)。日常使用以 [操作手册](docs/operator-manual.md) 为准；日期化的计划、证据和进度记录只说明当时状态，不替代 README、操作手册和运行时契约。

## 恢复与数据重置

服务启动时会尝试恢复可安全重放的场景、章节、风格学习和验证后台任务；持久化租约用于避免重复接管。写作界面的 `同步与恢复中心` 会收集浏览器本地冲突稿、离线稿和配额失败稿，可比较、导出、重试或恢复。

浏览器恢复记录不是服务端备份，清理站点数据、换浏览器/设备、无痕模式或存储配额耗尽都可能令其不可用。

数据库备份与恢复必须在服务停止后进行；工具会校验 sidecar 清单、SHA-256、SQLite 完整性与外键。可用 `scripts/db_backup_drill.ps1`（Windows）或 `scripts/db_backup_drill.sh`（Linux）在临时副本上演练，详细命令见[运行安全与资源边界](docs/runtime-safety.md)。

`reset_author_state` 会批量删除作者态项目与运行产物，不属于首次启动步骤。只有在已有数据库备份且确认要清空作者态时才执行：

```powershell
cd backend
.\.venv\Scripts\python.exe -m novel_system.tools.reset_author_state
.\.venv\Scripts\python.exe -m novel_system.tools.reset_author_state --execute --yes
```

第一条命令仅做 dry-run。

## 验证

```powershell
cd frontend-react
npm run test
npm run build
```

```powershell
cd backend
0..3 | ForEach-Object {
  .\.venv\Scripts\python.exe scripts\pytest_shard.py --shard-index $_ --shard-count 4 -- -q -m "not chroma_integration"
}
```

该循环与 CI、`scripts/verify_windows.ps1` 使用同一分片规则；四片全部通过才等价于后端 non-Chroma 全量通过。

React 主线的浏览器契约验收会使用隔离 SQLite 数据库和中性测试夹具，不会接触日常开发库：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/verify_react_e2e.ps1
```

Linux/CI 使用等价入口：`bash scripts/verify_react_e2e.sh`。GitHub Actions 会在每次 PR/push 中运行后端测试、React 单测与构建和 React 契约 E2E。

关键代码入口：

- 前端导航：`frontend-react/src/ws-app.jsx`
- 雪花工作台：`frontend-react/src/ws-snow.jsx`
- 写作与本地恢复：`frontend-react/src/ws-writer.jsx`、`frontend-react/src/wr-doc-store.jsx`
- API 客户端：`frontend-react/src/lib/client.js`
- 后端应用与健康检查：`backend/src/novel_system/api/app.py`
- 场景执行与归档：`backend/src/novel_system/api/routes/scenes.py`
- 长篇契约：`backend/src/novel_system/services/longform_tower.py`

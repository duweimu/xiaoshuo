# 运行安全与资源边界

本系统默认按“单作者、本机桌面服务”运行。默认配置只接受回环地址请求；不要把本地开发服务直接暴露到公网，也不要把共享访问令牌当成多用户身份系统。

## 网络访问

- `NOVEL_SYSTEM_LOCAL_ONLY=true`：默认值，只允许 `127.0.0.1`、`::1`、`localhost`；`/live` 与 `/ready` 仍可用于健康探测。
- `NOVEL_SYSTEM_LOCAL_ONLY=false`：显式开启远程访问，同时必须设置 `NOVEL_SYSTEM_REMOTE_ACCESS_TOKEN`，所有非健康请求都要携带 `X-Novel-Access-Token`。
- 第一方 React 前端可在构建时设置 `VITE_NOVEL_SYSTEM_ACCESS_TOKEN`。该值会进入浏览器资产，只适合可信作者使用的受限网络；它不能替代账号、权限和租户隔离。
- 远程模式的共享 token 不是用户身份系统，因此服务端会忽略客户端 `X-Operator-Ref`，审计主体统一记为 `remote-access-token`；需要区分真实用户时必须在可信代理之后接入正式认证/RBAC。
- 远程模式下同时收紧 `NOVEL_SYSTEM_CORS_ORIGINS`，并在主机防火墙或可信反向代理处限制来源。

## 向量后端与 Chroma 隔离

- 默认 `NOVEL_SYSTEM_VECTOR_BACKEND=memory`，默认哈希锁安装不包含 Chroma。
- Chroma 仅作为 WSL/Linux 中的显式可选依赖：`cd backend && UV_PROJECT_ENVIRONMENT=.venv-wsl uv sync --locked --extra dev --extra chroma`，随后再设置 `NOVEL_SYSTEM_VECTOR_BACKEND=chroma`。
- 当前集成只允许进程内 `PersistentClient`。不要运行或暴露 Chroma 自带的 HTTP/FastAPI 服务；当前锁定版本仍受 `CVE-2026-45829` 的服务端代码注入问题影响，上游发布修复版前必须保持这一网络隔离。

## LLM 硬额度

**所有额度默认关闭（`0` = 不限制）。** 本系统是单作者本机服务，硬额度挡不住任何第三方，只会在作者写到一半时把生成拦下来，因此不预设上限；需要给自己上闸门时，把对应环境变量设成正数即可立即生效。

启用后的额度在真正调用供应商之前原子检查：达到上限时请求不会发给模型，也不会被记成已消费 token。一道闸门都没启用时，派发前的检查会直接短路，不做任何计数扫描。

关掉额度不等于停止记账：账本与成本看板的用量读数照常记录，只是不再有上限可比对，看板对应行显示为「未设限」。唯一的例外是「今日金额」行——它按上表的 env 单价计价，与看板其余成本数字（走 `config/pricing.yaml` 快照）不是同一口径，未启用金额闸门时恒为 0，因此只在该闸门启用时才显示。

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `NOVEL_SYSTEM_LLM_DAILY_TOKEN_LIMIT` | `0`（不限制） | 全实例 UTC 日 token 上限 |
| `NOVEL_SYSTEM_LLM_MONTHLY_TOKEN_LIMIT` | `0`（不限制） | 全实例 UTC 月 token 上限 |
| `NOVEL_SYSTEM_LLM_PROJECT_DAILY_TOKEN_LIMIT` | `0`（不限制） | 单项目 UTC 日 token 上限 |
| `NOVEL_SYSTEM_LLM_DAILY_REQUEST_LIMIT` | `0`（不限制） | 全实例 UTC 日请求上限 |
| `NOVEL_SYSTEM_LLM_MAX_CONCURRENT_REQUESTS` | `0`（不限制） | 同时在途的供应商请求上限 |
| `NOVEL_SYSTEM_LLM_RESERVATION_RECOVERY_TTL_SECONDS` | `3600` | 启动时回收无场景/任务所有权的陈旧 LLM 预留前，至少等待的秒数 |
| `NOVEL_SYSTEM_LLM_DAILY_COST_LIMIT_USD` | `0` | 可选日美元上限；`0` 表示不用金额闸门 |
| `NOVEL_SYSTEM_LLM_INPUT_COST_PER_MILLION_USD` | `0` | 金额估算所用输入单价 |
| `NOVEL_SYSTEM_LLM_OUTPUT_COST_PER_MILLION_USD` | `0` | 金额估算所用输出单价 |

启用金额上限时必须至少配置一种 token 单价。不同供应商或模型价格不同时，应使用能够覆盖风险的保守单价；系统内的金额仍是治理估算，不是供应商账单。

启用后，全局日/月/项目 token 配额对尚未结束的调用按 reservation 占位，对终态调用按供应商返回的实际 `total_tokens`（缺失时按保守估算）计费。场景预算的 `budget_charged_tokens` 仍受 reservation 上限约束；它不是全局供应商用量口径。

## 内容与来源安全

- `NOVEL_SYSTEM_CONTENT_SAFETY_MODE=review`（默认）：少数高风险复合启发式命中会阻止无人值守归档，正文不会丢失；作者逐项核对后可确认精确 finding code 再提交。
- `NOVEL_SYSTEM_CONTENT_SAFETY_MODE=audit`：只留痕和提示，不阻断归档。
- 启发式不能判断真实年龄、同意关系、叙事立场、隐喻或跨语言表达；未命中不等于安全，命中也不是法律或平台分级结论。
- 服务器路径导入参考书默认关闭。只有设置 `NOVEL_SYSTEM_STYLE_REFERENCE_IMPORT_ROOTS` 后，管理员才能从列出的根目录导入；Windows 多目录使用分号分隔。浏览器上传仍是推荐入口。

## 后台恢复边界

服务启动时会恢复尚未派发的场景/章节任务，并通过数据库租约和任务 CAS 防止同波重复执行。已有活跃 lease 的任务不会被抢占。参考书抽取没有安全的中段检查点，因此崩溃遗留的运行会被明确标成“失败、可重试”，而不是伪装为断点续跑。

启动恢复还会对超过 `NOVEL_SYSTEM_LLM_RESERVATION_RECOVERY_TTL_SECONDS` 的 legacy LLM 预留做保守对账：仅处理没有 `scene_id`、没有 `run_job_id` 且不是 scene scope 的调用；未派发预留会释放，已派发但没有持久化结果的预留会标记失败并按估算用量落账。场景与任务所有权链路由各自的 lease/checkpoint 恢复负责，不进入这项扫描。

当前执行器仍是进程内线程池，不是外部持久队列。需要多主机、高可用或不可信多用户访问时，应另行引入身份授权、租户隔离、外部任务队列、密钥托管和集中审计；现有远程开关不代表这些能力已经具备。

## SQLite 备份与恢复

备份使用 SQLite 在线备份 API，可包含仍在 WAL 中但已经提交的写入。新快照只有在完整性、外键、页信息和 SHA-256 清单全部通过后才会替换同名旧备份；恢复拒绝没有 `.meta.json` 清单、被篡改、外键损坏或 WAL 正忙的来源/目标。

恢复仍然是停机操作。工具可以发现活动事务，却不能证明另一个空闲进程不会在检查后重新写入；先用 `stop-dev.cmd` 或对应 Linux 停止脚本停掉服务，再备份/恢复。不要把浏览器恢复记录或回收站当成数据库备份。

```powershell
cd backend
python -m novel_system.tools.db_backup --backup .\novel_system.db .\backups\novel-system.db
python -m novel_system.tools.db_backup --verify .\backups\novel-system.db
python -m novel_system.tools.db_backup --restore .\backups\novel-system.db .\novel_system.db
cd ..
powershell -ExecutionPolicy Bypass -File scripts\db_backup_drill.ps1
```

Linux 恢复演练入口为 `bash scripts/db_backup_drill.sh`。两个演练脚本都只破坏系统临时目录中的副本，不改动传入的真实源库。

## 历史 LLM 审计载荷脱敏

`LlmCall`、`LlmCallAttempt` 与幂等 `OperationLog` 是计费、追踪和故障恢复账本，不应成为第二份小说正文仓库。新写入只保留哈希、长度、消息角色、受限协议字段及有界结构摘要；0073 迁移会把旧记录中的完整提示词、作者草稿、模型输出和供应商错误正文改写为同类摘要。该迁移不可逆，升级前先按上节完成可验证备份。

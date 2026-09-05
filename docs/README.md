# 文档导航

本文档是仓库文档的统一入口，最后核查日期为 2026-09-04。若日期化计划、旧证据与现行代码冲突，以根目录 `README.md`、本页列出的运行时契约和当前代码为准。

## 日常使用

- [操作手册](operator-manual.md)：React 正式工作台的入口、创作主线、异常处理和数据边界。
- [运行安全与资源边界](runtime-safety.md)：网络、令牌、额度、内容复核、路径导入、恢复和备份约束。
- [正史连续性与长篇记忆](canon-continuity.md)：正文事实候选、证据复核、权威提交、上下文注入和历史数据迁移。

## 开发与发布

- [发布检查清单](release-checklist.md)：CI、Windows、React E2E 和 WSL Chroma 发布门。

## 当前专项记录

- [章节编排 LLM 接入设计（2026-07-16，已实现）](chapter-arrangement-llm-design-2026-07-16.md)：章节蓝图一等公民 + 上下文底座 + 候选/补全/体检三通道与只填空补丁纪律。
- [雪花「整理成章节结构」重新设计（2026-07-25，设计稿待实施）](snowflake-chaptering-design-2026-07-25.md)：构思侧章表一等公民 + 可预览分章 + scene_id 撞号与幽灵场两个数据缺陷的修复方案。
- [风格参考设计](style_reference_module_design_v1.1.md)、[实施账本](style-reference-progress.md)与[Phase 3 完成记录](style-reference-phase3-backlog.md)：后两者是历史实施依据，Phase 3 A/B/C 已全部完成。
- [风格参考动态模仿 v2](style-reference-dynamic-imitation-v2-2026-08-20.md)：任意参考语料契约、软分布提示、自然度门控、独立评测和开源融合决策。
- [风格参考 RAG v2：内容克制检索](style-reference-rag-content-independence.md)：结构化风格签名、旧索引迁移、合成 A/B 及证据边界。
- [风格参考运行时契约与反馈闭环](style-reference-runtime-contract.md)：冻结风格血缘、统一上下文/基线、降级规则和盲选校准反馈。

## 文档维护规则

1. 根 README 只保留当前启动、主流程、数据迁移和验证入口。
2. 操作手册只描述正式 React 工作台。
3. `output/`、测试截图、PID、日志、IDE 配置和测试缓存不得提交。需要长期引用的运行结论应归档为小型、可复算的摘要或 manifest。
4. 一次性审计、已完成迁移包和过期实施计划不在主线长期保留；Git 历史承担追溯职责。
5. 日期化证据不得被改写成“当前状态”；当前 Alembic head、命令和能力边界必须重新从代码或根 README 核对。

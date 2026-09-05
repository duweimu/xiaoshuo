# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Product Scope

Single-machine, single-author long-form novel writing system (Chinese-language product). The root `README.md` (Chinese) is the product-truth entry point and `docs/README.md` indexes all documentation — the operator manual documents only the React workbench; dated plans/evidence describe their original run, not the current contract. Product mainline: create work → 10-step snowflake ideation → materialize chapters/scenes → per-scene AI drafting or manual writing → author review promotes authoritative text → final approval in 成稿中心. Explicitly *not* a multi-user SaaS, HA service, or automatic publication arbiter.

**2026-09 subtraction (batch 1)** removed, wholesale: the legacy Vue frontend (`frontend/`), the outcome-governance / blind-evaluation / hidden-benchmark cluster, the knowledge-promotion + versioning + vector-alias / reindex / verify layer and its 知识/索引/域 consoles, the interop (bundle-worksheet) center, the 长篇控制塔 backend (anchors, chapter contracts, audits, structure guidance), the legacy `style_profile` extractor and its six rule tables, the advisory narrative overlays (theme anchor, tension curve, foreshadow lifecycle, character arc/psychology, relationship matrix, voice fingerprint, POV coloring, style-drift guidance, reverse causal skeleton, work profiles), chapter runtime backfill/manual-hold, project backtrack items, scene quality contracts / auto-rewrite, author structure extraction / discovery drafts, writer-review LLM diagnosis nodes, writer-room / author-desk, and most operator tools (only `reset_author_state`, `db_backup`, `sync_prompt_templates`, `raise_llm_output_budget`, `chroma_smoke` remain). A few thin v1 primitives were deliberately kept because the test suite builds on them even though React never calls them: `POST/GET /api/v1/projects|chapters|scenes`, `GET /api/v1/projects/{id}/dashboard`, the v1 `snowflake` route, `POST …/chapters/{id}/run` (sync), and `POST /api/v1/scenes/{id}/run/full`. Migration `20260904_0083` drops their tables irreversibly. Do not re-introduce these; batch 2 (simplify approvals, accounting, system config) and batch 3 (scene pipeline rewrite) are planned next. **Demo data / fake generation are retired**: there is no product demo work, no seeded 「潮汐档案」/「盐镇来信」, and no offline/deterministic stub generation. Every generation node is fail-closed — with no live LLM configured it returns a 409/502 with an `author_action` (never canned prose). The old demo seed lives on only as neutral **test fixtures** under `backend/tests/fixture_works.py` (works `work-a`/`work-b`) + `backend/tests/fixture_runtime.py` (`seed_runtime_fixture`), consumed by tests and the contract-E2E lane; the production runtime never seeds them.

## Common Development Commands

### Full Stack Lifecycle (Windows)
- Start full stack: `.\start-dev.cmd` (runs backend + frontend, opens browser)
- Stop full stack: `.\stop-dev.cmd`
- Restart full stack: `.\restart-dev.cmd`
- Reset runtime DB/artifacts but keep LLM config: `.\reset-runtime-keep-llm.cmd` (→ `scripts/reset_runtime_keep_llm.ps1 -StopServices`; distinct from the Python `reset_author_state` tool below)

On Linux use the shell equivalents: `scripts/start-all-linux.sh` / `scripts/stop-all-linux.sh` (one-shot backend + React frontend, health-probed, logs + pidfiles under `.codex-run/`), or the per-leg `scripts/start-backend-linux.sh` / `scripts/start-frontend-linux.sh` (all safe to re-run — each leg stops its own previous instance first; shared helpers in `scripts/lib/dev-lifecycle.sh`). The backend leg runs `alembic upgrade head` with **`backend/.venv/bin/python`** (not system python) and forces `NOVEL_SYSTEM_VECTOR_BACKEND=memory`; the frontend leg resolves Node in the order `NOVEL_SYSTEM_NODE_BIN` (a `bin/` dir) → nvm at `~/.nvm` (falling back to `nvm use 16` only when no default is on PATH) → `~/.local/node/bin` (plain tarball install) → `node` already on `PATH`, failing with a clear message if none is found, and always preloads `frontend-react/crypto-polyfill.cjs` via `NODE_OPTIONS --require` (a no-op on Node ≥ 18; needed on Node 16, the ceiling of the old CentOS 7 / glibc 2.17 host, which lacks the global WebCrypto Vite 6 needs). `start-all-linux.sh` fails fast if a leg's process exits during startup and prints the tail of that leg's log. CI (`.github/workflows/ci.yml`, Ubuntu + Node 22) installs locked frontend dependencies with `npm ci`, runs unit/build gates, and uses `scripts/verify_react_e2e.sh` for an isolated migrated backend + React browser contract lane.

Default addresses: the **React frontend** `http://127.0.0.1:5174` (what `start-dev.cmd` auto-opens) and the backend `http://127.0.0.1:8000`. `start-dev.cmd` (→ `scripts/dev.ps1`) brings up the backend + React frontend in one shot: it runs `alembic upgrade head` (**no demo seed** — the production launcher never injects demo works), forces `NOVEL_SYSTEM_VECTOR_BACKEND=memory`, auto-generates a `NOVEL_SYSTEM_CONFIG_SECRET`, and writes pid/url files under `.codex-run/` (`backend.url`, `frontend.url`, `frontend-react.url`). If port 8000 is busy it scans upward and records the chosen URL in `.codex-run/backend.url`. Backend readiness is probed at `GET /ready` (a 90s timeout — if migrations are stale the probe fails and the browser never opens). The backend also exposes `GET /live` (process liveness only) and `GET /ready` (adds DB connectivity + migration revision + required-structure checks) — deployment probes should use their distinct semantics.

### Backend (Python 3.12 / FastAPI)
Located in `backend/`. **On Windows, do not activate a venv** — run backend `pytest` / `alembic` directly with the Anaconda Python on `PATH`, from inside `backend/`. The `.venv` / `.venv-wsl` dirs, when present, are Linux/WSL venvs (no `Scripts\Activate.ps1`) and are only consumed by the WSL/release lanes (e.g. `scripts/verify_wsl_strict.sh`). `pyproject.toml` sets `pythonpath=["src"]` and `testpaths=["tests"]`, so pytest/alembic must run **from `backend/`** with paths relative to it — and the PowerShell working dir resets to repo root between calls, so always `cd backend` in the same command.

CI installs `requirements.lock` with `pip --require-hashes`; `uv.lock` is the canonical cross-platform resolution. After changing `pyproject.toml`, run `uv lock --python 3.12` and then `uv export --locked --all-extras --no-emit-project --format requirements-txt --output-file requirements.lock`, and review both files. The root is not an npm package; only `frontend-react/` owns a Node lockfile.

```powershell
cd backend
python -m pytest                                        # all Windows-safe tests
python -m pytest tests/test_snowflake_workspace_v2.py          # single file (path is relative to backend/)
python -m pytest -k "test_materialize"                 # by name pattern
python -m pytest -m "not chroma_integration"           # exclude Linux-only tests
```

Tests marked `@pytest.mark.chroma_integration` require real ChromaDB and are auto-skipped on Windows by `backend/tests/conftest.py`. Run them via WSL:
```
wsl -d Ubuntu-24.04 bash -lc "cd <wsl-path> && bash scripts/verify_wsl_strict.sh"
```
The other declared marker, `consistency_validation` (blueprint §17 Action B recall/precision), is **not** auto-skipped and runs in the default Windows suite.

Full Windows CI lane: `powershell -ExecutionPolicy Bypass -File scripts/verify_windows.ps1` (pytest `-m "not chroma_integration"` + the **React mainline** `frontend-react` `npm test` (vitest) + build). Full release lane (Windows CI → **React mainline contract E2E** (`scripts/verify_react_e2e.ps1`, see below) → WSL strict Chroma): `scripts/verify_release.ps1`.

GitHub Actions (`.github/workflows/ci.yml`) gates on every PR/push: **Backend Quality Gates** (ruff + architecture tests), **Backend Tests** (pytest `-m "not chroma_integration"`, 4 shards), **Backend Chroma Integration**, **Frontend Tests (React mainline)** (`frontend-react`: `npm ci` → vitest → build), and **React Contract E2E** (fresh migration + isolated seeded backend + real Chromium). WSL strict Chroma remains local in `verify_release.ps1`; React contract E2E runs both in CI and in that local release lane.

**Schema-drift guards** (`backend/tests/test_metadata_isolation.py`) — the most important non-obvious gotcha. Historical Alembic revisions are frozen explicit DDL and may not import the live application ORM. The test suite builds its schema via `Base.metadata.create_all`, while dev/prod use Alembic `upgrade head` (`auto_create_tables` defaults to `False`); the guard builds it **both** ways and diffs tables/columns/named-indexes. An ORM change without a matching migration must fail here instead of surfacing later as `OperationalError: no such column`. If it fails: write the missing migration, or declare a migration-only index in the model's `__table_args__`. Run: `cd backend; python -m pytest tests/test_metadata_isolation.py`.

### Frontend (React, primary) — `frontend-react/`
The production frontend is the maintained Vite + React 18 「潮汐工作台」 in
`frontend-react/`. `start-dev.cmd` serves it on `http://127.0.0.1:5174` and opens it
by default (landing view is `主页`, not the snowflake view).

```powershell
cd frontend-react
npm ci             # exact versions from package-lock.json
npm run dev        # http://127.0.0.1:5174
npm run build
```

Architecture rules:
- **Store layer only**: views keep the prototype's store contracts — `WsWorks` / `WsCatalog` +
  `WsTrashStore` / `WsReview` (the review store) / `WsLibrary` / `Lf7Bridge`, plus the newer
  `WsManuStore` (成稿中心, `ws-manuscripts-store.jsx`), `WsCost` (`ws-cost.jsx`), `WsEval`
  (`ws-eval.jsx`), `WsAiProviders` (`ws-ai-providers.jsx`), and the writer-side
  `WrDocs`/`WrDocVersions`/`WrRecovery` (`wr-doc-store.jsx`). These are **runtime globals attached
  to `window`** (`Object.assign(window, {...})`) from kebab-case files — grep `window.WsWorks`,
  not an ES import. Stores are API-backed with sync in-memory caches (optimistic write +
  rollback / refetch-on-failure). Writer/advanced mode gating lives here too (`ws-app.jsx`
  `WS_NAV_GROUPS`).
- **Store unit tests** (vitest, `src/*.test.jsx`, ~20 suites): cover the optimistic-write +
  rollback/refetch contract per store — `ws-works`, `ws-catalog` (incl. `WsTrashStore`),
  `ws-review`, `lf7-bridge`, and the newer `ws-ai-providers` / `ws-chapter-run` / `ws-cost` /
  `ws-scene-run` / `ws-manuscripts` / `wr-doc-store` / `wr-canonical-control` /
  `wr-content-safety-review` / `wr-recovery-center` suites.
  They `vi.mock("./lib/client.js")` and route `apiGet` by URL through the shared
  `src/test-helpers.js` `installApiRouter`; each store loads in isolation via `vi.resetModules()` +
  dynamic import (the active work falls back to a `__loading__` placeholder until a seeded
  `/api/v2/projects` resolves with a real `project_id`). Run `npm test` (or the `verify_windows.ps1`
  React gate / GitHub CI). Keep tests falsifiable — verify rollback/alert paths actually trip.
- `src/lib/client.js` owns the shared client contract (envelope / X-Idempotency-Key /
  X-Operator-Ref / `novel-system-api-base` localStorage override).
- localStorage holds only UI preferences and read caches of backend truth
  (`wr-doc:*` is a write-through cache of author-drafts); business writes all go
  through `/api/v1` + `/api/v2` endpoints.
- Test/E2E fixture works come from `backend/tests/fixture_works.py` (`seed_fixture_works`,
  project ids `work-a`/`work-b` — keep these literal ids) + `backend/tests/fixture_runtime.py`
  (`seed_runtime_fixture`, also seeds the `PRJ_DEMO_CH001` runtime fixture). These are neutral
  placeholder works for tests only — the product runtime never seeds them.
- Contract-level E2E: `cd frontend-react; node scripts/run-smokes.mjs [BASE] [API]`
  (runs the acceptance suite, `smoke-phase2..7`, `smoke-ai-settings`, and `qa2-ui`, reseeding via `python tests/fixture_runtime.py` before each suite; uses
  frontend-react's own locked Playwright install). Defaults are `BASE=http://127.0.0.1:5174/` and a **separate
  seeded backend** `API=http://127.0.0.1:8009` — not the dev `:8000`. `scripts/verify_react_e2e.ps1`
  now orchestrates this end-to-end (isolated e2e sqlite under `.codex-run/e2e/`, seeded `:8009`
  backend + React `:5174`, full process-tree teardown) and is wired into both GitHub Actions
  (`scripts/verify_react_e2e.sh`) and `verify_release.ps1` as a default gate. **Gotcha**: a fresh `alembic upgrade head` aborts on migration `20260523_0036`'s
  legacy-backup guard unless `backups/style_reference_legacy_*.json` exists; the e2e lane satisfies
  it via that migration's `STYLE_REFERENCE_REPO_ROOT` test override pointed at a placeholder backup.
- Current user and engineering documentation is indexed in `docs/README.md`; dated plans and
  evidence describe their original run and are not the current runtime contract.

### Database Migrations
```powershell
cd backend
python -m alembic current
python -m alembic heads
python -m alembic upgrade head
```

A `database operation failed` response usually means the schema is stale — run `upgrade head` first.

Migration `20260716_0073` **irreversibly** rewrites historical LLM-audit payloads (prompts, drafts, model output, provider error bodies) into bounded fingerprints, and `20260904_0083` irreversibly drops the tables of the retired feature clusters — back up an existing DB before upgrading past either. `GET /ready` performs the structural check (revision + required columns); the standalone preflight tool was removed.

### Author-State Reset
Wipes all project/snowflake/chapter data while preserving reference profiles and system config:
```powershell
cd backend
python -m novel_system.tools.reset_author_state           # dry-run
python -m novel_system.tools.reset_author_state --execute --yes
```

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `NOVEL_SYSTEM_DATABASE_URL` | `sqlite:///./novel_system.db` | SQLAlchemy DB URL |
| `NOVEL_SYSTEM_VECTOR_BACKEND` | `chroma` | `chroma` or `memory` (memory = deterministic, no ChromaDB) |
| `NOVEL_SYSTEM_CHROMA_DIR` | `./.vector_store` | ChromaDB persistence path |
| `NOVEL_SYSTEM_LLM_ENABLED` | `false` | Enable real LLM calls |
| `NOVEL_SYSTEM_LLM_PROVIDER` | `openai_compatible` | Provider key resolved against the `services/llm_providers/` adapter registry (12 adapters: `openai_compatible`, `openai`, `anthropic`, `deepseek`, `zhipu_glm`, `gemini`, `qwen_dashscope`, `moonshot`, `minimax`, `doubao_ark`, `xai`, `ollama`) |
| `NOVEL_SYSTEM_LLM_BASE_URL` | `https://api.openai.com/v1` | Provider base URL |
| `NOVEL_SYSTEM_LLM_API_KEY` | — | API key for the provider |
| `NOVEL_SYSTEM_LLM_TIMEOUT_SECONDS` | `0` (no ceiling) | Per-call LLM response timeout. `0` = wait as long as the model needs (slow long-context tasks are normal work, not faults); connection setup keeps its own finite `LLM_CONNECT_TIMEOUT_SECONDS` so an unreachable endpoint still fails fast. A positive value re-arms a ceiling, as does a per-node `timeout_seconds` in `config/models.yaml`. Connectivity probes ("测试连接") are always finite and ignore this |
| `NOVEL_SYSTEM_ADMIN_TOKEN` | — | Admin endpoint auth token |
| `NOVEL_SYSTEM_CONFIG_SECRET` | — | Secret for encrypted config snapshots |
| `NOVEL_SYSTEM_AUTO_CREATE_TABLES` | `false` | If true, bypass Alembic and `create_all` tables on startup — **dangerous**: hides schema drift (see the schema-drift guard) |
| `NOVEL_SYSTEM_LLM_AUTO_CRITIQUE_ENABLED` | `false` | §8 opt-in: layer an independent LLM editor critic on top of the rule-based pass in `orchestrator.py` (after Best-of-N, before soft QC) |
| `NOVEL_SYSTEM_LLM_EVENT_EXTRACTION_ENABLED` | `false` | §2 opt-in: extract narrative events from finished prose (`prose_event_extractor.py`) |
| `NOVEL_SYSTEM_CHROMA_COLLECTION_PREFIX` | `novel_system` | Prefix for ChromaDB collection names |
| `NOVEL_SYSTEM_CORS_ORIGINS` | `…:5173/5174/5175/8081` | Comma-separated allowed CORS origins |
| `NOVEL_SYSTEM_EXPOSE_ERROR_DETAIL` | `false` | Whether the error envelope exposes `error.details` |
| `NOVEL_SYSTEM_LOCAL_ONLY` | `true` | Loopback-only: rejects non-loopback clients and forwarded headers. Setting `false` **requires** `NOVEL_SYSTEM_REMOTE_ACCESS_TOKEN` (`create_app` refuses to start otherwise) |
| `NOVEL_SYSTEM_REMOTE_ACCESS_TOKEN` | — | Shared access token when not local-only; the browser sends it as `X-Novel-Access-Token` (frontend default injectable via `VITE_NOVEL_SYSTEM_ACCESS_TOKEN`, runtime value lives in sessionStorage). Not user auth/RBAC |
| `NOVEL_SYSTEM_LLM_DAILY_TOKEN_LIMIT` / `…_MONTHLY_TOKEN_LIMIT` / `…_PROJECT_DAILY_TOKEN_LIMIT` / `…_DAILY_REQUEST_LIMIT` / `…_MAX_CONCURRENT_REQUESTS` | `0` (every fence off) | LLM quota family, all opt-in: `0` disables a fence, a positive value arms it and the accounting layer enforces it *before* provider dispatch. A single-author desktop install fences off nobody but the author, so nothing ships armed |
| `NOVEL_SYSTEM_LLM_DAILY_COST_LIMIT_USD` (+ `…_LLM_INPUT_COST_PER_MILLION_USD` / `…_LLM_OUTPUT_COST_PER_MILLION_USD`) | `0.0` (off) | Optional daily cost cap; a nonzero cap requires nonzero unit prices |
| `NOVEL_SYSTEM_LLM_RESERVATION_RECOVERY_TTL_SECONDS` | `3600` | TTL after which orphaned LLM accounting reservations are reclaimed |
| `NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER` | `0` (disarmed) | Per-scene lifecycle budget: the end-to-end token ceiling is `N × single-shot baseline` plus finite business/provider attempt caps. This was the last hard fence that shipped armed; it now joins the fence family — `0` = no scene ceiling (finite sentinel budgets, the pre-dispatch CAS gate is a no-op, so a single scene run is never blocked mid-draft). Accounting is unchanged (the ledger + 成本看板 still record every token; the dashboard shows a disarmed scene as 不限). A positive `N` re-arms `N × baseline`, and the attempt caps fall back to `config/models.yaml` `retry_budget`. Only affects **newly** initialized scenes — a scene whose immutable `scene_budget_basis_json` already exists keeps it (expand it via the author topup) |
| `NOVEL_SYSTEM_SNOWFLAKE_INPUT_TOKEN_BUDGET` | `0` (use per-template value) | Snowflake-workspace prompt **input** budget override, in estimated tokens. `0` = use each template's declared `input_token_budget` (the whole family is `24000`, a measured number — see the comment above `snowflake_generate_book_brief` in `config/prompts.yaml`). Set a positive value to tighten it for a small-context local model. Over-budget payloads are shed by relevance via `services/snowflake_prompt_budget.py` — never the step contract, the author's adopted direction, or the focused members — and the shedding is recorded in the LLM audit summary (`prompt_budget_*` fields) plus surfaced to the author as `health.generation_notice` when the ladder still can't fit |
| `NOVEL_SYSTEM_SCENE_INPUT_TOKEN_BUDGET` | `0` (use per-template value) | Prompt **input** budget override for the scene / planning / chapter template families that go through `services/prompt_builder.py` (scene-run drafting + QC + near-final review, writer scene/chapter diagnosis + revision, deep review + passage patch, author structure-extract / proposal, scene blueprint + chapter architecture + character pressure). `0` = use `max(template.input_token_budget, RUNTIME_MIN_INPUT_BUDGETS[name])` — the measured family values `24000` (scene) / `8000` (planning + passage patch) / `30000` (chapter), see the comment block above `neutral_draft` in `config/prompts.yaml`; the code floor is what protects a live install whose DB prompts snapshot still carries the pre-estimator values. A positive value replaces both (tighten for a small-context local model); it does not touch the snowflake family, which has its own `NOVEL_SYSTEM_SNOWFLAKE_INPUT_TOKEN_BUDGET` |
| `NOVEL_SYSTEM_CONTENT_SAFETY_MODE` | `review` | `review` or `audit` — content-risk triage mode on publication actions |
| `NOVEL_SYSTEM_SQLITE_FOREIGN_KEYS_ENABLED` | `true` | Enforce FK pragma on SQLite connections |
| `NOVEL_SYSTEM_CORS_ALLOW_CREDENTIALS` | `true` | CORS credentials flag |
| `NOVEL_SYSTEM_STYLE_REFERENCE_IMPORT_ROOTS` | — | Allowed filesystem roots for style-reference path imports |

## High-Level Architecture

### Backend (`backend/src/novel_system/`)

FastAPI application (`api/app.py`) with one router per domain area (`api/routes/`). Each route file maps to a service in `services/`. The `db/models.py` file contains all SQLAlchemy ORM models; `db/session.py` manages the engine. The authoritative list of mounted routers is the `include_router` calls in `api/app.py`; `api/routes/__init__.py`'s `__all__` now mirrors it one-to-one, enforced by the drift guard `backend/tests/test_routes_all_manifest.py`.

**Domain layers:**
- `services/projects.py` + `services/snowflake_workspace.py` — Snowflake Method planning pipeline
- `services/snowflake_planner.py` + `services/snowflake_steps.py` — step catalog, completeness gates, materialization rules
- `services/snowflake_workspace_llm.py` + `services/llm_task_runner.py` — LLM call orchestration. Two non-obvious contracts live in the first file: 「场景规划」(`scene_details`) full-table generation is **batched server-side** (`SCENE_DETAIL_BATCH_SIZE` scenes per call, `SCENE_DETAIL_MAX_BATCHES_PER_RUN` batches per request, resuming at the first scene that still has a missing field) because one call for a whole scene table always overflows `max_output_tokens`; and every task on this path goes through `services/snowflake_prompt_budget.py` for a real, enforced **input** budget (see the env var table). Progress, partial failures, and un-fittable payloads all report through `WorkspaceLLMResult.notice` → `health_json.generation_notice` → a warning toast in `ws-snow.jsx` — a half-finished or shed generation is never reported as a plain success
- `services/llm_client.py` + `services/llm_providers/` — multi-provider LLM client built on a **pluggable adapter registry** (`llm_providers/registry.py` + `presets.py`, one adapter module per provider, 12 in total). Edit provider behavior / default base URLs in `llm_providers/`, not `llm_client.py`
- `services/scene_generation.py` + `services/qc_engine.py` — scene pipeline and quality gates
- `services/style_reference/` — reference-book style subsystem (ingest → segment → extract → synthesize → inject → validate → materialize); replaced the legacy `reference_learning.py`. See "Style Reference subsystem" below
- `services/source_safety.py` + `services/reference_safety.py` — copy guardrails (protected source terms + n-gram copy detection over reference material)
- `services/vector_store.py` — ChromaDB abstraction; swapped for in-memory store when `NOVEL_SYSTEM_VECTOR_BACKEND=memory`
- `services/idempotency.py` + `services/hash_engine.py` — idempotency contracts for LLM calls and content hashing
- `services/orchestrator.py` (+ `bundle_builder.py`, `scene_execution.py`) — the scene-run pipeline (bundle context → generate → Best-of-N → optional auto-critique → QC). The **blueprint quality-floor v2 / anti-AI-taste** cluster hangs off it: `services/literary_quality.py` (21 weighted quality dimensions incl. `perception_filter` / `self_repetition` / `conflict_too_clean`; route `/api/v1/literary-quality`), `services/best_of_n_blind_eval.py`, `services/self_repetition.py` (cross-scene n-gram + semantic guard, reuses the style-reference plagiarism engine), `services/auto_critique.py` (Reflexion-style editor pass), `services/scene_criticality.py`
- Narrative-coherence / continuity overlay (backs the scene-run pipeline): `services/narrative_event_log.py` + `services/prose_event_extractor.py` (append-only event sourcing, populated by the 成稿中心 正史 extract flow), `services/canon_continuity.py`, `services/character_continuity.py` (deterministic pronoun/identity drift checks), `services/pov_knowledge_projection.py` (who-knows-what redaction). The theme/tension/foreshadow/arc/psychology/relationship/voice/drift advisory modules were removed in the 2026-09 subtraction
- **LLM cost cluster**: `services/llm_accounting.py` (durable accounting boundary around *every* provider POST — short reservation → dispatch → settlement transactions, no DB write txn held during network I/O; quotas fail before dispatch), `services/cost_aggregation.py` + `services/pricing.py` + `config/pricing.yaml` (centralized per-provider/model price snapshots, all placeholder rates marked `is_estimate: true`), `services/llm_audit.py` (audit rows store bounded fingerprints, not payloads — see migration `0073`), route `api/routes/cost.py`
- **Persistent jobs & crash recovery**: `services/scene_run_jobs.py` (cancellable persistent scene-run jobs) + `scene_run_checkpoint.py` + `scene_run_preflight.py`, `services/chapter_runner.py` (advanced-mode 运行本章 chapter jobs with polled progress), `services/background_recovery.py` (`run_startup_recovery` wired into the FastAPI lifespan — startup scan submits durable candidates; workers own the CAS so duplicate scans from multiple ASGI workers are harmless)
- **Canonical final-text lifecycle**: `services/canonical_manuscripts.py` + `services/final_text_gate.py` + `services/chapter_approval.py` — 成稿中心 final approval requires an explicit read-through confirmation of the *server-side* text, then a body-hash confirm, then project-level `approve-final`; reopening requires a reason and the server cascades revocation of downstream approvals. `services/archiver.py` and `services/narrative_position.py` support this layer
- `services/content_safety.py` — deterministic, auditable content-risk triage on publication: blocks only unattended publication of a few compound high-risk patterns; the author acknowledges exact finding codes on the exact publication action (dark themes stay writable, ordinary genre violence is advisory)
- `services/author_preferences.py` + `services/author_instructions.py` — scoped, prompt-safe author preference/instruction summaries shared by generation, review, and bundle construction

**Key data models** (all in `db/models.py`):
- `StoryProject` / `OutlinePlan` — top-level novel project and its outline
- `SnowflakeArtifact` / `SnowflakeStepRun` — per-step artifacts and run state for the 10-step snowflake
- `SnowflakeScenePlan` / `SnowflakeSceneTriageItem` — scene-level plans and quality triage
- `SnowflakeCharacterPlan` — per-character snowflake data
- `ChapterGoal` / `SceneCard` — materialized chapter/scene production units (created by structure materialization)
- `StyleReferenceBook` / `…Paragraph` / `…Run` / `…Extraction` / `…Finding` / `…Evidence` / `…Quote` / `…Profile` / `…InjectionBinding` / `…ValidationReport` / `…BannedTerm` / `…MetricEvent` / `…FindingFeedback` — the Style Reference subsystem's table family
- `NarrativeEvent` (append-only event-sourcing log — replay events up to a scene to reconstruct entity state), `VolumeSummary`, plus blueprint-v2 columns (`SceneCard.constraint_intensity`, `SceneRunState.criticality_level` / `candidate_dispersion_score`) — the causal/foreshadow/theme overlay on the snowflake pipeline
- `LlmCall` / `LlmCallAttempt` — durable LLM accounting rows (reservation → dispatch → settlement); payload columns hold bounded fingerprints since migration `0073`
- `ChapterRunJob` / `BackgroundRecoveryLease` — persistent chapter runs and the startup-recovery lease
- `AuthorPreferenceProfile` — scoped author preferences injected into prompts

**Configuration** lives in the project-root `config/` directory (not inside `backend/`):
- `config/models.yaml` — model profiles (`local_fast`, `quality_strong`, `dual_track`), task routing (task name → provider/model/temperature/response_format), and top-level `retry_budget` + `job_runtime` (lease/idempotency TTLs). The blueprint-v2 quality floor is config-driven here too: new task-routing entries (`scene_blueprint`, `character_pressure_blueprint`, …) and decoding penalties on `stylize` (`frequency_penalty` / `presence_penalty`, §7 anti-mean sampling)
- `config/prompts.yaml` — prompt templates with `system_prompt`, `task_prompt`, `structured_schema`, and `input_token_budget` (incl. the Scene Literary Blueprint v2 / Character Pressure Blueprint templates)
- `config/allowlists.yaml` / `config/hash_contract.yaml` — domain policy files
- `config/style_reference/` — Style Reference policy files (`banned_adjectives.yaml`, `extraction.yaml`, `injection_budget.yaml` incl. `rag_*` keys, `input_thresholds.yaml`, `sensory_lexicon.yaml`, `tolerance_floors.yaml`, `feedback.yaml`, `anti_plagiarism_template.txt`, `prompts/`)
- `config/pricing.yaml` — per-(provider, model) price snapshots with `effective_at`, used by cost aggregation (shipped rates are placeholder estimates, `is_estimate: true`)

Runtime LLM config can also be stored in the DB (via `SystemConfigSnapshot`) and applied on top of env vars by `settings.py:get_settings()`.

**Gotcha — the DB snapshot wins over the repo file.** Once the 系统配置 UI has saved a category, `load_active_config_payload(category)` returns the *active DB snapshot* and the repo file is never read: editing `config/models.yaml` (or `prompts.yaml`) then has **no effect on a running install**, silently. Re-importing the whole file is not a safe workaround either — it clobbers the per-node provider/model routing configured through the UI. To change one field on a live install, patch the active snapshot and activate a new version; `tools/raise_llm_output_budget.py` does exactly that for `max_output_tokens` (dry-run by default, `--execute` to apply) and is the pattern to copy.

**LLM node registry** (`services/llm_node_registry.py`) defines the catalog of all LLM-calling nodes as `LLMNodeSpec` dataclasses (node_id, model, temperature, reasoning_level, api_mode). The system config UI routes each node to a specific provider at runtime. `config/models.yaml` provides task-level defaults; DB-stored routes override them.

**Response envelope**: all API responses use `{ok: bool, data: ..., error: {code, message, details}, request_id}` (see `api/response.py`). Frontend `client.js` parses this envelope and throws `ApiRequestError` with structured fields on failure.

**Request middleware**: every request gets `request.state.request_id` (a hex prefix) and `request.state.operator_ref` (from `X-Operator-Ref` header or `"operator"`). Mutating frontend calls pass `X-Idempotency-Key` and `X-Operator-Ref` headers for idempotency and audit trails.

**Author-action pattern** (`services/author_actions.py`): when the backend detects a missing prerequisite (e.g., LLM not configured, step incomplete), it returns an `author_action` dict that tells the frontend which view to navigate to and what button to show. This avoids hard-blocking the user while still guiding them.

**Style Reference subsystem** (`services/style_reference/`, route prefix `/api/v2/style-reference`): the reference-book style engine that replaced the legacy `reference_learning.py`. Pipeline: `ingest` (import + checksum + `assess_input_size` layer gating) → `segmentation/` (paragraph typing via heuristic + LLM classifier with anchor-set calibration) → `extractors/` (four layers — `language` / `narrative` / `scene` / `theme` — over 16 sub-dimensions; each finding requires ≥2 evidence spans and rejects banned vague adjectives, enforced by Pydantic + two-level retry) → `profile_synthesizer` (16 sub-profiles → `StyleProfile` + metrics baseline) → `injection.py` (A=System-prompt / B=Few-shot / C=RAG strategies with a `style_intensity` slider and per-`TaskType` defaults; binding scope resolves scene > character > project > global) → `validation/` (three concurrent checks — `quantitative` adaptive-tolerance + `semantic` critic-LLM + `plagiarism` n-gram; sync fast-path for QC gates, async polling otherwise) → `materialization` (profile → `ReviewItem` → style rules). `metrics.py` computes hard quantitative anchors (sentence length, sensory-word frequency, dialogue ratio) as pure functions reused across extract/validate/preview. Findings are `observation` or `forbidden_pattern` (anti-samples), distinguished by `finding_kind`. Anti-plagiarism is two-layer: prevention (fixed System-prompt red-line segment) + detection (8-gram / 12-char). **Phase 3 additions** (the recent work): `rag.py` — Strategy C is a real three-granularity (sentence/paragraph/scene) vector recall, with chroma collections `style_ref_rag_{profile_id}_{granularity}` built at synthesize time, deterministic rerank, and no LLM on the inject hot path (acceptance `hit@5 ≥ 0.7`, WSL-only `chroma_integration` test); `finding_feedback.py` — per-operator 👍/👎 votes recalibrate a finding's confidence ±1 tier off a frozen `base_confidence` (policy in `config/style_reference/feedback.yaml`); scene/character binding scopes (`BindingScope` PROJECT/SCENE/CHARACTER); and `cleanup.py` `purge_derived_data` — the library-delete (删书) cascade that manually deletes ~10 derived tables in FK-reverse order plus `ReviewItem` rows and each profile's RAG index (SQLite has no `ON DELETE CASCADE`), deliberately keeping `…MetricEvent`. Authoritative design: `docs/style_reference_module_design_v1.1.md`; progress log: `docs/style-reference-progress.md`.

### Key Architectural Concepts

- **Snowflake Method pipeline**: The primary authoring flow. Author progresses through 10 ordered steps (reader positioning → one-line summary → one-paragraph summary → character summaries → one-page synopsis → character backstories → long outline → character bibles → scene list → scene planning). Each step produces a `SnowflakeStepRun` with draft JSON. Steps 1, 2, 3, 9, 10 are hard gates for structure materialization; others produce warnings.
- **Structure Materialization**: `POST /api/v2/projects/{id}/snowflake-workspace/materialize` converts approved `SnowflakeScenePlan` rows into `ChapterGoal` and `SceneCard` records. Proactive scenes get `Goal/Conflict/Setback` written to `SceneCard.writer_brief_json`; reactive scenes get `Reaction/Dilemma/Decision`.
- **Scene Triage**: Before materialization, each scene plan is scored and assigned a triage status (`qualified`, `needs_fix`, `rewrite`). The `suggest` endpoint uses LLM to recommend triage decisions.
- **Vector Backend Split**: Windows tests always use `memory` backend; real ChromaDB only runs in Linux/WSL and is gated by the `chroma_integration` pytest marker. `conftest.py` applies this automatically.
- **Style Reference (style imitation)**: Profiles are *abstract* (layered rhythm/syntax/imagery/narrative dimensions + anti-clone `forbidden_pattern`s) — the system must never copy source text, characters, settings, or signature imagery. See the "Style Reference subsystem" above for the ingest→extract→inject→validate pipeline; `services/source_safety.py` + `services/reference_safety.py` enforce the copy guardrails.
- **Dual-stack Pagination**: API responses support both `page`/`page_size` (offset) and `cursor`/`limit` (cursor-based) patterns via `services/pagination.py`.
- **Test Isolation**: `conftest.py` auto-creates an isolated SQLite DB per test in `tmp_path`, resets the engine, and auto-skips `chroma_integration`-marked tests on Windows. No shared test state between tests.
- **Snowflake Assistant Turns**: `SnowflakeWorkspaceAssistantService` stores conversational coaching turns per step, enabling LLM-guided iterative refinement of snowflake drafts without losing context.
- **Schema build split**: tests build the schema via `Base.metadata.create_all`, but dev/prod build it via Alembic migrations (`auto_create_tables` defaults to `False`). A model change without a matching migration therefore passes CI but 500s at runtime — the `test_metadata_isolation.py` drift guard (see Backend commands above) is the tripwire. Adding a column/index means writing a migration *and* keeping the ORM `__table_args__` in sync.
- **Scene-run orchestration & quality floor**: `services/orchestrator.py` runs a scene end-to-end — bundle context → generate (single candidate in production; the multi-candidate selection machinery is only reachable when tests override `_best_of_n_count`) → optional LLM auto-critique → QC gates — scored by `literary_quality.py`'s 21 dimensions and guarded against repetition by `self_repetition.py`. This is the "blueprint quality floor v2" layer on top of raw scene generation.
- **Narrative event sourcing**: `NarrativeEvent` is an append-only log treated as the single source of truth for story state; entity state at a given scene is reconstructed by replaying events up to that point (`narrative_event_log.py`, populated from prose by `prose_event_extractor.py` when event extraction is enabled).
- **Durable LLM accounting & quotas**: every provider POST goes through `llm_accounting.py`'s reservation → dispatch → settlement transactions; daily/monthly/per-project token, request, concurrency, and cost quotas are checked *before* dispatch, and no DB write transaction is held while waiting on the network. Orphaned reservations are reclaimed after a TTL. **Every quota is opt-in and ships disabled** (`0` = no ceiling) — with none armed the pre-dispatch gate short-circuits before running any counting scan. Accounting itself is unconditional: the ledger and the 成本看板 usage readings do not depend on a fence being armed.
- **Startup background recovery**: the FastAPI lifespan runs `run_startup_recovery` to re-submit durable scene/chapter/style/validation jobs after a crash; persistent leases + worker-owned CAS make duplicate scans harmless. Scene runs are cancellable and checkpointed.
- **Canonical final-text lifecycle**: chapter/project final approval is a deliberate multi-step gate (read-through confirm of server text → body-hash confirm → `approve-final`); the catalog cannot fake `approved`, and reopening needs an auditable reason with server-side cascade revocation of downstream approvals.
- **Local-only network boundary**: the backend defaults to `NOVEL_SYSTEM_LOCAL_ONLY=true` (loopback clients only, forwarded headers rejected); remote access requires an explicit shared token — there is no user auth, RBAC, or tenant isolation.
- **Demo vs. real data**: retired. There is no product demo work and no fake/offline generation — the 「潮汐档案」/「盐镇来信」 seeds and all offline stub clients were removed. Neutral placeholder works survive only as test fixtures (`backend/tests/fixture_works.py`, ids `work-a`/`work-b`). Every generation path is fail-closed: no live LLM → 409/502 + `author_action`, never canned output.

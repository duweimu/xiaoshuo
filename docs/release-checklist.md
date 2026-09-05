# Release Checklist

Use this checklist before converting a Draft PR to ready state or treating the current branch as release-ready.

## Automatic PR checks

- GitHub Actions **Backend Tests** job passed.
- GitHub Actions **Frontend Tests (React mainline)** job passed (vitest + build for `frontend-react`) — this is the authoritative frontend gate.
- GitHub Actions **React Contract E2E** job passed (fresh Alembic migration, isolated fixture database, real Chromium).

## Required local checks on this machine

- Run `powershell -ExecutionPolicy Bypass -File scripts/verify_windows.ps1`. The backend lane uses the same deterministic four-way file sharding as CI and writes one JUnit file per shard under `backend/.test-results/`; every shard must pass.
- Treat the backend pytest half of `scripts/verify_windows.ps1` as the required true-generation backend lane:
  `backend/tests/test_scene_generation.py` for fake-provider generation,
  `backend/tests/test_qc_engine.py` for fake-provider QC,
  and `backend/tests/test_chapter_runner.py` for the current chapter runner path.
- Run the **React mainline contract E2E** (default release gate for the production frontend):
  `powershell -ExecutionPolicy Bypass -File scripts/verify_react_e2e.ps1` — spins up an isolated
  seeded `:8009` backend + React `:5174`, runs `run-smokes.mjs` (acceptance, phase2..7,
  ai-settings, and qa2-ui), then tears the process tree down. Also runs automatically inside
  `scripts/verify_release.ps1`; Linux uses `bash scripts/verify_react_e2e.sh`.
- Run `wsl -d Ubuntu-24.04 bash -lc "cd <current-checkout-in-wsl> && bash scripts/verify_wsl_strict.sh"`
- Before that lane, prepare its isolated WSL environment with
  `cd backend && UV_PROJECT_ENVIRONMENT=.venv-wsl uv sync --locked --extra dev --extra chroma`; the default Windows environment and hashed install intentionally exclude Chroma.
- Replace `<current-checkout-in-wsl>` with the checkout/worktree root under review so the WSL lane verifies the same tree as the Windows lane.
- Deterministic fixture verification is required in CI; the React contract E2E job enforces it.
- Real-provider smoke tests are local-only evidence until secrets handling is formalized; if you run one, label it separately from CI-required coverage.

## Seeded browser E2E acceptance

- Record the `scripts/verify_react_e2e.ps1` result in the PR; this is the production React browser gate.
- Confirm the lane covers project creation/profile, catalog and prose persistence, trash restore/purge,
  review effects and dedupe, library projection, AI settings, cache-loss
  recovery, deep links, and a console-error sweep.
- Confirm every suite reseeded successfully; reseed failures must fail closed rather than reuse dirty state.
- Record the browser lane as deterministic offline fixture evidence, not a real-provider generation run.
- Use the manual walkthrough from the README only if the automated E2E lane fails or extra exploratory validation is needed

## PR evidence

- Paste or summarize the Windows verification result in the PR.
- Paste or summarize the seeded browser E2E result in the PR.
- Paste or summarize the WSL strict Chroma result in the PR.
- Record the provider config used for each verification lane, including whether `NOVEL_SYSTEM_LLM_ENABLED` stayed false / offline and any local-only real-provider overrides.
- Record the prompt template name/version used for the exercised scene pipeline templates from `config/prompts.yaml`.
- Record generation evidence: provider, model, prompt hash, finish reason, and final scene row id / archive receipt.
- Record QC evidence: hard/soft resolution code, next action, pass flag, and any human-review outcome if the lane did not archive cleanly.
- Describe how `X-Operator-Ref` was validated during the seeded E2E lane.
- Note where dual pagination (`page/page_size` and `cursor/limit`) was revalidated for review items if the change touched list contracts.
- Note any environment caveats or skipped checks.

## Release gate

- Keep the PR as draft until both local verification lanes and GitHub Actions are green.
- If any risk remains, document it in the PR before marking the work ready.


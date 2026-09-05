"""Add covering indexes for foreign-key lookup columns.

Revision ID: 20260805_0081
Revises: 20260802_0080
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections import defaultdict

import sqlalchemy as sa
from alembic import op


revision = "20260805_0081"
down_revision = "20260802_0080"
branch_labels = None
depends_on = None


FOREIGN_KEY_INDEXES: tuple[tuple[str, str], ...] = (
    ("attempt_tracker", "chapter_id"),
    ("chapter_audit_findings", "chapter_id"),
    ("chapter_contracts", "chapter_id"),
    ("chapter_goals", "outline_plan_id"),
    ("chapter_run_jobs", "chapter_id"),
    ("human_review_events", "chapter_id"),
    ("outline_plans", "project_id"),
    ("project_backtrack_items", "chapter_id"),
    ("project_backtrack_items", "project_id"),
    ("project_backtrack_items", "scene_id"),
    ("project_backtrack_items", "source_contract_id"),
    ("project_backtrack_items", "source_qc_report_id"),
    ("qc_reports", "chapter_id"),
    ("quality_benchmark_runs", "policy_id"),
    ("quality_strategy_policies", "evidence_experiment_id"),
    ("quality_strategy_policies", "benchmark_manifest_id"),
    ("scene_blueprints", "chapter_id"),
    ("scene_blueprints", "scene_id"),
    ("scene_bundles", "chapter_id"),
    ("scene_cards", "outline_plan_id"),
    ("scene_drafts", "chapter_id"),
    ("scene_execution_contracts", "project_id"),
    ("scene_execution_contracts", "chapter_id"),
    ("scene_execution_contracts", "scene_id"),
    ("scene_quality_contracts", "chapter_id"),
    ("scene_quality_contracts", "scene_id"),
    ("snowflake_artifacts", "project_id"),
    ("snowflake_assistant_turns", "project_id"),
    ("snowflake_character_plans", "project_id"),
    ("snowflake_revision_links", "project_id"),
    ("snowflake_scene_triage_items", "scene_plan_id"),
    ("snowflake_scene_triage_items", "project_id"),
    ("snowflake_step_runs", "project_id"),
    ("staged_backfill", "scene_id"),
    ("staged_backfill", "chapter_id"),
    ("story_characters", "project_id"),
    ("style_reference_evidences", "quote_id"),
    ("style_reference_findings", "run_id"),
    ("style_reference_profiles", "run_id"),
    ("style_reference_quotes", "paragraph_id"),
)

CORE_FOREIGN_KEYS: tuple[tuple[str, str, str, str, str], ...] = (
    ("chapter_goals", "project_id", "story_projects", "project_id", "fk_chapter_goals_project_id"),
    ("chapter_goals", "outline_plan_id", "outline_plans", "plan_id", "fk_chapter_goals_outline_plan_id"),
    ("scene_cards", "project_id", "story_projects", "project_id", "fk_scene_cards_project_id"),
    ("scene_cards", "outline_plan_id", "outline_plans", "plan_id", "fk_scene_cards_outline_plan_id"),
)

REPAIR_ONLY_INDEXES = {
    ("chapter_goals", "outline_plan_id"),
    ("scene_cards", "outline_plan_id"),
}


def _index_name(table_name: str, column_name: str) -> str:
    return f"ix_{table_name}_{column_name}"


def _repair_core_foreign_keys() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    missing: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)

    for table_name, child_column, parent_table, parent_column, constraint_name in CORE_FOREIGN_KEYS:
        exists = any(
            tuple(foreign_key.get("constrained_columns") or ()) == (child_column,)
            and str(foreign_key.get("referred_table") or "") == parent_table
            and tuple(foreign_key.get("referred_columns") or ()) == (parent_column,)
            for foreign_key in inspector.get_foreign_keys(table_name)
        )
        if exists:
            continue
        orphan_count = bind.execute(
            sa.text(
                f'SELECT COUNT(*) FROM "{table_name}" AS child '
                f'WHERE child."{child_column}" IS NOT NULL '
                f'AND NOT EXISTS (SELECT 1 FROM "{parent_table}" AS parent '
                f'WHERE parent."{parent_column}" = child."{child_column}")'
            )
        ).scalar_one()
        if orphan_count:
            raise RuntimeError(
                f"cannot add {constraint_name}: {table_name}.{child_column} "
                f"has {orphan_count} orphan row(s)"
            )
        missing[table_name].append(
            (constraint_name, child_column, parent_table, parent_column)
        )

    for table_name, constraints in missing.items():
        with op.batch_alter_table(table_name) as batch_op:
            for constraint_name, child_column, parent_table, parent_column in constraints:
                batch_op.create_foreign_key(
                    constraint_name,
                    parent_table,
                    [child_column],
                    [parent_column],
                )


def upgrade() -> None:
    _repair_core_foreign_keys()
    inspector = sa.inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())
    for table_name, column_name in FOREIGN_KEY_INDEXES:
        if table_name not in existing_tables:
            # Tables retired by later revisions never exist on databases that were
            # materialized from a newer ORM; there is nothing to index.
            continue
        indexes = inspector.get_indexes(table_name)
        if any((index.get("column_names") or [None])[0] == column_name for index in indexes):
            continue
        op.create_index(_index_name(table_name, column_name), table_name, [column_name], unique=False)
        inspector = sa.inspect(op.get_bind())


def downgrade() -> None:
    # Core foreign keys are a repair of the pre-0081 schema contract and are
    # intentionally retained on downgrade.  Removing them would reintroduce
    # deployment-path-dependent integrity drift.
    inspector = sa.inspect(op.get_bind())
    for table_name, column_name in reversed(FOREIGN_KEY_INDEXES):
        if (table_name, column_name) in REPAIR_ONLY_INDEXES:
            continue
        name = _index_name(table_name, column_name)
        if any(index.get("name") == name for index in inspector.get_indexes(table_name)):
            op.drop_index(name, table_name=table_name)
            inspector = sa.inspect(op.get_bind())

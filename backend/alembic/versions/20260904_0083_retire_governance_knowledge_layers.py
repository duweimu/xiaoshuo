"""Retire the outcome-governance, knowledge-promotion, long-form tower and advisory-overlay tables.

Revision ID: 20260904_0083
Revises: 20260818_0082
Create Date: 2026-09-04

The single-author workbench no longer ships the blind-evaluation / hidden-benchmark
policy engine, the review-promotion knowledge layer (style rules, world rules,
vector alias registry, reindex/verify jobs), the long-form control tower (anchors,
chapter contracts, audits, structure guidance), the retired advisory overlays
(foreshadow tracker, work profiles, staged backfill, project backtracks, scene
quality contracts, auto-rewrite runs, author structure candidates) or the
interop artifact store.  Their tables are dropped here; the migration is
irreversible because the product features that wrote these rows are gone.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260904_0083"
down_revision = "20260818_0082"
branch_labels = None
depends_on = None


# Children before parents so the drop order is valid even with FK checks on.
_DROPPED_TABLES = (
    # outcome governance / evaluation
    "quality_benchmark_results",
    "quality_benchmark_runs",
    "quality_value_observations",
    "quality_strategy_policies",
    "quality_benchmark_manifests",
    "evaluation_votes",
    "evaluation_pairs",
    "evaluation_experiments",
    # knowledge promotion / versioning
    "verify_jobs",
    "reindex_jobs",
    "reconcile_faults",
    "version_registry",
    "vector_alias_registry",
    "style_observations",
    "style_rules",
    "narrative_patterns",
    "banned_rule_clusters",
    "world_rules",
    "calibration_lines",
    # long-form control tower
    "chapter_audit_findings",
    "chapter_contracts",
    "longform_anchors",
    "longform_diagnostic_cards",
    "longform_structure_guidance",
    # retired advisory overlays and v1 surfaces
    "foreshadow_tracker",
    "work_profiles",
    "staged_backfill",
    "project_backtrack_items",
    "auto_rewrite_runs",
    "scene_quality_contracts",
    "author_structure_candidates",
    "interop_artifacts",
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    for table_name in _DROPPED_TABLES:
        if table_name in existing:
            op.drop_table(table_name)


def downgrade() -> None:
    # The features that owned these tables were removed; nothing can repopulate them.
    pass

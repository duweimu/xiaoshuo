"""ChapterState 运行时状态行的唯一 ensure 入口（审计 P-1）。

历史上章的运行时状态行由三条创建链路各自补建（approve_outline_plan /
POST /api/v1/chapters / chapter_runtime.sync_chapter），而目录冷启动链路
（catalog.create_chapter）不建 —— 归档/聚合段对 ``session.get(ChapterState, …)``
的裸解引用会在 QC 全通过后的最后一步 AttributeError。所有消费方一律经此
函数获取状态行，缺行即按统一默认值补建。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, ChapterState
from novel_system.services.errors import DomainError


def ensure_chapter_state(session: Session, chapter_id: str) -> ChapterState:
    """返回 chapter 的运行时状态行；缺行时按全库统一默认值补建并 flush。"""
    chapter_state = session.get(ChapterState, chapter_id)
    if chapter_state is None:
        if session.get(ChapterGoal, chapter_id) is None:
            raise DomainError("CHAPTER_NOT_FOUND", "chapter not found", status_code=404)
        chapter_state = ChapterState(
            chapter_id=chapter_id,
            current_phase="drafting",
            mid_aggregate_enabled_effective=0,
            aggregate_block_reason="none",
        )
        session.add(chapter_state)
        session.flush()
    return chapter_state


def chapter_state_snapshot(session: Session, chapter_id: str) -> dict:
    """只读投影章的运行时状态；缺行时返回默认值而不落库（GET 路径不得写状态）。"""
    if session.get(ChapterGoal, chapter_id) is None:
        raise DomainError("CHAPTER_NOT_FOUND", "chapter not found", status_code=404)
    chapter_state = session.get(ChapterState, chapter_id)
    return {
        "chapter_id": chapter_id,
        "current_phase": chapter_state.current_phase if chapter_state is not None else "drafting",
        "chapter_passed_scene_count": (
            int(chapter_state.chapter_passed_scene_count or 0) if chapter_state is not None else 0
        ),
        "chapter_backfill_pending_count": 0,
        "mid_aggregate_enabled_effective": (
            int(chapter_state.mid_aggregate_enabled_effective or 0) if chapter_state is not None else 0
        ),
        "aggregate_block_reason": (
            chapter_state.aggregate_block_reason if chapter_state is not None else "none"
        ),
        "manual_hold_reason": None,
        "last_interim_memory_row_id": (
            chapter_state.last_interim_memory_row_id if chapter_state is not None else None
        ),
        "last_final_memory_row_id": (
            chapter_state.last_final_memory_row_id if chapter_state is not None else None
        ),
        "staged_backfill": [],
    }

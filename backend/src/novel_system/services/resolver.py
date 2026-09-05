from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterMemory,
    RelationProfile,
    SceneCard,
    SceneMemory,
    VoiceProfile,
)


class Resolver:
    @staticmethod
    def _scoped_clause(model_cls, scene: SceneCard):
        return or_(
            model_cls.scope == "global",
            and_(model_cls.scope == "chapter", model_cls.scope_ref_id == scene.chapter_id),
            and_(model_cls.scope == "scene", model_cls.scope_ref_id == scene.scene_id),
        )

    def resolve_relation_profile_id(self, scene: SceneCard) -> str | None:
        if scene.resolved_relation_id:
            return scene.resolved_relation_id
        chars = list(dict.fromkeys(scene.onstage_chars_json or []))
        if len(chars) == 2:
            return f"REL_{chars[0]}_{chars[1]}"
        return None

    def resolve_voice_profile_id(self, scene: SceneCard) -> str | None:
        if scene.pov_character_id:
            return f"VOICE_{scene.pov_character_id}"
        return None

    def resolve_active_relation_profile(self, session: Session, scene: SceneCard) -> RelationProfile | None:
        relation_profile_id = self.resolve_relation_profile_id(scene)
        if relation_profile_id is None:
            return None
        return session.execute(
            select(RelationProfile)
            .where(
                RelationProfile.relation_profile_id == relation_profile_id,
                RelationProfile.active_flag == 1,
            )
            .order_by(RelationProfile.version.desc())
        ).scalars().first()

    def resolve_active_voice_profile(self, session: Session, scene: SceneCard) -> VoiceProfile | None:
        voice_profile_id = self.resolve_voice_profile_id(scene)
        if voice_profile_id is None:
            return None
        return session.execute(
            select(VoiceProfile)
            .where(
                VoiceProfile.voice_profile_id == voice_profile_id,
                VoiceProfile.active_flag == 1,
            )
            .order_by(VoiceProfile.version.desc())
        ).scalars().first()


    def resolve_scene_summary(self, session: Session, scene: SceneCard) -> SceneMemory | None:
        return session.execute(
            select(SceneMemory)
            .where(
                SceneMemory.scene_id == scene.scene_id,
                SceneMemory.active_flag == 1,
                SceneMemory.source_review_id.is_not(None),
            )
            .order_by(SceneMemory.created_at.desc(), SceneMemory.row_id.desc())
        ).scalars().first()

    def resolve_chapter_summary(self, session: Session, scene: SceneCard) -> ChapterMemory | None:
        return session.execute(
            select(ChapterMemory)
            .where(
                ChapterMemory.chapter_id == scene.chapter_id,
                ChapterMemory.active_flag == 1,
                ChapterMemory.source_review_id.is_not(None),
            )
            .order_by(ChapterMemory.created_at.desc(), ChapterMemory.row_id.desc())
        ).scalars().first()

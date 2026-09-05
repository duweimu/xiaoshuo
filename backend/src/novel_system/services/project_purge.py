"""Project purge planning: vector collections and indirectly owned rows.

The knowledge-promotion/versioning layer was retired, so a project only owns
its scene vector collection plus a few scope-referenced rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AuthorPreferenceProfile,
    RelationProfile,
    ReviewItem,
    SceneBundle,
    StyleReferenceInjectionBinding,
    VoiceProfile,
)
from novel_system.services.errors import DomainError
from novel_system.services.vector_store import VectorStore


@dataclass(frozen=True)
class ProjectPurgePlan:
    project_id: str
    chapter_ids: tuple[str, ...]
    scene_ids: tuple[str, ...]
    character_ids: tuple[str, ...]
    bundle_ids: tuple[str, ...]
    review_ids: tuple[str, ...]
    vector_collections: tuple[str, ...]

    @property
    def scope_ref_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    self.project_id,
                    *self.chapter_ids,
                    *self.scene_ids,
                    *self.character_ids,
                )
            )
        )


def build_project_purge_plan(
    session: Session,
    *,
    project_id: str,
    chapter_ids: list[str],
    scene_ids: list[str],
    character_ids: list[str],
) -> ProjectPurgePlan:
    chapters = tuple(dict.fromkeys(chapter_ids))
    scenes = tuple(dict.fromkeys(scene_ids))
    characters = tuple(dict.fromkeys(character_ids))
    refs = tuple(dict.fromkeys((project_id, *chapters, *scenes, *characters)))

    bundle_ids = tuple(
        session.execute(
            select(SceneBundle.bundle_id).where(
                SceneBundle.scene_id.in_(scenes or ("",))
                | SceneBundle.chapter_id.in_(chapters or ("",))
            )
        ).scalars().all()
    )
    review_ids = tuple(
        session.execute(
            select(ReviewItem.review_id).where(
                (ReviewItem.project_id == project_id)
                | ReviewItem.scene_id.in_(scenes or ("",))
                | ReviewItem.chapter_id.in_(chapters or ("",))
            )
        ).scalars().all()
    )

    collection_names: set[str] = {f"scenes_{project_id}"}

    return ProjectPurgePlan(
        project_id=project_id,
        chapter_ids=chapters,
        scene_ids=scenes,
        character_ids=characters,
        bundle_ids=tuple(dict.fromkeys(bundle_ids)),
        review_ids=tuple(dict.fromkeys(review_ids)),
        vector_collections=tuple(sorted(collection_names)),
    )


def purge_project_vectors(plan: ProjectPurgePlan, vector_store: VectorStore) -> tuple[str, ...]:
    """Delete and verify every known external vector collection.

    External deletion deliberately runs before the database transaction removes
    the ownership metadata.  A database failure can rebuild vectors from retained
    rows; deleting the database first would make an external privacy leak
    impossible to discover or repair deterministically.
    """

    deleted: list[str] = []
    for collection_name in plan.vector_collections:
        try:
            vector_store.delete_collection(collection_name)
            collection_remains = vector_store.collection_exists(collection_name)
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError(
                "PROJECT_VECTOR_PURGE_FAILED",
                "project vector collection could not be permanently deleted",
                status_code=503,
                details={
                    "project_id": plan.project_id,
                    "collection_name": collection_name,
                    "retryable": True,
                },
            ) from exc
        if collection_remains:
            raise DomainError(
                "PROJECT_VECTOR_PURGE_FAILED",
                "project vector collection still exists after deletion",
                status_code=503,
                details={
                    "project_id": plan.project_id,
                    "collection_name": collection_name,
                    "retryable": True,
                },
            )
        deleted.append(collection_name)
    return tuple(deleted)


def delete_indirect_project_rows(session: Session, plan: ProjectPurgePlan) -> dict[str, int]:
    """Delete project-owned rows that cannot be discovered from ``project_id``."""

    refs = plan.scope_ref_ids
    counts: dict[str, int] = {}

    def execute(stmt: Any, key: str) -> None:
        result = session.execute(stmt)
        counts[key] = counts.get(key, 0) + max(int(result.rowcount or 0), 0)

    execute(
        delete(StyleReferenceInjectionBinding).where(
            StyleReferenceInjectionBinding.scope_ref_id.in_(refs)
        ),
        "style_reference_injection_bindings",
    )
    execute(
        delete(AuthorPreferenceProfile).where(
            AuthorPreferenceProfile.scope_ref_id.in_(refs)
        ),
        "author_preference_profiles",
    )
    if plan.character_ids:
        execute(
            delete(VoiceProfile).where(VoiceProfile.character_id.in_(plan.character_ids)),
            "voice_profiles",
        )
        execute(
            delete(RelationProfile).where(
                RelationProfile.left_character_id.in_(plan.character_ids)
                | RelationProfile.right_character_id.in_(plan.character_ids)
            ),
            "relation_profiles",
        )

    return counts

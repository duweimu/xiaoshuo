from __future__ import annotations

import hashlib
import json
import difflib
import re
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AuthorDraft,
    AuthorDraftEvent,
    AuthorDraftProposal,
    AuthorDraftRevision,
    AuthorPreferenceProfile,
    ChapterGoal,
    ChapterMemory,
    ChapterState,
    FinalScene,
    PassagePatchCandidate,
    ReviewItem,
    SceneCard,
    SceneRunState,
    StoryProject,
)
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.canonical_manuscripts import canonicalize_author_text
from novel_system.services.chapter_approval import require_author_target_mutation_allowed
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import canonical_json
from novel_system.services.llm_accounting import LLMCallContext
from novel_system.services.llm_fail_closed import raise_llm_domain_error
from novel_system.services.llm_task_runner import (
    LLMNodeExecutionError,
    LLMNodeRunner,
    current_llm_execution_id,
)
from novel_system.services.manuscript_html import sanitize_manuscript_html
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.snowflake_steps import get_step_definition
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService
from novel_system.services.writer_briefs import (
    empty_chapter_writer_brief,
    empty_scene_writer_brief,
    normalize_chapter_writer_brief,
    normalize_scene_writer_brief,
)
from novel_system.services.writing_stats import WritingStatsService, count_words

AUTHOR_DRAFT_EVENT_TYPES = {
    "created",
    "edited",
    "candidate_inserted",
    "candidate_saved",
    "candidate_rejected",
    "proposal_applied",
    "proposal_rejected",
}

_RUNTIME_FINAL_UNAVAILABLE = object()

# 发现稿「提取结构」允许导入的雪花步骤；提示词契约、归一化与错误提示共用这一份。
PROJECT_DISCOVERY_STEP_KEYS = (
    "book_brief",
    "one_sentence_summary",
    "one_paragraph_summary",
    "scene_list",
    "scene_details",
)
# 骨架里不给模型看的字段：系统默认策略，不是要从稿子里提取的东西。
_PROJECT_STEP_SKELETON_OMIT = {"book_brief": {"safety_rules"}}

DESK_DEFAULT_MODE = "write_first"
AUTHOR_PROPOSAL_TRIAD = ("structure_candidate", "passage_candidate", "language_candidate")
AUTHOR_PROPOSAL_APPLY_MODES = {"replace", "append", "new_version", "local_patch", "range_replace", "paragraph_replace"}
AUTHOR_PROPOSAL_KIND_APPLY_MODES = {
    "structure_note": "append",
    "whole_draft": "replace",
    "local_patch": "local_patch",
    "dialogue_pass": "local_patch",
    "language_pass": "local_patch",
    "continuation": "append",
    "near_final_rewrite": "replace",
}
AUTHOR_PROPOSAL_MODE_TRIADS = {
    "explore": (("structure_candidate", "structure_note"), ("continuation", "continuation"), ("passage_candidate", "local_patch")),
    "structure": (("structure_candidate", "structure_note"), ("whole_draft", "whole_draft"), ("local_patch", "local_patch")),
    "dialogue": (("dialogue_pass", "dialogue_pass"), ("local_patch", "local_patch"), ("language_pass", "language_pass")),
    "language": (("language_pass", "language_pass"), ("local_patch", "local_patch"), ("near_final_rewrite", "near_final_rewrite")),
    "rewrite": (("whole_draft", "whole_draft"), ("local_patch", "local_patch"), ("structure_candidate", "structure_note")),
    "continuation": (("continuation", "continuation"), ("structure_candidate", "structure_note"), ("language_pass", "language_pass")),
    # Writer-room continuation tray: three insertable continuations generated
    # under one durable idempotency intent, with distinct prompt directions.
    "continuation_variants": (("continuation", "continuation"), ("continuation", "continuation"), ("continuation", "continuation")),
    "near_final": (("near_final_rewrite", "near_final_rewrite"), ("language_pass", "language_pass"), ("dialogue_pass", "dialogue_pass")),
    "acceptance": (("near_final_rewrite", "near_final_rewrite"), ("language_pass", "language_pass"), ("structure_candidate", "structure_note")),
    "daily": (("structure_candidate", "structure_note"), ("passage_candidate", "local_patch"), ("language_candidate", "language_pass")),
}
CONTINUATION_VARIANT_DIRECTIONS = (
    ("action", "候选方向：优先用动作推进下一拍，避免解释和总结。"),
    ("relationship", "候选方向：优先增加人物关系压力，用反应、停顿或选择推进。"),
    ("suspense", "候选方向：优先释放一个新信息或悬念钩子，但不要越过下一拍。"),
)


class AuthorDraftService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.lifecycle = AuthorLifecycleService(session)

    def _llm_context_for_target(
        self,
        runner: LLMNodeRunner,
        target: dict[str, Any],
        *,
        node_id: str,
        step: str,
        execution_step_key: str,
    ) -> LLMCallContext:
        execution_id = current_llm_execution_id()
        common = {
            "scope_id": target["object_id"],
            "project_id": target["project_id"],
            "node_id": node_id,
            "step": step,
            "execution_id": execution_id,
            "execution_step_key": execution_step_key if execution_id is not None else None,
            "provider_execution_mode": runner.provider_execution_mode,
        }
        if target["object_type"] == "scene":
            return LLMCallContext(
                scope_type="scene",
                scene_id=target["scene_id"],
                chapter_id=target["chapter_id"],
                **common,
            )
        if target["object_type"] == "chapter":
            return LLMCallContext(
                scope_type="chapter",
                chapter_id=target["chapter_id"],
                **common,
            )
        return LLMCallContext(scope_type="project", **common)

    def current(self, object_type: str, object_id: str) -> dict[str, Any]:
        self._require_target(object_type, object_id)
        draft = self._current_row(object_type, object_id)
        if draft is None:
            return {"draft": None}
        return self._draft_response(draft)

    def ensure(self, object_type: str, object_id: str, *, actor_ref: str = "operator") -> dict[str, Any]:
        self._require_target(object_type, object_id)
        current = self._current_row(object_type, object_id)
        if current is not None:
            return self._draft_response(current)
        try:
            source = self._source_for_target(object_type, object_id)
        except DomainError as exc:
            if exc.code != "AUTHOR_DRAFT_SOURCE_MISSING":
                raise
            source = self._blank_source_for_target(object_type, object_id)
        draft = self._create_draft_row(object_type, object_id, source=source, actor_ref=actor_ref)
        return self._draft_response(draft)

    def ensure_blank(self, object_type: str, object_id: str, *, actor_ref: str = "operator") -> dict[str, Any]:
        self._require_target(object_type, object_id)
        current = self._current_row(object_type, object_id)
        if current is not None:
            return self._draft_response(current)
        source = self._blank_source_for_target(object_type, object_id)
        draft = self._create_draft_row(object_type, object_id, source=source, actor_ref=actor_ref)
        return self._draft_response(draft)


    def _create_draft_row(
        self,
        object_type: str,
        object_id: str,
        *,
        source: dict[str, str],
        actor_ref: str,
        event_payload: dict[str, Any] | None = None,
    ) -> AuthorDraft:
        draft = AuthorDraft(
            draft_id=f"author_draft_{object_type}_{object_id}_{uuid.uuid4().hex[:10]}",
            object_type=object_type,
            object_id=object_id,
            source_text_ref=source["source_text_ref"],
            content=sanitize_manuscript_html(source["content"]),
            revision_no=1,
            status="current",
            created_by=actor_ref or "author_draft",
            updated_by=actor_ref or "author_draft",
        )
        self.session.add(draft)
        self.session.flush()
        self._add_event(
            draft,
            event_type="created",
            actor_ref=actor_ref,
            payload={"source_text_ref": source["source_text_ref"], **(event_payload or {})},
        )
        self._snapshot_revision(draft, actor_ref=actor_ref, origin="created")
        self.session.flush()
        return draft

    def _resolve_project_id(self, object_type: str, object_id: str) -> str | None:
        if object_type == "project":
            return object_id
        if object_type == "chapter":
            chapter = self.session.get(ChapterGoal, object_id)
            return chapter.project_id if chapter else None
        if object_type == "scene":
            scene = self.session.get(SceneCard, object_id)
            if scene is None:
                return None
            if scene.project_id:
                return scene.project_id
            chapter = self.session.get(ChapterGoal, scene.chapter_id)
            return chapter.project_id if chapter else None
        return None

    def save(self, draft_id: str, payload: dict[str, Any], *, actor_ref: str = "operator") -> dict[str, Any]:
        draft = self._require_draft(draft_id)
        previous_content = draft.content or ""
        base_revision_no = payload.get("base_revision_no")
        if int(base_revision_no or 0) != int(draft.revision_no):
            raise DomainError(
                "AUTHOR_DRAFT_CONFLICT",
                "author draft has changed; refresh before saving",
                status_code=409,
                details={"current_revision_no": draft.revision_no},
            )
        content = payload.get("content")
        if not isinstance(content, str):
            raise DomainError("AUTHOR_DRAFT_INVALID", "content must be a string", status_code=400)
        content = sanitize_manuscript_html(content)
        content_changed = content != previous_content
        require_author_target_mutation_allowed(
            self.session,
            object_type=draft.object_type,
            object_id=draft.object_id,
            changed_fields=["author_draft.content"] if content_changed else [],
            operation="author_draft.save",
        )
        if not content_changed:
            response = self._draft_response(draft)
            response["changed"] = False
            return response
        new_words = count_words(content)
        words_delta = new_words - count_words(previous_content)
        next_revision_no = int(draft.revision_no) + 1
        updated = self.session.execute(
            update(AuthorDraft)
            .where(
                AuthorDraft.draft_id == draft_id,
                AuthorDraft.revision_no == int(base_revision_no),
                AuthorDraft.status == "current",
            )
            .values(
                content=content,
                revision_no=next_revision_no,
                updated_by=actor_ref or draft.updated_by,
            )
            .execution_options(synchronize_session=False)
        )
        if updated.rowcount != 1:
            # The pre-check above gives fast feedback in the common case; this
            # database compare-and-swap is the actual concurrency guarantee.
            self.session.rollback()
            current = self.session.get(AuthorDraft, draft_id)
            raise DomainError(
                "AUTHOR_DRAFT_CONFLICT",
                "author draft has changed; refresh before saving",
                status_code=409,
                details={
                    "current_revision_no": current.revision_no if current else None,
                    "current_status": current.status if current else None,
                },
            )
        self.session.expire(draft)
        self.session.refresh(draft)
        # FE-ALIGN P2 字数埋点（D2）：保存主路径上报 words_delta，统计按 project 聚合。
        project_id = self._resolve_project_id(draft.object_type, draft.object_id)
        if words_delta and project_id:
            WritingStatsService(self.session).record_words_delta(project_id, words_delta)
        # FE-ALIGN P3 目录 rollup：场景正文字数落 SceneCard.words_current，
        # 响应带最新 rollup（前端不再自算 delta）。
        words_rollup: dict[str, Any] | None = None
        if draft.object_type == "scene":
            scene = self.session.get(SceneCard, draft.object_id)
            if scene is not None:
                scene.words_current = new_words
                from novel_system.services.catalog import CatalogService

                words_rollup = CatalogService(self.session).words_rollup(scene)
                if project_id:
                    words_rollup["words_total"] = WritingStatsService(self.session).stats_payload(project_id)["words_total"]
        self._add_event(
            draft,
            event_type="edited",
            actor_ref=actor_ref,
            patch_id=_optional_text(payload, "patch_id"),
            revision_id=_optional_text(payload, "revision_id"),
            option_id=_optional_text(payload, "option_id"),
            note=_optional_text(payload, "note"),
            payload={"base_revision_no": base_revision_no, "revision_no": draft.revision_no},
        )
        self._record_direct_edit_preferences(
            draft,
            before_text=previous_content,
            after_text=content,
            actor_ref=actor_ref,
        )
        self._snapshot_revision(draft, actor_ref=actor_ref, origin="edited")
        self.session.flush()
        response = self._draft_response(draft)
        response["changed"] = True
        if words_rollup is not None:
            response["words_rollup"] = words_rollup
        return response


    def proposals(self, draft_id: str) -> dict[str, Any]:
        draft = self._require_draft(draft_id)
        rows = self.session.execute(
            select(AuthorDraftProposal)
            .where(AuthorDraftProposal.draft_id == draft.draft_id)
            .order_by(AuthorDraftProposal.created_at.desc(), AuthorDraftProposal.proposal_id.desc())
        ).scalars().all()
        return {
            "draft_id": draft.draft_id,
            "items": [self.serialize_proposal(row) for row in rows],
        }

    def generate_proposal(
        self,
        draft_id: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        draft = self._require_draft(draft_id)
        request_payload = payload or {}
        proposal_type = _optional_text(request_payload, "proposal_type") or (
            "scene_draft" if draft.object_type == "scene" else "chapter_draft"
        )
        instruction = _optional_text(request_payload, "instruction")
        target_range = request_payload.get("target_range") if isinstance(request_payload.get("target_range"), dict) else None
        replacement_text = _optional_text(request_payload, "replacement_text")
        proposal_kind = _optional_text(request_payload, "proposal_kind") or _proposal_kind_from_type(proposal_type)
        source_evaluation_id = _optional_text(request_payload, "source_evaluation_id")
        target = self._target_payload(draft.object_type, draft.object_id)
        proposal = self._create_proposal(
            draft,
            target=target,
            proposal_type=proposal_type,
            instruction=instruction,
            proposal_source=_optional_text(request_payload, "proposal_source") or "single_request",
            proposal_kind=proposal_kind,
            target_range=target_range,
            replacement_text=replacement_text,
            source_evaluation_id=source_evaluation_id,
            actor_ref=actor_ref,
        )
        self.session.flush()
        return {"proposal": self.serialize_proposal(proposal)}

    def generate_proposal_set(
        self,
        draft_id: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        draft = self._require_draft(draft_id)
        request_payload = payload or {}
        requested_mode = _optional_text(request_payload, "mode")
        mode = _proposal_generation_mode(requested_mode)
        proposal_source = f"author_cockpit_{mode}" if requested_mode else "author_cockpit_triad"
        instruction = _optional_text(request_payload, "instruction")
        target_range = request_payload.get("target_range") if isinstance(request_payload.get("target_range"), dict) else None
        source_evaluation_id = _optional_text(request_payload, "source_evaluation_id")
        target = self._target_payload(draft.object_type, draft.object_id)
        proposals: list[AuthorDraftProposal] = []
        for index, (proposal_type, proposal_kind) in enumerate(_proposal_mode_triads(mode)):
            effective_source = proposal_source
            effective_instruction = instruction
            if mode == "continuation_variants":
                slot, direction = CONTINUATION_VARIANT_DIRECTIONS[index]
                effective_source = f"writer_room_continuation_variants:{slot}"
                effective_instruction = "\n".join(
                    part for part in (instruction, direction) if part
                )
            proposals.append(self._create_proposal(
                draft,
                target=target,
                proposal_type=proposal_type,
                instruction=effective_instruction,
                proposal_source=effective_source,
                proposal_kind=proposal_kind,
                target_range=target_range,
                replacement_text=None,
                source_evaluation_id=source_evaluation_id,
                actor_ref=actor_ref,
            ))
        self.session.flush()
        return {"draft_id": draft.draft_id, "mode": mode, "proposals": [self.serialize_proposal(row) for row in proposals]}

    def proposal_diff(self, draft_id: str, proposal_id: str) -> dict[str, Any]:
        draft = self._require_draft(draft_id)
        proposal = self._require_proposal(proposal_id)
        self._validate_proposal_for_draft(draft, proposal)
        before_text = draft.content or ""
        after_text = _apply_proposal_to_content(before_text, proposal, _apply_mode_for_proposal(proposal))
        merge_status = "clean" if _proposal_hash_matches(proposal, before_text) else "conflict"
        return {
            "draft_id": draft.draft_id,
            "proposal_id": proposal.proposal_id,
            "object_type": draft.object_type,
            "object_id": draft.object_id,
            "proposal_kind": proposal.proposal_kind or _proposal_kind_from_type(proposal.proposal_type),
            "proposal_type": proposal.proposal_type,
            "target_range": proposal.target_range_json or None,
            "before_text_hash": proposal.before_text_hash,
            "current_text_hash": _text_hash(before_text),
            "merge_status": merge_status,
            "before_text": before_text,
            "after_text": after_text,
            "replacement_text": proposal.replacement_text or proposal.content or "",
            "source_evaluation_id": proposal.source_evaluation_id,
            "source_llm_call_id": proposal.source_llm_call_id,
            "rationale": proposal.rationale,
        }

    def apply_proposal_to_draft(
        self,
        draft_id: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        draft = self._require_draft(draft_id)
        proposal_id = _required_text(payload or {}, "proposal_id")
        proposal = self._require_proposal(proposal_id)
        self._validate_proposal_for_draft(draft, proposal)
        if proposal.status != "candidate":
            raise DomainError("AUTHOR_DRAFT_PROPOSAL_CLOSED", "author draft proposal is not open", status_code=409)
        current = draft.content or ""
        if not _proposal_hash_matches(proposal, current):
            proposal.merge_status = "conflict"
            self.session.flush()
            raise DomainError(
                "AUTHOR_DRAFT_PROPOSAL_CONFLICT",
                "author draft changed after this proposal was created; review the diff before applying",
                status_code=409,
                details={
                    "draft_id": draft.draft_id,
                    "proposal_id": proposal.proposal_id,
                    "before_text_hash": proposal.before_text_hash,
                    "current_text_hash": _text_hash(current),
                },
            )
        request_payload = payload or {}
        apply_mode = _normalize_apply_mode(_optional_text(request_payload, "apply_mode"), proposal)
        if apply_mode not in AUTHOR_PROPOSAL_APPLY_MODES:
            raise DomainError("AUTHOR_DRAFT_PROPOSAL_APPLY_MODE_INVALID", "unsupported proposal apply_mode", status_code=400)
        next_content = sanitize_manuscript_html(_apply_proposal_to_content(current, proposal, apply_mode))
        require_author_target_mutation_allowed(
            self.session,
            object_type=draft.object_type,
            object_id=draft.object_id,
            changed_fields=["author_draft.revision_no", "proposal.status"]
            + (["author_draft.content"] if next_content != current else []),
            operation="author_draft.apply_proposal",
        )
        draft.content = next_content
        draft.revision_no += 1
        draft.updated_by = actor_ref or draft.updated_by
        proposal.status = "accepted"
        proposal.merge_status = "applied"
        proposal.author_decision_note = _optional_text(request_payload, "note") or proposal.author_decision_note
        decision_reason = _optional_text(request_payload, "decision_reason")
        self._add_event(
            draft,
            event_type="proposal_applied",
            actor_ref=actor_ref,
            revision_id=proposal.proposal_id,
            note=proposal.author_decision_note,
            payload={
                "proposal_id": proposal.proposal_id,
                "proposal_type": proposal.proposal_type,
                "proposal_kind": proposal.proposal_kind,
                "proposal_source": proposal.proposal_source,
                "apply_mode": apply_mode,
                "target_range": proposal.target_range_json,
                "affected_excerpt": _target_excerpt(proposal) or _short_excerpt(proposal.replacement_text or proposal.content),
                "decision_reason": decision_reason or "",
                "revision_no": draft.revision_no,
            },
        )
        self._refresh_proposal_preference_profile(proposal, actor_ref=actor_ref, decision_reason=decision_reason)
        self._snapshot_revision(draft, actor_ref=actor_ref, origin="proposal_applied")
        self.session.flush()
        return {"proposal": self.serialize_proposal(proposal), **self._draft_response(draft)}

    def apply_proposal(
        self,
        proposal_id: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        proposal = self._require_proposal(proposal_id)
        if proposal.status != "candidate":
            raise DomainError("AUTHOR_DRAFT_PROPOSAL_CLOSED", "author draft proposal is not open", status_code=409)
        draft = self._require_draft(proposal.draft_id)
        if draft.object_type != proposal.object_type or draft.object_id != proposal.object_id:
            raise DomainError("AUTHOR_DRAFT_PROPOSAL_TARGET_MISMATCH", "proposal target does not match author draft", status_code=409)
        request_payload = payload or {}
        apply_mode = _normalize_apply_mode(_optional_text(request_payload, "apply_mode"), proposal)
        if apply_mode not in AUTHOR_PROPOSAL_APPLY_MODES:
            raise DomainError("AUTHOR_DRAFT_PROPOSAL_APPLY_MODE_INVALID", "unsupported proposal apply_mode", status_code=400)
        decision_reason = _optional_text(request_payload, "decision_reason")
        affected_excerpt = _optional_text(request_payload, "affected_excerpt")

        current_content = draft.content or ""
        next_content = sanitize_manuscript_html(_apply_proposal_to_content(current_content, proposal, apply_mode))
        require_author_target_mutation_allowed(
            self.session,
            object_type=draft.object_type,
            object_id=draft.object_id,
            changed_fields=["author_draft.revision_no", "proposal.status"]
            + (["author_draft.content"] if next_content != current_content else []),
            operation="author_draft.apply_proposal",
        )
        draft.content = next_content
        draft.revision_no += 1
        draft.updated_by = actor_ref or draft.updated_by
        proposal.status = "accepted"
        proposal.merge_status = "applied"
        proposal.author_decision_note = _optional_text(request_payload, "note") or proposal.author_decision_note
        self._add_event(
            draft,
            event_type="proposal_applied",
            actor_ref=actor_ref,
            revision_id=proposal.proposal_id,
            note=proposal.author_decision_note,
            payload={
                "proposal_id": proposal.proposal_id,
                "proposal_type": proposal.proposal_type,
                "proposal_kind": proposal.proposal_kind,
                "proposal_source": proposal.proposal_source,
                "apply_mode": apply_mode,
                "affected_excerpt": affected_excerpt or _short_excerpt(proposal.content),
                "decision_reason": decision_reason or "",
                "revision_no": draft.revision_no,
            },
        )
        self._refresh_proposal_preference_profile(proposal, actor_ref=actor_ref, decision_reason=decision_reason)
        self._snapshot_revision(draft, actor_ref=actor_ref, origin="proposal_applied")
        self.session.flush()
        return {"proposal": self.serialize_proposal(proposal), **self._draft_response(draft)}

    def reject_proposal(
        self,
        proposal_id: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        proposal = self._require_proposal(proposal_id)
        if proposal.status != "candidate":
            raise DomainError("AUTHOR_DRAFT_PROPOSAL_CLOSED", "author draft proposal is not open", status_code=409)
        draft = self._require_draft(proposal.draft_id)
        note = _optional_text(payload or {}, "note")
        decision_reason = _optional_text(payload or {}, "decision_reason")
        rejected_ai_trace = _optional_text(payload or {}, "rejected_ai_trace")
        proposal.status = "rejected"
        proposal.merge_status = "rejected"
        proposal.author_decision_note = note or proposal.author_decision_note
        self._add_event(
            draft,
            event_type="proposal_rejected",
            actor_ref=actor_ref,
            revision_id=proposal.proposal_id,
            note=proposal.author_decision_note,
            payload={
                "proposal_id": proposal.proposal_id,
                "proposal_type": proposal.proposal_type,
                "proposal_source": proposal.proposal_source,
                "decision_reason": decision_reason or "",
                "rejected_ai_trace": rejected_ai_trace or "",
                "revision_no": draft.revision_no,
            },
        )
        self._refresh_proposal_preference_profile(
            proposal,
            actor_ref=actor_ref,
            decision_reason=decision_reason,
            rejected_ai_trace=rejected_ai_trace,
        )
        self.session.flush()
        return {"proposal": self.serialize_proposal(proposal), **self._draft_response(draft)}

    def _create_proposal(
        self,
        draft: AuthorDraft,
        *,
        target: dict[str, Any],
        proposal_type: str,
        instruction: str | None,
        proposal_source: str,
        proposal_kind: str,
        target_range: dict[str, Any] | None,
        replacement_text: str | None,
        source_evaluation_id: str | None,
        actor_ref: str,
    ) -> AuthorDraftProposal:
        source_llm_call_id = None
        generated_rationale: str | None = None
        if replacement_text:
            proposal_content = _apply_patch_preview(draft.content or "", target_range or {}, replacement_text)
        else:
            generated = self._generate_proposal_content(
                draft,
                target=target,
                proposal_type=proposal_type,
                instruction=instruction,
                proposal_kind=proposal_kind,
                target_range=target_range,
            )
            proposal_content = generated["content"]
            generated_rationale = generated.get("rationale")
            source_llm_call_id = generated.get("source_llm_call_id")
        proposal = AuthorDraftProposal(
            proposal_id=f"author_draft_proposal_{draft.object_type}_{draft.object_id}_{uuid.uuid4().hex[:10]}",
            draft_id=draft.draft_id,
            object_type=draft.object_type,
            object_id=draft.object_id,
            proposal_type=proposal_type,
            proposal_source=proposal_source,
            content=proposal_content,
            rationale=generated_rationale or _proposal_rationale(target=target, proposal_type=proposal_type, instruction=instruction),
            source_llm_call_id=source_llm_call_id,
            target_range_json=target_range,
            before_text_hash=_text_hash(draft.content or ""),
            replacement_text=replacement_text,
            proposal_kind=proposal_kind,
            source_evaluation_id=source_evaluation_id,
            merge_status="pending",
            status="candidate",
            created_by=actor_ref or "author_draft_proposal",
        )
        self.session.add(proposal)
        return proposal

    def _generate_proposal_content(
        self,
        draft: AuthorDraft,
        *,
        target: dict[str, Any],
        proposal_type: str,
        instruction: str | None,
        proposal_kind: str,
        target_range: dict[str, Any] | None,
    ) -> dict[str, str | None]:
        preference_summary = self._proposal_preference_summary(draft, target)
        target_for_prompt = {
            **target,
            "author_preference_summary": preference_summary,
            "proposal_instruction": instruction or "",
        }
        snapshot = _proposal_generate_snapshot(
            draft,
            target=target_for_prompt,
            proposal_type=proposal_type,
            proposal_kind=proposal_kind,
            instruction=instruction,
            target_range=target_range,
            preference_summary=preference_summary,
        )
        prompt = PromptBuilder().build(snapshot, "author_proposal_generate")
        bundle_hash = hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()
        runner = LLMNodeRunner(self.session)
        execution_step_key = f"author_proposal_generate:{draft.draft_id}:{proposal_type}"
        context = self._llm_context_for_target(
            runner,
            target,
            node_id="author_proposal_generate",
            step="author_proposal_generate",
            execution_step_key=execution_step_key,
        )
        try:
            node_result = runner.run(
                scene_id=target.get("scene_id") or target.get("project_id") or draft.object_id,
                chapter_id=target.get("chapter_id") or target.get("project_id") or draft.object_id,
                bundle_id=f"author_draft:{draft.draft_id}:proposal:{proposal_type}",
                bundle_hash=bundle_hash,
                node_id="author_proposal_generate",
                step="author_proposal_generate",
                prompt=prompt,
                user_prompt=_proposal_generate_user_prompt(prompt["user_prompt"], draft=draft, target=target_for_prompt),
                source_draft_row_id=draft.draft_id,
                source_draft_content=draft.content,
                execution_step_key=execution_step_key,
                context=context,
            )
        except LLMNodeExecutionError as exc:
            raise_llm_domain_error(
                exc,
                capability_code="AUTHOR_PROPOSAL_LLM_NOT_CONFIGURED",
                failure_code="AUTHOR_PROPOSAL_GENERATE_FAILED",
                operation="author proposal generation",
                node_id="author_proposal_generate",
                next_action="configure_author_proposal_route_and_retry",
            )
        normalized = _normalize_proposal_payload(
            node_result.response.structured_output,
            draft=draft,
            target=target,
            proposal_type=proposal_type,
            instruction=instruction,
        )
        normalized["source_llm_call_id"] = node_result.llm_call_id
        return normalized


    # FE-ALIGN F2 修订历史：每次 revision_no 推进存完整内容快照，支撑成稿中心版本对比。
    def _snapshot_revision(self, draft: AuthorDraft, *, actor_ref: str, origin: str) -> None:
        existing = self.session.execute(
            select(AuthorDraftRevision.draft_revision_id)
            .where(AuthorDraftRevision.draft_id == draft.draft_id)
            .where(AuthorDraftRevision.revision_no == int(draft.revision_no))
        ).scalar_one_or_none()
        if existing is not None:
            return
        self.session.add(
            AuthorDraftRevision(
                draft_revision_id=f"author_draft_rev_{uuid.uuid4().hex[:12]}",
                draft_id=draft.draft_id,
                revision_no=int(draft.revision_no),
                content=draft.content or "",
                words=count_words(draft.content or ""),
                origin=origin,
                created_by=actor_ref or "author_draft",
            )
        )

    def revisions(self, draft_id: str) -> dict[str, Any]:
        draft = self._require_draft(draft_id)
        rows = self.session.execute(
            select(AuthorDraftRevision)
            .where(AuthorDraftRevision.draft_id == draft.draft_id)
            .order_by(AuthorDraftRevision.revision_no.desc())
        ).scalars().all()
        return {
            "draft_id": draft.draft_id,
            "object_type": draft.object_type,
            "object_id": draft.object_id,
            "revision_no": draft.revision_no,
            "items": [
                {
                    "revision_no": row.revision_no,
                    "words": row.words,
                    "origin": row.origin,
                    "created_by": row.created_by,
                    "created_at": row.created_at,
                }
                for row in rows
            ],
        }

    def revision(self, draft_id: str, revision_no: int) -> dict[str, Any]:
        draft = self._require_draft(draft_id)
        row = self.session.execute(
            select(AuthorDraftRevision)
            .where(AuthorDraftRevision.draft_id == draft.draft_id)
            .where(AuthorDraftRevision.revision_no == int(revision_no))
        ).scalar_one_or_none()
        if row is None:
            raise DomainError(
                "AUTHOR_DRAFT_REVISION_NOT_FOUND",
                "author draft revision not found",
                status_code=404,
                details={"draft_id": draft.draft_id, "revision_no": revision_no},
            )
        return {
            "revision": {
                "draft_id": draft.draft_id,
                "revision_no": row.revision_no,
                "content": sanitize_manuscript_html(row.content),
                "words": row.words,
                "origin": row.origin,
                "created_by": row.created_by,
                "created_at": row.created_at,
            }
        }

    def _draft_response(self, draft: AuthorDraft) -> dict[str, Any]:
        desk_context = self._desk_context(draft)
        runtime_ref = desk_context.get("runtime_final_ref")
        runtime_final_id = (
            runtime_ref.removeprefix("final_scene:")
            if isinstance(runtime_ref, str) and runtime_ref.startswith("final_scene:")
            else None
        )
        serialized = self.serialize_draft(
            draft,
            current_final_scene_row_id=(
                runtime_final_id if draft.object_type == "scene" else _RUNTIME_FINAL_UNAVAILABLE
            ),
        )
        return {"draft": serialized, **desk_context}

    def _desk_context(self, draft: AuthorDraft) -> dict[str, Any]:
        runtime_final_ref = None
        aggregate_ref = None
        try:
            source = self._source_for_target(draft.object_type, draft.object_id)
            if source["source_text_ref"].startswith("final_scene:"):
                runtime_final_ref = source["source_text_ref"]
            elif source["source_text_ref"].startswith("chapter_memory:"):
                aggregate_ref = source["source_text_ref"]
        except DomainError:
            pass
        if draft.object_type == "scene":
            scene = self.lifecycle.require_active_scene(draft.object_id)
            aggregate = self._chapter_aggregate(scene.chapter_id)
            if aggregate is not None:
                aggregate_ref = f"chapter_memory:{aggregate.row_id}"
        elif draft.object_type == "chapter":
            aggregate = self._chapter_aggregate(draft.object_id)
            if aggregate is not None:
                aggregate_ref = f"chapter_memory:{aggregate.row_id}"
        preference = self.session.execute(
            select(AuthorPreferenceProfile)
            .where(AuthorPreferenceProfile.scope_type == "global", AuthorPreferenceProfile.scope_ref_id == "global")
            .order_by(AuthorPreferenceProfile.updated_at.desc(), AuthorPreferenceProfile.profile_id.desc())
        ).scalars().first()
        return {
            "draft_mode": draft.object_type,
            "desk_mode": DESK_DEFAULT_MODE,
            "source_layer": _source_layer(draft.source_text_ref),
            "runtime_final_ref": runtime_final_ref,
            "aggregate_ref": aggregate_ref,
            "open_patch_candidates": [
                _serialize_patch_candidate(row)
                for row in self.session.execute(
                    select(PassagePatchCandidate)
                    .where(
                        PassagePatchCandidate.object_type == draft.object_type,
                        PassagePatchCandidate.object_id == draft.object_id,
                        PassagePatchCandidate.status == "candidate",
                    )
                    .order_by(PassagePatchCandidate.created_at.desc(), PassagePatchCandidate.patch_id.desc())
                ).scalars().all()
            ],
            "open_draft_proposals": [
                self.serialize_proposal(row)
                for row in self.session.execute(
                    select(AuthorDraftProposal)
                    .where(
                        AuthorDraftProposal.draft_id == draft.draft_id,
                        AuthorDraftProposal.status == "candidate",
                    )
                    .order_by(AuthorDraftProposal.created_at.desc(), AuthorDraftProposal.proposal_id.desc())
                ).scalars().all()
            ],
            "author_preference_summary": preference.summary_json if preference is not None else {},
        }


    @staticmethod
    def serialize_draft(
        row: AuthorDraft | None,
        *,
        current_final_scene_row_id: str | None | object = _RUNTIME_FINAL_UNAVAILABLE,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        canonical_dirty = row.last_promoted_revision_no != row.revision_no
        if row.object_type == "scene":
            # Without runtime state, never claim that a scene is canonical. Desk
            # responses pass the pointer explicitly and therefore remain exact.
            canonical_dirty = bool(
                canonical_dirty
                or current_final_scene_row_id is _RUNTIME_FINAL_UNAVAILABLE
                or not row.last_promoted_final_scene_row_id
                or current_final_scene_row_id != row.last_promoted_final_scene_row_id
            )
        return {
            "draft_id": row.draft_id,
            "object_type": row.object_type,
            "object_id": row.object_id,
            "source_text_ref": row.source_text_ref,
            "content": sanitize_manuscript_html(row.content),
            "revision_no": row.revision_no,
            "last_promoted_revision_no": row.last_promoted_revision_no,
            "last_promoted_final_scene_row_id": row.last_promoted_final_scene_row_id,
            "canonical_dirty": canonical_dirty,
            "status": row.status,
            "created_by": row.created_by,
            "updated_by": row.updated_by,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def serialize_event(row: AuthorDraftEvent) -> dict[str, Any]:
        return {
            "event_id": row.event_id,
            "draft_id": row.draft_id,
            "object_type": row.object_type,
            "object_id": row.object_id,
            "event_type": row.event_type,
            "patch_id": row.patch_id,
            "revision_id": row.revision_id,
            "option_id": row.option_id,
            "note": row.note,
            "payload_json": row.payload_json or {},
            "created_by": row.created_by,
            "created_at": row.created_at,
        }

    @staticmethod
    def serialize_proposal(row: AuthorDraftProposal) -> dict[str, Any]:
        return {
            "proposal_id": row.proposal_id,
            "draft_id": row.draft_id,
            "object_type": row.object_type,
            "object_id": row.object_id,
            "proposal_type": row.proposal_type,
            "proposal_source": row.proposal_source,
            "content": row.content,
            "rationale": row.rationale,
            "source_llm_call_id": row.source_llm_call_id,
            "target_range": row.target_range_json or None,
            "before_text_hash": row.before_text_hash,
            "replacement_text": row.replacement_text,
            "proposal_kind": row.proposal_kind or _proposal_kind_from_type(row.proposal_type),
            "source_evaluation_id": row.source_evaluation_id,
            "merge_status": row.merge_status or "pending",
            "status": row.status,
            "author_decision_note": row.author_decision_note,
            "created_by": row.created_by,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }


    def _require_target(self, object_type: str, object_id: str) -> None:
        if object_type == "project":
            if self.session.get(StoryProject, object_id) is None:
                raise DomainError("PROJECT_NOT_FOUND", "project not found", status_code=404)
            return
        if object_type == "chapter":
            self.lifecycle.require_active_chapter(object_id)
            return
        if object_type == "scene":
            self.lifecycle.require_active_scene(object_id)
            return
        raise DomainError("AUTHOR_DRAFT_TARGET_INVALID", "object_type must be scene, chapter, or project", status_code=400)

    def _current_row(self, object_type: str, object_id: str) -> AuthorDraft | None:
        return self.session.execute(
            select(AuthorDraft)
            .where(
                AuthorDraft.object_type == object_type,
                AuthorDraft.object_id == object_id,
                AuthorDraft.status == "current",
            )
            .order_by(AuthorDraft.updated_at.desc(), AuthorDraft.draft_id.desc())
        ).scalars().first()

    def _require_draft(self, draft_id: str) -> AuthorDraft:
        draft = self.session.get(AuthorDraft, draft_id)
        if draft is None:
            raise DomainError("AUTHOR_DRAFT_NOT_FOUND", "author draft not found", status_code=404)
        if draft.status != "current":
            raise DomainError("AUTHOR_DRAFT_NOT_CURRENT", "author draft is not current", status_code=409)
        return draft

    def _require_proposal(self, proposal_id: str) -> AuthorDraftProposal:
        proposal = self.session.get(AuthorDraftProposal, proposal_id)
        if proposal is None:
            raise DomainError("AUTHOR_DRAFT_PROPOSAL_NOT_FOUND", "author draft proposal not found", status_code=404)
        return proposal

    def _validate_proposal_for_draft(self, draft: AuthorDraft, proposal: AuthorDraftProposal) -> None:
        if proposal.draft_id != draft.draft_id:
            raise DomainError("AUTHOR_DRAFT_PROPOSAL_DRAFT_MISMATCH", "proposal belongs to a different author draft", status_code=409)
        if draft.object_type != proposal.object_type or draft.object_id != proposal.object_id:
            raise DomainError("AUTHOR_DRAFT_PROPOSAL_TARGET_MISMATCH", "proposal target does not match author draft", status_code=409)

    def _proposal_preference_summary(self, draft: AuthorDraft, target: dict[str, Any]) -> dict[str, Any]:
        scope_candidates: list[tuple[str, str]] = [("global", "global")]
        project_id = str(target.get("project_id") or "").strip()
        chapter_id = str(target.get("chapter_id") or "").strip()
        project = self.session.get(StoryProject, project_id) if project_id else None
        genre = _normalized_preference_scope_ref(project.genre if project else None)
        if genre:
            scope_candidates.append(("genre", genre))
        if project_id:
            scope_candidates.append(("project", project_id))
        if chapter_id:
            scope_candidates.append(("chapter", chapter_id))

        merged: dict[str, Any] = {}
        applied_scopes: list[dict[str, str]] = []
        for scope_type, scope_ref_id in scope_candidates:
            profiles = self.session.execute(
                select(AuthorPreferenceProfile)
                .where(
                    AuthorPreferenceProfile.scope_type == scope_type,
                    AuthorPreferenceProfile.scope_ref_id == scope_ref_id,
                    AuthorPreferenceProfile.status == "approved",
                    AuthorPreferenceProfile.runtime_eligible == 1,
                )
                .order_by(AuthorPreferenceProfile.updated_at.asc(), AuthorPreferenceProfile.profile_id.asc())
            ).scalars().all()
            for profile in profiles:
                summary = profile.summary_json or {}
                if isinstance(summary, dict):
                    merged = _merge_preference_summaries(merged, summary)
                    applied_scopes.append(
                        {"scope_type": scope_type, "scope_ref_id": scope_ref_id, "profile_id": profile.profile_id}
                    )
        if applied_scopes:
            merged["scope_type"] = applied_scopes[-1]["scope_type"]
            merged["scope_ref_id"] = applied_scopes[-1]["scope_ref_id"]
            merged["applied_scopes"] = applied_scopes
        return _safe_preference_summary_for_prompt(merged)

    def _refresh_proposal_preference_profile(
        self,
        proposal: AuthorDraftProposal,
        *,
        actor_ref: str,
        decision_reason: str | None = None,
        rejected_ai_trace: str | None = None,
    ) -> AuthorPreferenceProfile:
        draft = self._require_draft(proposal.draft_id)
        target = self._target_payload(draft.object_type, draft.object_id)
        scopes = self._preference_learning_scopes(target)
        profiles = [
            self._refresh_scoped_proposal_preference(
                proposal,
                scope_type=scope_type,
                scope_ref_id=scope_ref_id,
                project_id=str(target.get("project_id") or "") or None,
                actor_ref=actor_ref,
                decision_reason=decision_reason,
                rejected_ai_trace=rejected_ai_trace,
            )
            for scope_type, scope_ref_id in scopes
        ]
        return profiles[0]

    def _refresh_scoped_proposal_preference(
        self,
        proposal: AuthorDraftProposal,
        *,
        scope_type: str,
        scope_ref_id: str,
        project_id: str | None,
        actor_ref: str,
        decision_reason: str | None,
        rejected_ai_trace: str | None,
    ) -> AuthorPreferenceProfile:
        profile_id = _preference_profile_id(scope_type, scope_ref_id, "proposals")
        profile = self.session.get(AuthorPreferenceProfile, profile_id)
        if profile is None:
            profile = AuthorPreferenceProfile(
                profile_id=profile_id,
                scope_type=scope_type,
                scope_ref_id=scope_ref_id,
                status="draft",
                runtime_eligible=0,
                summary_json={},
                source_patch_ids_json=[],
                created_by=actor_ref or "author_draft_proposal",
            )
            self.session.add(profile)

        summary = dict(profile.summary_json or {})
        decisions = [row for row in summary.get("proposal_decisions", []) if isinstance(row, dict)]
        decisions.append(
            {
                "proposal_id": proposal.proposal_id,
                "proposal_type": proposal.proposal_type,
                "proposal_source": proposal.proposal_source,
                "object_type": proposal.object_type,
                "object_id": proposal.object_id,
                "decision": proposal.status,
                "note": proposal.author_decision_note or "",
                "decision_reason": decision_reason or "",
                "content_excerpt": _short_excerpt(proposal.content),
            }
        )
        decisions = decisions[-30:]
        summary["proposal_decisions"] = decisions
        summary["accepted_proposal_count"] = sum(1 for row in decisions if row.get("decision") == "accepted")
        summary["rejected_proposal_count"] = sum(1 for row in decisions if row.get("decision") == "rejected")
        summary["accepted_by_type"] = _decision_counts_by_type(decisions, "accepted")
        summary["rejected_by_type"] = _decision_counts_by_type(decisions, "rejected")
        rejected_trace = rejected_ai_trace or _ai_trace_from_decision(proposal.author_decision_note)
        traces = [str(item) for item in summary.get("rejected_ai_traces", []) if str(item).strip()]
        if proposal.status == "rejected" and rejected_trace:
            traces.append(rejected_trace)
        summary["rejected_ai_traces"] = _unique_tail(traces, limit=20)
        if proposal.status == "rejected":
            signal = _safe_preference_signal(
                note=proposal.author_decision_note,
                decision_reason=decision_reason,
                proposal_type=proposal.proposal_type,
                proposal_id=proposal.proposal_id,
            )
            if signal:
                signals = [row for row in summary.get("preference_signals", []) if isinstance(row, dict)]
                signals.append(signal)
                summary["preference_signals"] = signals[-30:]
                hints = [str(item) for item in summary.get("safe_preference_hints", []) if str(item).strip()]
                hints.extend(str(label) for label in signal.get("labels", []) if str(label).strip())
                summary["safe_preference_hints"] = _unique_tail(hints, limit=20)
        profile.status = "draft"
        profile.runtime_eligible = 0
        profile.summary_json = summary
        profile.created_by = actor_ref or profile.created_by
        self._upsert_preference_review(profile, project_id=project_id)
        return profile

    def _preference_learning_scopes(self, target: dict[str, Any]) -> list[tuple[str, str]]:
        project_id = str(target.get("project_id") or "").strip()
        project = self.session.get(StoryProject, project_id) if project_id else None
        scopes: list[tuple[str, str]] = []
        if project_id:
            scopes.append(("project", project_id))
        genre = _normalized_preference_scope_ref(project.genre if project else None)
        if genre:
            scopes.append(("genre", genre))
        return scopes or [("global", "global")]

    def _record_direct_edit_preferences(
        self,
        draft: AuthorDraft,
        *,
        before_text: str,
        after_text: str,
        actor_ref: str,
    ) -> None:
        observation = _direct_edit_preference_observation(
            before_text,
            after_text,
            draft_id=draft.draft_id,
            revision_no=draft.revision_no,
            object_type=draft.object_type,
            object_id=draft.object_id,
        )
        if observation is None:
            return
        target = self._target_payload(draft.object_type, draft.object_id)
        project_id = str(target.get("project_id") or "").strip() or None
        for scope_type, scope_ref_id in self._preference_learning_scopes(target):
            profile_id = _preference_profile_id(scope_type, scope_ref_id, "manual_edits")
            profile = self.session.get(AuthorPreferenceProfile, profile_id)
            if profile is None:
                profile = AuthorPreferenceProfile(
                    profile_id=profile_id,
                    scope_type=scope_type,
                    scope_ref_id=scope_ref_id,
                    status="draft",
                    runtime_eligible=0,
                    summary_json={},
                    source_patch_ids_json=[],
                    created_by=actor_ref or "author_manual_edit",
                )
                self.session.add(profile)
            summary = dict(profile.summary_json or {})
            observations = [row for row in summary.get("manual_edit_observations", []) if isinstance(row, dict)]
            observations.append(observation)
            summary["manual_edit_observations"] = observations[-30:]
            hints = [str(item) for item in summary.get("safe_preference_hints", []) if str(item).strip()]
            hints.extend(str(label) for label in observation.get("labels", []) if str(label).strip())
            summary["safe_preference_hints"] = _unique_tail(hints, limit=20)
            summary["manual_edit_count"] = len(observations[-30:])
            profile.status = "draft"
            profile.runtime_eligible = 0
            profile.summary_json = summary
            profile.created_by = actor_ref or profile.created_by
            self._upsert_preference_review(profile, project_id=project_id)

    def _upsert_preference_review(self, profile: AuthorPreferenceProfile, *, project_id: str | None) -> ReviewItem:
        review_id = f"review_{profile.profile_id}"
        summary = profile.summary_json or {}
        payload = {
            "profile_id": profile.profile_id,
            "scope_type": profile.scope_type,
            "scope_ref_id": profile.scope_ref_id,
            "summary": summary,
            "source_patch_ids": profile.source_patch_ids_json or [],
        }
        review = self.session.get(ReviewItem, review_id)
        if review is None:
            review = ReviewItem(
                review_id=review_id,
                project_id=project_id,
                item_type="author_preference_profile",
                status="pending",
                candidate_text=json.dumps(summary, ensure_ascii=False, sort_keys=True),
                candidate_payload_json=payload,
                active_on_approve=1,
                materialize_status="pending",
            )
            self.session.add(review)
            return review
        review.project_id = project_id or review.project_id
        review.status = "pending"
        review.candidate_text = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        review.candidate_payload_json = payload
        review.active_on_approve = 1
        review.materialize_status = "pending"
        review.approved_item_row_id = None
        review.approved_item_id = None
        return review

    def _source_for_target(self, object_type: str, object_id: str) -> dict[str, str]:
        if object_type == "project":
            self._require_target(object_type, object_id)
            raise DomainError("AUTHOR_DRAFT_SOURCE_MISSING", "project discovery draft has no runtime source", status_code=409)
        if object_type == "scene":
            return self._scene_source(object_id)
        return self._chapter_source(object_id)

    def _blank_source_for_target(self, object_type: str, object_id: str) -> dict[str, str]:
        if object_type == "project":
            self._require_target(object_type, object_id)
            return {"source_text_ref": f"project_discovery:{object_id}:blank", "content": ""}
        if object_type == "chapter":
            self.lifecycle.require_active_chapter(object_id)
            return {"source_text_ref": f"author_blank:chapter:{object_id}", "content": ""}
        scene = self.lifecycle.require_active_scene(object_id)
        chapter = self.lifecycle.require_active_chapter(scene.chapter_id)
        return {
            "source_text_ref": f"scene_card:{scene.scene_id}:blank",
            "content": _scene_blank_scaffold(scene, chapter_goal=chapter.chapter_goal),
        }

    def _scene_source(self, scene_id: str) -> dict[str, str]:
        scene = self.lifecycle.require_active_scene(scene_id)
        state = self.session.get(SceneRunState, scene.scene_id)
        final_row = self.session.get(FinalScene, state.current_final_scene_row_id) if state and state.current_final_scene_row_id else None
        if final_row is None:
            raise DomainError("AUTHOR_DRAFT_SOURCE_MISSING", "scene has no current final scene", status_code=409)
        return {"source_text_ref": f"final_scene:{final_row.row_id}", "content": final_row.content or ""}

    def _chapter_source(self, chapter_id: str) -> dict[str, str]:
        self.lifecycle.require_active_chapter(chapter_id)
        aggregate = self._chapter_aggregate(chapter_id)
        if aggregate is not None:
            return {"source_text_ref": f"chapter_memory:{aggregate.row_id}", "content": aggregate.content or ""}
        scenes = self.session.execute(
            select(SceneCard)
            .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
            .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
        ).scalars().all()
        parts: list[str] = []
        for scene in scenes:
            state = self.session.get(SceneRunState, scene.scene_id)
            final_row = self.session.get(FinalScene, state.current_final_scene_row_id) if state and state.current_final_scene_row_id else None
            if final_row is not None:
                parts.append(final_row.content or "")
        if not parts:
            raise DomainError("AUTHOR_DRAFT_SOURCE_MISSING", "chapter has no manuscript text", status_code=409)
        return {"source_text_ref": f"chapter_assembled:{chapter_id}", "content": "\n".join(parts)}

    def _chapter_aggregate(self, chapter_id: str) -> ChapterMemory | None:
        state = self.session.get(ChapterState, chapter_id)
        if state is not None and state.last_final_memory_row_id:
            pointed = self.session.get(ChapterMemory, state.last_final_memory_row_id)
            if pointed is not None and pointed.chapter_id == chapter_id and pointed.aggregate_stage == "final":
                return pointed
        return self.session.execute(
            select(ChapterMemory)
            .where(
                ChapterMemory.chapter_id == chapter_id,
                ChapterMemory.aggregate_stage == "final",
                ChapterMemory.active_flag == 1,
            )
            .order_by(ChapterMemory.created_at.desc(), ChapterMemory.row_id.desc())
        ).scalars().first()

    def _add_event(
        self,
        draft: AuthorDraft,
        *,
        event_type: str,
        actor_ref: str,
        patch_id: str | None = None,
        revision_id: str | None = None,
        option_id: str | None = None,
        note: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuthorDraftEvent:
        event = AuthorDraftEvent(
            event_id=f"author_draft_event_{uuid.uuid4().hex[:12]}",
            draft_id=draft.draft_id,
            object_type=draft.object_type,
            object_id=draft.object_id,
            event_type=event_type,
            patch_id=patch_id,
            revision_id=revision_id,
            option_id=option_id,
            note=note,
            payload_json=payload or {},
            created_by=actor_ref or "author_draft",
        )
        self.session.add(event)
        return event

    def _target_payload(self, object_type: str, object_id: str) -> dict[str, Any]:
        if object_type == "project":
            project = self.session.get(StoryProject, object_id)
            if project is None:
                raise DomainError("PROJECT_NOT_FOUND", "project not found", status_code=404)
            return {
                "object_type": "project",
                "object_id": project.project_id,
                "project_id": project.project_id,
                "chapter_id": None,
                "scene_id": None,
                "project": {
                    "title": project.title,
                    "genre": project.genre or "",
                    "target_chapter_count": project.target_chapter_count,
                    "target_word_count": project.target_word_count,
                    "outline_text": project.outline_text or "",
                    "planning_mode": project.planning_mode,
                },
                "chapter_goal": "",
                "chapter_writer_brief": {},
                "scene_card": {},
                "current_writer_brief": {},
            }
        if object_type == "scene":
            scene = self.lifecycle.require_active_scene(object_id)
            chapter = self.lifecycle.require_active_chapter(scene.chapter_id)
            return {
                "object_type": "scene",
                "object_id": scene.scene_id,
                "project_id": scene.project_id or chapter.project_id,
                "chapter_id": scene.chapter_id,
                "scene_id": scene.scene_id,
                "chapter_goal": chapter.chapter_goal or "",
                "chapter_writer_brief": normalize_chapter_writer_brief(chapter.writer_brief_json),
                "scene_card": {
                    "scene_goal": scene.scene_goal or "",
                    "beats": scene.beats_json or [],
                    "location": scene.location or "",
                    "exit_change": scene.exit_change or "",
                    "hook": scene.hook or "",
                },
                "current_writer_brief": normalize_scene_writer_brief(scene.writer_brief_json),
            }
        chapter = self.lifecycle.require_active_chapter(object_id)
        return {
            "object_type": "chapter",
            "object_id": chapter.chapter_id,
            "project_id": chapter.project_id,
            "chapter_id": chapter.chapter_id,
            "scene_id": None,
            "chapter_goal": chapter.chapter_goal or "",
            "chapter_writer_brief": normalize_chapter_writer_brief(chapter.writer_brief_json),
            "scene_card": {},
            "current_writer_brief": normalize_chapter_writer_brief(chapter.writer_brief_json),
        }


def _optional_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = _optional_text(payload, key)
    if value is None:
        raise DomainError("AUTHOR_DRAFT_INVALID", f"{key} is required", status_code=400)
    return value


def _source_layer(source_text_ref: str | None) -> str:
    value = str(source_text_ref or "")
    if value.startswith("author_blank:") or value.endswith(":blank"):
        return "author_blank"
    if value.startswith("project_discovery:"):
        return "project_discovery"
    if value.startswith("final_scene:"):
        return "ai_draft"
    if value.startswith("chapter_memory:") or value.startswith("chapter_assembled:"):
        return "runtime_aggregate"
    if value.startswith("author_draft:"):
        return "author_draft"
    return "unknown"


def _replace_or_append(content: str, source_excerpt: str, replacement: str) -> str:
    current = str(content or "")
    needle = str(source_excerpt or "").strip()
    if needle and needle in current:
        return current.replace(needle, replacement, 1)
    trimmed = current.rstrip()
    return f"{trimmed}\n\n{replacement}" if trimmed else replacement


def _apply_proposal_content(current: str, proposal_content: str, apply_mode: str) -> str:
    proposal_text = str(proposal_content or "").strip()
    if apply_mode == "append":
        trimmed = str(current or "").rstrip()
        return f"{trimmed}\n\n{proposal_text}" if trimmed else proposal_text
    return proposal_text


def _text_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _proposal_kind_from_type(proposal_type: str | None) -> str:
    value = str(proposal_type or "").strip()
    if value in {"passage_candidate", "local_patch"}:
        return "local_patch"
    if value in {"language_candidate", "language_pass"}:
        return "language_pass"
    if value in {"structure_candidate", "structure_note"}:
        return "structure_note"
    if value in {"dialogue_pass", "continuation", "near_final_rewrite"}:
        return value
    if value in {"scene_draft", "chapter_draft", "whole_draft"}:
        return "whole_draft"
    return "whole_draft"


def _apply_mode_for_proposal(proposal: AuthorDraftProposal) -> str:
    kind = str(proposal.proposal_kind or "").strip() or _proposal_kind_from_type(proposal.proposal_type)
    return AUTHOR_PROPOSAL_KIND_APPLY_MODES.get(kind, "replace")


def _normalize_apply_mode(requested: str | None, proposal: AuthorDraftProposal) -> str:
    value = str(requested or "").strip()
    if not value:
        return _apply_mode_for_proposal(proposal)
    if value in AUTHOR_PROPOSAL_APPLY_MODES:
        return value
    return AUTHOR_PROPOSAL_KIND_APPLY_MODES.get(value, _apply_mode_for_proposal(proposal))


def _proposal_generation_mode(mode: str | None) -> str:
    value = str(mode or "").strip().lower()
    aliases = {
        "draft": "daily",
        "write": "daily",
        "exploration": "explore",
        "structure_draft": "structure",
        "dialogue_pass": "dialogue",
        "local_language": "language",
        "full_rewrite": "rewrite",
        "scene_rewrite": "rewrite",
        "near_final_review": "near_final",
        "final": "acceptance",
    }
    normalized = aliases.get(value, value)
    return normalized if normalized in AUTHOR_PROPOSAL_MODE_TRIADS else "daily"


def _proposal_mode_triads(mode: str) -> tuple[tuple[str, str], tuple[str, str], tuple[str, str]]:
    return AUTHOR_PROPOSAL_MODE_TRIADS.get(mode, AUTHOR_PROPOSAL_MODE_TRIADS["daily"])


def _proposal_hash_matches(proposal: AuthorDraftProposal, current: str) -> bool:
    return not proposal.before_text_hash or proposal.before_text_hash == _text_hash(current)


def _target_excerpt(proposal: AuthorDraftProposal) -> str:
    target_range = proposal.target_range_json if isinstance(proposal.target_range_json, dict) else {}
    for key in ("source_excerpt", "before_text", "excerpt"):
        value = target_range.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _apply_patch_preview(current: str, target_range: dict[str, Any], replacement_text: str | None) -> str:
    replacement = str(replacement_text or "").strip()
    if not replacement:
        return str(current or "")
    if str(target_range.get("unit") or "") == "char":
        try:
            start = max(0, int(target_range.get("start", 0)))
            end = max(start, int(target_range.get("end", start)))
        except (TypeError, ValueError):
            start = end = 0
        return f"{current[:start]}{replacement}{current[end:]}"
    source_excerpt = ""
    for key in ("source_excerpt", "before_text", "excerpt"):
        value = target_range.get(key)
        if isinstance(value, str) and value.strip():
            source_excerpt = value.strip()
            break
    return _replace_or_append(current, source_excerpt, replacement)


def _apply_proposal_to_content(current: str, proposal: AuthorDraftProposal, apply_mode: str) -> str:
    if apply_mode in {"local_patch", "range_replace", "paragraph_replace"}:
        return _apply_patch_preview(current, proposal.target_range_json or {}, proposal.replacement_text or proposal.content)
    return _apply_proposal_content(current, proposal.replacement_text or proposal.content or "", apply_mode)


def _proposal_generate_snapshot(
    draft: AuthorDraft,
    *,
    target: dict[str, Any],
    proposal_type: str,
    proposal_kind: str,
    instruction: str | None,
    target_range: dict[str, Any] | None,
    preference_summary: dict[str, Any],
) -> dict[str, Any]:
    inline_digests = {
        "author_draft": draft.content or "",
        "target_metadata": json.dumps(target, ensure_ascii=False, sort_keys=True),
        "proposal_request": json.dumps(
            {
                "proposal_type": proposal_type,
                "proposal_kind": proposal_kind,
                "instruction": instruction or "",
                "target_range": target_range or {},
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        "author_preference_summary": json.dumps(preference_summary or {}, ensure_ascii=False, sort_keys=True),
    }
    return {
        "contract_version": "AUTHOR_PROPOSAL_GENERATE_SOURCE_v1",
        "stage_allowlist_name": "author_proposal_generate",
        "scene_id": target.get("scene_id") or "",
        "chapter_id": target.get("chapter_id") or "",
        "source_version_refs": {
            "source_draft_id": draft.draft_id,
            "object_type": draft.object_type,
            "object_id": draft.object_id,
            "proposal_type": proposal_type,
        },
        "resolved_ref_ids": {},
        "ordered_injections": [
            {"slot": "author_draft", "ref_id": draft.draft_id, "digest_key": "author_draft"},
            {"slot": "target_metadata", "ref_id": draft.object_id, "digest_key": "target_metadata"},
            {"slot": "proposal_request", "ref_id": proposal_type, "digest_key": "proposal_request"},
            {"slot": "author_preference_summary", "ref_id": "author_preferences", "digest_key": "author_preference_summary"},
        ],
        "inline_digests": inline_digests,
    }


def _proposal_generate_user_prompt(base_prompt: str, *, draft: AuthorDraft, target: dict[str, Any]) -> str:
    return "\n".join(
        [
            base_prompt,
            "",
            "## Author Draft Target",
            f"Object Type: {draft.object_type}",
            f"Object ID: {draft.object_id}",
            f"Project ID: {target.get('project_id') or ''}",
            f"Chapter ID: {target.get('chapter_id') or ''}",
            f"Scene ID: {target.get('scene_id') or ''}",
            "",
            "## Current Author Draft",
            draft.content or "",
            "",
            "## Current Metadata",
            json.dumps(target, ensure_ascii=False, sort_keys=True),
        ]
    )


def _normalize_proposal_payload(
    payload: Any,
    *,
    draft: AuthorDraft,
    target: dict[str, Any],
    proposal_type: str,
    instruction: str | None,
) -> dict[str, str | None]:
    if not isinstance(payload, dict):
        raise DomainError(
            "AUTHOR_PROPOSAL_OUTPUT_INVALID",
            "author proposal response must be a JSON object",
            status_code=502,
            details={"node_id": "author_proposal_generate", "proposal_type": proposal_type},
        )
    content = str(payload.get("content") or payload.get("proposal") or "").strip()
    if not content:
        # 假生成已退役：模型没给出正文时不再用模板拼占位稿冒充提案。
        raise DomainError(
            "AUTHOR_PROPOSAL_OUTPUT_INVALID",
            "author proposal response is missing non-empty content",
            status_code=502,
            details={"node_id": "author_proposal_generate", "proposal_type": proposal_type},
        )
    rationale = str(payload.get("rationale") or "").strip()
    if not rationale:
        rationale = _proposal_rationale(target=target, proposal_type=proposal_type, instruction=instruction)
    return {"content": content, "rationale": rationale, "source_llm_call_id": None}


def _proposal_rationale(*, target: dict[str, Any], proposal_type: str, instruction: str | None) -> str:
    focus = instruction or target.get("chapter_goal") or "writer-facing drafting target"
    if proposal_type == "structure_candidate":
        return f"结构候选：先处理场景承诺、选择代价和结尾动作。依据：{focus}"
    if proposal_type == "passage_candidate":
        return f"局部段落候选：给作者一个可追加或替换的高压片段。依据：{focus}"
    if proposal_type == "language_candidate":
        return f"语言候选：压缩模型腔、解释句和重复动作。依据：{focus}"
    if proposal_type == "dialogue_pass":
        return f"对白深改：把说明性对白改成关系压力、停顿和反问。依据：{focus}"
    if proposal_type == "language_pass":
        return f"语言压缩：保留意象和动作，删去解释、重复和过度总结。依据：{focus}"
    if proposal_type == "continuation":
        return f"续写候选：只推进下一拍，不改写作者现有正文。依据：{focus}"
    if proposal_type == "near_final_rewrite":
        return f"近终稿重写：保留作者声线、核心意象和已成立关系，只处理解释性对白与收束。依据：{focus}"
    if proposal_type in {"whole_draft", "scene_draft", "chapter_draft"}:
        return f"整段候选：提供可对照的完整改写，但仍需作者显式采纳。依据：{focus}"
    return f"Generated as a comparable {proposal_type} proposal from the current author draft target: {focus}"


def _safe_preference_signal(
    *,
    note: str | None,
    decision_reason: str | None,
    proposal_type: str,
    proposal_id: str,
) -> dict[str, Any] | None:
    text = f"{note or ''} {decision_reason or ''}".lower()
    labels: list[str] = []
    if any(token in text for token in ("exposition", "explain", "explains", "backstory", "info dump", "telling")):
        labels.append("avoid_exposition")
    if any(token in text for token in ("dialogue", "dialog", "conversation", "speech")):
        labels.append("avoid_dialogue_style")
    if any(token in text for token in ("tone", "formal", "flat", "generic", "ai voice", "model voice")):
        labels.append("avoid_tone")
    if any(token in text for token in ("pacing", "pace", "slow", "drag", "rushed", "too fast")):
        labels.append("avoid_pacing")
    if any(token in text for token in ("voice", "keep voice", "author voice", "character voice")):
        labels.append("prefer_voice")
    if any(token in text for token in ("structure", "arc", "beat", "setup", "payoff")):
        labels.append("prefer_structure")
    if not labels and text.strip():
        labels.append("other_safe_note")
    labels = _unique_tail(labels, limit=20)
    if not labels:
        return None
    return {
        "source_proposal_id": proposal_id,
        "proposal_type": proposal_type,
        "labels": labels,
        "safe_summary": "; ".join(labels),
    }


def _normalized_preference_scope_ref(value: str | None) -> str:
    return " ".join(str(value or "").strip().lower().split())[:120]


def _preference_profile_id(scope_type: str, scope_ref_id: str, source: str) -> str:
    digest = hashlib.sha256(f"{scope_type}:{scope_ref_id}".encode("utf-8")).hexdigest()[:16]
    return f"author_pref_{scope_type}_{digest}_{source}"


def _merge_preference_summaries(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in incoming.items():
        if key in {"safe_preference_hints", "rejected_ai_traces"} and isinstance(value, list):
            current = [str(item) for item in merged.get(key, []) if str(item).strip()]
            current.extend(str(item) for item in value if str(item).strip())
            merged[key] = _unique_tail(current, limit=20)
            continue
        if key in {"preference_signals", "manual_edit_observations"} and isinstance(value, list):
            rows = [row for row in merged.get(key, []) if isinstance(row, dict)]
            rows.extend(row for row in value if isinstance(row, dict))
            seen: set[str] = set()
            unique_rows: list[dict[str, Any]] = []
            for row in rows:
                marker = canonical_json(row)
                if marker in seen:
                    continue
                seen.add(marker)
                unique_rows.append(row)
            merged[key] = unique_rows[-30:]
            continue
        merged[key] = value
    return merged


def _direct_edit_preference_observation(
    before_text: str,
    after_text: str,
    *,
    draft_id: str,
    revision_no: int,
    object_type: str,
    object_id: str,
) -> dict[str, Any] | None:
    before = before_text.strip()
    after = after_text.strip()
    # Initial drafting and tiny corrections are not reliable preference evidence.
    if before == after or min(len(before), len(after)) < 80:
        return None
    similarity = difflib.SequenceMatcher(a=before, b=after, autojunk=False).ratio()
    change_ratio = round(1.0 - similarity, 4)
    if change_ratio < 0.12:
        return None

    before_len = len(before)
    after_len = len(after)
    length_ratio = after_len / max(before_len, 1)
    before_dialogue = _dialogue_line_ratio(before)
    after_dialogue = _dialogue_line_ratio(after)
    before_paragraph = _average_paragraph_length(before)
    after_paragraph = _average_paragraph_length(after)
    before_sentence = _average_sentence_length(before)
    after_sentence = _average_sentence_length(after)
    labels: list[str] = []
    if length_ratio <= 0.85:
        labels.append("prefer_concise")
    elif length_ratio >= 1.15:
        labels.append("prefer_expansion")
    if after_dialogue - before_dialogue >= 0.12:
        labels.append("prefer_more_dialogue")
    elif before_dialogue - after_dialogue >= 0.12:
        labels.append("prefer_less_dialogue")
    if before_paragraph and after_paragraph <= before_paragraph * 0.78:
        labels.append("prefer_shorter_paragraphs")
    elif before_paragraph and after_paragraph >= before_paragraph * 1.28:
        labels.append("prefer_longer_paragraphs")
    if before_sentence and after_sentence <= before_sentence * 0.78:
        labels.append("prefer_shorter_sentences")
    elif before_sentence and after_sentence >= before_sentence * 1.28:
        labels.append("prefer_longer_sentences")
    labels = _unique_tail(labels, limit=8)
    if not labels:
        return None
    return {
        "source_draft_id": draft_id,
        "source_revision_no": int(revision_no),
        "object_type": object_type,
        "object_id": object_id,
        "labels": labels,
        "metrics": {
            "change_ratio": change_ratio,
            "length_ratio": round(length_ratio, 4),
            "dialogue_ratio_delta": round(after_dialogue - before_dialogue, 4),
            "paragraph_length_ratio": round(after_paragraph / max(before_paragraph, 1.0), 4),
            "sentence_length_ratio": round(after_sentence / max(before_sentence, 1.0), 4),
        },
    }


def _dialogue_line_ratio(text: str) -> float:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return 0.0
    dialogue = sum(1 for line in lines if line.startswith(("“", '"', "「", "『", "—")))
    return dialogue / len(lines)


def _average_paragraph_length(text: str) -> float:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n|\n", text) if part.strip()]
    return sum(len(part) for part in paragraphs) / max(len(paragraphs), 1)


def _average_sentence_length(text: str) -> float:
    sentences = [part.strip() for part in re.split(r"[。！？!?；;]+", text) if part.strip()]
    return sum(len(part) for part in sentences) / max(len(sentences), 1)


def _safe_preference_summary_for_prompt(summary: dict[str, Any]) -> dict[str, Any]:
    if not summary:
        return {}
    safe: dict[str, Any] = {}
    for key in (
        "accepted_proposal_count",
        "rejected_proposal_count",
        "accepted_by_type",
        "rejected_by_type",
        "scope_type",
        "scope_ref_id",
        "manual_edit_count",
        "applied_scopes",
    ):
        if key in summary:
            safe[key] = summary[key]

    signals = []
    source_signals = summary.get("preference_signals", [])
    for row in source_signals if isinstance(source_signals, list) else []:
        if not isinstance(row, dict):
            continue
        labels = [str(label) for label in row.get("labels", []) if str(label).strip()]
        if labels:
            signals.append(
                {
                    "source_proposal_id": str(row.get("source_proposal_id") or ""),
                    "proposal_type": str(row.get("proposal_type") or ""),
                    "labels": labels[:8],
                    "safe_summary": "; ".join(labels[:8]),
                }
            )
    if signals:
        safe["preference_signals"] = signals[-20:]

    hints = [str(item) for item in summary.get("safe_preference_hints", []) if str(item).strip()]
    if hints:
        safe["safe_preference_hints"] = _unique_tail(hints, limit=20)

    for key in (
        "preferred_revision_moves",
        "rejected_revision_moves",
        "preferred_patch_categories",
        "rejected_patch_categories",
        "preference_tags",
        "ai_trace_terms_to_watch",
    ):
        source_values = summary.get(key, [])
        values = []
        for value in source_values if isinstance(source_values, list) else []:
            sanitized = _safe_trace_for_prompt(value)
            if sanitized:
                values.append(sanitized)
        if values:
            safe[key] = _unique_tail(values, limit=20)

    traces = []
    source_traces = summary.get("rejected_ai_traces", [])
    for trace in source_traces if isinstance(source_traces, list) else []:
        sanitized = _safe_trace_for_prompt(trace)
        if sanitized:
            traces.append(sanitized)
    if traces:
        safe["rejected_ai_traces"] = _unique_tail(traces, limit=20)
    return safe


def _safe_trace_for_prompt(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    lowered = text.lower()
    blocked = (
        "ignore previous",
        "ignore all",
        "system prompt",
        "developer message",
        "tool call",
        "execute ",
        "忽略以上",
        "忽略之前",
        "系统提示",
        "开发者消息",
        "调用工具",
        "执行命令",
    )
    if any(marker in lowered for marker in blocked):
        return ""
    return text[:120]


def _short_excerpt(text: str, *, limit: int = 160) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else f"{compact[:limit].rstrip()}..."


def _decision_counts_by_type(decisions: list[dict[str, Any]], decision: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in decisions:
        if row.get("decision") != decision:
            continue
        proposal_type = str(row.get("proposal_type") or "unknown")
        counts[proposal_type] = counts.get(proposal_type, 0) + 1
    return counts


def _ai_trace_from_decision(note: str | None) -> str:
    value = str(note or "").strip()
    if not value:
        return ""
    trace_markers = ("模型腔", "AI", "ai", "解释", "直白", "套路", "模板")
    return value if any(marker in value for marker in trace_markers) else ""


def _unique_tail(values: list[str], *, limit: int) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result[-limit:]


def _serialize_patch_candidate(row: PassagePatchCandidate) -> dict[str, Any]:
    return {
        "patch_id": row.patch_id,
        "object_type": row.object_type,
        "object_id": row.object_id,
        "chapter_id": row.chapter_id,
        "scene_id": row.scene_id,
        "source_text_ref": row.source_text_ref,
        "target_text_ref": row.target_text_ref,
        "source_draft_id": row.source_draft_id,
        "source_excerpt": row.source_excerpt,
        "issue_dimension": row.issue_dimension,
        "candidate_category": row.candidate_category,
        "target_range": row.target_range_json or None,
        "revision_strategy": row.revision_strategy,
        "preference_tags": row.preference_tags_json or [],
        "inserted_into_author_draft": bool(row.inserted_into_author_draft),
        "replacement_options": row.replacement_options_json or [],
        "rationale": row.rationale,
        "status": row.status,
        "author_decision": row.author_decision,
        "selected_option_id": row.selected_option_id,
        "author_decision_note": row.author_decision_note,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _scene_blank_scaffold(scene: SceneCard, *, chapter_goal: str) -> str:
    parts: list[str] = []
    if chapter_goal:
        parts.append(f"【章节目标】{chapter_goal}")
    if scene.scene_goal:
        parts.append(f"【场景目标】{scene.scene_goal}")
    if scene.location:
        parts.append(f"【地点】{scene.location}")
    beats = [str(item).strip() for item in (scene.beats_json or []) if str(item).strip()]
    if beats:
        parts.append(f"【节拍】{' / '.join(beats)}")
    if scene.exit_change:
        parts.append(f"【结尾变化】{scene.exit_change}")
    if scene.hook:
        parts.append(f"【读者钩子】{scene.hook}")
    return "\n".join(parts)



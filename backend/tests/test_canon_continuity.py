from __future__ import annotations

import hashlib

import pytest

from novel_system.db.models import (
    CanonCommit,
    ChapterGoal,
    ContinuitySnapshot,
    FactCandidate,
    FinalScene,
    NarrativeEvent,
    SceneCard,
    SceneRunState,
    StoryCharacter,
    StoryProject,
    TimelineEvent,
)
from novel_system.services.canon_continuity import CanonContinuityService
from novel_system.services.errors import DomainError
from novel_system.services.narrative_event_log import NarrativeEventLog


def _seed_scene(session, key: str = "BASE") -> dict[str, str]:
    project_id = f"CANON_CONT_{key}"
    chapter_id = f"{project_id}_CH01"
    scene_id = f"{chapter_id}_SC01"
    next_scene_id = f"{chapter_id}_SC02"
    final_scene_row_id = f"final_scene_{scene_id}_v1"
    content = "林远在钟楼醒来，发现自己的右臂已经折断。"
    session.add(
        StoryProject(
            project_id=project_id,
            title="正史连续性测试",
            outline_text="",
        )
    )
    session.flush()
    session.add(
        ChapterGoal(
            chapter_id=chapter_id,
            project_id=project_id,
            chapter_goal="验证正文事实闭环",
            planned_scene_count=2,
            display_order=1,
        )
    )
    session.flush()
    session.add_all(
        [
            SceneCard(
                scene_id=scene_id,
                chapter_id=chapter_id,
                project_id=project_id,
                scene_seq=1,
                scene_goal="受伤",
            ),
            SceneCard(
                scene_id=next_scene_id,
                chapter_id=chapter_id,
                project_id=project_id,
                scene_seq=2,
                scene_goal="继续行动",
            ),
        ]
    )
    session.flush()
    session.add(
        SceneRunState(
            scene_id=scene_id,
            scene_status="archived",
            current_final_scene_row_id=final_scene_row_id,
            narrative_sync_status="synced",
        )
    )
    final = FinalScene(
        row_id=final_scene_row_id,
        scene_id=scene_id,
        chapter_id=chapter_id,
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        status="archived",
        source_bundle_id=f"bundle_{scene_id}",
        source_bundle_hash="bundle-hash",
    )
    session.add(final)
    session.add(
        StoryCharacter(
            character_id="CHAR_LINYUAN",
            project_id=project_id,
            display_name="林远",
            summary_json={"aliases": ["阿远"]},
            status="active",
        )
    )
    session.flush()
    return {
        "project_id": project_id,
        "chapter_id": chapter_id,
        "scene_id": scene_id,
        "next_scene_id": next_scene_id,
        "final_scene_row_id": final_scene_row_id,
    }


def _pending_event(session, seeded: dict[str, str], *, raw_entity: str = "林远") -> NarrativeEvent:
    return NarrativeEventLog(session).log_event(
        project_id=seeded["project_id"],
        chapter_id=seeded["chapter_id"],
        scene_id=seeded["scene_id"],
        event_type="character_state",
        entity_type="character",
        entity_id=raw_entity,
        fact_key="injury",
        fact_value="右臂骨折",
        confidence="extracted",
        source_text_excerpt="右臂已经折断",
        payload={"source": "prose"},
        authority_status="pending",
        source_kind="prose_extraction",
        final_scene_row_id=seeded["final_scene_row_id"],
    )


def _complete_timeline_fact(
    session,
    seeded: dict[str, str],
) -> tuple[CanonContinuityService, TimelineEvent, dict[str, object]]:
    service = CanonContinuityService(session)
    service.mark_archive_pending(seeded["final_scene_row_id"])
    timeline = TimelineEvent(
        event_id=f"timeline_{seeded['scene_id']}",
        project_id=seeded["project_id"],
        label="林远右臂受伤",
        event_mode="planned",
        realization_status="planned",
    )
    session.add(timeline)
    session.flush()
    event = _pending_event(session, seeded)
    staged = service.stage_extraction(
        seeded["final_scene_row_id"],
        outcome="completed_events",
        event_ids=[event.event_id],
    )
    candidate = session.get(FactCandidate, staged["candidate_ids"][0])
    assert candidate is not None
    candidate.planned_timeline_event_id = timeline.event_id
    decision = service.decide_candidate(
        seeded["project_id"],
        candidate.candidate_id,
        action="accept",
        actor_ref="author",
    )
    service.verify_scene_complete(
        seeded["project_id"],
        seeded["scene_id"],
        actor_ref="author",
        note="已确认计划事件在正文中兑现",
        expected_final_scene_row_id=seeded["final_scene_row_id"],
    )
    return service, timeline, decision


def test_pending_extraction_cannot_pollute_runtime_projection(session) -> None:
    seeded = _seed_scene(session, "PENDING")
    event = _pending_event(session, seeded)
    session.flush()

    assert event.authority_status == "pending"
    projected = NarrativeEventLog(session).project_character_state(
        "CHAR_LINYUAN",
        seeded["project_id"],
        before_scene_id=seeded["next_scene_id"],
    )
    assert projected.get("injury") is None
    assert raw_entity_not_in_accepted_index(session, seeded["project_id"], "林远")


def raw_entity_not_in_accepted_index(session, project_id: str, entity_id: str) -> bool:
    log = NarrativeEventLog(session)
    return entity_id not in log._entities_of_type_in_project(project_id, "character")


def test_accepting_resolved_candidate_commits_canon_and_builds_snapshot(session) -> None:
    seeded = _seed_scene(session, "ACCEPT")
    service = CanonContinuityService(session)
    service.mark_archive_pending(seeded["final_scene_row_id"])
    event = _pending_event(session, seeded, raw_entity="阿远")

    staged = service.stage_extraction(
        seeded["final_scene_row_id"],
        outcome="completed_events",
        event_ids=[event.event_id],
    )
    candidate = session.get(FactCandidate, staged["candidate_ids"][0])
    assert candidate is not None
    assert candidate.entity_resolution_status == "alias"
    assert candidate.resolved_entity_id == "CHAR_LINYUAN"
    assert candidate.status == "pending"
    assert service.scene_status(seeded["project_id"], seeded["scene_id"])["status"] == "pending_review"
    assert service.format_recent_checkpoint_for_prompt(
        seeded["project_id"],
        seeded["next_scene_id"],
    ) == ""

    decided = service.decide_candidate(
        seeded["project_id"],
        candidate.candidate_id,
        action="accept",
        actor_ref="author",
        expected_final_scene_row_id=seeded["final_scene_row_id"],
    )
    session.flush()

    assert decided["candidate"]["status"] == "accepted"
    assert session.get(CanonCommit, decided["commit_id"]) is not None
    assert event.authority_status == "pending"
    assert event.entity_id == "CHAR_LINYUAN"
    assert event.canon_commit_id == decided["commit_id"]
    state = session.get(SceneRunState, seeded["scene_id"])
    assert state.narrative_sync_status == "pending_review"

    snapshot = session.get(
        ContinuitySnapshot,
        f"continuity_scene_{seeded['final_scene_row_id']}",
    )
    assert snapshot is not None
    assert snapshot.status == "pending"
    assert snapshot.entity_ids_json == []
    assert snapshot.state_deltas_json == []
    projected_before_verification = NarrativeEventLog(session).project_character_state(
        "CHAR_LINYUAN",
        seeded["project_id"],
        before_scene_id=seeded["next_scene_id"],
    )
    assert projected_before_verification.get("injury") is None

    verified = service.verify_scene_complete(
        seeded["project_id"],
        seeded["scene_id"],
        actor_ref="author",
        note="候选已逐条核对，并通读确认没有遗漏的持久事实",
        expected_final_scene_row_id=seeded["final_scene_row_id"],
    )
    assert verified["complete"] is True
    assert state.narrative_sync_status == "synced"
    assert state.narrative_sync_final_scene_row_id == seeded["final_scene_row_id"]
    assert snapshot.status == "complete"
    assert event.authority_status == "accepted"
    assert snapshot.entity_ids_json == ["CHAR_LINYUAN"]
    assert snapshot.state_deltas_json[0]["fact_key"] == "injury"

    projected = NarrativeEventLog(session).project_character_state(
        "CHAR_LINYUAN",
        seeded["project_id"],
        before_scene_id=seeded["next_scene_id"],
    )
    assert projected.get("injury") == "右臂骨折"
    checkpoint = service.format_recent_checkpoint_for_prompt(
        seeded["project_id"],
        seeded["next_scene_id"],
    )
    assert "Recent Committed Continuity Changes" in checkpoint
    assert "CHAR_LINYUAN.injury = 右臂骨折" in checkpoint


def test_candidate_acceptance_commit_cannot_fake_scene_completion(session) -> None:
    seeded = _seed_scene(session, "CANDIDATE_ONLY")
    service = CanonContinuityService(session)
    service.mark_archive_pending(seeded["final_scene_row_id"])
    event = _pending_event(session, seeded)
    staged = service.stage_extraction(
        seeded["final_scene_row_id"],
        outcome="completed_events",
        event_ids=[event.event_id],
    )
    decision = service.decide_candidate(
        seeded["project_id"],
        staged["candidate_ids"][0],
        action="accept",
        actor_ref="author",
    )

    # Simulate a stale/buggy writer outside the canon service attempting to
    # promote the old status flag and snapshot without a completeness commit.
    state = session.get(SceneRunState, seeded["scene_id"])
    snapshot = session.get(
        ContinuitySnapshot,
        f"continuity_scene_{seeded['final_scene_row_id']}",
    )
    state.narrative_sync_status = "synced"
    state.narrative_sync_final_scene_row_id = seeded["final_scene_row_id"]
    snapshot.status = "complete"
    event.authority_status = "accepted"
    session.flush()

    status = service.scene_status(seeded["project_id"], seeded["scene_id"])
    assert status["complete"] is False
    assert status["status"] == "pending_review"
    projected = NarrativeEventLog(session).project_character_state(
        "CHAR_LINYUAN",
        seeded["project_id"],
        before_scene_id=seeded["next_scene_id"],
    )
    assert projected.get("injury") is None


def test_repeated_stage_is_idempotent_after_scene_completion(session) -> None:
    seeded = _seed_scene(session, "RESTAGE_COMPLETE")
    service = CanonContinuityService(session)
    service.mark_archive_pending(seeded["final_scene_row_id"])
    event = _pending_event(session, seeded)
    staged = service.stage_extraction(
        seeded["final_scene_row_id"],
        outcome="completed_events",
        event_ids=[event.event_id],
    )
    decision = service.decide_candidate(
        seeded["project_id"],
        staged["candidate_ids"][0],
        action="accept",
        actor_ref="author",
    )
    service.verify_scene_complete(
        seeded["project_id"],
        seeded["scene_id"],
        actor_ref="author",
        note="已确认清单完整",
    )

    repeated = service.stage_extraction(
        seeded["final_scene_row_id"],
        outcome="completed_events",
        event_ids=[event.event_id, event.event_id],
    )

    assert repeated["status"] == "synced"
    assert repeated["candidate_ids"] == staged["candidate_ids"]
    assert event.authority_status == "accepted"
    assert event.canon_commit_id == decision["commit_id"]
    assert service.scene_status(seeded["project_id"], seeded["scene_id"])["complete"] is True


def test_repeated_stage_preserves_rejected_decision_before_verification(session) -> None:
    seeded = _seed_scene(session, "RESTAGE_REJECTED")
    service = CanonContinuityService(session)
    service.mark_archive_pending(seeded["final_scene_row_id"])
    event = _pending_event(session, seeded)
    staged = service.stage_extraction(
        seeded["final_scene_row_id"],
        outcome="completed_events",
        event_ids=[event.event_id],
    )
    service.decide_candidate(
        seeded["project_id"],
        staged["candidate_ids"][0],
        action="reject",
        actor_ref="author",
    )

    repeated = service.stage_extraction(
        seeded["final_scene_row_id"],
        outcome="completed_events",
        event_ids=[event.event_id],
    )

    assert repeated["candidate_ids"] == staged["candidate_ids"]
    assert event.authority_status == "rejected"
    assert session.get(FactCandidate, staged["candidate_ids"][0]).status == "rejected"


def test_actual_final_text_hash_invalidates_stale_completion_and_evidence(session) -> None:
    seeded = _seed_scene(session, "HASH_DRIFT")
    service = CanonContinuityService(session)
    service.mark_archive_pending(seeded["final_scene_row_id"])
    event = _pending_event(session, seeded)
    staged = service.stage_extraction(
        seeded["final_scene_row_id"],
        outcome="completed_events",
        event_ids=[event.event_id],
    )
    decision = service.decide_candidate(
        seeded["project_id"],
        staged["candidate_ids"][0],
        action="accept",
        actor_ref="author",
    )
    service.verify_scene_complete(
        seeded["project_id"],
        seeded["scene_id"],
        actor_ref="author",
        note="已核对原始终稿",
    )
    final = session.get(FinalScene, seeded["final_scene_row_id"])
    final.content = "林远在钟楼醒来，右臂完好无损。"
    # Simulate a legacy in-place writer that forgot to refresh content_hash.
    session.flush()

    status = service.scene_status(seeded["project_id"], seeded["scene_id"])

    assert status["complete"] is False
    assert status["status"] == "pending_review"
    assert status["candidates"][0]["evidence"]["grounded"] is False
    assert service.format_recent_checkpoint_for_prompt(
        seeded["project_id"],
        seeded["next_scene_id"],
    ) == ""

    service.mark_archive_pending(seeded["final_scene_row_id"])
    assert event.authority_status == "superseded"
    assert session.get(FactCandidate, staged["candidate_ids"][0]).status == "superseded"
    assert session.get(CanonCommit, decision["commit_id"]).status == "superseded"
    replacement = service.create_manual_candidate(
        seeded["project_id"],
        seeded["scene_id"],
        event_type="character_state",
        raw_entity_ref="林远",
        fact_key="injury",
        fact_value="右臂完好无损",
        evidence_text="右臂完好无损",
    )
    service.decide_candidate(
        seeded["project_id"],
        replacement["candidate_id"],
        action="accept",
        actor_ref="author",
    )
    service.verify_scene_complete(
        seeded["project_id"],
        seeded["scene_id"],
        actor_ref="author",
        note="已重新核对修改后的正文",
    )
    projected = NarrativeEventLog(session).project_character_state(
        "CHAR_LINYUAN",
        seeded["project_id"],
        before_scene_id=seeded["next_scene_id"],
    )
    assert projected.get("injury") == "右臂完好无损"


def test_ambiguous_alias_requires_explicit_author_resolution(session) -> None:
    seeded = _seed_scene(session, "AMBIG")
    session.add(
        StoryCharacter(
            character_id="CHAR_OTHER",
            project_id=seeded["project_id"],
            display_name="林宁",
            summary_json={"aliases": ["阿远"]},
            status="active",
        )
    )
    service = CanonContinuityService(session)
    service.mark_archive_pending(seeded["final_scene_row_id"])
    event = _pending_event(session, seeded, raw_entity="阿远")
    staged = service.stage_extraction(
        seeded["final_scene_row_id"],
        outcome="completed_events",
        event_ids=[event.event_id],
    )
    candidate_id = staged["candidate_ids"][0]
    candidate = session.get(FactCandidate, candidate_id)
    assert candidate.entity_resolution_status == "ambiguous"
    assert set(candidate.entity_candidates_json) == {"CHAR_LINYUAN", "CHAR_OTHER"}

    with pytest.raises(DomainError) as unresolved:
        service.decide_candidate(
            seeded["project_id"],
            candidate_id,
            action="accept",
            actor_ref="author",
        )
    assert unresolved.value.code == "CANON_ENTITY_RESOLUTION_REQUIRED"

    result = service.decide_candidate(
        seeded["project_id"],
        candidate_id,
        action="accept",
        selected_entity_id="CHAR_LINYUAN",
        actor_ref="author",
    )
    assert result["candidate"]["resolved_entity_id"] == "CHAR_LINYUAN"


def test_extractor_hallucinated_evidence_cannot_be_committed(session) -> None:
    seeded = _seed_scene(session, "UNGROUNDED")
    service = CanonContinuityService(session)
    service.mark_archive_pending(seeded["final_scene_row_id"])
    event = _pending_event(session, seeded)
    event.source_text_excerpt = "终稿中不存在的证据句"
    staged = service.stage_extraction(
        seeded["final_scene_row_id"],
        outcome="completed_events",
        event_ids=[event.event_id],
    )

    with pytest.raises(DomainError) as blocked:
        service.decide_candidate(
            seeded["project_id"],
            staged["candidate_ids"][0],
            action="accept",
            actor_ref="author",
        )
    assert blocked.value.code == "CANON_EVIDENCE_NOT_IN_FINAL"


def test_extractor_missing_evidence_cannot_be_committed(session) -> None:
    seeded = _seed_scene(session, "NO_EVIDENCE")
    service = CanonContinuityService(session)
    service.mark_archive_pending(seeded["final_scene_row_id"])
    event = _pending_event(session, seeded)
    event.source_text_excerpt = None
    staged = service.stage_extraction(
        seeded["final_scene_row_id"],
        outcome="completed_events",
        event_ids=[event.event_id],
    )

    with pytest.raises(DomainError) as blocked:
        service.decide_candidate(
            seeded["project_id"],
            staged["candidate_ids"][0],
            action="accept",
            actor_ref="author",
        )

    assert blocked.value.code == "CANON_EVIDENCE_NOT_IN_FINAL"
    assert event.authority_status == "pending"


def test_scene_verification_requires_nonblank_audit_note(session) -> None:
    seeded = _seed_scene(session, "VERIFY_NOTE")
    service = CanonContinuityService(session)
    service.mark_archive_pending(seeded["final_scene_row_id"])
    service.stage_extraction(
        seeded["final_scene_row_id"],
        outcome="completed_empty",
        event_ids=[],
    )

    with pytest.raises(DomainError) as blocked:
        service.verify_scene_complete(
            seeded["project_id"],
            seeded["scene_id"],
            actor_ref="author",
            note="   ",
        )

    assert blocked.value.code == "CANON_VERIFICATION_NOTE_REQUIRED"


def test_completed_empty_requires_explicit_author_verification(session) -> None:
    seeded = _seed_scene(session, "EMPTY")
    service = CanonContinuityService(session)
    service.mark_archive_pending(seeded["final_scene_row_id"])
    staged = service.stage_extraction(
        seeded["final_scene_row_id"],
        outcome="completed_empty",
        event_ids=[],
    )
    assert staged["status"] == "pending_review"

    verified = service.verify_scene_complete(
        seeded["project_id"],
        seeded["scene_id"],
        expected_final_scene_row_id=seeded["final_scene_row_id"],
        actor_ref="author",
        note="通读确认，本场没有需要延续的状态变化",
    )
    assert verified["status"] == "synced"
    assert verified["commit_id"]


def test_completed_scene_rejects_late_manual_candidate(session) -> None:
    seeded = _seed_scene(session, "IMMUTABLE_COMPLETE")
    service = CanonContinuityService(session)
    service.mark_archive_pending(seeded["final_scene_row_id"])
    service.stage_extraction(
        seeded["final_scene_row_id"],
        outcome="completed_empty",
        event_ids=[],
    )
    service.verify_scene_complete(
        seeded["project_id"],
        seeded["scene_id"],
        actor_ref="author",
        note="已确认本场没有新的持久事实",
    )

    with pytest.raises(DomainError) as blocked:
        service.create_manual_candidate(
            seeded["project_id"],
            seeded["scene_id"],
            event_type="character_state",
            raw_entity_ref="林远",
            fact_key="injury",
            fact_value="右臂骨折",
            evidence_text="右臂已经折断",
        )

    assert blocked.value.code == "CANON_SCENE_ALREADY_COMMITTED"


def test_facts_unchanged_cannot_carry_from_uncommitted_source(session) -> None:
    seeded = _seed_scene(session, "CARRY_UNCOMMITTED")
    service = CanonContinuityService(session)
    service.mark_archive_pending(seeded["final_scene_row_id"])

    replacement_id = f"{seeded['final_scene_row_id']}_v2"
    replacement_content = "林远离开钟楼。"
    session.add(
        FinalScene(
            row_id=replacement_id,
            scene_id=seeded["scene_id"],
            chapter_id=seeded["chapter_id"],
            content=replacement_content,
            content_hash=hashlib.sha256(replacement_content.encode("utf-8")).hexdigest(),
            status="archived",
            source_bundle_id=f"bundle_{seeded['scene_id']}_v2",
            source_bundle_hash="bundle-hash-v2",
            parent_final_scene_row_id=seeded["final_scene_row_id"],
        )
    )
    state = session.get(SceneRunState, seeded["scene_id"])
    state.current_final_scene_row_id = replacement_id
    state.narrative_sync_final_scene_row_id = replacement_id
    session.flush()
    service.mark_archive_pending(replacement_id)

    with pytest.raises(DomainError) as blocked:
        service.carry_forward_facts_unchanged(
            replacement_id,
            source_final_scene_row_id=seeded["final_scene_row_id"],
            actor_ref="author",
            note="不能从未核验版本继承",
        )
    assert blocked.value.code == "CANON_CARRY_SOURCE_NOT_COMMITTED"


def test_removed_timeline_realization_reverts_only_when_replacement_is_verified(session) -> None:
    seeded = _seed_scene(session, "TIMELINE_REMOVED")
    service, timeline, old_decision = _complete_timeline_fact(session, seeded)
    replacement_id = f"{seeded['final_scene_row_id']}_v2"
    replacement_content = "林远在钟楼醒来，推门走进晨雾。"
    session.add(
        FinalScene(
            row_id=replacement_id,
            scene_id=seeded["scene_id"],
            chapter_id=seeded["chapter_id"],
            content=replacement_content,
            content_hash=hashlib.sha256(replacement_content.encode("utf-8")).hexdigest(),
            status="archived",
            source_bundle_id=f"bundle_{seeded['scene_id']}_v2",
            source_bundle_hash="bundle-hash-v2",
            parent_final_scene_row_id=seeded["final_scene_row_id"],
        )
    )
    state = session.get(SceneRunState, seeded["scene_id"])
    state.current_final_scene_row_id = replacement_id
    session.flush()

    service.mark_archive_pending(replacement_id)
    service.stage_extraction(
        replacement_id,
        outcome="completed_empty",
        event_ids=[],
    )
    assert timeline.realization_status == "realized"
    assert timeline.realized_canon_commit_id == old_decision["commit_id"]

    service.verify_scene_complete(
        seeded["project_id"],
        seeded["scene_id"],
        actor_ref="author",
        note="已通读新版，旧计划事件不再发生",
        expected_final_scene_row_id=replacement_id,
    )

    assert timeline.realization_status == "planned"
    assert timeline.realized_canon_commit_id is None
    assert timeline.realized_scene_id is None


def test_facts_unchanged_rebinds_timeline_to_new_completion_commit(session) -> None:
    seeded = _seed_scene(session, "TIMELINE_CARRY")
    service, timeline, old_decision = _complete_timeline_fact(session, seeded)
    replacement_id = f"{seeded['final_scene_row_id']}_v2"
    replacement_content = "林远在钟楼醒来；他的右臂已经折断。"
    session.add(
        FinalScene(
            row_id=replacement_id,
            scene_id=seeded["scene_id"],
            chapter_id=seeded["chapter_id"],
            content=replacement_content,
            content_hash=hashlib.sha256(replacement_content.encode("utf-8")).hexdigest(),
            status="archived",
            source_bundle_id=f"bundle_{seeded['scene_id']}_v2",
            source_bundle_hash="bundle-hash-v2",
            parent_final_scene_row_id=seeded["final_scene_row_id"],
        )
    )
    state = session.get(SceneRunState, seeded["scene_id"])
    state.current_final_scene_row_id = replacement_id
    session.flush()
    service.mark_archive_pending(replacement_id)

    carried = service.carry_forward_facts_unchanged(
        replacement_id,
        source_final_scene_row_id=seeded["final_scene_row_id"],
        actor_ref="author",
        note="仅调整措辞，故事事实不变",
    )

    assert carried["complete"] is True
    assert carried["commit_id"] != old_decision["commit_id"]
    assert timeline.realization_status == "realized"
    assert timeline.realized_canon_commit_id == carried["commit_id"]
    assert timeline.realized_scene_id == seeded["scene_id"]
    assert session.get(CanonCommit, old_decision["commit_id"]).status == "superseded"


def test_abandoned_partial_revision_cannot_replace_last_completed_timeline(session) -> None:
    seeded = _seed_scene(session, "TIMELINE_ABANDONED")
    service, timeline, old_decision = _complete_timeline_fact(session, seeded)
    state = session.get(SceneRunState, seeded["scene_id"])

    partial_id = f"{seeded['final_scene_row_id']}_v2"
    partial_content = "林远醒来，发现自己的右臂已经痊愈。"
    session.add(
        FinalScene(
            row_id=partial_id,
            scene_id=seeded["scene_id"],
            chapter_id=seeded["chapter_id"],
            content=partial_content,
            content_hash=hashlib.sha256(partial_content.encode("utf-8")).hexdigest(),
            status="archived",
            source_bundle_id=f"bundle_{seeded['scene_id']}_v2",
            source_bundle_hash="bundle-hash-v2",
            parent_final_scene_row_id=seeded["final_scene_row_id"],
        )
    )
    state.current_final_scene_row_id = partial_id
    session.flush()
    service.mark_archive_pending(partial_id)
    partial_event = NarrativeEventLog(session).log_event(
        project_id=seeded["project_id"],
        chapter_id=seeded["chapter_id"],
        scene_id=seeded["scene_id"],
        event_type="character_state",
        entity_type="character",
        entity_id="林远",
        fact_key="injury",
        fact_value="右臂痊愈",
        confidence="extracted",
        source_text_excerpt="右臂已经痊愈",
        payload={"source": "prose"},
        authority_status="pending",
        source_kind="prose_extraction",
        final_scene_row_id=partial_id,
    )
    staged = service.stage_extraction(
        partial_id,
        outcome="completed_events",
        event_ids=[partial_event.event_id],
    )
    partial_candidate = session.get(FactCandidate, staged["candidate_ids"][0])
    assert partial_candidate is not None
    partial_candidate.planned_timeline_event_id = timeline.event_id
    partial_decision = service.decide_candidate(
        seeded["project_id"],
        partial_candidate.candidate_id,
        action="accept",
        actor_ref="author",
    )
    assert timeline.realized_canon_commit_id == old_decision["commit_id"]

    abandoned_id = f"{seeded['final_scene_row_id']}_v3"
    abandoned_content = "林远在钟楼醒来，雨声盖过了远处的钟鸣。"
    session.add(
        FinalScene(
            row_id=abandoned_id,
            scene_id=seeded["scene_id"],
            chapter_id=seeded["chapter_id"],
            content=abandoned_content,
            content_hash=hashlib.sha256(abandoned_content.encode("utf-8")).hexdigest(),
            status="archived",
            source_bundle_id=f"bundle_{seeded['scene_id']}_v3",
            source_bundle_hash="bundle-hash-v3",
            parent_final_scene_row_id=partial_id,
        )
    )
    state.current_final_scene_row_id = abandoned_id
    session.flush()
    service.mark_archive_pending(abandoned_id)

    assert partial_candidate.status == "superseded"
    assert partial_event.authority_status == "superseded"
    assert session.get(CanonCommit, partial_decision["commit_id"]).status == "superseded"
    assert session.get(CanonCommit, old_decision["commit_id"]).status == "active"
    assert timeline.realization_status == "realized"
    assert timeline.realized_canon_commit_id == old_decision["commit_id"]


def test_replacement_completion_supersedes_prior_revision_canon(session) -> None:
    seeded = _seed_scene(session, "REPLACE")
    service = CanonContinuityService(session)
    service.mark_archive_pending(seeded["final_scene_row_id"])
    timeline = TimelineEvent(
        event_id=f"timeline_{seeded['scene_id']}",
        project_id=seeded["project_id"],
        label="林远右臂受伤",
        event_mode="planned",
        realization_status="planned",
    )
    session.add(timeline)
    session.flush()
    old_event = _pending_event(session, seeded)
    old_staged = service.stage_extraction(
        seeded["final_scene_row_id"],
        outcome="completed_events",
        event_ids=[old_event.event_id],
    )
    old_candidate = session.get(FactCandidate, old_staged["candidate_ids"][0])
    old_candidate.planned_timeline_event_id = timeline.event_id
    old_decision = service.decide_candidate(
        seeded["project_id"],
        old_staged["candidate_ids"][0],
        action="accept",
        actor_ref="author",
    )
    assert timeline.realization_status == "planned"
    assert timeline.realized_canon_commit_id is None
    old_verified = service.verify_scene_complete(
        seeded["project_id"],
        seeded["scene_id"],
        actor_ref="author",
        note="旧版候选已核对完整",
        expected_final_scene_row_id=seeded["final_scene_row_id"],
    )
    assert timeline.realization_status == "realized"
    assert timeline.realized_canon_commit_id == old_decision["commit_id"]
    old_snapshot = session.get(
        ContinuitySnapshot,
        f"continuity_scene_{seeded['final_scene_row_id']}",
    )
    assert old_verified["complete"] is True
    assert old_snapshot is not None and old_snapshot.status == "complete"

    replacement_id = f"{seeded['final_scene_row_id']}_v2"
    replacement_content = "林远在钟楼醒来，发现自己的右臂已经痊愈。"
    session.add(
        FinalScene(
            row_id=replacement_id,
            scene_id=seeded["scene_id"],
            chapter_id=seeded["chapter_id"],
            content=replacement_content,
            content_hash=hashlib.sha256(replacement_content.encode("utf-8")).hexdigest(),
            status="archived",
            source_bundle_id=f"bundle_{seeded['scene_id']}_v2",
            source_bundle_hash="bundle-hash-v2",
            parent_final_scene_row_id=seeded["final_scene_row_id"],
        )
    )
    state = session.get(SceneRunState, seeded["scene_id"])
    state.current_final_scene_row_id = replacement_id
    state.narrative_sync_final_scene_row_id = replacement_id
    session.flush()
    service.mark_archive_pending(replacement_id)
    # The previous verified canon remains the last committed truth while the
    # replacement is under review.
    assert timeline.realization_status == "realized"
    assert timeline.realized_canon_commit_id == old_decision["commit_id"]
    new_event = NarrativeEventLog(session).log_event(
        project_id=seeded["project_id"],
        chapter_id=seeded["chapter_id"],
        scene_id=seeded["scene_id"],
        event_type="character_state",
        entity_type="character",
        entity_id="林远",
        fact_key="injury",
        fact_value="右臂痊愈",
        confidence="extracted",
        source_text_excerpt="右臂已经痊愈",
        payload={"source": "prose"},
        authority_status="pending",
        source_kind="prose_extraction",
        final_scene_row_id=replacement_id,
    )
    new_staged = service.stage_extraction(
        replacement_id,
        outcome="completed_events",
        event_ids=[new_event.event_id],
    )
    new_candidate = session.get(FactCandidate, new_staged["candidate_ids"][0])
    new_candidate.planned_timeline_event_id = timeline.event_id
    new_decision = service.decide_candidate(
        seeded["project_id"],
        new_staged["candidate_ids"][0],
        action="accept",
        actor_ref="author",
    )
    assert timeline.realized_canon_commit_id == old_decision["commit_id"]
    projected_while_replacement_pending = NarrativeEventLog(session).project_character_state(
        "CHAR_LINYUAN",
        seeded["project_id"],
        before_scene_id=seeded["next_scene_id"],
    )
    assert projected_while_replacement_pending.get("injury") == "右臂骨折"
    verified = service.verify_scene_complete(
        seeded["project_id"],
        seeded["scene_id"],
        actor_ref="author",
        note="新版候选已核对完整",
        expected_final_scene_row_id=replacement_id,
    )

    assert new_decision["scene"]["complete"] is False
    assert verified["complete"] is True
    assert old_event.authority_status == "superseded"
    assert session.get(CanonCommit, old_decision["commit_id"]).status == "superseded"
    assert old_snapshot.status == "superseded"
    assert timeline.realization_status == "realized"
    assert timeline.realized_canon_commit_id == new_decision["commit_id"]
    assert timeline.realized_scene_id == seeded["scene_id"]
    projected = NarrativeEventLog(session).project_character_state(
        "CHAR_LINYUAN",
        seeded["project_id"],
        before_scene_id=seeded["next_scene_id"],
    )
    assert projected.get("injury") == "右臂痊愈"


def test_chapter_status_reports_pending_scene_and_blocks_publish_contract(session) -> None:
    seeded = _seed_scene(session, "CHAPTER")
    service = CanonContinuityService(session)
    service.mark_archive_pending(seeded["final_scene_row_id"])

    status = service.chapter_status(seeded["project_id"], seeded["chapter_id"])
    assert status["complete"] is False
    assert status["pending_scene_ids"] == [seeded["scene_id"]]

    with pytest.raises(DomainError) as blocked:
        service.require_chapter_complete(seeded["project_id"], seeded["chapter_id"])
    assert blocked.value.code == "CHAPTER_CANON_NOT_COMMITTED"


def test_canon_review_api_exposes_and_commits_pending_candidate(client, session) -> None:
    seeded = _seed_scene(session, "API")
    service = CanonContinuityService(session)
    service.mark_archive_pending(seeded["final_scene_row_id"])
    event = _pending_event(session, seeded, raw_entity="阿远")
    staged = service.stage_extraction(
        seeded["final_scene_row_id"],
        outcome="completed_events",
        event_ids=[event.event_id],
    )
    session.commit()

    status_response = client.get(
        f"/api/v1/projects/{seeded['project_id']}/canon/scenes/{seeded['scene_id']}"
    )
    assert status_response.status_code == 200
    status = status_response.json()["data"]
    assert status["status"] == "pending_review"
    assert status["candidates"][0]["entity_resolution_status"] == "alias"

    decision_response = client.post(
        (
            f"/api/v1/projects/{seeded['project_id']}/canon/candidates/"
            f"{staged['candidate_ids'][0]}/decision"
        ),
        json={
            "action": "accept",
            "expected_final_scene_row_id": seeded["final_scene_row_id"],
            "note": "正文证据清楚，确认写入正史。",
        },
        headers={"X-Idempotency-Key": "canon-api-accept-once"},
    )
    assert decision_response.status_code == 200, decision_response.text
    result = decision_response.json()["data"]
    assert result["candidate"]["status"] == "accepted"
    assert result["scene"]["status"] == "pending_review"

    replay = client.post(
        (
            f"/api/v1/projects/{seeded['project_id']}/canon/candidates/"
            f"{staged['candidate_ids'][0]}/decision"
        ),
        json={
            "action": "accept",
            "expected_final_scene_row_id": seeded["final_scene_row_id"],
            "note": "正文证据清楚，确认写入正史。",
        },
        headers={"X-Idempotency-Key": "canon-api-accept-once"},
    )
    assert replay.status_code == 200
    assert replay.headers["X-Idempotency-Status"] == "replayed"

    verify_response = client.post(
        f"/api/v1/projects/{seeded['project_id']}/canon/scenes/{seeded['scene_id']}/verify",
        json={
            "note": "候选已逐条裁决，并通读确认没有遗漏。",
            "expected_final_scene_row_id": seeded["final_scene_row_id"],
        },
        headers={"X-Idempotency-Key": "canon-api-verify-once"},
    )
    assert verify_response.status_code == 200, verify_response.text
    assert verify_response.json()["data"]["status"] == "synced"


def test_manual_candidate_api_requires_exact_final_scene_evidence(client, session) -> None:
    seeded = _seed_scene(session, "MANUAL_API")
    CanonContinuityService(session).mark_archive_pending(seeded["final_scene_row_id"])
    session.commit()

    response = client.post(
        f"/api/v1/projects/{seeded['project_id']}/canon/scenes/{seeded['scene_id']}/candidates",
        json={
            "event_type": "character_state",
            "raw_entity_ref": "林远",
            "fact_key": "injury",
            "fact_value": "右臂骨折",
            "evidence_text": "正文里并不存在的句子",
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CANON_EVIDENCE_NOT_IN_FINAL"


def test_author_requested_extraction_fails_closed_when_live_model_is_disabled(
    client,
    session,
) -> None:
    seeded = _seed_scene(session, "EXTRACT_DISABLED")
    CanonContinuityService(session).mark_archive_pending(seeded["final_scene_row_id"])
    session.commit()

    response = client.post(
        f"/api/v1/projects/{seeded['project_id']}/canon/scenes/{seeded['scene_id']}/extract",
        json={},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LLM_DISABLED_FOR_CANON_EXTRACTION"

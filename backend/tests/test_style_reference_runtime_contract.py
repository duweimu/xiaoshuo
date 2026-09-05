from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from novel_system.db.models import (
    ChapterGoal,
    SceneBundle,
    SceneCard,
    SceneRunState,
    StoryProject,
)
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.style_reference.injection import InjectionService
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.runtime_contract import (
    STYLE_RUNTIME_CONTRACT_VERSION,
    blend_profile_metric_baselines,
    build_style_runtime_contract,
    contract_profile_objects,
    extract_style_generation_context,
    resolve_style_runtime_contract_state,
    style_runtime_contract_from_bundle,
    style_runtime_contract_status_from_bundle,
    validate_style_runtime_contract,
)
from novel_system.services.style_reference.validation import run_sync_validate_profiles


def _seed_reference(
    session,
    *,
    seed: str,
    strategy: str = "A",
    task_type: str = "scene_generation",
    feature: str = "句式舒展，收束克制",
):
    repo = StyleReferenceRepository(session)
    book_id = f"contract_book_{seed}"
    run_id = f"contract_run_{seed}"
    profile_id = f"contract_profile_{seed}"
    binding_id = f"contract_binding_{seed}_{task_type}"
    quote_id = f"contract_quote_{seed}"
    quote_text = "雨在旧檐边停了一瞬，灯影便向里缩了缩。"
    repo.create_book(
        book_id=book_id,
        title="匿名参考",
        source_kind="upload",
        cloud_policy="segments_only",
        text_checksum=hashlib.sha256(seed.encode("utf-8")).hexdigest(),
        total_chars=10_000,
        status="ready",
        stats_json={
            "rights_declaration": {
                "declared": True,
                "send_rights": True,
            }
        },
    )
    repo.create_run(
        run_id=run_id,
        book_id=book_id,
        status="completed",
        phase="completed",
    )
    repo.create_quote(
        quote_id=quote_id,
        book_id=book_id,
        paragraph_id=None,
        span_start=0,
        span_end=len(quote_text),
        quote_text=quote_text,
        illustrates_dims=["language.rhythm"],
        extracted_features={},
    )
    repo.create_profile(
        profile_id=profile_id,
        book_id=book_id,
        run_id=run_id,
        title="匿名风格",
        status="active",
        profile_json={
            "narrative_summary": "克制观察，动作先于解释。",
            "qualitative_summary": "克制观察，动作先于解释。",
            "style_features": [feature],
            "banned_replication_rules": ["不要复刻专名与独特意象"],
            "scene_samples_index": {"narration": [quote_id]},
            "metrics_baseline": {
                "avg_sentence_length": {"mean": 18.0, "std": 2.0},
                "short_sentence_ratio": {"mean": 0.2, "std": 0.05},
            },
            # Simulate a future profile field that contains source prose.  The
            # runtime-contract allow-list must keep it out of SceneBundle.
            "future_raw_excerpt": "这段未来原文字段绝不能进入运行契约。",
        },
        coverage_json={},
        source_finding_ids_json=[],
        version_tag="v1",
    )
    repo.create_banned_term(
        term_id=f"contract_term_{seed}",
        profile_id=profile_id,
        term="不可复用的专名",
        replacement_hint=None,
        source="test",
        scope="generation",
    )
    binding = repo.create_binding(
        binding_id=binding_id,
        profile_id=profile_id,
        scope="project",
        scope_ref_id=f"contract_project_{seed}",
        task_type=task_type,
        strategy=strategy,
        config_json={"intensity": 70},
        status="active",
    )
    session.flush()
    return SimpleNamespace(
        repo=repo,
        book_id=book_id,
        profile_id=profile_id,
        binding_id=binding_id,
        quote_id=quote_id,
        quote_text=quote_text,
        project_id=f"contract_project_{seed}",
        binding=binding,
    )


def test_contract_is_hashed_tamper_evident_and_contains_no_raw_quote(session) -> None:
    seeded = _seed_reference(session, seed="hash", strategy="B")
    contract = build_style_runtime_contract(
        seeded.repo,
        [seeded.binding],
        task_type="scene_generation",
    )

    assert contract is not None
    assert contract["contract_version"] == STYLE_RUNTIME_CONTRACT_VERSION
    assert validate_style_runtime_contract(contract) == contract
    serialized = json.dumps(contract, ensure_ascii=False)
    assert seeded.quote_text not in serialized
    assert "这段未来原文字段绝不能进入运行契约" not in serialized
    assert contract["layers"][0]["profile"]["profile_json"][
        "qualitative_summary"
    ] == "克制观察，动作先于解释。"
    assert contract["layers"][0]["sample_quote_refs"] == [
        {
            "quote_id": seeded.quote_id,
            "quote_sha256": hashlib.sha256(
                seeded.quote_text.encode("utf-8")
            ).hexdigest(),
        }
    ]
    assert contract["layers"][0]["sample_paragraph_refs"] == []

    tampered = copy.deepcopy(contract)
    tampered["layers"][0]["profile"]["profile_json"]["style_features"] = [
        "篡改后的风格"
    ]
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_style_runtime_contract(tampered)


def test_contract_freezes_only_generation_safe_forbidden_findings(session) -> None:
    seeded = _seed_reference(session, seed="safe_forbidden_contract", strategy="A")
    profile = seeded.repo.get_profile(seeded.profile_id)
    profile.profile_json = {
        **dict(profile.profile_json or {}),
        "generation_safe_forbidden_findings": [
            {
                "finding_id": "safe_forbidden_1",
                "sub_dimension": "language.vocabulary",
                "statement": "避免复用参考文本中的具体措辞",
                "status": "approved",
            }
        ],
        "source_overlap_filter": {"applied": True, "threshold_chars": 8},
    }
    session.flush()

    contract = build_style_runtime_contract(
        seeded.repo,
        [seeded.binding],
        task_type="scene_generation",
    )

    assert contract is not None
    assert contract["layers"][0]["forbidden_findings"] == [
        {
            "finding_id": "safe_forbidden_1",
            "sub_dimension": "language.vocabulary",
            "statement": "避免复用参考文本中的具体措辞",
            "status": "approved",
        }
    ]
    assert validate_style_runtime_contract(contract) == contract


def test_frozen_contract_render_does_not_follow_later_profile_or_binding_edits(
    session,
) -> None:
    seeded = _seed_reference(session, seed="frozen", strategy="A")
    contract = build_style_runtime_contract(
        seeded.repo,
        [seeded.binding],
        task_type="scene_generation",
    )
    assert contract is not None
    context = extract_style_generation_context(
        "中性草稿：她推开窗，确认街上已经没有人。",
        source_kind="generation_source",
        max_chars=2_000,
    )
    first_service = InjectionService(session)
    frozen_before = first_service.fragments_for_contract(
        contract,
        project_id=seeded.project_id,
        context=context,
    ).to_system_prompt_prefix()

    profile = seeded.repo.get_profile(seeded.profile_id)
    profile.profile_json = {
        **dict(profile.profile_json or {}),
        "style_features": ["后来被修改的实时风格"],
    }
    seeded.binding.strategy = "C"
    seeded.binding.config_json = {"intensity": 5}
    session.flush()

    second_service = InjectionService(session)
    frozen_after = second_service.fragments_for_contract(
        contract,
        project_id=seeded.project_id,
        context=context,
    ).to_system_prompt_prefix()
    live_after = (
        InjectionService(session)
        .fragments_for(
            seeded.project_id,
            "scene_generation",
        )
        .to_system_prompt_prefix()
    )

    assert frozen_after == frozen_before
    assert "句式舒展，收束克制" in frozen_after
    assert "后来被修改的实时风格" not in frozen_after
    assert "后来被修改的实时风格" in live_after
    assert (
        second_service.last_runtime_audit["contract_hash"] == contract["contract_hash"]
    )
    assert second_service.last_runtime_audit["context"] == context.audit_dict()
    assert context.query_text not in json.dumps(
        second_service.last_runtime_audit,
        ensure_ascii=False,
    )


def test_frozen_few_shot_requires_unchanged_quote_and_current_send_rights(
    session,
) -> None:
    seeded = _seed_reference(session, seed="quote", strategy="B")
    contract = build_style_runtime_contract(
        seeded.repo,
        [seeded.binding],
        task_type="scene_generation",
    )
    assert contract is not None
    context = extract_style_generation_context(
        "她在门外停步。",
        source_kind="generation_source",
    )

    original = InjectionService(session).fragments_for_contract(
        contract,
        project_id=seeded.project_id,
        context=context,
    )
    assert seeded.quote_text in original.few_shot_block

    quote = seeded.repo.get_quote(seeded.quote_id)
    quote.quote_text = "被修改后的参考原句不应进入提示词。"
    session.flush()
    changed = InjectionService(session).fragments_for_contract(
        contract,
        project_id=seeded.project_id,
        context=context,
    )
    assert changed.few_shot_block == ""

    quote.quote_text = seeded.quote_text
    book = seeded.repo.get_book(seeded.book_id)
    book.stats_json = {"rights_declaration": {"declared": True, "send_rights": False}}
    session.flush()
    revoked = InjectionService(session).fragments_for_contract(
        contract,
        project_id=seeded.project_id,
        context=context,
    )
    assert revoked.few_shot_block == ""


def test_frozen_few_shot_prefers_hashed_complete_parent_paragraph(session) -> None:
    seeded = _seed_reference(session, seed="paragraph", strategy="B")
    paragraph_id = "contract_paragraph_parent"
    paragraph_text = (
        "檐下的人没有立即进屋，只把湿伞靠在墙角。"
        + seeded.quote_text
        + "院门外又响了一阵水声，他等那声音过去，才慢慢抬手拨亮灯芯。"
    )
    seeded.repo.create_paragraph(
        paragraph_id=paragraph_id,
        book_id=seeded.book_id,
        paragraph_index=0,
        paragraph_type="narration",
        start_offset=0,
        end_offset=len(paragraph_text),
        text=paragraph_text,
        char_count=len(paragraph_text),
        classifier_confidence=0.9,
    )
    quote = seeded.repo.get_quote(seeded.quote_id)
    quote.paragraph_id = paragraph_id
    session.flush()

    contract = build_style_runtime_contract(
        seeded.repo,
        [seeded.binding],
        task_type="scene_generation",
    )

    assert contract is not None
    layer = contract["layers"][0]
    assert layer["sample_quote_refs"][0]["paragraph_id"] == paragraph_id
    assert layer["sample_paragraph_refs"] == [
        {
            "paragraph_id": paragraph_id,
            "paragraph_sha256": hashlib.sha256(
                paragraph_text.encode("utf-8")
            ).hexdigest(),
        }
    ]
    assert paragraph_text not in json.dumps(contract, ensure_ascii=False)

    context = extract_style_generation_context(
        "她在门外停步。", source_kind="generation_source"
    )
    original = InjectionService(session).fragments_for_contract(
        contract,
        project_id=seeded.project_id,
        context=context,
    )
    assert "完整参考段落" in original.few_shot_block
    assert "院门外又响了一阵水声" in original.few_shot_block

    paragraph = seeded.repo.get_paragraph(paragraph_id)
    paragraph.text = seeded.quote_text + "这段父段落后来被改过。"
    session.flush()
    changed = InjectionService(session).fragments_for_contract(
        contract,
        project_id=seeded.project_id,
        context=context,
    )
    assert "院门外又响了一阵水声" not in changed.few_shot_block
    assert seeded.quote_text in changed.few_shot_block


def test_frozen_rag_requires_unchanged_reference_book_checksum(
    session,
    monkeypatch,
) -> None:
    from novel_system.services.style_reference.rag import RagRetriever, RagSnippet

    seeded = _seed_reference(session, seed="rag_checksum", strategy="C")
    contract = build_style_runtime_contract(
        seeded.repo,
        [seeded.binding],
        task_type="scene_generation",
    )
    assert contract is not None
    monkeypatch.setattr(
        RagRetriever,
        "retrieve",
        lambda self, profile_id, query: [
            RagSnippet(
                snippet_id="frozen_rag_sample",
                text="一段只用于校验冻结来源版本的检索样例。",
                granularity="paragraph",
                paragraph_type="narration",
                score=0.9,
            )
        ],
    )
    context = extract_style_generation_context(
        "她在门外停步。",
        source_kind="generation_source",
    )

    original = InjectionService(session).fragments_for_contract(
        contract,
        project_id=seeded.project_id,
        context=context,
    )
    assert "冻结来源版本" in original.rag_block

    book = seeded.repo.get_book(seeded.book_id)
    book.text_checksum = "changed-after-bundle-freeze"
    session.flush()
    changed = InjectionService(session).fragments_for_contract(
        contract,
        project_id=seeded.project_id,
        context=context,
    )
    assert changed.rag_block == ""


def test_frozen_validation_uses_frozen_terms_and_rejects_changed_source(
    session,
) -> None:
    seeded = _seed_reference(session, seed="validation_inputs", strategy="A")
    contract = build_style_runtime_contract(
        seeded.repo,
        [seeded.binding],
        task_type="scene_generation",
    )
    assert contract is not None
    profiles = contract_profile_objects(contract)

    term = seeded.repo.list_banned_terms(
        seeded.profile_id,
        scope="generation",
    )[0]
    term.term = "后来才加入的实时禁用词"
    session.flush()
    report = run_sync_validate_profiles("她说出不可复用的专名。", profiles, session)

    assert [hit["pattern_statement"] for hit in report.forbidden_hits_json] == [
        "不可复用的专名"
    ]
    book = seeded.repo.get_book(seeded.book_id)
    book.text_checksum = "changed-before-validation"
    session.flush()
    with pytest.raises(ValueError, match="source changed"):
        run_sync_validate_profiles("她停下脚步。", profiles, session)


def test_task_specific_bundle_contract_and_scene_injection_context_are_auditable(
    session,
) -> None:
    seeded = _seed_reference(session, seed="tasks", strategy="A")
    long_binding = seeded.repo.create_binding(
        binding_id="contract_binding_tasks_long",
        profile_id=seeded.profile_id,
        scope="project",
        scope_ref_id=seeded.project_id,
        task_type="long_form_continuation",
        strategy="A",
        config_json={},
        status="active",
    )
    session.flush()
    scene_contract = build_style_runtime_contract(
        seeded.repo,
        [seeded.binding],
        task_type="scene_generation",
    )
    long_contract = build_style_runtime_contract(
        seeded.repo,
        [long_binding],
        task_type="long_form_continuation",
    )
    assert scene_contract is not None and long_contract is not None
    bundle = {
        "snapshot": {
            "inline_digests": {
                "_style_reference_runtime_contract": json.dumps(scene_contract),
                "_style_reference_runtime_contract_long_form_continuation": json.dumps(
                    long_contract
                ),
            }
        }
    }
    assert (
        style_runtime_contract_from_bundle(bundle)["contract_hash"]
        == scene_contract["contract_hash"]
    )
    assert (
        style_runtime_contract_from_bundle(
            bundle,
            task_type="long_form_continuation",
        )["contract_hash"]
        == long_contract["contract_hash"]
    )

    session.add_all(
        [
            StoryProject(
                project_id=seeded.project_id,
                title="冻结契约项目",
                outline_text="",
            ),
            ChapterGoal(
                chapter_id="contract_chapter_tasks",
                project_id=seeded.project_id,
                chapter_goal="她必须确认门外是谁。",
            ),
            SceneCard(
                scene_id="contract_scene_tasks",
                project_id=seeded.project_id,
                chapter_id="contract_chapter_tasks",
                scene_seq=1,
                scene_goal="确认门外来客的身份。",
                onstage_chars_json=[],
            ),
            SceneRunState(scene_id="contract_scene_tasks"),
        ]
    )
    session.flush()
    neutral = "她先听见两下敲门声，随后把手按在没有点亮的灯罩上。"
    injected = SceneGenerationService(session)._inject_style_reference(
        {"system_prompt": "基础系统提示。"},
        session.get(SceneCard, "contract_scene_tasks"),
        task_type="scene_generation",
        bundle=bundle,
        context_text=neutral,
    )
    audit = injected["_style_reference_runtime_audit"]
    assert audit["contract_hash"] == scene_contract["contract_hash"]
    assert (
        audit["context"]["query_sha256"]
        == hashlib.sha256(neutral.encode("utf-8")).hexdigest()
    )
    assert neutral not in json.dumps(audit, ensure_ascii=False)


def test_qc_gate_validates_the_frozen_contract_profiles(session, monkeypatch) -> None:
    seeded = _seed_reference(session, seed="qc", strategy="A")
    contract = build_style_runtime_contract(
        seeded.repo,
        [seeded.binding],
        task_type="scene_generation",
    )
    assert contract is not None
    project = StoryProject(
        project_id=seeded.project_id,
        title="质检契约项目",
        outline_text="",
    )
    chapter = ChapterGoal(
        chapter_id="contract_chapter_qc",
        project_id=seeded.project_id,
        chapter_goal="验证同源质检。",
    )
    scene = SceneCard(
        scene_id="contract_scene_qc",
        project_id=seeded.project_id,
        chapter_id=chapter.chapter_id,
        scene_seq=1,
        scene_goal="验证冻结画像。",
        onstage_chars_json=[],
    )
    session.add_all(
        [
            project,
            chapter,
            scene,
            SceneRunState(
                scene_id=scene.scene_id,
                current_bundle_id="contract_bundle_qc",
                current_bundle_hash="qc-hash",
            ),
            SceneBundle(
                bundle_id="contract_bundle_qc",
                scene_id=scene.scene_id,
                chapter_id=chapter.chapter_id,
                execution_mode="P2",
                bundle_snapshot_hash="qc-hash",
                frozen_snapshot_json={
                    "inline_digests": {
                        "_style_reference_runtime_contract": json.dumps(contract)
                    }
                },
            ),
        ]
    )
    session.flush()
    profile = seeded.repo.get_profile(seeded.profile_id)
    profile.profile_json = {
        **dict(profile.profile_json or {}),
        "style_features": ["不应被本次质检读取的实时修改"],
    }
    session.flush()

    captured: dict[str, object] = {}

    def fake_validate(text, profiles, current_session):
        captured["text"] = text
        captured["profiles"] = profiles
        captured["session"] = current_session
        return SimpleNamespace(verdict=SimpleNamespace(value="pass"))

    monkeypatch.setattr(
        "novel_system.services.style_reference.validation.run_sync_validate_profiles",
        fake_validate,
    )

    verdict = HardQcEngine(session)._apply_style_validation_gate(
        scene,
        "待质检正文。",
    )

    assert verdict == "pass"
    assert captured["session"] is session
    frozen_profile = captured["profiles"][0]
    assert frozen_profile.profile_json["style_features"] == ["句式舒展，收束克制"]


def test_layered_baseline_blends_mean_total_variance_and_validation_target(
    session,
) -> None:
    base = SimpleNamespace(
        profile_id="base",
        book_id="",
        profile_json={
            "metrics_baseline": {"avg_sentence_length": {"mean": 10.0, "std": 1.0}}
        },
    )
    specific = SimpleNamespace(
        profile_id="specific",
        book_id="",
        profile_json={
            "metrics_baseline": {"avg_sentence_length": {"mean": 20.0, "std": 2.0}}
        },
    )

    blended = blend_profile_metric_baselines([base, specific])
    assert blended["avg_sentence_length"]["mean"] == pytest.approx(50.0 / 3.0)
    assert blended["avg_sentence_length"]["std"] > 2.0
    report = run_sync_validate_profiles("她停下。门开了。", [base, specific], session)
    target = next(
        item
        for item in report.quantitative_json
        if item["metric"] == "avg_sentence_length"
    )
    assert target["target_mean"] == pytest.approx(50.0 / 3.0)


def test_context_extractor_is_bounded_normalized_and_audit_contains_only_hash() -> None:
    context = extract_style_generation_context(
        "前文\r\n\x00后文" + "甲" * 30,
        source_kind="continuation_tail",
        max_chars=12,
    )

    assert context.query_text == "甲" * 12
    assert context.char_count == 12
    assert set(context.audit_dict()) == {
        "version",
        "source_kind",
        "query_sha256",
        "char_count",
    }
    assert context.query_text not in json.dumps(
        context.audit_dict(), ensure_ascii=False
    )


def test_contract_aware_bundle_never_falls_back_to_a_later_live_binding(
    session,
) -> None:
    seeded = _seed_reference(session, seed="no_fallback", strategy="A")
    contract = build_style_runtime_contract(
        seeded.repo,
        [seeded.binding],
        task_type="scene_generation",
    )
    assert contract is not None
    scene = SceneCard(
        scene_id="contract_scene_no_fallback",
        project_id=seeded.project_id,
        chapter_id="contract_chapter_no_fallback",
        scene_seq=1,
        scene_goal="验证冻结空状态。",
        onstage_chars_json=[],
    )
    base = {"system_prompt": "BASE", "user_prompt": "USER"}
    absent_bundle = {
        "snapshot": {
            "source_version_refs": {
                "style_reference_runtime_contract_version": STYLE_RUNTIME_CONTRACT_VERSION,
                "style_reference_runtime_contract_status": "absent",
            },
            "inline_digests": {},
        }
    }
    degraded_bundle = copy.deepcopy(absent_bundle)
    degraded_bundle["snapshot"]["source_version_refs"][
        "style_reference_runtime_contract_status"
    ] = "degraded"
    conflicting_bundle = copy.deepcopy(absent_bundle)
    conflicting_bundle["snapshot"]["inline_digests"][
        "_style_reference_runtime_contract"
    ] = contract

    absent = SceneGenerationService(session)._inject_style_reference(
        base,
        scene,
        bundle=absent_bundle,
        context_text="中性草稿。",
    )
    degraded = SceneGenerationService(session)._inject_style_reference(
        base,
        scene,
        bundle=degraded_bundle,
        context_text="中性草稿。",
    )
    conflicting = SceneGenerationService(session)._inject_style_reference(
        base,
        scene,
        bundle=conflicting_bundle,
        context_text="中性草稿。",
    )

    assert style_runtime_contract_status_from_bundle(absent_bundle) == "absent"
    assert resolve_style_runtime_contract_state(absent_bundle).mode == "absent"
    assert absent is base
    assert "句式舒展，收束克制" not in absent["system_prompt"]
    assert degraded["system_prompt"] == "BASE"
    assert degraded["_style_reference_runtime_audit"]["outcome"] == "degraded"
    assert "句式舒展，收束克制" not in degraded["system_prompt"]
    assert conflicting["system_prompt"] == "BASE"
    assert conflicting["_style_reference_runtime_audit"]["error_code"] == (
        "runtime_contract_status_conflict"
    )

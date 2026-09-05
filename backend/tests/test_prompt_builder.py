from __future__ import annotations

from copy import deepcopy

from novel_system.db.models import (
    ChapterGoal,
    FinalScene,
    SceneCard,
    SceneMemory,
    SceneRunState,
    StoryProject,
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceProfile,
    StyleReferenceRun,
)
from novel_system.services.bundle_builder import BundleBuilder
from novel_system.services.prompt_builder import (
    PromptBuilder,
    PromptConfigurationError,
    load_prompt_templates,
)


def _bundle_snapshot() -> dict:
    return {
        "contract_version": "BSHASH_v1",
        "stage_allowlist_name": "bundle_build_allowlist_v1",
        "scene_id": "CH001_SC01",
        "chapter_id": "CH001",
        "source_version_refs": {
            "chapter_goal": "CH001",
            "scene_card": "CH001_SC01",
            "style_observation_ids": ["STY_SCENE_01", "STY_SCENE_02"],
        },
        "resolved_ref_ids": {
            "relation_ids": ["REL_A_B"],
            "world_rule_ids": ["WR_GLOBAL_014"],
            "open_foreshadow_ids": ["F014"],
        },
        "ordered_injections": [
            {"slot": "chapter_goal", "ref_id": "CH001", "digest_key": "chapter_goal"},
            {"slot": "scene_card", "ref_id": "CH001_SC01", "digest_key": "scene_card"},
            {
                "slot": "style_observations",
                "ref_id": "STY_SCENE_01",
                "digest_key": "style_observation",
            },
        ],
        "inline_digests": {
            "chapter_goal": "Close the reunion chapter with a traceable reveal.",
            "scene_card": "Reunite the leads and turn the old letter into immediate action.",
            "character_contract": (
                '{"contract_version":"CHARACTER_CONTRACT_v1","characters":'
                '[{"character_id":"CHAR_A","display_name":"Mira","pronouns":["she"],'
                '"role":"archivist","aliases":["M"]}]}'
            ),
            "voice_card": "Short clipped lines; pressure makes the tone harder.",
            "style_rule": "Keep emotion in gesture and pause.",
            "banned_rule": "Do not explain the whole backstory at reunion time.",
            "style_observation": (
                "Gesture before explanation. Let silence carry accusation. "
                "End paragraphs on pressure, not exposition. Keep the emotional turn tactile."
            ),
            "calibration_line": "The door closed like a sentence left unfinished.",
            "relation_card": "Reunion tension; B knows slightly more than A.",
            "world_rule": "Public spellcasting inside the city is forbidden.",
            "foreshadow": "The old letter sender clue is now in play.",
            "scene_memory": "Previous scene memory digest about the hidden sender.",
            "scene_summary": "Current scene summary digest about the reunion beat.",
            "chapter_summary": "Chapter summary digest about guarded trust replacing suspicion.",
            "similar_scene": (
                "Similar-scene reference: another gate reunion leaned too heavily on explanation "
                "and lost pressure halfway through."
            ),
        },
    }


def test_bundle_snapshot_carries_active_reference_profile_provenance(session) -> None:
    session.add(
        StoryProject(
            project_id="PROJ_REF_PROV",
            title="Reference provenance",
            outline_text="",
            planning_mode="snowflake",
        )
    )
    session.add(
        ChapterGoal(
            chapter_id="CH_REF_PROV",
            project_id="PROJ_REF_PROV",
            planned_scene_count=1,
            chapter_goal="Keep reference provenance in the frozen bundle.",
        )
    )
    session.add(
        SceneCard(
            scene_id="CH_REF_PROV_SC01",
            chapter_id="CH_REF_PROV",
            project_id="PROJ_REF_PROV",
            scene_seq=1,
            onstage_chars_json=[],
            scene_goal="Draft an original scene with auditable reference provenance.",
        )
    )
    session.add(SceneRunState(scene_id="CH_REF_PROV_SC01"))
    session.add(
        StyleReferenceBook(
            book_id="sr_book_ref_prov",
            title="Public domain source",
            source_kind="path",
            cloud_policy="segments_only",
            text_checksum="checksum-ref-prov",
            stats_json={"rights_declaration": {"declared": True, "send_rights": True}},
        )
    )
    session.add(
        StyleReferenceRun(
            run_id="sr_run_ref_prov", book_id="sr_book_ref_prov", status="done"
        )
    )
    session.add(
        StyleReferenceProfile(
            profile_id="sr_profile_ref_prov",
            book_id="sr_book_ref_prov",
            run_id="sr_run_ref_prov",
            title="Audited profile",
            status="active",
            profile_json={"style_features": ["abstract craft only"]},
        )
    )
    session.add(
        StyleReferenceInjectionBinding(
            binding_id="sr_bind_ref_prov",
            profile_id="sr_profile_ref_prov",
            scope="project",
            scope_ref_id="PROJ_REF_PROV",
            task_type="scene_generation",
            strategy="A",
            status="active",
        )
    )
    session.commit()

    snapshot = BundleBuilder(session).build("CH_REF_PROV_SC01")["snapshot"]

    assert snapshot["source_version_refs"]["reference_profile_ids"] == [
        "sr_profile_ref_prov"
    ]
    from novel_system.services.style_reference.runtime_contract import (
        style_runtime_contract_from_bundle,
    )

    contract = style_runtime_contract_from_bundle(snapshot)
    assert contract is not None
    assert contract["profile_ids"] == ["sr_profile_ref_prov"]
    assert contract["binding_ids"] == ["sr_bind_ref_prov"]
    assert (
        snapshot["source_version_refs"]["style_reference_runtime_contract_hash"]
        == contract["contract_hash"]
    )
    assert (
        snapshot["source_version_refs"]["style_reference_runtime_contract_status"]
        == "frozen"
    )


def test_prompt_builder_hash_changes_only_for_relevant_inputs() -> None:
    builder = PromptBuilder()
    baseline_snapshot = _bundle_snapshot()
    irrelevant_change = deepcopy(baseline_snapshot)
    relevant_change = deepcopy(baseline_snapshot)

    irrelevant_change["source_version_refs"]["debug_timestamp"] = "2026-04-14T21:00:00Z"
    relevant_change["inline_digests"][
        "scene_card"
    ] = "The leads reunite, but the letter clue stays buried."

    baseline = builder.build(baseline_snapshot, "neutral_draft")
    same_hash = builder.build(irrelevant_change, "neutral_draft")
    changed_hash = builder.build(relevant_change, "neutral_draft")

    assert baseline["prompt_hash"] == same_hash["prompt_hash"]
    assert baseline["prompt_hash"] != changed_hash["prompt_hash"]


def test_prompt_builder_enforces_budget_using_rendered_prompt_shape() -> None:
    builder = PromptBuilder()
    snapshot = _bundle_snapshot()

    baseline = builder.build(snapshot, "neutral_draft")
    threshold = baseline["token_budget"]["estimated_input_tokens"] - 1

    payload = builder.build(snapshot, "neutral_draft", max_input_tokens=threshold)

    assert payload["token_budget"]["estimated_input_tokens"] <= threshold
    assert (
        payload["token_budget"]["section_status"]["similar_scene_context"]["status"]
        == "omitted"
    )


def test_prompt_builder_returns_isolated_schema_copies() -> None:
    builder = PromptBuilder()
    snapshot = _bundle_snapshot()

    original = builder.build(snapshot, "neutral_draft")
    original_hash = original["prompt_hash"]
    original["structured_schema"]["required"].append("mutated_field")
    original["structured_schema"]["properties"]["scene_text"]["type"] = "array"

    repeated = builder.build(snapshot, "neutral_draft")

    assert repeated["prompt_hash"] == original_hash
    assert repeated["structured_schema"]["required"] == ["scene_text"]
    assert repeated["structured_schema"]["properties"]["scene_text"]["type"] == "string"


def test_prompt_builder_injects_literary_freshness_budget() -> None:
    builder = PromptBuilder()
    snapshot = _bundle_snapshot()
    snapshot["inline_digests"][
        "literary_freshness_budget"
    ] = """
{
  "schema_version": "literary_freshness_budget_v1",
  "avoid_action_templates": ["pronoun_looked_at_object_then_silence"],
  "avoid_image_fields": ["钥匙"],
  "avoid_summary_endings": ["一切都变了"]
}
""".strip()

    payload = builder.build(snapshot, "style_draft")

    assert "## Literary Freshness Budget" in payload["user_prompt"]
    assert "pronoun_looked_at_object_then_silence" in payload["user_prompt"]
    assert "avoid_summary_endings" in payload["user_prompt"]
    assert "literary_freshness_budget" in payload["token_budget"]["included_sections"]


def test_chapter_summary_schema_requires_carry_forward() -> None:
    payload = PromptBuilder().build(_bundle_snapshot(), "chapter_summary")

    assert payload["structured_schema"]["required"] == ["summary", "carry_forward"]


def test_writer_passage_patch_schema_is_manual_only_and_targeted() -> None:
    payload = PromptBuilder().build(_bundle_snapshot(), "writer_passage_patch")

    assert payload["structured_schema"]["required"] == [
        "patches",
        "rationale",
        "manual_only",
    ]
    patch_schema = payload["structured_schema"]["properties"]["patches"]["items"]
    assert patch_schema["required"] == [
        "target_text_ref",
        "source_excerpt",
        "replacement_text",
        "patch_type",
        "changed_dimensions",
        "why_it_helps",
    ]
    assert (
        "Required top-level JSON keys: patches, rationale, manual_only"
        in payload["user_prompt"]
    )


def test_hard_qc_uses_runtime_minimum_budget_for_default_runs(tmp_path) -> None:
    prompt_path = tmp_path / "prompts.yaml"
    prompt_path.write_text(
        """
templates:
  hard_qc:
    version: "test"
    input_token_budget: 60
    system_prompt: "system"
    task_prompt: "task"
    structured_schema:
      type: object
      additionalProperties: false
      required:
        - resolution_code
        - pass_flag
        - next_action
        - issues
      properties:
        resolution_code:
          type: string
        pass_flag:
          type: boolean
        next_action:
          type: string
        issues:
          type: array
          items:
            type: object
        rewrite_brief:
          type: array
          items:
            type: string
""".strip(),
        encoding="utf-8",
    )
    builder = PromptBuilder(prompt_path)

    default_payload = builder.build(_bundle_snapshot(), "hard_qc")
    explicit_payload = builder.build(_bundle_snapshot(), "hard_qc", max_input_tokens=60)

    assert default_payload["token_budget"]["target_input_tokens"] >= 3200
    assert explicit_payload["token_budget"]["target_input_tokens"] == 60


def test_prompt_builder_passes_template_task_kind_to_context_budget() -> None:
    builder = PromptBuilder()

    hard_qc = builder.build(_bundle_snapshot(), "hard_qc", max_input_tokens=120)
    drafting = builder.build(_bundle_snapshot(), "style_draft", max_input_tokens=120)
    chapter_review = builder.build(
        _bundle_snapshot(), "chapter_summary", max_input_tokens=120
    )

    assert hard_qc["token_budget"]["task_kind"] == "hard_qc"
    assert (
        "drop_style_context_before_fact_context"
        in hard_qc["token_budget"]["continuity_policy"]
    )
    assert drafting["token_budget"]["task_kind"] == "drafting"
    assert (
        "preserve_style_profile_author_preference_and_calibration"
        in drafting["token_budget"]["continuity_policy"]
    )
    assert chapter_review["token_budget"]["task_kind"] == "chapter_review"
    assert (
        "preserve_chapter_promise_payoff_and_memory"
        in chapter_review["token_budget"]["continuity_policy"]
    )


def test_hard_qc_schema_requires_rewrite_brief_for_runtime_validator() -> None:
    payload = PromptBuilder().build(_bundle_snapshot(), "hard_qc")

    assert payload["structured_schema"]["required"] == [
        "resolution_code",
        "pass_flag",
        "next_action",
        "issues",
        "rewrite_brief",
    ]
    assert (
        "Required top-level JSON keys: resolution_code, pass_flag, next_action, issues, rewrite_brief"
        in payload["user_prompt"]
    )


def test_load_prompt_templates_rejects_invalid_config(tmp_path) -> None:
    missing_field_path = tmp_path / "prompts_missing.yaml"
    missing_field_path.write_text(
        """
templates:
  neutral_draft:
    version: "2026-04-14.v1"
    input_token_budget: 2600
    system_prompt: "system"
    structured_schema: {}
""".strip(),
        encoding="utf-8",
    )

    wrong_type_path = tmp_path / "prompts_wrong_type.yaml"
    wrong_type_path.write_text(
        """
templates:
  neutral_draft:
    version: 20260414
    input_token_budget: "2600"
    system_prompt: "system"
    task_prompt: "task"
    structured_schema: {}
""".strip(),
        encoding="utf-8",
    )

    try:
        load_prompt_templates(missing_field_path)
    except PromptConfigurationError as exc:
        assert (
            str(exc) == "template neutral_draft is missing required fields: task_prompt"
        )
    else:
        raise AssertionError("expected missing-field prompt config to be rejected")

    try:
        load_prompt_templates(wrong_type_path)
    except PromptConfigurationError as exc:
        assert str(exc) == "template neutral_draft.version must be a string"
    else:
        raise AssertionError("expected wrong-type prompt config to be rejected")


def test_load_prompt_templates_rejects_invalid_structured_schema_shape(
    tmp_path,
) -> None:
    invalid_schema_path = tmp_path / "prompts_invalid_schema.yaml"
    invalid_schema_path.write_text(
        """
templates:
  neutral_draft:
    version: "2026-04-14.v1"
    input_token_budget: 2600
    system_prompt: "system"
    task_prompt: "task"
    structured_schema:
      type: array
      properties: []
      required: scene_text
""".strip(),
        encoding="utf-8",
    )

    try:
        load_prompt_templates(invalid_schema_path)
    except PromptConfigurationError as exc:
        assert (
            str(exc) == "template neutral_draft.structured_schema.type must be 'object'"
        )
    else:
        raise AssertionError("expected invalid structured_schema shape to be rejected")


def test_load_prompt_templates_rejects_unsupported_structured_schema_type(
    tmp_path,
) -> None:
    invalid_schema_path = tmp_path / "prompts_invalid_schema_type.yaml"
    invalid_schema_path.write_text(
        """
templates:
  neutral_draft:
    version: "2026-04-14.v1"
    input_token_budget: 2600
    system_prompt: "system"
    task_prompt: "task"
    structured_schema:
      type: object
      additionalProperties: false
      required:
        - scene_text
      properties:
        scene_text:
          type: dictionary
""".strip(),
        encoding="utf-8",
    )

    try:
        load_prompt_templates(invalid_schema_path)
    except PromptConfigurationError as exc:
        assert str(exc) == (
            "template neutral_draft.structured_schema.properties.scene_text.type "
            "has unsupported value dictionary"
        )
    else:
        raise AssertionError(
            "expected unsupported structured_schema type to be rejected"
        )


def test_load_prompt_templates_rejects_required_fields_missing_from_properties_when_closed(
    tmp_path,
) -> None:
    invalid_schema_path = tmp_path / "prompts_invalid_required.yaml"
    invalid_schema_path.write_text(
        """
templates:
  neutral_draft:
    version: "2026-04-14.v1"
    input_token_budget: 2600
    system_prompt: "system"
    task_prompt: "task"
    structured_schema:
      type: object
      additionalProperties: false
      required:
        - scene_text
        - continuity_notes
      properties:
        scene_text:
          type: string
""".strip(),
        encoding="utf-8",
    )

    try:
        load_prompt_templates(invalid_schema_path)
    except PromptConfigurationError as exc:
        assert str(exc) == (
            "template neutral_draft.structured_schema.required contains entries not declared in properties: "
            "continuity_notes"
        )
    else:
        raise AssertionError(
            "expected closed-schema required/property mismatch to be rejected"
        )


def test_bundle_builder_scene_digest_includes_operational_scene_constraints(
    session,
) -> None:
    session.add(
        ChapterGoal(
            chapter_id="CH901",
            planned_scene_count=1,
            chapter_goal="Open the trial with a visible cost.",
        )
    )
    session.add(
        SceneCard(
            scene_id="CH901_SC01",
            chapter_id="CH901",
            scene_seq=1,
            location="Moon bridge",
            scene_goal="Test the initiate without copying source material.",
            beats_json=["arrival", "seal wakes", "choice under pressure"],
            must_include_text="the spirit seal glows like cold jade",
            forbidden_text="Do not use source names.",
            exit_change="The mountain gate answers.",
            hook="continue",
            target_length_band="short",
            scene_type="cultivation_trial",
        )
    )
    session.add(SceneRunState(scene_id="CH901_SC01"))
    session.commit()

    snapshot = BundleBuilder(session).build("CH901_SC01")["snapshot"]
    scene_digest = snapshot["inline_digests"]["scene_card"]

    assert "Goal: Test the initiate without copying source material." in scene_digest
    assert "Location: Moon bridge" in scene_digest
    assert "Beats: arrival; seal wakes; choice under pressure" in scene_digest
    assert (
        "Required beats to weave naturally: the spirit seal glows like cold jade"
        in scene_digest
    )
    assert "Forbidden text: Do not use source names." in scene_digest
    assert "Exit change: The mountain gate answers." in scene_digest
    assert "Hook: continue" in scene_digest
    assert "Target length: short" in scene_digest


def test_bundle_builder_uses_only_prior_scene_memory(session) -> None:
    session.add(
        ChapterGoal(
            chapter_id="CH902",
            planned_scene_count=2,
            chapter_goal="Move from first sign to second choice.",
        )
    )
    session.add_all(
        [
            SceneCard(
                scene_id="CH902_SC01",
                chapter_id="CH902",
                scene_seq=1,
                onstage_chars_json=[],
                scene_goal="Open the chapter.",
            ),
            SceneCard(
                scene_id="CH902_SC02",
                chapter_id="CH902",
                scene_seq=2,
                onstage_chars_json=[],
                scene_goal="Continue after the first result.",
            ),
            SceneRunState(scene_id="CH902_SC01"),
            SceneRunState(scene_id="CH902_SC02"),
            SceneMemory(
                row_id="scene_memory_CH902_SC01_v1",
                scene_id="CH902_SC01",
                chapter_id="CH902",
                content="prior scene memory",
                source_bundle_id="bundle_CH902_SC01_v1",
                final_scene_row_id="final_scene_CH902_SC01_v1",
                active_flag=1,
                created_at="2026-04-20T00:00:00+00:00",
            ),
            SceneMemory(
                row_id="scene_memory_CH902_SC02_v1",
                scene_id="CH902_SC02",
                chapter_id="CH902",
                content="current scene stale memory",
                source_bundle_id="bundle_CH902_SC02_v1",
                final_scene_row_id="final_scene_CH902_SC02_v1",
                active_flag=1,
                created_at="2026-04-20T01:00:00+00:00",
            ),
        ]
    )
    session.commit()

    first_snapshot = BundleBuilder(session).build("CH902_SC01")["snapshot"]
    second_snapshot = BundleBuilder(session).build("CH902_SC02")["snapshot"]

    assert "scene_memory" not in first_snapshot["inline_digests"]
    assert second_snapshot["source_version_refs"]["scene_memory_prev"] == "CH902_SC01"
    assert second_snapshot["inline_digests"]["scene_memory"] == "prior scene memory"


def test_bundle_builder_adds_literary_freshness_budget_from_prior_final_scenes(
    session,
) -> None:
    session.add(
        ChapterGoal(
            chapter_id="CH903",
            planned_scene_count=3,
            chapter_goal="Protect the witness without draining the chapter rhythm.",
        )
    )
    session.add_all(
        [
            SceneCard(
                scene_id="CH903_SC01",
                chapter_id="CH903",
                scene_seq=1,
                scene_goal="First exchange.",
            ),
            SceneCard(
                scene_id="CH903_SC02",
                chapter_id="CH903",
                scene_seq=2,
                scene_goal="Second exchange.",
            ),
            SceneCard(
                scene_id="CH903_SC03",
                chapter_id="CH903",
                scene_seq=3,
                scene_goal="Break the pattern.",
            ),
            SceneRunState(scene_id="CH903_SC01"),
            SceneRunState(scene_id="CH903_SC02"),
            SceneRunState(scene_id="CH903_SC03"),
            FinalScene(
                row_id="final_scene_CH903_SC01_v1",
                scene_id="CH903_SC01",
                chapter_id="CH903",
                content="林岑低头看着钥匙，沉默了片刻。雨敲着门。她必须选择公开。",
                source_bundle_id="bundle_CH903_SC01_v1",
                source_bundle_hash="hash_CH903_SC01_v1",
            ),
            FinalScene(
                row_id="final_scene_CH903_SC02_v1",
                scene_id="CH903_SC02",
                chapter_id="CH903",
                content="许望低头看着证据，沉默了片刻。雨又敲着门。他必须选择隐瞒。",
                source_bundle_id="bundle_CH903_SC02_v1",
                source_bundle_hash="hash_CH903_SC02_v1",
            ),
        ]
    )
    session.commit()

    snapshot = BundleBuilder(session).build("CH903_SC03")["snapshot"]

    budget = snapshot["inline_digests"]["literary_freshness_budget"]
    assert "literary_freshness_budget_v1" in budget
    assert "pronoun_looked_at_object_then_silence" in budget
    assert "avoid_summary_endings" in budget
    assert snapshot["source_version_refs"][
        "literary_freshness_source_final_scene_ids"
    ] == [
        "final_scene_CH903_SC01_v1",
        "final_scene_CH903_SC02_v1",
    ]

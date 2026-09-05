from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from novel_system.db.models import (
    AuthorPreferenceProfile,
    ChapterGoal,
    FinalScene,
    LlmCall,
    PassagePatchCandidate,
    ReviewItem,
    SceneCard,
    SceneRunState,
    StoryProject,
    WriterEvaluation,
)
from novel_system.services.author_drafts import AuthorDraftService
from novel_system.services.llm_client import LLMResponse, OnlineAccountedExecution
from novel_system.services.writer_deep_review import LITERARY_REVISION_RUBRIC_ID
from novel_system.services.writer_deep_review import WriterDeepReviewService
from novel_system.services.writer_deep_review import _infer_scene_form
from novel_system.services.writer_deep_review import _normalize_deep_review_output


import pytest as _pytest_wr
from tests.real_llm_fakes import install_online_writer_pipeline as _install_online_writer_pipeline


@_pytest_wr.fixture(autouse=True)
def _auto_online_writer(monkeypatch):
    """假生成已退役：作家评审/深评/passage-patch 走在线记账替身（按 node_id 派发）。"""
    _install_online_writer_pipeline(monkeypatch)


CHAPTER_ID = "DEEP_CH01"
SCENE_ID = "DEEP_CH01_SC01"
FINAL_ROW_ID = "final_DEEP_CH01_SC01"
PROJECT_ID = "PROJECT_DEEP_CH01"


def test_scene_form_inference_recognizes_chinese_atmosphere_markers() -> None:
    assert _infer_scene_form("雨夜里灯光晃了一下。") == "atmosphere_scene"
    assert _infer_scene_form("雪落在门前。") == "atmosphere_scene"


def test_normalize_deep_review_output_validates_model_lens_evaluations() -> None:
    normalized = _normalize_deep_review_output(
        {
            "overall_score": 0.6,
            "scores": {"choice_pressure": 0.5},
            "findings": [
                {"lens": "story", "dimension": "choice_pressure", "severity": "revision", "issue": "选择没有落成动作。"},
                {"lens": "reader", "dimension": "ending_drive", "severity": "taste", "issue": "结尾可以更硬。"},
            ],
            "revision_brief": [],
            "requires_human_review": False,
            "lens_evaluations": [
                {"lens": " Story ", "overall_score": 1.7, "scores": {"choice_pressure": 0.5, "bogus_dim": 0.1}, "findings": [{"dimension": "choice_pressure", "severity": "nonsense", "issue": "a"}]},
                {"lens": "story", "findings": [{"dimension": "dialogue_subtext", "severity": "taste", "issue": "b"}]},
                {"lens": "camera"},
                "garbage",
            ],
        }
    )
    by_lens = {entry["lens"]: entry for entry in normalized["lens_evaluations"]}
    # 非法镜头名整条丢弃，大小写/空白容错，重复镜头合并，漏掉的镜头从顶层 findings 重建
    assert set(by_lens) == {"story", "reader"}
    assert by_lens["story"]["overall_score"] == 1.0
    assert "bogus_dim" not in by_lens["story"]["scores"]
    merged = by_lens["story"]["findings"]
    assert [item["issue"] for item in merged] == ["a", "b"]
    assert all(item["lens"] == "story" for item in merged)
    assert merged[0]["severity"] == "revision"
    assert by_lens["reader"]["findings"][0]["issue"] == "结尾可以更硬。"
    assert len(by_lens["story"]["revision_brief"]) == 2


def test_normalize_deep_review_output_rebuilds_lenses_when_model_omits_grouping() -> None:
    normalized = _normalize_deep_review_output(
        {
            "overall_score": 0.7,
            "scores": {},
            "findings": [
                {"lens": "THEME", "dimension": "theme_pressure", "severity": "revision", "issue": "主题压力未落到选择上。"},
                {"lens": "看不懂", "dimension": "choice_pressure", "severity": "taste", "issue": "非法镜头归入 story。"},
            ],
            "revision_brief": [],
            "requires_human_review": False,
        }
    )
    by_lens = {entry["lens"]: entry for entry in normalized["lens_evaluations"]}
    assert set(by_lens) == {"story", "theme"}
    assert by_lens["theme"]["findings"][0]["issue"] == "主题压力未落到选择上。"
    assert by_lens["story"]["findings"][0]["lens"] == "story"


def _seed_finished_scene(session) -> None:
    session.add(StoryProject(project_id=PROJECT_ID, title="Deep review project", outline_text=""))
    session.add(
        ChapterGoal(
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            planned_scene_count=1,
            chapter_goal="林岑必须决定是否公开导师留下的失踪案证据。",
            main_plot_push="把旧档案线推进到公开真相的选择。",
            emotional_target="从职业克制转向道德压力。",
            ending_effect="读者知道她已经不能只做修复师。",
            writer_brief_json={
                "chapter_promise": "真相和保护幸存者不能同时满足。",
                "relationship_delta": "林岑开始不再完全信任许望的判断。",
                "ending_question": "她会把证据交给谁？",
            },
        )
    )
    session.add(
        SceneCard(
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            scene_seq=1,
            scene_goal="林岑发现关键录音，但必须决定公开还是隐藏。",
            beats_json=["修复录音", "听见幸存者编号", "决定暂缓公开"],
            exit_change="她把一半证据藏起来，准备独自核查。",
            hook="录音最后出现她自己的心跳声。",
            writer_brief_json={
                "character_desire": "确认导师留下的证据是否真实。",
                "choice_under_pressure": "公开证据或先保护幸存者。",
                "power_shift": "林岑从被动修复者变成证据持有人。",
                "reader_aftertaste": "她越冷静，越像在越界。",
            },
        )
    )
    session.add(
        SceneRunState(
            scene_id=SCENE_ID,
            scene_status="archived",
            current_final_scene_row_id=FINAL_ROW_ID,
            current_bundle_id="bundle_deep",
            current_bundle_hash="hash_deep",
        )
    )
    session.add(
        FinalScene(
            row_id=FINAL_ROW_ID,
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            content=(
                "林岑的手指在潮湿的档案盒边缘停顿。盐霜泛着幽蓝的冷光。"
                "她低声说证据还不能公开，然后又解释这是为了保护所有人。"
                "许望没有回答，录音里传来三声钟响。林岑的手指再次停顿。"
            ),
            status="approved",
            source_bundle_id="bundle_deep",
            source_bundle_hash="hash_deep",
        )
    )
    session.commit()


def test_scene_deep_review_creates_strict_literary_evaluation_and_theme_lens(client: TestClient, session) -> None:
    _seed_finished_scene(session)

    response = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review")

    assert response.status_code == 200
    payload = response.json()["data"]
    evaluation = payload["latest_evaluation"]

    assert payload["rubric_id"] == LITERARY_REVISION_RUBRIC_ID
    assert evaluation["rubric_id"] == LITERARY_REVISION_RUBRIC_ID
    assert evaluation["overall_score"] <= 0.85
    severities = {item["severity"] for item in evaluation["findings"]}
    assert {"blocking", "revision", "taste", "ignore_ok"}.issubset(severities)
    assert evaluation["scene_form"] in {
        "plot_scene",
        "atmosphere_scene",
        "relationship_scene",
        "revelation_scene",
        "transition_scene",
    }
    assert all(
        item.get("evidence_excerpt") is not None
        and item.get("why_it_matters")
        and item.get("recommendation")
        and item.get("scene_form")
        for item in evaluation["findings"]
    )
    assert "theme" in {item["lens"] for item in payload["lens_evaluations"]}
    assert any(item["dimension"] == "repetitive_expression" for item in evaluation["findings"])
    assert any(item["classification"] == "revision" for item in evaluation["revision_brief"])

    session.expire_all()
    aggregate_rows = session.query(WriterEvaluation).filter_by(
        object_type="scene",
        object_id=SCENE_ID,
        rubric_id=LITERARY_REVISION_RUBRIC_ID,
        parent_evaluation_id=None,
    ).all()
    assert len(aggregate_rows) == 1


def test_scene_deep_review_uses_llm_when_live(client: TestClient, session, monkeypatch) -> None:
    class FakeDeepReviewRunner:
        def __init__(self, db_session, **kwargs) -> None:
            self.session = db_session

        @property
        def provider_execution_mode(self):
            return "online"

        def run(self, **kwargs):
            assert kwargs["node_id"] == "writer_deep_review"
            context = kwargs["context"]
            assert context.scope_type == "scene"
            assert context.scope_id == SCENE_ID
            assert context.project_id == PROJECT_ID
            assert context.chapter_id == CHAPTER_ID
            assert context.scene_id == SCENE_ID
            return SimpleNamespace(
                llm_call_id="llm_call_writer_deep_review_test",
                response=SimpleNamespace(
                    structured_output={
                        "overall_score": 0.61,
                        "scores": {"choice_pressure": 0.55, "dialogue_subtext": 0.6},
                        "findings": [
                            {
                                "lens": "story",
                                "dimension": "choice_pressure",
                                "severity": "revision",
                                "classification": "revision",
                                "issue": "The choice is described rather than enacted.",
                                "recommendation": "Rewrite the choice as a visible action.",
                                "evidence_excerpt": "truth mattered",
                                "evidence_location": "scene body",
                                "why_it_matters": "Deep review must use textual evidence.",
                                "scene_form": "plot_scene",
                            }
                        ],
                        "revision_brief": [
                            {
                                "classification": "revision",
                                "dimension": "choice_pressure",
                                "recommendation": "Make the choice visible.",
                            }
                        ],
                        "lens_evaluations": [
                            {
                                "lens": "story",
                                "overall_score": 0.61,
                                "scores": {"choice_pressure": 0.55},
                                "findings": [],
                                "revision_brief": [],
                            }
                        ],
                    }
                ),
            )

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    monkeypatch.setattr("novel_system.services.writer_deep_review.LLMNodeRunner", FakeDeepReviewRunner)
    _seed_finished_scene(session)

    response = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review")

    assert response.status_code == 200
    evaluation = response.json()["data"]["latest_evaluation"]
    assert evaluation["evaluator_llm_call_id"] == "llm_call_writer_deep_review_test"
    assert evaluation["overall_score"] == 0.61
    assert evaluation["findings"][0]["issue"] == "The choice is described rather than enacted."


def test_scene_deep_review_prefers_current_author_draft_over_runtime_final(client: TestClient, session) -> None:
    _seed_finished_scene(session)
    draft = AuthorDraftService(session).ensure("scene", SCENE_ID, actor_ref="writer")["draft"]
    AuthorDraftService(session).save(
        draft["draft_id"],
        {
            "content": "林岑把证据袋压进袖口，没有解释，只问许望：你要我现在开门吗？",
            "base_revision_no": draft["revision_no"],
        },
        actor_ref="writer",
    )
    session.commit()

    response = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review")

    assert response.status_code == 200
    evaluation = response.json()["data"]["latest_evaluation"]
    assert evaluation["source_text_ref"] == f"author_draft:{draft['draft_id']}"


def test_passage_patch_candidate_accepts_without_overwriting_final_and_records_preference(client: TestClient, session) -> None:
    _seed_finished_scene(session)
    original_final = session.get(FinalScene, FINAL_ROW_ID).content

    create_response = client.post(
        "/api/v1/passages/patch-candidates",
        json={
            "object_type": "scene",
            "object_id": SCENE_ID,
            "chapter_id": CHAPTER_ID,
            "scene_id": SCENE_ID,
            "target_text_ref": f"final_scene:{FINAL_ROW_ID}",
            "source_excerpt": "她低声说证据还不能公开，然后又解释这是为了保护所有人。",
            "issue_dimension": "dialogue_subtext",
        },
    )

    assert create_response.status_code == 200
    candidate = create_response.json()["data"]["candidate"]
    assert candidate["status"] == "candidate"
    assert candidate["manual_only"] is True
    assert len(candidate["replacement_options"]) == 3
    assert {option["tone"] for option in candidate["replacement_options"]} == {"shorter", "sharper", "subtler"}

    accept_response = client.post(
        f"/api/v1/passage-patch-candidates/{candidate['patch_id']}/accept",
        json={"selected_option_id": candidate["replacement_options"][1]["option_id"], "note": "更有刺。"},
    )

    assert accept_response.status_code == 200
    accepted = accept_response.json()["data"]["candidate"]
    assert accepted["status"] == "accepted"
    assert accepted["author_decision"] == "accepted"

    session.expire_all()
    assert session.get(FinalScene, FINAL_ROW_ID).content == original_final
    row = session.get(PassagePatchCandidate, candidate["patch_id"])
    assert row.selected_option_id == candidate["replacement_options"][1]["option_id"]

    profile_response = client.get("/api/v1/author-preference-profile")
    assert profile_response.status_code == 200
    profile = profile_response.json()["data"]["profile"]
    assert profile["status"] == "draft"
    assert profile["runtime_eligible"] is False
    assert "更锋利" in " ".join(profile["summary"]["preferred_revision_moves"])

    session.expire_all()
    db_profile = session.get(AuthorPreferenceProfile, profile["profile_id"])
    assert db_profile.status == "draft"


def test_rejecting_passage_patch_updates_candidate_but_keeps_preference_unpublished(client: TestClient, session) -> None:
    _seed_finished_scene(session)

    candidate = client.post(
        "/api/v1/passages/patch-candidates",
        json={
            "object_type": "scene",
            "object_id": SCENE_ID,
            "chapter_id": CHAPTER_ID,
            "scene_id": SCENE_ID,
            "target_text_ref": f"final_scene:{FINAL_ROW_ID}",
            "source_excerpt": "林岑的手指再次停顿。",
            "issue_dimension": "repetitive_expression",
        },
    ).json()["data"]["candidate"]

    reject_response = client.post(
        f"/api/v1/passage-patch-candidates/{candidate['patch_id']}/reject",
        json={"note": "这处重复保留为人物习惯。"},
    )

    assert reject_response.status_code == 200
    rejected = reject_response.json()["data"]["candidate"]
    assert rejected["status"] == "rejected"
    assert rejected["author_decision"] == "rejected"

    profile = client.get("/api/v1/author-preference-profile").json()["data"]["profile"]
    assert profile["runtime_eligible"] is False
    assert any("保留" in item for item in profile["summary"]["rejected_revision_moves"])


class ScriptedPassagePatchClient(OnlineAccountedExecution):
    def __init__(self) -> None:
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        output = {
            "patches": [
                {
                    "target_text_ref": "author_draft:test",
                    "source_excerpt": "她低声说证据还不能公开，然后又解释这是为了保护所有人。",
                    "replacement_text": "她把证据袋压在袖口下，只问许望：你也要我现在开门吗？",
                    "patch_type": "replace_excerpt",
                    "changed_dimensions": ["dialogue_subtext", "choice_pressure"],
                    "why_it_helps": "把解释改成动作和反问，关系压力更清楚。",
                },
                {
                    "target_text_ref": "author_draft:test",
                    "source_excerpt": "她低声说证据还不能公开，然后又解释这是为了保护所有人。",
                    "replacement_text": "证据袋在她袖口里折出硬角。她没有解释，只把门锁重新扣上。",
                    "patch_type": "replace_excerpt",
                    "changed_dimensions": ["information_rhythm", "image_necessity"],
                    "why_it_helps": "用物件和动作承载信息，减少说明。",
                },
            ],
            "rationale": "保留事实，把解释句改成选择压力。",
            "manual_only": True,
        }
        return LLMResponse(
            request_id="patch_req_1",
            provider="fake",
            model=request.model,
            text="{}",
            structured_output=output,
            response_format=request.response_format,
            raw_response={"id": "patch_req_1", "model": request.model},
            usage={"input_tokens": 11, "output_tokens": 22, "total_tokens": 33},
            finish_reason="stop",
        )

    def generate_accounted(self, request, *, accounting_hook):
        handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
        response = self.generate(request)
        accounting_hook.after_response(handle, request=request, response=response, latency_ms=1)
        return response


def test_passage_patch_candidate_uses_writer_passage_patch_llm_and_records_provenance(session) -> None:
    _seed_finished_scene(session)
    draft = AuthorDraftService(session).ensure("scene", SCENE_ID, actor_ref="writer")["draft"]
    llm_client = ScriptedPassagePatchClient()

    result = WriterDeepReviewService(session, llm_client=llm_client).create_patch_candidate(
        {
            "object_type": "scene",
            "object_id": SCENE_ID,
            "chapter_id": CHAPTER_ID,
            "scene_id": SCENE_ID,
            "target_text_ref": f"author_draft:{draft['draft_id']}",
            "source_draft_id": draft["draft_id"],
            "source_excerpt": "她低声说证据还不能公开，然后又解释这是为了保护所有人。",
            "issue_dimension": "dialogue_subtext",
        },
        actor_ref="writer",
    )

    candidate = result["candidate"]
    assert llm_client.requests[0].node_id == "writer_passage_patch"
    assert candidate["source_draft_id"] == draft["draft_id"]
    assert candidate["generation_llm_call_id"]
    assert candidate["rationale"] == "保留事实，把解释句改成选择压力。"
    assert [option["label"] for option in candidate["replacement_options"]] == ["版本 1", "版本 2"]
    assert candidate["replacement_options"][0]["tone"] == "dialogue_subtext"

    session.expire_all()
    row = session.get(PassagePatchCandidate, candidate["patch_id"])
    assert row.source_draft_id == draft["draft_id"]
    assert row.generation_llm_call_id == candidate["generation_llm_call_id"]
    assert session.get(LlmCall, candidate["generation_llm_call_id"]).node_id == "writer_passage_patch"
    patch_call = session.get(LlmCall, candidate["generation_llm_call_id"])
    assert patch_call.scope_type == "scene"
    assert patch_call.scope_id == SCENE_ID
    assert patch_call.project_id == PROJECT_ID


def test_passage_patch_candidate_records_category_range_strategy_and_preference_tags(session) -> None:
    _seed_finished_scene(session)
    draft = AuthorDraftService(session).ensure("scene", SCENE_ID, actor_ref="writer")["draft"]

    result = WriterDeepReviewService(session).create_patch_candidate(
        {
            "object_type": "scene",
            "object_id": SCENE_ID,
            "chapter_id": CHAPTER_ID,
            "scene_id": SCENE_ID,
            "target_text_ref": f"author_draft:{draft['draft_id']}",
            "source_draft_id": draft["draft_id"],
            "source_excerpt": "她低声说证据还不能公开，然后又解释这是为了保护所有人。",
            "issue_dimension": "dialogue_subtext",
            "candidate_category": "dialogue_rewrite",
            "target_range": {"start": 8, "end": 32, "unit": "char"},
            "revision_strategy": "反问替代解释",
            "preference_tags": ["少解释", "对白更短", "动作承压"],
        },
        actor_ref="writer",
    )

    candidate = result["candidate"]

    assert candidate["candidate_category"] == "dialogue_rewrite"
    assert candidate["target_range"] == {"start": 8, "end": 32, "unit": "char"}
    assert candidate["revision_strategy"] == "反问替代解释"
    assert candidate["preference_tags"] == ["少解释", "对白更短", "动作承压"]
    assert candidate["inserted_into_author_draft"] is False

    session.expire_all()
    row = session.get(PassagePatchCandidate, candidate["patch_id"])
    assert row.candidate_category == "dialogue_rewrite"
    assert row.target_range_json == {"start": 8, "end": 32, "unit": "char"}
    assert row.revision_strategy == "反问替代解释"
    assert row.preference_tags_json == ["少解释", "对白更短", "动作承压"]
    assert row.inserted_into_author_draft == 0


def test_passage_patch_candidate_records_quality_signal_id_for_quality_handoff(session) -> None:
    _seed_finished_scene(session)
    draft = AuthorDraftService(session).ensure("scene", SCENE_ID, actor_ref="writer")["draft"]

    result = WriterDeepReviewService(session).create_patch_candidate(
        {
            "object_type": "scene",
            "object_id": SCENE_ID,
            "chapter_id": CHAPTER_ID,
            "scene_id": SCENE_ID,
            "target_text_ref": f"author_draft:{draft['draft_id']}",
            "source_draft_id": draft["draft_id"],
            "source_excerpt": "林岑的手指再次停顿。",
            "issue_dimension": "template_action_reuse",
            "quality_signal_id": "quality:scene:DEEP_CH01_SC01:template_action_reuse",
        },
        actor_ref="writer",
    )

    candidate = result["candidate"]

    assert candidate["quality_signal_id"] == "quality:scene:DEEP_CH01_SC01:template_action_reuse"

    session.expire_all()
    row = session.get(PassagePatchCandidate, candidate["patch_id"])
    assert row.quality_signal_id == "quality:scene:DEEP_CH01_SC01:template_action_reuse"


def test_passage_patch_prompt_includes_only_approved_runtime_author_preference(session) -> None:
    _seed_finished_scene(session)
    draft = AuthorDraftService(session).ensure("scene", SCENE_ID, actor_ref="writer")["draft"]
    session.add(
        AuthorPreferenceProfile(
            profile_id="author_pref_draft_ignored",
            scope_type="global",
            scope_ref_id="global",
            status="draft",
            runtime_eligible=0,
            summary_json={"preferred_revision_moves": ["草稿偏好不应进入提示词"]},
            source_patch_ids_json=[],
        )
    )
    session.add(
        AuthorPreferenceProfile(
            profile_id="author_pref_approved_runtime",
            scope_type="global",
            scope_ref_id="global",
            status="approved",
            runtime_eligible=1,
            summary_json={"preferred_revision_moves": ["更锋利的反问"], "rejected_revision_moves": ["解释性对白"]},
            source_patch_ids_json=[],
        )
    )
    session.flush()
    llm_client = ScriptedPassagePatchClient()

    WriterDeepReviewService(session, llm_client=llm_client).create_patch_candidate(
        {
            "object_type": "scene",
            "object_id": SCENE_ID,
            "chapter_id": CHAPTER_ID,
            "scene_id": SCENE_ID,
            "target_text_ref": f"author_draft:{draft['draft_id']}",
            "source_draft_id": draft["draft_id"],
            "source_excerpt": "林岑的手指再次停顿。",
            "issue_dimension": "repetitive_expression",
        },
        actor_ref="writer",
    )

    user_prompt = llm_client.requests[0].messages[1]["content"]
    assert "更锋利的反问" in user_prompt
    assert "解释性对白" in user_prompt
    assert "草稿偏好不应进入提示词" not in user_prompt

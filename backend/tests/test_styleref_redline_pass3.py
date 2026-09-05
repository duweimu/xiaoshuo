"""pass3 R2/R4：Style Reference 红线对抗式测试（先红后绿）。

- SR-G1: Strategy B few-shot 注入缺 cloud_policy=local_only 守卫（RAG 有），
  把源引文逐字灌进可能上云的生成 prompt，违反 local_only 数据安全契约。
- SR-G3: 删书级联漏清 5 张物化提升表（style_observations/style_rules/...），
  删书后 approved 风格行变孤儿、仍 runtime-active 注入下游成稿。
（SR-G2 反抄袭引擎缺 NFKC+繁简归一化 → 降为文档化观察，正确修需 opencc 级繁简表。）
"""

from __future__ import annotations

import pytest

from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.cleanup import purge_derived_data
from novel_system.services.style_reference.config_loader import clear_config_cache
from novel_system.services.style_reference.injection import InjectionService
from novel_system.services.style_reference.repository import StyleReferenceRepository


@pytest.fixture(autouse=True)
def _reset_yaml_cache():
    clear_config_cache()
    yield
    clear_config_cache()


def _seed_fewshot_binding(*, seed: str, cloud_policy: str, project_id: str) -> None:
    """建 book(指定 cloud_policy)+run+quote+profile(scene_samples_index)+binding(B)。"""
    book_id = f"sr_book_{seed}"
    run_id = f"sr_run_{seed}"
    profile_id = f"sr_profile_{seed}"
    quote_id = f"sr_q_{seed}"
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_book(
            book_id=book_id, title="t", source_kind="upload", cloud_policy=cloud_policy,
            text_checksum=f"chk_{seed}", total_chars=10, status="ready",
            stats_json=(
                {"rights_declaration": {
                    "declared": True, "analysis_rights": True, "send_rights": True,
                }}
                if cloud_policy != "local_only"
                else {}
            ),
        )
        repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
        repo.create_quote(
            quote_id=quote_id, book_id=book_id, paragraph_id=None,
            span_start=0, span_end=14, quote_text="他低头看着脚下的路,一言不发。",
            illustrates_dims=[], extracted_features={},
        )
        repo.create_profile(
            profile_id=profile_id, book_id=book_id, run_id=run_id, title="t",
            status="active",
            profile_json={
                "narrative_summary": "短句白描",
                "style_features": ["短句"],
                "scene_samples_index": {"dialogue": [quote_id]},
            },
            coverage_json={},
            source_finding_ids_json=[],
        )
        repo.create_binding(
            binding_id=f"sr_bind_{seed}", profile_id=profile_id,
            scope="project", scope_ref_id=project_id,
            task_type="scene_generation", strategy="B",
            config_json={}, status="active",
        )
        session.commit()


# ---------------------------------------------------------------------------
# SR-G1: few-shot 必须对 local_only 书跳过（不把源引文送云端生成 prompt）
# ---------------------------------------------------------------------------
def test_few_shot_skips_source_quote_for_local_only_book():
    """local_only 书：few-shot 不得注入源引文（修前红：引文在场=泄漏；修后绿：空）。"""
    _seed_fewshot_binding(seed="g1local", cloud_policy="local_only", project_id="proj_g1local")
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for("proj_g1local", "scene_generation")
    assert "他低头看着脚下的路" not in fragments.few_shot_block, (
        "local_only 书的源引文逐字进了 few-shot block → 送云端生成 prompt = 违反数据安全契约"
    )
    assert "他低头看着脚下的路" not in fragments.to_system_prompt_prefix()


def test_few_shot_still_renders_for_segments_only_book():
    """对照：segments_only 允许段级送云 → few-shot 仍应渲染源引文（守卫不可过宽）。"""
    _seed_fewshot_binding(seed="g1seg", cloud_policy="segments_only", project_id="proj_g1seg")
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for("proj_g1seg", "scene_generation")
    assert "他低头看着脚下的路" in fragments.few_shot_block


# ---------------------------------------------------------------------------
# SR-G3: 删书级联必须清掉物化提升的运行时风格行（否则孤儿仍注入下游成稿）
# ---------------------------------------------------------------------------

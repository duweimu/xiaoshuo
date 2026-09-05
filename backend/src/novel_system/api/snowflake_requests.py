from __future__ import annotations

from typing import Annotated

from pydantic import Field

from novel_system.api.request_types import BoundedJsonObject, StrictRequestModel


SnowflakeReference = Annotated[str, Field(min_length=1, max_length=255)]


class SnowflakeStepGenerateRequest(StrictRequestModel):
    """Fixed command envelope around an evolvable snowflake draft."""

    # Kept for v1/v2 callers that explicitly name regeneration intent.  The
    # workspace generator always creates a new step-run, so ``True`` does not
    # need a separate service branch; retaining it makes that contract visible
    # without reopening the envelope to arbitrary ignored fields.
    force_new: bool | None = None
    skip: bool | None = None
    skip_reason: str | None = Field(default=None, max_length=4000)
    direction_text: str | None = Field(default=None, max_length=2000)
    focus_scene_refs: list[SnowflakeReference] | None = Field(
        default=None,
        max_length=256,
    )
    focus_character_refs: list[SnowflakeReference] | None = Field(
        default=None,
        max_length=256,
    )
    draft_override: BoundedJsonObject | None = None
    require_llm: bool | None = None
    # React 工作台的结构化生成通道（ws-snow.jsx ``structuredGenerate``）用它标记
    # 触发入口：fe_scaffold_ai / fe_candidate_adopt / fe_scene_focus_ai /
    # fe_char_focus_ai。它只是随 step-run 落库的出处标签，不选择生成分支；
    # 缺了这个字段整个 envelope 就 422，作者的每一次 AI 生成点击都被校验层挡下。
    # 界定成小写下划线短标识，而不是把 StrictRequestModel 放开成任意忽略字段。
    source: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9_]+$")


class LegacySnowflakeStepGenerateRequest(StrictRequestModel):
    """Public V1 planner command; generated artifacts remain service-owned."""

    force_new: bool | None = None
    skip: bool | None = None
    skip_reason: str | None = Field(default=None, max_length=4000)


class LegacySnowflakeArtifactUpdateRequest(StrictRequestModel):
    artifact_json: BoundedJsonObject | None = None
    diagnosis_json: BoundedJsonObject | None = None


class SnowflakeFeCandidatesRequest(StrictRequestModel):
    context: str | None = Field(default=None, max_length=6000)
    draft: str | None = Field(default=None, max_length=3000)
    target_chars: int | None = Field(default=None, ge=40, le=400)


class SnowflakeStepRestoreRequest(StrictRequestModel):
    # Missing/blank IDs intentionally remain service-level domain errors.
    step_run_id: str | None = Field(default=None, max_length=255)


class SnowflakeAcceptStaleStepRequest(StrictRequestModel):
    note: str | None = Field(default=None, max_length=10_000)


class SnowflakeAssistantRequest(StrictRequestModel):
    step_key: str | None = Field(default=None, max_length=128)
    draft_override: BoundedJsonObject | None = None
    focus_scene_id: str | None = Field(default=None, max_length=255)
    message: str | None = Field(default=None, max_length=20_000)
    # The current client embeds the excerpt in ``message`` as well.  Keep this
    # compatibility field explicit instead of accepting arbitrary ignored keys.
    discovery_draft_excerpt: str | None = Field(default=None, max_length=10_000)


class SnowflakeSceneTriageSuggestRequest(StrictRequestModel):
    draft_override: BoundedJsonObject | None = None


class SnowflakeAcceptStaleScenesRequest(StrictRequestModel):
    scene_plan_ids: list[SnowflakeReference] | None = Field(
        default=None,
        max_length=10_000,
    )
    note: str | None = Field(default=None, max_length=10_000)


class SnowflakeOrphanResolveRequest(StrictRequestModel):
    # Vocabulary stays in the domain service so its stable error code survives.
    action: str | None = Field(default=None, max_length=64)


class SnowflakeResyncRequest(StrictRequestModel):
    dry_run: bool | None = None
    scene_plan_ids: list[SnowflakeReference] | None = Field(
        default=None,
        max_length=10_000,
    )
    scene_ids: list[SnowflakeReference] | None = Field(
        default=None,
        max_length=10_000,
    )

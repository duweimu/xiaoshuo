from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import HumanReviewEvent, OperationLog, ReviewItem
from novel_system.services.errors import DomainError


class HumanReviewManager:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_generation_blocker_event(
        self,
        *,
        scene_id: str,
        chapter_id: str,
        object_ref: str,
        target_type: str,
        target_id: str,
        target_ref: str,
        failure_reason: str,
        trigger_reason: str,
        recommended_action: str,
        replay_context: dict,
        allow_soft_risk_acceptance: bool = False,
    ) -> HumanReviewEvent:
        allowed_actions = ["inspect", "accept_soft_risk"] if allow_soft_risk_acceptance else ["inspect"]
        result_status_map = {
            "inspect": "needs_followup",
            **({"accept_soft_risk": "resolved"} if allow_soft_risk_acceptance else {}),
        }
        event = HumanReviewEvent(
            event_id=f"human_review_generation_{scene_id}_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}",
            scene_id=scene_id,
            chapter_id=chapter_id,
            object_ref=object_ref,
            event_source="scene_generation",
            priority="high",
            status="needs_followup",
            allowed_actions_json=allowed_actions,
            result_status_map_json=result_status_map,
            details_json={
                "failure_reason": failure_reason,
                "trigger_reason": trigger_reason,
                "recommended_action": recommended_action,
                "soft_risk_acceptance_allowed": bool(allow_soft_risk_acceptance),
                "linked_target_type": target_type,
                "linked_target_id": target_id,
                "linked_target_ref": target_ref,
                "replay_context": replay_context,
            },
            default_action="inspect",
        )
        self.session.add(event)
        self.session.flush()
        return event


    def accepted_soft_risk_waiver(
        self,
        *,
        scene_id: str,
        trigger_reason: str,
        source_draft_content_hash: str,
    ) -> dict[str, str] | None:
        rows = self.session.execute(
            select(HumanReviewEvent)
            .where(HumanReviewEvent.scene_id == scene_id)
            .where(HumanReviewEvent.event_source == "scene_generation")
            .where(HumanReviewEvent.status == "resolved")
            .order_by(HumanReviewEvent.created_at.desc(), HumanReviewEvent.event_id.desc())
        ).scalars().all()
        for event in rows:
            details = dict(event.details_json or {})
            acceptance = details.get("soft_risk_acceptance") if isinstance(details.get("soft_risk_acceptance"), dict) else {}
            replay_context = details.get("replay_context") if isinstance(details.get("replay_context"), dict) else {}
            if not acceptance.get("accepted"):
                continue
            if str(acceptance.get("trigger_reason") or details.get("trigger_reason") or "") != trigger_reason:
                continue
            accepted_hash = str(acceptance.get("source_draft_content_hash") or replay_context.get("source_draft_content_hash") or "")
            if accepted_hash != source_draft_content_hash:
                continue
            return {
                "event_id": event.event_id,
                "reason": str(acceptance.get("reason") or ""),
                "actor_ref": str(acceptance.get("actor_ref") or ""),
                "qc_report_id": str(acceptance.get("qc_report_id") or replay_context.get("current_qc_report_id") or ""),
            }
        return None



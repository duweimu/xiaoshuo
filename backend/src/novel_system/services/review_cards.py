"""FE-ALIGN Phase 5: 待办收件箱卡片服务（ReviewItem 扩展原表 —— 一个收件箱一份真相）。

- 卡片行：item_type="fe_card"、status 恒 "pending"（legacy CheckConstraint 兼容），
  生命周期走 state（open/resolved/snoozed）。
- legacy 行（QC/安全/triage 等旧生产者）在统一列表里映射：status pending→open、
  approved/rejected→resolved，kind/priority 给默认值。
- dedupe_key 与 project_id 联合唯一（onceTask 语义）：重复投递返回已存在卡。
- resolve(action_index)：同一事务执行 actions_json[i].effect（注册表见 review_effects）。
- 派生卡（services/review_derived，id 前缀 derived:）只读：不可 resolve，可按指纹 snooze。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ReviewDerivedSnooze, ReviewItem, utcnow
from novel_system.services.errors import DomainError
from novel_system.services.review_derived import derive_cards
from novel_system.services.review_effects import run_effect

CARD_ITEM_TYPE = "fe_card"
CARD_KINDS = ("decision", "risk", "qc", "idea", "note")
CARD_STATES = ("open", "resolved", "snoozed")

# legacy item_type → 卡片 kind 的展示默认（响应映射，不回写行）
LEGACY_KIND_DEFAULTS = {
    "author_preference_profile": "decision",
}


class ReviewCardService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---- 创建（rvPush / onceTask 入口） ----

    def create_card(self, payload: dict[str, Any], *, actor_ref: str = "operator") -> dict[str, Any]:
        body = dict(payload or {})
        project_id = str(body.get("project_id") or "").strip() or None
        kind = str(body.get("kind") or "note")
        if kind not in CARD_KINDS:
            raise DomainError("REVIEW_CARD_KIND_INVALID", f"kind must be one of {CARD_KINDS}", status_code=400)
        title = str(body.get("title") or "").strip()
        if not title:
            raise DomainError("REVIEW_CARD_TITLE_REQUIRED", "title is required", status_code=400)
        dedupe_key = str(body.get("dedupe_key") or "").strip() or None
        if dedupe_key:
            existing = self.session.execute(
                select(ReviewItem).where(
                    ReviewItem.project_id == project_id,
                    ReviewItem.dedupe_key == dedupe_key,
                )
            ).scalars().first()
            if existing is not None:
                # onceTask：重复触发静默返回已存在卡（即使已 resolved 也不再造新卡）
                return {"card": self.card_payload(existing), "deduped": True}
        item = ReviewItem(
            review_id=f"card_{uuid.uuid4().hex[:12]}",
            item_type=CARD_ITEM_TYPE,
            status="pending",
            candidate_text=title,
            candidate_payload_json={},
            project_id=project_id,
            scene_id=str(body.get("scene_id") or "").strip() or None,
            chapter_id=str(body.get("chapter_id") or "").strip() or None,
            kind=kind,
            priority=int(body.get("priority") or 2),
            provenance_json={
                "source": str(body.get("source") or ""),
                "where": str(body.get("where") or ""),
                "occurred_at": str(body.get("occurred_at") or utcnow()),
                "actor_ref": actor_ref,
            },
            card_json={
                key: body[key]
                for key in ("detail", "preview", "checklist", "options")
                if body.get(key) is not None
            },
            actions_json=list(body.get("actions") or [{"label": "知道了", "intent": "quiet", "op": "resolve"}]),
            state="open",
            dedupe_key=dedupe_key,
        )
        self.session.add(item)
        self.session.flush()
        return {"card": self.card_payload(item), "deduped": False}

    # ---- 统一列表（持久卡 ∪ 派生卡） ----

    def list_cards(self, project_id: str, state: str = "open") -> dict[str, Any]:
        if state not in ("open", "snoozed"):
            raise DomainError("REVIEW_STATE_INVALID", "state must be open or snoozed", status_code=400)
        rows = self.session.execute(
            select(ReviewItem)
            .where(
                (ReviewItem.project_id == project_id)
                # 全局卡（如风格画像 decision 卡）在任一作品的收件箱可见；
                # legacy 行（project_id 同为 NULL）不全局扩散
                | (ReviewItem.project_id.is_(None) & (ReviewItem.item_type == CARD_ITEM_TYPE))
            )
            .order_by(ReviewItem.created_at.desc(), ReviewItem.review_id.desc())
        ).scalars().all()
        persistent = [self.card_payload(row) for row in rows if self._unified_state(row) == state]
        derived = derive_cards(self.session, project_id)
        snoozed_fps = {
            row.fingerprint
            for row in self.session.execute(
                select(ReviewDerivedSnooze).where(ReviewDerivedSnooze.project_id == project_id)
            ).scalars().all()
        }
        if state == "open":
            derived_visible = [card for card in derived if card["id"] not in snoozed_fps]
        else:
            derived_visible = [card for card in derived if card["id"] in snoozed_fps]
        items = derived_visible + persistent
        items.sort(key=lambda card: 0 if card.get("priority") == 1 else 1)
        return {"items": items}

    def badge(self, project_id: str) -> dict[str, Any]:
        open_items = self.list_cards(project_id, state="open")["items"]
        return {"count": sum(1 for card in open_items if card.get("priority") == 1)}

    # ---- 状态流转 ----

    def resolve(
        self,
        review_id: str,
        *,
        action_index: int | None = None,
        project_id: str | None = None,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        if review_id.startswith("derived:"):
            raise DomainError(
                "REVIEW_DERIVED_NOT_RESOLVABLE",
                "实时派生项不能被划掉：去源头处理（修好会自动消失），或选择稍后。",
                status_code=409,
            )
        item = self._require_card(review_id)
        effect_result: dict[str, Any] | None = None
        if action_index is not None:
            actions = list(item.actions_json or [])
            if not (0 <= int(action_index) < len(actions)):
                raise DomainError("REVIEW_ACTION_INDEX_INVALID", "action_index out of range", status_code=400)
            effect = actions[int(action_index)].get("effect")
            if effect:
                effect_result = run_effect(
                    self.session,
                    item.project_id or project_id,
                    dict(effect),
                )
            item.resolved_action_index = int(action_index)
        item.state = "resolved"
        self.session.flush()
        return {"card": self.card_payload(item), "effect_result": effect_result}

    def unresolve(self, review_id: str) -> dict[str, Any]:
        item = self._require_card(review_id)
        # 撤销只回标记，不回滚 effect（复杂逆操作不做——卡片文案需注明）
        item.state = "open"
        item.resolved_action_index = None
        self.session.flush()
        return {"card": self.card_payload(item)}

    def snooze(self, review_id: str, *, project_id: str | None = None) -> dict[str, Any]:
        if review_id.startswith("derived:"):
            if not project_id:
                raise DomainError("REVIEW_PROJECT_REQUIRED", "project_id is required to snooze derived items", status_code=400)
            existing = self.session.get(ReviewDerivedSnooze, (project_id, review_id))
            if existing is None:
                self.session.add(ReviewDerivedSnooze(project_id=project_id, fingerprint=review_id))
                self.session.flush()
            return {"id": review_id, "state": "snoozed"}
        item = self._require_card(review_id)
        item.state = "snoozed"
        self.session.flush()
        return {"card": self.card_payload(item)}

    def unsnooze(self, review_id: str, *, project_id: str | None = None) -> dict[str, Any]:
        if review_id.startswith("derived:"):
            if project_id:
                existing = self.session.get(ReviewDerivedSnooze, (project_id, review_id))
                if existing is not None:
                    self.session.delete(existing)
                    self.session.flush()
            return {"id": review_id, "state": "open"}
        item = self._require_card(review_id)
        item.state = "open"
        self.session.flush()
        return {"card": self.card_payload(item)}

    # ---- 序列化 ----

    def card_payload(self, item: ReviewItem) -> dict[str, Any]:
        card = dict(item.card_json or {})
        provenance = dict(item.provenance_json or {})
        return {
            "id": item.review_id,
            "project_id": item.project_id,
            "kind": item.kind or LEGACY_KIND_DEFAULTS.get(item.item_type, "qc"),
            "priority": int(item.priority or 2),
            "title": item.candidate_text,
            "where": provenance.get("where") or "",
            "source": provenance.get("source") or item.item_type,
            "occurred_at": provenance.get("occurred_at") or item.created_at,
            "detail": card.get("detail") or "",
            "preview": card.get("preview"),
            "checklist": card.get("checklist"),
            "options": card.get("options"),
            "live": False,
            "actions": list(item.actions_json or []),
            "state": self._unified_state(item),
            "dedupe_key": item.dedupe_key,
            "resolved_action_index": item.resolved_action_index,
            "legacy": item.item_type != CARD_ITEM_TYPE,
        }

    @staticmethod
    def _unified_state(item: ReviewItem) -> str:
        if item.item_type == CARD_ITEM_TYPE or item.state:
            return item.state or "open"
        # legacy 行：pending→open，approved/rejected→resolved
        return "open" if item.status == "pending" else "resolved"

    def _require_card(self, review_id: str) -> ReviewItem:
        item = self.session.get(ReviewItem, review_id)
        if item is None:
            raise DomainError("REVIEW_NOT_FOUND", f"review {review_id} not found", status_code=404)
        return item

"""Finite host-selected notice destinations; no recipient inference or new authority."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from muli.proactive_memory import PreferenceEvidence, check_preference
from muli.reply_origin import ReplyOrigin, RouteError

if TYPE_CHECKING:
    from muli.config import Settings


@dataclass(frozen=True, slots=True)
class FrozenRouting:
    classification: Literal["bound", "independent", "legacy_local"] = "legacy_local"
    origin: ReplyOrigin = ReplyOrigin()
    policy_fingerprint: str = ""
    memory_evidence: PreferenceEvidence | None = None

    @classmethod
    def bound(cls, origin: ReplyOrigin) -> FrozenRouting:
        return cls("bound", origin) if origin.kind in {"web", "channel"} else cls()

    @classmethod
    def independent(cls, settings: Settings, db: sqlite3.Connection | None = None) -> FrozenRouting:
        policy = settings.channels.proactive
        evidence, _ = (
            check_preference(settings, db) if policy.external_enabled else (None, "paused")
        )
        destination = (
            policy.memory_preference.destination.model_dump()
            if evidence is not None and policy.memory_preference is not None
            else policy.default_destination.model_dump()
            if policy.external_enabled
            else {"kind": "web"}
        )
        origin = ReplyOrigin.load({**destination, "input_id": "independent"})
        fingerprint = hashlib.sha256(
            json.dumps(policy.model_dump(), sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        return cls("independent", origin, fingerprint, evidence)

    @classmethod
    def load(cls, value: object) -> FrozenRouting:
        if not isinstance(value, dict) or value.get("v") != 1:
            return cls()
        classification, selected = value.get("classification"), value.get("selected_by")
        if not isinstance(classification, str) or not isinstance(selected, str):
            return cls()
        if classification == "legacy_local":
            return cls()
        if (classification, selected) not in {
            ("bound", "origin"),
            ("independent", "default"),
            ("independent", "memory"),
        }:
            return cls()
        evidence = (
            PreferenceEvidence.load(value.get("memory_evidence")) if selected == "memory" else None
        )
        if selected == "memory" and evidence is None:
            return cls()
        destination = value.get("destination")
        if not isinstance(destination, dict) or set(destination) - {"kind", "route"}:
            return cls()
        identifier = value.get("source_input_id") if classification == "bound" else "independent"
        origin = ReplyOrigin.load({**destination, "input_id": identifier})
        if origin.kind not in {"web", "channel"}:
            return cls()
        fingerprint = value.get("policy_fingerprint", "")
        routes = origin.delivery_routes()
        if classification == "independent" and (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
            or (routes and "reply_to" in routes[0])
        ):
            return cls()
        return cls(
            cast(Literal["bound", "independent"], classification),
            origin,
            fingerprint if classification == "independent" else "",
            evidence,
        )

    def dump(self) -> dict[str, Any]:
        origin = self.origin.dump()
        destination = {key: value for key, value in origin.items() if key != "input_id"}
        if self.classification == "legacy_local":
            destination = {"kind": "web"}
        return {
            "v": 1,
            "classification": self.classification,
            "destination": destination,
            "source_input_id": self.origin.input_id if self.classification == "bound" else None,
            "selected_by": {
                "bound": "origin",
                "independent": "memory" if self.memory_evidence is not None else "default",
                "legacy_local": "legacy_local",
            }[self.classification],
            "policy_fingerprint": self.policy_fingerprint,
            **(
                {"memory_evidence": self.memory_evidence.dump()}
                if self.memory_evidence is not None
                else {}
            ),
        }

    @property
    def batch_key(self) -> str:
        return json.dumps(
            [
                self.classification,
                self.origin.destination_key,
                self.memory_evidence.dump() if self.memory_evidence else None,
            ],
            ensure_ascii=False,
            sort_keys=True,
        )

    def reply_origin(self, input_id: str) -> ReplyOrigin:
        if self.classification == "bound":
            return self.origin
        if self.classification == "independent":
            return ReplyOrigin.load({**self.origin.dump(), "input_id": input_id})
        return ReplyOrigin()


def guard_delivery(
    settings: Settings,
    route: Mapping[str, Any],
    metadata: object,
    *,
    db: sqlite3.Connection | None = None,
) -> None:
    if metadata is None:
        return  # Ordinary owner/Team replies keep their existing channel authority.
    frozen = FrozenRouting.load(metadata)
    if frozen.origin.delivery_routes() != [dict(route)]:
        raise RouteError("主动投递的冻结目的地证据无效", status_code=403)
    if frozen.classification == "independent":
        if not settings.channels.proactive.external_enabled:
            raise RouteError("独立事项的主动外发已暂停", status_code=403)
        if not settings.attention.enabled:
            raise RouteError("注意事项处理已关闭", status_code=403)
        if frozen.memory_evidence is not None:
            evidence, reason = check_preference(settings, db)
            preference = settings.channels.proactive.memory_preference
            if evidence != frozen.memory_evidence or preference is None:
                raise RouteError(
                    "主动通知记忆依据已失效："
                    + (reason if evidence is None else "binding_changed"),
                    status_code=403,
                )
            current = ReplyOrigin.load(
                {**preference.destination.model_dump(), "input_id": "independent"}
            )
            if current.destination_key != frozen.origin.destination_key:
                raise RouteError("主动通知记忆绑定的目的地已改变", status_code=403)

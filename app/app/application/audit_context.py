from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_REASON_LENGTH = 500


@dataclass(frozen=True)
class AuditContext:
    correlation_id: str
    operation_id: str | None = None
    reason: str | None = None
    actor_id: str | None = None


_current_context: ContextVar[AuditContext | None] = ContextVar(
    "knowmart_audit_context", default=None
)


def _safe_id(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized if _SAFE_ID.fullmatch(normalized) else None


def _safe_reason(value: str | None) -> str | None:
    normalized = (value or "").strip()
    if not normalized or len(normalized) > _MAX_REASON_LENGTH:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        return None
    return normalized


def from_headers(headers: Mapping[str, str]) -> AuditContext:
    """Caller passes the request headers; lookup must be case-insensitive there."""
    return AuditContext(
        correlation_id=_safe_id(headers.get("X-Correlation-ID")) or str(uuid.uuid4()),
        operation_id=_safe_id(headers.get("X-Operation-ID")),
        reason=_safe_reason(headers.get("X-Audit-Reason")),
    )


def set_context(context: AuditContext) -> Token[AuditContext | None]:
    return _current_context.set(context)


def reset_context(token: Token[AuditContext | None]) -> None:
    _current_context.reset(token)


def get_context() -> AuditContext | None:
    return _current_context.get()


def set_actor(actor_id: str) -> None:
    context = get_context()
    if context is not None:
        _current_context.set(replace(context, actor_id=actor_id))

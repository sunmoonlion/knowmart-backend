"""按关系校验其他应用的服务令牌（SDD 0027 P1/P2）。

knowmart 接受两种关系：knowlab 交付分身（deliver）、investment 取分身清单
（summon）。每种关系一个调用方：受众是调用方的 client_id（Casdoor 给
client_credentials 令牌的 aud），主体是 `admin/<调用方服务应用>`，令牌必须带这种
关系的 scope。三项都由部署从调用方配置推出来，写进 `SERVICE_AUTH_<关系>_AUDIENCE`、
`_SUBJECT_ALLOWLIST`、`_REQUIRED_SCOPE`（`common/backend/service-client`）。
没配就是这种关系没开，一律拒绝。

签名、签发方、有效期用模板的 OIDC 客户端核（与浏览器登录同一个 Casdoor、同一套
密钥）。部署下发的 `SERVICE_AUTH_<关系>_DISCOVERY_URL`、`_BACKCHANNEL_ENDPOINT`
这里不用。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import Header

from app.application.errors.exceptions import (
    ForbiddenError,
    ServiceUnavailableError,
    UnauthorizedError,
)
from app.domain.security import Principal
from app.infrastructure.security.oidc import OidcProviderClient
from core.config import Settings, get_settings

Relation = Literal["deliver", "summon"]


@dataclass(frozen=True)
class RelationBinding:
    relation: str
    audience: str
    subjects: frozenset[str]
    required_scope: str


def relation_binding(
    relation: str, environ: Mapping[str, str] | None = None
) -> RelationBinding | None:
    env = os.environ if environ is None else environ
    prefix = f"SERVICE_AUTH_{relation.upper()}_"
    audience = env.get(prefix + "AUDIENCE", "").strip()
    subjects = frozenset(
        s.strip()
        for s in env.get(prefix + "SUBJECT_ALLOWLIST", "").split(",")
        if s.strip()
    )
    scope = env.get(prefix + "REQUIRED_SCOPE", "").strip()
    if not audience or not subjects or scope != f"knowmart:{relation}":
        return None
    return RelationBinding(relation, audience, subjects, scope)


class RelationServiceVerifier:
    def __init__(
        self,
        relation: Relation,
        *,
        settings: Settings | None = None,
        oidc_client: OidcProviderClient | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._relation = relation
        self._settings = settings or get_settings()
        self._environ = environ
        self._oidc = oidc_client or OidcProviderClient(
            self._settings, self._settings.browser_profile("admin")
        )

    async def verify(self, encoded: str) -> Principal:
        binding = relation_binding(self._relation, self._environ)
        if binding is None:
            raise ServiceUnavailableError(
                "This service relation is not configured",
                code="service_relation_unconfigured",
            )
        if not encoded or len(encoded) > 16384:
            raise UnauthorizedError(
                "The service identity token is invalid", code="token_invalid"
            )
        claims = await self._oidc.verify_access_token(
            encoded, audience=binding.audience
        )
        subject = claims.get("sub")
        issuer = claims.get("iss")
        if not isinstance(subject, str) or not isinstance(issuer, str) or not issuer:
            raise UnauthorizedError(
                "The service identity token is invalid", code="token_invalid"
            )
        if subject not in binding.subjects:
            raise ForbiddenError(
                "The service identity is not bound to this relation",
                code="service_subject_unbound",
            )
        if binding.required_scope not in _scopes(claims):
            raise ForbiddenError(
                "The service identity scope is not authorized",
                code="service_scope_denied",
            )
        return Principal(
            actor_type="service",
            subject=subject,
            issuer=issuer,
            app=self._settings.app_slug,
            surface="internal",
            audience=binding.audience,
            scopes=frozenset({binding.required_scope}),
            authenticated_at=_timestamp(claims.get("iat")),
            expires_at=_timestamp(claims.get("exp")),
            policy_version="service-v1",
        )


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, (int, float)):
        raise UnauthorizedError(
            "The service identity token is invalid", code="token_invalid"
        )
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise UnauthorizedError(
            "The service identity token is invalid", code="token_invalid"
        ) from exc


def _scopes(claims: dict[str, Any]) -> frozenset[str]:
    raw = claims.get("scope", claims.get("scp", ()))
    if isinstance(raw, str):
        return frozenset(re.split(r"\s+", raw.strip())) if raw.strip() else frozenset()
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return frozenset(raw)
    raise UnauthorizedError(
        "The service identity token has invalid scopes", code="token_invalid"
    )


def _bearer(authorization: str | None) -> str:
    if not authorization:
        raise UnauthorizedError("A service bearer token is required")
    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() != "bearer" or not encoded.strip():
        raise UnauthorizedError("A service bearer token is required")
    return encoded.strip()


_verifiers: dict[str, RelationServiceVerifier] = {}


def relation_verifier(relation: Relation) -> RelationServiceVerifier:
    if relation not in _verifiers:
        _verifiers[relation] = RelationServiceVerifier(relation)
    return _verifiers[relation]


async def require_deliver_service(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> Principal:
    return await relation_verifier("deliver").verify(_bearer(authorization))


async def require_summon_service(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> Principal:
    return await relation_verifier("summon").verify(_bearer(authorization))

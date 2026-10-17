from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Cookie, Depends, Header, Request

from app.application.audit_context import set_actor
from app.application.errors.exceptions import UnauthorizedError
from app.application.services.auth_service import AuthService
from app.bootstrap.auth import build_auth_service
from app.domain.security import BrowserSession, Principal
from app.infrastructure.security import ServiceIdentityVerifier
from core.config import get_settings

_settings = get_settings()
_admin_profile = _settings.browser_profile("admin")
_web_profile = _settings.browser_profile("web")
admin_auth_service = build_auth_service("admin", _settings)
web_auth_service = build_auth_service("web", _settings)
service_identity_verifier = ServiceIdentityVerifier(_settings)


async def _session(
    *,
    service: AuthService,
    request: Request,
    session_id: str | None,
    csrf_token: str | None,
) -> BrowserSession:
    session = await service.get_browser_session(session_id)
    if session is None or session.principal.actor_type != "user":
        raise UnauthorizedError()
    service.validate_csrf(
        session=session,
        method=request.method,
        origin=request.headers.get("origin"),
        csrf_token=csrf_token,
    )
    set_actor(str(session.principal.actor_id))
    return session


async def get_admin_browser_session(
    request: Request,
    session_id: str | None = Cookie(
        default=None, alias=_admin_profile.session_cookie_name
    ),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> BrowserSession:
    return await _session(
        service=admin_auth_service,
        request=request,
        session_id=session_id,
        csrf_token=csrf_token,
    )


async def get_web_browser_session(
    request: Request,
    session_id: str | None = Cookie(
        default=None, alias=_web_profile.session_cookie_name
    ),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> BrowserSession:
    return await _session(
        service=web_auth_service,
        request=request,
        session_id=session_id,
        csrf_token=csrf_token,
    )


async def get_admin_current_user(
    session: Annotated[BrowserSession, Depends(get_admin_browser_session)],
) -> Principal:
    return session.principal


async def get_web_current_user(
    session: Annotated[BrowserSession, Depends(get_web_browser_session)],
) -> Principal:
    return session.principal


def require_admin_scopes(*required: str) -> Callable[..., Awaitable[Principal]]:
    required_set = frozenset(required)

    async def dependency(
        session: Annotated[BrowserSession, Depends(get_admin_browser_session)],
    ) -> Principal:
        principal = session.principal
        # 白名单里的管理角色（admin、operator，待办 98）也算管理员：
        # 部署只给角色，不发「应用名:admin」这个权限；只认权限的话，
        # 管理端业务接口对谁都是 403（2026-10-17 knowlab 走查，账 134）
        if not set(principal.roles) & _admin_profile.role_allowlist:
            admin_auth_service.require_scopes(principal, required_set)
        return principal

    return dependency


require_knowmart_admin = require_admin_scopes(*_admin_profile.required_scopes)


async def get_internal_service_principal(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> Principal:
    scheme, _, encoded = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not encoded or " " in encoded:
        raise UnauthorizedError(
            "A Bearer service identity is required", code="service_auth_required"
        )
    principal = await service_identity_verifier.verify(
        encoded, required_scopes=frozenset()
    )
    set_actor(principal.subject)
    return principal


def require_internal_scopes(*required: str) -> Callable[..., Awaitable[Principal]]:
    required_set = frozenset(required)

    async def dependency(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> Principal:
        scheme, _, encoded = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not encoded or " " in encoded:
            raise UnauthorizedError(
                "A Bearer service identity is required",
                code="service_auth_required",
            )
        principal = await service_identity_verifier.verify(
            encoded, required_scopes=required_set
        )
        set_actor(principal.subject)
        return principal

    return dependency

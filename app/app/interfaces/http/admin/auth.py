from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Query, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.application.errors.exceptions import AppException
from app.bootstrap.auth import build_auth_service
from app.domain.security import BrowserSession
from app.interfaces.http.auth_common import (
    frontend_redirect,
    set_browser_cookie,
    set_session_cookie,
)
from app.interfaces.http.middleware.auth import get_admin_browser_session
from core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
profile = settings.browser_profile("admin")
router = APIRouter(prefix="/auth/admin", tags=["Admin authentication"])
auth_service = build_auth_service("admin", settings)


@router.get("/login", summary="Start Admin OIDC login")
async def login(return_to: str | None = Query(default=None)) -> RedirectResponse:
    start = await auth_service.begin_login(return_to)
    response = RedirectResponse(url=start.authorization_url, status_code=302)
    set_browser_cookie(
        response,
        settings=settings,
        key=profile.transaction_cookie_name,
        value=start.transaction_id,
        max_age=settings.auth_transaction_ttl_seconds,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/callback", summary="Complete Admin OIDC login")
async def callback(
    code: str,
    state: str,
    transaction_id: str | None = Cookie(
        default=None, alias=profile.transaction_cookie_name
    ),
) -> RedirectResponse:
    try:
        session, return_to = await auth_service.complete_login(
            code=code, state=state, transaction_id=transaction_id
        )
    except AppException as exc:
        logger.warning("admin_oidc_callback_rejected status=%s", exc.status_code)
        response = RedirectResponse(
            url=frontend_redirect(profile, "/login?error=auth_failed"),
            status_code=302,
        )
        response.delete_cookie(profile.transaction_cookie_name, path="/")
        response.headers["Cache-Control"] = "no-store"
        return response
    response = RedirectResponse(
        url=frontend_redirect(profile, return_to), status_code=302
    )
    set_session_cookie(
        response,
        settings=settings,
        profile=profile,
        session=session,
        session_id=session.session_id,
        expires_at=session.expires_at,
    )
    return response


@router.post(
    "/logout",
    summary="Log out Admin browser",
    responses={
        200: {
            "description": "Logged out; navigate to logout_url to end single sign-on"
        },
        204: {"description": "Logged out locally"},
    },
)
async def logout(
    _: Annotated[BrowserSession, Depends(get_admin_browser_session)],
    session_id: str | None = Cookie(default=None, alias=profile.session_cookie_name),
) -> Response:
    logout_url = await auth_service.end_session(session_id)
    response: Response = (
        JSONResponse({"logout_url": logout_url})
        if logout_url
        else Response(status_code=204)
    )
    response.delete_cookie(profile.session_cookie_name, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/logged-out", summary="Return from provider logout")
async def logged_out() -> RedirectResponse:
    """The provider redirects here after ending its session; on to the Admin app."""
    response = RedirectResponse(
        url=frontend_redirect(profile, profile.default_return_to), status_code=302
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/me", summary="Get current Admin user")
async def me(
    session: Annotated[BrowserSession, Depends(get_admin_browser_session)],
) -> dict[str, object]:
    principal = session.principal
    return {
        "contract_version": 1,
        "authenticated": True,
        "user": {
            "actor_id": str(principal.actor_id),
            "app": principal.app,
            "surface": principal.surface,
            "display_name": principal.display_name,
            "email": principal.email,
            "roles": list(principal.roles),
            "scopes": sorted(principal.scopes),
            "expires_at": principal.expires_at.isoformat(),
        },
        "csrf_token": session.csrf_token,
    }

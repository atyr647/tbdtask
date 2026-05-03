"""Passkey enrollment + management + step-up routes (Phase 8a).

Three URL groups:

* ``/passkey/register`` (GET + POST) — mint a challenge, render
  the registration page, then verify the navigator.credentials.create
  response and persist the credential.
* ``/passkey/manage`` (GET) and ``/passkey/<id>/revoke``,
  ``/passkey/<id>/rename`` (POST) — list / revoke / rename
  credentials.
* ``/step-up`` (GET + POST) — mint an assertion challenge for a
  given purpose, then verify the response and create a StepUpGrant.

Challenges round-trip via signed cookies (``itsdangerous``). The cookie
carries the random 32-byte challenge plus the expires_at and the
purpose; signature binding prevents an attacker from swapping in a
challenge they minted themselves. Each cookie is single-use: it is
cleared on success and naturally expires after the TTL.

The whole module honors ``TBDTASK_WEBAUTHN_ENABLED``: when the flag is
off, every endpoint here returns 404 so the surface area is gone.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature
from sqlalchemy.orm import Session

from .. import models as M
from ..auth import step_up as step_up_mod
from ..auth import webauthn as wa
from ..auth.security import signer
from ..db import SessionLocal
from ..templating import templates


router = APIRouter()


# ---------------------------------------------------------------------------
# Cookie naming + helpers
# ---------------------------------------------------------------------------

# Cookies that round-trip the challenge between begin and finish. Must
# be set with HttpOnly + SameSite=strict + Secure (when we have HTTPS).
# Names mirror the session-cookie convention: ``__Host-`` prefix for
# production, plain name for dev with TBDTASK_INSECURE_LOCAL_COOKIES=1.
if os.environ.get("TBDTASK_INSECURE_LOCAL_COOKIES", "0") == "1":
    REG_COOKIE = "tbdtask_passkey_reg_local"
    ASSERT_COOKIE = "tbdtask_passkey_assert_local"
    _COOKIE_SECURE = False
else:
    REG_COOKIE = "__Host-tbdtask_passkey_reg"
    ASSERT_COOKIE = "__Host-tbdtask_passkey_assert"
    _COOKIE_SECURE = True


REG_TTL_SECONDS = int(wa.REGISTRATION_TIMEOUT.total_seconds())
ASSERT_TTL_SECONDS = int(wa.ASSERTION_TIMEOUT.total_seconds())


def _sign_state(salt: str, data: dict) -> str:
    return signer(salt).dumps(data)


def _load_state(salt: str, token: str, max_age: int) -> Optional[dict]:
    try:
        return signer(salt).loads(token, max_age=max_age)
    except BadSignature:
        return None


def _set_state_cookie(response: Response, name: str, value: str, ttl: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=ttl,
        httponly=True,
        secure=_COOKIE_SECURE,
        samesite="strict",
        path="/",
    )


def _clear_state_cookie(response: Response, name: str) -> None:
    response.delete_cookie(name, path="/")


def _enabled_or_404() -> None:
    if not wa.is_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@router.get("/passkey/register", response_class=HTMLResponse)
def passkey_register_page(
    request: Request,
    next: Optional[str] = None,
) -> Response:
    """Render the passkey registration page.

    Mints a fresh registration challenge and embeds it (with the
    options blob) in the page. The challenge is also signed and set
    as ``REG_COOKIE`` so the POST handler can verify the response.
    """
    _enabled_or_404()
    user = _require_user_or_redirect(request)
    if isinstance(user, Response):
        return user

    db = _request_db(request)
    challenge = wa.begin_registration(db, user=user)

    state = {
        "c": _b64(challenge.challenge),
        "u": _b64(challenge.user_handle),
        "rp": challenge.rp_id,
        "exp": int(challenge.expires_at.timestamp()),
    }
    cookie_value = _sign_state("passkey-register", state)

    has_credential = wa.has_active_credential(db, user=user)
    response = templates.TemplateResponse(
        request,
        "auth/passkey_register.html",
        {
            "options_json": challenge.options_json,
            "next": next or "/",
            "has_credential": has_credential,
        },
    )
    _set_state_cookie(response, REG_COOKIE, cookie_value, REG_TTL_SECONDS)
    return response


@router.post("/passkey/register")
async def passkey_register_finish(
    request: Request,
    next: str = Form("/"),
    nickname: str = Form(""),
    response_json: str = Form(...),
) -> Response:
    """Verify the navigator.credentials.create() response."""
    _enabled_or_404()
    user = _require_user_or_redirect(request)
    if isinstance(user, Response):
        return user
    db = _request_db(request)

    cookie_value = request.cookies.get(REG_COOKIE)
    if not cookie_value:
        return _flash_error(request, "Registration session expired. Try again.")
    state = _load_state("passkey-register", cookie_value, REG_TTL_SECONDS)
    if state is None:
        return _flash_error(request, "Registration challenge invalid or expired.")

    try:
        client_response = json.loads(response_json)
    except json.JSONDecodeError:
        return _flash_error(request, "Bad client response.")

    challenge = wa.RegistrationChallenge(
        challenge=_unb64(state["c"]),
        user_handle=_unb64(state["u"]),
        rp_id=state["rp"],
        # ``finish_registration`` re-derives expiry from this ts; pass
        # the value we signed earlier.
        expires_at=__import__("datetime").datetime.utcfromtimestamp(state["exp"]),
        options_json="",
    )
    try:
        wa.finish_registration(
            db,
            user=user,
            client_response=client_response,
            challenge=challenge,
            nickname=nickname.strip() or None,
            request_ip=_client_ip(request),
            request_user_agent=request.headers.get("user-agent"),
        )
    except wa.ChallengeExpired:
        return _flash_error(request, "Registration challenge expired. Try again.")
    except wa.WebAuthnVerificationError:
        return _flash_error(request, "Could not verify the new passkey.")

    # Single-use: clear the cookie so a stolen value can't be replayed.
    safe_next = _safe_next_path(next)
    redirect = RedirectResponse(safe_next, status_code=303)
    _clear_state_cookie(redirect, REG_COOKIE)
    return redirect


# ---------------------------------------------------------------------------
# Manage credentials
# ---------------------------------------------------------------------------


@router.get("/passkey/manage", response_class=HTMLResponse)
def passkey_manage_page(request: Request) -> Response:
    _enabled_or_404()
    user = _require_user_or_redirect(request)
    if isinstance(user, Response):
        return user
    db = _request_db(request)
    creds = (
        db.query(M.UserWebauthnCredential)
        .filter(M.UserWebauthnCredential.user_id == user.id)
        .order_by(M.UserWebauthnCredential.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        request,
        "auth/passkey_manage.html",
        {"credentials": creds},
    )


@router.post("/passkey/{credential_uuid}/revoke")
def passkey_revoke(
    credential_uuid: str,
    request: Request,
) -> Response:
    _enabled_or_404()
    user = _require_user_or_redirect(request)
    if isinstance(user, Response):
        return user
    db = _request_db(request)
    # Refuse to revoke the last active credential — the UI should warn,
    # but enforce server-side too.
    creds = (
        db.query(M.UserWebauthnCredential)
        .filter(
            M.UserWebauthnCredential.user_id == user.id,
            M.UserWebauthnCredential.revoked_at.is_(None),
        )
        .all()
    )
    if len(creds) <= 1 and any(c.id == credential_uuid for c in creds):
        return _flash_error(
            request,
            "Cannot revoke your last passkey. Register another first.",
            redirect_to="/passkey/manage",
        )
    wa.revoke_credential(db, user=user, credential_uuid=credential_uuid)
    return RedirectResponse("/passkey/manage", status_code=303)


@router.post("/passkey/{credential_uuid}/rename")
def passkey_rename(
    credential_uuid: str,
    request: Request,
    nickname: str = Form(""),
) -> Response:
    _enabled_or_404()
    user = _require_user_or_redirect(request)
    if isinstance(user, Response):
        return user
    db = _request_db(request)
    cred = (
        db.query(M.UserWebauthnCredential)
        .filter(
            M.UserWebauthnCredential.id == credential_uuid,
            M.UserWebauthnCredential.user_id == user.id,
        )
        .first()
    )
    if cred is not None:
        cred.nickname = nickname.strip()[:64] or None
    return RedirectResponse("/passkey/manage", status_code=303)


# ---------------------------------------------------------------------------
# Step-up
# ---------------------------------------------------------------------------


@router.get("/step-up", response_class=HTMLResponse)
def step_up_page(
    request: Request,
    purpose: str,
    next: str = "/",
) -> Response:
    _enabled_or_404()
    user = _require_user_or_redirect(request)
    if isinstance(user, Response):
        return user
    db = _request_db(request)
    if not wa.has_active_credential(db, user=user):
        # No passkey to challenge against — bounce to enrollment.
        return RedirectResponse(
            f"/passkey/register?next={request.url.query or ''}",
            status_code=303,
        )

    challenge = wa.begin_assertion(db, user=user, purpose=purpose)
    state = {
        "c": _b64(challenge.challenge),
        "rp": challenge.rp_id,
        "p": purpose,
        "n": next,
        "exp": int(challenge.expires_at.timestamp()),
    }
    cookie_value = _sign_state("passkey-assert", state)
    response = templates.TemplateResponse(
        request,
        "auth/step_up_prompt.html",
        {
            "options_json": challenge.options_json,
            "purpose": purpose,
            "next": next,
        },
    )
    _set_state_cookie(response, ASSERT_COOKIE, cookie_value, ASSERT_TTL_SECONDS)
    return response


@router.post("/step-up")
async def step_up_finish(
    request: Request,
    purpose: str = Form(...),
    next: str = Form("/"),
    response_json: str = Form(...),
) -> Response:
    _enabled_or_404()
    user = _require_user_or_redirect(request)
    if isinstance(user, Response):
        return user
    db = _request_db(request)
    sess = getattr(request.state, "session", None)
    if sess is None:
        raise HTTPException(401, detail="No session")

    cookie_value = request.cookies.get(ASSERT_COOKIE)
    if not cookie_value:
        return _flash_error(
            request, "Verification session expired. Try again.", redirect_to=next
        )
    state = _load_state("passkey-assert", cookie_value, ASSERT_TTL_SECONDS)
    if state is None or state.get("p") != purpose:
        return _flash_error(
            request, "Verification challenge invalid.", redirect_to=next
        )

    try:
        client_response = json.loads(response_json)
    except json.JSONDecodeError:
        return _flash_error(request, "Bad client response.", redirect_to=next)

    challenge = wa.AssertionChallenge(
        challenge=_unb64(state["c"]),
        rp_id=state["rp"],
        expires_at=__import__("datetime").datetime.utcfromtimestamp(state["exp"]),
        purpose=purpose,
        allow_credential_ids=[],
        options_json="",
    )
    try:
        cred_uuid = wa.finish_assertion(
            db,
            user=user,
            client_response=client_response,
            challenge=challenge,
            request_ip=_client_ip(request),
            request_user_agent=request.headers.get("user-agent"),
        )
    except wa.ChallengeExpired:
        return _flash_error(
            request, "Verification challenge expired.", redirect_to=next
        )
    except wa.WebAuthnVerificationError:
        return _flash_error(request, "Could not verify the passkey.", redirect_to=next)

    step_up_mod.grant_step_up(
        db,
        session_id=sess.id,
        credential_id=cred_uuid,
        purpose=purpose,
    )

    safe_next = _safe_next_path(next)
    redirect = RedirectResponse(safe_next, status_code=303)
    _clear_state_cookie(redirect, ASSERT_COOKIE)
    return redirect


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _request_db(request: Request) -> Session:
    db = getattr(request.state, "db", None)
    if db is None:
        # Public paths might not have a middleware-provided session.
        db = SessionLocal()
        request.state.db = db
    return db


def _require_user_or_redirect(request: Request):
    user = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    return user


def _client_ip(request: Request) -> Optional[str]:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _safe_next_path(value: str) -> str:
    """Restrict ``next`` to relative paths so it can't redirect off-site."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def _flash_error(
    request: Request, message: str, *, redirect_to: Optional[str] = None
) -> Response:
    """Tiny error responder used by the passkey routes.

    Intentionally minimal. A future iteration can move this onto the
    main template flash mechanism if/when one exists.
    """
    if redirect_to:
        return RedirectResponse(_safe_next_path(redirect_to), status_code=303)
    return JSONResponse({"error": message}, status_code=400)

"""Account resolution from a normalized OIDC identity.

Locked behaviour (see Phase 1 plan):

* Lookup is always ``(provider, subject)``. Match → log in.
* No match: only consider linking by email when the IdP returned
  ``email_verified=True`` AND the candidate user is a single account.
  Even then, **never silently merge** — return a ``LinkRequired`` outcome
  that the route layer turns into a "log in with the other provider to
  prove ownership" page.
* Apple private relay addresses are pre-flagged as ``email_verified=False``
  upstream in ``providers.normalize_userinfo``, so they reach this module
  as unverified by construction.
* If there's no email match either, create a new ``UserAccount`` and a
  fresh ``Identity`` row.

This module is deliberately I/O-bound to a single Session and produces
plain Python outcomes so it's trivial to unit-test without HTTP.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as M
from .providers import NormalizedIdentity


@dataclass(frozen=True)
class LoggedIn:
    """The identity matched an existing user; sign them in."""

    user_id: int
    identity_id: int


@dataclass(frozen=True)
class CreatedAccount:
    """First sign-in: a new user account was created."""

    user_id: int
    identity_id: int


@dataclass(frozen=True)
class LinkRequired:
    """An account exists with the same verified email under a different
    provider. The route layer must redirect the user through the link
    flow rather than auto-merging.
    """

    existing_user_id: int
    candidate_email: str
    incoming: NormalizedIdentity


Outcome = Union[LoggedIn, CreatedAccount, LinkRequired]


def resolve_identity(db: Session, ident: NormalizedIdentity) -> Outcome:
    """Apply the linking-safety rules and return one of the three outcomes."""

    # 1. Exact (provider, subject) match — log in.
    match = db.execute(
        select(M.Identity).where(
            M.Identity.provider == ident.provider,
            M.Identity.subject == ident.subject,
        )
    ).scalar_one_or_none()
    if match is not None:
        # Refresh the snapshot fields in case the upstream IdP rotated
        # them. Subject is immutable so the row's identity is unchanged.
        if ident.email and match.email != ident.email:
            match.email = ident.email
        if ident.email_verified and not match.email_verified:
            match.email_verified = True
        db.flush()
        return LoggedIn(user_id=match.user_id, identity_id=match.id)

    # 2. No (provider, subject) match. Try email-based linking *only* when
    #    the IdP says email is verified AND we treat it as linking-safe
    #    (private-relay addresses fail this check upstream).
    if ident.email and ident.email_verified:
        candidate = db.execute(
            select(M.UserAccount).where(M.UserAccount.email == ident.email)
        ).scalar_one_or_none()
        if candidate is not None:
            return LinkRequired(
                existing_user_id=candidate.id,
                candidate_email=ident.email,
                incoming=ident,
            )

    # 3. No safe match anywhere — create a new account + identity.
    #    The canonical email goes on user_account only when verified AND
    #    not already taken by another account. An unverified email that
    #    happens to collide with an existing account falls back to a
    #    synthetic placeholder so the unique constraint can't fail and so
    #    the existing user's email isn't squatted.
    canonical_email = ident.email if (ident.email and ident.email_verified) else None
    if canonical_email is not None:
        collision = db.execute(
            select(M.UserAccount).where(M.UserAccount.email == canonical_email)
        ).scalar_one_or_none()
        if collision is not None:
            canonical_email = None
    if canonical_email is None:
        canonical_email = _placeholder_email(ident)

    user = M.UserAccount(
        email=canonical_email,
        display_name=ident.display_name,
    )
    db.add(user)
    db.flush()
    identity = M.Identity(
        user_id=user.id,
        provider=ident.provider,
        subject=ident.subject,
        email=ident.email,
        email_verified=ident.email_verified,
    )
    db.add(identity)
    db.flush()
    return CreatedAccount(user_id=user.id, identity_id=identity.id)


def link_identity_to_user(
    db: Session, *, user_id: int, ident: NormalizedIdentity
) -> M.Identity:
    """Attach a new identity to an existing user.

    Caller is responsible for proving the user owns both providers (the
    link-flow route does this by requiring a fresh login from the
    existing provider before calling here).
    """
    if ident.provider not in ("apple", "google", "microsoft"):
        raise ValueError(f"unknown provider {ident.provider!r}")

    # Block re-linking an identity that already belongs to a different user.
    existing = db.execute(
        select(M.Identity).where(
            M.Identity.provider == ident.provider,
            M.Identity.subject == ident.subject,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.user_id != user_id:
            raise ValueError("identity is already linked to a different account")
        return existing

    row = M.Identity(
        user_id=user_id,
        provider=ident.provider,
        subject=ident.subject,
        email=ident.email,
        email_verified=ident.email_verified,
    )
    db.add(row)
    db.flush()
    return row


def _placeholder_email(ident: NormalizedIdentity) -> str:
    """Return a synthetic email when none was provided.

    Some IdPs (notably Apple with private relay disabled) won't return any
    email at all. Storing a synthetic ``<provider>+<subject>@no-email.tbdtask``
    keeps the unique constraint happy without inviting accidental linking.
    The user can update it later through profile settings.
    """
    return f"{ident.provider}+{ident.subject}@no-email.tbdtask"

"""Clerk OAuth/email-code bridge for the *entire* MYRAA workspace.

Clerk verifies identity; MYRAA keeps local approval and access roles.  No
email/password fallback is trusted in Clerk mode.  Signing a short MYRAA
HttpOnly cookie permits existing no-bundler frontend and image endpoints to
work without copying third-party JWTs into every legacy fetch call.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import uuid
from functools import lru_cache
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import Base, SessionLocal
from workspace_models import WorkspaceUser
from workspace_auth import COOKIE, sign_session, user_data, get_user_from_cookie
from services.app_auth import origin_allowed

router = APIRouter(prefix="/api/clerk", tags=["Clerk full-app authentication"])
BOUND_COOKIE = "myraa_clerk_bound"
# Short-lived server cookie; the Clerk browser session can refresh it.
SESSION_MAX_AGE = 15 * 60


class ClerkIdentity(Base):
    __tablename__ = "myraa_clerk_identities"
    id = Column(Integer, primary_key=True)
    clerk_user_id = Column(String(160), unique=True, nullable=False, index=True)
    workspace_user_id = Column(Integer, ForeignKey("myraa_workspace_users.id"), nullable=False, unique=True)


def configured() -> bool:
    return bool(os.getenv("CLERK_PUBLISHABLE_KEY") and os.getenv("CLERK_SECRET_KEY")
                and os.getenv("CLERK_AUTHORIZED_PARTIES") and os.getenv("MYRAA_USER_SESSION_SECRET"))


def allowed_parties() -> list[str]:
    entries = [x.strip().rstrip("/") for x in os.getenv("CLERK_AUTHORIZED_PARTIES", "").split(",") if x.strip()]
    if not entries or any(urlsplit(e).scheme != "https" or not urlsplit(e).hostname or urlsplit(e).path for e in entries):
        raise HTTPException(503, "CLERK_AUTHORIZED_PARTIES must list HTTPS origins, not paths")
    return entries


def binding_signature(session_cookie: str) -> str:
    secret = os.getenv("MYRAA_USER_SESSION_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(503, "MYRAA_USER_SESSION_SECRET missing")
    return hmac.new(secret.encode(), ("clerk-bound:" + session_cookie).encode(), hashlib.sha256).hexdigest()


def bound_user_from_request(request: Request) -> WorkspaceUser | None:
    session_cookie = request.cookies.get(COOKIE, "")
    bound = request.cookies.get(BOUND_COOKIE, "")
    if not session_cookie or len(bound) != 64 or not hmac.compare_digest(binding_signature(session_cookie), bound):
        return None
    with SessionLocal() as db:
        account = get_user_from_cookie(session_cookie, db)
        if account is None:
            return None
        # Prevent granting a session to an unlinked old local-only identity.
        if not db.query(ClerkIdentity.id).filter(ClerkIdentity.workspace_user_id == account.id).first():
            return None
        return account


@lru_cache(maxsize=1)
def _clerk_sdk():
    # Import on demand so missing dependency fails as a CONFIG error rather
    # than turning off protection. Install clerk-backend-api on Railway.
    from clerk_backend_api import Clerk
    return Clerk(bearer_auth=os.environ["CLERK_SECRET_KEY"])


def validate_clerk_token(request: Request) -> str:
    bearer = request.headers.get("authorization", "")
    if not bearer.startswith("Bearer ") or not bearer[7:].strip():
        raise HTTPException(401, "Clerk session token required")
    try:
        from clerk_backend_api.security.types import AuthenticateRequestOptions
        options = AuthenticateRequestOptions(
            authorized_parties=allowed_parties(),
            jwt_key=os.getenv("CLERK_JWT_KEY", "").replace("\\n", "\n") or None,
        )
        state = _clerk_sdk().authenticate_request(request, options)
        if not state.is_signed_in:
            raise HTTPException(401, "Clerk session invalid or expired")
        payload = state.payload or {}
        sub = payload.get("sub", "")
        if not isinstance(sub, str) or not re.fullmatch(r"user_[a-zA-Z0-9]+", sub):
            raise HTTPException(401, "Invalid Clerk user ID")
        return sub
    except HTTPException:
        raise
    except (ImportError, KeyError) as exc:
        raise HTTPException(503, "Clerk SDK/server keys are not configured") from exc
    except Exception as exc:
        raise HTTPException(401, "Clerk session verification failed") from exc


def clerk_verified_identity(user_id: str) -> tuple[str, str]:
    """Obtain VERIFIED primary email from Clerk's server-side API, never JS."""
    key = os.getenv("CLERK_SECRET_KEY", "")
    if not key:
        raise HTTPException(503, "CLERK_SECRET_KEY missing")
    try:
        with httpx.Client(timeout=12) as client:
            r = client.get("https://api.clerk.com/v1/users/" + user_id,
                           headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
            r.raise_for_status()
            info = r.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Could not verify account email with Clerk")
    if info.get("id") != user_id:
        raise HTTPException(401, "Clerk account ID mismatch")
    primary = info.get("primary_email_address_id")
    for entry in info.get("email_addresses", []):
        if (entry.get("id") == primary
                and (entry.get("verification") or {}).get("status") == "verified"):
            email = str(entry.get("email_address", "")).strip().lower()
            if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                name = " ".join(str(info.get(k) or "").strip() for k in ("first_name", "last_name")).strip()
                return email, (name or email.split("@", 1)[0])[:160]
    raise HTTPException(403, "Verified primary email required")


def ensure_clerk_account(db: Session, clerk_id: str, email: str, name: str) -> WorkspaceUser:
    link = db.query(ClerkIdentity).filter(ClerkIdentity.clerk_user_id == clerk_id).first()
    if link:
        account = db.get(WorkspaceUser, link.workspace_user_id)
        if not account:
            raise HTTPException(403, "Linked MYRAA account missing")
        # Email changes need explicit admin review; no silent account switching.
        if account.email.casefold() != email.casefold():
            raise HTTPException(403, "Your Clerk email changed; ask admin to relink the account")
        return account
    account = db.query(WorkspaceUser).filter(WorkspaceUser.email == email).first()
    if account and db.query(ClerkIdentity.id).filter(ClerkIdentity.workspace_user_id == account.id).first():
        raise HTTPException(409, "This email is linked to a different Clerk account; contact admin")
    if not account:
        account = WorkspaceUser(
            login_id="clerk-" + uuid.uuid4().hex[:18], email=email,
            display_name=name, business_name="", password_hash="clerk-managed-no-password",
            role="viewer", verified=True, approved=False, active=True,
            token_version=uuid.uuid4().hex,
        )
        db.add(account)
        db.flush()
    else:
        # Only attach to an existing admin/seller when Clerk has cryptographically
        # verified the PRIMARY email and it matches the stored account email.
        if not account.verified:
            account.verified = True
    db.add(ClerkIdentity(clerk_user_id=clerk_id, workspace_user_id=account.id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Concurrent account linking conflict; retry sign-in")
    db.refresh(account)
    return account


@router.get("/config")
def public_config():
    # Publishable key is intentionally public; never return secret key or JWT key.
    return {"configured": configured(), "publishableKey": os.getenv("CLERK_PUBLISHABLE_KEY", "") if configured() else ""}


@router.post("/exchange")
def exchange(request: Request):
    if not configured():
        raise HTTPException(503, "Clerk settings incomplete on Railway")
    if not origin_allowed(request):
        raise HTTPException(403, "Origin is not allowed")
    clerk_id = validate_clerk_token(request)
    email, name = clerk_verified_identity(clerk_id)
    with SessionLocal() as db:
        account = ensure_clerk_account(db, clerk_id, email, name)
        if not account.active:
            raise HTTPException(403, "MYRAA account disabled by admin")
        if not account.approved:
            response = JSONResponse({"ok": True, "approved": False, "user": user_data(account),
                                     "message": "Verified with Clerk. Waiting for MYRAA admin approval."})
            response.delete_cookie(COOKIE, path="/")
            response.delete_cookie(BOUND_COOKIE, path="/")
            return response
        cookie = sign_session(account)
        response = JSONResponse({"ok": True, "approved": True, "user": user_data(account)})
        # Use short expiry, bounded by Clerk's ongoing browser session refresh.
        secure = request.url.scheme == "https" or bool(os.getenv("RAILWAY_ENVIRONMENT"))
        response.set_cookie(COOKIE, cookie, max_age=SESSION_MAX_AGE, path="/",
                            httponly=True, secure=secure, samesite="strict")
        response.set_cookie(BOUND_COOKIE, binding_signature(cookie), max_age=SESSION_MAX_AGE,
                            path="/", httponly=True, secure=secure, samesite="strict")
        return response


@router.post("/logout")
def clerk_logout(request: Request):
    if not origin_allowed(request):
        raise HTTPException(403, "Invalid origin")
    # Remove server cookies even if Clerk session itself has expired.
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    response.delete_cookie(BOUND_COOKIE, path="/")
    return response

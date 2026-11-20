"""Full-MYRAA account registration, email OTP, approval, login, and user profiles.

Approved sellers see the *same* inventory workspace. This is shared-team access,
not per-seller data isolation: separate stores require a tenant migration.
Approval and role changes are restricted to an authenticated admin account.\nThe owner account is bootstrapped once through the private Railway console.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import re
import secrets
import smtplib
import uuid
import time
from collections import defaultdict, deque
from threading import Lock
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from PIL import Image, UnidentifiedImageError

from database import get_db, SessionLocal
from workspace_models import WorkspaceUser
from services import app_auth

router = APIRouter(prefix="/api/workspace-auth", tags=["MYRAA full app login/signup"])
admin = APIRouter(prefix="/api/workspace-admin", tags=["Workspace user administration"])
COOKIE = "myraa_user_session"
MAX_AGE = 12 * 3600
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
ID_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{3,80}$")
PHOTO_DIR = Path(os.getenv("MYRAA_USER_PHOTO_DIR", "/app/private/user_profiles")).resolve()
_signup_attempts = defaultdict(deque)
_signup_lock = Lock()


def signup_rate_limit(ip: str):
    """Basic per-process signup throttling; use a shared rate limiter for multi-replica deployments."""
    tick = time.monotonic()
    with _signup_lock:
        entries = _signup_attempts[ip]
        while entries and tick - entries[0] > 3600:
            entries.popleft()
        if len(entries) >= 5:
            raise HTTPException(429, "Too many registrations from this address. Try later.")
        entries.append(tick)
        if len(_signup_attempts) > 5000:
            for key in list(_signup_attempts)[:500]:
                _signup_attempts.pop(key, None)


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def secret() -> bytes:
    value = os.getenv("MYRAA_USER_SESSION_SECRET", "")
    if len(value) < 32:
        raise HTTPException(503, "Set MYRAA_USER_SESSION_SECRET to a random 32+ character value")
    return value.encode()


def origin_guard(request: Request):
    if not app_auth.origin_allowed(request):
        raise HTTPException(403, "Invalid request origin")


def mail_configured() -> bool:
    return all([os.getenv("MYRAA_SMTP_HOST") or os.getenv("SELLER_SMTP_HOST"),
                os.getenv("MYRAA_SMTP_USER") or os.getenv("SELLER_SMTP_USER"),
                os.getenv("MYRAA_SMTP_PASSWORD") or os.getenv("SELLER_SMTP_PASSWORD"),
                os.getenv("MYRAA_SMTP_FROM") or os.getenv("SELLER_SMTP_FROM")])


def send_code(email: str, code: str):
    if not mail_configured():
        raise HTTPException(503, "Email OTP unavailable: configure MYRAA_SMTP_* settings")
    host = os.getenv("MYRAA_SMTP_HOST") or os.getenv("SELLER_SMTP_HOST")
    username = os.getenv("MYRAA_SMTP_USER") or os.getenv("SELLER_SMTP_USER")
    password = os.getenv("MYRAA_SMTP_PASSWORD") or os.getenv("SELLER_SMTP_PASSWORD")
    sender = os.getenv("MYRAA_SMTP_FROM") or os.getenv("SELLER_SMTP_FROM")
    use_ssl = (os.getenv("MYRAA_SMTP_SSL") or os.getenv("SELLER_SMTP_SSL") or "0") == "1"
    port = int(os.getenv("MYRAA_SMTP_PORT") or os.getenv("SELLER_SMTP_PORT") or ("465" if use_ssl else "587"))
    message = EmailMessage()
    message["From"] = sender
    message["To"] = email
    message["Subject"] = "MYRAA account verification code"
    message.set_content(f"Your MYRAA verification code: {code}\nExpires in 5 minutes. Do not share it.")
    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=12) as smtp:
                smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=12) as smtp:
                smtp.starttls()
                smtp.login(username, password)
                smtp.send_message(message)
    except (OSError, smtplib.SMTPException):
        raise HTTPException(503, "OTP delivery failed. Check SMTP settings")


def hash_password(password: str, salt: bytes | None = None) -> str:
    if not (12 <= len(password) <= 128):
        raise HTTPException(422, "Password must have 12–128 characters")
    salt = salt or secrets.token_bytes(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return "pbkdf2_sha256$310000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(hashed).decode()


def password_matches(password: str, encoded: str) -> bool:
    try:
        algo, rounds, salt, digest = encoded.split("$")
        if algo != "pbkdf2_sha256" or int(rounds) > 2_000_000:
            return False
        given = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(rounds))
        return hmac.compare_digest(given, base64.b64decode(digest))
    except (ValueError, TypeError, AttributeError):
        return False


def make_code(user: WorkspaceUser, kind: str) -> str:
    if user.otp_type == kind and user.otp_last_sent and now() - user.otp_last_sent < timedelta(seconds=60):
        raise HTTPException(429, "Please wait 60 seconds before requesting another code")
    code = f"{secrets.randbelow(1_000_000):06d}"
    nonce = secrets.token_hex(16)
    user.otp_digest = hmac.new(secret(), f"{user.id}:{kind}:{nonce}:{code}".encode(), hashlib.sha256).hexdigest()
    user.otp_nonce = nonce
    user.otp_type = kind
    user.otp_expires = now() + timedelta(minutes=5)
    user.otp_last_sent = now()
    user.otp_attempts = 0
    return code


def check_code(user: WorkspaceUser, kind: str, code: str) -> bool:
    if (not user.otp_digest or user.otp_type != kind or not user.otp_nonce
            or not user.otp_expires or user.otp_expires < now() or user.otp_attempts >= 5):
        return False
    expected = hmac.new(secret(), f"{user.id}:{kind}:{user.otp_nonce}:{code}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, user.otp_digest)


def clear_code(user: WorkspaceUser):
    user.otp_digest = None
    user.otp_nonce = None
    user.otp_type = None
    user.otp_expires = None
    user.otp_attempts = 0


def sign_session(user: WorkspaceUser) -> str:
    exp = int((now() + timedelta(seconds=MAX_AGE)).replace(tzinfo=timezone.utc).timestamp())
    raw = f"{user.id}.{exp}.{user.token_version}"
    signature = hmac.new(secret(), raw.encode(), hashlib.sha256).hexdigest()
    return raw + "." + signature


def get_user_from_cookie(token: str, db: Session) -> WorkspaceUser | None:
    try:
        ident, expiry, version, signature = token.split(".", 3)
        if not ident.isdigit() or len(version) != 32 or len(signature) != 64:
            return None
        exp = int(expiry)
        current = int(now().replace(tzinfo=timezone.utc).timestamp())
        if exp < current or exp > current + MAX_AGE + 60:
            return None
        raw = f"{ident}.{exp}.{version}"
        expected = hmac.new(secret(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return None
        user = db.get(WorkspaceUser, int(ident))
        if not user or not user.active or not user.approved or not user.verified:
            return None
        return user if hmac.compare_digest(user.token_version, version) else None
    except (ValueError, TypeError, AttributeError, HTTPException):
        return None


def account_from_request(request: Request) -> WorkspaceUser | None:
    # Never trust old local password/OTP cookies as proof of Clerk login.
    from clerk_auth import bound_user_from_request
    return bound_user_from_request(request)


def current_user(request: Request, db: Session = Depends(get_db)) -> WorkspaceUser:
    account = account_from_request(request)
    if account is None:
        raise HTTPException(401, "MYRAA user login required")
    # Attach the authenticated identity to this request's active DB session so
    # profile writes persist (the auth-check's object lives in another session).
    attached = db.get(WorkspaceUser, account.id)
    if attached is None or not attached.active or not attached.approved or not attached.verified:
        raise HTTPException(401, "MYRAA user login required")
    return attached


def operator_only(request: Request):
    """Admin privileges require a signed, verified and approved account cookie."""
    account = account_from_request(request)
    if account is None or account.role != "admin":
        raise HTTPException(403, "Only a MYRAA administrator can manage workspace users")



def user_data(user: WorkspaceUser):
    return {"id": user.id, "login_id": user.login_id, "email": user.email,
            "display_name": user.display_name, "business_name": user.business_name,
            "phone": user.phone, "address": user.address,
            "verified": bool(user.verified), "approved": bool(user.approved),
            "active": bool(user.active), "role": user.role,
            "photo_url": "/api/workspace-auth/me/photo" if user.photo_name else None}


class Signup(BaseModel):
    login_id: str = Field(min_length=3, max_length=80)
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=12, max_length=128)
    display_name: str = Field(min_length=2, max_length=160)
    business_name: str = Field(default="", max_length=180)
    phone: str = Field(default="", max_length=32)


class Login(BaseModel):
    login: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class Verify(BaseModel):
    login: str
    code: str = Field(pattern=r"^\d{6}$")


@router.post("/signup")
def signup(body: Signup, request: Request, db: Session = Depends(get_db)):
    origin_guard(request)
    secret()
    if not mail_configured():
        raise HTTPException(503, "Email OTP unavailable: configure MYRAA_SMTP_* variables")
    login = body.login_id.strip().lower()
    email = body.email.strip().lower()
    if not ID_PATTERN.fullmatch(login) or not EMAIL_PATTERN.fullmatch(email):
        raise HTTPException(422, "Use a valid email and login ID (letters, digits, dot, underscore or hyphen)")
    ip = request.client.host if request.client else "unknown"
    signup_rate_limit(ip)
    if db.query(WorkspaceUser).filter(or_(WorkspaceUser.email == email, WorkspaceUser.login_id == login)).first():
        raise HTTPException(409, "Login ID or email already registered")
    account = WorkspaceUser(login_id=login, email=email, password_hash=hash_password(body.password),
                            display_name=body.display_name.strip(), business_name=body.business_name.strip(),
                            phone=body.phone.strip(), role="viewer", approved=False,
                            verified=False, active=True, token_version=uuid.uuid4().hex)
    try:
        db.add(account)
        db.flush()
        code = make_code(account, "signup")
        send_code(email, code)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Login ID or email already registered")
    except Exception:
        db.rollback()
        raise
    return {"ok": True, "message": "Verification code sent to email. Owner approval required after verification."}


@router.post("/verify-signup")
def verify_signup(body: Verify, request: Request, db: Session = Depends(get_db)):
    origin_guard(request)
    account = db.query(WorkspaceUser).filter(or_(WorkspaceUser.login_id == body.login.strip().lower(), WorkspaceUser.email == body.login.strip().lower())).first()
    if not account or not check_code(account, "signup", body.code):
        if account:
            account.otp_attempts += 1
            db.commit()
        raise HTTPException(401, "Verification code invalid or expired")
    account.verified = True
    clear_code(account)
    db.commit()
    return {"ok": True, "message": "Email verified. Awaiting administrator approval."}


class ResendSignup(BaseModel):
    login: str = Field(min_length=3, max_length=255)


@router.post("/resend-signup")
def resend_signup(body: ResendSignup, request: Request, db: Session = Depends(get_db)):
    origin_guard(request)
    secret()
    if not mail_configured():
        raise HTTPException(503, "Email OTP unavailable: configure MYRAA_SMTP_* variables")
    ident = body.login.strip().lower()
    account = db.query(WorkspaceUser).filter(or_(WorkspaceUser.login_id == ident, WorkspaceUser.email == ident)).first()
    # Do not disclose whether an account exists.
    if account and not account.verified:
        code = make_code(account, "signup")
        send_code(account.email, code)
        db.commit()
    return {"ok": True, "message": "If registration is pending, an email verification code was sent."}


@router.post("/login")
def login(body: Login, request: Request, db: Session = Depends(get_db)):
    origin_guard(request)
    secret()
    ip = request.client.host if request.client else "unknown"
    if not app_auth.login_allowed(ip):
        raise HTTPException(429, "Too many failed attempts")
    login_id = body.login.strip().lower()
    account = db.query(WorkspaceUser).filter(or_(WorkspaceUser.login_id == login_id, WorkspaceUser.email == login_id)).first()
    if not account:
        app_auth.record_failed_login(ip)
        raise HTTPException(401, "Invalid login ID or password")
    if account.locked_until and account.locked_until > now():
        raise HTTPException(429, "Account temporarily locked")
    if not password_matches(body.password, account.password_hash):
        app_auth.record_failed_login(ip)
        account.failed_logins += 1
        if account.failed_logins >= 8:
            account.locked_until = now() + timedelta(minutes=15)
            account.failed_logins = 0
        db.commit()
        raise HTTPException(401, "Invalid login ID or password")
    if not account.verified:
        raise HTTPException(403, "Email not verified. Please complete registration OTP.")
    if not account.approved or not account.active:
        raise HTTPException(403, "Your account is awaiting administrator approval or is disabled")
    code = make_code(account, "login")
    send_code(account.email, code)
    account.failed_logins = 0
    db.commit()
    app_auth.clear_failed_logins(ip)
    return {"otp_required": True, "message": "One-time code sent to registered email"}


@router.post("/verify-login")
def verify_login(body: Verify, request: Request, db: Session = Depends(get_db)):
    origin_guard(request)
    ident = body.login.strip().lower()
    account = db.query(WorkspaceUser).filter(or_(WorkspaceUser.login_id == ident, WorkspaceUser.email == ident)).first()
    if not account or not account.verified or not account.approved or not account.active or not check_code(account, "login", body.code):
        if account and account.otp_digest:
            account.otp_attempts += 1
            db.commit()
        raise HTTPException(401, "OTP invalid, expired or account not approved")
    clear_code(account)
    db.commit()
    response = JSONResponse({"ok": True, "user": user_data(account)})
    response.set_cookie(COOKIE, sign_session(account), httponly=True,
                        secure=bool(os.getenv("RAILWAY_ENVIRONMENT") or request.url.scheme == "https"),
                        samesite="strict", max_age=MAX_AGE, path="/")
    return response


@router.get("/status")
def status(request: Request, db: Session = Depends(get_db)):
    account = account_from_request(request)
    return {"authenticated": bool(account), "user": user_data(account) if account else None,
            "signup_enabled": bool(os.getenv("CLERK_PUBLISHABLE_KEY"))}


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    origin_guard(request)
    account = account_from_request(request)
    if account:
        account.token_version = uuid.uuid4().hex
        db.commit()
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    response.delete_cookie("myraa_clerk_bound", path="/")
    return response


@router.get("/me")
def me(account: WorkspaceUser = Depends(current_user)):
    return user_data(account)


class MeUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=2, max_length=160)
    business_name: str | None = Field(default=None, max_length=180)
    phone: str | None = Field(default=None, max_length=32)
    address: str | None = Field(default=None, max_length=2000)


@router.patch("/me")
def update_me(body: MeUpdate, request: Request, account: WorkspaceUser = Depends(current_user), db: Session = Depends(get_db)):
    origin_guard(request)
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(account, field, value.strip())
    db.commit()
    return user_data(account)


class ChangePassword(BaseModel):
    current_password: str
    new_password: str = Field(min_length=12, max_length=128)


@router.post("/me/password")
def change_password(body: ChangePassword, request: Request, account: WorkspaceUser = Depends(current_user), db: Session = Depends(get_db)):
    origin_guard(request)
    if not password_matches(body.current_password, account.password_hash):
        raise HTTPException(401, "Current password incorrect")
    account.password_hash = hash_password(body.new_password)
    account.token_version = uuid.uuid4().hex
    db.commit()
    response = JSONResponse({"ok": True, "message": "Password updated. Sign in again."})
    response.delete_cookie(COOKIE, path="/")
    return response


def photo_path(filename: str) -> Path:
    if not filename or Path(filename).name != filename:
        raise HTTPException(404, "Photo unavailable")
    return PHOTO_DIR / filename


@router.post("/me/photo")
async def upload_photo(request: Request, file: UploadFile = File(...), account: WorkspaceUser = Depends(current_user), db: Session = Depends(get_db)):
    origin_guard(request)
    content = await file.read(2_000_001)
    if not content or len(content) > 2_000_000:
        raise HTTPException(422, "Photo max 2 MB")
    try:
        with Image.open(io.BytesIO(content)) as picture:
            if picture.format not in ("PNG", "WEBP", "JPEG"):
                raise HTTPException(422, "Only JPG, PNG, WEBP")
            picture.thumbnail((512, 512))
            data = io.BytesIO()
            picture.convert("RGB").save(data, "WEBP", quality=82)
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise HTTPException(422, "Invalid image")
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex + ".webp"
    photo_path(name).write_bytes(data.getvalue())
    old = account.photo_name
    account.photo_name = name
    db.commit()
    if old:
        photo_path(old).unlink(missing_ok=True)
    return {"photo_url": "/api/workspace-auth/me/photo?v=" + uuid.uuid4().hex[:8]}


@router.get("/me/photo")
def get_photo(account: WorkspaceUser = Depends(current_user)):
    if not account.photo_name or not photo_path(account.photo_name).is_file():
        raise HTTPException(404, "No profile photo")
    return FileResponse(photo_path(account.photo_name), media_type="image/webp", headers={"Cache-Control": "private, no-store"})


@admin.get("/users")
def list_users(request: Request, q: str = "", db: Session = Depends(get_db)):
    operator_only(request)
    query = db.query(WorkspaceUser).order_by(WorkspaceUser.created_at.desc())
    if q.strip():
        needle = f"%{q.strip()[:100]}%"
        query = query.filter(or_(WorkspaceUser.email.ilike(needle), WorkspaceUser.login_id.ilike(needle), WorkspaceUser.display_name.ilike(needle)))
    return {"items": [user_data(x) for x in query.limit(250).all()]}


class Approval(BaseModel):
    approved: bool
    active: bool = True
    role: str = Field(pattern=r"^(viewer|manager)$")


@admin.patch("/users/{user_id}")
def approve(user_id: int, body: Approval, request: Request, db: Session = Depends(get_db)):
    operator_only(request)
    origin_guard(request)
    account = db.get(WorkspaceUser, user_id)
    if not account:
        raise HTTPException(404, "User not found")
    if account.role == "admin":
        raise HTTPException(403, "Admin account permissions cannot be changed from seller management")
    if body.approved and not account.verified:
        raise HTTPException(422, "Email must be verified before approval")
    account.approved = body.approved
    account.active = body.active
    account.role = body.role
    account.token_version = uuid.uuid4().hex  # Revoke previous sessions on role/approval change.
    db.commit()
    return user_data(account)

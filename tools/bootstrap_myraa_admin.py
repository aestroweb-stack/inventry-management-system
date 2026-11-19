#!/usr/bin/env python3
"""One-time secure admin bootstrap. No APP_PASSWORD and no plaintext credentials.

Run in the deployed Railway service console only, e.g.:
  cd /app/backend
  /app/.venv/bin/python ../tools/bootstrap_myraa_admin.py --email you@example.com

This script does not send email. The new admin must still receive a 6-digit
email OTP during normal login (SMTP must be configured before sign-in).
"""
from __future__ import annotations
import argparse
import getpass
import os
from pathlib import Path
import re
import sys
import uuid

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if not BACKEND.is_dir():
    raise SystemExit("Missing backend/ folder")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)  # Align SQLite relative path with the running app process.
from database import Base, SessionLocal, engine
from workspace_models import WorkspaceUser
from workspace_auth import hash_password


def main():
    parser = argparse.ArgumentParser(description="Create the first MYRAA admin account securely")
    parser.add_argument("--email", required=True, help="Admin inbox that will receive login OTP")
    parser.add_argument("--login-id", default="admin", help="Unique MYRAA login ID (default: admin)")
    parser.add_argument("--name", default="MYRAA Administrator", help="Visible profile name")
    args = parser.parse_args()
    email = args.email.strip().lower()
    login_id = args.login_id.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise SystemExit("Provide a valid admin email address")
    if not re.fullmatch(r"[a-z0-9._-]{3,80}", login_id):
        raise SystemExit("Login ID must be 3-80 letters, digits, dots, hyphens or underscores")
    if len(os.getenv("MYRAA_USER_SESSION_SECRET", "")) < 32:
        raise SystemExit("Set MYRAA_USER_SESSION_SECRET (32+ random characters) in Railway first")
    Base.metadata.create_all(bind=engine, tables=[WorkspaceUser.__table__])
    with SessionLocal() as db:
        if db.query(WorkspaceUser).filter(WorkspaceUser.role == "admin").first():
            raise SystemExit("Admin already exists; refusing to create a second one")
        if db.query(WorkspaceUser).filter((WorkspaceUser.email == email) | (WorkspaceUser.login_id == login_id)).first():
            raise SystemExit("Email or login ID is already registered; choose another unique ID")
        pw1 = getpass.getpass("New admin account password (12+ chars): ")
        pw2 = getpass.getpass("Confirm admin password: ")
        if pw1 != pw2:
            raise SystemExit("Passwords do not match. No account created.")
        digest = hash_password(pw1)
        user = WorkspaceUser(login_id=login_id, email=email, display_name=args.name,
                             password_hash=digest, business_name="MYRAA",
                             role="admin", verified=True, approved=True, active=True,
                             token_version=uuid.uuid4().hex)
        db.add(user)
        db.commit()
        print("PASS: MYRAA admin account created; login ID:", login_id)
        print("NEXT: Log in via web form and verify the OTP sent to:", email)
        print("APP_PASSWORD is not used. Do not remove any old access until OTP login works.")


if __name__ == "__main__":
    main()

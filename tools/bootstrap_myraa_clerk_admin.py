#!/usr/bin/env python3
"""One-time MYRAA admin bootstrap for Clerk. NO app password or SMTP.

After deployment, run only in Railway shell:
    cd /app/backend
    /app/.venv/bin/python ../tools/bootstrap_myraa_clerk_admin.py --email YOUR_GOOGLE_EMAIL

The admin must sign in to Clerk with this exact verified primary email.
"""
from __future__ import annotations
import argparse
import os
import re
import sys
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / 'backend'
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)  # Keep the deployment's SQLite path consistent.
from database import Base, SessionLocal, engine
from workspace_models import WorkspaceUser
from clerk_auth import ClerkIdentity

# Refuse to touch a different relative SQLite DB from the running server.
# On Railway, PID 1 is normally the web server process.
if engine.url.get_backend_name() == 'sqlite' and engine.url.database and not os.path.isabs(engine.url.database):
    try:
        service_cwd = Path('/proc/1/cwd').resolve(strict=True)
    except (OSError, RuntimeError):
        service_cwd = None
    if service_cwd is not None and service_cwd != BACKEND.resolve():
        raise SystemExit(
            'STOP: Web server cwd is ' + str(service_cwd) + ', but admin script uses ' + str(BACKEND) +
            '. Confirm DATABASE_URL and actual SQLite database path before proceeding.'
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--email', required=True)
    parser.add_argument('--name', default='MYRAA Administrator')
    parser.add_argument('--promote-verified-pending', action='store_true',
                        help='Explicitly promote a linked Clerk-verified pending account when NO admin exists')
    args=parser.parse_args()
    email=args.email.strip().lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise SystemExit('Invalid admin email address')
    if len(os.getenv('MYRAA_USER_SESSION_SECRET', '')) < 32:
        raise SystemExit('Set MYRAA_USER_SESSION_SECRET in Railway first')
    Base.metadata.create_all(bind=engine, tables=[WorkspaceUser.__table__])
    with SessionLocal() as db:
        admins=db.query(WorkspaceUser).filter(WorkspaceUser.role=='admin').all()
        if admins:
            if len(admins)==1 and admins[0].email.lower()==email:
                print('PASS: Existing MYRAA admin email already matches. Clerk will link on verified login.')
                return 0
            raise SystemExit('An admin already exists with another email. Existing admin must be reviewed manually; no overwrite performed.')
        existing=db.query(WorkspaceUser).filter(WorkspaceUser.email==email).first()
        if existing:
            if not args.promote_verified_pending:
                raise SystemExit('This Clerk account already exists as a seller. Re-run with '
                                 '--promote-verified-pending ONLY for your own verified first-admin account.')
            if not existing.verified or not existing.active or existing.approved or existing.role!='viewer':
                raise SystemExit('Account is not an active, verified, unapproved viewer; no changes made.')
            link=db.query(ClerkIdentity).filter(ClerkIdentity.workspace_user_id==existing.id).first()
            if not link:
                raise SystemExit('No verified Clerk identity is linked to this account; no changes made.')
            existing.role='admin'
            existing.approved=True
            existing.token_version=uuid.uuid4().hex  # Revoke stale session
            db.commit()
            print('PASS: Existing verified Clerk account promoted to MYRAA first admin:', email)
            print('Click Check access on the login page, or refresh it and sign in again.')
            return 0
        user=WorkspaceUser(login_id='admin',email=email,display_name=args.name,
                           password_hash='clerk-managed-no-password',business_name='MYRAA',
                           role='admin',verified=True,approved=True,active=True,token_version=uuid.uuid4().hex)
        db.add(user);db.commit()
        print('PASS: Admin created for verified Clerk email:',email)
        print('Next: sign in with Google / email OTP using this exact address.')
        print('No APP_PASSWORD, SMTP, or local account password is required.')
        return 0

if __name__ == '__main__':
    raise SystemExit(main())

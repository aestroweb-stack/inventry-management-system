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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--email', required=True)
    parser.add_argument('--name', default='MYRAA Administrator')
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
        if db.query(WorkspaceUser).filter(WorkspaceUser.email==email).first():
            raise SystemExit('Email belongs to an existing seller. Review it and promote explicitly in DB only after verifying ownership.')
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

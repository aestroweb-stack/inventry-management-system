#!/usr/bin/env python3
"""Safely transfer the mistaken placeholder-only bootstrap admin to a previously
Clerk-verified pending user. Run from the Railway app's backend environment.

Dry run by default; never reads stdin or asks an interactive question.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / 'backend'
if not BACKEND.exists():
    # Also allow this file to be copied directly to /app/tools/ (same layout).
    raise SystemExit('STOP: Place this script in /app/tools/ next to /app/backend/')
sys.path.insert(0, str(BACKEND))

import os
os.chdir(BACKEND)
from sqlalchemy import func
from database import SessionLocal, engine
from workspace_models import WorkspaceUser
from clerk_auth import ClerkIdentity

PLACEHOLDER = 'your_google_email@gmail.com'


def check_db_path():
    driver = engine.url.get_backend_name()
    print('Database driver:', driver)
    if driver != 'sqlite':
        return None
    raw = engine.url.database
    if not raw or raw == ':memory:':
        raise SystemExit('STOP: SQLite database path is missing/in-memory.')
    file = Path(raw).resolve()
    if not file.is_file():
        raise SystemExit('STOP: SQLite database not present at ' + str(file))
    if not Path(raw).is_absolute():
        try:
            cwd = Path('/proc/1/cwd').resolve(strict=True)
        except (OSError, RuntimeError):
            raise SystemExit('STOP: Unable to verify running web server cwd for relative SQLite DB.')
        if cwd != BACKEND.resolve():
            raise SystemExit('STOP: Web server cwd differs: ' + str(cwd)
                             + ' (script cwd ' + str(BACKEND) + '). Confirm correct database.')
    print('SQLite file:', file)
    return file


def backup_sqlite(file):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    destination = file.with_name(f'{file.stem}_before_admin_transfer_{stamp}_{uuid.uuid4().hex[:6]}.db')
    with sqlite3.connect(str(file)) as src, sqlite3.connect(str(destination)) as dst:
        src.backup(dst)
    print('BACKUP:', destination)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--email', required=True, help='Your REAL verified Google login email (not the example placeholder)')
    p.add_argument('--execute', action='store_true', help='Apply transfer after successful dry run and backup')
    p.add_argument('--backup-confirmed', action='store_true', help='Required for non-SQLite database writes')
    args = p.parse_args()
    target = args.email.strip().casefold()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', target) or target == PLACEHOLDER:
        raise SystemExit('STOP: Replace the example email with YOUR real verified Google email.')

    db_file = check_db_path()
    with SessionLocal() as db:
        admins = db.query(WorkspaceUser).filter(WorkspaceUser.role == 'admin').all()
        wrong = db.query(WorkspaceUser).filter(func.lower(WorkspaceUser.email) == PLACEHOLDER).one_or_none()
        correct = db.query(WorkspaceUser).filter(func.lower(WorkspaceUser.email) == target).one_or_none()
        print('Admin count:', len(admins))
        print('Placeholder admin present:', bool(wrong))
        print('Target MYRAA user present:', bool(correct))
        if len(admins) != 1 or wrong is None or admins[0].id != wrong.id:
            raise SystemExit('STOP: Admin state is not exactly one placeholder admin; no changes made.')
        if correct is None:
            raise SystemExit('STOP: Real email not registered in MYRAA. Sign in with Google first.')
        if correct.id == wrong.id:
            raise SystemExit('STOP: Placeholder and target cannot be the same account.')

        wrong_link = db.query(ClerkIdentity).filter(ClerkIdentity.workspace_user_id == wrong.id).first()
        correct_link = db.query(ClerkIdentity).filter(ClerkIdentity.workspace_user_id == correct.id).first()
        if wrong_link is not None:
            raise SystemExit('STOP: Placeholder has a linked Clerk identity; manual account review required.')
        if not (correct_link and correct_link.clerk_user_id
                and correct.verified is True and correct.active is True
                and correct.approved is False and correct.role == 'viewer'):
            raise SystemExit('STOP: Target must be a Clerk-linked, verified, active, pending viewer.')
        print('PASS: Conditions verified for transferring admin to:', target)
        if not args.execute:
            print('DRY RUN ONLY: No data changed. Re-run with --execute after reviewing target.')
            return 0
        if db_file is None and not args.backup_confirmed:
            raise SystemExit('STOP: Non-SQLite database requires an independently confirmed backup; '
                             're-run with --backup-confirmed after backup.')
        if db_file is not None:
            backup_sqlite(db_file)
        wrong.role = 'viewer'
        wrong.approved = False
        wrong.active = False
        wrong.token_version = uuid.uuid4().hex
        correct.role = 'admin'
        correct.approved = True
        correct.token_version = uuid.uuid4().hex
        db.commit()
        print('PASS: Real Clerk-verified account is now MYRAA admin:', target)
        print('PASS: Mistaken placeholder admin is disabled (record preserved).')
        return 0


if __name__ == '__main__':
    sys.exit(main())

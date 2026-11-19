"""Offline identity and authorization tests — no email sent and no production DB writes."""
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

root = Path(__file__).resolve().parents[1]
backend = root / 'backend'
if not backend.exists():
    backend = root / 'staging' / 'backend'
sys.path.insert(0, str(backend))
_tmp = tempfile.TemporaryDirectory()
os.environ['DATABASE_URL'] = 'sqlite:///' + str(Path(_tmp.name) / 'test_accounts.sqlite3')
os.environ['MYRAA_USER_SESSION_SECRET'] = 'long-private-test-secret-only-for-offline-tests-12345'

from database import Base, engine, SessionLocal
from workspace_models import WorkspaceUser
from workspace_auth import (hash_password,password_matches,make_code,check_code,clear_code,
                            sign_session,get_user_from_cookie,now)
Base.metadata.create_all(bind=engine)

class UserTests(unittest.TestCase):
    def new_user(self):
        stamp=uuid.uuid4().hex[:8]
        return WorkspaceUser(login_id='test'+stamp,email='u'+stamp+'@example.test',
                             display_name='Demo Seller',business_name='Demo Co',
                             password_hash=hash_password('CorrectPassword123'),
                             verified=False,approved=False,active=True,
                             role='viewer',token_version=uuid.uuid4().hex)

    def test_password_hash_is_salted(self):
        a=hash_password('CorrectPassword123')
        b=hash_password('CorrectPassword123')
        self.assertNotEqual(a,b)
        self.assertTrue(password_matches('CorrectPassword123',a))
        self.assertFalse(password_matches('wrong',a))

    def test_password_minimum(self):
        with self.assertRaises(Exception):hash_password('short')

    def test_verified_and_approved_required_for_session(self):
        with SessionLocal() as db:
            u=self.new_user();db.add(u);db.commit();db.refresh(u)
            token=sign_session(u)
            self.assertIsNone(get_user_from_cookie(token,db))
            u.verified=True;db.commit()
            self.assertIsNone(get_user_from_cookie(token,db))
            u.approved=True;db.commit()
            self.assertIsNotNone(get_user_from_cookie(token,db))
            u.active=False;db.commit()
            self.assertIsNone(get_user_from_cookie(token,db))

    def test_token_tampering_and_revocation(self):
        with SessionLocal() as db:
            u=self.new_user();u.verified=True;u.approved=True;db.add(u);db.commit();db.refresh(u)
            token=sign_session(u)
            self.assertIsNotNone(get_user_from_cookie(token,db))
            self.assertIsNone(get_user_from_cookie(token+'x',db))
            u.token_version=uuid.uuid4().hex;db.commit()
            self.assertIsNone(get_user_from_cookie(token,db))

    def test_one_time_code(self):
        with SessionLocal() as db:
            u=self.new_user();db.add(u);db.flush()
            otp=make_code(u,'signup')
            self.assertEqual(len(otp),6)
            self.assertTrue(check_code(u,'signup',otp))
            self.assertFalse(check_code(u,'login',otp))
            self.assertFalse(check_code(u,'signup','000000') if otp!='000000' else check_code(u,'signup','111111'))
            clear_code(u)
            self.assertFalse(check_code(u,'signup',otp))
            db.rollback()

    def test_viewer_and_manager_roles_are_distinct(self):
        u=self.new_user()
        self.assertEqual(u.role,'viewer')
        u.role='manager'
        self.assertEqual(u.role,'manager')

class AuthAPITests(unittest.TestCase):
    def test_complete_signup_approval_and_login(self):
        from unittest.mock import patch
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from workspace_auth import router,admin
        server=FastAPI(); server.include_router(router);server.include_router(admin)
        c=TestClient(server)
        name=uuid.uuid4().hex[:9]
        with patch('workspace_auth.mail_configured',return_value=True), patch('workspace_auth.send_code') as sender:
            r=c.post('/api/workspace-auth/signup',json={'login_id':name,'display_name':'Test Seller','email':name+'@example.test','password':'StrongSellerPassword123','business_name':'Test Boutique'})
            self.assertEqual(r.status_code,200,r.text)
            code=sender.call_args.args[1]
            r=c.post('/api/workspace-auth/verify-signup',json={'login':name,'code':code})
            self.assertEqual(r.status_code,200,r.text)
            r=c.post('/api/workspace-auth/login',json={'login':name,'password':'StrongSellerPassword123'})
            self.assertEqual(r.status_code,403,r.text)
            r=c.get('/api/workspace-admin/users')
            self.assertEqual(r.status_code,403)
            owner=TestClient(server)
            with SessionLocal() as db:
                admin=WorkspaceUser(login_id='admin'+uuid.uuid4().hex[:8],email='admin'+uuid.uuid4().hex[:8]+'@example.test',
                                   display_name='Test Admin',password_hash=hash_password('AdminPassword12345'),
                                   verified=True,approved=True,active=True,role='admin',token_version=uuid.uuid4().hex)
                db.add(admin);db.commit();db.refresh(admin)
                admin_cookie=sign_session(admin)
            owner.cookies.set('myraa_user_session',admin_cookie)
            r=owner.get('/api/workspace-admin/users')
            self.assertEqual(r.status_code,200,r.text)
            item=next(x for x in r.json()['items'] if x['login_id']==name)
            r=owner.patch('/api/workspace-admin/users/'+str(item['id']),json={'approved':True,'role':'manager','active':True})
            self.assertEqual(r.status_code,200,r.text)
            r=c.post('/api/workspace-auth/login',json={'login':name,'password':'StrongSellerPassword123'})
            self.assertEqual(r.status_code,200,r.text)
            otp=sender.call_args.args[1]
            r=c.post('/api/workspace-auth/verify-login',json={'login':name,'code':otp})
            self.assertEqual(r.status_code,200,r.text)
            self.assertIn('myraa_user_session',c.cookies)
            r=c.get('/api/workspace-auth/me')
            self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(r.json()['role'],'manager')
            r=c.patch('/api/workspace-auth/me',json={'phone':'+91-9999999999'})
            self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(r.json()['phone'],'+91-9999999999')
            r=c.post('/api/workspace-auth/logout')
            self.assertEqual(r.status_code,200,r.text)
            r=c.get('/api/workspace-auth/me')
            self.assertEqual(r.status_code,401)

if __name__ == "__main__": unittest.main()

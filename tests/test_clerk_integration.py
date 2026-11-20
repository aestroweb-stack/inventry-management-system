"""Offline Clerk bridge tests — never call real OAuth, email, or production DB."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient
from database import Base
import workspace_models
import clerk_auth
import workspace_auth

class ClerkIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread':False}, poolclass=StaticPool)
        Base.metadata.create_all(bind=self.engine, tables=[workspace_models.WorkspaceUser.__table__, clerk_auth.ClerkIdentity.__table__])
        self.Session = sessionmaker(bind=self.engine)
        self.patchsession=patch.object(clerk_auth,'SessionLocal',self.Session)
        self.patchwa=patch.object(workspace_auth,'SessionLocal',self.Session)
        self.patchsession.start();self.patchwa.start()
        self.env=patch.dict(os.environ,{
            'MYRAA_USER_SESSION_SECRET':'X'*64,
            'CLERK_PUBLISHABLE_KEY':'pk_test_dGVzdC5jbGVyay5hY2NvdW50cy5kZXYk',
            'CLERK_SECRET_KEY':'sk_test_FAKE_FOR_OFFLINE_TEST_ONLY',
            'CLERK_AUTHORIZED_PARTIES':'https://inventory.example.com',
        })
        self.env.start()
        self.identity=patch.object(clerk_auth,'validate_clerk_token',return_value='user_ABC123')
        self.verified=patch.object(clerk_auth,'clerk_verified_identity',return_value=('admin@example.com','Admin'))
        self.identity.start();self.verified.start()
        app=FastAPI();app.include_router(clerk_auth.router);app.include_router(workspace_auth.router);app.include_router(workspace_auth.admin)
        def isolated_db():
            with self.Session() as session:
                yield session
        app.dependency_overrides[workspace_auth.get_db] = isolated_db
        self.client=TestClient(app,base_url='https://inventory.example.com')

    def tearDown(self):
        self.identity.stop();self.verified.stop();self.env.stop();self.patchwa.stop();self.patchsession.stop()
        self.engine.dispose()

    def exchange(self):
        return self.client.post('/api/clerk/exchange',headers={
            'Authorization':'Bearer test.jwt.signature','Origin':'https://inventory.example.com'})

    def test_missing_bearer_rejected(self):
        self.identity.stop()
        try:
            r=self.client.post('/api/clerk/exchange',headers={'Origin':'https://inventory.example.com'})
            self.assertEqual(r.status_code,401)
        finally:self.identity.start()

    def test_new_user_requires_approval(self):
        self.verified.stop()
        self.verified=patch.object(clerk_auth,'clerk_verified_identity',return_value=('seller@example.com','Seller'))
        self.verified.start()
        r=self.exchange()
        self.assertEqual(r.status_code,200)
        self.assertFalse(r.json()['approved'])
        self.assertNotIn(clerk_auth.COOKIE,r.cookies)
        with self.Session() as db:
            user=db.query(workspace_models.WorkspaceUser).filter_by(email='seller@example.com').one()
            self.assertTrue(user.verified)
            self.assertFalse(user.approved)
            self.assertEqual(user.role,'viewer')

    def test_link_existing_admin_secure_cookie(self):
        import uuid
        with self.Session() as db:
            admin=workspace_models.WorkspaceUser(email='admin@example.com',login_id='admin',display_name='Admin',
                password_hash='unused',verified=True,approved=True,active=True,role='admin',token_version=uuid.uuid4().hex)
            db.add(admin);db.commit()
        response=self.exchange()
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()['approved'])
        self.assertIn(clerk_auth.COOKIE,response.cookies)
        self.assertIn(clerk_auth.BOUND_COOKIE,response.cookies)
        from starlette.requests import Request
        cookies={k:v for k,v in self.client.cookies.items()}
        async def recv(): return {'type':'http.request','body':b'','more_body':False}
        request=Request({'type':'http','method':'GET','path':'/api/products',
            'headers':[(b'cookie','; '.join(f'{k}={v}' for k,v in cookies.items()).encode())]},receive=recv)
        account=clerk_auth.bound_user_from_request(request)
        self.assertEqual(account.role,'admin')
        # Old valid local cookie WITHOUT Clerk binding is not accepted.
        request2=Request({'type':'http','method':'GET','path':'/api/products',
            'headers':[(b'cookie',(clerk_auth.COOKIE+'='+cookies[clerk_auth.COOKIE]).encode())]},receive=recv)
        self.assertIsNone(clerk_auth.bound_user_from_request(request2))

    def test_clerk_email_changed_does_not_auto_takeover(self):
        with self.Session() as db:
            user=clerk_auth.ensure_clerk_account(db,'user_ABC123','admin@example.com','Admin')
            self.assertEqual(user.email,'admin@example.com')
            with self.assertRaises(Exception):
                clerk_auth.ensure_clerk_account(db,'user_ABC123','other@example.com','Other')

    def test_unverified_primary_email_rejected(self):
        class Resp:
            def raise_for_status(self):pass
            def json(self): return {'id':'user_ABC123','primary_email_address_id':'id1','email_addresses':[
                {'id':'id1','email_address':'admin@example.com','verification':{'status':'unverified'}}]}
        class Client:
            def __init__(self,*a,**kw):pass
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def get(self,*a,**kw):return Resp()
        self.verified.stop()
        try:
            with patch.object(clerk_auth.httpx,'Client',Client):
                with self.assertRaises(Exception):clerk_auth.clerk_verified_identity('user_ABC123')
        finally:
            self.verified.start()


    def test_profile_changes_persist_with_clerk_session(self):
        import uuid
        with self.Session() as db:
            db.add(workspace_models.WorkspaceUser(email='admin@example.com',login_id='admin',display_name='Admin',
                password_hash='unused',verified=True,approved=True,active=True,role='admin',token_version=uuid.uuid4().hex))
            db.commit()
        self.assertTrue(self.exchange().json()['approved'])
        r=self.client.patch('/api/workspace-auth/me',headers={'Origin':'https://inventory.example.com'},
            json={'display_name':'Myraa Admin New'})
        self.assertEqual(r.status_code,200,r.text)
        with self.Session() as db:
            user=db.query(workspace_models.WorkspaceUser).filter_by(email='admin@example.com').one()
            self.assertEqual(user.display_name,'Myraa Admin New')

    def test_pending_clerk_does_not_receive_old_admin_cookie(self):
        import uuid
        with self.Session() as db:
            db.add(workspace_models.WorkspaceUser(email='admin@example.com',login_id='admin',display_name='Admin',
                password_hash='unused',verified=True,approved=True,active=True,role='admin',token_version=uuid.uuid4().hex))
            db.commit()
        self.assertTrue(self.exchange().json()['approved'])
        self.assertIn(clerk_auth.COOKIE,self.client.cookies)
        self.identity.stop();self.identity=patch.object(clerk_auth,'validate_clerk_token',return_value='user_OTH123');self.identity.start()
        self.verified.stop();self.verified=patch.object(clerk_auth,'clerk_verified_identity',return_value=('other@example.com','New Seller'));self.verified.start()
        result=self.exchange()
        self.assertFalse(result.json()['approved'])
        self.assertNotIn(clerk_auth.COOKIE,self.client.cookies)
        self.assertNotIn(clerk_auth.BOUND_COOKIE,self.client.cookies)

    def test_untrusted_origin_rejected(self):
        r=self.client.post('/api/clerk/exchange',headers={
            'Authorization':'Bearer test.jwt.signature','Origin':'https://evil.example.org'})
        self.assertEqual(r.status_code,403)

if __name__=='__main__':unittest.main()

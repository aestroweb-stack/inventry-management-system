"""Offline MYRAA production unit tests; no cloud API credits needed.
Run: python -m unittest discover -s tests -p 'test_myraa_production.py' -v
"""
import asyncio
import io
import importlib.util
import os
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

# In an offline evaluation container only, a minimal MCP shim allows Claude
# service tests without downloading dependencies. Normal installs use real MCP.
fake_mcp = None
if importlib.util.find_spec('mcp') is None:
    fake_mcp = types.ModuleType('inventory_mcp')
    for tool in ('search_inventory', 'get_product', 'get_inventory_summary', 'get_inventory_alerts',
                 'get_reorder_shortages', 'get_stock_history', 'get_daily_dispatch_summary',
                 'get_dispatch_analytics', 'list_pending_order_batches', 'get_order_batch'):
        setattr(fake_mcp, tool, lambda **kwargs: {'status': 'ok', 'items': []})
    fake_mcp.prepare_inventory_action = lambda action, payload: {'status':'awaiting_user_confirmation', 'action':action}
    fake_mcp.confirm_inventory_action = lambda token: {'status':'completed'}
    sys.modules['inventory_mcp'] = fake_mcp

from services import cloud_assistant_service as assistant, app_auth, claude_photo_service
from database import Base
import models
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from PIL import Image


class CloudProductionTests(unittest.TestCase):
    def test_gemini_is_default_and_key_is_server_only(self):
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'test-secret'}, clear=True):
            self.assertEqual(assistant.provider_name(), 'gemini')
            self.assertEqual(assistant.model_name(), 'gemini-3.8-flash')
            self.assertTrue(assistant.is_configured())
            self.assertTrue(all('parameters' in t for t in assistant._TOOLS))

    def test_gemini_responds_without_local_companion(self):
        async def run():
            message = {"role": "model", "parts": [{"text": "Aaj 8 units stock mein hain."}]}
            with patch.dict(os.environ, {"MYRAA_CLOUD_PROVIDER": "gemini", "GEMINI_API_KEY": "test-key"}):
                with patch.object(assistant, "_generate", AsyncMock(return_value=message)) as generate:
                    result = await assistant.chat([{"role": "user", "content": "Stock batao"}])
                    self.assertEqual(result["provider"], "Gemini")
                    self.assertIn("8 units", result["reply"])
                    self.assertIsNone(result["pending_action"])
                    generate.assert_awaited_once()
        asyncio.run(run())

    def test_gemini_write_prepares_never_commits_and_preserves_tool_call_id(self):
        async def run():
            call = {"role": "model", "parts": [{"functionCall": {"name": "prepare_inventory_action", "id": "call-123",
                    "args": {"action": "stock_change", "payload": {"variant_code": "KUR-IV-001_M", "operation": "add", "quantity": 5}}},
                    "thoughtSignature": "opaque-signed-token"}]}
            reply = {"role": "model", "parts": [{"text": "Review and confirm the change."}]}
            contents_seen = []
            async def generate(contents, key):
                contents_seen.append(list(contents))
                return call if len(contents_seen) == 1 else reply
            preview = {"status": "awaiting_user_confirmation", "action": "stock_change",
                       "details": {"product": "Ivory Kurta", "size": "M", "quantity": 5},
                       "confirmation_token": "signed.preview.token"}
            with patch.dict(os.environ, {"MYRAA_CLOUD_PROVIDER": "gemini", "GEMINI_API_KEY": "test-key"}):
                with patch.object(assistant, "_generate", side_effect=generate):
                    with patch.object(assistant, "_execute_tool", return_value=preview) as prepare:
                        result = await assistant.chat([{"role": "user", "content": "Ivory Kurta size M me 5 add karo"}])
                        self.assertEqual(result["pending_action"]["confirmation_token"], "signed.preview.token")
                        self.assertEqual(result["provider"], "Gemini")
                        self.assertEqual(prepare.call_count, 1)
                        self.assertIs(contents_seen[1][1], call)
                        self.assertEqual(contents_seen[1][2]["parts"][0]["functionResponse"]["id"], "call-123")
                        self.assertNotIn("signed.preview.token", str(contents_seen[1][2]))
        asyncio.run(run())

    def test_gemini_vision_returns_suggestions_without_writes(self):
        class Response:
            status_code = 200
            def json(self):
                return {"candidates": [{"content": {"parts": [{"text":
                    '{"description":"White kurta","keywords":["white","kurta"]}'}]}}]}
        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def post(self, url, **kwargs):
                self_outer.assertIn("gemini-3.8-flash", url)
                self_outer.assertEqual(kwargs["headers"]["x-goog-api-key"], "test-key")
                self_outer.assertIn("inlineData", kwargs["json"]["contents"][0]["parts"][0])
                self_outer.assertNotIn("messages", kwargs["json"])
                return Response()
        self_outer = self
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            product = models.Product(sku="KUR-IV-001", name="White Ivory Kurta", category="Kurta")
            db.add(product); db.flush()
            db.add(models.Variant(product_id=product.id, size="M", variant_code="KUR-IV-001_M",
                                  current_stock=27, last_counted_at=datetime.now(timezone.utc)))
            db.commit()
            picture = Image.new("RGB", (40, 60), "white")
            output = io.BytesIO(); picture.save(output, format="PNG")
            with patch.dict(os.environ, {"MYRAA_CLOUD_PROVIDER": "gemini", "GEMINI_API_KEY": "test-key"}):
                with patch("httpx.AsyncClient", return_value=Client()):
                    response = asyncio.run(claude_photo_service.identify_photo(output.getvalue(), db))
            self.assertEqual(response["status"], "suggestions")
            self.assertEqual(response["matches"][0]["sku"], "KUR-IV-001")
            self.assertEqual(db.query(models.Variant).first().current_stock, 27)
        finally:
            db.close(); engine.dispose()

    def test_claude_responds_without_local_process(self):
        async def run():
            response = {'content': [{'type': 'text', 'text': 'Aaj 8 units stock mein hain.'}]}
            with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key', 'MYRAA_CLOUD_PROVIDER': 'claude'}):
                with patch.object(assistant, '_claude_generate', AsyncMock(return_value=response)) as generator:
                    result = await assistant.chat([{'role': 'user', 'content': 'Stock batao'}])
                    self.assertIn('8 units', result['reply'])
                    self.assertEqual(result['provider'], 'Claude')
                    self.assertIsNone(result['pending_action'])
                    self.assertEqual(generator.await_count, 1)
        asyncio.run(run())

    def test_write_only_prepared_never_committed(self):
        async def run():
            tool_use = {'content': [{'type': 'tool_use', 'id': 'toolu_1',
                       'name': 'prepare_inventory_action', 'input': {'action': 'stock_change',
                       'payload': {'variant_code': 'KUR-IV-001_M', 'operation': 'add', 'quantity': 5}}}]}
            response = {'content': [{'type': 'text', 'text': 'Please review and confirm.'}]}
            with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key', 'MYRAA_CLOUD_PROVIDER': 'claude'}):
                with patch.object(assistant, '_claude_generate', AsyncMock(side_effect=[tool_use, response])):
                    with patch.object(assistant, '_execute_tool', return_value={
                        'status': 'awaiting_user_confirmation', 'action': 'stock_change',
                        'details': {'product': 'Ivory Kurta', 'size': 'M', 'quantity': 5},
                        'confirmation_token': 'mock.signed.token',
                    }) as prepare:
                        result = await assistant.chat([{'role': 'user', 'content': 'Ivory Kurta size M mein 5 pieces add karo'}])
                        self.assertEqual(result['pending_action']['action'], 'stock_change')
                        self.assertEqual(result['pending_action']['confirmation_token'], 'mock.signed.token')
                        prepare.assert_called_once()
        asyncio.run(run())

    def test_auth_cookie_signed_and_rejects_tamper(self):
        with patch.dict(os.environ, {'APP_PASSWORD': 'strong-private-password', 'APP_SESSION_SECRET': 'a-long-secret-for-cookie-validation'}):
            token = app_auth.create_session()
            self.assertTrue(app_auth.verify_session(token))
            self.assertFalse(app_auth.verify_session(token + 'bad'))
            self.assertTrue(app_auth.verify_password('strong-private-password'))
            self.assertFalse(app_auth.verify_password('wrong'))

    def test_photo_suggests_but_does_not_write(self):
        class FakeResponse:
            status_code = 200
            def json(self):
                return {'content': [{'type': 'text', 'text': '{"description":"White kurta garment","keywords":["kurta","white"]}'}]}
        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def post(self, *args, **kwargs):
                self_outer.assertEqual(kwargs['json']['messages'][0]['content'][0]['type'], 'image')
                return FakeResponse()
        self_outer = self
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        try:
            product = models.Product(sku='KUR-IV-001', name='White Ivory Kurta', category='Kurta')
            session.add(product)
            session.flush()
            session.add(models.Variant(product_id=product.id, size='M', variant_code='KUR-IV-001_M', current_stock=27,
                                       last_counted_at=datetime.now(timezone.utc)))
            session.commit()
            picture = Image.new('RGB', (60, 80), 'white')
            out = io.BytesIO(); picture.save(out, format='PNG')
            with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'server-secret', 'MYRAA_CLOUD_PROVIDER': 'claude'}):
                with patch('httpx.AsyncClient', return_value=FakeClient()):
                    result = asyncio.run(claude_photo_service.identify_photo(out.getvalue(), session))
            self.assertEqual(result['status'], 'suggestions')
            self.assertEqual(result['matches'][0]['sku'], 'KUR-IV-001')
            self.assertEqual(result['matches'][0]['total_stock'], 27)
            self.assertIn('Verify', result['notice'])
            self.assertEqual(session.query(models.Variant).first().current_stock, 27)
        finally:
            session.close(); engine.dispose()


if __name__ == '__main__':
    unittest.main()

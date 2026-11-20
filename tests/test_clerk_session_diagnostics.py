"""Offline tests: rejected Clerk tokens remain rejected; reason is sanitized."""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from fastapi import HTTPException
from starlette.requests import Request
import clerk_auth


class FakeOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeState:
    def __init__(self, signed_in=False, reason=None, payload=None):
        self.is_signed_in = signed_in
        self.reason = reason
        self.payload = payload


class FakeSdk:
    def __init__(self, state):
        self.state = state

    def authenticate_request(self, request, options):
        assert options.kwargs["authorized_parties"] == ["https://inventory.example.com"]
        return self.state


def make_request(authorized=True):
    headers = [(b"authorization", b"Bearer FAKE_TEST_TOKEN")] if authorized else []
    return Request({"type": "http", "method": "POST", "path": "/api/clerk/exchange", "headers": headers})


class ClerkSessionReasonTests(unittest.TestCase):
    def invoke(self, state):
        fake = types.ModuleType("clerk_backend_api.security.types")
        fake.AuthenticateRequestOptions = FakeOptions
        with patch.dict(sys.modules, {"clerk_backend_api.security.types": fake}), \
             patch.dict("os.environ", {"CLERK_AUTHORIZED_PARTIES": "https://inventory.example.com"}), \
             patch.object(clerk_auth, "_clerk_sdk", return_value=FakeSdk(state)):
            return clerk_auth.validate_clerk_token(make_request())

    def test_invalid_azp_rejected_and_reason_visible(self):
        with self.assertRaises(HTTPException) as c:
            self.invoke(FakeState(reason=types.SimpleNamespace(name="TOKEN_INVALID_AUTHORIZED_PARTIES")))
        self.assertEqual(c.exception.status_code, 401)
        self.assertEqual(c.exception.detail, "Clerk session rejected: TOKEN_INVALID_AUTHORIZED_PARTIES")

    def test_malformed_reason_not_reflected(self):
        with self.assertRaises(HTTPException) as c:
            self.invoke(FakeState(reason=types.SimpleNamespace(name="ERROR <secret>\nnot-safe")))
        self.assertEqual(c.exception.detail, "Clerk session rejected: UNKNOWN")

    def test_valid_claim_accepted(self):
        value = self.invoke(FakeState(signed_in=True, payload={"sub": "user_ABC123"}))
        self.assertEqual(value, "user_ABC123")

    def test_missing_authorization_still_rejected(self):
        with self.assertRaises(HTTPException) as c:
            clerk_auth.validate_clerk_token(make_request(authorized=False))
        self.assertEqual(c.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()

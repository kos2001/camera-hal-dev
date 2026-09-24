"""Authentication and tenant isolation checks."""
from __future__ import annotations

import tempfile
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.app import RunStore, _execution_slot, app as main_app
from backend.auth import AuthConfig, IdentityMiddleware, TokenVerifier
from camhal_sim.runner import run_fault


class AuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.config = AuthConfig("oidc", "https://issuer.example/", "camhal-api", "https://issuer.example/jwks")
        cls.verifier = TokenVerifier(cls.config)
        cls.verifier.jwks = SimpleNamespace(get_signing_key_from_jwt=lambda _token:
                                            SimpleNamespace(key=cls.private_key.public_key()))

    @classmethod
    def token(cls, **overrides):
        claims = {"sub": "user-a", "iss": cls.config.issuer, "aud": cls.config.audience,
                  "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}
        for key, value in overrides.items():
            if value is None:
                claims.pop(key, None)
            else:
                claims[key] = value
        return jwt.encode(claims, cls.private_key, algorithm="RS256")

    def test_oidc_signature_claims_and_expiry(self):
        self.assertEqual(self.verifier.verify(self.token()), "user-a")
        for claims in ({"aud": "other"}, {"iss": "https://other.example/"},
                       {"exp": datetime.now(timezone.utc) - timedelta(seconds=1)},
                       {"exp": None}, {"sub": ""}):
            with self.subTest(claims=claims), self.assertRaises(jwt.PyJWTError):
                self.verifier.verify(self.token(**claims))
        forged = jwt.encode({"sub": "user-a", "iss": self.config.issuer,
                             "aud": self.config.audience,
                             "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
                            rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm="RS256")
        with self.assertRaises(jwt.PyJWTError):
            self.verifier.verify(forged)
        symmetric = jwt.encode({"sub": "user-a", "iss": self.config.issuer,
                                "aud": self.config.audience,
                                "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
                               "a" * 32, algorithm="HS256")
        with self.assertRaises(jwt.PyJWTError):
            self.verifier.verify(symmetric)

    def test_jwks_key_selection_and_signature(self):
        verifier = TokenVerifier(self.config)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private_key.public_key()))
        jwk["kid"] = "test-key"
        signed = jwt.encode({"sub": "user-a", "iss": self.config.issuer,
                             "aud": self.config.audience,
                             "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
                            self.private_key, algorithm="RS256", headers={"kid": "test-key"})
        with patch.object(verifier.jwks, "fetch_data", return_value={"keys": [jwk]}):
            self.assertEqual(verifier.verify(signed), "user-a")

    def test_middleware_protects_every_route_except_health(self):
        app = FastAPI()
        app.add_middleware(IdentityMiddleware, config=self.config, verifier=self.verifier)

        @app.get("/api/health")
        def health():
            return {"ok": True}

        @app.get("/api/ready")
        def ready():
            return {"ok": True}

        @app.get("/")
        def home(request: Request):
            return {"owner": request.state.owner}

        client = TestClient(app)
        self.assertEqual(client.get("/api/health").status_code, 200)
        self.assertEqual(client.get("/api/ready").status_code, 200)
        self.assertEqual(client.get("/").status_code, 401)
        self.assertEqual(client.get("/", headers={"Authorization": "Bearer invalid"}).status_code, 401)
        response = client.get("/", headers={"Authorization": f"Bearer {self.token()}"})
        self.assertEqual(response.json(), {"owner": "user-a"})

    def test_production_fails_closed_without_oidc(self):
        with patch.dict("os.environ", {"CAMHAL_ENV": "production", "CAMHAL_AUTH_MODE": "local"}):
            with self.assertRaises(ValueError):
                AuthConfig.from_env()

    def test_run_store_isolates_users(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = RunStore(3, Path(tmp) / "runs.sqlite3")
            rid = db.put(run_fault("F04", seed=7), owner="user-a")
            self.assertEqual(db.get(rid, "user-a")["payload"]["id"], rid)
            self.assertEqual(db.get_field(rid, "payload", "user-a")["id"], rid)
            self.assertEqual(db.items("user-b"), [])
            self.assertEqual(db.count("user-b"), 0)
            with self.assertRaises(Exception) as raised:
                db.get_field(rid, "report", "user-b")
            self.assertEqual(raised.exception.status_code, 404)
            db.close()

    def test_real_api_routes_isolate_two_authenticated_users(self):
        service = FastAPI()
        service.include_router(main_app.router)
        service.add_middleware(IdentityMiddleware, config=self.config, verifier=self.verifier)
        client = TestClient(service)
        user_a = {"Authorization": f"Bearer {self.token(sub='user-a')}"}
        user_b = {"Authorization": f"Bearer {self.token(sub='user-b')}"}
        with tempfile.TemporaryDirectory() as tmp:
            db = RunStore(10, Path(tmp) / "runs.sqlite3")
            with patch("backend.app.store", db):
                response = client.post("/api/faults/F04/run", json={"seed": 7}, headers=user_a)
                self.assertEqual(response.status_code, 200)
                rid = response.json()["id"]
                self.assertEqual(client.get("/api/runs", headers=user_a).json()[0]["id"], rid)
                self.assertEqual(client.get("/api/runs", headers=user_b).json(), [])
                for suffix in ("", "/events", "/issue", "/report.html"):
                    self.assertEqual(client.get(f"/api/runs/{rid}{suffix}", headers=user_b).status_code, 404)
                self.assertEqual(client.get(f"/api/runs/{rid}/report.html", headers=user_a).status_code, 200)
                self.assertEqual(client.delete(f"/api/runs/{rid}", headers=user_b).status_code, 404)
                self.assertEqual(client.get(f"/api/runs/{rid}", headers=user_a).status_code, 200)
                deleted = client.delete(f"/api/runs/{rid}", headers=user_a)
                self.assertEqual(deleted.status_code, 204)
                self.assertEqual(deleted.content, b"")
                self.assertEqual(client.get(f"/api/runs/{rid}", headers=user_a).status_code, 404)
                self.assertEqual(client.get("/api/runs", headers=user_a).json(), [])
                second_a = client.post("/api/faults/F04/run", json={"seed": 8}, headers=user_a).json()["id"]
                second_b = client.post("/api/faults/F04/run", json={"seed": 9}, headers=user_b).json()["id"]
                self.assertEqual(client.delete("/api/runs", headers=user_a).json(), {"deleted": 1})
                self.assertEqual(client.delete("/api/runs", headers=user_a).json(), {"deleted": 0})
                self.assertEqual(client.get(f"/api/runs/{second_a}", headers=user_a).status_code, 404)
                self.assertEqual(client.get(f"/api/runs/{second_b}", headers=user_b).status_code, 200)
            db.close()

    def test_one_user_cannot_occupy_every_execution_slot(self):
        service = FastAPI()
        service.include_router(main_app.router)
        service.add_middleware(IdentityMiddleware, config=self.config, verifier=self.verifier)
        client = TestClient(service)
        user_a = {"Authorization": f"Bearer {self.token(sub='user-a')}"}
        user_b = {"Authorization": f"Bearer {self.token(sub='user-b')}"}
        with tempfile.TemporaryDirectory() as tmp:
            db = RunStore(10, Path(tmp) / "runs.sqlite3")
            with patch("backend.app.store", db):
                with _execution_slot("user-a"), _execution_slot("user-a"):
                    denied = client.post("/api/faults/F04/run", json={"seed": 7}, headers=user_a)
                    self.assertEqual(denied.status_code, 503)
                    self.assertEqual(denied.headers["Retry-After"], "1")
                    allowed = client.post("/api/faults/F04/run", json={"seed": 7}, headers=user_b)
                    self.assertEqual(allowed.status_code, 200)
                self.assertEqual(client.post("/api/faults/F04/run", json={"seed": 7},
                                             headers=user_a).status_code, 200)
            db.close()

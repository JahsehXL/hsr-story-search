import time
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt
import pytest

from backend.app import create_app
from backend.cloudflare_access import AccessDenied, AccessVerifier


@pytest.fixture
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key, **changes):
    claims = {"iss": "https://test-team.cloudflareaccess.com", "aud": ["writing-app"],
              "email": "owner@example.com", "sub": "owner", "iat": int(time.time()) - 1,
              "exp": int(time.time()) + 300, **changes}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})


def test_access_verifies_signature_expiry_issuer_audience_and_email(signing_key):
    verifier = AccessVerifier("https://test-team.cloudflareaccess.com", "writing-app", ["owner@example.com"])
    with patch.object(verifier.keys, "get_signing_key_from_jwt", return_value=SimpleNamespace(key=signing_key.public_key())):
        valid = token(signing_key)
        assert verifier.verify({"Cf-Access-Jwt-Assertion": valid})["email"] == "owner@example.com"
        assert verifier.verify({"Cookie": "CF_Authorization=" + valid})["subject"] == "owner"
        failures = [
            {"Cf-Access-Authenticated-User-Email": "owner@example.com"},
            {"Cf-Access-Jwt-Assertion": token(signing_key, exp=1)},
            {"Cf-Access-Jwt-Assertion": token(signing_key, iss="https://attacker.cloudflareaccess.com")},
            {"Cf-Access-Jwt-Assertion": token(signing_key, aud=["other-app"])},
            {"Cf-Access-Jwt-Assertion": token(signing_key, email="other@example.com")},
            {"Cf-Access-Jwt-Assertion": token(rsa.generate_private_key(public_exponent=65537, key_size=2048))},
            {"Cf-Access-Jwt-Assertion": jwt.encode({"email": "owner@example.com"}, "attacker" * 8, algorithm="HS256", headers={"kid": "test-key"})},
        ]
        for headers in failures:
            with pytest.raises(AccessDenied):
                verifier.verify(headers)


def test_cloudflare_server_guards_admin_assets_and_disables_password_bypass(tmp_path, monkeypatch, signing_key):
    for key, value in {"HSR_ADMIN_AUTH": "cloudflare", "HSR_CF_TEAM_DOMAIN": "https://test-team.cloudflareaccess.com",
                       "HSR_CF_AUDIENCE": "writing-app", "HSR_CF_ALLOWED_EMAILS": "owner@example.com",
                       "HSR_ACCESS_CODE": "", "HSR_ADMIN_CODE": "legacy-admin-password"}.items():
        monkeypatch.setenv(key, value)
    static = tmp_path / "bundle" / "frontend" / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("public search", encoding="utf-8")
    (static / "assets" / "admin-test.js").write_text("private administration", encoding="utf-8")
    monkeypatch.setattr("sys._MEIPASS", tmp_path / "bundle", raising=False)
    with patch("backend.cloudflare_access.jwt.PyJWKClient") as keys:
        keys.return_value.get_signing_key_from_jwt.return_value = SimpleNamespace(key=signing_key.public_key())
        app = create_app(tmp_path / "data", mode="server")
        app.state.runtime.jobs.append({"id": "a" * 32, "kind": "download_source", "status": "failed"})
        with TestClient(app) as client:
            assert client.get("/").status_code == 200
            for path in ("/api/v1/admin/settings", "/api/v1/admin/overview", "/assets/admin-test.js",
                         "/assets/%2e/admin-test.js", "/assets/foo/%2e%2e/admin-test.js", "/assets%5cadmin-test.js", "/ASSETS/ADMIN-TEST.JS"):
                response = client.get(path, headers={"Cf-Access-Authenticated-User-Email": "owner@example.com"})
                assert response.status_code == 403
                assert response.headers["cache-control"] == "no-store"
                assert "private administration" not in response.text
            assert client.post("/api/v1/auth/login", json={"admin": True, "code": "legacy-admin-password"}).status_code == 403
            headers = {"Cf-Access-Jwt-Assertion": token(signing_key)}
            assert client.get("/api/v1/admin/overview", headers=headers).status_code == 200
            assert client.get("/assets/admin-test.js", headers=headers).text == "private administration"
            assert client.get("/api/v1/status", headers=headers).json()["admin"] is False
            assert client.post("/api/v1/admin/jobs", json={"kind": "download_source"}, headers=headers).status_code == 400
            assert client.post("/api/v1/admin/jobs/" + "a" * 32 + "/resume", headers=headers).status_code == 400
            assert app.state.runtime.active_path()[0] is None


def test_incomplete_cloudflare_configuration_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HSR_ADMIN_AUTH", "cloudflare")
    monkeypatch.delenv("HSR_CF_TEAM_DOMAIN", raising=False)
    with pytest.raises(ValueError, match="HTTPS"):
        create_app(tmp_path / "data", mode="server")

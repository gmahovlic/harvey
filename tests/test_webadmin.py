"""Browser-only administration: branding, config form, AI token, extra env,
mailbox OAuth URLs, and the Cloudflare Access guard."""

import base64
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harvey import dashboard, webadmin
from harvey.paths import PROJECT_ROOT


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A dashboard whose files all live under tmp_path."""
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "harvey.yaml"
    shutil.copy(PROJECT_ROOT / "harvey.yaml", cfg)
    local = tmp_path / "harvey.local.yaml"

    monkeypatch.setattr(webadmin, "DATA_DIR", data)
    monkeypatch.setattr(webadmin, "BRANDING_FILE", data / "branding.json")
    monkeypatch.setattr(webadmin, "BRANDING_DIR", data / "branding")
    monkeypatch.setattr(webadmin, "LOCAL_CONFIG", local)
    monkeypatch.setattr(webadmin, "TRAIN_LOG", data / "train.log")
    monkeypatch.setattr(dashboard, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dashboard, "DB_PATH", data / "harvey.db")
    monkeypatch.setattr(dashboard, "_resolve_config_file",
                        lambda: local if local.exists() else cfg)
    for key in ("CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD", "CF_ACCESS_ALLOWED_EMAILS",
                "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    dashboard._quota_clients.clear()
    return TestClient(dashboard.app)


# ── Branding ──


def test_branding_defaults_and_roundtrip(client):
    r = client.get("/api/branding")
    assert r.status_code == 200
    assert r.json()["app_name"] == "Harvey"
    assert r.json()["logo_url"] == ""

    r = client.post("/api/branding", json={"app_name": "Vendas", "tagline": "oi",
                                           "theme_default": "dark", "accent": "#2c5f7c"})
    assert r.status_code == 200 and r.json()["success"]
    b = client.get("/api/branding").json()
    assert b["app_name"] == "Vendas"
    assert b["theme_default"] == "dark"
    assert b["accent"] == "#2C5F7C"

    # The favicon falls back to the first letter of the name.
    fav = client.get("/branding/favicon")
    assert fav.status_code == 200 and b"V" in fav.content


def test_branding_rejects_bad_values(client):
    assert client.post("/api/branding", json={"theme_default": "neon"}).status_code == 400
    assert client.post("/api/branding", json={"accent": "blue"}).status_code == 400
    assert client.post("/api/branding", json=[1, 2]).status_code == 400


def test_logo_upload_serve_delete(client):
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
    r = client.post("/api/branding/logo", json={"mime": "image/png", "data": png})
    assert r.status_code == 200 and r.json()["success"]
    assert client.get("/api/branding").json()["logo"] == "logo.png"

    served = client.get("/branding/logo")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/png")
    assert served.content.startswith(b"\x89PNG")

    # SVG with a script is refused; wrong type is refused.
    svg = base64.b64encode(b"<svg><script>alert(1)</script></svg>").decode()
    assert client.post("/api/branding/logo", json={"mime": "image/svg+xml", "data": svg}).status_code == 400
    assert client.post("/api/branding/logo", json={"mime": "text/html", "data": png}).status_code == 400

    assert client.delete("/api/branding/logo").json()["success"]
    assert client.get("/branding/logo").status_code == 404


# ── Config form ──


def test_config_get_is_effective_and_flags_template(client):
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert body["is_template"] is True
    assert body["effective"]["usage"]["max_daily_claude_percent"] == 80
    assert body["errors"] == []
    assert "America/Sao_Paulo" in body["timezones"]


def test_config_put_validates_and_writes_local_yaml(client, tmp_path):
    bad = client.put("/api/config", json={"usage": {"max_daily_claude_percent": 150}})
    assert bad.status_code == 400
    assert bad.json()["errors"][0]["field"] == "usage.max_daily_claude_percent"
    assert not (tmp_path / "harvey.local.yaml").exists()

    ok = client.put("/api/config", json={
        "usage": {"max_daily_claude_percent": 55, "quiet_hours": {"timezone": "America/Sao_Paulo"}},
        "persona": {"company": "Exemplo"},
    })
    assert ok.status_code == 200, ok.text
    local = tmp_path / "harvey.local.yaml"
    assert local.exists()
    import yaml
    written = yaml.safe_load(local.read_text())
    # Deep merge: the rest of the template survives next to the edits.
    assert written["usage"]["max_daily_claude_percent"] == 55
    assert written["usage"]["quiet_hours"]["timezone"] == "America/Sao_Paulo"
    assert written["usage"]["quiet_hours"]["start"] == "22:00"
    assert written["persona"]["company"] == "Exemplo"
    assert written["product"]["name"] == "Your Product"

    # The next GET reads the local file.
    again = client.get("/api/config").json()
    assert again["path"].endswith("harvey.local.yaml")
    assert again["effective"]["usage"]["max_daily_claude_percent"] == 55


def test_email_provider_switch_writes_yaml(client, tmp_path):
    assert client.post("/api/email/provider", json={"provider": "nope"}).status_code == 400
    r = client.post("/api/email/provider", json={"provider": "smtp"})
    assert r.json() == {"success": True, "provider": "smtp"}
    assert client.get("/api/email/status").json()["provider"] == "smtp"


# ── AI token + extra env ──


def test_brain_token_lifecycle(client, tmp_path, monkeypatch):
    monkeypatch.setattr("harvey.usage.claude_config_dir", lambda: tmp_path / "nocreds")
    assert client.get("/api/brain/status").json()["token_source"] == "none"

    assert client.post("/api/brain/token", json={"token": "short"}).status_code == 400
    assert client.post("/api/brain/token", json={"token": "sk-ant-oat01-" + "x" * 40}).json()["success"]
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-" in (tmp_path / ".env").read_text()
    assert client.get("/api/brain/status").json()["token_source"] == "token"
    env = client.get("/api/admin/env").json()
    assert env["CLAUDE_CODE_OAUTH_TOKEN_set"] is True
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env  # secrets never echo

    assert client.delete("/api/brain/token").json()["success"]
    assert client.get("/api/brain/status").json()["token_source"] == "none"


def test_admin_env_roundtrip_strips_newlines(client, tmp_path):
    r = client.post("/api/admin/env", json={"M365_CLIENT_ID": "abc\nEVIL=1", "SERPER_API_KEY": "k1"})
    assert r.json()["success"]
    text = (tmp_path / ".env").read_text()
    assert "M365_CLIENT_ID=abc EVIL=1" in text and "\nEVIL=1" not in text
    env = client.get("/api/admin/env").json()
    assert env["M365_CLIENT_ID"] == "abc EVIL=1"
    assert env["SERPER_API_KEY_set"] is True


# ── Mailbox OAuth from the browser ──


def test_gmail_auth_url_needs_credentials_then_uses_public_host(client):
    r = client.get("/api/gmail/auth/url", headers={"x-forwarded-host": "harvey.example.com",
                                                   "x-forwarded-proto": "https"})
    assert r.json()["success"] is False
    assert r.json()["redirect_uri"] == "https://harvey.example.com/api/gmail/callback"

    client.post("/api/settings/env", json={"GMAIL_CLIENT_ID": "cid", "GMAIL_CLIENT_SECRET": "sec"})
    r = client.get("/api/gmail/auth/url", headers={"x-forwarded-host": "harvey.example.com",
                                                   "x-forwarded-proto": "https"})
    body = r.json()
    assert body["success"] and body["url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "redirect_uri=https%3A%2F%2Fharvey.example.com%2Fapi%2Fgmail%2Fcallback" in body["url"]
    assert "access_type=offline" in body["url"]


def test_gmail_callback_rejects_unknown_state(client):
    r = client.get("/api/gmail/callback?state=bogus&code=abc", follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"] and r.headers["location"].endswith("#email")


# ── Cloudflare Access guard ──


@pytest.fixture
def access(monkeypatch):
    """Guard on, with a throwaway RSA key standing in for Cloudflare's."""
    jwt = pytest.importorskip("jwt")
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "test-kid"

    async def fake_jwks(team_domain):
        return {"keys": [jwk]}

    monkeypatch.setattr(webadmin, "_access_jwks", fake_jwks)
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "seutime")
    monkeypatch.setenv("CF_ACCESS_AUD", "aud-123")
    monkeypatch.setenv("CF_ACCESS_ALLOWED_EMAILS", "ana@example.com, Bruno@example.com")

    def token_for(email, aud="aud-123", iss="https://seutime.cloudflareaccess.com"):
        return jwt.encode({"email": email, "aud": aud, "iss": iss, "exp": 4102444800},
                          key, algorithm="RS256", headers={"kid": "test-kid"})
    return token_for


def test_guard_off_by_default(client):
    assert client.get("/api/branding").status_code == 200


def test_guard_blocks_without_token(client, access):
    r = client.get("/api/branding")
    assert r.status_code == 403
    assert "Cloudflare Access" in r.json()["error"]


def test_guard_allows_listed_email_only(client, access):
    ok = client.get("/api/branding", headers={"cf-access-jwt-assertion": access("bruno@example.com")})
    assert ok.status_code == 200

    stranger = client.get("/api/branding", headers={"cf-access-jwt-assertion": access("x@example.com")})
    assert stranger.status_code == 403
    assert "não está na lista" in stranger.json()["error"]

    wrong_aud = client.get("/api/branding", headers={"cf-access-jwt-assertion": access("ana@example.com", aud="other")})
    assert wrong_aud.status_code == 403

    # The cookie Cloudflare sets works as well as the header.
    cookie = client.get("/api/branding", cookies={"CF_Authorization": access("ana@example.com")})
    assert cookie.status_code == 200


def test_me_reports_access_state(client, access):
    r = client.get("/api/me", headers={"cf-access-jwt-assertion": access("ana@example.com"),
                                       "cf-access-authenticated-user-email": "ana@example.com"})
    body = r.json()
    assert body["email"] == "ana@example.com"
    assert body["access_enforced"] is True
    assert body["allowed_emails"] == ["ana@example.com", "bruno@example.com"]

"""Harvey web admin — everything a person needs to run Harvey from the
browser alone, with no terminal: AI sign-in, mailbox sign-in, the whole
harvey.yaml as a form, training, branding, and the Cloudflare Access guard.

Mounted into the dashboard app by ``harvey/dashboard.py``. Kept in its own
module so the original dashboard stays recognisable and merges from
upstream stay easy.

Files this module owns (all under PROJECT_ROOT, all gitignored):

    data/branding.json        name, tagline, theme default, accent, logo
    data/branding/logo.*      the uploaded logo
    data/train.log            output of the last training run
    harvey.local.yaml         the config the form writes (wins over harvey.yaml)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import httpx
import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.webadmin")

router = APIRouter()

DATA_DIR = PROJECT_ROOT / "data"
BRANDING_FILE = DATA_DIR / "branding.json"
BRANDING_DIR = DATA_DIR / "branding"
LOCAL_CONFIG = PROJECT_ROOT / "harvey.local.yaml"
TEMPLATE_CONFIG = PROJECT_ROOT / "harvey.yaml"
TRAIN_LOG = DATA_DIR / "train.log"
TRAIN_PID = DATA_DIR / "train.pid"

# The extra .env keys the admin screens manage, on top of the ones the
# original Settings tab already handles. Secrets never echo back.
ADMIN_ENV_KEYS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "M365_CLIENT_ID", "M365_TENANT_ID",
    "SERPER_API_KEY",
    "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "DATAFORSEO_SANDBOX",
    "PRECLEARED_API_KEY",
    "HUNTER_MONTHLY_SEARCHES",
    "CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD", "CF_ACCESS_ALLOWED_EMAILS",
)
ADMIN_ENV_SECRETS = {
    "CLAUDE_CODE_OAUTH_TOKEN", "DATAFORSEO_PASSWORD", "PRECLEARED_API_KEY",
    "SERPER_API_KEY",
}

LOGO_MAX_BYTES = 2 * 1024 * 1024
LOGO_TYPES = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/svg+xml": ".svg",
    "image/webp": ".webp", "image/gif": ".gif",
}

DEFAULT_BRANDING = {
    "app_name": "Harvey",
    "tagline": "Always Be Closing",
    "theme_default": "auto",      # auto | light | dark
    "accent": "",                 # hex like #2C5F7C, "" = the design's ink
    "logo": "",                   # file name under data/branding/
    "language": "pt-BR",
}


# ── Small helpers shared with dashboard.py (imported lazily: the dashboard
#    imports this module, so a top-level import would be circular) ──


def _dash():
    from harvey import dashboard
    return dashboard


def _read_env() -> dict[str, str]:
    return _dash()._read_env_file()


def _write_env(updates: dict[str, str]):
    cleaned = {}
    for k, v in updates.items():
        cleaned[k] = str(v if v is not None else "").replace("\n", " ").replace("\r", " ").strip()
    _dash()._write_env_file(cleaned)
    # The dashboard process must see the new values too (quota reader,
    # provider tests), not only the next Harvey subprocess.
    for k, v in cleaned.items():
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)


def _public_base_url(request: Request) -> str:
    """Where the browser reached us. Behind cloudflared the app sees plain
    HTTP on localhost, so trust the forwarded headers Cloudflare sets."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost:5555"
    proto = request.headers.get("x-forwarded-proto")
    if not proto:
        proto = "http" if host.split(":")[0] in ("localhost", "127.0.0.1", "::1") else "https"
    return f"{proto}://{host}"


def _json_body(data) -> dict | None:
    return data if isinstance(data, dict) else None


# ── Who am I (Cloudflare Access) ──


@router.get("/api/me")
async def me(request: Request):
    env = _read_env()
    team, aud, allowed = _access_settings()
    email = request.headers.get("cf-access-authenticated-user-email", "")
    return {
        "email": email,
        "access_enforced": bool(team and aud),
        "allowed_emails": sorted(allowed),
        "access_header_present": bool(request.headers.get("cf-access-jwt-assertion")),
        # The team-domain logout revokes the session without going through the
        # app hostname (and the tunnel), which is the reliable one. Fall back to
        # the app-local path when the team domain isn't configured yet.
        "logout_url": (f"https://{team}/cdn-cgi/access/logout" if team else "/cdn-cgi/access/logout") if email else "",
    }


# ── Branding: name, tagline, theme default, accent colour, logo ──


def _load_branding() -> dict:
    data = dict(DEFAULT_BRANDING)
    try:
        if BRANDING_FILE.is_file():
            stored = json.loads(BRANDING_FILE.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                data.update({k: stored[k] for k in DEFAULT_BRANDING if k in stored})
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not read branding.json: %s", e)
    if data["logo"] and not (BRANDING_DIR / data["logo"]).is_file():
        data["logo"] = ""
    return data


def _save_branding(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BRANDING_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _valid_hex(value: str) -> bool:
    v = value.strip()
    if not v:
        return True
    if not v.startswith("#") or len(v) not in (4, 7):
        return False
    try:
        int(v[1:], 16)
        return True
    except ValueError:
        return False


@router.get("/api/branding")
async def get_branding():
    data = _load_branding()
    data["logo_url"] = f"/branding/logo?v={int((BRANDING_DIR / data['logo']).stat().st_mtime)}" if data["logo"] else ""
    return data


@router.post("/api/branding")
async def save_branding(request: Request):
    try:
        body = _json_body(await request.json())
    except Exception:
        body = None
    if body is None:
        return JSONResponse({"success": False, "message": "Corpo da requisição inválido."}, status_code=400)
    data = _load_branding()
    if "app_name" in body:
        name = str(body["app_name"] or "").strip()[:40]
        data["app_name"] = name or "Harvey"
    if "tagline" in body:
        data["tagline"] = str(body["tagline"] or "").strip()[:80]
    if "theme_default" in body:
        theme = str(body["theme_default"] or "auto").strip().lower()
        if theme not in ("auto", "light", "dark"):
            return JSONResponse({"success": False, "message": "Tema deve ser auto, light ou dark."}, status_code=400)
        data["theme_default"] = theme
    if "accent" in body:
        accent = str(body["accent"] or "").strip()
        if not _valid_hex(accent):
            return JSONResponse({"success": False, "message": "Cor de destaque deve ser um hex como #2C5F7C."}, status_code=400)
        data["accent"] = accent.upper()
    try:
        _save_branding(data)
    except OSError as e:
        return JSONResponse({"success": False, "message": f"Não consegui gravar: {e}"}, status_code=500)
    return {"success": True, "branding": data}


@router.post("/api/branding/logo")
async def upload_logo(request: Request):
    """Logo as base64 JSON: avoids a multipart dependency, and a logo is
    small. {"mime": "image/png", "data": "<base64>"}"""
    try:
        body = _json_body(await request.json())
    except Exception:
        body = None
    if body is None:
        return JSONResponse({"success": False, "message": "Corpo da requisição inválido."}, status_code=400)
    mime = str(body.get("mime", "")).strip().lower()
    if mime not in LOGO_TYPES:
        return JSONResponse({"success": False, "message": "Use PNG, JPG, SVG, WEBP ou GIF."}, status_code=400)
    try:
        raw = base64.b64decode(str(body.get("data", "")), validate=True)
    except (ValueError, TypeError):
        return JSONResponse({"success": False, "message": "Imagem inválida."}, status_code=400)
    if not raw or len(raw) > LOGO_MAX_BYTES:
        return JSONResponse({"success": False, "message": "A imagem precisa ter até 2 MB."}, status_code=400)
    if mime == "image/svg+xml" and b"<script" in raw.lower():
        return JSONResponse({"success": False, "message": "SVG com script não é permitido."}, status_code=400)

    data = _load_branding()
    BRANDING_DIR.mkdir(parents=True, exist_ok=True)
    # Remove any previous logo so a format change doesn't leave two files.
    for old in BRANDING_DIR.glob("logo.*"):
        try:
            old.unlink()
        except OSError:
            pass
    name = "logo" + LOGO_TYPES[mime]
    (BRANDING_DIR / name).write_bytes(raw)
    data["logo"] = name
    _save_branding(data)
    return {"success": True, "logo_url": f"/branding/logo?v={int(time.time())}"}


@router.delete("/api/branding/logo")
async def delete_logo():
    data = _load_branding()
    for old in BRANDING_DIR.glob("logo.*"):
        try:
            old.unlink()
        except OSError:
            pass
    data["logo"] = ""
    _save_branding(data)
    return {"success": True}


@router.get("/branding/logo")
async def serve_logo():
    data = _load_branding()
    if not data["logo"]:
        return Response(status_code=404)
    path = BRANDING_DIR / data["logo"]
    mime = next((m for m, ext in LOGO_TYPES.items() if path.suffix == ext), "application/octet-stream")
    headers = {"Cache-Control": "public, max-age=3600"}
    if mime == "image/svg+xml":
        headers["Content-Security-Policy"] = "script-src 'none'"
    return Response(path.read_bytes(), media_type=mime, headers=headers)


_DEFAULT_FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="5" fill="#1E1B17"/>'
    '<text x="16" y="22" text-anchor="middle" font-family="Georgia,serif" font-size="19" fill="#FBFAF8">{letter}</text>'
    '</svg>'
)


@router.get("/branding/favicon")
async def serve_favicon():
    data = _load_branding()
    if data["logo"]:
        return await serve_logo()
    letter = (data.get("app_name") or "H").strip()[:1].upper() or "H"
    return Response(_DEFAULT_FAVICON.format(letter=letter), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600"})


# ── Configuration (harvey.yaml as a form) ──


def _config_path_for_write() -> Path:
    return LOCAL_CONFIG


def _read_yaml(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def _active_config_path() -> Path:
    return _dash()._resolve_config_file()


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _deep_merge(base: dict, incoming: dict) -> dict:
    out = dict(base)
    for k, v in incoming.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _validation_errors(e) -> list[dict]:
    out = []
    for err in e.errors():
        out.append({
            "field": ".".join(str(p) for p in err["loc"]) or "(raiz)",
            "message": err["msg"],
        })
    return out


@router.get("/api/config")
async def get_config():
    """The effective configuration: what Harvey will actually run with,
    with every default filled in, plus where it came from."""
    from pydantic import ValidationError
    from harvey.config import HarveyConfig

    path = _active_config_path()
    raw = _read_yaml(path)
    effective = None
    errors = []
    try:
        effective = HarveyConfig(**raw).model_dump() if raw else None
    except ValidationError as e:
        errors = _validation_errors(e)
    except Exception as e:  # noqa: BLE001 — a broken file must not 500 the form
        errors = [{"field": "(arquivo)", "message": str(e)}]

    import pytz
    return {
        "path": _rel(path),
        "writes_to": _rel(_config_path_for_write()),
        "is_template": raw.get("persona", {}).get("company") in ("", "Your Company", None)
                       or raw.get("product", {}).get("name") in ("", "Your Product", None),
        "raw": raw,
        "effective": effective,
        "errors": errors,
        "timezones": list(pytz.common_timezones),
    }


@router.put("/api/config")
async def put_config(request: Request):
    """Validate the whole config with Harvey's own models, then write it to
    harvey.local.yaml (the file that wins). Unknown keys already in the file
    (geo_coordinates, anything upstream adds later) are kept."""
    from pydantic import ValidationError
    from harvey.config import HarveyConfig

    try:
        body = _json_body(await request.json())
    except Exception:
        body = None
    if body is None:
        return JSONResponse({"success": False, "message": "Corpo da requisição inválido."}, status_code=400)

    current = _read_yaml(_active_config_path())
    merged = _deep_merge(current, body)
    try:
        HarveyConfig(**merged)
    except ValidationError as e:
        return JSONResponse({"success": False, "message": "Alguns campos estão inválidos.",
                             "errors": _validation_errors(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"success": False, "message": str(e)}, status_code=400)

    target = _config_path_for_write()
    try:
        tmp = target.with_suffix(".yaml.tmp")
        tmp.write_text(
            "# Gerado pelo painel do Harvey. Este arquivo vence o harvey.yaml.\n"
            + yaml.safe_dump(merged, allow_unicode=True, sort_keys=False, width=100),
            encoding="utf-8",
        )
        tmp.replace(target)
    except OSError as e:
        return JSONResponse({"success": False, "message": f"Não consegui gravar {target.name}: {e}"}, status_code=500)
    return {"success": True, "path": target.name,
            "note": "Se o Harvey estiver rodando, pare e inicie de novo para aplicar."}


# ── Extra .env keys (the ones the original Settings tab doesn't cover) ──


@router.get("/api/admin/env")
async def get_admin_env():
    env = _read_env()
    out = {}
    for key in ADMIN_ENV_KEYS:
        value = (env.get(key) or os.getenv(key, "") or "").strip()
        if key in ADMIN_ENV_SECRETS:
            out[key + "_set"] = bool(value)
        else:
            out[key] = value
    return out


@router.post("/api/admin/env")
async def save_admin_env(request: Request):
    try:
        body = _json_body(await request.json())
    except Exception:
        body = None
    if body is None:
        return JSONResponse({"success": False, "message": "Corpo da requisição inválido."}, status_code=400)
    updates = {k: body[k] for k in ADMIN_ENV_KEYS if k in body and body[k] is not None}
    if not updates:
        return {"success": True}
    async with _dash()._env_lock:
        try:
            _write_env(updates)
        except OSError as e:
            return JSONResponse({"success": False, "message": f"Não consegui gravar o .env: {e}"}, status_code=500)
    return {"success": True}


# ── The AI (Claude via your subscription, or Codex) ──


def _token_source() -> str:
    if os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip() or _read_env().get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        return "token"
    from harvey.usage import claude_config_dir
    if (claude_config_dir() / ".credentials.json").is_file():
        return "login"
    return "none"


def _cli_version(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        return ""
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15)
        return (out.stdout or out.stderr).strip().splitlines()[0][:80] if (out.stdout or out.stderr) else "instalado"
    except (subprocess.TimeoutExpired, OSError):
        return "instalado"


@router.get("/api/brain/status")
async def brain_status():
    brain = _dash()._active_brain()
    provider = brain.provider if brain else "claude"
    model = brain.model if brain else ""
    status = {
        "provider": provider,
        "model": model,
        "reasoning_effort": brain.reasoning_effort if brain else "",
        "claude_cli": _cli_version("claude"),
        "codex_cli": _cli_version("codex"),
        "token_source": _token_source(),
        "quota": None,
        "quota_error": "",
        "paused_reason": "",
    }
    try:
        health = await _dash()._brain_status()
        status["paused_reason"] = (health or {}).get("reason", "") or ""
    except Exception:
        pass
    try:
        client = _dash()._quota_client_for(provider, model)
        if client is not None:
            status["quota"] = await client.get_utilization()
            if not status["quota"]:
                why = getattr(client, "last_error", "") or ""
                status["quota_error"] = "Não consegui ler a cota da assinatura" + (f": {why}" if why else ".")
    except Exception as e:  # noqa: BLE001
        status["quota_error"] = str(e)[:200]
    return status


@router.post("/api/brain/token")
async def save_brain_token(request: Request):
    """Store the long-lived token from `claude setup-token`. Validated only
    by shape; the Test button is the real check."""
    try:
        body = _json_body(await request.json())
    except Exception:
        body = None
    token = str((body or {}).get("token", "")).strip()
    if not token:
        return JSONResponse({"success": False, "message": "Cole o token primeiro."}, status_code=400)
    if len(token) < 20 or any(ch.isspace() for ch in token):
        return JSONResponse({"success": False, "message": "Isso não parece um token válido. Copie a linha inteira que o comando mostrou."}, status_code=400)
    async with _dash()._env_lock:
        _write_env({"CLAUDE_CODE_OAUTH_TOKEN": token})
    # Forget any cached quota failure so the gauge refreshes right away.
    _dash()._quota_clients.clear()
    return {"success": True}


@router.delete("/api/brain/token")
async def delete_brain_token():
    async with _dash()._env_lock:
        _write_env({"CLAUDE_CODE_OAUTH_TOKEN": ""})
    _dash()._quota_clients.clear()
    return {"success": True}


@router.post("/api/brain/test")
async def brain_test():
    """One tiny real call through the configured provider. Proves the
    sign-in works end to end, which no file check can."""
    from harvey.brain import Brain
    from harvey.state import StateManager

    # Make sure the token saved in .env is in this process's environment:
    # the CLI we spawn reads CLAUDE_CODE_OAUTH_TOKEN from there, and a
    # dashboard restarted after the save would otherwise run it logged out.
    from dotenv import load_dotenv
    env_file = _dash().ENV_FILE
    if env_file.is_file():
        load_dotenv(str(env_file), override=True)

    brain_cfg = _dash()._active_brain()
    provider = brain_cfg.provider if brain_cfg else "claude"
    binary = "codex" if provider == "codex" else "claude"
    if not shutil.which(binary):
        return {"success": False,
                "message": f"O programa '{binary}' não está instalado (ou não está no PATH do painel). "
                           f"Na VM: curl -fsSL https://claude.ai/install.sh | bash, depois "
                           f"sudo systemctl restart harvey-dashboard.",
                "path": os.environ.get("PATH", "")}
    if provider == "claude" and _token_source() == "none":
        return {"success": False, "message": "Nenhum token salvo. Cole o token do 'claude setup-token' e salve primeiro."}

    # The brain reports what went wrong through its logger, not its return
    # value; catch those lines so the browser sees the real reason.
    captured: list[str] = []

    class _Catch(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    catcher = _Catch(level=logging.WARNING)
    brain_logger = logging.getLogger("harvey.brain")
    brain_logger.addHandler(catcher)
    state = StateManager()
    try:
        await state.init_db()
        brain = Brain(state, brain_cfg)
        started = time.monotonic()
        try:
            reply = await asyncio.wait_for(
                brain.think("Respond with exactly: HARVEY_READY", session_id="harvey-web-test",
                            agent="dashboard", task="connection_test"),
                timeout=180,
            )
        except asyncio.TimeoutError:
            return {"success": False, "message": "A IA não respondeu em 3 minutos.", "log": captured[-5:]}
        elapsed = time.monotonic() - started
        if "HARVEY_READY" in (reply or ""):
            return {"success": True, "message": f"A IA respondeu em {elapsed:.1f}s. Tudo certo.", "seconds": round(elapsed, 1)}
        detail = " | ".join(line[:300] for line in captured[-3:]) or "sem detalhes no log"
        return {"success": False,
                "message": f"A IA não respondeu. O que o Claude disse: {detail}",
                "reply": (reply or "")[:200], "log": captured[-5:]}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "message": f"Erro ao testar: {str(e)[:200]}", "log": captured[-5:]}
    finally:
        brain_logger.removeHandler(catcher)


# ── Mailbox sign-in from the browser (Gmail / Microsoft 365) ──

# state → {"provider", "verifier", "redirect_uri", "tenant", "expires"}
_oauth_states: dict[str, dict] = {}


def _new_state(**fields) -> str:
    now = time.time()
    for key in [k for k, v in _oauth_states.items() if v["expires"] < now]:
        _oauth_states.pop(key, None)
    state = secrets.token_urlsafe(24)
    _oauth_states[state] = {**fields, "expires": now + 600}
    return state


def _pop_state(state: str, provider: str) -> dict | None:
    entry = _oauth_states.pop(state or "", None)
    if not entry or entry["provider"] != provider or entry["expires"] < time.time():
        return None
    return entry


def _back_to(tab: str, **params) -> RedirectResponse:
    query = urllib.parse.urlencode(params) if params else ""
    return RedirectResponse(url=f"/?{query}#{tab}" if query else f"/#{tab}", status_code=303)


@router.get("/api/gmail/auth/url")
async def gmail_auth_url(request: Request):
    from harvey.integrations import gmail as g
    env = _read_env()
    client_id = (env.get("GMAIL_CLIENT_ID") or os.getenv("GMAIL_CLIENT_ID", "")).strip()
    client_secret = (env.get("GMAIL_CLIENT_SECRET") or os.getenv("GMAIL_CLIENT_SECRET", "")).strip()
    redirect_uri = _public_base_url(request) + "/api/gmail/callback"
    if not client_id or not client_secret:
        return {"success": False, "redirect_uri": redirect_uri,
                "message": "Salve o Client ID e o Client Secret do Google antes de conectar."}
    state = _new_state(provider="gmail", redirect_uri=redirect_uri)
    url = g.AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": g.SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
    return {"success": True, "url": url, "redirect_uri": redirect_uri}


@router.get("/api/gmail/callback")
async def gmail_callback(request: Request):
    from harvey.integrations import gmail as g
    q = request.query_params
    entry = _pop_state(q.get("state", ""), "gmail")
    if entry is None:
        return _back_to("email", error="Sessão de login expirou. Tente conectar de novo.")
    if q.get("error") or not q.get("code"):
        return _back_to("email", error=f"Google recusou: {q.get('error') or 'sem código'}")
    env = _read_env()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(g.TOKEN_URL, data={
                "client_id": (env.get("GMAIL_CLIENT_ID") or "").strip(),
                "client_secret": (env.get("GMAIL_CLIENT_SECRET") or "").strip(),
                "code": q.get("code"),
                "grant_type": "authorization_code",
                "redirect_uri": entry["redirect_uri"],
            })
    except httpx.HTTPError as e:
        return _back_to("email", error=f"Falha ao falar com o Google: {e}")
    if resp.status_code != 200:
        return _back_to("email", error=f"Google não aceitou o código ({resp.status_code}). Confira o Client Secret e a URI de redirecionamento.")
    data = resp.json()
    if not data.get("refresh_token"):
        return _back_to("email", error="O Google não devolveu um refresh token. Remova o Harvey dos apps conectados da sua conta Google e tente de novo.")
    g._save_token({
        "refresh_token": data["refresh_token"],
        "scope": data.get("scope", g.SCOPE),
        "obtained_at": int(time.time()),
    })
    return _back_to("email", ok="gmail")


@router.post("/api/gmail/disconnect")
async def gmail_disconnect():
    from harvey.integrations import gmail as g
    try:
        g.TOKEN_FILE.unlink(missing_ok=True)
    except OSError as e:
        return {"success": False, "message": str(e)}
    return {"success": True}


@router.get("/api/microsoft/auth/url")
async def microsoft_auth_url(request: Request):
    from harvey.integrations import microsoft as m
    env = _read_env()
    client_id = (env.get("M365_CLIENT_ID") or os.getenv("M365_CLIENT_ID", "")).strip()
    tenant = (env.get("M365_TENANT_ID") or os.getenv("M365_TENANT_ID", "")).strip() or "organizations"
    redirect_uri = _public_base_url(request) + "/api/microsoft/callback"
    if not client_id:
        return {"success": False, "redirect_uri": redirect_uri,
                "message": "Salve o Application (client) ID da Microsoft antes de conectar."}
    verifier, challenge = m._pkce_pair()
    state = _new_state(provider="microsoft", redirect_uri=redirect_uri, verifier=verifier, tenant=tenant)
    url = f"{m.LOGIN}/{tenant}/oauth2/v2.0/authorize?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": m.SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    })
    return {"success": True, "url": url, "redirect_uri": redirect_uri}


@router.get("/api/microsoft/callback")
async def microsoft_callback(request: Request):
    from harvey.integrations import microsoft as m
    q = request.query_params
    entry = _pop_state(q.get("state", ""), "microsoft")
    if entry is None:
        return _back_to("email", error="Sessão de login expirou. Tente conectar de novo.")
    if q.get("error") or not q.get("code"):
        return _back_to("email", error=f"Microsoft recusou: {q.get('error_description') or q.get('error') or 'sem código'}")
    env = _read_env()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(m._token_url(entry["tenant"]), data={
                "client_id": (env.get("M365_CLIENT_ID") or "").strip(),
                "grant_type": "authorization_code",
                "code": q.get("code"),
                "redirect_uri": entry["redirect_uri"],
                "code_verifier": entry["verifier"],
                "scope": m.SCOPE,
            })
    except httpx.HTTPError as e:
        return _back_to("email", error=f"Falha ao falar com a Microsoft: {e}")
    if resp.status_code != 200:
        return _back_to("email", error=f"Microsoft não aceitou o código ({resp.status_code}). Confira o registro do app e a URI de redirecionamento.")
    data = resp.json()
    if not data.get("refresh_token"):
        return _back_to("email", error="A Microsoft não devolveu um refresh token (falta offline_access?).")
    m._save_token({
        "refresh_token": data["refresh_token"],
        "tenant": entry["tenant"],
        "scope": data.get("scope", m.SCOPE),
        "obtained_at": int(time.time()),
    })
    return _back_to("email", ok="microsoft")


@router.post("/api/microsoft/disconnect")
async def microsoft_disconnect():
    from harvey.integrations import microsoft as m
    try:
        m.TOKEN_FILE.unlink(missing_ok=True)
    except OSError as e:
        return {"success": False, "message": str(e)}
    return {"success": True}


@router.get("/api/email/status")
async def email_status(request: Request):
    from harvey.integrations import gmail as g
    from harvey.integrations import microsoft as m
    base = _public_base_url(request)
    return {
        "provider": _dash()._current_provider(),
        "gmail_connected": g.TOKEN_FILE.is_file(),
        "microsoft_connected": m.TOKEN_FILE.is_file(),
        "gmail_redirect_uri": base + "/api/gmail/callback",
        "microsoft_redirect_uri": base + "/api/microsoft/callback",
    }


@router.post("/api/email/provider")
async def set_email_provider(request: Request):
    try:
        body = _json_body(await request.json())
    except Exception:
        body = None
    provider = str((body or {}).get("provider", "")).strip().lower()
    if provider not in ("gmail", "microsoft", "smtp", "instantly"):
        return JSONResponse({"success": False, "message": "Provedor inválido."}, status_code=400)
    current = _read_yaml(_active_config_path())
    merged = _deep_merge(current, {"channels": {"email": {"provider": provider}}})
    target = _config_path_for_write()
    try:
        target.write_text(
            "# Gerado pelo painel do Harvey. Este arquivo vence o harvey.yaml.\n"
            + yaml.safe_dump(merged, allow_unicode=True, sort_keys=False, width=100),
            encoding="utf-8",
        )
    except OSError as e:
        return JSONResponse({"success": False, "message": f"Não consegui gravar: {e}"}, status_code=500)
    return {"success": True, "provider": provider}


@router.post("/api/email/test")
async def email_test():
    """Real round-trip against the configured mailbox."""
    from harvey.config import ConfigError, load_config, load_env
    from harvey.integrations.mail_provider import get_mail_provider
    try:
        config = load_config()
    except ConfigError as e:
        return {"success": False, "message": f"Configuração inválida: {str(e)[:200]}"}
    except Exception as e:  # noqa: BLE001 — pydantic errors
        return {"success": False, "message": "A configuração ainda tem campos inválidos. Abra Configurações e salve."}
    env = load_env()
    provider_name = config.channels.email.provider
    if provider_name == "instantly":
        key = env.instantly_api_key
        if not key:
            return {"success": False, "message": "Informe a API key do Instantly."}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get("https://api.instantly.ai/api/v2/accounts",
                                        headers={"Authorization": f"Bearer {key}"})
            ok = resp.status_code == 200
            return {"success": ok, "message": "Conectado ao Instantly." if ok else f"Instantly respondeu {resp.status_code}."}
        except httpx.HTTPError as e:
            return {"success": False, "message": f"Falha de conexão: {e}"}
    provider = get_mail_provider(config, env)
    if provider is None:
        return {"success": False, "message": f"Provedor desconhecido: {provider_name}"}
    if not provider.is_configured():
        return {"success": False, "message": "Esse provedor ainda não está configurado ou conectado."}
    try:
        ok, message = await asyncio.wait_for(provider.test_connection(), timeout=60)
    except asyncio.TimeoutError:
        return {"success": False, "message": "O servidor de e-mail não respondeu em 60s."}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "message": f"Erro: {str(e)[:200]}"}
    return {"success": bool(ok), "message": message}


# ── Training (harvey train <url>) from the browser ──

_train_process: subprocess.Popen | None = None


def _train_running() -> bool:
    global _train_process
    if _train_process is not None and _train_process.poll() is None:
        return True
    return False


@router.get("/api/train/status")
async def train_status():
    running = _train_running()
    exit_code = None if running or _train_process is None else _train_process.returncode
    lines: list[str] = []
    if TRAIN_LOG.exists():
        try:
            with open(TRAIN_LOG, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 65536))
                lines = f.read().decode("utf-8", errors="replace").strip().splitlines()[-120:]
        except OSError:
            pass
    trained = (PROJECT_ROOT / "skills" / "product_knowledge.md").is_file()
    return {"running": running, "exit_code": exit_code, "lines": lines,
            "trained": trained, "local_config": LOCAL_CONFIG.is_file()}


@router.post("/api/train/run")
async def train_run(request: Request):
    global _train_process
    try:
        body = _json_body(await request.json())
    except Exception:
        body = None
    url = str((body or {}).get("url", "")).strip()
    if not url:
        return JSONResponse({"success": False, "message": "Informe o endereço do site."}, status_code=400)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        max_pages = int((body or {}).get("max_pages", 100) or 100)
    except (TypeError, ValueError):
        max_pages = 100
    max_pages = max(1, min(max_pages, 500))
    if _train_running():
        return {"success": False, "message": "Já tem um treinamento rodando."}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        log = open(TRAIN_LOG, "w", encoding="utf-8")
        try:
            log.write(f"== Treinando com {url} (até {max_pages} páginas) ==\n")
            log.flush()
            _train_process = subprocess.Popen(
                [sys.executable, "-m", "harvey.cli", "train", url, str(max_pages)],
                cwd=str(PROJECT_ROOT), stdout=log, stderr=log, start_new_session=True,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        finally:
            log.close()
    except OSError as e:
        return {"success": False, "message": f"Não consegui iniciar: {e}"}
    return {"success": True}


@router.post("/api/train/stop")
async def train_stop():
    global _train_process
    if not _train_running():
        return {"success": False, "message": "Nenhum treinamento rodando."}
    try:
        _train_process.terminate()
    except OSError:
        pass
    return {"success": True}


# ── Cloudflare Access guard ──
#
# Behind a Cloudflare Tunnel the only way in is through Access, which signs
# every request with a JWT. Verifying it here means that even if the tunnel
# were ever pointed at the app without an Access policy, nothing answers.
# Enabled by setting CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD in .env; off by
# default so a plain `harvey dashboard` on a laptop still works.

_jwks_cache: dict = {"keys": None, "fetched": 0.0, "team": ""}
JWKS_TTL = 3600.0


async def _access_jwks(team_domain: str) -> dict | None:
    now = time.time()
    if (_jwks_cache["keys"] and _jwks_cache["team"] == team_domain
            and now - _jwks_cache["fetched"] < JWKS_TTL):
        return _jwks_cache["keys"]
    url = f"https://{team_domain}/cdn-cgi/access/certs"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            _jwks_cache.update(keys=resp.json(), fetched=now, team=team_domain)
            return _jwks_cache["keys"]
        logger.warning("Access certs returned %s", resp.status_code)
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Could not fetch Access certs: %s", e)
    return _jwks_cache["keys"] if _jwks_cache["team"] == team_domain else None


def _access_settings() -> tuple[str, str, set[str]]:
    env = _read_env()

    def get(key):
        return (os.getenv(key, "").strip() or env.get(key, "").strip())

    team, aud = get("CF_ACCESS_TEAM_DOMAIN"), get("CF_ACCESS_AUD")
    if team and not team.endswith(".cloudflareaccess.com"):
        team = team + ".cloudflareaccess.com"
    allowed = {e.strip().lower() for e in get("CF_ACCESS_ALLOWED_EMAILS").split(",") if e.strip()}
    return team, aud, allowed


def access_enforced() -> bool:
    """True when the Cloudflare Access guard is configured (team + AUD)."""
    team, aud, _ = _access_settings()
    return bool(team and aud)


async def verify_access(request: Request) -> str | None:
    """None when the request may proceed; otherwise a reason to refuse."""
    team, aud, allowed = _access_settings()
    if not team or not aud:
        return None
    token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get("CF_Authorization")
    if not token:
        return "Acesso negado: faça login pelo Cloudflare Access."
    try:
        import jwt
        from jwt import PyJWKClient  # noqa: F401 — presence check for [crypto]
    except ImportError:
        return "PyJWT não está instalado; o guard do Cloudflare Access não pode validar o login."
    jwks = await _access_jwks(team)
    if not jwks:
        return "Não consegui obter as chaves do Cloudflare Access."
    try:
        header = jwt.get_unverified_header(token)
        key_data = next((k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")), None)
        if key_data is None:
            _jwks_cache["fetched"] = 0.0  # key rotation: refetch next time
            return "Chave de assinatura desconhecida; tente recarregar."
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key_data))
        claims = jwt.decode(token, public_key, algorithms=["RS256"], audience=aud,
                            issuer=f"https://{team}")
    except Exception as e:  # noqa: BLE001 — any failure is a refusal
        return f"Login do Cloudflare Access inválido: {type(e).__name__}"
    # Second gate, independent of the Access policy: only the people listed
    # in CF_ACCESS_ALLOWED_EMAILS get in, even if the policy is ever loosened.
    if allowed:
        email = str(claims.get("email", "")).strip().lower()
        if email not in allowed:
            return f"O e-mail {email or '(sem e-mail)'} não está na lista de pessoas autorizadas."
    return None


def install(app):
    """Mount routes and the guard on the dashboard app."""
    app.include_router(router)

    @app.middleware("http")
    async def _access_guard(request: Request, call_next):
        reason = await verify_access(request)
        if reason is not None:
            logger.warning("Refused %s %s: %s", request.method, request.url.path, reason)
            return JSONResponse({"error": reason}, status_code=403)
        return await call_next(request)

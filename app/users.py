"""Per-user Laserfiche credentials.

Identity comes from the reverse proxy (oauth2-proxy sets X-Auth-Request-Email). The user's
Laserfiche password is stored encrypted (Fernet, key = APP_SECRET) so the app can re-login
when Laserfiche's short-lived token expires. One LaserficheClient is kept per user.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi import HTTPException, Request

from .laserfiche import LaserficheClient, LaserficheError

_lock = threading.Lock()
_clients: dict[str, LaserficheClient] = {}


def _fernet() -> Fernet:
    secret = os.environ.get("APP_SECRET")
    if not secret:
        raise RuntimeError("APP_SECRET is not set (any long random string; used to encrypt stored Laserfiche passwords)")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def _store_path() -> Path:
    return Path(os.environ.get("USERS_FILE", os.path.join(os.environ.get("WORK_DIR", "./work"), "users.json")))


def _load() -> dict:
    p = _store_path()
    return json.loads(p.read_text()) if p.exists() else {}


def _dump(d: dict) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    tmp.replace(p)


def current_email(request: Request) -> str:
    email = (request.headers.get("x-auth-request-email") or os.environ.get("DEV_USER_EMAIL") or "").strip().lower()
    if not email:
        raise HTTPException(401, "No signed-in user (expected X-Auth-Request-Email from the proxy)")
    return email


def get_creds(email: str) -> dict | None:
    rec = _load().get(email)
    if not rec:
        return None
    return {"username": rec["username"], "password": _fernet().decrypt(rec["password"].encode()).decode()}


def set_creds(email: str, username: str, password: str) -> None:
    # verify before storing
    LaserficheClient(username, password)._login()
    with _lock:
        d = _load()
        d[email] = {"username": username, "password": _fernet().encrypt(password.encode()).decode()}
        _dump(d)
        _clients.pop(email, None)


def clear_creds(email: str) -> None:
    with _lock:
        d = _load()
        d.pop(email, None)
        _dump(d)
        _clients.pop(email, None)


def client_for_email(email: str) -> LaserficheClient:
    with _lock:
        c = _clients.get(email)
        if c:
            return c
        creds = get_creds(email)
        if not creds:
            raise HTTPException(401, "lf_credentials_required")
        c = LaserficheClient(creds["username"], creds["password"])
        _clients[email] = c
        return c


def lf_for(request: Request) -> LaserficheClient:
    """FastAPI dependency: the Laserfiche client for the signed-in user."""
    return client_for_email(current_email(request))


def service_client() -> LaserficheClient | None:
    """Optional service account for unattended work (mailbox capture)."""
    if os.environ.get("LF_USERNAME") and os.environ.get("LF_PASSWORD"):
        return LaserficheClient(os.environ["LF_USERNAME"], os.environ["LF_PASSWORD"])
    return None

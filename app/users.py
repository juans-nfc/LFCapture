"""Per-user Laserfiche credentials with the app's own session cookie.

The Laserfiche login IS the identity: the user enters their LF username/password once, the app
verifies it against Laserfiche, stores the password encrypted (Fernet, key = APP_SECRET) keyed by
the LF username, and sets a signed cookie holding that username. Every later request looks the
client up by that cookie. No dependency on the reverse proxy's identity headers.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, Response

from .laserfiche import LaserficheClient

COOKIE = "lfcapture_user"
COOKIE_DAYS = int(os.environ.get("SESSION_DAYS", "30"))

_lock = threading.Lock()
_clients: dict[str, LaserficheClient] = {}


def _fernet() -> Fernet:
    secret = os.environ.get("APP_SECRET")
    if not secret:
        raise RuntimeError("APP_SECRET is not set (any long random string; encrypts stored Laserfiche passwords and signs sessions)")
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


def _key(username: str) -> str:
    return username.strip().lower()


# ---- session cookie ----
def current_user(request: Request) -> str | None:
    """LF username from the session cookie, or None."""
    tok = request.cookies.get(COOKIE)
    if not tok:
        return None
    try:
        return _fernet().decrypt(tok.encode(), ttl=COOKIE_DAYS * 86400).decode()
    except (InvalidToken, ValueError):
        return None


def set_session(response: Response, username: str) -> None:
    tok = _fernet().encrypt(_key(username).encode()).decode()
    response.set_cookie(COOKIE, tok, max_age=COOKIE_DAYS * 86400, httponly=True, samesite="lax",
                        secure=os.environ.get("COOKIE_SECURE", "1") != "0", path="/")


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")


# ---- credential store ----
def _short(name: str) -> str:
    """'northernfruit\\juans', 'juans@northernfruit.com', 'JUANS' -> 'juans'"""
    n = name.strip().lower()
    n = n.split("\\")[-1]
    n = n.split("@")[0]
    return n


def resolve_username(name: str) -> str | None:
    """Find the stored login that belongs to this person, whichever form the name is in."""
    d = _load()
    if _key(name) in d:
        return _key(name)
    want = _short(name)
    matches = [k for k in d if _short(k) == want]
    return matches[0] if len(matches) == 1 else None


def get_creds(username: str) -> dict | None:
    rec = _load().get(_key(username))
    if not rec:
        k = resolve_username(username)
        rec = _load().get(k) if k else None
    if not rec:
        return None
    return {"username": rec["username"], "password": _fernet().decrypt(rec["password"].encode()).decode()}


def set_creds(username: str, password: str) -> None:
    LaserficheClient(username, password)._login()  # verify before storing
    with _lock:
        d = _load()
        d[_key(username)] = {"username": username.strip(), "password": _fernet().encrypt(password.encode()).decode(), "since": time.time()}
        _dump(d)
        _clients.pop(_key(username), None)


def clear_creds(username: str) -> None:
    with _lock:
        d = _load()
        d.pop(_key(username), None)
        _dump(d)
        _clients.pop(_key(username), None)


def client_for(username: str) -> LaserficheClient:
    k = resolve_username(username) or _key(username)
    with _lock:
        c = _clients.get(k)
        if c:
            return c
        creds = get_creds(username)
        if not creds:
            raise HTTPException(401, "lf_credentials_required")
        c = LaserficheClient(creds["username"], creds["password"])
        _clients[k] = c
        return c


def lf_for(request: Request) -> LaserficheClient:
    """FastAPI dependency: the Laserfiche client for the signed-in user."""
    u = current_user(request)
    if not u:
        raise HTTPException(401, "lf_credentials_required")
    return client_for(u)


def require_user(request: Request) -> str:
    u = current_user(request)
    if not u:
        raise HTTPException(401, "lf_credentials_required")
    return u


def service_client() -> LaserficheClient | None:
    """Optional service account for unattended work (mailbox capture)."""
    if os.environ.get("LF_USERNAME") and os.environ.get("LF_PASSWORD"):
        return LaserficheClient(os.environ["LF_USERNAME"], os.environ["LF_PASSWORD"])
    return None


# ---- per-user preferences (stored alongside the credentials record) ----
DEFAULT_PREFS = {
    "working_folder": "",          # default Save-to and Browse start; blank = LF_INBOX_PATH
    "show_shared": True,           # see drop-folder / mailbox documents in the queue
    "scan_only_no_template": True, # From Laserfiche defaults
    "scan_recursive": False,
    "unsure_below": 0.8,           # confidence under this shows the "not sure" flag
    "after_save": "next",          # next | stay
    "split_batches": "ask",        # never | ask | always — detect several documents inside one uploaded PDF and split them
    "auto_file": False,            # file my uploads automatically when confident (no review)
    "auto_file_min": 0.9,          # ...at or above this confidence
}


def get_prefs(username: str) -> dict:
    rec = _load().get(_key(username)) or {}
    p = {**DEFAULT_PREFS, **(rec.get("prefs") or {})}
    if isinstance(p.get("split_batches"), bool):  # older stored value
        p["split_batches"] = "always" if p["split_batches"] else "never"
    return p


def set_prefs(username: str, prefs: dict) -> dict:
    clean = {k: prefs[k] for k in DEFAULT_PREFS if k in prefs}
    with _lock:
        d = _load()
        rec = d.get(_key(username))
        if not rec:
            raise HTTPException(401, "lf_credentials_required")
        rec["prefs"] = {**(rec.get("prefs") or {}), **clean}
        _dump(d)
    return {**DEFAULT_PREFS, **rec["prefs"]}

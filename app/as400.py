"""
AS400 → Laserfiche metadata bridge.

Two halves:
  1. The AS400 Bridge desktop agent POSTs "user X is currently looking at invoice Y in Computech"
     to /api/as400/current whenever the record on their BlueZone screen changes.
  2. The Laserfiche business process "Fill from AS400" POSTs {entry_id, user} to /api/as400/apply;
     we take the latest record for that user, map it onto the AP template via as400_map.json,
     and write template + fields straight onto the entry.

Only the newest record per user is kept, persisted to WORK_DIR/as400_current.json so a
container restart does not lose it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel

from . import users
from .laserfiche import LaserficheClient, LaserficheError

log = logging.getLogger("lf-capture")

AGENT_TOKEN = os.environ.get("AS400_AGENT_TOKEN", "")
MAX_AGE = int(os.environ.get("AS400_MAX_AGE_SECONDS", "900"))          # ignore records older than this (15 min)
MAP_FILE = Path(os.environ.get("AS400_MAP_FILE", str(Path(__file__).with_name("as400_map.json"))))
STORE = Path(os.environ.get("WORK_DIR", "./work")) / "as400_current.json"

_lock = threading.Lock()
_current: dict[str, dict] = {}   # short username -> {"at": epoch, "machine": str, "record": {...}}


# ---------- storage ----------
def _load_store() -> None:
    global _current
    try:
        if STORE.exists():
            _current = json.loads(STORE.read_text())
    except Exception as e:  # corrupt file: start clean
        log.warning("as400: could not read %s: %s", STORE, e)
        _current = {}


def _save_store() -> None:
    try:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.write_text(json.dumps(_current))
    except Exception as e:
        log.warning("as400: could not write %s: %s", STORE, e)


_load_store()


def _map() -> dict:
    if not MAP_FILE.exists():
        raise HTTPException(500, f"AS400 field map not found: {MAP_FILE}")
    return json.loads(MAP_FILE.read_text())


# ---------- request models ----------
class CurrentRequest(BaseModel):
    user: str
    domain: str | None = None
    machine: str | None = None
    capturedAt: str | None = None
    screenId: str | None = None          # None/blank = user left the AP screens → clear
    fields: dict[str, str] = {}
    lists: dict[str, list[str]] = {}
    hasMorePages: bool = False


class ApplyRequest(BaseModel):
    entry_id: int | None = None
    entry_ids: list[int] = []
    token: str | None = None             # alternative to the Authorization header (Workflow can't always set headers)
    user: str                            # Workflow %(Initiator): domain\user or user@domain
    template: str | None = None          # override the template named in as400_map.json


# ---------- value mapping ----------
def _pick(record: dict, spec: str) -> list[str]:
    """'apInvoiceNumber|vphInvoiceNumber' → first non-empty value found in fields or lists."""
    for key in [s.strip() for s in spec.split("|") if s.strip()]:
        v = record.get("fields", {}).get(key)
        if v not in (None, ""):
            return [str(v)]
        lst = record.get("lists", {}).get(key)
        if lst:
            return [str(x) for x in lst if x not in (None, "")]
    return []


_DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\s*$")


def _coerce(values: list[str], ftype: str) -> list[str]:
    """AS400 shows M/D/YYYY; Laserfiche date fields want ISO. Numbers: strip separators."""
    out = []
    for v in values:
        t = (ftype or "").lower()
        if t in ("date", "datetime"):
            m = _DATE_RE.match(v)
            if m:
                mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if yy < 100:
                    yy += 2000
                v = datetime(yy, mm, dd).strftime("%Y-%m-%d")
        elif t in ("number", "integer", "longinteger"):
            v = v.replace(",", "").replace("$", "").strip()
        out.append(v)
    return out


def build_fields(record: dict, tdef: dict | None, fmap: dict[str, str]) -> dict[str, list[str]]:
    """Map an AS400 record onto Laserfiche field names using the template definition for types/lengths."""
    defs = {f["name"]: f for f in (tdef or {}).get("fields", [])}
    out: dict[str, list[str]] = {}
    for lf_name, spec in fmap.items():
        values = _pick(record, spec)
        if not values:
            continue
        fdef = defs.get(lf_name)
        if fdef:
            values = _coerce(values, fdef.get("type", "String"))
            if not fdef.get("multi") and len(values) > 1:
                values = [", ".join(values)]
            if fdef.get("length"):
                values = [v[: fdef["length"]] for v in values]
        else:
            log.warning("as400: field %r is not in template %r — check as400_map.json", lf_name, (tdef or {}).get("name"))
        out[lf_name] = values
    return out


# ---------- routes ----------
def install(app: FastAPI, *, wf_token: str, wf_client: Callable[[str | None], tuple[LaserficheClient, str]],
            lenient_json: Callable[[bytes], dict], templates: Callable[..., list[dict]],
            log_activity: Callable[..., None], tag_failed: str) -> None:
    """Wire the routes into the main app; called from main.py once its helpers exist."""
    r = APIRouter(prefix="/api/as400")

    def _agent_auth(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        if not AGENT_TOKEN or auth != f"Bearer {AGENT_TOKEN}":
            raise HTTPException(401, "bad or missing AS400_AGENT_TOKEN")

    def _wf_auth(request: Request, body_token: str | None) -> None:
        auth = request.headers.get("authorization", "")
        if not wf_token or (auth != f"Bearer {wf_token}" and (body_token or "") != wf_token):
            raise HTTPException(401, "bad or missing LF_WORKFLOW_TOKEN")

    @r.post("/current")
    def api_current(req: CurrentRequest, request: Request):
        """Desktop agent: newest AS400 record for this user (or clear when screenId is blank)."""
        _agent_auth(request)
        who = users._short(req.user)
        with _lock:
            if not req.screenId:
                _current.pop(who, None)
                _save_store()
                return {"ok": True, "user": who, "cleared": True}
            _current[who] = {
                "at": time.time(),
                "machine": req.machine or "",
                "record": {"screenId": req.screenId, "capturedAt": req.capturedAt,
                           "fields": req.fields, "lists": req.lists, "hasMorePages": req.hasMorePages},
            }
            _save_store()
        inv = req.fields.get("apInvoiceNumber") or req.fields.get("vphInvoiceNumber") or ""
        return {"ok": True, "user": who, "invoice": inv}

    @r.get("/current")
    def api_current_get(request: Request, user: str | None = None):
        """Debug: what the server currently holds. Accepts either token."""
        auth = request.headers.get("authorization", "")
        if auth not in (f"Bearer {AGENT_TOKEN}", f"Bearer {wf_token}") or not auth.split(" ", 1)[-1]:
            raise HTTPException(401, "bad or missing token")
        with _lock:
            items = {k: {**v, "age_seconds": int(time.time() - v["at"])} for k, v in _current.items()}
        if user:
            return items.get(users._short(user)) or {}
        return items

    @r.post("/apply")
    async def api_apply(request: Request):
        """Laserfiche business process: write the user's current AS400 record onto the entry."""
        try:
            req = ApplyRequest(**lenient_json(await request.body()))
        except Exception as e:
            raise HTTPException(422, f"Could not read request body: {e}")
        _wf_auth(request, req.token)

        who = users._short(req.user)
        with _lock:
            cur = _current.get(who)
        if not cur:
            raise HTTPException(404, f"No AS400 record seen for {who}. Is AS400 Bridge running on their PC and showing an AP screen?")
        age = time.time() - cur["at"]
        if age > MAX_AGE:
            raise HTTPException(409, f"The AS400 record for {who} is {int(age // 60)} min old; bring the invoice back up in Computech and try again")

        record = cur["record"]
        m = _map()
        allowed = m.get("screens") or []
        if allowed and record.get("screenId") not in allowed:
            raise HTTPException(409, f"Screen {record.get('screenId')} is not one metadata can be taken from")

        ids = list(req.entry_ids) + ([req.entry_id] if req.entry_id else [])
        if not ids:
            raise HTTPException(400, "entry_id or entry_ids required")

        lf, acting = wf_client(req.user)
        template = req.template or m.get("template")
        if not template:
            raise HTTPException(500, "as400_map.json has no template name")
        tdef = next((t for t in templates(lf) if t["name"] == template), None)
        if tdef is None:
            raise HTTPException(500, f"Template {template!r} not found in Laserfiche (check as400_map.json)")

        mapped = build_fields(record, tdef, m.get("fields", {}))
        if not mapped:
            raise HTTPException(409, "AS400 record produced no field values — check as400_map.json")

        results = []
        for eid in ids:
            entry = lf.get_entry(eid)
            if entry.get("entryType") == "Folder":
                results.append({"entry_id": eid, "skipped": "folder"})
                continue
            current_tpl = entry.get("templateName") or None
            try:
                existing = lf.entry_fields(eid) if current_tpl == template else {}
                fields = {**existing, **mapped} if m.get("overwrite", True) else {**mapped, **{k: v for k, v in existing.items() if v}}
                lf.update_document(eid, template, fields, current_tpl)
                try:
                    lf.set_tags(eid, remove=[tag_failed])
                except LaserficheError:
                    pass
                log_activity(acting, kind="as400_apply", entry_id=eid, name=entry.get("name"), folder=entry.get("fullPath"),
                             template=template, screen=record.get("screenId"),
                             invoice=(mapped.get(m.get("invoice_field", ""), [""]) or [""])[0])
                results.append({"entry_id": eid, "name": entry.get("name"), "fields": mapped})
            except Exception as e:
                try:
                    lf.set_tags(eid, add=[tag_failed])
                except Exception:
                    pass
                log_activity(acting, kind="as400_apply_failed", entry_id=eid, name=entry.get("name"), error=str(e))
                results.append({"entry_id": eid, "error": str(e)})

        failed = [x for x in results if "error" in x]
        body: dict[str, Any] = {"acting_as": acting, "template": template, "results": results}
        if failed and len(failed) == len(results):
            raise HTTPException(500, json.dumps(body))
        return body

    app.include_router(r)

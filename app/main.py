from __future__ import annotations

import json
import logging
import os
import shutil
import queue
import re
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import extractor, mailbox  # noqa: E402
from .laserfiche import LaserficheClient, LaserficheError  # noqa: E402
from . import users  # noqa: E402

INBOX = Path(os.environ.get("INBOX_DIR", "./inbox"))
WORK = Path(os.environ.get("WORK_DIR", "./work"))
DONE = INBOX / "_done"
FAILED = INBOX / "_failed"
for p in (INBOX, WORK, DONE, FAILED):
    p.mkdir(parents=True, exist_ok=True)

AUTO_SAVE = float(os.environ.get("AUTO_SAVE_CONFIDENCE", "0") or 0)

app = FastAPI(title="LF Capture")
_svc = users.service_client()          # optional; only the mailbox path uses it
_lock = threading.Lock()
_templates: list[dict] = []
_templates_at = 0.0
_inbox_id: int | None = None


# ---------- helpers --------------------------------------------------
def templates(lf: LaserficheClient | None = None, refresh: bool = False) -> list[dict]:
    """Template catalog (shared cache; any authenticated client may refresh it)."""
    global _templates, _templates_at
    with _lock:
        if refresh or not _templates or time.time() - _templates_at > 3600:
            if lf is None:
                if not _templates:
                    raise LaserficheError("Template catalog not loaded yet")
                return _templates
            _templates = lf.templates()
            _templates_at = time.time()
        return _templates


_folder_ids: dict[str, int] = {}


def folder_entry_id(lf: LaserficheClient, path: str | None = None) -> int:
    path = (path or os.environ["LF_INBOX_PATH"]).strip().rstrip("\\") or "\\"
    if path not in _folder_ids:
        _folder_ids[path] = lf.entry_id_by_path(path)
    return _folder_ids[path]


def _write_meta(path: Path, data: dict) -> None:
    """Atomic write: readers never see a half-written file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def _read_meta(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _job_dir(job_id: str, user: str | None = None) -> Path:
    """Job folder; when a user is given, enforce that the job is theirs or shared (no owner)."""
    d = WORK / job_id
    if not d.exists():
        raise HTTPException(404, "Unknown job")
    if user is not None:
        m = d / "meta.json"
        owner = json.loads(m.read_text()).get("owner") if m.exists() else None
        if owner and owner != user:
            raise HTTPException(403, "That document is in another user's queue")
    return d


def _new_job(pdf_bytes: bytes, source_name: str, context: str = "", owner: str | None = None) -> str:
    job_id = uuid.uuid4().hex[:12]
    d = WORK / job_id
    d.mkdir()
    (d / "doc.pdf").write_bytes(pdf_bytes)
    _write_meta(d / "meta.json", {"source": source_name, "created": time.time(), "context": context, "owner": owner})
    return job_id


ALIASES_FILE = Path(os.environ.get("ALIASES_FILE", str(WORK / "aliases.json")))


def _aliases() -> dict:
    """{ "Sold To": { "northern fruit company": "Northern Fruit", ... }, "*": {...} } — keys compared case-insensitively."""
    try:
        raw = json.loads(ALIASES_FILE.read_text()) if ALIASES_FILE.exists() else {}
    except Exception:
        return {}
    return {field: {str(k).strip().lower(): v for k, v in mapping.items()} for field, mapping in raw.items()}


def _nums(s: str) -> set[int]:
    return {int(x) for x in re.findall(r"\d+", s or "")}


_NAME_NOISE = {"co", "company", "inc", "llc", "ltd", "corp", "the", "and", "of", "mr", "mrs", "ms"}


def _name_tokens(s: str) -> set[str]:
    """'Sanchez, Javier' / 'JAVIER SANCHEZ' / 'J. Sanchez' -> comparable tokens (initials kept as single letters)."""
    return {t.strip(".") for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t.strip(".") and t.strip(".") not in _NAME_NOISE}


def _match_by_name(v: str, allowed: list[str]) -> str | None:
    """Snap a proposed name to the single list entry naming the same person/company."""
    pt = _name_tokens(v)
    if not pt:
        return None
    scored = []
    for a in allowed:
        at = _name_tokens(a)
        if not at:
            continue
        full = {t for t in at if len(t) > 1}; pfull = {t for t in pt if len(t) > 1}
        if pfull and full and (pfull <= at or full <= pt):              # all full words of one side appear in the other
            scored.append(a)
        elif pfull and full and len(pfull & full) >= 2:                  # first + last both present, order/extra words ignored
            scored.append(a)
        elif len(pfull & full) == 1 and (pt - pfull) and any(t[0] in {x[0] for x in full - pfull} for t in pt - pfull):
            scored.append(a)                                             # surname + matching initial
    return scored[0] if len(scored) == 1 else None


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%Y%m%d")


def _to_iso_date(v: str) -> str | None:
    """Anything a document might print -> YYYY-MM-DD; a bare year -> YYYY-01-01. None if unparseable."""
    from datetime import datetime
    t = str(v).strip()
    if re.fullmatch(r"\d{4}", t):
        return f"{t}-01-01"
    t2 = t.split("T")[0].split(" ")[0] if re.match(r"\d{4}-\d{2}-\d{2}", t) else t
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(t2, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _snap_values(template: str | None, fields: dict[str, list[str]], catalog: list[dict]) -> dict[str, list[str]]:
    """Make proposed values match what Laserfiche will accept: aliases first, then fixed list values.
    For numbered list values, a proposed value whose number matches a *different* list entry is re-pointed by number."""
    tdef = next((t for t in catalog if t["name"] == template), None)
    aliases = _aliases()
    out = {}
    for name, vals in fields.items():
        fdef = next((f for f in (tdef["fields"] if tdef else []) if f["name"] == name), None)
        amap = {**aliases.get("*", {}), **aliases.get(name, {})}
        allowed = (fdef or {}).get("list") or []
        ftype = (fdef or {}).get("type") or ""
        snapped = []
        for v in vals:
            if ftype in ("Date", "DateTime"):
                iso = _to_iso_date(v)
                if iso is None:
                    logging.getLogger("lf-capture").warning("dropping unparseable date %r for %s", v, name)
                    continue
                v = iso if ftype == "Date" else iso + "T00:00:00"
            key = str(v).strip().lower()
            if key in amap:
                v = amap[key]
            if allowed:
                exact = next((a for a in allowed if a.strip().lower() == str(v).strip().lower()), None)
                if exact:
                    # numbered lists: if the proposed value's own number points at a different single entry, prefer that
                    pn = _nums(str(v))
                    if pn and any(_nums(a) for a in allowed):
                        by_num = [a for a in allowed if _nums(a) & pn]
                        if len(by_num) == 1 and by_num[0] != exact:
                            logging.getLogger("lf-capture").info("list snap by number: %r -> %r", v, by_num[0])
                            exact = by_num[0]
                    v = exact
                else:
                    close = [a for a in allowed if str(v).strip().lower().startswith(a.strip().lower()) or a.strip().lower().startswith(str(v).strip().lower())]
                    if len(close) == 1:
                        v = close[0]
                    elif not any(_nums(a) for a in allowed):              # name-style lists: same person, different formatting
                        byname = _match_by_name(str(v), allowed)
                        if byname:
                            logging.getLogger("lf-capture").info("list snap by name: %r -> %r", v, byname)
                            v = byname
            snapped.append(v)
        out[name] = snapped
    return out


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


NAME_HINT_MODE = os.environ.get("NAME_HINT_MODE", "force").lower()   # force = a template named in the file name is used; suggest = only a hint


def _name_matches(name: str, catalog: list[dict]) -> tuple[str, list[str]]:
    """'Castaneda-Perez-Maybeth-W4' + template 'W4' -> (base name, [matching template names, longest first])."""
    base = re.sub(r"\.pdf$", "", (name or "").split(":", 1)[-1].strip(), flags=re.I)
    base = re.sub(r"\s*\[p[\d-]+\]$", "", base)   # strip split-part suffix
    sq = _squash(base)
    segs = [_squash(x) for x in re.split(r"[-_ ,./]+", base) if len(_squash(x)) >= 2]   # "W4", "i9", "Emergency", ...
    hits = []
    for t in catalog:
        tn = t["name"]
        if _is_generic(tn):
            continue
        tsq = _squash(tn)
        words = [_squash(w) for w in tn.split() if _squash(w)]          # "I-9 Employment" -> ["i9", "employment"]
        if len(tsq) >= 2 and tsq in sq:                       # whole template name inside the file name
            hits.append(tn)
        elif any(seg == tsq or (len(seg) >= 2 and tsq.startswith(seg) and len(seg) >= min(4, len(tsq))) or (words and seg == words[0]) for seg in segs):
            hits.append(tn)                                   # a name segment equals the template, its start, or its first word
    exact = {n for n in hits if _squash(n) in segs}                      # a segment IS the template name
    hits.sort(key=lambda n: (0 if n in exact else 1, -len(_squash(n))))   # exact first, then longest
    return base, hits


def _name_hint(name: str, catalog: list[dict]) -> str:
    base, hits = _name_matches(name, catalog)
    line = f"Document name: \"{base}\". File names here usually include the document type (e.g. '...-W4', '...-i9', '...-Emergency-Contact-Form'); treat the name as strong evidence for the template when it matches one."
    if hits:
        line += f" The name suggests template: {hits[0]}." + (f" (also possible: {', '.join(hits[1:3])})" if len(hits) > 1 else "")
    return line


def _run_extract(job_id: str, forced: str | None, lf: LaserficheClient | None = None) -> dict:
    d = _job_dir(job_id)
    meta = json.loads((d / "meta.json").read_text())
    cat = templates(lf)
    ctx = (meta.get("context", "") + "\n" + _name_hint(meta.get("source", ""), cat)).strip()
    by_name = None
    base, hits = _name_matches(meta.get("source", ""), cat)
    if not forced and NAME_HINT_MODE == "force":
        if hits and (len(hits) == 1 or _squash(hits[0]) in [_squash(x) for x in re.split(r"[-_ ,./]+", base)] or len(_squash(hits[0])) > len(_squash(hits[1]))):   # unambiguous: exact, or clearly longest
            by_name = hits[0]
            forced = by_name
            ctx += f"\nThe template is fixed to \"{by_name}\" because the file name names it; fill its fields."
    logging.getLogger("lf-capture").info("name rule: name=%r matches=%s forced=%r mode=%s (catalog: %d templates)", base, hits, forced, NAME_HINT_MODE, len(cat))
    result = extractor.extract((d / "doc.pdf").read_bytes(), cat, forced, ctx, lessons=_lessons_text(forced))
    if by_name:
        result["template"] = by_name
        result["notes"] = (f"Template taken from the file name ({by_name}). " + (result.get("notes") or "")).strip()
    result["fields"] = _snap_values(result.get("template"), result.get("fields", {}), templates(lf))
    meta.update(result)
    _write_meta(d / "meta.json", meta)
    return {"id": job_id, **meta}


# ---------- API ------------------------------------------------------
@app.get("/")
def root():
    # served directly (no redirect) so the app works under a reverse-proxy prefix like /lfcapture/
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/me")
def api_me(request: Request):
    u = users.current_user(request)
    creds = users.get_creds(u) if u else None
    return {"lf_username": creds["username"] if creds else None, "has_lf_creds": bool(creds),
            "prefs": users.get_prefs(u) if creds else dict(users.DEFAULT_PREFS),
            "proxy_email": (request.headers.get("x-auth-request-email") or "").strip().lower() or None,  # from oauth2-proxy, if configured
            "inbox_path": os.environ.get("LF_INBOX_PATH", ""), "repository": os.environ.get("LF_REPOSITORY_ID", ""),
            "mailbox_enabled": bool(os.environ.get("MAIL_MAILBOX")) and _svc is not None}


class LfCredsRequest(BaseModel):
    username: str
    password: str


@app.post("/api/settings/lf")
def api_set_lf(req: LfCredsRequest, response: Response):
    try:
        users.set_creds(req.username.strip(), req.password)
    except LaserficheError as e:
        raise HTTPException(400, f"Laserfiche rejected that login: {e}")
    users.set_session(response, req.username)
    return {"ok": True}


class PrefsRequest(BaseModel):
    working_folder: str | None = None
    show_shared: bool | None = None
    scan_only_no_template: bool | None = None
    scan_recursive: bool | None = None
    unsure_below: float | None = None
    after_save: str | None = None
    split_batches: str | None = None
    auto_file: bool | None = None
    auto_file_min: float | None = None


@app.put("/api/settings/prefs")
def api_set_prefs(req: PrefsRequest, request: Request, lf: LaserficheClient = Depends(users.lf_for)):
    u = users.require_user(request)
    data = {k: v for k, v in req.model_dump().items() if v is not None}
    if data.get("working_folder"):
        data["working_folder"] = data["working_folder"].strip().rstrip("\\")
        try:
            folder_entry_id(lf, data["working_folder"])   # must exist and be visible to this user
        except LaserficheError:
            raise HTTPException(400, f"Folder not found in Laserfiche: {data['working_folder']}")
    for k in ("unsure_below", "auto_file_min"):
        if k in data:
            data[k] = min(1.0, max(0.0, float(data[k])))
    if "after_save" in data and data["after_save"] not in ("next", "stay"):
        raise HTTPException(400, "after_save must be next or stay")
    if "split_batches" in data and data["split_batches"] not in ("never", "ask", "always"):
        raise HTTPException(400, "split_batches must be never, ask or always")
    return users.set_prefs(u, data)


@app.delete("/api/settings/lf")
def api_clear_lf(request: Request, response: Response):
    u = users.current_user(request)
    if u:
        users.clear_creds(u)
    users.clear_session(response)
    return {"ok": True}


@app.get("/api/templates")
def api_templates(refresh: bool = False, lf: LaserficheClient = Depends(users.lf_for)):
    try:
        return templates(lf, refresh)
    except LaserficheError as e:
        raise HTTPException(502, str(e))


@app.get("/api/queue")
def api_queue(request: Request):
    """Drop-folder PDFs (shared) plus extracted-but-unsaved jobs: the caller's own first, then shared ones."""
    user = users.current_user(request)
    show_shared = users.get_prefs(user)["show_shared"] if user else True
    pending = sorted(p.name for p in INBOX.glob("*.pdf")) if show_shared else []
    mine, shared = [], []
    for d in sorted(WORK.iterdir(), key=lambda p: p.stat().st_mtime):
        m = d / "meta.json"
        if not d.is_dir() or not m.exists():
            continue
        meta = _read_meta(m)
        if meta is None:
            continue   # being written right now; it will show on the next poll
        reading = "template" not in meta and "notes" not in meta   # extraction still running (or crashed mid-way)
        row = {"id": d.name, "source": meta.get("source"), "template": meta.get("template"), "confidence": meta.get("confidence"),
               "lf_path": meta.get("lf_path"), "owner": meta.get("owner"), "created": meta.get("created"), "reading": reading}
        if not meta.get("owner"):
            if show_shared:
                shared.append(row)
        elif meta.get("owner") == user:
            mine.append(row)
    ready = [r for r in mine + shared if not r["reading"]]
    reading = [r for r in mine + shared if r["reading"]]
    return {"inbox": pending, "jobs": ready, "reading": reading,
            "mine": sum(1 for r in ready if r["owner"]), "shared": sum(1 for r in ready if not r["owner"]) + len(pending)}


@app.post("/api/extract")
async def api_extract(request: Request, file: UploadFile | None = File(None), inbox_name: str | None = Form(None), template: str | None = Form(None), lf: LaserficheClient = Depends(users.lf_for)):
    owner = users.current_user(request) if file is not None else None
    if file is not None:
        data = await file.read()
        name = file.filename or "upload.pdf"
    elif inbox_name:
        src = INBOX / Path(inbox_name).name
        if not src.exists():
            raise HTTPException(404, "File not in inbox")
        data = src.read_bytes()
        name = src.name
        shutil.move(src, WORK / f"{src.name}.claimed")  # keep out of queue while in review
    else:
        raise HTTPException(400, "Send a file or an inbox_name")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "Not a PDF")
    job_id = _new_job(data, name, owner=owner)
    try:
        result = _run_extract(job_id, template or None, lf)
    except Exception as e:  # surface the reason to the UI and drop the half-made job
        shutil.rmtree(WORK / job_id, ignore_errors=True)
        claimed = WORK / f"{name}.claimed"
        if claimed.exists():
            shutil.move(claimed, FAILED / name)
        raise HTTPException(502, f"Extraction failed: {e}")
    if template:
        return result
    return _maybe_auto_save(job_id, result, name, lf)


@app.post("/api/upload")
async def api_upload(request: Request, files: list[UploadFile] = File(...), split: str = Form("0"), lf: LaserficheClient = Depends(users.lf_for)):
    """Many PDFs at once. Each becomes a job read in the background; split=1 also splits scanned batches into separate documents."""
    owner = users.require_user(request)
    ids, skipped = [], []
    for f in files:
        data = await f.read()
        if not data.startswith(b"%PDF"):
            skipped.append(f.filename)
            continue
        job_id = _new_job(data, f.filename or "upload.pdf", owner=owner)
        if split == "1":
            mp = WORK / job_id / "meta.json"
            _write_meta(mp, {**json.loads(mp.read_text()), "split": True})
        ids.append(job_id)
        _bf_queue.put(("job", job_id, owner))
    _bf_state["queued"] += len(ids)
    return {"queued": len(ids), "ids": ids, "skipped": skipped}


class SaveManyRequest(BaseModel):
    ids: list[str]
    folder: str | None = None


@app.post("/api/save_many")
def api_save_many(req: SaveManyRequest, request: Request, lf: LaserficheClient = Depends(users.lf_for)):
    """Save several queued jobs as proposed (template, fields, suggested name). Returns per-job results."""
    user = users.require_user(request)
    out = []
    for job_id in req.ids:
        try:
            d = _job_dir(job_id, user)
            meta = json.loads((d / "meta.json").read_text())
            if not meta.get("template"):
                raise HTTPException(400, "no template chosen")
            name = meta.get("suggested_filename") or str(meta.get("source", "document")).replace(".pdf", "")
            folder = req.folder or (users.get_prefs(user)["working_folder"] or None)
            res = _save(job_id, meta["template"], meta.get("fields", {}), name, lf, folder)
            out.append({"id": job_id, "ok": True, "entry_id": res["entry_id"], "source": meta.get("source")})
        except HTTPException as e:
            out.append({"id": job_id, "ok": False, "error": e.detail})
        except LaserficheError as e:
            out.append({"id": job_id, "ok": False, "error": str(e)})
    return {"results": out, "saved": sum(1 for r in out if r["ok"]), "failed": sum(1 for r in out if not r["ok"])}


@app.post("/api/reextract/{job_id}")
def api_reextract(job_id: str, request: Request, template: str = Form(...), lf: LaserficheClient = Depends(users.lf_for)):
    _job_dir(job_id, users.current_user(request))
    try:
        return _run_extract(job_id, template, lf)
    except Exception as e:
        raise HTTPException(502, f"Extraction failed: {e}")


@app.get("/api/job/{job_id}")
def api_job(job_id: str, request: Request):
    return {"id": job_id, **json.loads((_job_dir(job_id, users.require_user(request)) / "meta.json").read_text())}


@app.get("/api/pdf/{job_id}")
def api_pdf(job_id: str, request: Request):
    return FileResponse(_job_dir(job_id, users.require_user(request)) / "doc.pdf", media_type="application/pdf")


class SaveRequest(BaseModel):
    template: str
    fields: dict[str, list[str]]
    filename: str
    folder: str | None = None   # Laserfiche folder path; defaults to LF_INBOX_PATH


def _save(job_id: str, template: str, fields: dict[str, list[str]], filename: str, lf: LaserficheClient, folder: str | None = None) -> dict:
    d = _job_dir(job_id)
    tdef = next((t for t in templates(lf) if t["name"] == template), None)
    if not tdef:
        raise HTTPException(400, f"Unknown template {template}")
    missing = [f["name"] for f in tdef["fields"] if f["required"] and not [v for v in fields.get(f["name"], []) if v]]
    if missing:
        raise HTTPException(400, "Required fields missing: " + ", ".join(missing))
    _record_corrections(json.loads((d / "meta.json").read_text()), template, fields, getattr(lf, "user", None))
    too_long = [f"{f['name']} ({max(len(v) for v in fields.get(f['name'], []))} chars, max {f['length']})"
                for f in tdef["fields"] if f.get("length") and fields.get(f["name"]) and any(len(v) > f["length"] for v in fields[f["name"]])]
    if too_long:
        raise HTTPException(400, "Too long for Laserfiche: " + "; ".join(too_long))
    meta = json.loads((d / "meta.json").read_text())
    if meta.get("lf_entry_id"):
        entry_id = int(meta["lf_entry_id"])
        refused = lf.update_document(entry_id, template, fields, meta.get("lf_template"))
        _bf_seen.discard(entry_id)
        if refused:
            raise HTTPException(400, "Saved, but Laserfiche refused these fields (check the values): " + ", ".join(refused))
    else:
        try:
            parent = folder_entry_id(lf, folder)
        except LaserficheError as e:
            raise HTTPException(400, f"Destination folder not found: {folder or os.environ.get('LF_INBOX_PATH')} ({e})")
        entry_id = lf.import_pdf(parent, filename, (d / "doc.pdf").read_bytes(), template, fields)
    src = meta.get("source", "")
    claimed = WORK / f"{src}.claimed"
    if claimed.exists():
        shutil.move(claimed, DONE / src)
    shutil.rmtree(d, ignore_errors=True)
    return {"entry_id": entry_id, "saved": True}


@app.post("/api/save/{job_id}")
def api_save(job_id: str, req: SaveRequest, request: Request, lf: LaserficheClient = Depends(users.lf_for)):
    _job_dir(job_id, users.current_user(request))
    try:
        return _save(job_id, req.template, req.fields, req.filename, lf, req.folder)
    except LaserficheError as e:
        raise HTTPException(502, str(e))


@app.delete("/api/job/{job_id}")
def api_discard(job_id: str, request: Request):
    d = _job_dir(job_id, users.require_user(request))
    meta = json.loads((d / "meta.json").read_text())
    if meta.get("lf_entry_id"):
        _bf_seen.discard(int(meta["lf_entry_id"]))
    claimed = WORK / f"{meta.get('source', '')}.claimed"
    if claimed.exists():
        shutil.move(claimed, FAILED / meta["source"])
    shutil.rmtree(d, ignore_errors=True)
    return {"discarded": True}


def _maybe_auto_save(job_id: str, result: dict, name: str, lf: LaserficheClient) -> dict:
    if AUTO_SAVE and result["confidence"] >= AUTO_SAVE and lf is not None:
        try:
            saved = _save(job_id, result["template"], result["fields"], result["suggested_filename"] or name, lf)
            return {**result, "auto_saved": True, **saved}
        except Exception as e:
            result["notes"] = f"Auto-save failed: {e}. " + result.get("notes", "")
            _write_meta(WORK / job_id / "meta.json", {**json.loads((WORK / job_id / "meta.json").read_text()), "notes": result["notes"]})
    return result


def _from_mailbox(name: str, pdf: bytes, context: str) -> None:
    """Mail poller callback: extract now so the queue shows a finished job, auto-save if allowed."""
    job_id = _new_job(pdf, name, context)
    try:
        result = _run_extract(job_id, None, _svc)
        _maybe_auto_save(job_id, result, name, _svc)
    except Exception as e:  # leave the job in the queue for a human; note the error
        m = WORK / job_id / "meta.json"
        _write_meta(m, {**json.loads(m.read_text()), "template": None, "fields": {}, "confidence": 0,
                                 "summary": "", "suggested_filename": "", "notes": f"Automatic read failed: {e}"})


# ---------- corrections memory: what humans changed before saving -> lessons for future reads ----------
CORRECTIONS = WORK / "corrections.jsonl"


def _norm(v) -> list[str]:
    vals = v if isinstance(v, list) else ([v] if v not in (None, "") else [])
    return [str(x).strip() for x in vals if str(x).strip()]


def _record_corrections(meta: dict, template: str, fields: dict[str, list[str]], user: str | None) -> None:
    """Compare what Claude proposed (meta) with what the human saved; append the differences."""
    if not meta.get("template") and not meta.get("fields"):
        return  # nothing was proposed (e.g. failed read)
    rows = []
    base = {"at": time.time(), "user": user, "source": meta.get("source"), "template": template}
    if meta.get("template") and meta["template"] != template:
        rows.append({**base, "kind": "template", "proposed": meta["template"], "final": template})
    proposed = meta.get("fields") or {}
    for name in set(proposed) | set(fields):
        a, b = _norm(proposed.get(name)), _norm(fields.get(name))
        if a == b:
            continue
        if not a and not b:
            continue
        rows.append({**base, "kind": "field", "field": name, "proposed": a, "final": b})
    if rows:
        with _lock:
            with CORRECTIONS.open("a") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")


def _load_corrections(limit: int = 400) -> list[dict]:
    if not CORRECTIONS.exists():
        return []
    lines = CORRECTIONS.read_text().splitlines()[-limit:]
    return [json.loads(l) for l in lines if l.strip()]


def _lessons_text(template: str | None = None, max_lines: int = 40) -> str:
    """Recent human corrections, most recent first, de-duplicated per (template, field, proposed->final)."""
    rows = list(reversed(_load_corrections()))
    if template:
        rows = [r for r in rows if r.get("template") == template or (r["kind"] == "template" and r.get("proposed") == template)]
    seen, out = set(), []
    for r in rows:
        if r["kind"] == "template":
            key = ("t", r["proposed"], r["final"])
            line = f'- A document read as "{r["proposed"]}" was actually "{r["final"]}" (document "{r.get("source")}").'
        else:
            key = ("f", r["template"], r["field"], "|".join(r["proposed"]), "|".join(r["final"]))
            if r["proposed"] and r["final"]:
                line = f'- {r["template"]} · {r["field"]}: read as "{" | ".join(r["proposed"])}", correct was "{" | ".join(r["final"])}" (document "{r.get("source")}").'
            elif r["final"]:
                line = f'- {r["template"]} · {r["field"]}: was left empty but should have been "{" | ".join(r["final"])}" (document "{r.get("source")}").'
            else:
                line = f'- {r["template"]} · {r["field"]}: read as "{" | ".join(r["proposed"])}" but the reviewer cleared it — it was not on the document.'
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= max_lines:
            break
    if not out:
        return ""
    return ("Lessons from past human corrections on this kind of document (most recent first). "
            "Apply the patterns they show — which printed value belongs in which field, what to leave blank — not the literal values:\n" + "\n".join(out))


@app.get("/api/corrections")
def api_corrections(request: Request, limit: int = 50):
    users.require_user(request)
    rows = list(reversed(_load_corrections()))[:limit]
    return {"items": rows, "total": len(_load_corrections(100000))}


# ---------- activity log: things filed without review ----------
ACTIVITY = WORK / "activity.jsonl"


def _log_activity(user: str, **row) -> None:
    with _lock:
        with ACTIVITY.open("a") as f:
            f.write(json.dumps({"user": user, "at": time.time(), **row}) + "\n")


@app.get("/api/activity")
def api_activity(request: Request, limit: int = 30):
    user = users.require_user(request)
    if not ACTIVITY.exists():
        return {"items": []}
    rows = [json.loads(l) for l in ACTIVITY.read_text().splitlines() if l.strip()]
    mine = [r for r in rows if r.get("user") == user][-limit:]
    return {"items": list(reversed(mine))}


# ---------- in-Laserfiche flow: Workflow / Business Process calls this, results are written straight onto the entries ----------
WF_TOKEN = os.environ.get("LF_WORKFLOW_TOKEN", "")
TAG_PROPOSED = os.environ.get("LF_TAG_PROPOSED", "")   # blank = don't tag successful reads
TAG_UNSURE = os.environ.get("LF_TAG_UNSURE", "AI-Unsure")
TAG_FAILED = os.environ.get("LF_TAG_FAILED", "AI-Failed")
DIRECT_UNSURE_BELOW = float(os.environ.get("DIRECT_UNSURE_BELOW", "0.8"))
# Templates that are placeholders (e.g. what the e-mail archive agent stamps on everything): treated as "no template yet"
GENERIC_TEMPLATES = {t.strip().lower() for t in os.environ.get("LF_GENERIC_TEMPLATES", "Misc. Document").split(",") if t.strip()}
# Optional: a field (defined in the repository, need not be on the template) that receives Claude's summary/confidence/notes
NOTES_FIELD = os.environ.get("LF_NOTES_FIELD", "").strip()
# Fields the unattended (workflow) flow never fills by itself — it only keeps a value that was already there
DIRECT_SKIP_FIELDS = {f.strip().lower() for f in os.environ.get("DIRECT_SKIP_FIELDS", "").split(",") if f.strip()}


def _is_generic(template: str | None) -> bool:
    return bool(template) and template.strip().lower() in GENERIC_TEMPLATES


class LfReadRequest(BaseModel):
    entry_ids: list[int] = []
    entry_id: int | None = None          # convenience for Workflow (one token)
    token: str | None = None             # alternative to the Authorization header (Workflow can't always set headers)
    user: str | None = None              # LF username of the person who started it (Workflow %(Initiator)); optional
    creator: str | None = None           # fallback identity (Workflow %(Creator)) for folder-triggered runs
    mode: str = "keep"                   # keep | overwrite (for entries that already have a template)
    recursive: bool = False              # when an entry is a folder: include subfolders


def _wf_client(user: str | None) -> tuple[LaserficheClient, str]:
    """Client to act as: the named user's stored login if they have one, else the service account."""
    if user:
        k = users.resolve_username(user)
        if k:
            return users.client_for(k), users.get_creds(k)["username"]
    if _svc is not None:
        return _svc, "service"
    raise HTTPException(400, "No usable Laserfiche login: the user has not signed in to LF Capture and no service account is configured")


def _lenient_json(raw: bytes) -> dict:
    """Workflow tokens like %(Initiator) expand to 'domain\\user' unescaped; fix stray backslashes and parse."""
    import re
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        fixed = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", text)
        return json.loads(fixed)


@app.post("/api/lf/read")
async def api_lf_read(request: Request):
    try:
        req = LfReadRequest(**_lenient_json(await request.body()))
    except Exception as e:
        raise HTTPException(422, f"Could not read request body: {e}")
    auth = request.headers.get("authorization", "")
    if not WF_TOKEN or (auth != f"Bearer {WF_TOKEN}" and (req.token or "") != WF_TOKEN):
        raise HTTPException(401, "bad or missing LF_WORKFLOW_TOKEN")
    lf, who = _wf_client((req.user or "").strip() or (req.creator or "").strip() or None)
    ids = list(req.entry_ids) + ([req.entry_id] if req.entry_id else [])
    if not ids:
        raise HTTPException(400, "entry_id or entry_ids required")
    # expand folders into their documents
    docs = []
    for eid in ids:
        e = lf.get_entry(eid)
        if e.get("entryType") == "Folder":
            docs += [d["id"] for d in lf.list_documents(e.get("fullPath"), req.recursive, only_no_template=False, limit=1000)]
        else:
            docs.append(eid)
    mode = "overwrite" if req.mode == "overwrite" else "keep"
    n = 0
    for eid in docs:
        _bf_queue.put(("direct", eid, who, mode))
        n += 1
    _bf_state["queued"] += n
    return {"queued": n, "acting_as": who, "mode": mode}


def _notify_failure(who: str, entry: dict, error: str) -> None:
    """Email the person who started the read when it fails after Workflow has already returned."""
    host = os.environ.get("SMTP_HOST")
    domain = os.environ.get("NOTIFY_DOMAIN")
    if not host or not domain or who == "service":
        return
    import smtplib
    from email.message import EmailMessage
    short = who.split("\\")[-1].split("@")[0]
    to = f"{short}@{domain}"
    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM", f"lfcapture@{domain}")
    msg["To"] = to
    msg["Subject"] = f"LF Capture could not read: {entry.get('name')}"
    msg.set_content(
        f"LF Capture was asked to read this document but could not finish.\n\n"
        f"Document: {entry.get('name')}\nFolder: {entry.get('fullPath')}\nEntry ID: {entry.get('id')}\n\n"
        f"Reason: {error}\n\nThe document has been tagged {TAG_FAILED}. Fill in its metadata by hand, or fix the cause and start the business process again."
    )
    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "25")), timeout=15) as sm:
            if os.environ.get("SMTP_STARTTLS", "0") == "1":
                sm.starttls()
            if os.environ.get("SMTP_USER"):
                sm.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            sm.send_message(msg)
    except Exception as ex:
        logging.getLogger("lf-capture").warning("failure e-mail to %s not sent: %s", to, ex)


class LfLearnRequest(BaseModel):
    entry_ids: list[int] = []
    entry_id: int | None = None
    token: str | None = None
    user: str | None = None


def _last_ai_write(entry_id: int) -> dict | None:
    if not ACTIVITY.exists():
        return None
    for l in reversed(ACTIVITY.read_text().splitlines()):
        if not l.strip():
            continue
        r = json.loads(l)
        if r.get("kind") == "lf_direct" and r.get("entry_id") == entry_id and "ai_fields" in r:
            return r
    return None


@app.post("/api/lf/learn")
async def api_lf_learn(request: Request):
    """Workflow 'Learn from this document': compare what a human left on the entry with what the tool wrote, record the differences as lessons."""
    try:
        req = LfLearnRequest(**_lenient_json(await request.body()))
    except Exception as e:
        raise HTTPException(422, f"Could not read request body: {e}")
    auth = request.headers.get("authorization", "")
    if not WF_TOKEN or (auth != f"Bearer {WF_TOKEN}" and (req.token or "") != WF_TOKEN):
        raise HTTPException(401, "bad or missing LF_WORKFLOW_TOKEN")
    lf, who = _wf_client((req.user or "").strip() or None)
    ids = list(req.entry_ids) + ([req.entry_id] if req.entry_id else [])
    learned, skipped = 0, []
    for eid in ids:
        prior = _last_ai_write(eid)
        if not prior:
            skipped.append({"entry_id": eid, "reason": "no AI write on record for this entry"})
            continue
        entry = lf.get_entry(eid)
        now_fields = {k: v for k, v in lf.entry_fields(eid).items() if k != NOTES_FIELD}
        now_tpl = entry.get("templateName") or ""
        before = len(_load_corrections(100000))
        _record_corrections({"template": prior["ai_template"], "fields": prior["ai_fields"], "source": entry.get("name")}, now_tpl, now_fields, who)
        n = len(_load_corrections(100000)) - before
        learned += n
        try:
            lf.set_tags(eid, remove=[TAG_UNSURE, TAG_FAILED])   # a human has looked at it
        except LaserficheError:
            pass
        _log_activity(who, kind="lf_learn", source=entry.get("name"), entry_id=eid, template=now_tpl, corrections=n)
    return {"learned": learned, "entries": len(ids), "skipped": skipped}


def _direct_one(entry_id: int, who: str, mode: str) -> None:
    """Read one existing entry and write the result straight back into Laserfiche, tagged for human review."""
    lf = _svc if who == "service" else users.client_for(who)
    entry = lf.get_entry(entry_id)
    if entry.get("entryType") == "Folder":
        return
    pdf = lf.export_pdf(entry_id, entry)
    existing = lf.entry_fields(entry_id)
    current_tpl = entry.get("templateName") or None
    generic = _is_generic(current_tpl)
    if mode == "overwrite" or not current_tpl or generic:
        ctx = (f"This document is already in Laserfiche at: {entry.get('fullPath')}\n"
               + (f"It carries the placeholder template '{current_tpl}' (assigned automatically on import, not a real classification); choose the correct template from the document.\n" if generic
                  else f"Currently filed under template: {current_tpl or 'none'} — may be wrong; choose from the document.\n")
               + f"Existing field values (may be wrong): {json.dumps(existing) if existing else 'none'}")
        forced = None
    else:
        ctx = (f"This document is already in Laserfiche at: {entry.get('fullPath')}\nCurrent template: {current_tpl}\n"
               f"Existing field values (kept; fill the EMPTY fields): {json.dumps(existing) if existing else 'none'}")
        forced = current_tpl
    job_id = _new_job(pdf, f"LF {entry_id}: {entry.get('name')}", ctx, owner=None)
    try:
        result = _run_extract(job_id, forced, lf)
        fields = result["fields"]
        template = result.get("template") or current_tpl
        if not template:
            raise RuntimeError("no template could be determined")
        tdef = next((t for t in templates(lf) if t["name"] == template), None)
        tnames = {f["name"] for f in tdef["fields"]} if tdef else set(fields)
        if mode != "overwrite" and current_tpl and not generic:
            fields = {**fields, **{k: v for k, v in existing.items() if v}}          # keep: existing values win
        else:
            # re-classified: carry over existing values only where the new template has that field and Claude left it empty
            for k, v in existing.items():
                if v and k in tnames and not fields.get(k):
                    fields[k] = v
        # unattended: never invent values for fields on the skip list
        for k in list(fields):
            if k.lower() in DIRECT_SKIP_FIELDS and not existing.get(k):
                fields.pop(k)
        # respect field lengths so the write cannot fail on that
        if tdef:
            for f in tdef["fields"]:
                if f.get("length") and fields.get(f["name"]):
                    fields[f["name"]] = [v[: f["length"]] for v in fields[f["name"]]]
        # dropdown values must be in the list; drop anything that is not rather than sink the whole write
        if tdef:
            for f in tdef["fields"]:
                if f.get("list") and fields.get(f["name"]):
                    ok = [v for v in fields[f["name"]] if v in f["list"]]
                    if len(ok) != len(fields[f["name"]]):
                        logging.getLogger("lf-capture").warning("%s: dropping %s value(s) not in list: %s", entry_id, f["name"], [v for v in fields[f["name"]] if v not in f["list"]])
                        result["notes"] = (f"{f['name']}: proposed value not in the list, left blank. " + (result.get("notes") or "")).strip()
                    fields[f["name"]] = ok
        unsure = result["confidence"] < DIRECT_UNSURE_BELOW
        if NOTES_FIELD:
            note = f"{template} · {round(result['confidence'] * 100)}% confident" + (" · UNSURE" if unsure else "")
            if result.get("summary"):
                note += f" — {result['summary']}"
            if result.get("notes"):
                note += f" | {result['notes']}"
            fields[NOTES_FIELD] = [note[:1000]]
        refused = lf.update_document(entry_id, template, fields, current_tpl)
        if refused:
            msg = "Laserfiche refused: " + ", ".join(refused)
            logging.getLogger("lf-capture").warning("%s: %s", entry_id, msg)
            if NOTES_FIELD and NOTES_FIELD not in refused:
                fields[NOTES_FIELD] = [(fields.get(NOTES_FIELD, [""])[0] + " | " + msg)[:1000]]
                try:
                    lf.set_fields(entry_id, {k: v for k, v in fields.items() if k not in refused})
                except LaserficheError:
                    pass
        try:
            add = ([TAG_PROPOSED] if TAG_PROPOSED else []) + ([TAG_UNSURE] if unsure else [])
            lf.set_tags(entry_id, add=add, remove=[TAG_FAILED] + ([] if unsure else [TAG_UNSURE]))
        except LaserficheError as e:
            logging.getLogger("lf-capture").warning("tagging %s failed (do the tags exist in the repository?): %s", entry_id, e)
        _log_activity(who, kind="lf_direct", source=entry.get("name"), template=template, entry_id=entry_id, folder=entry.get("fullPath"),
                      confidence=result["confidence"], name=entry.get("name"), notes=result.get("notes", ""),
                      ai_template=template, ai_fields={k: v for k, v in fields.items() if k != NOTES_FIELD})
    except Exception as e:
        try:
            lf.set_tags(entry_id, add=[TAG_FAILED])
        except Exception:
            pass
        _log_activity(who, kind="lf_direct_failed", source=entry.get("name"), entry_id=entry_id, folder=entry.get("fullPath"), error=str(e))
        _notify_failure(who, entry, str(e))
        raise
    finally:
        shutil.rmtree(WORK / job_id, ignore_errors=True)


# ---------- backfill: documents already in Laserfiche ----------
_bf_queue: "queue.Queue[tuple]" = queue.Queue()
_bf_state = {"queued": 0, "done": 0, "failed": 0, "errors": []}
_bf_seen: set[int] = set()


class ScanRequest(BaseModel):
    folder: str
    recursive: bool = False
    only_no_template: bool = True
    limit: int = 500


@app.post("/api/backfill/scan")
def api_backfill_scan(req: ScanRequest, lf: LaserficheClient = Depends(users.lf_for)):
    try:
        docs = lf.list_documents(req.folder, req.recursive, req.only_no_template, req.limit, generic=GENERIC_TEMPLATES)
    except LaserficheError as e:
        raise HTTPException(502, str(e))
    return {"count": len(docs), "documents": [
        {"id": d["id"], "name": d.get("name"), "path": d.get("fullPath"), "template": d.get("templateName"),
         "pages": d.get("pageCount"), "queued": d["id"] in _bf_seen} for d in docs]}


@app.get("/api/lf/folders")
def api_lf_folders(path: str = "\\", lf: LaserficheClient = Depends(users.lf_for)):
    try:
        return lf.list_folders(path)
    except LaserficheError as e:
        raise HTTPException(502, str(e))


class QueueRequest(BaseModel):
    entry_ids: list[int]
    mode: str = "keep"   # keep = keep existing template, fill empty fields only; overwrite = re-read everything


@app.post("/api/backfill/queue")
def api_backfill_queue(req: QueueRequest, request: Request):
    email = users.require_user(request)
    users.client_for(email)  # fail fast if no credentials
    mode = "overwrite" if req.mode == "overwrite" else "keep"
    n = 0
    for eid in req.entry_ids:
        if eid in _bf_seen:
            continue
        _bf_seen.add(eid)
        _bf_queue.put(("lf", eid, email, mode))
        n += 1
    _bf_state["queued"] += n
    return {"queued": n}


@app.get("/api/backfill/status")
def api_backfill_status():
    return {**_bf_state, "pending": _bf_queue.qsize(), "errors": _bf_state["errors"][-10:]}


def _backfill_one(entry_id: int, email: str, mode: str = "keep") -> None:
    lf = users.client_for(email)
    entry = lf.get_entry(entry_id)
    pdf = lf.export_pdf(entry_id, entry)
    existing = lf.entry_fields(entry_id)
    current_tpl = entry.get("templateName") or None
    generic = _is_generic(current_tpl)
    if mode == "overwrite" or not current_tpl or generic:
        ctx = (f"This document is ALREADY in Laserfiche at: {entry.get('fullPath')}\n"
               + (f"It carries the placeholder template '{current_tpl}' (assigned automatically on import, not a real classification); choose the correct template from the document itself.\n" if generic
                  else f"It is currently filed under template: {current_tpl or 'none'} — this MAY be wrong; choose the correct template from the document itself.\n")
               + f"Existing field values (may be wrong or outdated; read the document and give the correct values): "
               f"{json.dumps(existing) if existing else 'none'}")
        forced = None
    else:
        ctx = (f"This document is ALREADY in Laserfiche at: {entry.get('fullPath')}\n"
               f"Current template: {current_tpl}\n"
               f"Existing field values (these will be kept; your job is to fill the EMPTY fields): "
               f"{json.dumps(existing) if existing else 'none'}")
        forced = current_tpl
    job_id = _new_job(pdf, f"LF {entry_id}: {entry.get('name')}", ctx, owner=email)
    m = WORK / job_id / "meta.json"
    _write_meta(m, {**json.loads(m.read_text()), "lf_entry_id": entry_id, "lf_path": entry.get("fullPath"),
                             "lf_template": current_tpl, "lf_fields": existing, "owner": email, "mode": mode})
    try:
        result = _run_extract(job_id, forced, lf)
        if mode != "overwrite" and current_tpl and not generic:
            # keep mode: values already in Laserfiche win; Claude only supplies the blanks
            merged = {**result["fields"], **{k: v for k, v in existing.items() if v}}
            meta = json.loads(m.read_text()); meta["fields"] = merged; _write_meta(m, meta)
    except Exception as e:
        _write_meta(m, {**json.loads(m.read_text()), "template": entry.get("templateName"), "fields": existing,
                                 "confidence": 0, "summary": "", "suggested_filename": "", "notes": f"Automatic read failed: {e}"})


def _pdf_page_count(pdf: bytes) -> int:
    try:
        import pymupdf
        return len(pymupdf.open(stream=pdf, filetype="pdf"))
    except Exception:
        return 1


def _split_pdf(pdf: bytes, first: int, last: int) -> bytes:
    import pymupdf
    src = pymupdf.open(stream=pdf, filetype="pdf")
    dst = pymupdf.open()
    dst.insert_pdf(src, from_page=first - 1, to_page=last - 1)
    return dst.tobytes()


def _process_upload(job_id: str, user: str) -> None:
    """Background read of an uploaded job: split scanned batches, extract, auto-file if the user wants it."""
    lf = users.client_for(user)
    prefs = users.get_prefs(user)
    d = WORK / job_id
    meta = json.loads((d / "meta.json").read_text())
    pdf = (d / "doc.pdf").read_bytes()
    pages = _pdf_page_count(pdf)
    if meta.get("split") and pages > 1 and not meta.get("split_from"):
        segs = extractor.detect_segments(pdf, pages, templates(lf))
        if len(segs) > 1:
            base = str(meta.get("source", "upload.pdf")).replace(".pdf", "")
            for i, sg in enumerate(segs, 1):
                part = _split_pdf(pdf, sg["first_page"], sg["last_page"])
                rng = f"p{sg['first_page']}" + (f"-{sg['last_page']}" if sg["last_page"] != sg["first_page"] else "")
                child = _new_job(part, f"{base} [{rng}]", context=f"Split from a scanned batch '{base}' ({pages} pages); this part: {sg.get('what', '')}", owner=user)
                cm = WORK / child / "meta.json"
                _write_meta(cm, {**json.loads(cm.read_text()), "split_from": job_id, "part": i, "parts": len(segs)})
                _bf_queue.put(("job", child, user))
            shutil.rmtree(d, ignore_errors=True)
            return
    result = _run_extract(job_id, None, lf)
    if prefs.get("auto_file") and result["confidence"] >= float(prefs.get("auto_file_min", 0.9)) and result.get("template"):
        try:
            name = result.get("suggested_filename") or str(meta.get("source", "document")).replace(".pdf", "")
            saved = _save(job_id, result["template"], result["fields"], name, lf, prefs.get("working_folder") or None)
            _log_activity(user, kind="auto_filed", source=meta.get("source"), template=result["template"], entry_id=saved["entry_id"],
                          folder=prefs.get("working_folder") or os.environ.get("LF_INBOX_PATH"), confidence=result["confidence"], name=name)
        except Exception as e:  # leave it in the queue with the reason
            mp = WORK / job_id / "meta.json"
            if mp.exists():
                mm = json.loads(mp.read_text()); mm["notes"] = f"Not auto-filed: {getattr(e, 'detail', e)}. " + (mm.get("notes") or ""); _write_meta(mp, mm)


def _backfill_worker():
    while True:
        item = _bf_queue.get()
        try:
            if item[0] == "job":
                _, job_id, user = item
                _process_upload(job_id, user)
            elif item[0] == "direct":
                _, eid, who, mode = item
                _direct_one(eid, who, mode)
            else:
                _, eid, email, mode = item
                _backfill_one(eid, email, mode)
            _bf_state["done"] += 1
        except Exception as e:
            _bf_state["failed"] += 1
            _bf_state["errors"].append(f"{item[1]}: {e}")
            if item[0] == "lf":
                _bf_seen.discard(item[1])
            elif item[0] == "job":  # leave a reviewable stub so the upload isn't silently lost
                mp = WORK / item[1] / "meta.json"
                if mp.exists():
                    mm = json.loads(mp.read_text()); mm.update({"template": None, "fields": {}, "confidence": 0, "summary": "", "suggested_filename": "", "notes": f"Automatic read failed: {e}"}); _write_meta(mp, mm)
        finally:
            _bf_queue.task_done()


@app.on_event("startup")
def _restore_seen():
    for d in WORK.iterdir():
        mp = d / "meta.json"
        if d.is_dir() and mp.exists():
            try:
                eid = json.loads(mp.read_text()).get("lf_entry_id")
                if eid:
                    _bf_seen.add(int(eid))
            except Exception:
                pass


@app.on_event("startup")
def _start_mail():
    if _svc is not None:
        mailbox.start_if_configured(_from_mailbox)
    elif os.environ.get("MAIL_MAILBOX"):
        logging.getLogger("lf-capture").warning("MAIL_MAILBOX is set but LF_USERNAME/LF_PASSWORD (service account) is not; mailbox capture disabled")
    for i in range(int(os.environ.get("BACKFILL_WORKERS", "3"))):
        threading.Thread(target=_backfill_worker, daemon=True, name=f"backfill-{i}").start()


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

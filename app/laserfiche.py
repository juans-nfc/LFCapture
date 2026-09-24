"""Laserfiche Repository API v2 client for a self-hosted API Server.

Auth (self-hosted): POST {base}/v2/Repositories/{repo}/Token
  form: grant_type=password&username=...&password=...  -> {access_token, expires_in}
Import: POST {base}/v2/Repositories/{repo}/Entries/{parentId}/Folder/Import
  multipart: file=<pdf>, request=<json {name, autoRename, pdfOptions, metadata{templateName, fields[{name, values[]}]}}>
Ref: developer.laserfiche.com -> Self-Hosted API Server / Import Documents (v2)
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx


class LaserficheError(RuntimeError):
    pass


class LaserficheClient:
    def __init__(self) -> None:
        self.base = os.environ["LF_BASE_URL"].rstrip("/")  # e.g. https://lf.northernfruit.com/LFRepositoryAPI
        self.repo = os.environ["LF_REPOSITORY_ID"]
        self.user = os.environ["LF_USERNAME"]
        self.password = os.environ["LF_PASSWORD"]
        self.generate_pages = os.environ.get("LF_GENERATE_PAGES", "1") != "0"
        self._token: str | None = None
        self._expires_at = 0.0
        self._http = httpx.Client(timeout=120, verify=os.environ.get("LF_VERIFY_TLS", "1") != "0")

    @property
    def _repo_url(self) -> str:
        return f"{self.base}/v2/Repositories/{self.repo}"

    # ---- auth -------------------------------------------------------
    def _login(self) -> None:
        r = self._http.post(
            f"{self._repo_url}/Token",
            data={"grant_type": "password", "username": self.user, "password": self.password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code >= 400:
            raise LaserficheError(f"Login failed ({r.status_code}): {r.text[:300]}")
        body = r.json()
        self._token = body.get("access_token")
        # tokens also die on idle (IdleSessionTimeout); a 401 anywhere triggers re-login.
        self._expires_at = time.time() + int(body.get("expires_in", 900)) - 60
        if not self._token:
            raise LaserficheError("Login returned no access_token")

    def _headers(self) -> dict[str, str]:
        if not self._token or time.time() >= self._expires_at:
            self._login()
        return {"Authorization": f"Bearer {self._token}"}

    def _req(self, method: str, path: str, retry: bool = True, **kw) -> Any:
        r = self._http.request(method, f"{self._repo_url}{path}", headers=self._headers(), **kw)
        if r.status_code == 401 and retry:
            self._token = None
            return self._req(method, path, retry=False, **kw)
        if r.status_code >= 400:
            raise LaserficheError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else None

    # ---- metadata catalog --------------------------------------------
    def templates(self) -> list[dict]:
        """All templates with their field definitions, shaped for the extractor."""
        allow = {t.strip() for t in os.environ.get("LF_TEMPLATE_ALLOWLIST", "").split(",") if t.strip()}
        out = []
        for t in self._req("GET", "/TemplateDefinitions").get("value", []):
            if allow and t["name"] not in allow:
                continue
            fields = self._req("GET", f"/TemplateDefinitions/{t['id']}/FieldDefinitions").get("value", [])
            out.append(
                {
                    "id": t["id"],
                    "name": t["name"],
                    "description": t.get("description") or "",
                    "fields": [
                        {
                            "name": f["name"],
                            "type": f.get("fieldType", "String"),
                            "required": bool(f.get("isRequired")),
                            "multi": bool(f.get("isMultiValue")),
                            "list": list(f.get("listValues") or []),
                            "length": f.get("length"),
                            "description": f.get("description") or "",
                        }
                        for f in fields
                    ],
                }
            )
        return out

    # ---- entries -----------------------------------------------------
    def entry_id_by_path(self, full_path: str) -> int:
        e = self._req("GET", "/Entries/ByPath", params={"fullPath": full_path, "fallbackToClosestAncestor": "false"})
        entry = e.get("entry") or e
        if not entry or "id" not in entry:
            raise LaserficheError(f"Folder not found: {full_path}")
        return int(entry["id"])

    def import_pdf(self, parent_id: int, file_name: str, pdf: bytes, template: str, fields: dict[str, list[str]]) -> int:
        """Create a document with template + fields under parent_id. Returns new entry id."""
        if not file_name.lower().endswith(".pdf"):
            file_name += ".pdf"
        lf_fields = [
            {"name": name, "values": [v for v in vals if v not in (None, "")]}
            for name, vals in fields.items()
        ]
        lf_fields = [f for f in lf_fields if f["values"]]
        body = {
            "name": file_name[:-4],
            "autoRename": True,
            "pdfOptions": {"generatePages": self.generate_pages, "generatePagesImageType": "StandardColor", "keepPdfAfterImport": True},
            "metadata": {"templateName": template, "fields": lf_fields},
        }
        r = self._http.post(
            f"{self._repo_url}/Entries/{parent_id}/Folder/Import",
            headers=self._headers(),
            files={
                "file": (file_name, pdf, "application/pdf"),
                "request": (None, json.dumps(body), "application/json"),
            },
        )
        if r.status_code == 401:
            self._token = None
            r = self._http.post(f"{self._repo_url}/Entries/{parent_id}/Folder/Import", headers=self._headers(),
                                files={"file": (file_name, pdf, "application/pdf"), "request": (None, json.dumps(body), "application/json")})
        if r.status_code >= 400:
            raise LaserficheError(f"Import failed ({r.status_code}): {r.text[:500]}")
        return int(r.json().get("id") or 0)

    # ---- existing documents (backfill) --------------------------------
    _DOC_SELECT = "id,name,fullPath,folderPath,entryType,templateName,templateId,extension,mimeType,pageCount,isElectronicDocument"

    def list_documents(self, folder_path: str, recursive: bool = False, only_no_template: bool = True, limit: int = 500) -> list[dict]:
        """Documents under a folder (optionally recursive). Uses Folder/Children with OData paging."""
        root_id = self.entry_id_by_path(folder_path)
        out: list[dict] = []
        pending = [root_id]
        while pending and len(out) < limit:
            fid = pending.pop(0)
            url = f"{self._repo_url}/Entries/{fid}/Folder/Children"
            params: dict | None = None  # default listing; this API Server build rejects $select/$top here
            while url and len(out) < limit:
                r = self._http.get(url, headers=self._headers(), params=params)
                if r.status_code == 401:
                    self._token = None
                    r = self._http.get(url, headers=self._headers(), params=params)
                if r.status_code >= 400:
                    raise LaserficheError(f"Children {fid} -> {r.status_code}: {r.text[:300]}")
                data = r.json()
                for e in data.get("value", []):
                    if e.get("entryType") == "Folder":
                        if recursive:
                            pending.append(e["id"])
                    elif e.get("entryType") == "Document":
                        if only_no_template and (e.get("templateName") or e.get("templateId")):
                            continue
                        out.append(e)
                url, params = data.get("@odata.nextLink"), None
        return out

    def get_entry(self, entry_id: int) -> dict:
        return self._req("GET", f"/Entries/{entry_id}")

    def entry_fields(self, entry_id: int) -> dict[str, list[str]]:
        vals = self._req("GET", f"/Entries/{entry_id}/Fields").get("value", [])
        return {f["name"]: [v for v in (f.get("values") or []) if v not in (None, "")] for f in vals}

    def export_pdf(self, entry_id: int, entry: dict | None = None) -> bytes:
        """PDF bytes for an existing document: the edoc if it is a PDF, otherwise the LF pages rendered to PDF."""
        entry = entry or self.get_entry(entry_id)
        has_edoc = entry.get("isElectronicDocument")
        ext = (entry.get("extension") or "").lower()
        mime = (entry.get("mimeType") or "").lower()
        # Try the edoc first when it is (or may be) a PDF; fall back to rendering the LF pages to PDF.
        attempts = []
        if has_edoc is not False and (ext in ("pdf", "") and mime in ("application/pdf", "")):
            attempts.append({"part": "Edoc"})
        attempts.append({"part": "Image", "imageOptions": {"format": "PDF", "includeAnnotations": False}})
        last = ""
        for body in attempts:
            try:
                link = self._req("POST", f"/Entries/{entry_id}/Export", json=body).get("value")
                if not link:
                    last = "no download link"; continue
                r = self._http.get(link, headers=self._headers(), follow_redirects=True)
                if r.status_code >= 400:
                    last = f"download {r.status_code}"; continue
                if r.content.startswith(b"%PDF"):
                    return r.content
                last = f"not a PDF ({r.headers.get('content-type')})"
            except LaserficheError as e:
                last = str(e)
        raise LaserficheError(f"Export {entry_id} failed: {last}")

    def set_template(self, entry_id: int, template: str) -> None:
        # v2: PUT /Entries/{id}/Template  {"templateName": ...}
        self._req("PUT", f"/Entries/{entry_id}/Template", json={"templateName": template})

    def set_fields(self, entry_id: int, fields: dict[str, list[str]]) -> None:
        # PUT replaces the full field set; caller passes every value that should remain.
        body = {"fields": [{"name": n, "values": [v for v in vals if v not in (None, "")]} for n, vals in fields.items()]}
        body["fields"] = [f for f in body["fields"] if f["values"]]
        self._req("PUT", f"/Entries/{entry_id}/Fields", json=body)

    def update_document(self, entry_id: int, template: str, fields: dict[str, list[str]], current_template: str | None) -> None:
        if template != (current_template or ""):
            self.set_template(entry_id, template)
        self.set_fields(entry_id, fields)

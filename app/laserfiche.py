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

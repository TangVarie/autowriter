"""Minimal Feishu Bitable API client.

Scope kept intentionally narrow — only the endpoints the generation server needs:
  - tenant_access_token caching
  - get single record
  - list records with filter
  - batch create records
  - update a single record

Docs (open.feishu.cn):
  https://open.feishu.cn/document/server-docs/docs/bitable-v1/bitable-overview
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests

from . import config

_BASE = "https://open.feishu.cn/open-apis"


class BitableError(RuntimeError):
    pass


class BitableClient:
    def __init__(
        self,
        app_id: str | None = None,
        app_secret: str | None = None,
        app_token: str | None = None,
    ) -> None:
        self.app_id = app_id or config.FEISHU_APP_ID
        self.app_secret = app_secret or config.FEISHU_APP_SECRET
        self.app_token = app_token or config.FEISHU_BITABLE_APP_TOKEN
        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._token_lock = threading.Lock()

    # ── Auth ──────────────────────────────────────────────────────────────

    def _tenant_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token
        with self._token_lock:
            if self._token and time.time() < self._token_expires_at - 60:
                return self._token
            resp = requests.post(
                f"{_BASE}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise BitableError(f"tenant_access_token failed: {data}")
            self._token = data["tenant_access_token"]
            self._token_expires_at = time.time() + int(data.get("expire", 7200))
            return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._tenant_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        url = f"{_BASE}{path}"
        resp = requests.request(
            method,
            url,
            headers=self._headers(),
            params=params,
            json=json_body,
            timeout=30,
        )
        try:
            data = resp.json()
        except ValueError:
            raise BitableError(f"{method} {path} non-json response: {resp.text[:200]}")
        if data.get("code") != 0:
            raise BitableError(f"{method} {path} failed: {data}")
        return data.get("data", {})

    # ── Records ──────────────────────────────────────────────────────────

    def _records_base(self, table_id: str) -> str:
        return f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records"

    def get_record(self, table_id: str, record_id: str) -> dict[str, Any]:
        data = self._request("GET", f"{self._records_base(table_id)}/{record_id}")
        return data.get("record", {})

    def list_records(
        self,
        table_id: str,
        *,
        filter_expr: str = "",
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Return all records matching filter_expr, paging through all pages.

        filter_expr uses Bitable's FQL, e.g.:
            CurrentValue.[启用] = TRUE()
            AND(CurrentValue.[项目] = "RIO", CurrentValue.[范围] = "项目")
        """
        records: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            if filter_expr:
                params["filter"] = filter_expr
            data = self._request("GET", self._records_base(table_id), params=params)
            records.extend(data.get("items", []) or [])
            if not data.get("has_more"):
                break
            page_token = data.get("page_token", "")
            if not page_token:
                break
        return records

    def batch_create(
        self,
        table_id: str,
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """records = [{"fields": {...}}, ...]  (max 500 per call)"""
        if not records:
            return []
        created: list[dict[str, Any]] = []
        for i in range(0, len(records), 500):
            chunk = records[i : i + 500]
            data = self._request(
                "POST",
                f"{self._records_base(table_id)}/batch_create",
                json_body={"records": chunk},
            )
            created.extend(data.get("records", []) or [])
        return created

    def update_record(
        self,
        table_id: str,
        record_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        data = self._request(
            "PUT",
            f"{self._records_base(table_id)}/{record_id}",
            json_body={"fields": fields},
        )
        return data.get("record", {})

    # ── Attachments ──────────────────────────────────────────────────────

    def download_attachment(self, file_token: str) -> bytes:
        """Download the raw bytes of a Bitable attachment.

        Uses /drive/v1/medias/{file_token}/download with the ``extra`` param
        that scopes the tenant_access_token to this Bitable.
        """
        import json as _json
        extra = _json.dumps({"bitable": {"app_token": self.app_token}}, ensure_ascii=False)
        resp = requests.get(
            f"{_BASE}/drive/v1/medias/{file_token}/download",
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
            params={"extra": extra},
            timeout=60,
        )
        if resp.status_code != 200:
            raise BitableError(
                f"download attachment {file_token} failed: "
                f"status={resp.status_code} body={resp.text[:200]}"
            )
        return resp.content


# ── Field helpers ────────────────────────────────────────────────────────

def text_of(field_value: Any) -> str:
    """Bitable multi-line text comes back as [{"type":"text","text":"..."}].
    Return a plain string regardless of shape."""
    if field_value is None:
        return ""
    if isinstance(field_value, str):
        return field_value
    if isinstance(field_value, list):
        parts: list[str] = []
        for item in field_value:
            if isinstance(item, dict):
                parts.append(item.get("text") or item.get("name") or "")
            else:
                parts.append(str(item))
        return "".join(parts)
    if isinstance(field_value, dict):
        return field_value.get("text") or field_value.get("name") or ""
    return str(field_value)


def single_select_of(field_value: Any) -> str:
    if isinstance(field_value, dict):
        return field_value.get("text") or field_value.get("name") or ""
    if isinstance(field_value, str):
        return field_value
    return ""


def multi_select_of(field_value: Any) -> list[str]:
    if not field_value:
        return []
    if isinstance(field_value, list):
        return [text_of(x) for x in field_value if text_of(x)]
    if isinstance(field_value, str):
        return [field_value]
    return []


def link_ids_of(field_value: Any) -> list[str]:
    """Extract linked record IDs from a 'link' field."""
    if not field_value:
        return []
    if isinstance(field_value, dict):
        # Some Bitable versions return {"link_record_ids":[...]}
        ids = field_value.get("link_record_ids") or field_value.get("record_ids")
        if ids:
            return list(ids)
    if isinstance(field_value, list):
        ids = []
        for item in field_value:
            if isinstance(item, dict):
                rid = item.get("record_id") or item.get("id")
                if rid:
                    ids.append(rid)
            elif isinstance(item, str):
                ids.append(item)
        return ids
    return []


def attachment_tokens_of(field_value: Any) -> list[dict[str, str]]:
    """Extract {file_token, mime_type, name} from an attachment-field value.

    Attachment field shape:
      [{"file_token": "boxcn...", "name": "1.jpg", "type": "image/jpeg",
        "size": 12345, "url": "...", ...}, ...]
    """
    if not field_value:
        return []
    results: list[dict[str, str]] = []
    items = field_value if isinstance(field_value, list) else [field_value]
    for item in items:
        if not isinstance(item, dict):
            continue
        token = item.get("file_token") or item.get("token")
        if not token:
            continue
        mime = item.get("type") or item.get("mime_type") or "image/jpeg"
        name = item.get("name") or ""
        results.append({"file_token": token, "mime_type": mime, "name": name})
    return results


_SUPPORTED_IMAGE_MIMES = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif",
}


def encode_attachments_as_images(
    client: BitableClient,
    attachments: list[dict[str, str]],
    *,
    max_images: int = 4,
) -> list[dict[str, str]]:
    """Download up to ``max_images`` attachments and return LLM-ready image
    dicts: [{"media_type": "image/jpeg", "data": "<base64>"}, ...].
    Non-image attachments are silently skipped.
    """
    import base64

    results: list[dict[str, str]] = []
    for att in attachments[:max_images]:
        mime = att.get("mime_type", "").lower()
        if mime not in _SUPPORTED_IMAGE_MIMES:
            continue
        try:
            raw = client.download_attachment(att["file_token"])
        except Exception:
            continue
        results.append({
            "media_type": mime,
            "data": base64.b64encode(raw).decode("ascii"),
        })
    return results

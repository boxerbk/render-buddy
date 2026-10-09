"""Thin client for the Phaze Enterprise API (control plane).

Docs: https://apidocs.phaze.app/
Copied from the help desk example, trimmed to what Render Buddy needs.
"""
import time
from typing import Iterator

import httpx

BASE_URL = "https://public-api.phaze.app/enterprise/v1"


class PhazeAPIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class PhazeAPI:
    def __init__(self, api_key: str):
        self.http = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )

    def _get(self, path: str, **params) -> dict:
        for attempt in range(3):
            r = self.http.get(path, params=params)
            if r.status_code == 429 and attempt < 2:
                time.sleep(int(r.headers.get("Retry-After", "5")))
                continue
            if r.status_code >= 400:
                raise PhazeAPIError(r.status_code, f"Phaze API {r.status_code} on {path}: {r.text[:300]}")
            return r.json()
        raise PhazeAPIError(429, f"Phaze API rate limited on {path}")

    def _paginate(self, path: str, **params) -> Iterator[dict]:
        offset = 0
        while True:
            page = self._get(path, limit=200, offset=offset, **params)
            items = page.get("data", [])
            yield from items
            offset += len(items)
            if not items or offset >= page.get("count", 0):
                return

    def list_orgs(self) -> list[dict]:
        return list(self._paginate("/orgs"))

    def list_machines(self, org_id: str) -> list[dict]:
        """Every machine in the org: id, name, is_online, os, assignee, group, ..."""
        return list(self._paginate(f"/orgs/{org_id}/machines"))

    def close(self):
        self.http.close()

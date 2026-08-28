"""Shared HTTP plumbing for the Radarr and Sonarr clients.

Both apps expose the same *Arr family API shape — an `X-Api-Key` header, a
`/api/v3/...` path, JSON in and out — so the bits that are identical (the
request wrapper, quality-profile/root-folder auto-detection) live here once.
What differs (lookup path, add path, movie vs. series records) stays in each
app's own thin subclass.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

LOGGER = logging.getLogger(__name__)


class ArrError(RuntimeError):
    """Raised when Radarr/Sonarr returns something the caller can't use."""


class ArrClient:
    def __init__(self, session: aiohttp.ClientSession, *, base_url: str, api_key: str) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-Api-Key": api_key}

    async def _request(
        self, method: str, path: str, *, json: Any = None, params: dict | None = None
    ) -> Any:
        url = f"{self._base_url}{path}"
        async with self._session.request(
            method, url, headers=self._headers, json=json, params=params
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise ArrError(f"{method} {path} failed: {resp.status} {body[:500]}")
            if resp.status == 204:
                return None
            return await resp.json()

    async def get(self, path: str, *, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json: dict) -> Any:
        return await self._request("POST", path, json=json)

    async def default_quality_profile_id(self, configured: int | None) -> int:
        if configured is not None:
            return configured
        profiles = await self.get("/api/v3/qualityprofile")
        if not profiles:
            raise ArrError("No quality profiles are configured")
        if len(profiles) > 1:
            LOGGER.warning(
                "%s: multiple quality profiles exist and none is configured — "
                "using the first one returned (%s)",
                self._base_url,
                profiles[0].get("name"),
            )
        return int(profiles[0]["id"])

    async def default_root_folder(self, configured: str | None) -> str:
        if configured is not None:
            return configured
        folders = await self.get("/api/v3/rootfolder")
        if not folders:
            raise ArrError("No root folders are configured")
        if len(folders) > 1:
            LOGGER.warning(
                "%s: multiple root folders exist and none is configured — "
                "using the first one returned (%s)",
                self._base_url,
                folders[0].get("path"),
            )
        return str(folders[0]["path"])

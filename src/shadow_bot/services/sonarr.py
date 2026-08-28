from __future__ import annotations

from shadow_bot.domain.media import MediaCandidate, build_sonarr_add_payload, parse_sonarr_lookup
from shadow_bot.services._arr_client import ArrClient


class SonarrClient(ArrClient):
    async def search(self, term: str) -> list[MediaCandidate]:
        raw = await self.get("/api/v3/series/lookup", params={"term": term})
        return parse_sonarr_lookup(raw or [])

    async def add(
        self, candidate: MediaCandidate, *, quality_profile_id: int | None, root_folder: str | None
    ) -> dict:
        payload = build_sonarr_add_payload(
            candidate,
            quality_profile_id=await self.default_quality_profile_id(quality_profile_id),
            root_folder=await self.default_root_folder(root_folder),
        )
        return await self.post("/api/v3/series", payload)

    async def get_series(self, series_id: int) -> dict:
        return await self.get(f"/api/v3/series/{series_id}")

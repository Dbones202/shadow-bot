"""Pure mapping logic for Radarr/Sonarr search results and requests.

Kept dependency-free of aiohttp and discord.py so the JSON-shape assumptions
here — which fields exist, which can be missing — are covered by fast unit
tests instead of only being exercised by a live HTTP call. `services/radarr.py`
and `services/sonarr.py` do the network I/O and hand this module raw dicts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MediaCandidate:
    """One search result from a Radarr/Sonarr lookup, ready to show or add."""

    title: str
    year: int | None
    poster_url: str | None
    imdb_id: str | None
    tmdb_id: int | None
    tvdb_id: int | None
    #: Radarr/Sonarr's own id for this title. 0 (or missing) means it is not
    #: yet in that app's library — that is also how `already_in_library` is
    #: derived, rather than trusting a separate flag either API might change.
    external_id: int
    already_in_library: bool

    @property
    def imdb_url(self) -> str | None:
        return f"https://www.imdb.com/title/{self.imdb_id}/" if self.imdb_id else None

    @property
    def display_title(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title


def _poster_url(raw: dict) -> str | None:
    """Radarr/Sonarr both put a convenience `remotePoster`, but it is not
    always present — fall back to scanning the `images` list for the same
    thing, which is always there when a poster exists at all."""
    remote_poster = raw.get("remotePoster")
    if remote_poster:
        return str(remote_poster)
    for image in raw.get("images") or []:
        if image.get("coverType") == "poster":
            url = image.get("remoteUrl") or image.get("url")
            if url:
                return str(url)
    return None


def _candidate(raw: dict) -> MediaCandidate:
    external_id = raw.get("id") or 0
    return MediaCandidate(
        title=str(raw.get("title") or "Unknown title"),
        year=raw.get("year") or None,
        poster_url=_poster_url(raw),
        imdb_id=raw.get("imdbId") or None,
        tmdb_id=raw.get("tmdbId") or None,
        tvdb_id=raw.get("tvdbId") or None,
        external_id=external_id,
        already_in_library=bool(external_id),
    )


def parse_radarr_lookup(raw_results: list[dict]) -> list[MediaCandidate]:
    return [_candidate(raw) for raw in raw_results]


def parse_sonarr_lookup(raw_results: list[dict]) -> list[MediaCandidate]:
    return [_candidate(raw) for raw in raw_results]


def build_radarr_add_payload(
    candidate: MediaCandidate, *, quality_profile_id: int, root_folder: str
) -> dict:
    """The body for `POST /api/v3/movie`. `searchForMovie` starts the download
    immediately instead of leaving the movie added-but-unmonitored."""
    return {
        "title": candidate.title,
        "tmdbId": candidate.tmdb_id,
        "year": candidate.year,
        "qualityProfileId": quality_profile_id,
        "rootFolderPath": root_folder,
        "monitored": True,
        "addOptions": {"searchForMovie": True},
    }


def build_sonarr_add_payload(
    candidate: MediaCandidate, *, quality_profile_id: int, root_folder: str
) -> dict:
    """The body for `POST /api/v3/series`. Donovan chose whole-series requests
    over per-season selection, so every season is monitored and searched."""
    return {
        "title": candidate.title,
        "tvdbId": candidate.tvdb_id,
        "qualityProfileId": quality_profile_id,
        "rootFolderPath": root_folder,
        "seasonFolder": True,
        "monitored": True,
        "addOptions": {"monitor": "all", "searchForMissingEpisodes": True},
    }


def movie_is_downloaded(record: dict) -> bool:
    """A Radarr movie record (`GET /api/v3/movie/{id}`) has `hasFile` set the
    moment the file lands, before any history/queue plumbing gets involved —
    the cheapest possible signal for the daily poller to check."""
    return bool(record.get("hasFile"))


def series_is_downloaded(record: dict) -> bool:
    """A Sonarr series record (`GET /api/v3/series/{id}`) carries a
    `statistics` block with episode counts. Complete means every monitored
    episode has a file — an empty/zero-episode series (metadata not refreshed
    yet) is deliberately not counted as complete."""
    stats = record.get("statistics") or {}
    episode_count = stats.get("episodeCount") or 0
    file_count = stats.get("episodeFileCount") or 0
    return episode_count > 0 and file_count >= episode_count

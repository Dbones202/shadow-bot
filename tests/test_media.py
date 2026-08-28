from shadow_bot.domain.media import (
    build_radarr_add_payload,
    build_sonarr_add_payload,
    movie_is_downloaded,
    parse_radarr_lookup,
    parse_sonarr_lookup,
    series_is_downloaded,
)

RADARR_RESULT = {
    "title": "Arrival",
    "year": 2016,
    "remotePoster": "https://example.test/arrival.jpg",
    "imdbId": "tt2543164",
    "tmdbId": 329865,
    "id": 0,
}

SONARR_RESULT = {
    "title": "The Expanse",
    "year": 2015,
    "images": [{"coverType": "poster", "remoteUrl": "https://example.test/expanse.jpg"}],
    "imdbId": "tt3230854",
    "tvdbId": 280619,
    "id": 42,
}


def test_parse_radarr_lookup_reads_the_expected_fields() -> None:
    (candidate,) = parse_radarr_lookup([RADARR_RESULT])
    assert candidate.title == "Arrival"
    assert candidate.year == 2016
    assert candidate.poster_url == "https://example.test/arrival.jpg"
    assert candidate.imdb_id == "tt2543164"
    assert candidate.tmdb_id == 329865
    assert candidate.display_title == "Arrival (2016)"
    assert candidate.imdb_url == "https://www.imdb.com/title/tt2543164/"


def test_parse_radarr_lookup_id_zero_means_not_in_library() -> None:
    (candidate,) = parse_radarr_lookup([RADARR_RESULT])
    assert candidate.already_in_library is False
    assert candidate.external_id == 0


def test_parse_sonarr_lookup_falls_back_to_images_list_for_poster() -> None:
    (candidate,) = parse_sonarr_lookup([SONARR_RESULT])
    assert candidate.poster_url == "https://example.test/expanse.jpg"
    assert candidate.tvdb_id == 280619
    assert candidate.already_in_library is True
    assert candidate.external_id == 42


def test_candidate_with_no_poster_or_year_degrades_gracefully() -> None:
    (candidate,) = parse_radarr_lookup([{"title": "Untitled Project", "id": 0}])
    assert candidate.poster_url is None
    assert candidate.year is None
    assert candidate.display_title == "Untitled Project"
    assert candidate.imdb_url is None


def test_build_radarr_add_payload_requests_a_search() -> None:
    (candidate,) = parse_radarr_lookup([RADARR_RESULT])
    payload = build_radarr_add_payload(candidate, quality_profile_id=4, root_folder="/movies")
    assert payload["tmdbId"] == 329865
    assert payload["qualityProfileId"] == 4
    assert payload["rootFolderPath"] == "/movies"
    assert payload["monitored"] is True
    assert payload["addOptions"] == {"searchForMovie": True}


def test_build_sonarr_add_payload_monitors_and_searches_every_season() -> None:
    (candidate,) = parse_sonarr_lookup([SONARR_RESULT])
    payload = build_sonarr_add_payload(candidate, quality_profile_id=7, root_folder="/tv")
    assert payload["tvdbId"] == 280619
    assert payload["qualityProfileId"] == 7
    assert payload["rootFolderPath"] == "/tv"
    assert payload["addOptions"] == {"monitor": "all", "searchForMissingEpisodes": True}


def test_movie_is_downloaded_reads_has_file() -> None:
    assert movie_is_downloaded({"hasFile": True}) is True
    assert movie_is_downloaded({"hasFile": False}) is False
    assert movie_is_downloaded({}) is False


def test_series_is_downloaded_requires_every_monitored_episode() -> None:
    complete = {"statistics": {"episodeCount": 10, "episodeFileCount": 10}}
    partial = {"statistics": {"episodeCount": 10, "episodeFileCount": 4}}
    assert series_is_downloaded(complete) is True
    assert series_is_downloaded(partial) is False


def test_series_is_downloaded_is_false_when_metadata_has_not_populated_yet() -> None:
    # A series just added: statistics haven't been refreshed, so episodeCount
    # is still 0. That must never read as "complete".
    assert series_is_downloaded({"statistics": {"episodeCount": 0, "episodeFileCount": 0}}) is False
    assert series_is_downloaded({}) is False

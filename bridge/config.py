"""Runtime configuration, read from environment variables (Docker-friendly)."""

import json
import os


def _bool(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


def _float(name, default):
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


CAT_GENERAL, CAT_ARTIST, CAT_COPYRIGHT, CAT_CHARACTER, CAT_META = 0, 1, 3, 4, 5


class Config:
    def __init__(self):
        # ---- pixiv
        self.pixiv_refresh_token = os.environ.get("PIXIV_REFRESH_TOKEN", "")
        self.pixiv_language = os.environ.get("PIXIV_LANGUAGE", "en-us")
        # Minimum seconds between pixiv API calls (pixiv rate-limits aggressively).
        self.pixiv_min_interval = _float("PIXIV_MIN_INTERVAL", 1.0)
        self.pixiv_timeout = _int("PIXIV_TIMEOUT", 30)
        # search_target used for mapped tags: exact_match_for_tags | partial_match_for_tags
        self.search_target = os.environ.get("PIXIV_SEARCH_TARGET", "exact_match_for_tags")
        # What an empty Danbooru query shows: follow | recommended | ranking | new
        self.empty_query_source = os.environ.get("EMPTY_QUERY_SOURCE", "follow")
        # One Danbooru post per pixiv page (all) or only the first page of each work (first).
        self.pages_mode = os.environ.get("PAGES_MODE", "all")
        # Max pixiv result pages (30 works each) scanned per request when local
        # filters (width:, rating:, -tag ...) drop results.
        self.max_scan_pages = _int("MAX_SCAN_PAGES", 10)
        # Drop AI-generated works everywhere.
        self.hide_ai = _bool("HIDE_AI", False)

        # ---- danbooru (tag mapping source)
        self.danbooru_url = os.environ.get("DANBOORU_URL", "https://danbooru.donmai.us").rstrip("/")
        self.danbooru_login = os.environ.get("DANBOORU_LOGIN", "")
        self.danbooru_api_key = os.environ.get("DANBOORU_API_KEY", "")
        self.danbooru_min_interval = _float("DANBOORU_MIN_INTERVAL", 1.0)
        # Look up tags live on Danbooru when they are not in the local database yet.
        self.live_lookups = _bool("DANBOORU_LIVE_LOOKUPS", True)
        # Danbooru requests made immediately per page of results for unknown tags;
        # the rest are looked up in the background.
        self.live_lookup_budget = _int("DANBOORU_LIVE_LOOKUP_BUDGET", 2)
        # How many pixiv names of a tag are OR-ed together when searching
        # (best Japanese names + a Chinese name). 1 = only the best name.
        self.search_aliases = _int("PIXIV_SEARCH_ALIASES", 3)
        # Sync the mapping database on start-up and then every N hours (0 = never).
        self.sync_on_start = _bool("SYNC_ON_START", True)
        self.sync_interval_hours = _float("SYNC_INTERVAL_HOURS", 24)
        self.db_path = os.environ.get("DB_PATH", "/data/mapping.sqlite3")
        # JSON {"danbooru_tag": "pixiv tag"} to pin the pixiv name used when searching
        self.tag_overrides = json.loads(os.environ.get("PIXIV_TAG_OVERRIDES") or "{}")
        # How pixiv tags with no Danbooru equivalent appear: translated | original | drop
        self.unmapped_tag_style = os.environ.get("UNMAPPED_TAG_STYLE", "translated")

        # ---- bridge
        self.host = os.environ.get("BRIDGE_HOST", "0.0.0.0")
        self.port = _int("BRIDGE_PORT", 8000)
        self.public_url = os.environ.get("BRIDGE_PUBLIC_URL", "").rstrip("/")
        self.bridge_login = os.environ.get("BRIDGE_LOGIN", "")
        self.bridge_api_key = os.environ.get("BRIDGE_API_KEY", "")
        self.allow_writes = _bool("ALLOW_WRITES", True)
        self.default_limit = _int("DEFAULT_LIMIT", 20)
        self.max_limit = _int("MAX_LIMIT", 200)
        self.search_cache_ttl = _int("SEARCH_CACHE_TTL", 600)
        self.cors = _bool("BRIDGE_CORS", True)
        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    @property
    def user_agent(self):
        return "pixiv-danbooru-bridge/1.0 (+https://github.com/AnimeArchive/pixiv-danbooru-bridge)"

"""Client for pixiv's (unofficial) mobile app API, authenticated with the
user's own OAuth refresh token. Endpoint details follow pixivpy and gallery-dl."""

import datetime
import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("bridge.pixiv")

API = "https://app-api.pixiv.net"
AUTH_URL = "https://oauth.secure.pixiv.net/auth/token"
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
HASH_SECRET = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"
HEADERS = {
    "App-OS": "ios",
    "App-OS-Version": "16.7.2",
    "App-Version": "7.19.1",
    "User-Agent": "PixivIOSApp/7.19.1 (iOS 16.7.2; iPhone12,8)",
    "Referer": "https://app-api.pixiv.net/",
}
IMAGE_REFERER = "https://app-api.pixiv.net/"
SEARCH_OFFSET_CAP = 5000
PLACEHOLDER_HOST = "s.pximg.net"


class PixivError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


class PixivClient:
    def __init__(self, cfg, token_store=None):
        """token_store: optional (get, set) callables used to persist a rotated refresh token."""
        self.cfg = cfg
        self.token_store = token_store
        self.refresh_token = (token_store[0]() if token_store else None) or cfg.pixiv_refresh_token
        self.access_token = None
        self.expires = 0.0
        self.user = {}
        self._auth_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._last = 0.0
        self.api = API
        self.auth_url = AUTH_URL
        self.image_base = "https://i.pximg.net/"  # overridable for tests

    # ------------------------------------------------------------ auth
    def authenticate(self, force=False):
        with self._auth_lock:
            if not force and self.access_token and time.time() < self.expires - 60:
                return
            if not self.refresh_token:
                raise PixivError(401, "PIXIV_REFRESH_TOKEN is not set")
            now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            headers = dict(HEADERS)
            headers.update({"X-Client-Time": now,
                            "X-Client-Hash": hashlib.md5((now + HASH_SECRET).encode()).hexdigest(),
                            "Content-Type": "application/x-www-form-urlencoded"})
            body = urllib.parse.urlencode({
                "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "grant_type": "refresh_token",
                "refresh_token": self.refresh_token, "get_secure_url": "1", "include_policy": "true",
            }).encode()
            req = urllib.request.Request(self.auth_url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.pixiv_timeout) as r:
                    data = json.loads(r.read())
            except urllib.error.HTTPError as e:
                raise PixivError(401, f"pixiv rejected the refresh token (HTTP {e.code}); get a new one") from None
            except urllib.error.URLError as e:
                raise PixivError(502, f"cannot reach pixiv: {e.reason}") from None
            data = data.get("response", data)
            self.access_token = data["access_token"]
            self.expires = time.time() + int(data.get("expires_in") or 3600)
            self.user = data.get("user") or {}
            new_rt = data.get("refresh_token")
            if new_rt and new_rt != self.refresh_token:
                self.refresh_token = new_rt
                if self.token_store:
                    self.token_store[1](new_rt)
            log.info("authenticated with pixiv as %s (id %s)", self.user.get("account"), self.user.get("id"))

    @property
    def user_id(self):
        self.authenticate()
        return int(self.user.get("id") or 0)

    # ------------------------------------------------------------ calls
    def _throttle(self):
        with self._rate_lock:
            wait = self._last + self.cfg.pixiv_min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def call(self, path, params=None, method="GET", data=None, _retry=True):
        self.authenticate()
        self._throttle()
        headers = dict(HEADERS, Authorization=f"Bearer {self.access_token}",
                       **{"Accept-Language": self.cfg.pixiv_language})
        url = path if path.startswith("http") else self.api + path
        body = None
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        if data is not None:
            body = urllib.parse.urlencode(data, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.pixiv_timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read()).get("error") or {}
            except Exception:
                err = {}
            msg = err.get("message") or err.get("user_message") or err.get("reason") or f"HTTP {e.code}"
            if "oauth" in msg.lower() and _retry:  # access token expired early
                self.authenticate(force=True)
                return self.call(path, params, method, data, _retry=False)
            if "rate limit" in msg.lower():
                raise PixivError(429, "pixiv rate limit reached; try again in a few minutes")
            if e.code == 404:
                raise PixivError(404, err.get("user_message") or "not found on pixiv")
            raise PixivError(e.code if e.code >= 400 else 502, err.get("user_message") or msg)
        except urllib.error.URLError as e:
            raise PixivError(502, f"cannot reach pixiv: {e.reason}")

    # ------------------------------------------------------------ endpoints
    def illust_detail(self, illust_id):
        return self.call("/v1/illust/detail", {"illust_id": illust_id})["illust"]

    def search(self, word, sort="date_desc", target=None, start_date=None, end_date=None,
               exclude_ai=False, offset=0):
        params = {"word": word, "search_target": target or self.cfg.search_target, "sort": sort,
                  "filter": "for_ios", "merge_plain_keyword_results": "true",
                  "include_translated_tag_results": "true"}
        if start_date:
            params["start_date"] = start_date.isoformat()
        if end_date:
            params["end_date"] = end_date.isoformat()
        if exclude_ai:
            params["search_ai_type"] = 0
        if offset:
            params["offset"] = offset
        return self.call("/v1/search/illust", params)

    def user_illusts(self, user_id, kind="illust", offset=0):
        p = {"user_id": user_id, "type": kind, "filter": "for_ios"}
        if offset:
            p["offset"] = offset
        return self.call("/v1/user/illusts", p)

    def user_detail(self, user_id):
        return self.call("/v1/user/detail", {"user_id": user_id, "filter": "for_ios"})

    def bookmarks(self, user_id, restrict="public", max_bookmark_id=None):
        p = {"user_id": user_id, "restrict": restrict, "filter": "for_ios"}
        if max_bookmark_id:
            p["max_bookmark_id"] = max_bookmark_id
        return self.call("/v1/user/bookmarks/illust", p)

    def ranking(self, mode="day", date=None, offset=0):
        p = {"mode": mode, "filter": "for_ios"}
        if date:
            p["date"] = date
        if offset:
            p["offset"] = offset
        return self.call("/v1/illust/ranking", p)

    def follow(self, offset=0):
        p = {"restrict": "public"}
        if offset:
            p["offset"] = offset
        return self.call("/v2/illust/follow", p)

    def recommended(self):
        return self.call("/v1/illust/recommended", {"content_type": "illust", "include_ranking_label": "true",
                                                     "filter": "for_ios"})

    def new(self, max_illust_id=None):
        p = {"content_type": "illust", "filter": "for_ios"}
        if max_illust_id:
            p["max_illust_id"] = max_illust_id
        return self.call("/v1/illust/new", p)

    def autocomplete(self, word):
        return self.call("/v2/search/autocomplete", {"word": word, "merge_plain_keyword_results": "true"}).get("tags", [])

    def bookmark_add(self, illust_id, restrict="public"):
        self.call("/v2/illust/bookmark/add", method="POST", data={"illust_id": illust_id, "restrict": restrict})

    def bookmark_delete(self, illust_id):
        self.call("/v1/illust/bookmark/delete", method="POST", data={"illust_id": illust_id})

    def next_page(self, next_url):
        """Follow a `next_url` (absolute URL with all paging params)."""
        return self.call(next_url)

    # ------------------------------------------------------------ images
    def open_image(self, url, range_header=None):
        if not url.startswith("https://i.pximg.net/"):
            raise PixivError(403, "only i.pximg.net images can be proxied")
        headers = {"Referer": IMAGE_REFERER, "User-Agent": HEADERS["User-Agent"]}
        if range_header:
            headers["Range"] = range_header
        req = urllib.request.Request(url.replace("https://i.pximg.net/", self.image_base, 1), headers=headers)
        try:
            return urllib.request.urlopen(req, timeout=self.cfg.pixiv_timeout)
        except urllib.error.HTTPError as e:
            if e.code == 416:
                return e
            raise PixivError(404 if e.code in (403, 404) else 502, f"image fetch failed: HTTP {e.code}")
        except urllib.error.URLError as e:
            raise PixivError(502, f"cannot reach i.pximg.net: {e.reason}")

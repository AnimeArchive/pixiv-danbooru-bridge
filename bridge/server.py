"""HTTP front-end: speaks the Danbooru API, answers from pixiv."""

import base64
import hmac
import json
import logging
import re
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .api import EMPTY_INDEXES, ApiError, Bridge, ImageResult, NotFound, Result
from .config import Config
from .convert import to_xml
from .pixiv import PixivError

log = logging.getLogger("bridge.http")

FMT = r"(?:\.(?P<fmt>json|xml))?"
ROUTES = []


def route(method, pattern, public=False, anonymous_ok=False):
    def deco(fn):
        fn.public, fn.anonymous_ok = public, anonymous_ok
        ROUTES.append((method, re.compile("^" + pattern + FMT + "$"), fn))
        return fn
    return deco


def flatten(obj, prefix="", out=None):
    """{"search": {"name": "x"}} -> {"search[name]": "x"} (Rails-style params)."""
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(v, f"{prefix}[{k}]" if prefix else str(k), out)
    elif isinstance(obj, list):
        out[prefix] = ",".join(str(x) for x in obj)
    elif isinstance(obj, bool):
        out[prefix] = "true" if obj else "false"
    elif obj is not None:
        out[prefix] = str(obj)
    return out


def _xml_attr(s):
    return str(s).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


class Handler(BaseHTTPRequestHandler):
    server_version = "pixiv-danbooru-bridge/1.0"
    protocol_version = "HTTP/1.1"
    bridge: Bridge = None
    cfg: Config = None

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Range")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cors(self):
        if self.cfg.cors:
            self.send_header("Access-Control-Allow-Origin", "*")

    def _params(self):
        url = urllib.parse.urlsplit(self.path)
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            raw = self.rfile.read(length)
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype == "application/json":
                try:
                    params.update(flatten(json.loads(raw.decode("utf-8") or "{}")))
                except ValueError:
                    raise ApiError(400, "invalid JSON body")
            elif ctype in ("application/x-www-form-urlencoded", ""):
                body = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
                params.update({k: v[-1] for k, v in body.items()})
        return url.path, params, params.pop("_method", None)

    def _base_url(self):
        if self.cfg.public_url:
            return self.cfg.public_url
        proto = self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip()
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or f"localhost:{self.cfg.port}"
        prefix = (self.headers.get("X-Forwarded-Prefix") or "").rstrip("/")
        return f"{proto}://{host.split(',')[0].strip()}{prefix}"

    def _auth(self, params):
        """True when authenticated, False when anonymous; raises 401 on bad credentials."""
        login, key = params.pop("login", None), params.pop("api_key", None)
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("basic "):
            try:
                login, key = base64.b64decode(auth[6:]).decode("utf-8").split(":", 1)
            except Exception:
                raise ApiError(401, "malformed Authorization header", "SessionLoader::AuthenticationFailure")
        if not self.cfg.bridge_api_key:
            return True
        if key is None:
            return False
        ok = hmac.compare_digest(key, self.cfg.bridge_api_key) and (
            not self.cfg.bridge_login or hmac.compare_digest(login or "", self.cfg.bridge_login))
        if not ok:
            raise ApiError(401, "Invalid API key", "SessionLoader::AuthenticationFailure")
        return True

    def _dispatch(self, method):
        fmt = "json"
        try:
            path, params, override = self._params()
            if override and method == "POST":
                method = override.upper()
            if params.get("format") in ("json", "xml"):
                fmt = params["format"]
            accept = self.headers.get("Accept") or ""
            if "xml" in accept and "json" not in accept:
                fmt = "xml"
            path = path.rstrip("/") or "/"
            for m, rx, fn in ROUTES:
                match = rx.match(path)
                if not match or (m != method and not (m == "GET" and method == "HEAD")):
                    continue
                if match.groupdict().get("fmt"):
                    fmt = match.group("fmt")
                kwargs = {k: v for k, v in match.groupdict().items() if k != "fmt"}
                authed = True
                if not fn.public:
                    authed = self._auth(params)
                    if not authed and not fn.anonymous_ok:
                        raise ApiError(401, "Authentication required (login + api_key)",
                                       "SessionLoader::AuthenticationFailure")
                result = fn(self, params, authed, **kwargs)
                if isinstance(result, ImageResult):
                    return self._stream(result, method)
                return self._send(result, fmt, method)
            raise NotFound("Page not found")
        except ApiError as e:
            self._error(e.status, e.error, e.message, fmt, method)
        except PixivError as e:
            self._error({404: 404, 429: 429}.get(e.status, 502), "PixivError", e.message, fmt, method)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa
            log.error("unhandled error: %s\n%s", e, traceback.format_exc())
            self._error(500, type(e).__name__, str(e), fmt, method)

    def _send(self, result: Result, fmt, method):
        if result.status == 204 or result.body is None:
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if fmt == "xml":
            body = result.body
            if isinstance(body, dict) and list(body) == [result.root]:
                body = body[result.root]
            data = to_xml(result.root, body).encode("utf-8")
            ctype = "application/xml; charset=utf-8"
        else:
            data = json.dumps(result.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        self.send_response(result.status)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(data)

    def _error(self, status, error, message, fmt, method):
        if fmt == "xml":
            data = (f'<?xml version="1.0" encoding="UTF-8"?>\n<result success="false" '
                    f'reason="{_xml_attr(message)}" message="{_xml_attr(message)}"/>\n').encode("utf-8")
            ctype = "application/xml; charset=utf-8"
        else:
            data = json.dumps({"success": False, "error": error, "message": message,
                               "reason": message, "backtrace": []}).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        try:
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream(self, res: ImageResult, method):
        resp = self.bridge.pixiv.open_image(res.url, self.headers.get("Range"))
        try:
            status = getattr(resp, "status", None) or resp.getcode()
            self.send_response(status)
            self._cors()
            for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag"):
                v = resp.headers.get(h)
                if v:
                    self.send_header(h, v)
            if status < 300:
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            if not resp.headers.get("Content-Length"):
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if method == "HEAD":
                return
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()

    # ------------------------------------------------------------ routes
    @route("GET", r"/posts")
    def r_posts(self, params, authed):
        return self.bridge.posts_index(params, self._base_url())

    @route("GET", r"/posts/random")
    def r_random(self, params, authed):
        return self.bridge.posts_random(params, self._base_url())

    @route("GET", r"/posts/(?P<post_id>\d+)")
    def r_post(self, params, authed, post_id):
        return self.bridge.posts_show(post_id, params, self._base_url())

    @route("POST", r"/posts/(?P<post_id>\d+)/favorite")
    def r_fav(self, params, authed, post_id):
        return self.bridge.favorites_create({"post_id": post_id})

    @route("DELETE", r"/posts/(?P<post_id>\d+)/favorite")
    def r_unfav(self, params, authed, post_id):
        return self.bridge.favorites_delete(post_id)

    @route("GET", r"/counts/posts")
    def r_counts(self, params, authed):
        return self.bridge.counts(params, self._base_url())

    @route("GET", r"/explore/posts/(?:popular|viewed|curated)")
    def r_popular(self, params, authed):
        return self.bridge.popular(params, self._base_url())

    @route("GET", r"/tags")
    def r_tags(self, params, authed):
        return self.bridge.tags_index(params)

    @route("GET", r"/tags/autocomplete")
    def r_tags_ac(self, params, authed):
        return self.bridge.legacy_tag_autocomplete(params)

    @route("GET", r"/tags/(?P<tag_id>\d+)")
    def r_tag(self, params, authed, tag_id):
        return self.bridge.tags_show(tag_id)

    @route("GET", r"/autocomplete")
    def r_autocomplete(self, params, authed):
        return self.bridge.autocomplete(params)

    @route("GET", r"/related_tag")
    def r_related(self, params, authed):
        return self.bridge.related_tag(params, self._base_url())

    @route("GET", r"/tag_aliases")
    def r_aliases(self, params, authed):
        return self.bridge.tag_aliases(params)

    @route("GET", r"/artists")
    def r_artists(self, params, authed):
        return self.bridge.artists_index(params)

    @route("GET", r"/artists/(?P<artist_id>\d+)")
    def r_artist(self, params, authed, artist_id):
        return self.bridge.artists_show(artist_id)

    @route("GET", r"/favorites")
    def r_favs(self, params, authed):
        return self.bridge.favorites_index(params, self._base_url())

    @route("POST", r"/favorites")
    def r_fav_create(self, params, authed):
        return self.bridge.favorites_create(params)

    @route("DELETE", r"/favorites/(?P<post_id>\d+)")
    def r_fav_delete(self, params, authed, post_id):
        return self.bridge.favorites_delete(post_id)

    @route("GET", r"/profile", anonymous_ok=True)
    def r_profile(self, params, authed):
        return self.bridge.profile(authed)

    @route("GET", r"/users/(?P<uid>\d+)")
    def r_user(self, params, authed, uid):
        return self.bridge.users_show(uid)

    @route("GET", r"/img/(?P<sig>[0-9a-f]{20})/(?P<path>.+)", public=True)
    def r_img(self, params, authed, sig, path):
        return self.bridge.image(sig, path)

    @route("GET", r"/(?:status|healthz)", public=True)
    def r_status(self, params, authed):
        return self.bridge.status()

    @route("GET", r"/", public=True)
    def r_root(self, params, authed):
        return Result({"name": "pixiv-danbooru-bridge", "api": "danbooru"}, "bridge")

    @route("GET", r"/(?P<resource>[a-z_]+)")
    def r_empty_index(self, params, authed, resource):
        if resource in EMPTY_INDEXES:
            return Result([], resource.replace("_", "-"))
        raise NotFound("Page not found")

    @route("GET", r"/(?P<resource>[a-z_]+)/(?P<rid>[^/]+)")
    def r_empty_show(self, params, authed, resource, rid):
        raise NotFound()


def sync_loop(bridge, cfg):
    first = True
    while True:
        if not first or cfg.sync_on_start:
            bridge.mapping.sync()
        first = False
        if cfg.sync_interval_hours <= 0:
            return
        time.sleep(cfg.sync_interval_hours * 3600)


def serve(cfg=None):
    cfg = cfg or Config()
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bridge = Bridge(cfg)
    Handler.bridge, Handler.cfg = bridge, cfg
    try:
        bridge.pixiv.authenticate()
    except PixivError as e:
        log.warning("pixiv login failed: %s", e.message)
    if not cfg.bridge_api_key:
        log.warning("BRIDGE_API_KEY is not set: anyone who can reach port %s can use your pixiv account", cfg.port)
    log.info("mapping database: %s", bridge.mapping.stats())
    threading.Thread(target=sync_loop, args=(bridge, cfg), daemon=True).start()
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    httpd.daemon_threads = True
    log.info("danbooru API listening on http://%s:%s", cfg.host, cfg.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

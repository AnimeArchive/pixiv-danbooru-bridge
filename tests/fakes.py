"""Fake pixiv (OAuth + app API + i.pximg.net) and fake Danbooru, on one local server."""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REFRESH_TOKEN = "good-refresh-token"
ACCESS_TOKEN = "fake-access-token"
REQUESTS = []

# ------------------------------------------------------------------ danbooru
WIKI = [
    {"id": 1, "title": "blue_sky", "other_names": ["青空", "蒼空"]},
    {"id": 2, "title": "hatsune_miku", "other_names": ["初音ミク", "ミク"]},
    {"id": 3, "title": "original", "other_names": ["オリジナル", "原创"]},
    {"id": 4, "title": "vocaloid", "other_names": ["VOCALOID", "ボーカロイド"]},
    {"id": 5, "title": "long_hair", "other_names": ["ロングヘア", "黒髪ロング", "长发"]},
    {"id": 6, "title": "kaito_(vocaloid)", "other_names": ["KAITO"]},
    {"id": 7, "title": "black_hair", "other_names": ["黒髪", "黑发", "黒髪ロング"]},
    {"id": 8, "title": "diana_(pokemon)", "other_names": ["ミク"]},  # noise: rare tag sharing a name
]
for w in WIKI:
    w.update(updated_at="2026-09-01T00:00:00.000-04:00", is_deleted=False)
TAGS = [
    {"id": 10, "name": "blue_sky", "category": 0, "post_count": 300000},
    {"id": 11, "name": "hatsune_miku", "category": 4, "post_count": 150000},
    {"id": 12, "name": "original", "category": 3, "post_count": 1500000},
    {"id": 13, "name": "vocaloid", "category": 3, "post_count": 210000},
    {"id": 14, "name": "long_hair", "category": 0, "post_count": 5000000},
    {"id": 15, "name": "kaito_(vocaloid)", "category": 4, "post_count": 20000},
    {"id": 16, "name": "miso_tanu", "category": 1, "post_count": 300},
    {"id": 17, "name": "black_hair", "category": 0, "post_count": 2200000},
    {"id": 18, "name": "diana_(pokemon)", "category": 4, "post_count": 5},
]
ALIASES = [{"antecedent_name": "kaito", "consequent_name": "kaito_(vocaloid)"}]
ARTISTS = [{"id": 100, "name": "miso_tanu", "is_deleted": False, "updated_at": "2026-09-01T00:00:00.000-04:00",
            "urls": [{"url": "https://www.pixiv.net/users/111"}, {"url": "https://x.com/miso_tanu"}]}]


def _cursor(rows, q):
    page = q.get("page", "")
    if not page.startswith("b"):
        # like real Danbooru: without a b<id> cursor the index's default order is
        # used (not id order), so a sync must always send a cursor
        return sorted(rows, key=lambda r: r["id"])[: int(q.get("limit", 20))]
    rows = sorted(rows, key=lambda r: -r["id"])
    rows = [r for r in rows if r["id"] < int(page[1:])]
    return rows[: int(q.get("limit", 20))]


def danbooru(path, q):
    if path == "/wiki_pages.json":
        rows = WIKI
        if "search[title]" in q:
            rows = [w for w in rows if w["title"] == q["search[title]"]]
        if "search[other_names_include_any_lower]" in q:
            want = set(q["search[other_names_include_any_lower]"].split())
            rows = [w for w in rows if want & {n.lower() for n in w["other_names"]}]
        return _cursor(rows, q)
    if path == "/artists.json":
        if "*" in q.get("search[url_matches]", ""):
            return "timeout"  # real Danbooru times out on wildcard-url searches deep in the table
        rows = ARTISTS
        if "search[name]" in q:
            rows = [a for a in rows if a["name"] == q["search[name]"]]
        url = q.get("search[url_matches]", "")
        if url and "*" not in url:  # space-separated list of exact urls
            wanted = set(url.split())
            rows = [a for a in rows if any(u["url"] in wanted for u in a["urls"])]
        return _cursor(rows, q)
    if path == "/tag_aliases.json":
        want = set(q.get("search[antecedent_name_comma]", "").split(","))
        return [a for a in ALIASES if a["antecedent_name"] in want]
    if path == "/tags.json":
        rows = TAGS
        if "search[name]" in q:
            rows = [t for t in rows if t["name"] == q["search[name]"]]
        if "search[name_comma]" in q:
            want = set(q["search[name_comma]"].split(","))
            rows = [t for t in rows if t["name"] in want]
            q = dict(q, page="b999999")
        return _cursor(rows, q)
    return None


# ------------------------------------------------------------------ pixiv
def _img(date, iid, page, ext="jpg"):
    return {
        "square_medium": f"https://i.pximg.net/c/360x360_70/img-master/img/{date}/{iid}_p{page}_square1200.jpg",
        "medium": f"https://i.pximg.net/c/540x540_70/img-master/img/{date}/{iid}_p{page}_master1200.jpg",
        "large": f"https://i.pximg.net/c/600x1200_90/img-master/img/{date}/{iid}_p{page}_master1200.jpg",
        "original": f"https://i.pximg.net/img-original/img/{date}/{iid}_p{page}.{ext}",
    }


def _illust(iid, user, tags, w, h, date, pages=1, x_restrict=0, sanity=2, bookmarks=0, ai=1, kind="illust"):
    path_date = date.replace("-", "/") + "/00/00/00"
    first = _img(path_date, iid, 0)
    if kind == "ugoira":
        first["original"] = f"https://i.pximg.net/img-original/img/{path_date}/{iid}_ugoira0.jpg"
    return {
        "id": iid, "title": f"work {iid}", "type": kind, "caption": "", "restrict": 0,
        "image_urls": {k: v for k, v in first.items() if k != "original"},
        "user": {"id": user[0], "name": user[1], "account": user[1].lower()},
        "tags": [{"name": t[0], "translated_name": t[1]} for t in tags],
        "tools": [], "create_date": f"{date}T00:00:00+09:00", "page_count": pages, "width": w, "height": h,
        "sanity_level": sanity, "x_restrict": x_restrict, "series": None,
        "meta_single_page": {"original_image_url": first["original"]} if pages == 1 else {},
        "meta_pages": [] if pages == 1 else [{"image_urls": _img(path_date, iid, p)} for p in range(pages)],
        "total_view": bookmarks * 10, "total_bookmarks": bookmarks, "is_bookmarked": False,
        "visible": True, "is_muted": False, "illust_ai_type": ai,
    }


ILLUSTS = {}
BASE = {"wiki": [dict(w) for w in WIKI], "tags": [dict(t) for t in TAGS], "artists": [dict(a) for a in ARTISTS]}


def reset():
    WIKI[:] = [dict(w) for w in BASE["wiki"]]
    TAGS[:] = [dict(t) for t in BASE["tags"]]
    ARTISTS[:] = [dict(a) for a in BASE["artists"]]
    ILLUSTS.clear()
    for i in (
        _illust(1001, (111, "MisoTanu"), [("初音ミク", "hatsune miku"), ("青空", "blue sky"), ("VOCALOID", None)],
                1500, 1100, "2026-09-01", bookmarks=500),
        _illust(1002, (222, "Someone"), [("初音ミク", None), ("ロングヘア", "long hair"), ("KAITO", None)],
                800, 1200, "2026-09-10", pages=3, sanity=4, bookmarks=100),
        _illust(1003, (222, "Someone"), [("オリジナル", "original"), ("青空", None), ("風景", "scenery"), ("R-18", None)],
                3500, 2500, "2026-09-20", x_restrict=1, bookmarks=50, ai=2),
        _illust(1004, (333, "Anim"), [("初音ミク", None), ("うごイラ", "ugoira")], 600, 600, "2026-09-25",
                kind="ugoira", bookmarks=5),
        # one pixiv tag -> two Danbooru tags; account name collides with a Danbooru tag
        _illust(1005, (444, "Original"), [("黒髪ロング", None)], 700, 1000, "2026-08-20", bookmarks=3),
        # tagged in Chinese only
        _illust(1006, (555, "Chen"), [("长发", None), ("原创", None)], 700, 1000, "2026-08-15", bookmarks=2),
    ):
        ILLUSTS[i["id"]] = i
    REQUESTS.clear()


reset()
PAGE = 2  # tiny pages so paging code gets exercised


def _page(items, q, base_path, base):
    off = int(q.get("offset", 0))
    chunk = items[off:off + PAGE]
    nxt = None
    if off + PAGE < len(items):
        nq = dict(q, offset=off + PAGE)
        nxt = f"{base}{base_path}?{urllib.parse.urlencode(nq)}"
    return {"illusts": chunk, "next_url": nxt}


def _matches(illust, word):
    """pixiv search syntax: AAA (BBB OR CCC) -DDD, or a bare A OR B."""
    import re
    names = {t["name"].lower() for t in illust["tags"]}
    if "(" not in word and " OR " in word:
        return any(w.strip().lower() in names for w in word.split(" OR "))
    for group, term in re.findall(r"\(([^)]*)\)|(\S+)", word):
        if group:
            if not any(w.strip().lower() in names for w in group.split(" OR ")):
                return False
        elif term.startswith("-"):
            if term[1:].lower() in names:
                return False
        elif term.lower() not in names:
            return False
    return True


def pixiv(path, q, base, method, form):
    by_date = sorted(ILLUSTS.values(), key=lambda i: i["create_date"], reverse=True)
    if path == "/app/v1/search/illust":
        items = [i for i in by_date if _matches(i, q["word"])]
        if q.get("search_ai_type") == "0":
            items = [i for i in items if i["illust_ai_type"] != 2]
        if q.get("start_date"):
            items = [i for i in items if i["create_date"][:10] >= q["start_date"]]
        if q.get("end_date"):
            items = [i for i in items if i["create_date"][:10] <= q["end_date"]]
        if q.get("sort") == "date_asc":
            items.reverse()
        if q.get("sort") == "popular_desc":
            items.sort(key=lambda i: -i["total_bookmarks"])
        return _page(items, q, path, base)
    if path == "/app/v1/illust/detail":
        i = ILLUSTS.get(int(q["illust_id"]))
        return {"illust": i} if i else (404, {"error": {"user_message": "not found"}})
    if path == "/app/v1/user/illusts":
        return _page([i for i in by_date if i["user"]["id"] == int(q["user_id"])], q, path, base)
    if path == "/app/v1/user/bookmarks/illust":
        return _page([i for i in by_date if i["is_bookmarked"]], q, path, base)
    if path in ("/app/v2/illust/follow", "/app/v1/illust/recommended", "/app/v1/illust/new"):
        return _page(by_date, q, path, base)
    if path == "/app/v1/illust/ranking":
        return _page(sorted(by_date, key=lambda i: -i["total_bookmarks"]), q, path, base)
    if path == "/app/v2/search/autocomplete":
        names = sorted({t["name"] for i in ILLUSTS.values() for t in i["tags"] if t["name"].startswith(q["word"])})
        return {"tags": [{"name": n, "translated_name": None} for n in names]}
    if path == "/app/v2/illust/bookmark/add" and method == "POST":
        ILLUSTS[int(form["illust_id"])]["is_bookmarked"] = True
        return {}
    if path == "/app/v1/illust/bookmark/delete" and method == "POST":
        ILLUSTS[int(form["illust_id"])]["is_bookmarked"] = False
        return {}
    if path == "/app/v1/user/detail":
        return {"user": {"id": int(q["user_id"]), "name": "x", "account": "x"}}
    return None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        form = {k: v[-1] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()} if length else {}
        base = f"http://127.0.0.1:{self.server.server_port}"
        REQUESTS.append((method, u.path, q, form, dict(self.headers)))
        if u.path == "/auth/token":
            if form.get("refresh_token") != REFRESH_TOKEN or form.get("grant_type") != "refresh_token":
                return self._send(400, {"has_error": True, "errors": {"system": {"message": "invalid_grant"}}})
            return self._send(200, {"access_token": ACCESS_TOKEN, "expires_in": 3600, "refresh_token": REFRESH_TOKEN,
                                    "user": {"id": "999", "name": "Me", "account": "me", "is_premium": False}})
        if u.path.startswith("/danbooru/"):
            out = danbooru(u.path[len("/danbooru"):], q)
            if out == "timeout":
                return self._send(500, {"success": False, "error": "ActiveRecord::QueryCanceled"})
            return self._send(200 if out is not None else 404, out if out is not None else {})
        if u.path.startswith("/pximg/"):
            if self.headers.get("Referer") != "https://app-api.pixiv.net/":
                return self._send(403, b"forbidden", "text/html")
            return self._send(200, ("IMG:" + u.path[len("/pximg/"):]).encode(), "image/jpeg")
        if u.path.startswith("/app/"):
            if self.headers.get("Authorization") != f"Bearer {ACCESS_TOKEN}":
                return self._send(400, {"error": {"message": "Error occurred at the OAuth process."}})
            out = pixiv(u.path, q, base, method, form)
            if isinstance(out, tuple):
                return self._send(*out)
            return self._send(200 if out is not None else 404, out if out is not None else {"error": {}})
        self._send(404, {})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def requests_to(prefix):
    return [r for r in REQUESTS if r[1].startswith(prefix)]

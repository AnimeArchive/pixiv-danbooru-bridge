"""Danbooru endpoint implementations on top of pixiv + the tag mapping."""

import datetime
import logging
import random
import secrets
import threading
import time
from collections import OrderedDict

from .convert import Converter, apply_only, booru_name, parse_only, words
from .mapping import Mapping, key
from .pixiv import SEARCH_OFFSET_CAP, PixivClient, PixivError
from .query import Evaluator, Planner, QueryError, parse, parse_range, check

log = logging.getLogger("bridge.api")


class ApiError(Exception):
    def __init__(self, status, message, error="ApiError"):
        super().__init__(message)
        self.status, self.message, self.error = status, message, error


class NotFound(ApiError):
    def __init__(self, message="That record was not found."):
        super().__init__(404, message, "ActiveRecord::RecordNotFound")


class Result:
    def __init__(self, body, root, status=200):
        self.body, self.root, self.status = body, root, status


class ImageResult:
    def __init__(self, url):
        self.url = url


STATIC_METATAGS = {
    "order": ["id", "id_desc", "score", "favcount", "rank", "created_at", "created_at_asc", "views",
              "mpixels", "landscape", "portrait", "tagcount", "random"],
    "rating": ["general", "sensitive", "questionable", "explicit"],
    "is": ["sfw", "nsfw", "ai", "manga", "illust", "ugoira", "bookmarked"],
    "filetype": ["jpg", "png", "gif", "zip"],
}
EMPTY_INDEXES = {"pools", "wiki_pages", "comments", "post_versions", "forum_topics", "forum_posts",
                 "artist_commentaries", "favorite_groups", "dmails", "post_votes", "uploads", "notes",
                 "saved_searches", "bulk_update_requests", "pool_versions", "note_versions",
                 "wiki_page_versions", "artist_versions", "post_flags", "post_appeals", "tag_implications",
                 "post_approvals", "post_replacements", "iqdb_queries", "media_assets"}


def _p(params, name, default=None):
    v = params.get(name)
    return default if v in (None, "") else v


class Stream:
    """Lazily fetched, locally filtered sequence of posts for one query plan."""

    def __init__(self, bridge, plan, base):
        self.bridge, self.plan, self.base = bridge, plan, base
        self.posts = []          # (post, ctx) that passed the filter
        self.seen = set()
        self.exhausted = False
        self.lock = threading.Lock()
        self._next = None        # callable returning the next raw page, or None
        self._started = False
        self._search_offset = 0
        self._end_date = plan.end_date
        self.created = time.monotonic()

    # ---------------------------------------------------------- page sources
    def _first(self):
        p, px = self.plan, self.bridge.pixiv
        if p.source == "detail":
            illusts = []
            for iid in p.illust_ids:
                try:
                    illusts.append(px.illust_detail(iid))
                except PixivError as e:
                    if e.status != 404:
                        raise
            return {"illusts": illusts, "next_url": None}
        if p.source == "search":
            return self._search_page()
        if p.source == "user":
            return px.user_illusts(p.user_ids[0])
        if p.source == "bookmarks":
            return px.bookmarks(px.user_id)
        if p.source == "ranking":
            data = px.ranking(p.ranking_mode, getattr(p, "ranking_date", None))
            return data
        if p.source == "recommended":
            return px.recommended()
        if p.source == "new":
            return px.new()
        return px.follow()

    def _search_page(self):
        p = self.plan
        data = self.bridge.pixiv.search(p.word, p.sort, start_date=p.start_date, end_date=self._end_date,
                                        exclude_ai=p.exclude_ai, offset=self._search_offset)
        illusts = data.get("illusts") or []
        self._search_offset += len(illusts)
        if data.get("next_url") and illusts:
            if self._search_offset >= SEARCH_OFFSET_CAP and p.sort in ("date_desc", "date_asc"):
                # pixiv stops at offset 5000: continue from the last date instead (like gallery-dl)
                last = datetime.date.fromisoformat(illusts[-1]["create_date"][:10])
                if p.sort == "date_desc":
                    self._end_date = last
                else:
                    p.start_date = last
                self._search_offset = 0
            data["next"] = self._search_page
        return data

    def _fetch_more(self, pages):
        """Fetch up to `pages` raw pixiv pages. Returns False when exhausted."""
        for _ in range(pages):
            if self.exhausted:
                return False
            if not self._started:
                data = self._first()
                self._started = True
            elif callable(self._next):
                data = self._next()
            elif self._next:
                data = self.bridge.pixiv.next_page(self._next)
            else:
                self.exhausted = True
                return False
            self._next = data.get("next") or data.get("next_url")
            if not self._next:
                self.exhausted = True
            illusts = [i for i in (data.get("illusts") or []) if i.get("id") not in self.seen]
            self.seen |= {i["id"] for i in illusts}
            ev = self.bridge.evaluator
            for post, ctx in self.bridge.conv.posts(illusts, self.base):
                if self.plan.exclude_ai and ctx["is_ai"]:
                    continue
                if ev.match(self.plan.tree, ctx):
                    self.posts.append((post, ctx))
        return not self.exhausted

    def ensure(self, n, budget=None):
        """Make at least n filtered posts available (bounded by the scan budget)."""
        budget = self.bridge.cfg.max_scan_pages if budget is None else budget
        with self.lock:
            while len(self.posts) < n and not self.exhausted and budget > 0:
                self._fetch_more(1)
                budget -= 1
            return self.ordered()

    def ensure_until(self, pred, budget=None):
        budget = self.bridge.cfg.max_scan_pages if budget is None else budget
        with self.lock:
            while not pred(self.posts) and not self.exhausted and budget > 0:
                self._fetch_more(1)
                budget -= 1
            return self.ordered()

    def ordered(self):
        items = list(self.posts)
        ls = self.plan.local_sort
        if ls:
            field, desc = ls
            if field == "random":
                rnd = random.Random(id(self))
                rnd.shuffle(items)
            elif field == "custom" and self.plan.custom_ids:
                order = {v: i for i, v in enumerate(self.plan.custom_ids)}
                items.sort(key=lambda pc: order.get(pc[0]["id"], 1 << 60))
            else:
                getters = {"bookmarks": lambda c: c["bookmarks"], "views": lambda c: c["views"],
                           "mpixels": lambda c: c["width"] * c["height"],
                           "ratio": lambda c: c["width"] / c["height"] if c["height"] else 0,
                           "tagcount": lambda c: c["tag_count"]}
                items.sort(key=lambda pc: getters[field](pc[1]), reverse=desc)
        if self.plan.random_count:
            items = items[:self.plan.random_count]
        return items


class Bridge:
    def __init__(self, cfg, pixiv=None, mapping=None):
        self.cfg = cfg
        self.mapping = mapping or Mapping(cfg)
        secret = self.mapping.meta_get("url_secret")
        if not secret:
            secret = secrets.token_hex(32)
            self.mapping.meta_set("url_secret", secret)
        self.pixiv = pixiv or PixivClient(cfg, (lambda: self.mapping.meta_get("pixiv_refresh_token"),
                                                lambda t: self.mapping.meta_set("pixiv_refresh_token", t)))
        self.conv = Converter(cfg, self.mapping, secret + cfg.bridge_api_key)
        self.planner = Planner(cfg, self.mapping)
        self.evaluator = Evaluator(self.mapping, cfg)
        self.streams = OrderedDict()
        self._lock = threading.Lock()

    # =============================================================== helpers
    def _limit(self, params, default=None, maximum=None):
        try:
            n = int(_p(params, "limit", default or self.cfg.default_limit))
        except ValueError:
            n = default or self.cfg.default_limit
        if n <= 0:
            n = default or self.cfg.default_limit
        return min(n, maximum or self.cfg.max_limit)

    def plan(self, tags):
        try:
            return self.planner.plan(tags)
        except (QueryError, ValueError) as e:
            raise ApiError(422, f"invalid query: {e}", "PostQuery::Error")

    def stream(self, plan, base, fresh=False):
        k = (plan.cache_key(), base)
        with self._lock:
            s = self.streams.get(k)
            if s and not fresh and time.monotonic() - s.created < self.cfg.search_cache_ttl:
                self.streams.move_to_end(k)
                return s
            s = Stream(self, plan, base)
            self.streams[k] = s
            while len(self.streams) > 64:
                self.streams.popitem(last=False)
            return s

    @staticmethod
    def only(body, params):
        spec = _p(params, "only")
        return apply_only(body, parse_only(spec)) if spec else body

    def _page(self, stream, params, limit):
        page = str(_p(params, "page", "1")).strip()
        if page[:1] in ("a", "b") and page[1:].isdigit():
            cursor = int(page[1:])
            if page[0] == "b":
                items = stream.ensure_until(lambda ps: sum(1 for p, _ in ps if p["id"] < cursor) >= limit)
                return [p for p, _ in items if p["id"] < cursor][:limit]
            items = stream.ensure(limit)
            above = [p for p, _ in items if p["id"] > cursor]
            return above[-limit:] if stream.plan.sort == "date_desc" else above[:limit]
        try:
            n = max(1, int(page))
        except ValueError:
            raise ApiError(400, f"invalid page: {page}")
        items = stream.ensure(n * limit)
        return [p for p, _ in items[(n - 1) * limit:n * limit]]

    def wrap(self, fn):
        try:
            return fn()
        except PixivError as e:
            status = {401: 503, 429: 429, 404: 404}.get(e.status, 502)
            raise ApiError(status, f"pixiv: {e.message}", "PixivError")

    # =============================================================== posts
    def posts_index(self, params, base):
        def run():
            md5 = _p(params, "md5")
            if md5:
                raise NotFound("md5 lookups are not supported for pixiv")
            plan = self.plan(_p(params, "tags", ""))
            if plan.impossible:
                return Result([], "posts")
            if str(_p(params, "random", "")).lower() in ("true", "1", "yes"):
                plan.local_sort = ("random", False)
            limit = self._limit(params, plan.limit)
            posts = self._page(self.stream(plan, base, fresh=plan.local_sort == ("random", False)), params, limit)
            return Result(self.only(posts, params), "posts")
        return self.wrap(run)

    def posts_random(self, params, base):
        def run():
            plan = self.plan(_p(params, "tags", ""))
            items = self.stream(plan, base).ensure(30, budget=2)
            if not items:
                raise NotFound()
            return Result(self.only(random.choice(items)[0], params), "post")
        return self.wrap(run)

    def post_by_id(self, post_id, base):
        pid = int(post_id)
        illust = self.pixiv.illust_detail(pid // 1000)
        for post, _ in self.conv.posts([illust], base, pages_mode="all"):
            if post["id"] == pid:
                return post
        raise NotFound()

    def posts_show(self, post_id, params, base):
        return self.wrap(lambda: Result(self.only(self.post_by_id(post_id, base), params), "post"))

    def counts(self, params, base):
        def run():
            plan = self.plan(_p(params, "tags", ""))
            if plan.impossible:
                return Result({"counts": {"posts": 0}}, "counts")
            items = self.stream(plan, base).ensure(10 ** 9, budget=3)
            return Result({"counts": {"posts": len(items)}}, "counts")
        return self.wrap(run)

    def popular(self, params, base):
        def run():
            scale = _p(params, "scale", "day")
            mode = {"week": "week", "month": "month"}.get(scale, "day")
            plan = self.plan("")
            plan.source, plan.ranking_mode, plan.word_terms = "ranking", mode, []
            date = _p(params, "date")
            if date:
                plan.ranking_date = date[:10]
            limit = self._limit(params)
            return Result(self.only(self._page(self.stream(plan, base), params, limit), params), "posts")
        return self.wrap(run)

    # =============================================================== tags
    def tags_index(self, params):
        cats = _p(params, "search[category]")
        categories = [int(c) for c in str(cats).split(",") if c.strip().isdigit()] if cats else None
        limit = self._limit(params, 20, 1000)
        order = _p(params, "search[order]", "count")
        try:
            page = max(1, int(_p(params, "page", "1")))
        except ValueError:
            page = 1
        names = _p(params, "search[name]") or _p(params, "search[name_comma]") or _p(params, "search[name_normalize]")
        pattern = (_p(params, "search[name_matches]") or _p(params, "search[name_or_alias_matches]")
                   or _p(params, "search[fuzzy_name_matches]") or "*")
        rows = []
        if names:
            for n in str(names).replace(" ", ",").split(","):
                if not n.strip():
                    continue
                name = booru_name(n)
                cat, count = self.mapping.category(name)
                if cat is not None and (categories is None or cat in categories):
                    rows.append((name, cat, count))
        else:
            rows = self.mapping.search_tags(booru_name(pattern), categories, limit, (page - 1) * limit,
                                            "name" if order == "name" else "count")
        return Result(self.only([self.conv.tag(n, c, pc) for n, c, pc in rows], params), "tags")

    def tags_show(self, tag_id):
        hit = self.conv.lookup_id(int(tag_id))
        if not hit:
            raise NotFound()
        cat, count = self.mapping.category(hit[0])
        return Result(self.conv.tag(hit[0], hit[1], count), "tag")

    def autocomplete(self, params):
        query = _p(params, "search[query]") or _p(params, "query") or _p(params, "search[name_matches]") or ""
        kind = _p(params, "search[type]", "tag_query")
        limit = self._limit(params, 10, 100)
        q = query.strip().lower()
        while q[:1] in ("-", "~"):
            q = q[1:]
        if not q or kind not in ("tag_query", "tag", "artist"):
            return Result([], "autocomplete")
        if ":" in q:
            meta, val = q.split(":", 1)
            if meta in STATIC_METATAGS:
                return Result([{"type": "static", "label": o, "value": f"{meta}:{o}"}
                               for o in STATIC_METATAGS[meta] if o.startswith(val)][:limit], "autocomplete")
        cats = [1] if kind == "artist" else None
        pattern = q if "*" in q else booru_name(q) + "*"
        rows = self.mapping.search_tags(pattern, cats, limit)
        if not rows and not q.isascii():
            # Japanese input: ask pixiv, then map its suggestions back to Danbooru tags
            try:
                sugg = self.pixiv.autocomplete(query.strip())
            except PixivError:
                sugg = []
            mapped = self.mapping.to_danbooru([s["name"] for s in sugg])
            seen = set()
            for s in sugg:
                for d in mapped.get(s["name"]) or []:
                    if d not in seen:
                        seen.add(d)
                        cat, count = self.mapping.category(d)
                        rows.append((d, cat or 0, count))
        out = []
        for name, cat, count in rows[:limit]:
            if kind == "artist":
                out.append({"type": "artist", "label": name.replace("_", " "), "value": name, "category": cat})
            else:
                out.append({"type": "tag" if name.startswith(booru_name(q)) else "tag-word",
                            "label": name.replace("_", " "), "value": name, "category": cat,
                            "post_count": count, "tag": self.conv.tag(name, cat, count)})
        return Result(out, "autocomplete")

    def legacy_tag_autocomplete(self, params):
        pattern = _p(params, "search[name_matches]") or _p(params, "search[name]") or _p(params, "query") or ""
        if pattern and "*" not in pattern:
            pattern += "*"
        return self.tags_index({"search[name_matches]": pattern, "limit": _p(params, "limit", "10")})

    def related_tag(self, params, base):
        def run():
            query = _p(params, "query") or _p(params, "search[query]") or ""
            limit = self._limit(params, 25, 1000)
            plan = self.plan(query)
            items = self.stream(plan, base).ensure(90, budget=3)
            from collections import Counter
            counter = Counter()
            for _, ctx in items:
                counter.update(ctx["tags"])
            n = max(1, len(items))
            rel = []
            for name, c in counter.most_common(limit):
                cat, count = self.mapping.category(name)
                rel.append({"tag": self.conv.tag(name, cat or 0, count), "cosine_similarity": c / n,
                            "jaccard_similarity": c / n, "overlap_coefficient": c / n, "frequency": c / n})
            qname = booru_name(query) if query and " " not in query.strip() else None
            cat, count = self.mapping.category(qname) if qname else (0, 0)
            return Result({"query": query, "post_count": len(items),
                           "tag": self.conv.tag(qname, cat or 0, count) if qname else None,
                           "related_tags": rel, "wiki_page_tags": []}, "related-tag")
        return self.wrap(run)

    def tag_aliases(self, params):
        name = _p(params, "search[consequent_name]") or _p(params, "search[name_matches]")
        ant = _p(params, "search[antecedent_name]")
        rows = []
        if name:
            tag = booru_name(name)
            rows = [(booru_name(a), tag) for a in self.mapping.aliases(tag)]
        elif ant:
            mapped = self.mapping.to_danbooru([ant])
            rows = [(booru_name(ant), d) for d in mapped.get(ant) or []]
        from .convert import boot_ts, stable_id
        body = [{"id": stable_id(f"{a}->{c}"), "antecedent_name": a, "reason": "pixiv name (Danbooru wiki other_names)",
                 "creator_id": 1, "consequent_name": c, "status": "active", "forum_topic_id": None,
                 "created_at": boot_ts(), "updated_at": boot_ts(), "approver_id": 1, "forum_post_id": None}
                for a, c in rows if a != c]
        return Result(body, "tag-aliases")

    # =============================================================== artists
    def _artist(self, name):
        from .convert import boot_ts
        users = self.mapping.users_for_artist(name)
        return {"id": self.conv.register(name, 1), "name": name, "group_name": "", "other_names": [],
                "is_banned": False, "is_deleted": False, "created_at": boot_ts(), "updated_at": boot_ts(),
                "urls": [{"id": u, "artist_id": self.conv.register(name, 1), "url": f"https://www.pixiv.net/users/{u}",
                          "is_active": True} for u in users]}

    def artists_index(self, params):
        name = (_p(params, "search[name]") or _p(params, "search[any_name_matches]")
                or _p(params, "search[name_matches]") or _p(params, "search[any_name_or_url_matches]"))
        url = _p(params, "search[url_matches]")
        if url:
            from .mapping import PIXIV_USER_RE
            m = PIXIV_USER_RE.search(url)
            a = self.mapping.artist_for_user(int(m.group(1))) if m else None
            return Result([self._artist(a)] if a else [], "artists")
        if not name:
            return Result([], "artists")
        rows = self.mapping.search_tags(booru_name(name), [1], self._limit(params, 20, 1000), mapped_only=False)
        return Result(self.only([self._artist(n) for n, _, _ in rows], params), "artists")

    def artists_show(self, artist_id):
        hit = self.conv.lookup_id(int(artist_id))
        if not hit or hit[1] != 1:
            raise NotFound()
        return Result(self._artist(hit[0]), "artist")

    # =============================================================== favorites
    def favorites_index(self, params, base):
        def run():
            plan = self.plan("fav:me")
            posts = self._page(self.stream(plan, base), params, self._limit(params, 20, 1000))
            return Result([{"id": p["id"], "user_id": self.pixiv.user_id, "post_id": p["id"]} for p in posts],
                          "favorites")
        return self.wrap(run)

    def favorites_create(self, params):
        self._require_writes()
        post_id = int(_p(params, "post_id") or _p(params, "favorite[post_id]") or 0)
        if not post_id:
            raise ApiError(422, "post_id is required")

        def run():
            self.pixiv.bookmark_add(post_id // 1000)
            self._invalidate()
            return Result({"id": post_id, "user_id": self.pixiv.user_id, "post_id": post_id}, "favorite", 201)
        return self.wrap(run)

    def favorites_delete(self, post_id):
        self._require_writes()

        def run():
            self.pixiv.bookmark_delete(int(post_id) // 1000)
            self._invalidate()
            return Result(None, "favorite", 204)
        return self.wrap(run)

    def _invalidate(self):
        with self._lock:
            self.streams.clear()

    def _require_writes(self):
        if not self.cfg.allow_writes:
            raise ApiError(403, "this bridge is read-only (ALLOW_WRITES=false)", "User::PrivilegeError")

    # =============================================================== users
    def profile(self, authed):
        if not authed:
            return Result(self.conv.anonymous(), "user")
        return self.wrap(lambda: (self.pixiv.authenticate(), Result(self.conv.user(self.pixiv.user), "user"))[1])

    def users_show(self, uid):
        def run():
            if int(uid) == self.pixiv.user_id:
                return Result(self.conv.user(self.pixiv.user, private=False), "user")
            data = self.pixiv.user_detail(int(uid))
            return Result(self.conv.user(data.get("user") or {}, private=False), "user")
        return self.wrap(run)

    # =============================================================== images/status
    def image(self, sig, path):
        if not self.conv.verify(sig, path):
            raise ApiError(403, "bad image signature", "User::PrivilegeError")
        return ImageResult("https://i.pximg.net/" + path)

    def status(self):
        st = self.mapping.stats()
        try:
            self.pixiv.authenticate()
            px = {"logged_in_as": self.pixiv.user.get("account"), "premium": self.pixiv.user.get("is_premium")}
            ok = True
        except PixivError as e:
            px, ok = {"error": e.message}, False
        return Result({"bridge": "pixiv-danbooru-bridge", "pixiv_ok": ok, "pixiv": px, "mapping": st}, "status")

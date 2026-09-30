"""Danbooru search query -> pixiv request plan + exact local filter.

Pixiv's search only understands a word string (AND by spaces, "-exclude",
"OR") and a few parameters (sort, date range, AI filter). So a query is handled
in two layers:

1. the *planner* picks a pixiv source (tag search, an artist's works, your
   bookmarks, single works, ranking or a feed) and builds the narrowest pixiv
   request it can, translating Danbooru tags to pixiv tags;
2. the *filter* evaluates the complete Danbooru query against every fetched
   post (mapped tags, size, rating, bookmarks, dates, ...), so results are
   exact even for syntax pixiv cannot express.
"""

import datetime
import fnmatch
import re

from .mapping import key

MAX_LOCAL = 1 << 62


class QueryError(Exception):
    pass


# ============================================================ tokenizer
def tokenize(query):
    tokens, cur, quote = [], [], False
    for ch in query:
        if ch == '"':
            quote = not quote
            continue
        if ch.isspace() and not quote:
            if cur:
                tokens.append("".join(cur))
                cur = []
            continue
        cur.append(ch)
    if cur:
        tokens.append("".join(cur))
    out, depth = [], 0
    for tok in tokens:
        while tok:
            if tok == "(":
                out.append("(")
                depth += 1
                tok = ""
            elif tok[:2] in ("-(", "~(") and tok[1:].count("(") > tok[1:].count(")"):
                out += [tok[0], "("]
                depth += 1
                tok = tok[2:]
            elif tok[0] == "(" and tok.count("(") > tok.count(")"):
                out.append("(")
                depth += 1
                tok = tok[1:]
            else:
                break
        closes = 0
        while tok.endswith(")") and depth > 0 and (tok == ")" or tok.count(")") > tok.count("(")):
            tok = tok[:-1]
            closes += 1
            depth -= 1
        if tok:
            out.append(tok)
        out += [")"] * closes
    return out


# ============================================================ nodes
class Tag:
    def __init__(self, name):
        self.name = name.lower()


class Meta:
    def __init__(self, k, v):
        self.k, self.v = k, v


class And:
    def __init__(self, items):
        self.items = items


class Or:
    def __init__(self, items):
        self.items = items


class Not:
    def __init__(self, item):
        self.item = item


METATAGS = {
    "rating", "order", "limit", "random", "id", "pixiv", "pixiv_id", "width", "height", "ratio",
    "mpixels", "filetype", "date", "age", "score", "favcount", "upvotes", "status", "is", "has",
    "source", "user", "fav", "ordfav", "tagcount", "gentags", "arttags", "chartags", "copytags",
    "metatags", "md5", "parent", "child", "pool", "commenter", "comm", "noter", "approver",
    "comments", "notes", "views", "pages",
}


def parse(query):
    p = _Parser(tokenize(query or ""))
    tree = p.parse_or()
    return tree


class _Parser:
    def __init__(self, toks):
        self.toks, self.i = toks, 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def next(self):
        t = self.peek()
        self.i += 1
        return t

    def parse_or(self):
        left = self.parse_and()
        while self.peek() is not None and self.peek().lower() == "or":
            self.next()
            left = Or([left, self.parse_and()])
        return left

    def parse_and(self):
        items, tildes = [], []
        while True:
            t = self.peek()
            if t is None or t == ")" or t.lower() == "or":
                break
            if t == "~" or (t.startswith("~") and len(t) > 1):
                self.next()
                tildes.append(self.parse_unary() if t == "~" else self.term_or_neg(t[1:]))
            elif t == "-":
                self.next()
                items.append(Not(self.parse_unary()))
            else:
                items.append(self.parse_unary())
            if self.i >= len(self.toks) and t is None:
                break
        if tildes:
            items.append(Or(tildes))
        return items[0] if len(items) == 1 else And(items)

    def parse_unary(self):
        t = self.next()
        if t == "(":
            node = self.parse_or()
            if self.peek() == ")":
                self.next()
            return node
        if t == "-":
            return Not(self.parse_unary())
        return self.term_or_neg(t)

    def term_or_neg(self, t):
        if t.startswith("-") and len(t) > 1:
            return Not(self.term(t[1:]))
        return self.term(t)

    @staticmethod
    def term(tok):
        if ":" in tok:
            k, v = tok.split(":", 1)
            if k.lower() in METATAGS and v != "":
                return Meta(k.lower(), v)
        return Tag(tok)


def top_level(tree):
    items, stack = [], [tree]
    while stack:
        n = stack.pop(0)
        if isinstance(n, And):
            stack[:0] = n.items
        else:
            items.append(n)
    return items


# ============================================================ value helpers
_RANGE_OP = re.compile(r"^(>=|<=|>|<|=)?(.*)$")


def parse_range(value, conv):
    v = value.strip()
    if "," in v and ".." not in v:
        return [("in", [conv(x) for x in v.split(",") if x])]
    for sep, hi_op in (("...", "<"), ("..", "<=")):
        if sep in v:
            lo, hi = v.split(sep, 1)
            out = []
            if lo:
                out.append((">=", conv(lo)))
            if hi:
                out.append((hi_op, conv(hi)))
            return out
    m = _RANGE_OP.match(v)
    return [(m.group(1) or "=", conv(m.group(2)))]


def check(value, op, n):
    if value is None:
        return False
    if op == "in":
        return value in n
    return {"=": value == n, "!=": value != n, ">": value > n, "<": value < n,
            ">=": value >= n, "<=": value <= n}[op]


def in_range(value, spec, conv):
    return all(check(value, op, n) for op, n in parse_range(spec, conv))


def parse_date(s):
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise QueryError(f"bad date: {s}")


_AGE = re.compile(r"^\s*([\d.]+)\s*(s|sec|seconds?|mi|min|minutes?|h|hours?|d|days?|w|weeks?|mo|months?|y|years?)\s*$", re.I)


def parse_age(s):
    m = _AGE.match(s)
    if not m:
        raise QueryError(f"bad age: {s}")
    n, u = float(m.group(1)), m.group(2).lower()
    mult = (1 if u.startswith("s") else 60 if u.startswith("mi") else 2592000 if u.startswith("mo")
            else 3600 if u.startswith("h") else 86400 if u.startswith("d") else 604800 if u.startswith("w")
            else 31536000)
    return n * mult


def ratio_value(s):
    for sep in (":", "/"):
        if sep in s:
            a, b = s.split(sep, 1)
            return float(a) / float(b)
    return float(s)


RATING_ALIASES = {"general": "g", "safe": "g", "sensitive": "s", "questionable": "q", "explicit": "e"}
FILETYPES = {"jpg": {"jpg", "jpeg"}, "jpeg": {"jpg", "jpeg"}, "png": {"png"}, "gif": {"gif"},
             "zip": {"zip"}, "ugoira": {"zip"}, "webp": {"webp"}}

SORTS = {
    "": ("date_desc", None), "id": ("date_asc", None), "id_asc": ("date_asc", None),
    "id_desc": ("date_desc", None), "created_at": ("date_desc", None),
    "created_at_asc": ("date_asc", None), "created_at_desc": ("date_desc", None),
    "date": ("date_desc", None), "date_asc": ("date_asc", None),
    # pixiv popularity sort needs pixiv Premium; ranking is used for empty queries
    "score": ("popular_desc", None), "favcount": ("popular_desc", None), "rank": ("popular_desc", None),
    "popular": ("popular_desc", None),
    # sorted in the bridge within the scanned window
    "score_asc": ("date_desc", ("bookmarks", False)), "favcount_asc": ("date_desc", ("bookmarks", False)),
    "views": ("date_desc", ("views", True)), "views_asc": ("date_desc", ("views", False)),
    "mpixels": ("date_desc", ("mpixels", True)), "mpixels_asc": ("date_desc", ("mpixels", False)),
    "landscape": ("date_desc", ("ratio", True)), "portrait": ("date_desc", ("ratio", False)),
    "tagcount": ("date_desc", ("tagcount", True)), "tagcount_asc": ("date_desc", ("tagcount", False)),
    "random": ("date_desc", ("random", False)), "none": ("date_desc", None), "custom": ("date_desc", ("custom", False)),
}


# ============================================================ plan
class Plan:
    def __init__(self):
        self.source = "search"   # search | user | bookmarks | detail | ranking | follow | recommended | new
        self.word_terms = []     # pixiv search words
        self.sort = "date_desc"
        self.local_sort = None   # (field, descending)
        self.start_date = None
        self.end_date = None
        self.exclude_ai = False
        self.user_ids = []
        self.illust_ids = []
        self.custom_ids = None
        self.limit = None
        self.random_count = None
        self.ranking_mode = "day"
        self.tree = None
        self.impossible = False
        self.notes = []

    @property
    def word(self):
        return " ".join(self.word_terms)

    def cache_key(self):
        import json
        return json.dumps([self.source, self.word, self.sort, self.start_date, self.end_date, self.exclude_ai,
                           self.user_ids, self.illust_ids, self.ranking_mode, self._tree_key(self.tree),
                           self.local_sort, self.random_count], default=str)

    def _tree_key(self, n):
        if isinstance(n, Tag):
            return n.name
        if isinstance(n, Meta):
            return f"{n.k}:{n.v}"
        if isinstance(n, Not):
            return ["not", self._tree_key(n.item)]
        if isinstance(n, (And, Or)):
            return [type(n).__name__, [self._tree_key(i) for i in n.items]]
        return None


class Planner:
    def __init__(self, cfg, mapping):
        self.cfg, self.mapping = cfg, mapping

    def pixiv_word(self, tag):
        if tag in self.cfg.tag_overrides:
            return self.cfg.tag_overrides[tag]
        names = self.mapping.pixiv_names(tag)
        return names[0] if names else tag

    def plan(self, query):
        p = Plan()
        tree = parse(query)
        p.tree = tree
        items = top_level(tree)
        positive_tags, artist_tag, or_group = [], None, None
        order = ""
        for n in items:
            if isinstance(n, Meta):
                k, v = n.k, n.v
                vl = v.lower()
                if k == "order":
                    order = vl
                elif k == "limit":
                    p.limit = int(v)
                elif k == "random":
                    p.random_count = int(v)
                elif k in ("pixiv", "pixiv_id") and vl.isdigit():
                    p.source, p.illust_ids = "detail", [int(vl)]
                elif k == "id":
                    spec = parse_range(v, int)
                    if len(spec) == 1 and spec[0][0] in ("=", "in"):
                        ids = spec[0][1] if spec[0][0] == "in" else [spec[0][1]]
                        p.source = "detail"
                        p.illust_ids = list(dict.fromkeys(i // 1000 for i in ids))
                        p.custom_ids = ids
                elif k in ("fav", "ordfav"):
                    if p.source == "search":
                        p.source = "bookmarks"
                elif k == "user" and vl.isdigit():
                    if p.source == "search":
                        p.source, p.user_ids = "user", [int(vl)]
                elif k == "date":
                    for op, d in parse_range(v, parse_date):
                        if op in ("=", ">=", ">"):
                            p.start_date = d + datetime.timedelta(days=1 if op == ">" else 0)
                        if op in ("=", "<=", "<"):
                            p.end_date = d - datetime.timedelta(days=1 if op == "<" else 0)
                        if op == "in":
                            p.start_date, p.end_date = min(d), max(d)
                elif k == "age":
                    today = datetime.date.today()
                    for op, secs in parse_range(v, parse_age):
                        d = today - datetime.timedelta(seconds=secs)
                        if op in ("<", "<="):
                            p.start_date = d
                        elif op in (">", ">="):
                            p.end_date = d
                elif k == "is" and vl in ("pending", "flagged", "banned", "deleted", "appealed", "modqueue",
                                          "parent", "child"):
                    p.impossible = True
            elif isinstance(n, Not) and isinstance(n.item, Tag) and "*" not in n.item.name:
                t = n.item.name
                if t in ("ai-generated", "ai_generated"):
                    p.exclude_ai = True
                else:
                    p.word_terms.append("-" + self.pixiv_word(t))
            elif isinstance(n, Not) and isinstance(n.item, Meta) and n.item.k == "is" and n.item.v.lower() == "ai":
                p.exclude_ai = True
            elif isinstance(n, Tag) and "*" not in n.name:
                cat, _ = self.mapping.category(n.name)
                users = self.mapping.users_for_artist(n.name) if cat == 1 else []
                if users and artist_tag is None:
                    artist_tag = (n.name, users)
                else:
                    positive_tags.append(n.name)
            elif isinstance(n, Or) and all(isinstance(i, Tag) and "*" not in i.name for i in n.items):
                or_group = [i.name for i in n.items]

        if order not in SORTS:
            raise QueryError(f"unknown order: {order}")
        p.sort, p.local_sort = SORTS[order]
        if self.cfg.hide_ai:
            p.exclude_ai = True
        if p.source in ("search",) and artist_tag:
            p.source, p.user_ids = "user", artist_tag[1]
        if p.source == "search":
            if positive_tags:
                p.word_terms[:0] = [self.pixiv_word(t) for t in positive_tags]
            elif or_group:
                p.word_terms[:0] = [" OR ".join(self.pixiv_word(t) for t in or_group)]
            if not any(not w.startswith("-") for w in p.word_terms):
                # nothing pixiv can search for: browse a feed and filter locally
                p.word_terms = []
                if order in ("score", "favcount", "rank", "popular"):
                    p.source = "ranking"
                else:
                    p.source = self.cfg.empty_query_source
        if p.random_count:
            p.local_sort = ("random", False)
        return p


# ============================================================ local filter
class Evaluator:
    """Evaluates a parsed query against a post context (see convert.post_context)."""

    def __init__(self, mapping, cfg):
        self.mapping, self.cfg = mapping, cfg
        self._fwd = {}

    def forward_keys(self, tag):
        if tag not in self._fwd:
            self._fwd[tag] = {key(n) for n in self.mapping.pixiv_names(tag)}
        return self._fwd[tag]

    def match(self, node, ctx):
        if node is None:
            return True
        if isinstance(node, And):
            return all(self.match(i, ctx) for i in node.items)
        if isinstance(node, Or):
            return any(self.match(i, ctx) for i in node.items)
        if isinstance(node, Not):
            return not self.match(node.item, ctx)
        if isinstance(node, Tag):
            return self.tag(node.name, ctx)
        return self.meta(node.k, node.v, ctx)

    def tag(self, name, ctx):
        if "*" in name:
            return any(fnmatch.fnmatchcase(t, name) for t in ctx["tags"])
        if name in ctx["tags"] or key(name) in ctx["raw_keys"]:
            return True
        return bool(self.forward_keys(name) & ctx["raw_keys"])

    def meta(self, k, v, c):
        vl = v.lower()
        if k in ("order", "limit", "random"):
            return True
        if k in ("fav", "ordfav"):
            return c["is_bookmarked"]
        if k == "rating":
            return c["rating"] in {RATING_ALIASES.get(r, r) for r in vl.split(",")}
        if k == "id":
            return in_range(c["id"], v, int)
        if k in ("pixiv", "pixiv_id"):
            if vl == "any":
                return True
            if vl == "none":
                return False
            return in_range(c["illust_id"], v, int)
        if k in ("width", "height"):
            return in_range(c[k], v, lambda s: int(float(s)))
        if k == "mpixels":
            return in_range(c["width"] * c["height"] / 1e6, v, float)
        if k == "ratio":
            r = c["width"] / c["height"] if c["height"] else 0
            return all((abs(r - n) < 0.01) if op == "=" else
                       (any(abs(r - x) < 0.01 for x in n) if op == "in" else check(r, op, n))
                       for op, n in parse_range(v, ratio_value))
        if k in ("score", "favcount", "upvotes"):
            return in_range(c["bookmarks"], v, int)
        if k == "views":
            return in_range(c["views"], v, int)
        if k == "pages":
            return in_range(c["page_count"], v, int)
        if k == "filetype":
            exts = set().union(*(FILETYPES.get(x.strip(), {x.strip()}) for x in vl.split(",")))
            return c["ext"] in exts
        if k == "date":
            return in_range(c["date"].date(), v, parse_date)
        if k == "age":
            age = (datetime.datetime.now(datetime.timezone.utc) - c["date"]).total_seconds()
            return in_range(age, v, parse_age)
        if k in ("tagcount", "gentags", "arttags", "chartags", "copytags", "metatags"):
            field = {"tagcount": "tag_count", "gentags": 0, "arttags": 1, "chartags": 4,
                     "copytags": 3, "metatags": 5}[k]
            n = c["tag_count"] if field == "tag_count" else c["cat_counts"].get(field, 0)
            return in_range(n, v, int)
        if k == "user":
            return vl in (str(c["user_id"]), c["user_account"].lower(), c["user_name"].lower())
        if k == "source":
            if vl == "none":
                return False
            pat = vl if "*" in vl else vl + "*"
            return fnmatch.fnmatch(c["source"].lower(), pat)
        if k == "status":
            return vl in ("any", "all", "active")
        if k == "is":
            return self._is(vl, c)
        if k == "has":
            return vl == "source"
        if k == "md5":
            return c["md5"] in vl.split(",")
        if k in ("parent", "child", "pool"):
            return vl == "none"
        if k in ("commenter", "comm", "noter", "approver"):
            return vl == "none"
        if k in ("comments", "notes"):
            return in_range(0, v, int)
        return True

    def _is(self, v, c):
        if v in RATING_ALIASES:
            return c["rating"] == RATING_ALIASES[v]
        if v == "sfw":
            return c["rating"] in ("g", "s")
        if v == "nsfw":
            return c["rating"] in ("q", "e")
        if v in FILETYPES:
            return c["ext"] in FILETYPES[v]
        if v == "ai":
            return c["is_ai"]
        if v in ("favorited", "bookmarked"):
            return c["is_bookmarked"]
        if v == "manga":
            return c["type"] == "manga"
        if v in ("illust", "illustration"):
            return c["type"] == "illust"
        if v in ("animated", "ugoira"):
            return c["type"] == "ugoira"
        if v == "active":
            return True
        return False

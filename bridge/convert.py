"""pixiv illusts -> Danbooru JSON/XML objects."""

import datetime
import hashlib
import hmac
import re
import unicodedata
from xml.sax.saxutils import escape

from .config import CAT_ARTIST, CAT_CHARACTER, CAT_COPYRIGHT, CAT_GENERAL, CAT_META
from .mapping import key

_WORD_SPLIT = re.compile(r"[_\W]+", re.UNICODE)
RATING_TAGS = {"r-18", "r-18g", "r18", "r18g"}
BOOT_TIME = datetime.datetime.now().astimezone()


def words(name):
    return [w for w in _WORD_SPLIT.split(name) if w]


def booru_name(text):
    return unicodedata.normalize("NFKC", text).strip().replace(" ", "_").lower()


def stable_id(text):
    import zlib
    return zlib.crc32(text.encode("utf-8")) & 0x7FFFFFFF or 1


def iso(dt):
    return dt.astimezone().isoformat(timespec="milliseconds")


def boot_ts():
    return BOOT_TIME.isoformat(timespec="milliseconds")


def parse_pixiv_date(s):
    try:
        return datetime.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return BOOT_TIME


def fit(w, h, bw, bh=None):
    bh = bh or bw
    if not w or not h:
        return bw, bh
    s = min(1.0, bw / w, bh / h)
    return max(1, round(w * s)), max(1, round(h * s))


def rating_of(illust):
    if illust.get("x_restrict"):
        return "e"
    sl = illust.get("sanity_level") or 2
    return "g" if sl <= 2 else "s" if sl <= 4 else "q"


def page_urls(illust, page):
    if illust.get("page_count", 1) <= 1 or not illust.get("meta_pages"):
        u = dict(illust.get("image_urls") or {})
        u["original"] = (illust.get("meta_single_page") or {}).get("original_image_url") or u.get("large")
        return u
    pages = illust["meta_pages"]
    return dict(pages[page]["image_urls"]) if page < len(pages) else None


def ugoira_zip(original):
    """gallery-dl's derivation: img-original/..._ugoira0.jpg -> img-zip-ugoira/..._ugoira1920x1080.zip"""
    m = re.match(r"(https://i\.pximg\.net/)img-original(/img/.+?_ugoira)0\.\w+$", original or "")
    return f"{m.group(1)}img-zip-ugoira{m.group(2)}1920x1080.zip" if m else None


class Converter:
    def __init__(self, cfg, mapping, secret):
        self.cfg, self.mapping = cfg, mapping
        self.secret = secret.encode()
        self._ids = {}

    # ------------------------------------------------------------- image urls
    def sign(self, path):
        return hmac.new(self.secret, path.encode(), hashlib.sha256).hexdigest()[:20]

    def image_url(self, base, pximg_url):
        path = pximg_url.split("://i.pximg.net/", 1)[1]
        return f"{base}/img/{self.sign(path)}/{path}"

    def verify(self, sig, path):
        return hmac.compare_digest(self.sign(path), sig)

    # ------------------------------------------------------------- tags
    def register(self, name, category):
        tid = stable_id(name)
        self._ids[tid] = (name, category)
        return tid

    def lookup_id(self, tid):
        return self._ids.get(tid)

    def illust_tags(self, illust, mapped, artists, remember):
        """-> ({category: set(names)}, raw_keys). `remember` collects stand-in
        names for pixiv tags/artists Danbooru doesn't know, so they can be searched."""
        cats = {CAT_GENERAL: set(), CAT_ARTIST: set(), CAT_COPYRIGHT: set(), CAT_CHARACTER: set(), CAT_META: set()}
        raw_keys = set()
        for t in illust.get("tags") or []:
            name, tr = t.get("name") or "", t.get("translated_name")
            raw_keys.add(key(name))
            if tr:
                raw_keys.add(key(tr))
            if name.lower() in RATING_TAGS:
                continue
            dans = mapped.get(name)
            if dans:
                for dan in dans:  # one pixiv tag can be several Danbooru tags
                    cat, _ = self.mapping.category(dan)
                    cats.setdefault(cat if cat is not None else CAT_GENERAL, set()).add(dan)
                continue
            style = self.cfg.unmapped_tag_style
            if style == "drop":
                continue
            label = booru_name(tr if (style == "translated" and tr) else name)
            cats[CAT_GENERAL].add(label)
            remember["tags"].append((label, name))
        user = illust.get("user") or {}
        uid = user.get("id")
        artist = artists.get(int(uid)) if uid else None
        if not artist:
            artist = self.mapping.local_artist_name(uid, user.get("account") or user.get("name") or "")
            remember["users"].append((uid, user.get("account") or "", user.get("name") or "", artist))
        cats[CAT_ARTIST].add(artist)
        raw_keys |= {key(user.get("account") or ""), key(user.get("name") or "")}
        w, h = illust.get("width") or 0, illust.get("height") or 0
        if w >= 1600 or h >= 1200:
            cats[CAT_META].add("highres")
        if w >= 3200 or h >= 2400:
            cats[CAT_META].add("absurdres")
        if illust.get("illust_ai_type") == 2:
            cats[CAT_META].add("ai-generated")
        if illust.get("type") == "ugoira":
            cats[CAT_META] |= {"animated", "ugoira"}
        if illust.get("type") == "manga":
            cats[CAT_GENERAL].add("comic")
        if (illust.get("page_count") or 1) > 1:
            cats[CAT_META].add("multiple_images")
        return cats, raw_keys

    # ------------------------------------------------------------- posts
    def posts(self, illusts, base, pages_mode=None):
        """-> list of (post_dict, context) for every visible page."""
        pages_mode = pages_mode or self.cfg.pages_mode
        illusts = [i for i in illusts if i.get("visible", True)]
        all_tags = {t.get("name") for i in illusts for t in (i.get("tags") or []) if t.get("name")}
        translations = {t["name"]: t.get("translated_name") for i in illusts for t in (i.get("tags") or [])
                        if t.get("name") and t.get("translated_name")}
        mapped = self.mapping.to_danbooru(sorted(all_tags), translations)
        artists = self.mapping.artists_for_users([(i.get("user") or {}).get("id") for i in illusts])
        remember = {"tags": [], "users": []}
        out = []
        for illust in illusts:
            cats, raw_keys = self.illust_tags(illust, mapped, artists, remember)
            n_pages = illust.get("page_count") or 1
            for page in range(n_pages if pages_mode == "all" else 1):
                urls = page_urls(illust, page)
                if not urls or not urls.get("original") or "s.pximg.net" in urls["original"]:
                    continue
                out.append(self.post(illust, page, urls, cats, raw_keys, base))
        self.mapping.remember_local(remember["tags"], remember["users"])
        return out

    def post(self, illust, page, urls, cats, raw_keys, base):
        iid = illust["id"]
        pid = iid * 1000 + page
        w, h = illust.get("width") or 0, illust.get("height") or 0
        original = urls["original"]
        is_ugoira = illust.get("type") == "ugoira"
        file_src = (ugoira_zip(original) if is_ugoira else None) or original
        ext = file_src.rsplit(".", 1)[-1].lower().split("?")[0]
        md5 = hashlib.md5(file_src.encode()).hexdigest()
        created = parse_pixiv_date(illust.get("create_date"))
        rating = rating_of(illust)
        bookmarks = illust.get("total_bookmarks") or 0
        all_tags = sorted(set().union(*cats.values()))
        medium, large = urls.get("medium") or original, urls.get("large") or original
        mw, mh = fit(w, h, 540)
        lw, lh = fit(w, h, 600, 1200)
        variants = [
            {"type": "180x180", "url": self.image_url(base, medium), "width": mw, "height": mh, "file_ext": "jpg"},
            {"type": "360x360", "url": self.image_url(base, medium), "width": mw, "height": mh, "file_ext": "jpg"},
            {"type": "720x720", "url": self.image_url(base, large), "width": lw, "height": lh, "file_ext": "jpg"},
            {"type": "sample", "url": self.image_url(base, large), "width": lw, "height": lh, "file_ext": "jpg"},
            {"type": "original", "url": self.image_url(base, file_src), "width": w, "height": h, "file_ext": ext},
        ]
        source = f"https://www.pixiv.net/artworks/{iid}"
        user = illust.get("user") or {}
        post = {
            "id": pid,
            "created_at": iso(created),
            "uploader_id": user.get("id") or 0,
            "score": bookmarks,
            "source": source,
            "md5": md5,
            "last_comment_bumped_at": None,
            "rating": rating,
            "image_width": w,
            "image_height": h,
            "tag_string": " ".join(all_tags),
            "fav_count": bookmarks,
            "file_ext": ext,
            "last_noted_at": None,
            "parent_id": iid * 1000 if page else None,
            "has_children": page == 0 and (illust.get("page_count") or 1) > 1,
            "approver_id": None,
            "tag_count_general": len(cats[CAT_GENERAL]),
            "tag_count_artist": len(cats[CAT_ARTIST]),
            "tag_count_character": len(cats[CAT_CHARACTER]),
            "tag_count_copyright": len(cats[CAT_COPYRIGHT]),
            "file_size": 0,
            "up_score": bookmarks,
            "down_score": 0,
            "is_pending": False,
            "is_flagged": False,
            "is_deleted": False,
            "tag_count": len(all_tags),
            "updated_at": iso(created),
            "is_banned": False,
            "pixiv_id": iid,
            "last_commented_at": None,
            "has_active_children": page == 0 and (illust.get("page_count") or 1) > 1,
            "bit_flags": 0,
            "tag_count_meta": len(cats[CAT_META]),
            "has_large": True,
            "has_visible_children": page == 0 and (illust.get("page_count") or 1) > 1,
            "media_asset": {
                "id": pid, "created_at": iso(created), "updated_at": iso(created), "md5": md5,
                "file_ext": ext, "file_size": 0, "image_width": w, "image_height": h,
                "duration": None, "status": "active", "file_key": str(iid), "is_public": True,
                "pixel_hash": md5, "variants": variants,
            },
            "tag_string_general": " ".join(sorted(cats[CAT_GENERAL])),
            "tag_string_character": " ".join(sorted(cats[CAT_CHARACTER])),
            "tag_string_copyright": " ".join(sorted(cats[CAT_COPYRIGHT])),
            "tag_string_artist": " ".join(sorted(cats[CAT_ARTIST])),
            "tag_string_meta": " ".join(sorted(cats[CAT_META])),
            "file_url": variants[4]["url"],
            "large_file_url": variants[3]["url"],
            "preview_file_url": variants[0]["url"],
        }
        ctx = {
            "id": pid, "illust_id": iid, "page": page, "width": w, "height": h, "rating": rating,
            "ext": "jpg" if ext == "jpeg" else ext, "bookmarks": bookmarks, "views": illust.get("total_view") or 0,
            "page_count": illust.get("page_count") or 1, "date": created if created.tzinfo else created.astimezone(),
            "is_ai": illust.get("illust_ai_type") == 2, "is_bookmarked": bool(illust.get("is_bookmarked")),
            "user_id": user.get("id") or 0, "user_account": user.get("account") or "",
            "user_name": user.get("name") or "", "source": source, "type": illust.get("type"),
            "md5": md5, "tags": set(all_tags), "raw_keys": raw_keys, "tag_count": len(all_tags),
            "cat_counts": {c: len(v) for c, v in cats.items()},
        }
        return post, ctx

    # ------------------------------------------------------------- other objects
    def tag(self, name, category, post_count):
        return {"id": self.register(name, category), "name": name, "post_count": post_count,
                "category": category, "created_at": boot_ts(), "updated_at": boot_ts(),
                "is_deprecated": False, "words": words(name)}

    def user(self, pixiv_user, private=True):
        u = {
            "id": int(pixiv_user.get("id") or 1), "created_at": boot_ts(),
            "name": pixiv_user.get("account") or "pixiv", "inviter_id": None, "level": 50,
            "post_upload_count": 0, "post_update_count": 0, "note_update_count": 0, "is_deleted": False,
            "level_string": "Admin", "is_banned": False, "wiki_page_version_count": 0,
            "artist_version_count": 0, "artist_commentary_version_count": 0, "pool_version_count": 0,
            "forum_post_count": 0, "comment_count": 0, "favorite_group_count": 0, "appeal_count": 0,
            "flag_count": 0, "positive_feedback_count": 0, "neutral_feedback_count": 0,
            "negative_feedback_count": 0,
        }
        if private:
            u.update({"last_logged_in_at": iso(datetime.datetime.now()), "comment_threshold": -8,
                      "default_image_size": "large", "favorite_tags": None, "blacklisted_tags": "",
                      "time_zone": "UTC", "per_page": self.cfg.default_limit, "favorite_count": 0,
                      "theme": "auto", "enable_safe_mode": False, "show_deleted_posts": False,
                      "statement_timeout": 60000, "tag_query_limit": 1000, "is_verified": True,
                      "is_premium": bool(pixiv_user.get("is_premium"))})
        return u

    @staticmethod
    def anonymous():
        return {"id": None, "name": "Anonymous", "level": 0, "level_string": None, "created_at": boot_ts(),
                "blacklisted_tags": "", "per_page": 20, "tag_query_limit": 1000, "favorite_count": 0}


# ================================================================= only=
def parse_only(spec):
    out, stack, cur = {}, [], ""
    node = out
    for ch in spec:
        if ch == ",":
            if cur.strip():
                node.setdefault(cur.strip(), True)
            cur = ""
        elif ch == "[":
            child = {}
            node[cur.strip()] = child
            stack.append(node)
            node, cur = child, ""
        elif ch == "]":
            if cur.strip():
                node.setdefault(cur.strip(), True)
            cur = ""
            node = stack.pop() if stack else out
        else:
            cur += ch
    if cur.strip():
        node.setdefault(cur.strip(), True)
    return out


def apply_only(obj, spec):
    if isinstance(obj, list):
        return [apply_only(o, spec) for o in obj]
    if not isinstance(obj, dict) or spec is True:
        return obj
    return {k: apply_only(obj[k], sub) for k, sub in spec.items() if k in obj}


# ================================================================= XML
_TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?[+-]\d\d:\d\d$")


def _singular(name):
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith("s"):
        return name[:-1]
    return name + "-item"


def _xml(name, value, out, indent):
    tag = name.replace("_", "-")
    pad = "  " * indent
    if value is None:
        out.append(f'{pad}<{tag} nil="true"/>')
    elif isinstance(value, bool):
        out.append(f'{pad}<{tag} type="boolean">{"true" if value else "false"}</{tag}>')
    elif isinstance(value, int):
        out.append(f'{pad}<{tag} type="integer">{value}</{tag}>')
    elif isinstance(value, float):
        out.append(f'{pad}<{tag} type="float">{value}</{tag}>')
    elif isinstance(value, dict):
        out.append(f"{pad}<{tag}>")
        for k, v in value.items():
            _xml(k, v, out, indent + 1)
        out.append(f"{pad}</{tag}>")
    elif isinstance(value, list):
        if not value:
            out.append(f'{pad}<{tag} type="array"/>')
            return
        out.append(f'{pad}<{tag} type="array">')
        for v in value:
            _xml(_singular(name), v, out, indent + 1)
        out.append(f"{pad}</{tag}>")
    else:
        s = str(value)
        if _TS_RE.match(s):
            out.append(f'{pad}<{tag} type="dateTime">{re.sub(r"[.]\d+", "", s)}</{tag}>')
        else:
            out.append(f"{pad}<{tag}>{escape(s)}</{tag}>")


def to_xml(root, value):
    out = ['<?xml version="1.0" encoding="UTF-8"?>']
    _xml(root, value, out, 0)
    return "\n".join(out) + "\n"

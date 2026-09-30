"""Danbooru <-> pixiv tag mapping, backed by a local SQLite copy of Danbooru's
wiki `other_names` (which list each tag's pixiv / Japanese names) and artist
entries (which list each artist's pixiv user URLs).

The database is filled by `sync()` (full on first run, incremental afterwards)
and, for anything not synced yet, by live lookups against the Danbooru API.
"""

import datetime
import json
import logging
import re
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("bridge.mapping")

PIXIV_USER_RE = re.compile(r"pixiv\.net/(?:(?:en/)?users/|member\.php\?id=|u/)(\d+)")
MISS_TTL = 7 * 86400


KANA_RE = re.compile(r"[぀-ヿ]")


def _is_japanese_writable(s):
    try:
        unicodedata.normalize("NFKC", s).encode("shift_jis")
        return True
    except UnicodeEncodeError:
        return False


def order_pixiv_names(names, seen=None):
    """Best pixiv search name first.

    Danbooru's other_names usually start with the pixiv tag, but for many
    general tags a Chinese name comes first ("长发", "双马尾"). So: names that
    are actually seen on pixiv win; names that cannot be written in Japanese
    go last; if the first name is Chinese, the page's other kanji-only names
    (e.g. traditional "雙馬尾") are suspect too and kana names are preferred;
    English-only names go after Japanese ones."""
    seen = seen or {}
    first_ok = bool(names) and _is_japanese_writable(names[0]) and not names[0].isascii()

    def score(item):
        i, n = item
        s = i
        if not _is_japanese_writable(n):
            s += 1000
        elif n.isascii():
            s += 300
        elif not first_ok and not KANA_RE.search(n):
            s += 500
        return (-seen.get(key(n), 0), s)
    return [n for _, n in sorted(enumerate(names), key=score)]


def key(s):
    """Normalisation used to compare pixiv tags with Danbooru other_names."""
    return unicodedata.normalize("NFKC", s).lower().replace("_", "").replace(" ", "")


class DanbooruError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class DanbooruClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._last = 0.0

    def get(self, path, params, retries=4):
        params = dict(params)
        if self.cfg.danbooru_login and self.cfg.danbooru_api_key:
            params.update(login=self.cfg.danbooru_login, api_key=self.cfg.danbooru_api_key)
        url = f"{self.cfg.danbooru_url}{path}?{urllib.parse.urlencode(params)}"
        for attempt in range(retries):
            with self._lock:
                wait = self._last + self.cfg.danbooru_min_interval - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            req = urllib.request.Request(url, headers={"User-Agent": self.cfg.user_agent, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise DanbooruError(f"danbooru {e.code} for {path}", e.code) from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise DanbooruError(f"cannot reach danbooru: {e}") from None


SCHEMA = """
CREATE TABLE IF NOT EXISTS tags (name TEXT PRIMARY KEY, category INTEGER, post_count INTEGER);
CREATE TABLE IF NOT EXISTS wiki (id INTEGER PRIMARY KEY, title TEXT, other_names TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS wiki_title ON wiki(title);
CREATE TABLE IF NOT EXISTS names (key TEXT, tag TEXT, rank INTEGER, raw TEXT, PRIMARY KEY (key, tag));
CREATE INDEX IF NOT EXISTS names_tag ON names(tag, rank);
CREATE TABLE IF NOT EXISTS artists (id INTEGER PRIMARY KEY, name TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS artists_name ON artists(name);
CREATE TABLE IF NOT EXISTS artist_pixiv (pixiv_user_id INTEGER, artist TEXT, PRIMARY KEY (pixiv_user_id, artist));
CREATE INDEX IF NOT EXISTS artist_pixiv_artist ON artist_pixiv(artist);
CREATE TABLE IF NOT EXISTS misses (key TEXT PRIMARY KEY, at REAL);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, n INTEGER);
"""


class Mapping:
    def __init__(self, cfg, danbooru=None):
        self.cfg = cfg
        self.danbooru = danbooru or DanbooruClient(cfg)
        self.db = sqlite3.connect(cfg.db_path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.sync_lock = threading.Lock()
        self.sync_state = "idle"

    # ================================================================ storage
    def _q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def meta_get(self, k, default=None):
        rows = self._q("SELECT v FROM meta WHERE k=?", (k,))
        return rows[0][0] if rows else default

    def meta_set(self, k, v):
        self._q("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, str(v)))

    def _store_wiki(self, pages):
        with self.lock:
            self.db.execute("BEGIN")
            try:
                for p in pages:
                    title = p["title"]
                    if p.get("is_deleted"):
                        self.db.execute("DELETE FROM wiki WHERE id=?", (p["id"],))
                        self.db.execute("DELETE FROM names WHERE tag=?", (title,))
                        continue
                    names = [n for n in (p.get("other_names") or []) if n]
                    self.db.execute("INSERT OR REPLACE INTO wiki VALUES (?, ?, ?, ?)",
                                    (p["id"], title, json.dumps(names, ensure_ascii=False), p.get("updated_at")))
                    self.db.execute("DELETE FROM names WHERE tag=?", (title,))
                    for rank, n in enumerate(names):
                        self.db.execute("INSERT OR IGNORE INTO names VALUES (?, ?, ?, ?)", (key(n), title, rank, n))
                    # the title itself also identifies the tag (english pixiv tags)
                    self.db.execute("INSERT OR IGNORE INTO names VALUES (?, ?, ?, ?)", (key(title), title, 1000, title))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def _store_artists(self, artists):
        with self.lock:
            self.db.execute("BEGIN")
            try:
                for a in artists:
                    self.db.execute("DELETE FROM artist_pixiv WHERE artist=?", (a["name"],))
                    if a.get("is_deleted"):
                        self.db.execute("DELETE FROM artists WHERE id=?", (a["id"],))
                        continue
                    self.db.execute("INSERT OR REPLACE INTO artists VALUES (?, ?, ?)", (a["id"], a["name"], a.get("updated_at")))
                    for u in a.get("urls") or []:
                        m = PIXIV_USER_RE.search(u.get("url", ""))
                        if m:
                            self.db.execute("INSERT OR IGNORE INTO artist_pixiv VALUES (?, ?)", (int(m.group(1)), a["name"]))
                    self.db.execute("INSERT OR IGNORE INTO tags VALUES (?, 1, 0)", (a["name"],))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def _store_tags(self, tags):
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany("INSERT OR REPLACE INTO tags VALUES (?, ?, ?)",
                                [(t["name"], t["category"], t.get("post_count", 0)) for t in tags])
            self.db.execute("COMMIT")

    def _miss(self, k):
        rows = self._q("SELECT at FROM misses WHERE key=?", (k,))
        return bool(rows) and time.time() - rows[0][0] < MISS_TTL

    def _set_miss(self, keys):
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO misses VALUES (?, ?)", [(k, time.time()) for k in keys])

    @property
    def synced(self):
        return self.meta_get("full_sync_done") == "1"

    # ================================================================ sync
    def _walk(self, path, params, store, label, mode):
        """Walk a Danbooru index newest-id-first with page=b<id> cursors.

        Always passes a cursor, so results are in id order (without one Danbooru
        uses the index's default order and the cursor would skip rows). Progress is
        saved after every batch, so a failed sync resumes where it stopped, and the
        batch size shrinks when Danbooru times out on a large page."""
        resume_key = f"cursor:{label}:{mode}"
        cursor = int(self.meta_get(resume_key) or 2 ** 31 - 1)
        limit, total = 1000, 0
        while True:
            p = dict(params, limit=limit, page=f"b{cursor}")
            try:
                batch = self.danbooru.get(path, p, retries=1)
            except DanbooruError as e:
                if e.status in (500, 502, 503, 504) and limit > 50:
                    limit //= 4
                    log.info("danbooru timed out on %s; retrying with limit=%d", label, limit)
                    continue
                raise
            if not batch:
                break
            store(batch)
            total += len(batch)
            cursor = min(x["id"] for x in batch)
            self.meta_set(resume_key, cursor)
            self.sync_state = f"syncing {label}: {total}"
            if len(batch) < limit:
                break
            limit = min(1000, limit * 2)
        self._q("DELETE FROM meta WHERE k=?", (resume_key,))
        log.info("synced %d %s", total, label)
        return total

    def sync(self, force_full=False):
        if not self.sync_lock.acquire(blocking=False):
            return False
        try:
            started = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            full = force_full or not self.synced
            since = None if full else self.meta_get("last_sync")
            upd = {"search[updated_at]": f">{since}"} if since else {"search[is_deleted]": "false"}
            log.info("mapping sync starting (%s)", "full" if full else f"since {since}")
            mode = "full" if full else "incremental"
            self._walk("/wiki_pages.json", dict(upd, **{"search[other_names_present]": "true",
                                                        "only": "id,title,other_names,updated_at,is_deleted"}),
                       self._store_wiki, "wiki pages", mode)
            # all artists, filtered locally: Danbooru's wildcard url filter times out deep in the table
            self._walk("/artists.json", dict(upd, only="id,name,updated_at,is_deleted,urls[url]"),
                       self._store_artists, "artists", mode)
            last_tags = float(self.meta_get("tags_synced_at", "0"))
            if full or time.time() - last_tags > 7 * 86400:
                self._walk("/tags.json", {"search[has_wiki_page]": "true", "only": "id,name,category,post_count"},
                           self._store_tags, "tags", mode)
                self.meta_set("tags_synced_at", time.time())
            self.meta_set("last_sync", started)
            self.meta_set("full_sync_done", "1")
            self._q("DELETE FROM misses")
            self.sync_state = "idle"
            return True
        except DanbooruError as e:
            log.warning("mapping sync failed: %s", e)
            self.sync_state = f"failed: {e}"
            return False
        finally:
            self.sync_lock.release()

    def stats(self):
        return {
            "wiki_pages": self._q("SELECT COUNT(*) FROM wiki")[0][0],
            "tag_names": self._q("SELECT COUNT(*) FROM names")[0][0],
            "artists": self._q("SELECT COUNT(*) FROM artists")[0][0],
            "pixiv_artists": self._q("SELECT COUNT(DISTINCT pixiv_user_id) FROM artist_pixiv")[0][0],
            "tags": self._q("SELECT COUNT(*) FROM tags")[0][0],
            "last_sync": self.meta_get("last_sync"),
            "full_sync_done": self.synced,
            "state": self.sync_state,
        }

    # ================================================================ lookups
    def category(self, tag):
        rows = self._q("SELECT category, post_count FROM tags WHERE name=?", (tag,))
        if rows:
            return rows[0][0], rows[0][1]
        if self.cfg.live_lookups and not self._miss("tag:" + tag):
            try:
                found = self.danbooru.get("/tags.json", {"search[name]": tag, "only": "id,name,category,post_count"})
            except DanbooruError:
                found = []
            if found:
                self._store_tags(found)
                return found[0]["category"], found[0].get("post_count", 0)
            self._set_miss(["tag:" + tag])
        return None, 0

    def pixiv_names(self, tag):
        """Danbooru tag -> its pixiv names, most canonical first. Names that cannot
        be written in Japanese (Shift-JIS), e.g. simplified Chinese or Korean
        entries, are moved to the end so pixiv is searched with the Japanese tag."""
        rows = self._q("SELECT raw FROM names WHERE tag=? AND rank < 1000 ORDER BY rank", (tag,))
        if rows:
            names = [r[0] for r in rows]
            keys = [key(n) for n in names]
            seen = dict(self._q(f"SELECT key, n FROM seen WHERE key IN ({','.join('?' * len(keys))})", keys))
            return order_pixiv_names(names, seen)
        if not self._q("SELECT 1 FROM wiki WHERE title=?", (tag,)) and self.cfg.live_lookups \
                and not self._miss("wiki:" + tag):
            try:
                pages = self.danbooru.get("/wiki_pages.json", {"search[title]": tag,
                                                               "only": "id,title,other_names,updated_at,is_deleted"})
            except DanbooruError:
                pages = []
            if pages:
                self._store_wiki(pages)
                return [n for n in pages[0].get("other_names") or [] if n]
            self._set_miss(["wiki:" + tag])
        return []

    def to_danbooru(self, pixiv_tags):
        """[pixiv tag] -> {pixiv tag: danbooru tag}. Prefers wiki pages that list
        the pixiv tag first, then more popular tags."""
        self._record_seen(pixiv_tags)
        out, missing = {}, []
        for t in pixiv_tags:
            best = self._best(key(t))
            if best:
                out[t] = best
            else:
                missing.append(t)
        # live reverse lookups only until the local database has been filled
        if missing and self.cfg.live_lookups and not self.synced:
            todo = [t for t in missing if " " not in t and not self._miss("rev:" + key(t))]
            for i in range(0, len(todo), 40):
                chunk = todo[i:i + 40]
                try:
                    pages = self.danbooru.get("/wiki_pages.json", {
                        "search[other_names_include_any_lower]": " ".join(c.lower() for c in chunk),
                        "only": "id,title,other_names,updated_at,is_deleted", "limit": 200})
                except DanbooruError:
                    break
                self._store_wiki(pages)
                for t in chunk:
                    best = self._best(key(t))
                    if best:
                        out[t] = best
                self._set_miss(["rev:" + key(t) for t in chunk if t not in out])
        return out

    def _record_seen(self, pixiv_tags):
        """Remember which spellings pixiv really uses (improves forward lookups over time)."""
        keys = [key(t) for t in pixiv_tags if t]
        if keys:
            with self.lock:
                self.db.executemany("INSERT INTO seen VALUES (?, 1) ON CONFLICT(key) DO UPDATE SET n = n + 1",
                                    [(k,) for k in keys])

    def _best(self, k):
        rows = self._q("""SELECT n.tag FROM names n LEFT JOIN tags t ON t.name = n.tag
                          WHERE n.key=? ORDER BY n.rank, COALESCE(t.post_count, 0) DESC LIMIT 1""", (k,))
        return rows[0][0] if rows else None

    def artist_for_user(self, user_id):
        rows = self._q("SELECT artist FROM artist_pixiv WHERE pixiv_user_id=? ORDER BY artist LIMIT 1", (int(user_id),))
        if rows:
            return rows[0][0]
        if self.cfg.live_lookups and not self.synced and not self._miss(f"user:{user_id}"):
            try:
                found = self.danbooru.get("/artists.json", {
                    "search[url_matches]": f"https://www.pixiv.net/users/{int(user_id)}",
                    "only": "id,name,updated_at,is_deleted,urls[url]"})
            except DanbooruError:
                found = []
            self._store_artists(found)
            rows = self._q("SELECT artist FROM artist_pixiv WHERE pixiv_user_id=? LIMIT 1", (int(user_id),))
            if rows:
                return rows[0][0]
            self._set_miss([f"user:{user_id}"])
        return None

    def users_for_artist(self, name):
        rows = self._q("SELECT pixiv_user_id FROM artist_pixiv WHERE artist=?", (name,))
        if rows or not self.cfg.live_lookups or self._q("SELECT 1 FROM artists WHERE name=?", (name,)) \
                or self._miss("artist:" + name):
            return [r[0] for r in rows]
        try:
            found = self.danbooru.get("/artists.json", {"search[name]": name, "only": "id,name,updated_at,is_deleted,urls[url]"})
        except DanbooruError:
            found = []
        self._store_artists(found)
        if not found:
            self._set_miss(["artist:" + name])
        return [r[0] for r in self._q("SELECT pixiv_user_id FROM artist_pixiv WHERE artist=?", (name,))]

    def search_tags(self, pattern, categories=None, limit=20, offset=0, order="count", mapped_only=True):
        """Danbooru-style name pattern (* wildcard) over tags that can be searched on pixiv."""
        like = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("*", "%")
        sql = "SELECT name, category, post_count FROM tags t WHERE name LIKE ? ESCAPE '\\'"
        args = [like]
        if categories:
            sql += f" AND category IN ({','.join('?' * len(categories))})"
            args += list(categories)
        if mapped_only:
            sql += (" AND (EXISTS (SELECT 1 FROM names n WHERE n.tag = t.name AND n.rank < 1000)"
                    " OR EXISTS (SELECT 1 FROM artist_pixiv a WHERE a.artist = t.name))")
        sql += " ORDER BY " + ("name" if order == "name" else "post_count DESC, name") + " LIMIT ? OFFSET ?"
        return self._q(sql, args + [limit, offset])

    def aliases(self, tag):
        """Non-canonical other names for a tag (shown as tag aliases)."""
        return [r[0] for r in self._q("SELECT raw FROM names WHERE tag=? AND rank BETWEEN 1 AND 999 ORDER BY rank", (tag,))]

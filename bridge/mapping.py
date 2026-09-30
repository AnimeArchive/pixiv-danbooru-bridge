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
REV_BATCH = 100       # pixiv names per live wiki lookup
USER_BATCH = 40       # pixiv user urls per live artist lookup
MAX_MULTI = 4         # a name listed on more pages than this is a grouping label
MIN_SHARE = 0.01      # drop matches with <1% of the best match's post count
REFRESH_AFTER = 6 * 3600


KANA_RE = re.compile(r"[\u3040-\u30ff]")
HAN_RE = re.compile(r"[\u3400-\u9fff]")
HANGUL_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")
NOT_A_TAG = ("list_of_", "tag_group:", "pool_group:", "howto:", "help:", "about:")


def _is_japanese_writable(s):
    try:
        unicodedata.normalize("NFKC", s).encode("shift_jis")
        return True
    except UnicodeEncodeError:
        return False


def order_pixiv_names(names, seen=None, ambiguous=()):
    """Best pixiv search name first.

    Danbooru's other_names usually start with the pixiv tag, but for many
    general tags a Chinese name comes first ("长发", "双马尾"). Ranking:
    1. names that also stand for other Danbooru tags ("黒髪ロング" is black_hair
       and long_hair) go after exclusive names, so they never narrow a search;
    2. Japanese names first, then English-only names, then names that cannot be
       written in Japanese (simplified Chinese, Korean). If the first name is
       Chinese, the page's other kanji-only names (e.g. traditional "雙馬尾") are
       suspect too -- unless pixiv results have shown that spelling in use;
    3. spellings seen more often in pixiv results; 4. Danbooru's order."""
    seen = seen or {}
    first_ok = bool(names) and _is_japanese_writable(names[0]) and not names[0].isascii()

    def score(item):
        i, n = item
        k = key(n)
        if not _is_japanese_writable(n):
            cls = 3
        elif n.isascii():
            cls = 1
        elif not first_ok and not KANA_RE.search(n) and not seen.get(k):
            cls = 2
        else:
            cls = 0
        return (k in ambiguous, cls, -seen.get(k, 0), i)
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
CREATE TABLE IF NOT EXISTS aliases (antecedent TEXT PRIMARY KEY, consequent TEXT);
CREATE TABLE IF NOT EXISTS learned (key TEXT, tag TEXT, raw TEXT, PRIMARY KEY (key, tag));
CREATE INDEX IF NOT EXISTS learned_tag ON learned(tag);
CREATE TABLE IF NOT EXISTS local_tags (display TEXT, pixiv TEXT, n INTEGER, PRIMARY KEY (display, pixiv));
CREATE TABLE IF NOT EXISTS pixiv_users (user_id INTEGER PRIMARY KEY, account TEXT, name TEXT, display TEXT);
CREATE INDEX IF NOT EXISTS pixiv_users_display ON pixiv_users(display);
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
        self._pending = {"rev": set(), "user": set()}
        self._pending_lock = threading.Lock()
        self._worker = None

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
            marks = ",".join("?" * len(keys))
            seen = dict(self._q(f"SELECT key, n FROM seen WHERE key IN ({marks})", keys))
            ambiguous = {r[0] for r in self._q(f"""SELECT key FROM names WHERE key IN ({marks}) AND rank < 1000
                                                GROUP BY key HAVING COUNT(DISTINCT tag) > 1""", keys)}
            names += [n for n in self._learned_names(tag) if n not in names]
            return order_pixiv_names(names, seen, ambiguous)
        learned = self._learned_names(tag)
        if learned:
            return learned
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

    def _learned_names(self, tag):
        return [r[0] for r in self._q("SELECT raw FROM learned WHERE tag=?", (tag,))]

    # ---------------------------------------------------------------- pixiv -> danbooru
    def to_danbooru(self, pixiv_tags, translations=None):
        """[pixiv tag] -> {pixiv tag: [danbooru tags]}.

        One pixiv tag can stand for several Danbooru tags ("黒髪ロング" ->
        black_hair + long_hair, "カイミク" -> kaito + hatsune_miku). Names the local
        database does not know yet are looked up on Danbooru right away (a couple
        of batched requests) and the rest in the background.

        `translations` ({pixiv tag: pixiv's English translation}) is a fallback
        for names no Danbooru wiki lists: if "夜空" is translated "night sky" and
        night_sky is a Danbooru tag, it maps there (and is remembered)."""
        pixiv_tags = [t for t in dict.fromkeys(pixiv_tags) if t]
        self._record_seen(pixiv_tags)
        out, missing = {}, []
        for t in pixiv_tags:
            tags = self._candidates(key(t))
            if tags:
                out[t] = tags
            elif self.cfg.live_lookups and not self._miss("rev:" + key(t)):
                missing.append(t)
        if missing:
            self._maybe_refresh()
            budget = self.cfg.live_lookup_budget
            for i in range(0, len(missing), REV_BATCH):
                chunk = missing[i:i + REV_BATCH]
                if budget <= 0:
                    self._enqueue("rev", chunk)
                    continue
                budget -= 1
                self._lookup_names(chunk)
                for t in chunk:
                    tags = self._candidates(key(t))
                    if tags:
                        out[t] = tags
        if translations:
            out.update(self._from_translations({t: translations[t] for t in pixiv_tags
                                                if t not in out and translations.get(t)}))
        return out

    def _from_translations(self, pairs):
        """{pixiv tag: english translation} -> {pixiv tag: [danbooru tag]} for
        translations that are existing Danbooru tags."""
        from_name = {}
        for t, tr in pairs.items():
            name = unicodedata.normalize("NFKC", tr).strip().lower().replace(" ", "_")
            if name and not name.startswith(NOT_A_TAG):
                from_name[t] = name
        unknown = [n for n in set(from_name.values())
                   if not self._q("SELECT 1 FROM tags WHERE name=?", (n,)) and not self._miss("tag:" + n)]
        if unknown and self.cfg.live_lookups:
            for i in range(0, len(unknown), 100):
                chunk = unknown[i:i + 100]
                try:
                    self._store_tags(self.danbooru.get("/tags.json", {
                        "search[name_comma]": ",".join(chunk), "only": "id,name,category,post_count", "limit": 1000}))
                except DanbooruError:
                    break
                self._set_miss(["tag:" + n for n in chunk if not self._q("SELECT 1 FROM tags WHERE name=?", (n,))])
        empty = [n for n in set(from_name.values()) if not self._post_count(n)]
        aliased = self.resolve_aliases(empty) if empty else {}
        out, learned = {}, []
        for t, name in from_name.items():
            name = aliased.get(name, name)
            if self._post_count(name):
                out[t] = [name]
                learned.append((key(t), name, t))
        if learned:
            with self.lock:
                self.db.executemany("INSERT OR IGNORE INTO learned VALUES (?, ?, ?)", learned)
        return out

    def _lookup_names(self, names):
        if not names:
            return
        try:
            pages = self.danbooru.get("/wiki_pages.json", {
                "search[other_names_include_any_lower]": " ".join(n.lower() for n in names),
                "only": "id,title,other_names,updated_at,is_deleted", "limit": 1000})
        except DanbooruError as e:
            log.info("live tag lookup failed: %s", e)
            return
        self._store_wiki(pages)
        titles = [p["title"] for p in pages if not p.get("is_deleted")]
        unknown = [t for t in titles if not self._q("SELECT 1 FROM tags WHERE name=?", (t,))]
        for i in range(0, len(unknown), 100):
            try:
                self._store_tags(self.danbooru.get("/tags.json", {
                    "search[name_comma]": ",".join(unknown[i:i + 100]), "only": "id,name,category,post_count",
                    "limit": 1000}))
            except DanbooruError:
                break
        self._set_miss(["rev:" + key(n) for n in names if not self._candidates(key(n))])

    def _post_count(self, name):
        rows = self._q("SELECT post_count FROM tags WHERE name=?", (name,))
        return rows[0][0] if rows else 0

    def resolve_aliases(self, names):
        """Danbooru tag aliases ("kancolle" -> "kantai_collection") for names
        that are not tags themselves; looked up on Danbooru in batches, cached."""
        out, todo = {}, []
        for n in dict.fromkeys(names):
            rows = self._q("SELECT consequent FROM aliases WHERE antecedent=?", (n,))
            if rows:
                out[n] = rows[0][0]
            elif self.cfg.live_lookups and not self._miss("alias:" + n):
                todo.append(n)
        for i in range(0, len(todo), 100):
            chunk = todo[i:i + 100]
            try:
                found = self.danbooru.get("/tag_aliases.json", {
                    "search[antecedent_name_comma]": ",".join(chunk), "search[status]": "active",
                    "only": "antecedent_name,consequent_name", "limit": 1000})
            except DanbooruError:
                break
            with self.lock:
                self.db.executemany("INSERT OR REPLACE INTO aliases VALUES (?, ?)",
                                    [(a["antecedent_name"], a["consequent_name"]) for a in found])
            for a in found:
                out[a["antecedent_name"]] = a["consequent_name"]
                self.category(a["consequent_name"])  # make sure its category/count is known
            self._set_miss(["alias:" + n for n in chunk if n not in out])
        return out

    def _candidates(self, k):
        """Danbooru tags whose wiki lists this pixiv name, filtered for noise:
        tags without posts (list pages, dead tags) are dropped; a match far less
        popular than the best one is dropped (e.g. "ミク" is also listed on a
        5-post Pokemon character); names listed on many pages are grouping labels
        (costume series etc.), so they are left unmapped."""
        rows = self._q("""SELECT n.tag, n.rank, t.post_count FROM names n LEFT JOIN tags t ON t.name = n.tag
                          WHERE n.key=? ORDER BY n.rank, COALESCE(t.post_count, 0) DESC""", (k,))
        # once tags are synced, a wiki title missing from the tags table is not a tag
        tags_known = bool(self.meta_get("tags_synced_at"))
        rows = [(tag, rank, pc) for tag, rank, pc in rows
                if pc != 0 and not (pc is None and tags_known) and not tag.startswith(NOT_A_TAG)]
        if not rows:
            learned = self._q("SELECT tag FROM learned WHERE key=?", (k,))
            return [r[0] for r in learned]
        uniq = list(dict.fromkeys(tag for tag, _, _ in rows))
        if len(uniq) > MAX_MULTI:
            return []  # a grouping label (e.g. a costume series): no single right answer
        top = max((pc or 0) for _, _, pc in rows)
        keep = [tag for tag, _, pc in rows if pc is None or pc >= top * MIN_SHARE]
        return list(dict.fromkeys(keep))

    def _record_seen(self, pixiv_tags):
        """Remember which spellings pixiv really uses (improves forward lookups over time)."""
        keys = [key(t) for t in pixiv_tags if t]
        if keys:
            with self.lock:
                self.db.executemany("INSERT INTO seen VALUES (?, 1) ON CONFLICT(key) DO UPDATE SET n = n + 1",
                                    [(k,) for k in keys])

    # ---------------------------------------------------------------- artists
    def artist_for_user(self, user_id):
        return self.artists_for_users([user_id]).get(int(user_id))

    def artists_for_users(self, user_ids):
        """pixiv user ids -> Danbooru artist names; unknown ids are looked up on Danbooru."""
        ids = [int(u) for u in dict.fromkeys(user_ids) if u]
        out = self._artists_from_db(ids)
        missing = [u for u in ids if u not in out and self.cfg.live_lookups and not self._miss(f"user:{u}")]
        if missing:
            self._maybe_refresh()
            self._lookup_users(missing[:USER_BATCH])
            self._enqueue("user", missing[USER_BATCH:])
            out.update(self._artists_from_db(missing[:USER_BATCH]))
        return out

    def _artists_from_db(self, ids):
        out = {}
        for u in ids:
            rows = self._q("SELECT artist FROM artist_pixiv WHERE pixiv_user_id=? ORDER BY artist LIMIT 1", (u,))
            if rows:
                out[u] = rows[0][0]
        return out

    def _lookup_users(self, ids):
        if not ids:
            return
        try:
            found = self.danbooru.get("/artists.json", {
                "search[url_matches]": " ".join(f"https://www.pixiv.net/users/{u}" for u in ids),
                "only": "id,name,updated_at,is_deleted,urls[url]", "limit": 1000})
        except DanbooruError as e:
            log.info("live artist lookup failed: %s", e)
            return
        self._store_artists(found)
        known = self._artists_from_db(ids)
        self._set_miss([f"user:{u}" for u in ids if u not in known])

    # ---------------------------------------------------------------- background work
    def _enqueue(self, kind, items):
        if not items:
            return
        with self._pending_lock:
            self._pending[kind].update(items)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._drain, daemon=True)
                self._worker.start()

    def _drain(self):
        while True:
            with self._pending_lock:
                if self._pending["rev"]:
                    kind, pool = "rev", self._pending["rev"]
                    size = REV_BATCH
                elif self._pending["user"]:
                    kind, pool = "user", self._pending["user"]
                    size = USER_BATCH
                else:
                    return
                batch = [pool.pop() for _ in range(min(size, len(pool)))]
            try:
                if kind == "rev":
                    self._lookup_names([n for n in batch if not self._candidates(key(n))])
                else:
                    self._lookup_users([u for u in batch if not self._artists_from_db([u])])
            except Exception as e:  # keep draining the queue
                log.warning("background lookup failed: %s", e)

    def wait_idle(self, timeout=30):
        """Wait for background lookups to finish (used by tests and the sync command)."""
        deadline = time.time() + timeout
        while self._worker is not None and self._worker.is_alive() and time.time() < deadline:
            time.sleep(0.05)

    def _maybe_refresh(self):
        """New names showed up: pull Danbooru's recent wiki/artist edits if the
        last sync is more than a few hours old (runs in the background)."""
        last = self.meta_get("last_sync")
        if not self.synced or not last or self.sync_lock.locked():
            return
        try:
            age = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(last)).total_seconds()
        except ValueError:
            return
        if age > REFRESH_AFTER:
            threading.Thread(target=self.sync, daemon=True).start()

    # ---------------------------------------------------------------- tags Danbooru doesn't have
    def local_artist_name(self, user_id, account):
        """Artist tag for a pixiv user with no Danbooru artist entry: the pixiv
        account name, or "account_(user id)" if that name is already a Danbooru
        tag (so it can't be confused with a different Danbooru artist)."""
        rows = self._q("SELECT display FROM pixiv_users WHERE user_id=?", (int(user_id or 0),))
        if rows and rows[0][0]:
            return rows[0][0]
        base = unicodedata.normalize("NFKC", account or "").strip().lower().replace(" ", "_") or "unknown_artist"
        taken = self._q("SELECT 1 FROM tags WHERE name=?", (base,)) or             self._q("SELECT 1 FROM pixiv_users WHERE display=? AND user_id<>?", (base, int(user_id or 0)))
        return f"{base}_({user_id})" if taken else base

    def remember_local(self, tags, users):
        """Store stand-in names (display -> pixiv tag / pixiv user) for later searches."""
        with self.lock:
            if tags:
                self.db.executemany("""INSERT INTO local_tags VALUES (?, ?, 1)
                                       ON CONFLICT(display, pixiv) DO UPDATE SET n = n + 1""", tags)
            if users:
                self.db.executemany("INSERT OR REPLACE INTO pixiv_users VALUES (?, ?, ?, ?)",
                                    [(int(u), a, n, d) for u, a, n, d in users if u])

    def local_pixiv_names(self, display):
        return [r[0] for r in self._q("SELECT pixiv FROM local_tags WHERE display=? ORDER BY n DESC LIMIT 3", (display,))]

    def local_users(self, display):
        return [r[0] for r in self._q("SELECT user_id FROM pixiv_users WHERE display=?", (display,))]

    # ---------------------------------------------------------------- danbooru -> pixiv
    def search_names(self, tag, limit=3):
        """pixiv names to OR together when searching for a Danbooru tag: the best
        Japanese names plus the best Chinese name (for works tagged in Chinese)."""
        names = [n for n in self.pixiv_names(tag) if not (n.isascii() and "_" in n)]
        if limit <= 1 or len(names) <= 1:
            return names[:1]
        japanese = [n for n in names if _is_japanese_writable(n)]
        chinese = [n for n in names if not _is_japanese_writable(n) and HAN_RE.search(n) and not HANGUL_RE.search(n)]
        if not japanese:
            return chinese[:limit]
        return japanese[:limit - 1] + (chinese[:1] or japanese[limit - 1:limit])

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

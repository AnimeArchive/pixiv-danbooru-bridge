"""End-to-end tests: real bridge HTTP server <-> fake pixiv + fake Danbooru.
Run: python -m unittest discover -s tests -v"""

import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import fakes  # noqa: E402
from bridge.config import Config  # noqa: E402

API_KEY = "bridge-secret"


class Base(unittest.TestCase):
    api_key = ""
    sync = True

    @classmethod
    def setUpClass(cls):
        fakes.reset()
        cls.fake = fakes.start()
        fb = f"http://127.0.0.1:{cls.fake.server_port}"
        cls.tmp = tempfile.mkdtemp()
        os.environ.update({
            "PIXIV_REFRESH_TOKEN": fakes.REFRESH_TOKEN, "DANBOORU_URL": fb + "/danbooru",
            "DB_PATH": os.path.join(cls.tmp, "m.sqlite3"), "PIXIV_MIN_INTERVAL": "0",
            "DANBOORU_MIN_INTERVAL": "0", "SYNC_ON_START": "false", "LOG_LEVEL": "WARNING",
            "BRIDGE_API_KEY": cls.api_key, "BRIDGE_LOGIN": "tester" if cls.api_key else "",
            "SEARCH_CACHE_TTL": "0", "MAX_SCAN_PAGES": "10",
        })
        from bridge import server
        cfg = Config()
        cls.bridge = server.Bridge(cfg)
        cls.bridge.pixiv.api = fb + "/app"
        cls.bridge.pixiv.auth_url = fb + "/auth/token"
        cls.bridge.pixiv.image_base = fb + "/pximg/"
        if cls.sync:
            assert cls.bridge.mapping.sync()
        handler = type("H", (server.Handler,), {"bridge": cls.bridge, "cfg": cfg})
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.fake.shutdown()
        cls.bridge.mapping.db.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        fakes.REQUESTS.clear()

    def req(self, path, params=None, method="GET", data=None, headers=None, raw=False):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        r = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(r) as resp:
                c = resp.read()
                return resp.status, (c if raw else json.loads(c) if c else None)
        except urllib.error.HTTPError as e:
            c = e.read()
            return e.code, (c if raw else json.loads(c) if c else None)

    def ids(self, tags, **extra):
        status, body = self.req("/posts.json", dict(tags=tags, **extra))
        self.assertEqual(status, 200, body)
        return [p["id"] for p in body]

    def pixiv_words(self):
        return [r[2].get("word") for r in fakes.requests_to("/app/v1/search/illust")]


class TestMapping(Base):
    def test_sync_stats(self):
        st = self.bridge.mapping.stats()
        self.assertEqual((st["wiki_pages"], st["pixiv_artists"], st["full_sync_done"]), (6, 1, True))

    def test_name_ordering(self):
        from bridge.mapping import order_pixiv_names
        self.assertEqual(order_pixiv_names(["长发", "ロングヘアー"])[0], "ロングヘアー")        # simplified Chinese first
        self.assertEqual(order_pixiv_names(["双马尾", "雙馬尾", "ツインテール"])[0], "ツインテール")
        self.assertEqual(order_pixiv_names(["青空", "蒼空", "青い空"])[0], "青空")            # kanji-only Japanese kept
        self.assertEqual(order_pixiv_names(["blue_eyes", "青い目"])[0], "青い目")
        self.assertEqual(order_pixiv_names(["黑发", "黒髪", "黒髪ロング"], {"黒髪": 5})[0], "黒髪")  # learned usage wins

    def test_lookups(self):
        m = self.bridge.mapping
        self.assertEqual(m.pixiv_names("blue_sky"), ["青空", "蒼空"])
        self.assertEqual(m.to_danbooru(["初音ミク", "蒼空", "vocaloid", "風景"]),
                         {"初音ミク": "hatsune_miku", "蒼空": "blue_sky", "vocaloid": "vocaloid"})
        self.assertEqual(m.artist_for_user(111), "miso_tanu")
        self.assertEqual(m.users_for_artist("miso_tanu"), [111])
        self.assertEqual(m.category("hatsune_miku")[0], 4)


class TestSearch(Base):
    def test_empty_query_uses_follow_feed(self):
        self.assertEqual(self.ids(""), [1004000, 1003000, 1002000, 1002001, 1002002, 1001000])
        self.assertTrue(fakes.requests_to("/app/v2/illust/follow"))

    def test_danbooru_tag_is_searched_as_pixiv_tag(self):
        self.assertEqual(self.ids("hatsune_miku"), [1004000, 1002000, 1002001, 1002002, 1001000])
        self.assertEqual(self.pixiv_words()[0], "初音ミク")

    def test_and_negation_and_local_filters(self):
        self.assertEqual(self.ids("hatsune_miku long_hair"), [1002000, 1002001, 1002002])
        self.assertEqual(self.pixiv_words()[0], "初音ミク ロングヘア")
        self.assertEqual(self.ids("blue_sky -rating:e"), [1001000])
        self.assertEqual(self.ids("blue_sky width:>2000"), [1003000])
        self.assertEqual(self.ids("hatsune_miku -kaito_(vocaloid)"), [1004000, 1001000])
        self.assertIn("初音ミク -KAITO", self.pixiv_words())
        self.assertEqual(self.ids("hatsune_miku score:>=100"), [1002000, 1002001, 1002002, 1001000])
        self.assertEqual(self.ids("hatsune_miku is:ugoira"), [1004000])

    def test_or(self):
        self.assertEqual(self.ids("~long_hair ~original"), [1003000, 1002000, 1002001, 1002002])
        self.assertEqual(self.pixiv_words()[0], "ロングヘア OR オリジナル")

    def test_artist_tag_uses_user_works(self):
        self.assertEqual(self.ids("miso_tanu"), [1001000])
        self.assertEqual(fakes.requests_to("/app/v1/user/illusts")[0][2]["user_id"], "111")

    def test_ai_filter_is_sent_to_pixiv(self):
        self.assertEqual(self.ids("blue_sky -ai-generated"), [1001000])
        self.assertEqual(fakes.requests_to("/app/v1/search/illust")[0][2].get("search_ai_type"), "0")

    def test_dates_are_sent_to_pixiv(self):
        self.assertEqual(self.ids("hatsune_miku date:2026-09-05..2026-09-30"), [1004000, 1002000, 1002001, 1002002])
        q = fakes.requests_to("/app/v1/search/illust")[0][2]
        self.assertEqual((q["start_date"], q["end_date"]), ("2026-09-05", "2026-09-30"))

    def test_pagination(self):
        self.assertEqual(self.ids("hatsune_miku", limit=2, page=2), [1002001, 1002002])
        self.assertEqual(self.ids("hatsune_miku", limit=2, page="b1002001"), [1002000, 1001000])
        self.assertEqual(self.ids("hatsune_miku limit:1"), [1004000])

    def test_id_lookup(self):
        self.assertEqual(self.ids("pixiv:1002"), [1002000, 1002001, 1002002])
        self.assertEqual(self.ids("id:1002001"), [1002001])

    def test_order(self):
        self.assertEqual(self.ids("hatsune_miku order:score")[0], 1001000)
        self.assertEqual(fakes.requests_to("/app/v1/search/illust")[0][2]["sort"], "popular_desc")
        self.assertEqual(self.ids("order:rank")[0], 1001000)
        self.assertTrue(fakes.requests_to("/app/v1/illust/ranking"))

    def test_counts_and_random(self):
        s, body = self.req("/counts/posts.json", {"tags": "blue_sky"})
        self.assertEqual(body, {"counts": {"posts": 2}})
        s, body = self.req("/posts/random.json", {"tags": "blue_sky"})
        self.assertIn(body["id"], (1001000, 1003000))


class TestPostFormat(Base):
    def test_fields(self):
        s, p = self.req("/posts/1001000.json")
        self.assertEqual(s, 200, p)
        self.assertEqual(p["tag_string_character"], "hatsune_miku")
        self.assertEqual(p["tag_string_copyright"], "vocaloid")
        self.assertEqual(p["tag_string_general"], "blue_sky")
        self.assertEqual(p["tag_string_artist"], "miso_tanu")
        self.assertEqual(p["rating"], "g")
        self.assertEqual(p["source"], "https://www.pixiv.net/artworks/1001")
        self.assertEqual(p["pixiv_id"], 1001)
        self.assertEqual(p["fav_count"], 500)
        self.assertEqual(p["file_ext"], "jpg")
        s, p = self.req("/posts/1003000.json")
        self.assertEqual(p["rating"], "e")
        self.assertEqual(p["tag_string_general"], "blue_sky scenery")  # unmapped -> pixiv translation
        self.assertEqual(p["tag_string_copyright"], "original")
        self.assertEqual(p["tag_string_meta"], "absurdres ai-generated highres")
        self.assertEqual(p["tag_string_artist"], "someone")  # no Danbooru artist -> pixiv account
        s, p = self.req("/posts/1002001.json")
        self.assertEqual((p["rating"], p["parent_id"]), ("s", 1002000))
        self.assertIn("_p1.jpg", p["file_url"])

    def test_ugoira(self):
        s, p = self.req("/posts/1004000.json")
        self.assertEqual(p["file_ext"], "zip")
        self.assertIn("img-zip-ugoira", p["file_url"])
        self.assertIn("_ugoira1920x1080.zip", p["file_url"])

    def test_image_proxy(self):
        s, p = self.req("/posts/1001000.json")
        s, raw = self.req(p["file_url"].replace(self.base, ""), raw=True)
        self.assertEqual(s, 200)
        self.assertEqual(raw, b"IMG:img-original/img/2026/09/01/00/00/00/1001_p0.jpg")
        bad = p["file_url"].replace(self.base, "").replace("1001_p0", "1002_p0")
        self.assertEqual(self.req(bad, raw=True)[0], 403)

    def test_xml_and_only(self):
        s, raw = self.req("/posts.xml", {"tags": "pixiv:1001"}, raw=True)
        self.assertIn(b"<tag-string-character>hatsune_miku</tag-string-character>", raw)
        s, body = self.req("/posts.json", {"tags": "pixiv:1001", "only": "id,rating"})
        self.assertEqual(body, [{"id": 1001000, "rating": "g"}])


class TestTagsAndWrites(Base):
    def test_tags_and_autocomplete(self):
        s, body = self.req("/tags.json", {"search[name_matches]": "*sky*"})
        self.assertEqual([t["name"] for t in body], ["blue_sky"])
        s, body = self.req("/autocomplete.json", {"search[query]": "hats", "search[type]": "tag_query"})
        self.assertEqual([t["value"] for t in body], ["hatsune_miku"])
        self.assertEqual(body[0]["category"], 4)
        s, body = self.req("/autocomplete.json", {"search[query]": "初音", "search[type]": "tag_query"})
        self.assertEqual([t["value"] for t in body], ["hatsune_miku"])
        s, body = self.req("/tag_aliases.json", {"search[consequent_name]": "blue_sky"})
        self.assertEqual([a["antecedent_name"] for a in body], ["蒼空"])
        s, body = self.req("/artists.json", {"search[name]": "miso_tanu"})
        self.assertEqual(body[0]["urls"][0]["url"], "https://www.pixiv.net/users/111")

    def test_favorites_are_pixiv_bookmarks(self):
        s, _ = self.req("/favorites.json", method="POST", data={"post_id": "1002001"})
        self.assertEqual(s, 201)
        self.assertEqual(fakes.requests_to("/app/v2/illust/bookmark/add")[0][3]["illust_id"], "1002")
        self.assertEqual(self.ids("fav:me"), [1002000, 1002001, 1002002])
        s, _ = self.req("/favorites/1002000.json", method="DELETE")
        self.assertEqual(s, 204)
        self.assertEqual(self.ids("fav:me"), [])

    def test_profile(self):
        s, body = self.req("/profile.json")
        self.assertEqual((body["id"], body["name"]), (999, "me"))


class TestLiveLookupsBeforeSync(Base):
    sync = False

    def test_live(self):
        self.assertEqual(self.ids("kaito_(vocaloid)"), [1002000, 1002001, 1002002])
        self.assertEqual(self.pixiv_words()[0], "KAITO")
        s, p = self.req("/posts/1001000.json")
        self.assertEqual(p["tag_string_artist"], "miso_tanu")
        self.assertEqual(p["tag_string_character"], "hatsune_miku")


class TestAuth(Base):
    api_key = API_KEY

    def test_auth(self):
        self.assertEqual(self.req("/posts.json")[0], 401)
        self.assertEqual(self.req("/posts.json", {"login": "tester", "api_key": "nope"})[0], 401)
        basic = base64.b64encode(f"tester:{API_KEY}".encode()).decode()
        s, body = self.req("/posts.json", {"tags": "pixiv:1001"}, headers={"Authorization": "Basic " + basic})
        self.assertEqual(s, 200)
        self.assertEqual(self.req(body[0]["preview_file_url"].replace(self.base, ""), raw=True)[0], 200)
        self.assertEqual(self.req("/profile.json")[1]["name"], "Anonymous")


if __name__ == "__main__":
    unittest.main()

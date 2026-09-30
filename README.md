# pixiv-danbooru-bridge

A self-hosted server that **speaks the Danbooru API** and answers from **pixiv**, using
your own pixiv account. Point any Danbooru client (Grabber, gallery-dl, booru browser
apps, scripts) at it and browse pixiv **with Danbooru tags**. Searching `hatsune_miku blue_sky`
becomes a pixiv search for `初音ミク 青空`, and the results come back tagged the Danbooru way
(`tag_string_character: hatsune_miku`, `tag_string_artist: …`, ratings `g/s/q/e`).

Sister project of [hydrus-danbooru-bridge](https://github.com/AnimeArchive/hydrus-danbooru-bridge).
Pure Python standard library, no dependencies.

## How the tag mapping works

Danbooru's wiki keeps an `other_names` list for each tag with its pixiv/Japanese names
(`blue_sky` → `青空, 蒼空, …`), and Danbooru artist entries list their pixiv profile URLs.
The bridge keeps a local SQLite copy of both:

- **Danbooru → pixiv** (searching): each tag is searched as its best pixiv name, and an
  artist tag searches that artist's pixiv works directly. Danbooru's `other_names` sometimes
  list a Chinese name first (`long_hair` → `长发`), so the bridge prefers Japanese spellings and
  learns which spellings really appear in pixiv results. You can pin any tag with
  `PIXIV_TAG_OVERRIDES`, e.g. `{"black_hair": "黒髪"}`.
- **pixiv → Danbooru** (results): each pixiv tag is mapped back to a Danbooru tag, with the
  right category (artist / copyright / character / general / meta). When a pixiv name belongs
  to several Danbooru tags, the one that lists it first wins, then the more popular one.
- Pixiv tags with no Danbooru match are shown as pixiv's English translation (or the
  original name; see `UNMAPPED_TAG_STYLE`).
- The artist tag comes from Danbooru's artist database; if the artist isn't there, the pixiv
  account name is used.
- The bridge also adds `highres`, `absurdres`, `ai-generated`, `animated`/`ugoira`, `comic`
  and `multiple_images` like Danbooru does.

On first start the bridge downloads the mapping from Danbooru's public API: about 176k wiki
pages with pixiv names, 400k artists (355k pixiv accounts) and 650k tags. That takes about
10–20 minutes at the default 1 request/second, and builds a ~200 MB database. After that it
refreshes incrementally every 24 hours (a few seconds). If the first sync is interrupted, it
resumes where it stopped. Until that first
sync finishes, tags are looked up live on Danbooru, so the bridge works right away, just more slowly.

## Quick start (Docker)

1. **Get a pixiv refresh token** for your account (see below).
2. Configure and start:
   ```bash
   cp .env.example .env        # set PIXIV_REFRESH_TOKEN and BRIDGE_API_KEY
   docker compose up -d        # pulls ghcr.io/animearchive/pixiv-danbooru-bridge:latest
   ```
   Or: `docker run -d -p 8001:8000 --env-file .env -v pixiv-bridge-data:/data ghcr.io/animearchive/pixiv-danbooru-bridge:latest`
3. In your client, add a **Danbooru (2.0)** site at `http://<host>:8001`, with username
   `BRIDGE_LOGIN` and API key `BRIDGE_API_KEY`.

`http://<host>:8001/healthz` shows whether pixiv login works and how far the mapping sync is.
The `/data` volume holds the mapping database and pixiv's rotated refresh token; keep it
between updates.

### Getting a refresh token

pixiv no longer allows password logins for third-party apps, so you need a refresh token,
the same thing gallery-dl and pixivpy use. The easiest ways:

- `gallery-dl oauth:pixiv` (if you have gallery-dl), or
- the [pixiv_auth.py](https://gist.github.com/ZipFile/c9ebedb224406f4f11845ab700124362) script:
  `python pixiv_auth.py login`

Both open pixiv's login page in your browser and print a `refresh_token` once you log in.
Put it in `.env` as `PIXIV_REFRESH_TOKEN`. Treat it like a password: it gives full access to
your pixiv account.

## Search syntax

Everything Danbooru supports is parsed. The parts pixiv can do are sent to pixiv; everything
else is filtered exactly by the bridge on the fetched results.

| Danbooru | pixiv |
|---|---|
| `tag1 tag2`, `-tag` | pixiv search `名前1 名前2 -名前3` (exact tag match) |
| `~a ~b`, `a or b` | `A OR B` |
| artist tag (e.g. `miso_tanu`) | that artist's works (`/v1/user/illusts`) |
| `fav:me`, `ordfav:…` | your pixiv bookmarks |
| `pixiv:123`, `id:123000` | that work |
| `date:2026-01-01..2026-02-01`, `age:<1w` | pixiv `start_date` / `end_date` |
| `-ai-generated`, `-is:ai`, `HIDE_AI=true` | pixiv `search_ai_type=0` |
| `order:score` / `favcount` / `rank` | `popular_desc` (**pixiv Premium only**). With no tags, this uses the daily ranking |
| `order:id` | `date_asc` |
| empty search | your follow feed (`EMPTY_QUERY_SOURCE`) |
| `rating:` `is:sfw` `width:` `height:` `ratio:` `mpixels:` `score:` `favcount:` `views:` `pages:` `filetype:` `is:manga` `is:ugoira` `is:ai` `tagcount:` `user:<id>` `limit:` `random:` `order:views`/`mpixels`/`landscape`/`portrait`/`random` | filtered/sorted by the bridge |

Ratings: pixiv R-18 and R-18G → `e`; otherwise pixiv's sanity level gives `g`, `s` or `q`.

## Mapping to Danbooru objects

| Danbooru | pixiv |
|---|---|
| post `id` | `illust_id × 1000 + page` (page 2 of work 1234 is post 1234001; pages link via `parent_id`) |
| `score`, `fav_count`, `up_score` | bookmark count |
| `source`, `pixiv_id` | `https://www.pixiv.net/artworks/<id>` |
| `file_url` | the original image, or the 1920×1080 zip for ugoira |
| `large_file_url` | pixiv's 1200px master |
| `preview_file_url` | pixiv's 540px thumbnail |
| favorites (`POST /favorites.json`, `DELETE /favorites/{id}.json`) | pixiv bookmarks |
| `/autocomplete.json`, `/tags.json` | Danbooru tags that have a pixiv name. `post_count` is **Danbooru's** count, used for ranking. Japanese input falls back to pixiv's autocomplete |
| `/tag_aliases.json` | the other pixiv names of a tag |
| `/artists.json` | Danbooru artists with their pixiv profile URLs |
| `/explore/posts/popular.json` | pixiv ranking (day/week/month) |

Images are streamed through the bridge. pixiv's image server needs a pixiv `Referer`, so
direct links wouldn't load. Image URLs are signed, so the bridge can't be used as an open proxy.

**Things that differ from Danbooru:**

- `md5` is a stable hash of the image URL, not of the file.
- `file_size` is 0.
- Every page of a multi-page work reports page 1's dimensions, because pixiv's API only gives
  those.
- `/counts/posts.json` is a lower bound: it counts what was fetched from the first few result
  pages.
- Very selective local filters can end a page early, since the bridge scans at most
  `MAX_SCAN_PAGES` pixiv pages per request.

## Please be nice

This uses pixiv's unofficial mobile-app API (the same one gallery-dl, pixivpy and PixEz use)
with your own account. Heavy automated use can get an account rate-limited or restricted and
may conflict with pixiv's terms, so keep `PIXIV_MIN_INTERVAL` sensible and use it for personal
browsing. The Danbooru sync is gentle (1 request/second, with a descriptive User-Agent). Set
`DANBOORU_LOGIN`/`DANBOORU_API_KEY` if you have an account.

## Configuration

All options are listed in [.env.example](.env.example). Command-line tools:

```bash
python -m bridge              # serve (default)
python -m bridge sync         # incremental mapping sync
python -m bridge sync-full    # re-download the whole mapping
python -m bridge stats        # mapping database stats
```

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests run the real bridge against fake pixiv (OAuth, app API, image server) and fake
Danbooru servers ([tests/fakes.py](tests/fakes.py)).

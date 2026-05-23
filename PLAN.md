# watchseries-grabber — plan doc

Started: 2026-05-21. Restoreable-from-blank-session brief.

## Goal

Build a service that lets Sonarr (and eventually Radarr) treat
`watchseries.bar` like any other indexer + download client. End goal: search a
show in Sonarr, click "search & download," files appear in the Plex library.

Per-user request: integrate into the home lab at `home.silberg.cloud`.
Output files must land in a Sonarr-importable folder.

## Working directory

`/home/damian/Developer/Watchseries-Downloader` — the repo we hijacked. Original
contents (Python-2 era scraper) deleted in this session; new code lives here.
Remote on GitHub is `manojprithvee/Watchseries-Downloader` (we will replace it
or use a different remote — undecided).

## Architecture (decided)

Two HTTP services in **one** Python process:

1. **Torznab indexer** at `/torznab/api`
   - `t=caps` / `t=tvsearch` / `t=movie` / `t=search`
   - Returns RSS XML; each item's enclosure is a magnet whose `xs=` param
     carries `watchseries://<media_type>/<tmdb_id>/sN/eM?quality=...&title=...&year=...`
   - The magnet's `xt=urn:btih:` is a deterministic SHA-1 of the payload (so
     Sonarr's de-dupe behaves sanely).

2. **Fake qBittorrent v2 API** at `/api/v2/...`
   - Minimum subset Sonarr's `QBittorrentProxy` calls:
     `auth/login`, `app/version`, `app/webapiVersion`, `app/buildInfo`,
     `app/preferences`, `torrents/add`, `torrents/info`, `torrents/delete`,
     `torrents/properties`, `torrents/files`, plus no-op `set*`/`pause`/`resume`.
   - On `torrents/add`, we parse our magnet's `xs=`, create a `Job`, and start
     a background thread that runs the actual scraper.
   - On `torrents/info`, we report state strings Sonarr understands
     (`downloading` → `pausedUP` when complete).

This is the Jackett/Jackettio pattern: pretend to be a torrent indexer + a
torrent client so we slot into the *arr ecosystem with zero changes there.

## Why a single process

Sonarr is happy to talk to one indexer and a separate download client. We just
put both routers in the same FastAPI app — fewer ports, fewer containers, and
they share the in-memory `JobManager`. The qBittorrent base URL Sonarr is
given includes `/api/v2`, so there's no path collision with `/torznab/api`.

## Source pipeline (already proven end-to-end via CLI)

For each (tmdb_id, season, episode) or (tmdb_id, movie):

1. `GET https://api.videasy.net/mb-flix/sources-with-title?tmdbId=...&seasonId=...&episodeId=...&title=...&mediaType=tv|movie`
   - **MUST send `Referer: https://cineby.sc/` and `Origin: https://cineby.sc`**
     (otherwise 403).
   - **MUST send the correct human title** as `title=` — the upstream filters
     by it. Empty or garbage title → 500. (This is why we now carry `title`
     inside the magnet `xs=` payload.)
2. Response is a hex ciphertext string.
3. `node decrypt.js <ciphertext> <tmdb_id>` — uses bundled `module.wasm` to
   produce `{ sources: [{url, quality}], subtitles: [{url, lang, language}] }`.
   Returned m3u8 URLs are HLS on `easy.speedsterwave.app`.
4. `ffmpeg -referer https://cineby.sc/ -i <m3u8> -c copy out.mp4` — note the
   referer is required for the CDN to serve 200 instead of 403.

Provider name `mb-flix` is the only one of ~12 we tried on `api.videasy.net`
that returned data for our test show. If it dies, alternates listed: `cdn`,
`moviebox`, `1movies`, `m4uhd`, `hdmovie`, `lamovie`, `superflix`, `meine`.

## Repo layout (current)

```
.
├── decrypt.js              # vendored from walterwhite-69/Videasy.net-Decryptor (MIT)
├── module.wasm             # WASM decryption engine
├── package.json            # crypto-js, hashids
├── node_modules/           # npm install output (gitignored)
├── download.py             # CLI entry point — uses src/watchseries/scraper.py
├── src/watchseries/
│   ├── __init__.py
│   ├── scraper.py          # api.videasy.net call + decrypt + ffmpeg
│   ├── jobs.py             # JobManager + magnet make/parse
│   ├── torznab.py          # /torznab/api router
│   ├── fakeqbt.py          # /api/v2 router (qBittorrent mimic)
│   └── main.py             # FastAPI app, env config, /health endpoint
├── README.md               # (placeholder — needs full rewrite at deploy time)
├── PLAN.md                 # this doc
└── .gitignore
```

Env vars consumed by the service:
- `WSG_DOWNLOAD_DIR` — default `save_path` reported to Sonarr (default `/downloads`)
- `WSG_INDEXER_TITLE` — display name in Torznab caps (default `WatchSeries.bar`)
- `TMDB_API_KEY` — optional. When present, indexer can resolve `tvdbid`/`imdbid`
  to TMDB IDs and do text search via `/search/tv` & `/search/movie`. Without it,
  callers must pass `tmdbid` directly.

## Status by task

| # | Task                            | Status      |
|---|---------------------------------|-------------|
| 1 | Refactor scraper into package   | ✅ done     |
| 2 | Job manager + state             | ✅ done     |
| 3 | Torznab indexer                 | ✅ done     |
| 4 | Fake qBittorrent API            | ✅ done     |
| 5 | FastAPI app + entrypoint        | ✅ done     |
| 6 | Dockerfile + docker-compose     | ✅ done     |
| 7 | Deploy to home server           | ✅ done (running on home.silberg.cloud, container `watchseries-grabber`, host port 8765) |
| 8 | README + Sonarr config steps    | ✅ done (README.md); Sonarr/Prowlarr UI config not yet performed |

## Deployment landed

Code lives at `~/HomeLab/services/watchseries-grabber/` on `home.silberg.cloud`.
Standard HomeLab Makefile targets installed:

  make watchseries-up        # build + start
  make watchseries-down
  make watchseries-restart
  make watchseries-logs
  make watchseries-status    # ps + curl /health

Container:
- runs as `1000:100` (damian:users) so NFS writes succeed
- mounts host `/mnt/nas/media2/downloads` → container `/downloads`
- joins `caddy_network`
- exposes 8765 on host

End-to-end smoke test on the server: posted a magnet to
`/api/v2/torrents/add` for Men of a Certain Age S01E01 360p; container
created `Men.of.a.Certain.Age.S01E01.360p.WEBDL.watchseries/Season 01/` on
the NAS with correct ownership and started ffmpeg.

## What's verified

Run locally (port 9876, `WSG_DOWNLOAD_DIR=/tmp/wsg-test`):
- `GET /health` → ok, no missing deps
- `GET /torznab/api?t=caps` → valid caps XML
- `GET /torznab/api?t=tvsearch&tmdbid=16208&season=1&ep=1` → RSS item with magnet
- `POST /api/v2/auth/login` → `Ok.`
- `GET /api/v2/app/version` → `v4.6.0`
- `POST /api/v2/torrents/add urls=<magnet>` → `Ok.`, job appears in `torrents/info`

End-to-end CLI download (the original task — get Men of a Certain Age S1 from
TMDB 16208) worked earlier in the session via `download.py`. That code path is
unchanged behind `src/watchseries/scraper.py`.

## What's broken right now (the bug I was mid-fix on when interrupted)

When I posted the magnet to `/api/v2/torrents/add`, the job ran, reported
`state=pausedUP progress=100%`, but **no files appeared on disk**. Root cause:

- The magnet's `dn=` was `Men.of.a.Certain.Age.S01E01.1080p` (release-style).
- The worker used that as `job.title` and passed it to the videasy API as
  `title=Men.of.a.Certain.Age.S01E01.1080p`, which returns
  `{"error":"Movie not found"}` (HTTP 500).
- Our `_fetch_ciphertext` treats 500 as "doesn't exist" and returns `None`.
- `_download_episode` then `return`s silently, the outer loop says
  "nothing to do, done!", and marks the job complete with zero files.

**Fix in progress** (already edited in `jobs.py:make_magnet`):
- Magnet `xs=` payload now also carries clean `title=` and `year=` query params.
- Worker needs to parse those out of `xs=` and use them when calling
  `get_sources`. **This is the next code change to make.** Also need to update
  `JobManager.add_from_magnet` and `_parse_magnet` to surface the title/year,
  and use them when constructing the `Job`.

Secondary fix to do at the same time: stop silently treating "no sources" as
success. If the worker can't find a single playable source for any requested
unit of work, the job should end in `STATE_ERROR` so Sonarr's history shows
the failure (right now it'd just see a clean-completed-empty torrent, which is
worse than failing visibly).

## Remaining work (in order)

1. **Finish the magnet-title bug fix** (jobs.py — parser + worker; surface
   "no episodes found" as `STATE_ERROR`; verify by re-posting a magnet to
   `/api/v2/torrents/add` and watching files appear in `/tmp/wsg-test`).
2. **Dockerfile** — base `python:3.12-slim`, install `nodejs`, `ffmpeg`,
   `npm ci`, `pip install -r requirements.txt`, run `uvicorn`.
3. **requirements.txt** — `fastapi`, `uvicorn[standard]`, `python-multipart`.
   (We needed `python-multipart` mid-session; it's not in the project pin yet.)
4. **docker-compose.yml** in this repo — exposes one port (e.g. 8765), mounts
   the host downloads dir read-write, joins `caddy_network`, restart unless-stopped.
5. **Add `HomeLab/services/watchseries-grabber/`** on home.silberg.cloud:
   `docker-compose.yml` pointing at this repo (clone or local build),
   `.env` with config, Makefile target. Mount `/mnt/nas/media2/downloads:/downloads`
   so Sonarr (which already maps that path) sees finished files at the same path.
6. **Caddy entry** so `https://watchseries-grabber.home.silberg.cloud` resolves.
7. **README** — server install, env vars, the Prowlarr setup steps (Custom
   Indexer → Torznab, base URL `http://watchseries-grabber:8765/torznab`),
   the Sonarr setup steps (Download Client → qBittorrent, host
   `watchseries-grabber`, port `8765`, no auth needed but accept anything,
   category e.g. `tv-watchseries`). Save path inside the container is what
   Sonarr's `Remote Path Mapping` should resolve to its
   `/mnt/nas/media2/downloads` view.

## Home server context (from `HomeLab/CLAUDE.md`)

- Host: `damian@home.silberg.cloud` (192.168.2.200). SSH key works (added host
  key this session).
- Services managed via `~/HomeLab/services/` with a `Makefile` per service.
  Convention: each service has its own subdir with `docker-compose.yml` + `.env`.
- Shared external Docker network: `caddy_network`. Caddy terminates HTTPS for
  `*.home.silberg.cloud`.
- Sonarr container's volume map:
  - `./config:/config`
  - `/mnt/nas/media2/tv:/tv`
  - `/mnt/nas/media2/downloads:/downloads`
- TV library lives at `/mnt/nas/media2/tv/`.
- Downloads staging at `/mnt/nas/media2/downloads/`.
- Same `/downloads` path is shared with the qBittorrent (real) container under
  Gluetun VPN. Our grabber should use the **same path** so Sonarr's existing
  category/path mappings work without modification — i.e., container-side
  `/downloads` = host `/mnt/nas/media2/downloads`.
- TZ: `America/Phoenix`. PUID=1000, PGID=100.

## Sonarr/Prowlarr wiring (planned)

1. **Prowlarr** → Indexers → Add → Generic Torznab. URL
   `http://watchseries-grabber:8765/torznab`. API key: any non-empty (we don't
   enforce). Categories: 5000 (TV) and/or 2000 (Movies). Sync to Sonarr.
2. **Sonarr** → Settings → Download Clients → qBittorrent.
   - Host: `watchseries-grabber`, Port: `8765`
   - URL Base: `/` (no extra path)
   - Username/Password: `watchseries` / anything (login is a no-op)
   - Category: `tv-watchseries`
   - "Initial State": Start
3. **Sonarr** → Indexers → enable the synced Torznab indexer; allow it for
   the desired series.
4. Recommended: enable **TMDB_API_KEY** in our service so text-only Sonarr
   searches work, otherwise only ID-based ones will resolve.

## Future ideas (out of MVP scope, raised by the user)

- A small web UI ("paste a watchseries.bar URL, hit go") that's useful when
  Sonarr isn't already tracking the show.
- Radarr integration — already supported by virtue of the indexer's
  `t=movie` and the worker's `media_type=movie` path; just needs a similar
  Sonarr-style wiring guide in the README.
- Support more upstream `videasy.net` providers as fallback if `mb-flix`
  fails for a given show.

## Open questions to resolve at deploy time

- Where does the code live on the server? Two viable options:
  (a) clone `Watchseries-Downloader` to `~/` and let the
      `HomeLab/services/watchseries-grabber/docker-compose.yml` build it from
      the local clone, or
  (b) publish an image to ghcr and pull it. (a) is simpler; (b) is cleaner.
- Should `WSG_DOWNLOAD_DIR=/downloads` host-side path be the same as Sonarr's
  staging dir (`/mnt/nas/media2/downloads`) so Sonarr imports without remote
  path mapping? Strong yes — keep it identical to the qBittorrent setup.

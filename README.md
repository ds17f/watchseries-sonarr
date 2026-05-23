# watchseries-sonarr

> Use `watchseries.bar` as a normal indexer + download client for Sonarr and
> Radarr — or as a standalone CLI to grab a show.

Two things in one repo:

1. **`download.py`** — a CLI that takes a `watchseries.bar` URL and writes
   MP4s to disk. Useful by itself.
2. **A small service** that pretends to be a [Torznab](https://torznab.github.io/spec-1.3-draft/)
   indexer **and** a qBittorrent v2 download client. Sonarr/Radarr/Prowlarr
   treat it like any other indexer + torrent client; under the hood it
   scrapes `watchseries.bar` via [api.videasy.net](https://www.videasy.net/),
   decrypts the source list with a bundled WASM module, and `ffmpeg`s the
   HLS stream to MP4.

Source extraction is vendored from
[walterwhite-69/Videasy.net-Decryptor](https://github.com/walterwhite-69/Videasy.net-Decryptor) (MIT).

## Why both pieces

The CLI is the smallest possible thing: "I want this show, run a command,
get files." The service exists for when you want to keep doing that without
running commands by hand — Sonarr's normal search/grab workflow does it for
you, with renaming, library import, and notifications.

You can use one without the other. They share `src/watchseries/scraper.py`
so any improvements benefit both.

---

## Use case 1 — CLI

### Setup

```sh
git clone https://github.com/ds17f/watchseries-sonarr.git
cd watchseries-sonarr
npm install
pip install fastapi 'uvicorn[standard]' python-multipart   # service deps; CLI uses stdlib only
```

System deps: Python 3.10+, Node.js 18+, `ffmpeg` on PATH.

### Run

```sh
# Whole series — walks seasons until none more are available.
python3 download.py "https://watchseries.bar/tv/men-of-a-certain-age/16208"

# Single season at lower quality, custom output dir.
python3 download.py URL --season 1 --end-season 1 --quality 720p --out /mnt/media

# Single episode.
python3 download.py URL --season 2 --episode 5 --end-season 2

# Movie.
python3 download.py "https://watchseries.bar/movie/<slug>/<tmdb_id>"
```

Default output: `~/Downloads/watchseries/<Show Title>/Season NN/<Title> - sNNeMM.mp4`.

Already-downloaded episodes (>1MB) are skipped on re-run, so it's safe to
resume after an interruption.

Flags:

| Flag           | Default     | Meaning |
|----------------|-------------|---------|
| `--season N`   | `1`         | start at season N |
| `--episode N`  | `1`         | start at episode N (within the start season) |
| `--end-season N` | none      | stop after season N |
| `--quality Q`  | `1080p`     | preferred quality (`1080p`/`720p`/`360p`); falls back if unavailable |
| `--out DIR`    | `~/Downloads/watchseries` | output root |

---

## Use case 2 — service wired into Sonarr / Radarr

### What it gives you

- An indexer at `http://<host>:8765/torznab/api` that responds to
  `t=caps`, `t=tvsearch`, `t=movie`, `t=search` — i.e. exactly what
  Prowlarr expects from a "Generic Torznab" entry.
- A qBittorrent-compatible API at `http://<host>:8765/api/v2/...` that
  satisfies Sonarr's "qBittorrent" download client.

The two routers live in one FastAPI process (`src/watchseries/main.py`), so
it's one container, one port, one log.

### Run with Docker

```sh
git clone https://github.com/ds17f/watchseries-sonarr.git
cd watchseries-sonarr
cp .env.example .env
$EDITOR .env    # at minimum set DOWNLOADS_DIR to where you want files

docker compose up -d --build
curl http://localhost:8765/health
# {"status":"ok","missing":[],"download_dir":"/downloads","jobs":0}
```

### Configuration via `.env`

| Variable             | Default                | Purpose |
|----------------------|------------------------|---------|
| `DOWNLOADS_DIR`      | `./downloads`          | Host path bind-mounted as `/downloads` inside the container. Point at Sonarr's existing download folder so import works without remote-path mapping. |
| `HOST_PORT`          | `8765`                 | Host port to expose the service on. |
| `PUID` / `PGID`      | `1000` / `1000`        | User/group the container runs as. Must own the `DOWNLOADS_DIR`. (Common NAS setups use 1000/100.) |
| `TZ`                 | `UTC`                  | Timezone (for log timestamps + scheduling). |
| `WSG_INDEXER_TITLE`  | `WatchSeries.bar`      | Display name in the Torznab capabilities. |
| `TMDB_API_KEY`       | _(unset)_              | Optional. If set, the indexer can resolve `tvdbid`/`imdbid` and do text search. Without it, callers must pass `tmdbid` directly. Get a key at https://www.themoviedb.org/settings/api. |

### Wire it into Sonarr

#### 1. Add the indexer (in Prowlarr, recommended)

Prowlarr → Indexers → Add → **Generic Torznab**:

- **URL:** `http://watchseries-grabber:8765/torznab` (use the container name if
  Prowlarr is in the same Docker network; otherwise the host + port)
- **API Path:** `/api`
- **API Key:** any non-empty string (we don't enforce one)
- **Categories:** 5000 (TV), 2040 (Movies/HD)

Then sync to Sonarr / Radarr. You can also add it directly in
Sonarr → Settings → Indexers → Add → Torznab if you don't use Prowlarr.

#### 2. Add the download client

Sonarr → Settings → Download Clients → Add → **qBittorrent**:

- **Host:** `watchseries-grabber` (Docker name) or your host's IP
- **Port:** `8765` (or whatever `HOST_PORT` you set)
- **URL Base:** _(leave blank)_
- **Username:** `watchseries`
- **Password:** anything
- **Category:** `tv-watchseries` (Sonarr) / `movies-watchseries` (Radarr)
- **Use SSL:** off

Authentication is a no-op; we accept any creds and any login attempt
succeeds.

#### 3. Path mapping

Inside the container files are written to `/downloads`, which is the
`DOWNLOADS_DIR` host path you set in `.env`. If that same host path is
mounted into your Sonarr container as `/downloads` too (the typical setup),
Sonarr's import works with no Remote Path Mapping.

If Sonarr sees the same files under a different path, configure
Sonarr → Settings → Download Clients → Remote Path Mappings.

### Caveats

- **`TMDB_API_KEY` strongly recommended.** Without it, only ID-based searches
  resolve — Sonarr usually passes `tvdbid` for shows, which means we need
  to look up the TMDB id, which needs the key.
- **One upstream provider currently.** We use `mb-flix` (one of ~12
  endpoints exposed by `api.videasy.net`). If it stops working, swap the
  `API` constant in `src/watchseries/scraper.py`. Known alternates:
  `cdn`, `moviebox`, `1movies`, `m4uhd`, `hdmovie`, `lamovie`, `superflix`,
  `meine`.
- **No real torrent.** The `urn:btih:` info-hash in our magnets is a
  deterministic SHA-1 of the request payload, not a real torrent hash. Don't
  put us behind a VPN that blocks non-torrent traffic on these "magnet"
  URIs — they're plain HTTP under the hood.

### How does it work?

When Sonarr searches the indexer, we return RSS items with magnets like:

```
magnet:?xt=urn:btih:<sha1(payload)>
       &dn=<release.name>
       &xs=watchseries://tv/<tmdb_id>/s<N>/e<M>?quality=1080p&title=...&year=...
```

The `xs=` parameter carries the real payload — media type, TMDB id,
season/episode, clean title (needed for the upstream API match), year.
Sonarr forwards this magnet to its download client; our fake qBittorrent
unwraps it and starts a job. The job runs the same pipeline as the CLI:

```
api.videasy.net/mb-flix → hex ciphertext
node decrypt.js          → JSON {sources: [{url, quality}], subtitles: [...]}
ffmpeg                   → out.mp4 in /downloads/<release.name>/Season NN/
```

While downloading, the job reports `state: downloading` with a `progress`
fraction computed from ffmpeg's stderr; when done it reports `state:
pausedUP` with `content_path` pointing at the folder Sonarr should import.

---

## Layout

```
.
├── decrypt.js              # WASM bridge (vendored, MIT)
├── module.wasm
├── package.json            # crypto-js, hashids
├── requirements.txt        # fastapi, uvicorn, python-multipart
├── Dockerfile
├── docker-compose.yml
├── docker-compose.homelab.yml   # external network override (optional)
├── .env.example
├── download.py             # CLI entry point
└── src/watchseries/
    ├── scraper.py          # videasy fetch + decrypt + ffmpeg
    ├── jobs.py             # JobManager + magnet codec
    ├── torznab.py          # /torznab/api router
    ├── fakeqbt.py          # /api/v2 router (qBittorrent mimic)
    └── main.py             # FastAPI app
```

## Legal

This scrapes a piracy aggregator. Where you are matters. Use accordingly.

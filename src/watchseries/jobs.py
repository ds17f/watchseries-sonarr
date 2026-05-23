"""Job manager: tracks fake-torrent downloads, runs scraper in background threads.

A "job" is what Sonarr thinks is a torrent. From our side it's a request to
download one or more episodes/a movie via the scraper. Each job has a stable
20-byte hex hash so we can present it to Sonarr like a real torrent.
"""
from __future__ import annotations

import hashlib
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from .scraper import (
    ffmpeg_download,
    get_sources,
    pick_source,
    safe_name,
    fetch_subtitle,
)

# qBittorrent state strings Sonarr understands.
STATE_QUEUED = "queuedDL"
STATE_DOWNLOADING = "downloading"
STATE_FINISHED = "pausedUP"   # complete + seeding-paused; Sonarr treats as done
STATE_ERROR = "error"


@dataclass
class Episode:
    season: int
    episode: int


@dataclass
class Job:
    hash: str
    name: str           # release name shown to Sonarr
    media_type: str     # "tv" | "movie"
    tmdb_id: str
    title: str
    year: str
    quality: str
    save_path: Path     # category folder Sonarr asked us to save to
    category: str
    episodes: list[Episode] = field(default_factory=list)  # explicit list of episodes
    season_pack: int | None = None  # set when whole season requested
    added_on: float = field(default_factory=time.time)
    state: str = STATE_QUEUED
    size_total: int = 0
    size_done: int = 0
    progress: float = 0.0
    error: str = ""
    files: list[Path] = field(default_factory=list)

    @property
    def content_path(self) -> Path:
        """Folder Sonarr will look in for completed files."""
        return self.save_path / safe_name(self.name)


class JobManager:
    def __init__(self, default_save_path: Path):
        self.default_save_path = default_save_path
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    # ---- public API ----

    def list(self, category: str | None = None) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        if category:
            jobs = [j for j in jobs if j.category == category]
        return jobs

    def get(self, h: str) -> Job | None:
        return self._jobs.get(h)

    def delete(self, h: str) -> None:
        with self._lock:
            self._jobs.pop(h, None)

    def add_from_magnet(self, magnet: str, save_path: Path | None,
                        category: str) -> Job | None:
        """Parse one of our magnets and start a job."""
        p = _parse_magnet(magnet)
        if p is None:
            return None
        # The xs= payload carries the clean title; the dn= is purely cosmetic.
        name = _name_from_magnet(magnet) or _release_name_for(
            p.media_type, p.title or f"tmdb-{p.tmdb_id}",
            p.season, p.episode, p.quality)
        h = _magnet_hash(magnet)
        if h in self._jobs:
            return self._jobs[h]
        episodes: list[Episode] = []
        if p.media_type == "tv" and p.season is not None:
            if p.episode is not None:
                episodes = [Episode(p.season, p.episode)]
            # season-only = whole season (episodes left empty, worker discovers)
        job = Job(
            hash=h, name=name, media_type=p.media_type, tmdb_id=p.tmdb_id,
            title=p.title or f"tmdb-{p.tmdb_id}", year=p.year, quality=p.quality,
            save_path=(save_path or self.default_save_path),
            category=category, episodes=episodes,
        )
        # If the magnet carries season but no episode, also remember the
        # bounded season so the worker doesn't walk the entire series.
        if p.media_type == "tv" and p.season is not None and p.episode is None:
            job.season_pack = p.season
        with self._lock:
            self._jobs[h] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    # ---- worker ----

    def _run(self, job: Job) -> None:
        try:
            job.state = STATE_DOWNLOADING
            job.content_path.mkdir(parents=True, exist_ok=True)

            if job.media_type == "movie":
                self._download_movie(job)
            elif job.episodes:
                for ep in job.episodes:
                    self._download_episode(job, ep.season, ep.episode)
            elif job.season_pack is not None:
                self._download_season(job, job.season_pack)
            else:
                self._download_series(job)

            if not job.files:
                job.state = STATE_ERROR
                job.error = "no episodes were downloaded"
                return
            job.progress = 1.0
            job.state = STATE_FINISHED
        except Exception as e:
            job.state = STATE_ERROR
            job.error = repr(e)

    def _download_movie(self, job: Job) -> None:
        srcs = get_sources("movie", job.tmdb_id, job.title, year=job.year)
        if srcs is None:
            raise RuntimeError("no sources for movie")
        src = pick_source(srcs.sources, job.quality)
        if not src:
            raise RuntimeError("no playable source quality")
        dest = job.content_path / f"{safe_name(job.title)}.mp4"
        self._run_ffmpeg(job, src.url, dest)
        for s in srcs.subtitles:
            ext = Path(s.url).suffix or ".vtt"
            fetch_subtitle(s.url, dest.with_name(f"{dest.stem}.{s.lang}{ext}"))

    def _download_episode(self, job: Job, season: int, episode: int) -> None:
        srcs = get_sources("tv", job.tmdb_id, job.title, season, episode)
        if srcs is None:
            return  # episode missing — skip silently
        src = pick_source(srcs.sources, job.quality)
        if not src:
            return
        season_dir = job.content_path / f"Season {season:02d}"
        dest = season_dir / f"{safe_name(job.title)} - s{season:02d}e{episode:02d}.mp4"
        self._run_ffmpeg(job, src.url, dest)
        for s in srcs.subtitles:
            ext = Path(s.url).suffix or ".vtt"
            fetch_subtitle(s.url, dest.with_name(f"{dest.stem}.{s.lang}{ext}"))

    def _download_season(self, job: Job, season: int) -> None:
        ep = 1
        while True:
            srcs = get_sources("tv", job.tmdb_id, job.title, season, ep)
            if srcs is None:
                break
            src = pick_source(srcs.sources, job.quality)
            if not src:
                break
            season_dir = job.content_path / f"Season {season:02d}"
            dest = season_dir / f"{safe_name(job.title)} - s{season:02d}e{ep:02d}.mp4"
            self._run_ffmpeg(job, src.url, dest)
            for s in srcs.subtitles:
                ext = Path(s.url).suffix or ".vtt"
                fetch_subtitle(s.url, dest.with_name(f"{dest.stem}.{s.lang}{ext}"))
            ep += 1

    def _download_series(self, job: Job) -> None:
        season = 1
        empty_seasons = 0
        while True:
            before = len(job.files)
            self._download_season(job, season)
            found_any = len(job.files) > before
            if found_any:
                empty_seasons = 0
            else:
                empty_seasons += 1
                if empty_seasons >= 1:
                    break
            season += 1

    def _run_ffmpeg(self, job: Job, m3u8: str, dest: Path) -> None:
        def cb(done: float, total: float | None):
            if total:
                job.progress = min(1.0, done / total)

        ok = ffmpeg_download(m3u8, dest, progress_cb=cb)
        if not ok:
            raise RuntimeError(f"ffmpeg failed for {dest.name}")
        job.files.append(dest)
        try:
            job.size_done += dest.stat().st_size
            job.size_total = max(job.size_total, job.size_done)
        except FileNotFoundError:
            pass


# ---- magnet helpers ----

MAGNET_SCHEME = "watchseries"  # encoded inside xs= param of magnet


def make_magnet(media_type: str, tmdb_id: str, title: str,
                season: int | None = None, episode: int | None = None,
                quality: str = "1080p", year: str = "") -> str:
    """Build a Torznab magnet that encodes our identifiers.

    Sonarr/Prowlarr treat it as a generic torrent magnet (xt is a fake info-hash
    we deterministically derive). The real payload is in the xs= param: it
    carries the media_type / tmdb_id / season / episode AND the clean title +
    year — the worker needs the title to query the videasy API, since IDs
    alone aren't sufficient for the upstream match.
    """
    payload_path = f"{media_type}/{tmdb_id}"
    if season is not None:
        payload_path += f"/s{season}"
    if episode is not None:
        payload_path += f"/e{episode}"
    payload_qs = urllib.parse.urlencode({
        "quality": quality, "title": title, "year": year,
    })
    payload = f"{MAGNET_SCHEME}://{payload_path}?{payload_qs}"

    # Deterministic fake info-hash so Sonarr de-dupes identical requests.
    fake_btih = hashlib.sha1(payload.encode()).hexdigest()
    dn = _release_name_for(media_type, title, season, episode, quality)
    # Build magnet manually: MonoTorrent (used by Prowlarr) rejects magnets
    # where the xt value is percent-encoded (e.g. urn%3Abtih%3A...). The
    # canonical form keeps `urn:btih:` unencoded; only the rest of the
    # value (dn, xs) gets percent-encoded.
    parts = [
        f"xt=urn:btih:{fake_btih}",
        f"dn={urllib.parse.quote(dn, safe='')}",
        f"xs={urllib.parse.quote(payload, safe='')}",
    ]
    return "magnet:?" + "&".join(parts)


@dataclass
class ParsedMagnet:
    media_type: str
    tmdb_id: str
    season: int | None
    episode: int | None
    title: str
    year: str
    quality: str


def _parse_magnet(magnet: str) -> ParsedMagnet | None:
    if not magnet.startswith("magnet:?"):
        return None
    qs = urllib.parse.parse_qs(magnet[len("magnet:?"):])
    xs_list = qs.get("xs", [])
    if not xs_list:
        return None
    xs = xs_list[0]
    if not xs.startswith(f"{MAGNET_SCHEME}://"):
        return None
    payload = xs[len(MAGNET_SCHEME) + 3:]
    path, _, query = payload.partition("?")
    parts = path.split("/")
    if len(parts) < 2:
        return None
    media_type = parts[0]
    tmdb_id = parts[1]
    season = episode = None
    for p in parts[2:]:
        if p.startswith("s") and p[1:].isdigit():
            season = int(p[1:])
        elif p.startswith("e") and p[1:].isdigit():
            episode = int(p[1:])
    payload_qs = urllib.parse.parse_qs(query)
    title = (payload_qs.get("title", [""]) or [""])[0]
    year = (payload_qs.get("year", [""]) or [""])[0]
    quality = (payload_qs.get("quality", ["1080p"]) or ["1080p"])[0]
    return ParsedMagnet(media_type, tmdb_id, season, episode, title, year, quality)


def _magnet_hash(magnet: str) -> str:
    qs = urllib.parse.parse_qs(magnet[len("magnet:?"):])
    for xt in qs.get("xt", []):
        if xt.startswith("urn:btih:"):
            return xt[len("urn:btih:"):].lower()
    return hashlib.sha1(magnet.encode()).hexdigest()


def _name_from_magnet(magnet: str) -> str | None:
    qs = urllib.parse.parse_qs(magnet[len("magnet:?"):])
    names = qs.get("dn", [])
    return names[0] if names else None


def _release_name_for(media_type: str, title: str, season: int | None,
                      episode: int | None, quality: str) -> str:
    safe = safe_name(title).replace(" ", ".")
    if media_type == "movie":
        return f"{safe}.{quality}.WEBDL.watchseries"
    parts = [safe]
    if season is not None and episode is not None:
        parts.append(f"S{season:02d}E{episode:02d}")
    elif season is not None:
        parts.append(f"S{season:02d}")
    parts += [quality, "WEBDL", "watchseries"]
    return ".".join(parts)


def _release_name(magnet: str, media_type: str, tmdb_id: str,
                  season: int | None, episode: int | None) -> str:
    dn = _name_from_magnet(magnet)
    if dn:
        return dn
    return _release_name_for(media_type, f"tmdb-{tmdb_id}", season, episode, "1080p")

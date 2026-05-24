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
    tmdb_season_episode_count,
    tmdb_seasons,
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
    expected_units: int = 1  # total episodes/files this job will produce
    current_unit_progress: float = 0.0  # 0-1 of the file ffmpeg is on right now
    current_unit_label: str = ""  # e.g. "S01E07" or "Movie"
    current_unit_started_at: float = 0.0  # epoch seconds; for ETA
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
    def __init__(self, default_save_path: Path, store=None):
        self.default_save_path = default_save_path
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._store = store  # optional JobStore for persistence
        if store is not None:
            # Resume: load any persisted jobs, restart workers for ones that
            # were mid-flight before shutdown.
            for job in store.load_all():
                self._jobs[job.hash] = job
                if job.state in (STATE_QUEUED, STATE_DOWNLOADING):
                    threading.Thread(target=self._run, args=(job,),
                                     daemon=True).start()

    # ---- public API ----

    def list(self, category: str | None = None) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        if category:
            jobs = [j for j in jobs if j.category == category]
        return jobs

    def get(self, h: str) -> Job | None:
        return self._jobs.get(h)

    def retry(self, h: str) -> bool:
        """Restart a job. Re-uses the existing record (so already-downloaded
        files are kept and skipped) and respawns the worker thread."""
        job = self._jobs.get(h)
        if job is None:
            return False
        # Reset transient state, keep files list so resume works.
        job.error = ""
        job.state = STATE_QUEUED
        job.progress = 0.0
        job.current_unit_progress = 0.0
        job.current_unit_label = ""
        self._persist(job)
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return True

    def delete(self, h: str) -> None:
        with self._lock:
            self._jobs.pop(h, None)
        if self._store is not None:
            try:
                self._store.delete(h)
            except Exception:
                pass

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
        self._persist(job)
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _persist(self, job: Job) -> None:
        if self._store is not None:
            try:
                self._store.save(job)
            except Exception:
                pass  # persistence is best-effort; don't break a download

    # ---- worker ----

    def _run(self, job: Job) -> None:
        missed: list[str] = []
        try:
            job.state = STATE_DOWNLOADING
            job.error = ""
            # Set expected episode count up-front so progress is meaningful.
            if job.media_type == "movie":
                job.expected_units = 1
            elif job.episodes:
                job.expected_units = max(1, len(job.episodes))
            elif job.season_pack is not None:
                n = tmdb_season_episode_count(job.tmdb_id, job.season_pack)
                job.expected_units = n or 20  # fallback estimate
            else:
                seasons = tmdb_seasons(job.tmdb_id) or [1]
                total = 0
                for s in seasons:
                    n = tmdb_season_episode_count(job.tmdb_id, s)
                    total += n or 20
                job.expected_units = max(1, total)
            self._persist(job)
            job.content_path.mkdir(parents=True, exist_ok=True)

            if job.media_type == "movie":
                if not self._download_movie(job):
                    missed.append("movie")
            elif job.episodes:
                for ep in job.episodes:
                    if not self._download_episode(job, ep.season, ep.episode):
                        missed.append(f"S{ep.season:02d}E{ep.episode:02d}")
            elif job.season_pack is not None:
                missed += self._download_season(job, job.season_pack)
            else:
                missed += self._download_series(job)

            if not job.files:
                job.state = STATE_ERROR
                job.error = "no episodes were downloaded"
                self._persist(job)
                return
            if missed:
                # Partial success: some episodes failed. Mark as error so the
                # user can see what's incomplete; files already downloaded
                # are kept on disk for Sonarr to import what's available.
                job.state = STATE_ERROR
                job.error = f"missing episodes: {', '.join(missed[:20])}" + (
                    f" (+{len(missed)-20} more)" if len(missed) > 20 else "")
                self._persist(job)
                return
            job.progress = 1.0
            job.state = STATE_FINISHED
            self._persist(job)
        except Exception as e:
            job.state = STATE_ERROR
            job.error = repr(e)
            self._persist(job)

    def _download_movie(self, job: Job) -> bool:
        dest = job.content_path / f"{safe_name(job.title)}.mp4"
        job.current_unit_label = "Movie"
        if _existing_ok(dest):
            self._record_file(job, dest)
            return True
        srcs = _retry_sources("movie", job.tmdb_id, job.title, year=job.year)
        if srcs is None:
            return False
        src = pick_source(srcs.sources, job.quality)
        if not src:
            return False
        try:
            self._run_ffmpeg(job, src.url, dest)
        except Exception:
            return False
        for s in srcs.subtitles:
            ext = Path(s.url).suffix or ".vtt"
            fetch_subtitle(s.url, dest.with_name(f"{dest.stem}.{s.lang}{ext}"))
        return True

    def _download_episode(self, job: Job, season: int, episode: int) -> bool:
        season_dir = job.content_path / f"Season {season:02d}"
        dest = season_dir / f"{safe_name(job.title)} - s{season:02d}e{episode:02d}.mp4"
        job.current_unit_label = f"S{season:02d}E{episode:02d}"
        if _existing_ok(dest):
            self._record_file(job, dest)
            return True
        srcs = _retry_sources("tv", job.tmdb_id, job.title, season, episode)
        if srcs is None:
            return False
        src = pick_source(srcs.sources, job.quality)
        if not src:
            return False
        try:
            self._run_ffmpeg(job, src.url, dest)
        except Exception:
            return False
        for s in srcs.subtitles:
            ext = Path(s.url).suffix or ".vtt"
            fetch_subtitle(s.url, dest.with_name(f"{dest.stem}.{s.lang}{ext}"))
        return True

    def _download_season(self, job: Job, season: int) -> list[str]:
        """Download every episode in a season. Returns list of episode
        labels that failed. Bounded by TMDB episode count when available;
        otherwise falls back to a probe loop with retries."""
        missed: list[str] = []
        total = tmdb_season_episode_count(job.tmdb_id, season)
        if total is None:
            # No TMDB info — probe until we get N consecutive failures.
            return self._download_season_probe(job, season)
        for ep in range(1, total + 1):
            if not self._download_episode(job, season, ep):
                missed.append(f"S{season:02d}E{ep:02d}")
        return missed

    def _download_season_probe(self, job: Job, season: int) -> list[str]:
        """Fallback when we don't know the episode count. Tolerate up to 3
        consecutive misses before assuming we've run past the end."""
        missed: list[str] = []
        consecutive_miss = 0
        ep = 1
        while consecutive_miss < 3 and ep <= 100:
            if self._download_episode(job, season, ep):
                consecutive_miss = 0
            else:
                consecutive_miss += 1
                missed.append(f"S{season:02d}E{ep:02d}")
            ep += 1
        # Trim trailing misses — those are "past the end of the season".
        while missed and missed[-1] == f"S{season:02d}E{ep-1:02d}":
            missed.pop()
            ep -= 1
        return missed

    def _download_series(self, job: Job) -> list[str]:
        """Download every season the show has. Prefers TMDB season list;
        falls back to probing seasons until we hit one with no episodes."""
        missed: list[str] = []
        seasons = tmdb_seasons(job.tmdb_id)
        if seasons:
            for s in seasons:
                missed += self._download_season(job, s)
            return missed
        # Probe-mode fallback
        season = 1
        empty = 0
        while empty < 1 and season < 50:
            before = len(job.files)
            missed += self._download_season(job, season)
            if len(job.files) == before:
                empty += 1
            else:
                empty = 0
            season += 1
        return missed

    def _run_ffmpeg(self, job: Job, m3u8: str, dest: Path) -> None:
        def cb(done: float, total: float | None):
            if total:
                job.current_unit_progress = min(1.0, done / total)
                _update_progress(job)

        job.current_unit_progress = 0.0
        job.current_unit_started_at = time.time()
        ok = ffmpeg_download(m3u8, dest, progress_cb=cb)
        if not ok:
            raise RuntimeError(f"ffmpeg failed for {dest.name}")
        job.current_unit_progress = 0.0
        self._record_file(job, dest)

    def _record_file(self, job: Job, dest: Path) -> None:
        if dest in job.files:
            return  # already tracked (resume case)
        job.files.append(dest)
        try:
            sz = dest.stat().st_size
            job.size_done += sz
            job.size_total = max(job.size_total, job.size_done)
        except FileNotFoundError:
            pass
        _update_progress(job)
        self._persist(job)


def _retry_sources(media_type: str, tmdb_id: str, title: str,
                   season: int | None = None, episode: int | None = None,
                   year: str = "", tries: int = 3, backoff: float = 2.0):
    """Wrap get_sources with retries — upstream returns 500 for both
    'episode doesn't exist' and transient errors, so retry a few times
    before giving up."""
    last: object = None
    for i in range(tries):
        try:
            last = get_sources(media_type, tmdb_id, title,
                               season=season, episode=episode, year=year)
            if last is not None:
                return last
        except Exception:
            pass
        time.sleep(backoff * (i + 1))
    return last


def _update_progress(job: Job) -> None:
    """Compute job-wide progress as (files_done + current_file_fraction)
    divided by the expected total. Clamped to [0, 1)."""
    done = len(job.files)
    fraction = job.current_unit_progress if 0 < done + 1 <= job.expected_units else 0
    total = max(1, job.expected_units)
    p = (done + fraction) / total
    job.progress = min(0.999, p)  # don't report 1.0 until _run sets it


def _existing_ok(dest: Path) -> bool:
    """Treat any file >1MB at the expected path as already-downloaded."""
    try:
        return dest.is_file() and dest.stat().st_size > 1_000_000
    except OSError:
        return False


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

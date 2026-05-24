"""Source-extraction + HLS download logic.

Single source of truth for talking to api.videasy.net. Used by both the CLI
(download.py) and the FastAPI service.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

API = "https://api.videasy.net/mb-flix/sources-with-title"
API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Referer": "https://cineby.sc/",
    "Origin": "https://cineby.sc",
}
FFMPEG_REFERER = "https://cineby.sc/"
QUALITY_ORDER = ["1080p", "720p", "360p"]

# Repo root (where decrypt.js + module.wasm live)
REPO = Path(__file__).resolve().parents[2]


@dataclass
class Source:
    url: str
    quality: str  # "360p" | "720p" | "1080p"


@dataclass
class Subtitle:
    url: str
    lang: str
    language: str


@dataclass
class Sources:
    sources: list[Source]
    subtitles: list[Subtitle]


def _fetch_ciphertext(media_type: str, tmdb_id: str, title: str,
                      season: int | None, episode: int | None,
                      year: str = "") -> str | None:
    qs = {
        "title": title,
        "mediaType": media_type,
        "year": year,
        "tmdbId": tmdb_id,
        "imdbId": "",
    }
    if media_type == "tv":
        qs["seasonId"] = str(season or 1)
        qs["episodeId"] = str(episode or 1)
    req = urllib.request.Request(f"{API}?{urllib.parse.urlencode(qs)}", headers=API_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", errors="replace").strip()
    except urllib.error.HTTPError as e:
        if e.code in (404, 500):
            return None
        raise
    return body or None


def _decrypt(ct: str, tmdb_id: str) -> dict:
    out = subprocess.run(
        ["node", "decrypt.js", ct, tmdb_id],
        cwd=REPO, capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(f"decrypt.js failed: {out.stderr}")
    data = json.loads(out.stdout)
    if not data.get("success"):
        raise RuntimeError(f"decryption error: {data.get('error')}")
    return data["data"]


def get_sources(media_type: str, tmdb_id: str, title: str,
                season: int | None = None, episode: int | None = None,
                year: str = "") -> Sources | None:
    """Fetch + decrypt source list. Returns None if the item isn't found."""
    ct = _fetch_ciphertext(media_type, tmdb_id, title, season, episode, year)
    if not ct:
        return None
    raw = _decrypt(ct, tmdb_id)
    return Sources(
        sources=[Source(url=s["url"], quality=s["quality"]) for s in raw.get("sources", [])],
        subtitles=[Subtitle(url=s["url"], lang=s.get("lang", "und"),
                            language=s.get("language", ""))
                   for s in raw.get("subtitles", [])],
    )


def pick_source(sources: list[Source], preferred: str = "1080p") -> Source | None:
    by_q = {s.quality: s for s in sources}
    for q in [preferred] + [q for q in QUALITY_ORDER if q != preferred]:
        if q in by_q:
            return by_q[q]
    return sources[0] if sources else None


def safe_name(s: str) -> str:
    return re.sub(r"[^-A-Za-z0-9_.() ]+", "-", s).strip()


def parse_watchseries_url(url: str) -> tuple[str, str, str]:
    """Parse a watchseries.bar URL → (media_type, slug, tmdb_id)."""
    m = re.match(r"https?://watchseries\.bar/(tv|movie)/([^/]+)/(\d+)/?", url)
    if not m:
        raise ValueError(f"Not a watchseries.bar URL: {url}")
    return m.group(1), m.group(2), m.group(3)


def ffmpeg_download(m3u8: str, dest: Path, progress_cb=None,
                    proc_sink=None) -> bool:
    """Download an HLS stream to MP4. Returns True on success.

    progress_cb(seconds_done: float, total_seconds: float | None) is called
    periodically while downloading, parsed from ffmpeg's stderr.

    proc_sink(proc) is called with the Popen as soon as ffmpeg starts, so
    the caller can terminate it on cancel. proc_sink(None) is called when
    the process has exited.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "info",
        "-referer", FFMPEG_REFERER,
        "-user_agent", API_HEADERS["User-Agent"],
        "-i", m3u8, "-c", "copy", "-bsf:a", "aac_adtstoasc",
        "-f", "mp4", "-y", str(tmp),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
    if proc_sink is not None:
        proc_sink(proc)
    total_seconds = None
    last_seconds = 0.0
    assert proc.stderr is not None
    for line in proc.stderr:
        if total_seconds is None:
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", line)
            if m:
                h, mi, s = m.groups()
                total_seconds = int(h) * 3600 + int(mi) * 60 + float(s)
        m = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
        if m:
            h, mi, s = m.groups()
            last_seconds = int(h) * 3600 + int(mi) * 60 + float(s)
            if progress_cb:
                progress_cb(last_seconds, total_seconds)
    proc.wait()
    if proc_sink is not None:
        proc_sink(None)
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 100_000:
        if tmp.exists():
            tmp.unlink()
        return False
    tmp.rename(dest)
    return True


def fetch_subtitle(url: str, dest: Path) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            dest.write_bytes(r.read())
        return True
    except Exception:
        return False


def tmdb_season_episode_count(tmdb_id: str, season: int) -> int | None:
    """Return the number of episodes in a season via TMDB, or None if no
    API key set / season missing. Used so season-pack workers iterate a
    bounded range instead of stopping on the first upstream hiccup."""
    import os
    key = os.environ.get("TMDB_API_KEY", "")
    if not key:
        return None
    url = (f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}"
           f"?api_key={key}")
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read())
    except Exception:
        return None
    eps = data.get("episodes")
    if not eps:
        return None
    return len(eps)


def tmdb_seasons(tmdb_id: str) -> list[int] | None:
    """Return the list of season numbers (excluding specials/0) for a show.
    Used by full-series workers to iterate seasons in TMDB order."""
    import os
    key = os.environ.get("TMDB_API_KEY", "")
    if not key:
        return None
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={key}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read())
    except Exception:
        return None
    seasons = data.get("seasons", [])
    return [s["season_number"] for s in seasons if s.get("season_number", 0) > 0]


def check_environment() -> list[str]:
    """Return a list of missing dependencies (empty = ready)."""
    missing = []
    if not shutil.which("ffmpeg"):
        missing.append("ffmpeg")
    if not shutil.which("node"):
        missing.append("node")
    if not (REPO / "decrypt.js").exists():
        missing.append("decrypt.js")
    if not (REPO / "module.wasm").exists():
        missing.append("module.wasm")
    if not (REPO / "node_modules").exists():
        missing.append("node_modules (run `npm install`)")
    return missing

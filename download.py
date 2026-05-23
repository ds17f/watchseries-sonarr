#!/usr/bin/env python3
"""CLI: download a full TV series (or single episode/movie) from watchseries.bar.

Usage:
    python3 download.py <watchseries.bar URL> [--season N] [--episode N]
                        [--quality 1080p|720p|360p] [--out DIR]

Output naming follows the Plex/Sonarr convention:
    <out>/<Title> (<Year>)/Season NN/<Title> - sNNeMM.mp4
"""
import argparse
import sys
from pathlib import Path

# Make src/ importable when run from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from watchseries.scraper import (
    QUALITY_ORDER,
    check_environment,
    fetch_subtitle,
    ffmpeg_download,
    get_sources,
    parse_watchseries_url,
    pick_source,
    safe_name,
)


def episode_exists(dest: Path) -> bool:
    return dest.exists() and dest.stat().st_size > 1_000_000


def progress_line(seconds: float, total: float | None) -> None:
    if total:
        pct = 100 * seconds / total
        print(f"\r  {seconds:7.1f}s / {total:7.1f}s ({pct:5.1f}%)", end="", flush=True)
    else:
        print(f"\r  {seconds:7.1f}s", end="", flush=True)


def download_one(media_type: str, slug: str, tmdb_id: str, *,
                 season: int | None, episode: int | None,
                 quality: str, out_root: Path) -> bool:
    title = slug.replace("-", " ").title()

    if media_type == "movie":
        label = title
        dest = out_root / f"{safe_name(title)}.mp4"
    else:
        label = f"S{season:02d}E{episode:02d}"
        dest = out_root / f"Season {season:02d}" / f"{safe_name(title)} - s{season:02d}e{episode:02d}.mp4"

    if episode_exists(dest):
        print(f"{label}: already downloaded, skipping")
        return True

    print(f"{label}: fetching sources...")
    srcs = get_sources(media_type, tmdb_id, title, season, episode)
    if srcs is None:
        return False

    src = pick_source(srcs.sources, quality)
    if not src:
        print(f"  no playable sources")
        return False
    print(f"  -> {src.quality} via ffmpeg")
    ok = ffmpeg_download(src.url, dest, progress_cb=progress_line)
    print()
    if not ok:
        print(f"  ! download failed")
        return False
    for sub in srcs.subtitles:
        ext = Path(sub.url).suffix or ".vtt"
        sub_dest = dest.with_name(f"{dest.stem}.{sub.lang}{ext}")
        if fetch_subtitle(sub.url, sub_dest):
            print(f"  -> subtitle {sub.lang}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="watchseries.bar TV or movie URL")
    ap.add_argument("--season", type=int)
    ap.add_argument("--episode", type=int)
    ap.add_argument("--end-season", type=int)
    ap.add_argument("--quality", default="1080p", choices=QUALITY_ORDER)
    ap.add_argument("--out", default=str(Path.home() / "Downloads" / "watchseries"))
    args = ap.parse_args()

    missing = check_environment()
    if missing:
        sys.exit(f"missing: {', '.join(missing)}")

    media_type, slug, tmdb_id = parse_watchseries_url(args.url)
    title = slug.replace("-", " ").title()
    out_root = Path(args.out) / safe_name(title)
    print(f"{'Movie' if media_type == 'movie' else 'Show'}: {title} (TMDB {tmdb_id}) -> {out_root}")

    if media_type == "movie":
        download_one("movie", slug, tmdb_id,
                     season=None, episode=None,
                     quality=args.quality, out_root=out_root)
        return

    season = args.season or 1
    consecutive_empty_seasons = 0
    while True:
        if args.end_season and season > args.end_season:
            break
        ep = args.episode if (season == (args.season or 1) and args.episode) else 1
        found_any = False
        while True:
            ok = download_one("tv", slug, tmdb_id,
                              season=season, episode=ep,
                              quality=args.quality, out_root=out_root)
            if not ok:
                break
            found_any = True
            ep += 1
        if found_any:
            consecutive_empty_seasons = 0
        else:
            consecutive_empty_seasons += 1
            if consecutive_empty_seasons >= 1:
                print(f"No more episodes; done.")
                break
        season += 1


if __name__ == "__main__":
    main()

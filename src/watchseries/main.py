"""FastAPI entrypoint for watchseries-grabber.

Mounts two routers:
  - /torznab/api     Torznab indexer (for Prowlarr/Sonarr/Radarr to search)
  - /api/v2/         Fake qBittorrent (for Sonarr/Radarr to "download" through)

Configured by env vars:
  WSG_DOWNLOAD_DIR  default save path for new "torrents" (default /downloads)
  WSG_INDEXER_TITLE display name in indexer caps
  TMDB_API_KEY      optional; enables tvdb→tmdb mapping and text search
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from . import dashboard, fakeqbt, torznab
from .jobs import JobManager
from .scraper import check_environment


def create_app() -> FastAPI:
    download_dir = Path(os.environ.get("WSG_DOWNLOAD_DIR", "/downloads"))
    download_dir.mkdir(parents=True, exist_ok=True)

    job_manager = JobManager(default_save_path=download_dir)
    fakeqbt.configure(job_manager)

    app = FastAPI(title="watchseries-grabber",
                  description="Torznab indexer + fake qBittorrent for watchseries.bar",
                  version="0.1.0")

    @app.get("/health")
    def health():
        return {"status": "ok", "missing": check_environment(),
                "download_dir": str(download_dir),
                "jobs": len(job_manager.list())}

    app.include_router(dashboard.router)  # mounts GET /
    app.include_router(torznab.router, prefix="/torznab")
    app.include_router(fakeqbt.router, prefix="/api/v2")
    return app


app = create_app()

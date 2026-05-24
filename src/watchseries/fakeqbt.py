"""qBittorrent v2 HTTP API — minimal subset that satisfies Sonarr's client.

Sonarr's QBittorrentProxy ultimately calls only ~10 endpoints to manage
torrents. We implement those, with magnets being decoded into JobManager jobs.
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Form, Request, Response

from .jobs import (
    Job,
    JobManager,
    STATE_DOWNLOADING,
    STATE_ERROR,
    STATE_FINISHED,
    STATE_QUEUED,
)

router = APIRouter()


def configure(job_manager: JobManager) -> None:
    """Inject the job manager — main.py calls this once at startup."""
    global _jobs
    _jobs = job_manager


_jobs: JobManager | None = None  # set by configure()


# ---- auth (no-op) ----

@router.post("/auth/login")
async def login() -> Response:
    # Sonarr just needs "Ok." in the body and an SID cookie set.
    r = Response(content="Ok.", media_type="text/plain")
    r.set_cookie("SID", "watchseries-fake-sid", path="/")
    return r


@router.post("/auth/logout")
def logout() -> Response:
    return Response(content="Ok.", media_type="text/plain")


# ---- app ----

@router.get("/app/version")
def app_version() -> Response:
    return Response(content="v4.6.0", media_type="text/plain")


@router.get("/app/webapiVersion")
def webapi_version() -> Response:
    return Response(content="2.9.3", media_type="text/plain")


@router.get("/app/buildInfo")
def build_info() -> dict:
    return {
        "qt": "5.15.2", "libtorrent": "1.2.19", "boost": "1.81.0",
        "openssl": "3.0.0", "bitness": 64,
    }


@router.get("/app/preferences")
def preferences() -> dict:
    assert _jobs is not None
    return {
        "save_path": str(_jobs.default_save_path),
        "temp_path_enabled": False,
        "temp_path": "",
        "create_subfolder_enabled": False,
        "max_ratio_enabled": False,
        "max_ratio": -1,
        "max_seeding_time_enabled": False,
        "max_seeding_time": -1,
        "dht": True, "pex": True, "lsd": True,
        "queueing_enabled": False,
        "listen_port": 6881,
        "web_ui_username": "watchseries",
    }


@router.post("/app/setPreferences")
def set_prefs() -> Response:
    return Response(status_code=200)


# ---- torrents ----

@router.post("/torrents/add")
async def torrents_add(request: Request) -> Response:
    """Sonarr posts form-data with urls=<magnet> + savepath + category."""
    assert _jobs is not None
    form = await _read_form(request)
    urls = form.get("urls", "")
    save_path = form.get("savepath") or None
    category = form.get("category", "")

    added = 0
    for line in urls.replace("\r", "").split("\n"):
        line = line.strip()
        if not line.startswith("magnet:?"):
            continue
        save = Path(save_path) if save_path else None
        job = _jobs.add_from_magnet(line, save, category)
        if job is not None:
            added += 1
    return Response(content="Ok." if added else "Fails.",
                    media_type="text/plain",
                    status_code=200)


@router.post("/torrents/delete")
async def torrents_delete(request: Request) -> Response:
    assert _jobs is not None
    form = await _read_form(request)
    hashes = form.get("hashes", "")
    delete_files = str(form.get("deleteFiles", "false")).lower() == "true"
    for h in hashes.split("|"):
        h = h.strip().lower()
        if h:
            _jobs.delete(h, delete_files=delete_files)
    return Response(status_code=200)


@router.get("/torrents/info")
def torrents_info(
    filter: str | None = None,
    category: str | None = None,
    hashes: str | None = None,
):
    assert _jobs is not None
    jobs = _jobs.list(category=category)
    if hashes:
        wanted = {h.strip().lower() for h in hashes.split("|") if h.strip()}
        jobs = [j for j in jobs if j.hash in wanted]
    if filter:
        f = filter.lower()
        if f == "completed":
            jobs = [j for j in jobs if j.state == STATE_FINISHED]
        elif f == "downloading":
            jobs = [j for j in jobs if j.state == STATE_DOWNLOADING]
    return [_job_to_dict(j) for j in jobs]


@router.get("/torrents/properties")
def torrents_properties(hash: str) -> dict:
    assert _jobs is not None
    job = _jobs.get(hash.lower())
    if not job:
        return {}
    return {
        "save_path": str(job.save_path),
        "creation_date": int(job.added_on),
        "piece_size": 16384, "comment": "",
        "total_wasted": 0, "total_uploaded": 0,
        "total_downloaded": job.size_done,
        "up_limit": -1, "dl_limit": -1,
        "time_elapsed": int(time.time() - job.added_on),
        "seeding_time": 0, "nb_connections": 0,
        "share_ratio": 0.0, "addition_date": int(job.added_on),
        "completion_date": int(time.time()) if job.state == STATE_FINISHED else -1,
        "created_by": "watchseries-grabber",
    }


@router.get("/torrents/files")
def torrents_files(hash: str):
    assert _jobs is not None
    job = _jobs.get(hash.lower())
    if not job:
        return []
    return [
        {
            "index": i, "name": str(f.relative_to(job.save_path)),
            "size": (f.stat().st_size if f.exists() else 0),
            "progress": 1.0, "priority": 1,
            "is_seed": False, "piece_range": [0, 0], "availability": 1.0,
        }
        for i, f in enumerate(job.files)
    ]


@router.post("/torrents/setCategory")
async def set_category(request: Request) -> Response:
    return Response(status_code=200)


@router.post("/torrents/createCategory")
async def create_category(request: Request) -> Response:
    return Response(status_code=200)


@router.post("/torrents/setLocation")
async def set_location(request: Request) -> Response:
    return Response(status_code=200)


@router.post("/torrents/pause")
@router.post("/torrents/resume")
@router.post("/torrents/setShareLimits")
@router.post("/torrents/setForceStart")
@router.post("/torrents/setSuperSeeding")
@router.post("/torrents/topPrio")
@router.post("/torrents/bottomPrio")
async def torrents_noop() -> Response:
    return Response(status_code=200)


@router.get("/torrents/categories")
def torrents_categories() -> dict:
    return {}


# ---- helpers ----

async def _read_form(request: Request) -> dict[str, str]:
    """Accept either form-urlencoded or multipart/form-data."""
    ctype = request.headers.get("content-type", "")
    out: dict[str, str] = {}
    if "multipart/form-data" in ctype or "application/x-www-form-urlencoded" in ctype:
        form = await request.form()
        for k, v in form.multi_items():
            # Multi-value: keep last (qBittorrent semantics)
            out[k] = str(v) if not isinstance(v, str) else v
    else:
        body = (await request.body()).decode("utf-8", errors="replace")
        import urllib.parse
        for k, vs in urllib.parse.parse_qs(body).items():
            out[k] = vs[-1] if vs else ""
    return out


def _job_to_dict(job: Job) -> dict:
    return {
        "hash": job.hash,
        "name": job.name,
        "size": job.size_total or 0,
        "progress": job.progress,
        "dlspeed": 0, "upspeed": 0,
        "priority": 0, "num_seeds": 0, "num_complete": 0,
        "num_leechs": 0, "num_incomplete": 0,
        "ratio": 0.0,
        "eta": -1,
        "state": job.state,
        "seq_dl": False, "f_l_piece_prio": False,
        "category": job.category,
        "tags": "",
        "super_seeding": False,
        "force_start": False,
        "save_path": str(job.save_path),
        "content_path": str(job.content_path),
        "added_on": int(job.added_on),
        "completion_on": int(time.time()) if job.state == STATE_FINISHED else -1,
        "tracker": "", "dl_limit": -1, "up_limit": -1,
        "downloaded": job.size_done, "uploaded": 0,
        "downloaded_session": job.size_done, "uploaded_session": 0,
        "amount_left": max(0, job.size_total - job.size_done),
        "completed": job.size_done if job.state == STATE_FINISHED else 0,
        "time_active": int(time.time() - job.added_on),
        "auto_tmm": False, "total_size": job.size_total or 0,
        "max_ratio": -1, "max_seeding_time": -1,
        "seeding_time": 0, "seen_complete": -1,
        # Non-standard fields the dashboard uses.
        "error_message": job.error,
        "current_unit_label": job.current_unit_label,
        "current_unit_progress": job.current_unit_progress,
        "expected_units": job.expected_units,
        "completed_units": len(job.files),
        "eta_seconds": _eta_seconds(job),
    }


def _eta_seconds(job) -> int:
    """Estimate seconds until the whole job finishes, based on the rate
    of the current file. Returns -1 if not enough data."""
    from .jobs import STATE_DOWNLOADING
    if job.state != STATE_DOWNLOADING:
        return -1
    elapsed = time.time() - (job.current_unit_started_at or job.added_on)
    if elapsed < 5 or job.current_unit_progress <= 0:
        return -1
    secs_per_unit = elapsed / max(job.current_unit_progress, 0.001)
    units_left_after_current = max(0, job.expected_units - len(job.files) - 1)
    seconds_left_on_current = secs_per_unit * (1 - job.current_unit_progress)
    return int(seconds_left_on_current + units_left_after_current * secs_per_unit)

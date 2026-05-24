"""Single-page HTML dashboard.

Served at `/`. Polls `/api/v2/torrents/info` every few seconds and renders
each job as a card with a progress bar.
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import fakeqbt

router = APIRouter()


@router.post("/jobs/{job_hash}/retry", include_in_schema=False)
def retry_job(job_hash: str) -> dict:
    jm = fakeqbt._jobs
    if jm is None:
        raise HTTPException(status_code=503, detail="job manager not configured")
    if not jm.retry(job_hash.lower()):
        raise HTTPException(status_code=404, detail="no such job")
    return {"ok": True}


@router.get("/disk")
def disk_listing(path: str = "") -> list[dict]:
    """List immediate children of <download_dir>/<path>. Each entry has:
        type: "dir" | "file"
        name: basename
        path: download-dir-relative path (used for further drilling)
        size: total bytes (dir totals are recursive)
        files: file count (1 for files, recursive count for dirs)
        partials: # of .part files inside (0 for non-dirs)
        tracked: bool — only meaningful at top level
        ours: bool — only meaningful at top level (name ends with .watchseries)

    Top-level entries are sorted ours-first, then alphabetical; nested
    entries are sorted dirs-first, then alphabetical."""
    import os
    from pathlib import Path
    jm = fakeqbt._jobs
    if jm is None:
        raise HTTPException(status_code=503, detail="job manager not configured")
    root = jm.default_save_path.resolve()
    target = (root / path).resolve() if path else root
    # No traversal outside the download dir.
    if root != target and root not in target.parents:
        raise HTTPException(status_code=400, detail="path outside download dir")
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="no such directory")

    is_top = (target == root)
    tracked = {Path(j.content_path).name for j in jm.list()} if is_top else set()

    out: list[dict] = []
    for entry in sorted(target.iterdir()):
        if entry.name.startswith(".") and is_top:
            continue
        rel = str(entry.relative_to(root))
        if entry.is_dir():
            total = 0
            files = 0
            partials = 0
            for dp, _, fns in os.walk(entry):
                for fn in fns:
                    fp = Path(dp) / fn
                    try:
                        total += fp.stat().st_size
                    except OSError:
                        continue
                    files += 1
                    if fn.endswith(".part"):
                        partials += 1
            out.append({
                "type": "dir",
                "name": entry.name,
                "path": rel,
                "size": total,
                "files": files,
                "partials": partials,
                "tracked": entry.name in tracked,
                "ours": entry.name.endswith(".watchseries") if is_top else False,
            })
        else:
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            out.append({
                "type": "file",
                "name": entry.name,
                "path": rel,
                "size": size,
                "files": 1,
                "partials": 1 if entry.name.endswith(".part") else 0,
                "tracked": False,
                "ours": False,
            })

    if is_top:
        out.sort(key=lambda r: (not r["ours"], r["name"].lower()))
    else:
        out.sort(key=lambda r: (r["type"] != "dir", r["name"].lower()))
    return out


class DiskDelete(BaseModel):
    name: str


@router.post("/disk/delete")
def disk_delete(body: DiskDelete) -> dict:
    """Delete a directory under /downloads. Refuses if the directory is
    currently tracked by an active job (delete the job first)."""
    import shutil
    jm = fakeqbt._jobs
    if jm is None:
        raise HTTPException(status_code=503, detail="job manager not configured")
    root = jm.default_save_path
    # Make sure the target is a direct child of root — no traversal.
    target = (root / body.name).resolve()
    if target.parent != root.resolve() or not target.exists():
        raise HTTPException(status_code=404, detail="no such directory")
    if body.name in {j.content_path.name for j in jm.list()}:
        raise HTTPException(status_code=409, detail="directory is owned by an active job; delete the job first")
    shutil.rmtree(target, ignore_errors=True)
    return {"ok": True}


@router.get("/settings")
def get_settings() -> dict:
    jm = fakeqbt._jobs
    if jm is None:
        raise HTTPException(status_code=503, detail="job manager not configured")
    return {
        "max_parallel": jm.max_parallel,
        "active_jobs": len(jm._active),
    }


class SettingsUpdate(BaseModel):
    max_parallel: int | None = None


@router.put("/settings")
def put_settings(body: SettingsUpdate) -> dict:
    jm = fakeqbt._jobs
    if jm is None:
        raise HTTPException(status_code=503, detail="job manager not configured")
    if body.max_parallel is not None:
        jm.set_max_parallel(body.max_parallel)
    return get_settings()

_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>watchseries-grabber</title>
  <style>
    :root {
      --bg: #0f1419;
      --panel: #1a2129;
      --panel-2: #232d38;
      --text: #e6edf3;
      --muted: #8b949e;
      --accent: #58a6ff;
      --ok: #3fb950;
      --warn: #d29922;
      --err: #f85149;
      --border: #30363d;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      background: var(--bg); color: var(--text); min-height: 100vh;
    }
    header {
      padding: 20px 28px; border-bottom: 1px solid var(--border);
      display: flex; align-items: center; justify-content: space-between;
      background: var(--panel);
    }
    h1 { margin: 0; font-size: 1.4rem; letter-spacing: -0.01em; }
    .meta { color: var(--muted); font-size: 0.9rem; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    main { padding: 24px 28px 60px; max-width: 1100px; margin: 0 auto; }
    .empty {
      padding: 60px 24px; text-align: center; color: var(--muted);
      border: 1px dashed var(--border); border-radius: 12px;
    }
    .job {
      background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
      padding: 18px 20px; margin-bottom: 14px;
    }
    .job-head {
      display: flex; align-items: baseline; justify-content: space-between;
      gap: 16px; margin-bottom: 10px;
    }
    .job-name {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.95rem; word-break: break-all;
    }
    .state { font-size: 0.8rem; padding: 3px 10px; border-radius: 999px;
      background: var(--panel-2); color: var(--muted);
      white-space: nowrap; flex-shrink: 0;
    }
    .state.downloading { background: rgba(88,166,255,0.15); color: var(--accent); }
    .state.pausedUP { background: rgba(63,185,80,0.15); color: var(--ok); }
    .state.error { background: rgba(248,81,73,0.15); color: var(--err); }
    .state.queuedDL { background: rgba(210,153,34,0.15); color: var(--warn); }
    .actions { display: flex; gap: 8px; align-items: center; }
    button.btn {
      background: var(--panel-2); color: var(--text);
      border: 1px solid var(--border); border-radius: 6px;
      padding: 4px 12px; font-size: 0.8rem; cursor: pointer;
      font-family: inherit;
    }
    button.btn:hover { background: #2d3946; border-color: var(--accent); }
    button.btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .settings {
      display: flex; align-items: center; gap: 14px;
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 12px; padding: 14px 18px; margin-bottom: 18px;
      font-size: 0.9rem;
    }
    .settings label { color: var(--muted); }
    .settings input[type="number"] {
      background: var(--panel-2); color: var(--text);
      border: 1px solid var(--border); border-radius: 6px;
      padding: 4px 8px; width: 60px; font-family: inherit;
    }
    .settings .indicator { color: var(--muted); margin-left: auto; font-size: 0.82rem; }
    .section-h {
      margin: 32px 0 12px; display: flex; align-items: baseline;
      justify-content: space-between;
    }
    .section-h h2 { margin: 0; font-size: 1.05rem; font-weight: 600; }
    .section-h .muted { color: var(--muted); font-size: 0.85rem; }
    .disk-row {
      display: grid; grid-template-columns: 1fr 80px 100px 110px 80px;
      gap: 12px; align-items: center;
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 8px; padding: 10px 14px; margin-bottom: 6px;
      font-size: 0.88rem;
    }
    .disk-row .disk-name { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }
    .disk-row .disk-meta { color: var(--muted); font-size: 0.82rem; }
    .disk-row .badge {
      display: inline-block; padding: 2px 8px; border-radius: 999px;
      font-size: 0.72rem; text-align: center;
    }
    .disk-row .badge.tracked { background: rgba(88,166,255,0.15); color: var(--accent); }
    .disk-row .badge.orphan { background: rgba(210,153,34,0.15); color: var(--warn); }
    .disk-row.foreign { opacity: 0.55; }
    .disk-row.foreign .badge { background: var(--panel-2); color: var(--muted); }
    .disk-divider { margin: 10px 0 6px; padding-left: 4px; font-size: 0.78rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
    .disk-row.is-dir { cursor: pointer; }
    .disk-row .chev { display: inline-block; width: 14px; color: var(--muted); transition: transform 0.15s; }
    .disk-row.open .chev { transform: rotate(90deg); }
    .disk-row.is-file { cursor: default; }
    .disk-children { margin-left: 22px; margin-bottom: 6px; }
    .disk-children .disk-row { padding: 6px 12px; font-size: 0.84rem; }
    .bar {
      position: relative; height: 8px; background: var(--panel-2);
      border-radius: 999px; overflow: hidden; margin: 8px 0 12px;
    }
    .bar-fill {
      position: absolute; left: 0; top: 0; bottom: 0;
      background: linear-gradient(90deg, var(--accent), #79c0ff);
      transition: width 0.5s ease;
    }
    .bar-fill.ok { background: var(--ok); }
    .bar-fill.err { background: var(--err); }
    .bar.small { height: 5px; margin-bottom: 8px; }
    .sub {
      display: flex; justify-content: space-between; align-items: baseline;
      font-size: 0.82rem; color: var(--muted); margin: 6px 0 2px;
    }
    .sub strong { color: var(--text); font-weight: 500; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    .detail {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px 24px; font-size: 0.85rem; color: var(--muted);
    }
    .detail strong { color: var(--text); font-weight: 500; margin-right: 6px; }
    .detail code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.85em; }
    .files {
      margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.82rem;
    }
    .files summary { color: var(--muted); cursor: pointer; user-select: none; }
    .files ul { margin: 8px 0 0; padding: 0; list-style: none; }
    .files li {
      display: grid; grid-template-columns: 1fr 60px 80px; gap: 10px;
      color: var(--text); padding: 3px 0;
    }
    .files .file-pct { text-align: right; color: var(--accent); }
    .files .file-pct.done { color: var(--ok); }
    .files .file-size { text-align: right; color: var(--muted); }
    .err-msg {
      color: var(--err); font-size: 0.85rem; margin-top: 8px;
      padding: 8px 12px; background: rgba(248,81,73,0.08);
      border-left: 3px solid var(--err); border-radius: 4px;
    }
    .footer {
      margin-top: 32px; padding: 16px; text-align: center;
      color: var(--muted); font-size: 0.8rem;
    }
    .footer a { color: var(--accent); text-decoration: none; }
    .footer a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <header>
    <h1>watchseries-grabber</h1>
    <div class="meta" id="meta">loading…</div>
  </header>
  <main>
    <div class="settings">
      <label for="max-parallel">max concurrent downloads</label>
      <input type="number" id="max-parallel" min="1" max="20" step="1"/>
      <button class="btn" id="save-settings">save</button>
      <span class="indicator" id="settings-indicator"></span>
    </div>
    <div id="jobs">
      <div class="empty">no jobs yet</div>
    </div>

    <div class="section-h">
      <h2>downloads on disk</h2>
      <span class="muted" id="disk-summary"></span>
    </div>
    <div id="disk"></div>

    <div class="footer">
      jobs poll every 3s · disk every 15s · <a href="/torznab/api?t=caps" target="_blank">torznab caps</a> · <a href="/health" target="_blank">health</a>
    </div>
  </main>

<script>
// Two cadences: jobs/health/settings poll every 3s for live progress;
// disk top-level polls every 15s since sizes change slowly and any
// already-expanded subtree is left alone unless the user re-clicks.
const POLL_MS_JOBS = 3000;
const POLL_MS_DISK = 15000;

function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B","KB","MB","GB","TB"];
  let i = 0; while (n >= 1024 && i < u.length-1) { n /= 1024; i++; }
  return n.toFixed(n >= 100 || i === 0 ? 0 : 1) + " " + u[i];
}
function fmtAge(ts) {
  const s = Math.floor(Date.now()/1000 - ts);
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s/60) + "m ago";
  if (s < 86400) return Math.floor(s/3600) + "h ago";
  return Math.floor(s/86400) + "d ago";
}
function fmtETA(s) {
  if (!s || s < 0) return "—";
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s/60) + "m " + (s % 60) + "s";
  const h = Math.floor(s/3600); const m = Math.floor((s % 3600) / 60);
  return h + "h " + m + "m";
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function tickJobs() {
  try {
    const [jobs, health] = await Promise.all([
      fetch("/api/v2/torrents/info").then(r => r.json()),
      fetch("/health").then(r => r.json()),
    ]);
    document.getElementById("meta").textContent =
      jobs.length + " job" + (jobs.length === 1 ? "" : "s") +
      " · download dir: " + health.download_dir +
      (health.missing && health.missing.length ? " · MISSING: " + health.missing.join(",") : "");
    await applyJobs(jobs);
  } catch (e) {
    document.getElementById("meta").textContent = "service unreachable";
  }
}

async function tickDisk() {
  try {
    const disk = await fetch("/disk").then(r => r.json());
    applyDisk(disk);
  } catch (e) { /* ignore */ }
}

// Paths that the user has expanded. Source of truth for which subtrees
// should be visible after a re-render.
const expandedPaths = new Set();

// ============================================================
// Disk section — top-level in-place update
// ============================================================
//
// We render the top-level rows once and update only the size/file-count/
// badge/delete-button fields each poll. Expanded subtrees (`.disk-children`)
// are never touched by the poll; they're rebuilt only when the user clicks
// the row to expand (or to manually refresh by collapse+re-expand).

function applyDisk(rows) {
  const root = document.getElementById("disk");
  const summary = document.getElementById("disk-summary");
  if (!rows.length) {
    root.innerHTML = '<div class="muted">empty</div>';
    summary.textContent = "";
    return;
  }
  const totalBytes = rows.reduce((s, r) => s + (r.size || 0), 0);
  const orphans = rows.filter(r => r.type === "dir" && !r.tracked).length;
  summary.textContent = `${rows.length} dir${rows.length === 1 ? "" : "s"} · ${fmtBytes(totalBytes)} total · ${orphans} orphan${orphans === 1 ? "" : "s"}`;

  // Build a desired-order list with dividers interleaved.
  const ours = rows.filter(r => r.ours);
  const others = rows.filter(r => !r.ours);
  const ordered = [];
  if (ours.length) ordered.push({divider: "from this service"});
  ordered.push(...ours);
  if (others.length) ordered.push({divider: "other downloads (shared dir)"});
  ordered.push(...others);

  // Map path -> existing row+childbox pair so we can reuse them.
  const existing = new Map();
  root.querySelectorAll(".disk-row[data-path]").forEach(el => {
    existing.set(el.dataset.path, {row: el, children: el.nextElementSibling});
  });

  // Build / update in order, appending or inserting as needed.
  const frag = document.createDocumentFragment();
  for (const item of ordered) {
    if (item.divider) {
      const d = document.createElement("div");
      d.className = "disk-divider";
      d.textContent = item.divider;
      frag.appendChild(d);
      continue;
    }
    let pair = existing.get(item.path);
    if (pair) {
      updateDiskRow(pair.row, item, /*topLevel*/true);
      // Reuse the existing children element to preserve its current state.
      frag.appendChild(pair.row);
      frag.appendChild(pair.children);
      existing.delete(item.path);
    } else {
      const {row, children} = buildDiskRow(item, /*topLevel*/true);
      frag.appendChild(row);
      frag.appendChild(children);
    }
  }
  // Anything left in `existing` no longer present on disk — drop.
  for (const {row, children} of existing.values()) {
    row.remove();
    if (children && children.matches(".disk-children")) children.remove();
  }
  // Clear and append fragment. (Removing only the divider <div>s before
  // appending is messier than just replacing root's children with the
  // rebuilt fragment, which preserves the per-row DOM nodes we just
  // moved into the fragment.)
  while (root.firstChild) root.firstChild.remove();
  root.appendChild(frag);
}

function buildDiskRow(r, topLevel) {
  const row = document.createElement("div");
  row.dataset.path = r.path;
  row.dataset.isDir = r.type === "dir";
  const children = document.createElement("div");
  children.className = "disk-children";
  children.dataset.childrenOf = r.path;
  children.style.display = "none";
  // Inner structure — set once, then updateDiskRow only touches text.
  row.innerHTML = `
    <div>
      <div class="disk-name"><span class="chev"></span> <span class="name-text"></span></div>
      <div class="disk-meta meta-text"></div>
    </div>
    <div class="disk-meta size-text" style="text-align:right"></div>
    <div style="text-align:center" class="badge-cell"></div>
    <div></div>
    <div style="text-align:right" class="action-cell"></div>`;
  updateDiskRow(row, r, topLevel);
  return {row, children};
}

function updateDiskRow(row, r, topLevel) {
  const isDir = r.type === "dir";
  row.className = `disk-row ${r.ours ? "" : (topLevel ? "foreign" : "")} ${isDir ? "is-dir" : "is-file"} ${expandedPaths.has(r.path) ? "open" : ""}`;
  row.querySelector(".chev").textContent = isDir ? "▶" : "·";
  row.querySelector(".name-text").textContent = r.name;
  const partials = r.partials > 0 ? ` · ${r.partials} .part` : "";
  row.querySelector(".meta-text").textContent =
    `${r.files} file${r.files === 1 ? "" : "s"}${partials}`;
  row.querySelector(".size-text").textContent = fmtBytes(r.size);

  const badge = row.querySelector(".badge-cell");
  if (topLevel) {
    if (r.tracked) badge.innerHTML = '<span class="badge tracked">tracked</span>';
    else if (isDir) badge.innerHTML = '<span class="badge orphan">orphan</span>';
    else badge.innerHTML = "";
  } else badge.innerHTML = "";

  const action = row.querySelector(".action-cell");
  if (topLevel && !r.tracked && isDir) {
    if (!action.querySelector("button[data-disk-delete]")) {
      action.innerHTML = `<button class="btn" data-disk-delete="${esc(r.name)}">delete</button>`;
    }
  } else {
    action.innerHTML = "";
  }
}

function renderDiskChildrenList(items) {
  // For nested levels we still use innerHTML — they're small, and only
  // get rebuilt when the user clicks (not on every poll).
  return items.map(c => {
    const isDir = c.type === "dir";
    const partials = c.partials > 0 ? ` · ${c.partials} .part` : "";
    const chev = isDir ? "▶" : "·";
    return `
      <div class="disk-row ${isDir ? "is-dir" : "is-file"}"
           data-path="${esc(c.path)}" data-is-dir="${isDir}">
        <div>
          <div class="disk-name"><span class="chev">${chev}</span> ${esc(c.name)}</div>
          <div class="disk-meta">${c.files} file${c.files === 1 ? "" : "s"}${partials}</div>
        </div>
        <div class="disk-meta" style="text-align:right">${fmtBytes(c.size)}</div>
        <div></div>
        <div></div>
        <div></div>
      </div>
      <div class="disk-children" data-children-of="${esc(c.path)}" style="display:none"></div>`;
  }).join("");
}

async function loadChildren(path, row, silent) {
  const childBox = row.nextElementSibling;
  if (!childBox || !childBox.matches(".disk-children")) return;
  if (!silent) {
    if (row.classList.contains("open")) {
      row.classList.remove("open");
      childBox.style.display = "none";
      expandedPaths.delete(path);
      return;
    }
    row.classList.add("open");
    expandedPaths.add(path);
  } else {
    row.classList.add("open");
  }
  try {
    const data = await fetch("/disk?path=" + encodeURIComponent(path)).then(r => r.json());
    childBox.innerHTML = renderDiskChildrenList(data);
    childBox.style.display = "block";
    // Re-expand any descendants the user previously had open.
    for (const p of [...expandedPaths]) {
      if (p === path || !p.startsWith(path + "/")) continue;
      const sub = childBox.querySelector(`.disk-row[data-path="${cssEsc(p)}"]`);
      if (sub) loadChildren(p, sub, true);
    }
  } catch (e) {
    childBox.innerHTML = '<div class="muted" style="padding:8px">failed to load</div>';
    childBox.style.display = "block";
  }
}

function cssEsc(s) { return s.replace(/(["\\])/g, "\\$1"); }

// ============================================================
// Jobs section — in-place update
// ============================================================
//
// Each job has a stable .job element keyed by data-hash. On each tick we
// reorder (added_on desc), append new, remove gone, and update text/widths
// in existing rows. The <details> file list is rebuilt only when its
// member set changes.

async function applyJobs(jobs) {
  const root = document.getElementById("jobs");
  // Remove the empty-state placeholder if present.
  const placeholder = root.querySelector(".empty");
  if (placeholder) placeholder.remove();

  if (!jobs.length) {
    while (root.firstChild) root.firstChild.remove();
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "no jobs yet · trigger a grab in Sonarr to see one appear";
    root.appendChild(empty);
    return;
  }

  jobs.sort((a, b) => b.added_on - a.added_on);
  // Pull file lists in parallel (cheap; only used to populate the panel).
  const fileLists = await Promise.all(jobs.map(j =>
    fetch("/api/v2/torrents/files?hash=" + encodeURIComponent(j.hash))
      .then(r => r.json()).catch(() => [])
  ));

  // Index existing rows by hash.
  const existing = new Map();
  root.querySelectorAll(".job[data-hash]").forEach(el => existing.set(el.dataset.hash, el));

  // Build/reuse rows in the new order.
  const wanted = new Set();
  for (let i = 0; i < jobs.length; i++) {
    const j = jobs[i];
    wanted.add(j.hash);
    let el = existing.get(j.hash);
    if (!el) {
      el = buildJobRow(j);
      root.appendChild(el);
    } else if (el.nextSibling !== null && root.children[i] !== el) {
      // Re-order: move to position i.
      const at = root.children[i];
      if (at !== el) root.insertBefore(el, at);
    }
    updateJobRow(el, j, fileLists[i] || []);
  }

  // Drop jobs that no longer exist.
  for (const [hash, el] of existing.entries()) {
    if (!wanted.has(hash)) el.remove();
  }
}

function buildJobRow(j) {
  const el = document.createElement("div");
  el.className = "job";
  el.dataset.hash = j.hash;
  el.innerHTML = `
    <div class="job-head">
      <div class="job-name"></div>
      <div class="actions">
        <button class="btn js-retry" style="display:none">retry</button>
        <span class="state"></span>
      </div>
    </div>
    <div class="bar"><div class="bar-fill"></div></div>
    <div class="sub" style="display:none">
      <span>now: <strong class="js-cur"></strong> <span class="js-cur-meta"></span></span>
      <span class="js-cur-pct"></span>
    </div>
    <div class="bar small js-cur-bar" style="display:none"><div class="bar-fill"></div></div>
    <div class="detail">
      <div><strong>eta</strong><span class="js-eta"></span></div>
      <div><strong>added</strong><span class="js-added"></span></div>
      <div><strong>downloaded</strong><span class="js-downloaded"></span></div>
      <div><strong>category</strong><code class="js-cat"></code></div>
      <div style="grid-column:1/-1"><strong>path</strong><code class="js-path"></code></div>
    </div>
    <div class="err-msg js-err" style="display:none"></div>
    <details class="files js-files" style="display:none">
      <summary class="js-files-summary"></summary>
      <ul class="js-files-list"></ul>
    </details>`;
  return el;
}

function updateJobRow(el, j, files) {
  el.querySelector(".job-name").textContent = j.name;

  const pct = (j.progress * 100).toFixed(1);
  const stateEl = el.querySelector(".state");
  stateEl.className = "state " + j.state;
  stateEl.textContent = `${j.state} · ${pct}%`;

  const bar = el.querySelector(".bar > .bar-fill");
  bar.className = "bar-fill " + (j.state === "pausedUP" ? "ok" : j.state === "error" ? "err" : "");
  bar.style.width = pct + "%";

  const completedUnits = j.completed_units ?? 0;
  const expectedUnits = j.expected_units ?? 1;
  const curLabel = j.current_unit_label || "";
  const curPct = ((j.current_unit_progress || 0) * 100).toFixed(1);
  const showSub = j.state === "downloading" && expectedUnits > 1 && curLabel;
  const sub = el.querySelector(".sub");
  const subBar = el.querySelector(".js-cur-bar");
  if (showSub) {
    sub.style.display = "";
    subBar.style.display = "";
    el.querySelector(".js-cur").textContent = curLabel;
    el.querySelector(".js-cur-meta").textContent = `(${completedUnits}/${expectedUnits} done)`;
    el.querySelector(".js-cur-pct").textContent = `${curPct}%`;
    subBar.querySelector(".bar-fill").style.width = curPct + "%";
  } else {
    sub.style.display = "none";
    subBar.style.display = "none";
  }

  el.querySelector(".js-eta").textContent = fmtETA(j.eta_seconds);
  el.querySelector(".js-added").textContent = fmtAge(j.added_on);
  el.querySelector(".js-downloaded").textContent = fmtBytes(j.downloaded);
  el.querySelector(".js-cat").textContent = j.category || "—";
  el.querySelector(".js-path").textContent = j.content_path;

  const errEl = el.querySelector(".js-err");
  if (j.state === "error" && j.error_message) {
    errEl.textContent = j.error_message;
    errEl.style.display = "";
  } else {
    errEl.style.display = "none";
  }

  const retryBtn = el.querySelector(".js-retry");
  const canRetry = j.state === "error" || j.state === "pausedUP";
  retryBtn.style.display = canRetry ? "" : "none";
  retryBtn.dataset.hash = j.hash;
  if (!retryBtn.disabled) retryBtn.textContent = "retry";

  // Files panel: rebuild <ul> only if file set or in-progress label changed.
  const filesEl = el.querySelector(".js-files");
  const inProgress = j.state === "downloading" && curLabel ? curLabel : "";
  const wantKey = files.map(f => f.name + ":" + f.size).join("|") + "##" + inProgress + ":" + curPct;
  if (filesEl.dataset.key !== wantKey) {
    filesEl.dataset.key = wantKey;
    const rows = files.map(f =>
      `<li><span class="file-name">${esc(f.name)}</span><span class="file-pct done">100%</span><span class="file-size">${fmtBytes(f.size)}</span></li>`
    );
    if (inProgress) {
      rows.push(`<li><span class="file-name">${esc(inProgress)} <em style="color:var(--muted);font-style:normal">· downloading</em></span><span class="file-pct">${curPct}%</span><span class="file-size">—</span></li>`);
    }
    el.querySelector(".js-files-list").innerHTML = rows.join("");
    const pending = expectedUnits - files.length - (inProgress ? 1 : 0);
    el.querySelector(".js-files-summary").textContent =
      `${files.length} done${inProgress ? ` · 1 in progress` : ""}${pending > 0 ? ` · ${pending} pending` : ""}`;
  }
  if (files.length || inProgress) filesEl.style.display = "";
  else filesEl.style.display = "none";
}

async function loadSettings() {
  try {
    const s = await fetch("/settings").then(r => r.json());
    const inp = document.getElementById("max-parallel");
    if (document.activeElement !== inp) inp.value = s.max_parallel;
    document.getElementById("settings-indicator").textContent =
      `${s.active_jobs} of ${s.max_parallel} slots in use`;
  } catch (e) { /* ignore */ }
}

document.getElementById("save-settings").addEventListener("click", async () => {
  const v = parseInt(document.getElementById("max-parallel").value, 10);
  if (!Number.isFinite(v)) return;
  const ind = document.getElementById("settings-indicator");
  ind.textContent = "saving…";
  try {
    await fetch("/settings", {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({max_parallel: v}),
    });
    await loadSettings();
    tick();
  } catch (e) {
    ind.textContent = "save failed";
  }
});

document.addEventListener("click", async (e) => {
  // Disk row toggle (anywhere on a dir row except buttons).
  const diskRow = e.target.closest(".disk-row.is-dir");
  if (diskRow && !e.target.closest("button")) {
    const path = diskRow.dataset.path;
    if (path) await loadChildren(path, diskRow, false);
    return;
  }
  const delBtn = e.target.closest("button[data-disk-delete]");
  if (delBtn) {
    const name = delBtn.dataset.diskDelete;
    if (!confirm("Permanently delete " + name + " from /downloads?")) return;
    delBtn.disabled = true; delBtn.textContent = "…";
    try {
      const r = await fetch("/disk/delete", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name}),
      });
      if (!r.ok) throw new Error(await r.text());
      tickDisk();
    } catch (err) {
      delBtn.textContent = "failed";
      setTimeout(() => { delBtn.disabled = false; delBtn.textContent = "delete"; }, 2000);
    }
    return;
  }
  const retryBtn = e.target.closest(".js-retry");
  if (retryBtn) {
    const hash = retryBtn.dataset.hash;
    if (!hash) return;
    retryBtn.disabled = true; retryBtn.textContent = "retrying…";
    try {
      const r = await fetch("/jobs/" + encodeURIComponent(hash) + "/retry", { method: "POST" });
      if (!r.ok) throw new Error("retry failed: " + r.status);
      retryBtn.disabled = false;
      tickJobs();
    } catch (err) {
      retryBtn.textContent = "failed";
      setTimeout(() => { retryBtn.disabled = false; retryBtn.textContent = "retry"; }, 2000);
    }
  }
});

loadSettings();
tickJobs();
tickDisk();
setInterval(() => { loadSettings(); tickJobs(); }, POLL_MS_JOBS);
setInterval(tickDisk, POLL_MS_DISK);
</script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    return HTMLResponse(_HTML)

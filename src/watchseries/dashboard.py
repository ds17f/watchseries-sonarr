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
    <div class="footer">
      polls every 3s · <a href="/torznab/api?t=caps" target="_blank">torznab caps</a> · <a href="/health" target="_blank">health</a>
    </div>
  </main>

<script>
const POLL_MS = 3000;

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

async function tick() {
  snapshotOpen();
  try {
    const [jobs, health] = await Promise.all([
      fetch("/api/v2/torrents/info").then(r => r.json()),
      fetch("/health").then(r => r.json()),
    ]);
    document.getElementById("meta").textContent =
      jobs.length + " job" + (jobs.length === 1 ? "" : "s") +
      " · download dir: " + health.download_dir +
      (health.missing && health.missing.length ? " · MISSING: " + health.missing.join(",") : "");
    render(jobs);
  } catch (e) {
    document.getElementById("meta").textContent = "service unreachable";
  }
}

// Hashes whose <details> file list the user has manually opened. Snapshot
// from the live DOM right before each re-render so polling doesn't snap
// them shut. The toggle event doesn't bubble and details elements get
// destroyed by innerHTML before any close event would fire, so we read
// the open state directly from the DOM instead.
const openHashes = new Set();
function snapshotOpen() {
  openHashes.clear();
  document.querySelectorAll("details.files[open]").forEach(d => {
    if (d.dataset.hash) openHashes.add(d.dataset.hash);
  });
}

async function render(jobs) {
  const root = document.getElementById("jobs");
  if (!jobs.length) {
    root.innerHTML = '<div class="empty">no jobs yet · trigger a grab in Sonarr to see one appear</div>';
    return;
  }
  jobs.sort((a, b) => b.added_on - a.added_on);
  // Fetch file lists per job in parallel.
  const fileLists = await Promise.all(jobs.map(j =>
    fetch("/api/v2/torrents/files?hash=" + encodeURIComponent(j.hash))
      .then(r => r.json()).catch(() => [])
  ));
  root.innerHTML = jobs.map((j, i) => {
    const pct = (j.progress * 100).toFixed(1);
    const fillClass = j.state === "pausedUP" ? "ok" : j.state === "error" ? "err" : "";
    const errBlock = (j.state === "error" && j.error_message)
      ? `<div class="err-msg">${esc(j.error_message)}</div>` : "";
    const files = fileLists[i] || [];
    const inProgress = j.state === "downloading" && curLabel ? curLabel : null;
    const rows = files.map(f =>
      `<li><span class="file-name">${esc(f.name)}</span><span class="file-pct done">100%</span><span class="file-size">${fmtBytes(f.size)}</span></li>`
    );
    if (inProgress) {
      rows.push(`<li><span class="file-name">${esc(inProgress)} <em style="color:var(--muted);font-style:normal">· downloading</em></span><span class="file-pct">${curPct}%</span><span class="file-size">—</span></li>`);
    }
    const fileBlock = rows.length ? `
      <details class="files" data-hash="${esc(j.hash)}">
        <summary>${files.length} done${inProgress ? ` · 1 in progress` : ""}${expectedUnits > files.length + (inProgress ? 1 : 0) ? ` · ${expectedUnits - files.length - (inProgress ? 1 : 0)} pending` : ""}</summary>
        <ul>${rows.join("")}</ul>
      </details>` : "";
    const completedUnits = j.completed_units ?? 0;
    const expectedUnits = j.expected_units ?? 1;
    const curLabel = j.current_unit_label || "";
    const curPct = ((j.current_unit_progress || 0) * 100).toFixed(1);
    const showSub = j.state === "downloading" && expectedUnits > 1 && curLabel;
    const subBlock = showSub ? `
        <div class="sub"><span>now: <strong>${esc(curLabel)}</strong> &nbsp;(${completedUnits}/${expectedUnits} done)</span><span>${curPct}%</span></div>
        <div class="bar small"><div class="bar-fill" style="width:${curPct}%"></div></div>
      ` : "";
    const eta = j.eta_seconds;
    const canRetry = j.state === "error" || j.state === "pausedUP";
    const retryBtn = canRetry
      ? `<button class="btn" data-action="retry" data-hash="${esc(j.hash)}">retry</button>`
      : "";
    return `
      <div class="job">
        <div class="job-head">
          <div class="job-name">${esc(j.name)}</div>
          <div class="actions">
            ${retryBtn}
            <span class="state ${esc(j.state)}">${esc(j.state)} · ${pct}%</span>
          </div>
        </div>
        <div class="bar"><div class="bar-fill ${fillClass}" style="width:${pct}%"></div></div>
        ${subBlock}
        <div class="detail">
          <div><strong>eta</strong>${fmtETA(eta)}</div>
          <div><strong>added</strong>${fmtAge(j.added_on)}</div>
          <div><strong>downloaded</strong>${fmtBytes(j.downloaded)}</div>
          <div><strong>category</strong><code>${esc(j.category || "—")}</code></div>
          <div style="grid-column:1/-1"><strong>path</strong><code>${esc(j.content_path)}</code></div>
        </div>
        ${errBlock}
        ${fileBlock}
      </div>`;
  }).join("");
  // Restore <details> open state after the innerHTML rebuild.
  root.querySelectorAll("details.files").forEach(d => {
    if (openHashes.has(d.dataset.hash)) d.open = true;
  });
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
  const btn = e.target.closest("button[data-action='retry']");
  if (!btn) return;
  const hash = btn.dataset.hash;
  btn.disabled = true; btn.textContent = "retrying…";
  try {
    const r = await fetch("/jobs/" + encodeURIComponent(hash) + "/retry", { method: "POST" });
    if (!r.ok) throw new Error("retry failed: " + r.status);
    tick();
  } catch (err) {
    btn.textContent = "failed";
    setTimeout(() => { btn.disabled = false; btn.textContent = "retry"; }, 2000);
  }
});

loadSettings();
tick();
setInterval(() => { loadSettings(); tick(); }, POLL_MS);
</script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    return HTMLResponse(_HTML)

"""downloader.py — Blueprint Flask: koleksi video dari URL via yt-dlp.

Fase status: queued → extracting → downloading → processing → done | error
"""
from __future__ import annotations
import json, os, re, shutil, sys, threading, time, traceback, uuid
from pathlib import Path
from flask import (Blueprint, abort, jsonify, render_template_string,
                   request, send_from_directory)

bp       = Blueprint("downloader", __name__)
BASE     = Path(__file__).parent.resolve()
LIBRARY  = Path(os.environ.get("CLIPPER_LIBRARY", BASE / "library")).expanduser()
LIBRARY.mkdir(parents=True, exist_ok=True)

JID   = re.compile(r"^[0-9a-f]{10}$")
JOBS: dict[str, dict] = {}
_SEM  = threading.Semaphore(int(os.environ.get("CLIPPER_DL_PARALLEL", "3")))

def _log(jid: str, *args) -> None:
    print(f"[dl:{jid}]", *args, file=sys.stderr, flush=True)

_UI = r"""<!doctype html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Downloader · Clipper</title>
<style>
:root{--bg:#0f1115;--card:#181c24;--bd:#2a3140;--fg:#e6edf3;--mut:#8b96a8;--acc:#4f9cf9;--ok:#3fb950;--err:#f85149;--warn:#d29922}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;align-items:center;gap:16px;padding:14px 20px;border-bottom:1px solid var(--bd);background:var(--card)}
header h1{margin:0;font-size:16px;font-weight:600}
header a{color:var(--mut);text-decoration:none;font-size:13px}header a:hover{color:var(--acc)}
.wrap{max-width:880px;margin:0 auto;padding:20px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:18px;margin-bottom:18px}
textarea{width:100%;min-height:100px;padding:10px;border-radius:8px;border:1px solid var(--bd);background:#0a0d12;color:var(--fg);resize:vertical;font:inherit}
textarea:focus,select:focus{outline:none;border-color:var(--acc)}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:12px}
select,button{padding:8px 14px;border-radius:8px;border:1px solid var(--bd);background:#0a0d12;color:var(--fg);cursor:pointer;font:inherit}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
button.primary:disabled{opacity:.5;cursor:wait}
button.mini{background:transparent;border:1px solid var(--bd);color:var(--mut);padding:4px 10px;font-size:12px}
button.mini:hover{color:var(--fg);border-color:var(--fg)}
button.danger{background:transparent;border:1px solid var(--err);color:var(--err);padding:4px 10px;font-size:12px}
button.warn{background:transparent;border:1px solid var(--warn);color:var(--warn);padding:4px 10px;font-size:12px}

.job{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px 14px;margin-bottom:8px}
.job .head{display:flex;gap:10px;align-items:center;margin-bottom:6px}
.job .title{flex:1;font-weight:600;margin:0;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.job .meta{color:var(--mut);font-size:12px;margin-bottom:6px;min-height:16px}
.job .actions{display:flex;gap:6px;align-items:center;margin-top:6px}

.bar{height:6px;background:#0a0d12;border-radius:3px;overflow:hidden;position:relative}
.bar>div{height:100%;background:var(--acc);transition:width .25s ease;border-radius:3px}
.bar.indet>div{width:35%;animation:slide 1.3s ease-in-out infinite}
@keyframes slide{0%{transform:translateX(-100%)}100%{transform:translateX(320%)}}
.bar.done>div{background:var(--ok)}

.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
.b-queued,.b-extracting,.b-processing{background:#3d2e00;color:var(--warn)}
.b-downloading{background:#1a3a5c;color:var(--acc)}
.b-done{background:#12341e;color:var(--ok)}
.b-error{background:#3d1418;color:var(--err)}
.empty{text-align:center;color:var(--mut);padding:40px}
a.act{color:var(--mut);text-decoration:none;font-size:12px;padding:4px 8px;border:1px solid var(--bd);border-radius:6px}
a.act:hover{color:var(--fg);border-color:var(--fg)}
</style></head><body>
<header><h1>📥 Downloader</h1><a href="/">← Kembali ke Clipper</a></header>
<div class="wrap">
  <div class="card">
    <label style="display:block;font-weight:600;margin-bottom:8px">URL Video (satu per baris)</label>
    <textarea id="urls" placeholder="https://www.youtube.com/watch?v=...&#10;https://www.tiktok.com/@user/video/...&#10;https://www.instagram.com/reel/..."></textarea>
    <div class="row">
      <select id="quality">
        <option value="bestvideo[height<=1080]+bestaudio/best[height<=1080]">1080p (maks)</option>
        <option value="bestvideo[height<=720]+bestaudio/best[height<=720]" selected>720p (maks)</option>
        <option value="bestvideo[height<=480]+bestaudio/best[height<=480]">480p (maks)</option>
        <option value="best">Terbaik</option>
        <option value="bestaudio/best">Audio saja</option>
      </select>
      <button id="go" class="primary">Mulai Download</button>
      <span id="msg" class="meta" style="margin-left:auto"></span>
    </div>
  </div>
  <div id="list"><div class="empty">Memuat…</div></div>
</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fs=b=>!b||b<1?'':b>1e9?(b/1e9).toFixed(2)+' GB':b>1e6?(b/1e6).toFixed(1)+' MB':((b/1e3)|0)+' KB';
const fd=s=>!s||s<0?'':Math.floor(s/60)+':'+String(s%60|0).padStart(2,'0');
const ACTIVE=['queued','extracting','downloading','processing'];
const badge=s=>`<span class="badge b-${s}">${s}</span>`;

$('go').onclick=async()=>{
  const urls=$('urls').value.split('
').map(s=>s.trim()).filter(Boolean);
  if(!urls.length)return;
  const format=$('quality').value;
  $('go').disabled=true;$('msg').textContent=`Mengirim ${urls.length} URL…`;
  let ok=0,fail=0;
  for(const url of urls){
    try{const r=await fetch('/api/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,format})});r.ok?ok++:fail++}catch(e){fail++}
  }
  $('urls').value='';$('msg').textContent=`✅ ${ok} antri${fail?` · ❌ ${fail} gagal`:''}`;
  $('go').disabled=false;refresh();
};

async function refresh(){
  try{
    const r=await fetch('/api/downloads');
    if(!r.ok) throw new Error('HTTP '+r.status);
    const {items}=await r.json();
    $('list').innerHTML=items.length?items.map(render).join(''):'<div class="empty">Belum ada download.</div>';
  }catch(e){
    $('list').innerHTML=`<div class="empty" style="color:var(--err)">Gagal muat: ${esc(e.message)}</div>`;
  }
}

function render(j){
  const st=j.status||'queued', pct=j.percent||0;
  const active=ACTIVE.includes(st);
  let meta;
  if(st==='done') meta=`${fs(j.size)} · ${fd(j.duration)}`;
  else if(st==='error') meta=`<span style="color:var(--err)">${esc((j.error||'').slice(0,160))}</span>`;
  else if(st==='extracting'||st==='queued') meta='Mengambil metadata…';
  else if(st==='processing') meta='Menggabungkan video+audio…';
  else meta=`${pct.toFixed(1)}% · ${fs(j.downloaded)} / ${fs(j.total)} · ${fs(j.speed)}/s${j.eta?' · ETA '+fd(j.eta):''}`;
  let bar='';
  if(st==='downloading') bar=`<div class="bar"><div style="width:${pct}%"></div></div>`;
  else if(st==='extracting'||st==='queued'||st==='processing') bar=`<div class="bar indet"><div></div></div>`;
  else if(st==='done') bar=`<div class="bar done"><div style="width:100%"></div></div>`;
  return `<div class="job">
    <div class="head">
      <p class="title" title="${esc(j.title||j.url||'')}">${esc(j.title||j.url||'(tanpa judul)')}</p>
      ${badge(st)}
    </div>
    <div class="meta">${meta}</div>
    ${bar}
    <div class="actions">
      ${j.url?`<a class="act" href="${esc(j.url)}" target="_blank" rel="noopener">🔗 sumber</a>`:''}
      ${j.file?`<a class="act" href="/library/${j.jid}/${encodeURIComponent(j.file)}" download>⬇ simpan</a>`:''}
      ${active?`<button class="warn" onclick="cancel('${j.jid}')">Batal</button>`:''}
      <button class="danger" style="margin-left:auto" onclick="del('${j.jid}')">Hapus</button>
    </div>
  </div>`;
}
async function del(jid){
  if(!confirm('Hapus file + job ini?'))return;
  await fetch('/api/download/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({jid})});
  refresh();
}
async function cancel(jid){
  await fetch('/api/download/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({jid})});
  refresh();
}
refresh();
setInterval(refresh,1000);
</script></body></html>"""


def _hook(jid: str, total_expected: list):
    """Progress hook: agregasi byte dari semua stream (video+audio).
    `total_expected[0]` = total byte semua stream (diisi saat extract_info).
    Progress hanya naik (monotonic) — tidak mundur saat ganti stream."""
    state = {"last_bytes": 0, "prev_stream_bytes": 0, "prev_total": 0}
    def hook(d):
        cur = JOBS.get(jid)
        if cur is None: return
        if cur.get("cancel"):
            raise RuntimeError("dibatalkan user")
        st = d.get("status")
        if st == "downloading":
            dl    = d.get("downloaded_bytes", 0) or 0
            # deteksi ganti stream: total stream berubah → simpan byte stream sebelumnya
            stream_total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if stream_total and state["prev_total"] and stream_total != state["prev_total"]:
                state["prev_stream_bytes"] += state["last_bytes"]
                state["last_bytes"] = 0
            if stream_total:
                state["prev_total"] = stream_total
            state["last_bytes"] = dl
            cumulative = state["prev_stream_bytes"] + dl
            overall = total_expected[0]
            if overall:
                pct = min(99.9, cumulative / overall * 100.0)
            else:
                # fallback: pakai total stream saat ini
                pct = (dl / stream_total * 100.0) if stream_total else 0.0
            # monotonic: jangan pernah turun
            pct = max(pct, cur.get("percent") or 0.0)
            cur.update(
                status="downloading",
                percent=pct,
                downloaded=cumulative,
                total=overall or stream_total,
                speed=d.get("speed") or 0, eta=d.get("eta") or 0,
            )
        elif st == "finished":
            # stream selesai — simpan byte & reset untuk stream berikutnya
            state["prev_stream_bytes"] += state["last_bytes"]
            state["last_bytes"] = 0
            cur.update(status="processing", percent=max(cur.get("percent") or 0.0, 99.9))
    return hook


def _worker(jid: str, url: str, opts: dict) -> None:
    with _SEM:
        cur = JOBS.get(jid)
        if cur is None: return
        try:
            import yt_dlp
        except ImportError:
            cur.update(status="error", error="yt-dlp tidak terpasang di venv")
            return
        out = LIBRARY / jid
        out.mkdir(parents=True, exist_ok=True)
        total_expected = [0]
        ydl_opts = {
            "outtmpl": str(out / "%(title).80s.%(ext)s"),
            "progress_hooks": [_hook(jid, total_expected)],
            "format": opts.get("format") or "bestvideo[height<=720]+bestaudio/best[height<=720]",
            "merge_output_format": "mp4",
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "retries": 3, "nocheckcertificate": True,
        }
        try:
            cur.update(status="extracting")
            _log(jid, f"extract: {url}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if cur.get("cancel"):
                    raise RuntimeError("dibatalkan user")
                # hitung total byte semua stream (video + audio)
                fmts = info.get("requested_formats") or []
                total_bytes = sum(
                    (f.get("filesize") or f.get("filesize_approx") or 0)
                    for f in fmts
                )
                if not total_bytes:
                    total_bytes = info.get("filesize") or info.get("filesize_approx") or 0
                total_expected[0] = total_bytes
                cur.update(
                    title=info.get("title") or "video",
                    thumbnail=info.get("thumbnail"),
                    duration=info.get("duration"),
                    uploader=info.get("uploader"),
                    total=total_bytes,
                    status="downloading",
                )
                _log(jid, f"download start: {info.get('title')}")
                ydl.process_ie_result(info, download=True)
            files = [p for p in out.iterdir()
                     if p.is_file() and p.suffix.lower() in
                     (".mp4", ".webm", ".mkv", ".m4a", ".mp3", ".opus")]
            if not files:
                raise RuntimeError("file hasil tidak ditemukan")
            final = max(files, key=lambda p: p.stat().st_mtime)
            meta = {
                "title": cur.get("title") or final.stem,
                "url": url,
                "duration": cur.get("duration"),
                "thumbnail": cur.get("thumbnail"),
                "uploader": cur.get("uploader"),
                "file": final.name,
                "size": final.stat().st_size,
                "downloaded_at": time.time(),
                "status": "done",
            }
            (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
            meta["percent"] = 100.0
            cur.update(meta)
            _log(jid, f"done: {final.name} ({final.stat().st_size} bytes)")
        except Exception as e:
            if cur.get("cancel"):
                _log(jid, "cancelled")
                cur.update(status="error", error="dibatalkan user")
            else:
                _log(jid, "ERROR:", traceback.format_exc())
                cur.update(status="error", error=str(e)[:500])


@bp.get("/download")
def page(): return render_template_string(_UI)


@bp.post("/api/download")
def start():
    data = request.get_json(force=True, silent=True) or {}
    url  = (data.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        return jsonify(error="URL harus http(s)://"), 400
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {
        "status": "queued", "url": url,
        "created": time.time(), "percent": 0,
    }
    threading.Thread(target=_worker, args=(jid, url, data), daemon=True).start()
    return jsonify(jid=jid)


@bp.get("/api/download/<jid>")
def status(jid):
    if not JID.match(jid) or jid not in JOBS:
        return jsonify(error="tidak ditemukan"), 404
    return jsonify(JOBS[jid])


@bp.get("/api/download/<jid>/debug")
def debug_job(jid):
    if not JID.match(jid):
        return jsonify(error="jid invalid"), 400
    return jsonify(
        job=JOBS.get(jid),
        on_disk=(LIBRARY / jid).exists(),
        files=[p.name for p in (LIBRARY / jid).iterdir()] if (LIBRARY / jid).exists() else [],
    )


@bp.get("/api/downloads")
def listing():
    items, seen = [], set()
    for jid, info in list(JOBS.items()):
        items.append({**info, "jid": jid}); seen.add(jid)
    if LIBRARY.is_dir():
        for d in LIBRARY.iterdir():
            if not (d.is_dir() and JID.match(d.name) and d.name not in seen):
                continue
            m = d / "meta.json"
            if m.exists():
                try:
                    rec = json.loads(m.read_text())
                    rec.setdefault("percent", 100.0)
                    items.append({**rec, "jid": d.name, "status": "done"})
                except Exception:
                    pass
    items.sort(key=lambda x: x.get("downloaded_at") or x.get("created") or 0,
               reverse=True)
    return jsonify(items=items)


@bp.post("/api/download/delete")
def delete():
    data = request.get_json(force=True, silent=True) or {}
    jid  = data.get("jid", "")
    if not JID.match(jid):
        return jsonify(error="jid invalid"), 400
    JOBS.pop(jid, None)
    shutil.rmtree(LIBRARY / jid, ignore_errors=True)
    return jsonify(ok=True)


@bp.post("/api/download/cancel")
def cancel():
    data = request.get_json(force=True, silent=True) or {}
    jid  = data.get("jid", "")
    if not JID.match(jid):
        return jsonify(error="jid invalid"), 400
    if jid in JOBS:
        JOBS[jid]["cancel"] = True
    return jsonify(ok=True)


@bp.get("/library/<jid>/<name>")
def library_file(jid, name):
    if not JID.match(jid):
        abort(404)
    return send_from_directory(LIBRARY / jid, name, conditional=True)

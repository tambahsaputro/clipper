#!/usr/bin/env bash
# setup_downloader.sh — pasang fitur downloader multi-sumber ke clipper
# Idempoten: aman dijalankan berulang, tidak menumpuk perubahan.
set -euo pipefail
cd "$(dirname "$0")"

# ── 0. Pastikan yt-dlp tersedia di venv ──────────────────────
if ! ./venv/bin/python -c "import yt_dlp" 2>/dev/null; then
  echo "▶ yt-dlp belum ada di venv — install…"
  ./venv/bin/pip install -q --upgrade "yt-dlp>=2024.1.0"
fi
./venv/bin/python -c "import yt_dlp; print('yt-dlp', yt_dlp.version.__version__)"

# ── 1. Tulis downloader.py (blueprint) ───────────────────────
echo "▶ 1/4 Membuat downloader.py…"
cat > downloader.py <<'PYEOF'
"""downloader.py — Blueprint Flask: koleksi video dari URL via yt-dlp.

Routes:
  GET  /download                    UI standalone
  POST /api/download                mulai job {url, format}
  GET  /api/download/<jid>          status satu job
  GET  /api/downloads               list semua (aktif + disk)
  POST /api/download/delete         hapus job + file
  POST /api/download/cancel         batalkan job berjalan
  GET  /library/<jid>/<name>        serve video
"""
from __future__ import annotations
import json, os, re, shutil, threading, time, uuid
from pathlib import Path
from flask import (Blueprint, abort, jsonify, render_template_string,
                   request, send_from_directory)

bp = Blueprint("downloader", __name__)
BASE    = Path(__file__).parent.resolve()
LIBRARY = Path(os.environ.get("CLIPPER_LIBRARY", BASE / "library")).expanduser()
LIBRARY.mkdir(parents=True, exist_ok=True)

JID          = re.compile(r"^[0-9a-f]{10}$")
JOBS: dict[str, dict] = {}
_SEM = threading.Semaphore(int(os.environ.get("CLIPPER_DL_PARALLEL", "3")))

_UI = r"""<!doctype html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Downloader · Clipper</title>
<style>
:root{--bg:#0f1115;--card:#181c24;--bd:#2a3140;--fg:#e6edf3;--mut:#8b96a8;--acc:#4f9cf9;--ok:#3fb950;--err:#f85149}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;align-items:center;gap:16px;padding:14px 20px;border-bottom:1px solid var(--bd);background:var(--card)}
header h1{margin:0;font-size:16px;font-weight:600}
header a{color:var(--mut);text-decoration:none;font-size:13px}header a:hover{color:var(--acc)}
.wrap{max-width:960px;margin:0 auto;padding:20px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:18px;margin-bottom:18px}
textarea{width:100%;min-height:100px;padding:10px;border-radius:8px;border:1px solid var(--bd);background:#0a0d12;color:var(--fg);resize:vertical;font:inherit}
textarea:focus,select:focus{outline:none;border-color:var(--acc)}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:12px}
select,button{padding:8px 14px;border-radius:8px;border:1px solid var(--bd);background:#0a0d12;color:var(--fg);cursor:pointer;font:inherit}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
button.primary:disabled{opacity:.5;cursor:wait}
button.danger{background:transparent;border-color:var(--err);color:var(--err);padding:4px 10px;font-size:12px}
.job{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px;margin-bottom:12px;display:flex;gap:14px}
.job img{width:120px;height:68px;object-fit:cover;border-radius:6px;background:#0a0d12;flex-shrink:0}
.job .info{flex:1;min-width:0}.job .title{font-weight:600;margin:0 0 4px;word-break:break-word}
.job .meta{color:var(--mut);font-size:12px}
.bar{height:6px;background:#0a0d12;border-radius:3px;overflow:hidden;margin-top:8px}
.bar div{height:100%;background:var(--acc);transition:width .3s}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px}
.b-queued{background:#2a3140;color:var(--mut)}.b-downloading{background:#1a3a5c;color:var(--acc)}
.b-processing{background:#3d2e00;color:#d29922}.b-done{background:#12341e;color:var(--ok)}
.b-error{background:#3d1418;color:var(--err)}
.empty{text-align:center;color:var(--mut);padding:40px}
video{max-width:100%;border-radius:6px;margin-top:8px}
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
const fs=b=>!b?'':b>1e9?(b/1e9).toFixed(2)+' GB':b>1e6?(b/1e6).toFixed(1)+' MB':((b/1e3)|0)+' KB';
const fd=s=>!s?'':Math.floor(s/60)+':'+String(s%60|0).padStart(2,'0');
const badge=s=>`<span class="badge b-${s}">${s}</span>`;
$('go').onclick=async()=>{
  const urls=$('urls').value.split('\n').map(s=>s.trim()).filter(Boolean);
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
  try{const r=await fetch('/api/downloads');const {items}=await r.json();
    $('list').innerHTML=items.length?items.map(render).join(''):'<div class="empty">Belum ada download.</div>';
  }catch(e){}
}
function render(j){
  const st=j.status||'queued',pct=j.percent||0;
  const active=['queued','downloading','processing'].includes(st);
  const meta=st==='done'?`${fs(j.size)} · ${fd(j.duration)}`:st==='error'?`<span style="color:var(--err)">${(j.error||'').slice(0,140)}</span>`:`${pct.toFixed(1)}% · ${fs(j.speed)}/s${j.eta?' · ETA '+fd(j.eta):''}`;
  const thumb=j.thumbnail?`<img src="${j.thumbnail}" onerror="this.style.visibility='hidden'">`:`<img style="opacity:.15">`;
  const video=st==='done'?`<video controls preload="metadata" src="/library/${j.jid}/${encodeURIComponent(j.file)}"></video>`:'';
  return `<div class="job">${thumb}<div class="info">
    <div style="display:flex;gap:10px;align-items:start"><p class="title" style="flex:1">${j.title||j.url||'(tanpa judul)'}</p>${badge(st)}</div>
    <div class="meta">${meta}</div>
    ${active?`<div class="bar"><div style="width:${pct}%"></div></div>`:''}
    ${video}
    <div style="margin-top:8px;display:flex;gap:8px;align-items:center">
      ${j.url?`<a href="${j.url}" target="_blank" rel="noopener" class="meta">🔗 sumber</a>`:''}
      ${j.file?`<a href="/library/${j.jid}/${encodeURIComponent(j.file)}" download class="meta">⬇ simpan</a>`:''}
      <button class="danger" style="margin-left:auto" onclick="del('${j.jid}')">Hapus</button>
    </div>
  </div></div>`;
}
async function del(jid){
  if(!confirm('Hapus file + job ini?'))return;
  await fetch('/api/download/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({jid})});
  refresh();
}
refresh();setInterval(refresh,2000);
</script></body></html>"""


def _hook(jid: str):
    def hook(d):
        cur = JOBS.get(jid)
        if cur is None:
            raise RuntimeError("job dihapus")
        if cur.get("cancel"):
            raise RuntimeError("dibatalkan user")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
            cur.update(status="downloading",
                       percent=(d.get("downloaded_bytes", 0) / total) * 100.0,
                       speed=d.get("speed") or 0, eta=d.get("eta") or 0)
        elif d.get("status") == "finished":
            cur.update(status="processing", percent=100.0)
    return hook


def _worker(jid: str, url: str, opts: dict) -> None:
    with _SEM:
        cur = JOBS.get(jid)
        if cur is None:
            return
        try:
            import yt_dlp  # lazy — modul besar
        except ImportError:
            cur.update(status="error", error="yt-dlp tidak terpasang di venv")
            return
        out = LIBRARY / jid
        out.mkdir(parents=True, exist_ok=True)
        ydl_opts = {
            "outtmpl": str(out / "%(title).80s.%(ext)s"),
            "progress_hooks": [_hook(jid)],
            "format": opts.get("format") or "bestvideo[height<=720]+bestaudio/best[height<=720]",
            "merge_output_format": "mp4",
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "retries": 3, "nocheckcertificate": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
            files = sorted((p for p in out.iterdir() if p.is_file()),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            final = next((f for f in files if f.suffix.lower() in
                          (".mp4", ".webm", ".mkv", ".m4a", ".mp3", ".opus")), None)
            if not final:
                raise RuntimeError("file hasil tidak ditemukan")
            meta = {"title": info.get("title") or final.stem, "url": url,
                    "duration": info.get("duration"), "thumbnail": info.get("thumbnail"),
                    "uploader": info.get("uploader"),
                    "file": final.name, "size": final.stat().st_size,
                    "downloaded_at": time.time(), "status": "done"}
            (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
            cur.update(meta)
        except Exception as e:
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
    JOBS[jid] = {"status": "queued", "url": url,
                 "created": time.time(), "percent": 0}
    threading.Thread(target=_worker, args=(jid, url, data), daemon=True).start()
    return jsonify(jid=jid)


@bp.get("/api/download/<jid>")
def status(jid):
    if not JID.match(jid) or jid not in JOBS:
        return jsonify(error="tidak ditemukan"), 404
    return jsonify(JOBS[jid])


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
                    items.append({**json.loads(m.read_text()),
                                  "jid": d.name, "status": "done"})
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
PYEOF

# ── 2. Patch webapp.py (idempoten) ───────────────────────────
echo "▶ 2/4 Patch webapp.py…"
python3 - <<'PYEOF'
import re, sys
from pathlib import Path
p, src, changed = Path("webapp.py"), Path("webapp.py").read_text(), 0

# 2a. import blueprint
if "from downloader import bp" not in src:
    anchor = "import clipper, editor"
    if anchor in src:
        src = src.replace(anchor, anchor + "\nfrom downloader import bp as downloader_bp", 1)
    else:
        lines = src.split("\n")
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith(("import ", "from ")):
                lines.insert(i + 1, "from downloader import bp as downloader_bp"); break
        src = "\n".join(lines)
    changed += 1

# 2b. register blueprint
if "register_blueprint(downloader_bp)" not in src:
    if "app = Flask(__name__)" not in src:
        sys.exit("ERR: 'app = Flask(__name__)' tidak ditemukan")
    src = src.replace("app = Flask(__name__)",
                      "app = Flask(__name__)\napp.register_blueprint(downloader_bp)", 1)
    changed += 1

# 2c. floating button di PAGE (fallback: skip kalau tidak ada <body>)
btn = ('<a href="/download" style="position:fixed;bottom:20px;right:20px;'
       'z-index:9999;display:inline-flex;align-items:center;gap:8px;'
       'padding:12px 18px;background:#4f9cf9;color:#fff;border-radius:50px;'
       'text-decoration:none;font-weight:600;'
       'box-shadow:0 6px 20px rgba(79,156,249,.45);'
       'font-family:system-ui,sans-serif">📥 Download</a>')
if btn not in src:
    src, n = re.subn(r"(<body[^>]*>)", r"\1\n" + btn, src, count=1)
    if n: changed += 1
    else: print("WARN: <body> tidak ditemukan di PAGE — tombol floating dilewati")

if changed:
    p.write_text(src); print(f"OK: {changed} perubahan ke webapp.py")
else:
    print("SKIP: webapp.py sudah dipatch sebelumnya")
PYEOF

# ── 3. .gitignore ────────────────────────────────────────────
echo "▶ 3/4 Update .gitignore…"
grep -qxF 'library/' .gitignore || echo 'library/' >> .gitignore

# ── 4. Verifikasi sintaks + commit ───────────────────────────
echo "▶ 4/4 Verifikasi & commit…"
python3 -c "import ast; [ast.parse(open(f).read()) for f in ('webapp.py','downloader.py')]; print('syntax OK')"

git add downloader.py webapp.py .gitignore
if ! git diff --cached --quiet; then
  git commit -m "feat(downloader): koleksi video dari URL via yt-dlp blueprint"
  echo "✅ Commit dibuat"
else
  echo "ℹ️  Tidak ada perubahan baru untuk di-commit"
fi

echo
echo "════════════════════════════════════════════"
echo "  ✅ Selesai. Jalankan:"
echo "     source venv/bin/activate && python webapp.py"
echo "     Buka: http://127.0.0.1:5000/download"
echo "════════════════════════════════════════════"

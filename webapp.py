#!/usr/bin/env python3
"""webapp.py - Kontrol Clipper lewat web. Jalankan: python webapp.py"""
import getpass, json, os, re, shutil, threading, uuid
from pathlib import Path
from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename
import clipper, editor

BASE = Path(__file__).parent.resolve()
RUNS = Path(os.environ.get("CLIPPER_DATA", BASE / "runs")).expanduser()
UPLOADS = RUNS / "_uploads"
PREVIEWS = RUNS / "_previews"
SETTINGS = BASE / "settings.json"
RUNS.mkdir(parents=True, exist_ok=True)
UPLOADS.mkdir(parents=True, exist_ok=True)
PREVIEWS.mkdir(parents=True, exist_ok=True)

JID = re.compile(r"^[0-9a-f]{10}$")
CLIP = re.compile(r"^clip(\d+)\.mp4$")
EDITED = re.compile(r"^edited\.mp4$")
app = Flask(__name__)
JOBS = {}


def load_settings():
    try: return json.loads(SETTINGS.read_text())
    except Exception: return {}


def drives():
    user = getpass.getuser()
    found = []
    for root in (Path("/media") / user, Path("/run/media") / user):
        if root.is_dir():
            for d in sorted(root.iterdir()):
                if d.is_dir() and os.path.ismount(d):
                    u = shutil.disk_usage(d)
                    found.append({"path": str(d), "free": u.free, "total": u.total})
    return found


def archive_dir():
    p = load_settings().get("drive")
    if p and os.path.ismount(p):
        return Path(p) / "clipper_arsip"
    return None


def run_picks(jd):
    try: return json.loads((jd / "picks.json").read_text())
    except Exception: return []


def clip_path(cid):
    jid, _, name = str(cid).partition("/")
    if JID.match(jid) and CLIP.match(name):
        p = RUNS / jid / name
        if p.exists(): return p
    return None


def remove_clip(p):
    p.unlink(missing_ok=True)
    p.with_suffix(".srt").unlink(missing_ok=True)
    if not list(p.parent.glob("clip*.mp4")):
        shutil.rmtree(p.parent, ignore_errors=True)


def slug(t):
    return re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-")[:40] or "clip"


def worker(jid, src, o):
    job = JOBS[jid]
    out = RUNS / jid
    out.mkdir(exist_ok=True)
    try:
        job["step"] = "1/4 Mengambil video..."
        video = clipper.get_video(src, out)
        job["step"] = "2/4 Membuat transkrip (bagian paling lama)..."
        segs = clipper.transcribe(video, out, o["model"], o["lang"])
        job["step"] = "3/4 Memilih highlight..."
        picks = None
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                picks = clipper.pick_with_llm(segs, o["clips"], o["lo"], o["hi"])
            except Exception as e:
                print("AI gagal, pakai heuristik:", e)
        if picks is None:
            picks = clipper.pick_heuristic(segs, o["clips"], o["lo"], o["hi"])
        (out / "picks.json").write_text(json.dumps(picks, ensure_ascii=False, indent=1))
        for i, p in enumerate(picks, 1):
            job["step"] = f"4/4 Render klip {i} dari {len(picks)}..."
            clipper.make_clip(video, segs, p, out, i)
            job["clips"].append({
                "file": f"clip{i}.mp4",
                "title": str(p.get("title", f"clip {i}")),
                "reason": str(p.get("reason", "")),
                "start": round(float(p["start"])),
                "end": round(float(p["end"])),
            })
        if o["delete_src"] and video.parent in (out, UPLOADS):
            video.unlink(missing_ok=True)
        job["status"], job["step"] = "done", "Selesai! Klip tersimpan di tab Perpustakaan."
    except Exception as e:
        import traceback; traceback.print_exc()
        job["status"] = "error"
        job["step"] = f"Gagal: {e} (lihat detail di terminal)"


def edit_worker(jid, src, params):
    job = JOBS[jid]
    out = RUNS / jid
    out.mkdir(exist_ok=True)
    try:
        def step(msg): job["step"] = msg
        editor.edit_video(src, params, out, step)
        job["status"] = "done"
        job["step"] = "Selesai! Video siap diunduh."
        job["output"] = "edited.mp4"
        for _p in (src, params.get("bgm"), params.get("watermark")):
            if _p and Path(_p).parent == UPLOADS:
                Path(_p).unlink(missing_ok=True)
    except Exception as e:
        import traceback; traceback.print_exc()
        job["status"] = "error"
        job["step"] = f"Gagal: {e} (lihat detail di terminal)"


@app.get("/")
def index(): return PAGE


@app.post("/start")
def start():
    f = request.files.get("file")
    url = request.form.get("url", "").strip()
    if f and f.filename:
        path = UPLOADS / secure_filename(f.filename); f.save(path); src = str(path)
    elif url:
        src = url
    else:
        return jsonify(error="Isi URL atau pilih file video."), 400
    try:
        o = {"clips": int(request.form.get("clips", 3)),
             "lo": int(request.form.get("lo", 25)),
             "hi": int(request.form.get("hi", 55)),
             "model": request.form.get("model", "small"),
             "lang": request.form.get("lang") or None,
             "delete_src": request.form.get("delete_src") == "on"}
    except ValueError:
        return jsonify(error="Angka tidak valid."), 400
    key = request.form.get("apikey", "").strip()
    if key: os.environ["ANTHROPIC_API_KEY"] = key
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"status": "running", "step": "Memulai...", "clips": []}
    threading.Thread(target=worker, args=(jid, src, o), daemon=True).start()
    return jsonify(id=jid)


@app.post("/edit")
def edit():
    f = request.files.get("file")
    url = request.form.get("url", "").strip()
    if f and f.filename:
        path = UPLOADS / secure_filename(f.filename); f.save(path); src = str(path)
    elif url:
        src = url
    else:
        return jsonify(error="Isi URL atau pilih file video."), 400
    bgm_f = request.files.get("bgm")
    bgm_path = None
    if bgm_f and bgm_f.filename:
        bgm_path = UPLOADS / f"bgm_{uuid.uuid4().hex[:8]}_{secure_filename(bgm_f.filename)}"
        bgm_f.save(bgm_path)
    wm_f = request.files.get("watermark")
    wm_path = None
    if wm_f and wm_f.filename:
        wm_path = UPLOADS / f"wm_{uuid.uuid4().hex[:8]}_{secure_filename(wm_f.filename)}"
        wm_f.save(wm_path)
    try:
        overlays = json.loads(request.form.get("text_overlays", "[]"))
    except Exception:
        overlays = []
    try:
        bgm_vol = float(request.form.get("bgm_volume", 30)) / 100.0
    except ValueError:
        bgm_vol = 0.3
    try:
        wm_size = int(request.form.get("watermark_size", 200))
    except ValueError:
        wm_size = 200
    params = {
        "aspect": request.form.get("aspect", "9:16"),
        "text_overlays": overlays,
        "color_grade": request.form.get("color_grade", "none"),
        "vignette": request.form.get("vignette") == "on",
        "sharpen": request.form.get("sharpen") == "on",
        "auto_subtitle": request.form.get("auto_subtitle") == "on",
        "model": request.form.get("model", "base"),
        "lang": request.form.get("lang", "id"),
        "bgm": str(bgm_path) if bgm_path else None,
        "bgm_volume": bgm_vol,
        "watermark": str(wm_path) if wm_path else None,
        "watermark_position": request.form.get("watermark_position", "top-right"),
        "watermark_size": wm_size,
    }
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"status": "running", "step": "Memulai...", "kind": "edit"}
    threading.Thread(target=edit_worker, args=(jid, src, params), daemon=True).start()
    return jsonify(id=jid)


@app.get("/status/<jid>")
def status(jid):
    if jid not in JOBS: abort(404)
    return jsonify(JOBS[jid])


@app.post("/preview")
def preview():
    f = request.files.get("file")
    url = request.form.get("url", "").strip()
    if f and f.filename:
        path = UPLOADS / f"prev_{uuid.uuid4().hex[:8]}_{secure_filename(f.filename)}"
        f.save(path)
        src = str(path)
    elif url:
        return jsonify(error="Preview hanya untuk file lokal (URL akan diunduh penuh)."), 400
    else:
        return jsonify(error="Pilih file video dulu."), 400
    try:
        overlays = json.loads(request.form.get("text_overlays", "[]"))
    except Exception:
        overlays = []
    try:
        wm_size = int(request.form.get("watermark_size", 200))
    except ValueError:
        wm_size = 200
    try:
        ptime = float(request.form.get("preview_time", 1))
    except ValueError:
        ptime = 1.0
    params = {
        "aspect": request.form.get("aspect", "9:16"),
        "text_overlays": overlays,
        "color_grade": request.form.get("color_grade", "none"),
        "vignette": request.form.get("vignette") == "on",
        "sharpen": request.form.get("sharpen") == "on",
        "watermark_position": request.form.get("watermark_position", "top-right"),
        "watermark_size": wm_size,
    }
    wm_f = request.files.get("watermark")
    if wm_f and wm_f.filename:
        wm_path = UPLOADS / f"wmp_{uuid.uuid4().hex[:8]}_{secure_filename(wm_f.filename)}"
        wm_f.save(wm_path)
        params["watermark"] = str(wm_path)
    pname = f"prev_{uuid.uuid4().hex[:10]}.png"
    try:
        editor.preview_frame(src, params, PREVIEWS, time=ptime, name=pname)
        return jsonify(url=f"/previews/{pname}", time=ptime)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(error=f"Preview gagal: {e}"), 500
    finally:
        Path(src).unlink(missing_ok=True)
        if params.get("watermark"):
            Path(params["watermark"]).unlink(missing_ok=True)
        for _t in PREVIEWS.glob("prev_txt_*.txt"):
            _t.unlink(missing_ok=True)


@app.get("/previews/<name>")
def previews(name):
    if not re.match(r"^prev_[a-f0-9]+\.png$", name):
        abort(404)
    return send_from_directory(PREVIEWS, name)


@app.get("/api/storage")
def api_storage():
    u = shutil.disk_usage(RUNS)
    ad = archive_dir()
    return jsonify(ssd={"free": u.free, "total": u.total}, drives=drives(),
                   drive=load_settings().get("drive"), archive=str(ad) if ad else None)


@app.post("/api/drive")
def api_drive():
    path = (request.json or {}).get("path", "")
    if path and path not in [d["path"] for d in drives()]:
        return jsonify(error="Drive tidak ditemukan."), 400
    SETTINGS.write_text(json.dumps({"drive": path}))
    return jsonify(ok=True)


@app.get("/api/library")
def api_library():
    clips = []
    for jd in sorted(RUNS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not (jd.is_dir() and JID.match(jd.name)): continue
        picks = run_picks(jd)
        for f in sorted(jd.glob("clip*.mp4")):
            m = CLIP.match(f.name)
            if not m: continue
            i = int(m.group(1)) - 1
            p = picks[i] if i < len(picks) else {}
            clips.append({"id": f"{jd.name}/{f.name}",
                          "url": f"/files/{jd.name}/{f.name}",
                          "title": str(p.get("title", f.name)),
                          "reason": str(p.get("reason", "")),
                          "size": f.stat().st_size})
    arsip = []
    ad = archive_dir()
    if ad and ad.is_dir():
        for f in sorted(ad.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
            meta = {}
            try: meta = json.loads(f.with_suffix(".json").read_text())
            except Exception: pass
            arsip.append({"id": f.name, "url": f"/archive/{f.name}",
                          "title": str(meta.get("title", f.name)),
                          "reason": str(meta.get("reason", "")),
                          "size": f.stat().st_size})
    return jsonify(clips=clips, arsip=arsip)


@app.post("/api/archive")
def api_archive():
    ad = archive_dir()
    if not ad:
        return jsonify(error="Pilih drive arsip dulu (pastikan HDD terpasang)."), 400
    ad.mkdir(parents=True, exist_ok=True)
    moved = failed = 0
    for cid in (request.json or {}).get("ids", []):
        src = clip_path(cid)
        if not src: continue
        pick, picks = {}, run_picks(src.parent)
        i = int(CLIP.match(src.name).group(1)) - 1
        if i < len(picks): pick = picks[i]
        dest = ad / f"{slug(str(pick.get('title','')))}_{src.parent.name}_{src.name}"
        try:
            shutil.copyfile(src, dest)
            if dest.stat().st_size != src.stat().st_size:
                raise OSError("ukuran tidak cocok")
            dest.with_suffix(".json").write_text(json.dumps(pick, ensure_ascii=False))
            remove_clip(src); moved += 1
        except OSError as e:
            print("Gagal arsip:", e); dest.unlink(missing_ok=True); failed += 1
    return jsonify(moved=moved, failed=failed)


@app.post("/api/delete")
def api_delete():
    data = request.json or {}
    n = 0
    if data.get("where") == "arsip":
        ad = archive_dir()
        for name in data.get("ids", []):
            name = Path(str(name)).name
            if ad and name.endswith(".mp4") and (ad / name).exists():
                (ad / name).unlink()
                (ad / name).with_suffix(".json").unlink(missing_ok=True)
                n += 1
    else:
        for cid in data.get("ids", []):
            p = clip_path(cid)
            if p: remove_clip(p); n += 1
    return jsonify(deleted=n)


@app.get("/files/<jid>/<name>")
def files(jid, name):
    if not (JID.match(jid) and (CLIP.match(name) or EDITED.match(name))):
        abort(404)
    return send_from_directory(RUNS / jid, name)


@app.get("/archive/<name>")
def archive_file(name):
    ad = archive_dir()
    if not ad or not name.endswith(".mp4"): abort(404)
    return send_from_directory(ad, name)


PAGE = r"""<!doctype html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clipper</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--tx:#1b1f24;--mu:#6b7280;--ac:#4f46e5;--bd:#e5e7eb;--rd:#dc2626}
@media(prefers-color-scheme:dark){:root{--bg:#111318;--card:#1a1d24;--tx:#e8eaed;--mu:#9aa0a6;--bd:#2a2e37}}
*{box-sizing:border-box}
body{margin:0;font:16px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--tx)}
main{max-width:900px;margin:0 auto;padding:24px 16px 60px}
h1{margin:0 0 12px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px;margin-bottom:14px}
label{display:block;font-size:14px;color:var(--mu);margin:12px 0 4px}
input,select{width:100%;padding:9px 10px;border:1px solid var(--bd);border-radius:8px;background:var(--bg);color:var(--tx);font:inherit}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px}
button{padding:9px 16px;border:0;border-radius:8px;background:var(--ac);color:#fff;font:inherit;font-weight:600;cursor:pointer}
button.sec{background:transparent;color:var(--tx);border:1px solid var(--bd)}
button.del{background:var(--rd)}
button:disabled{opacity:.5;cursor:wait}
.tabs{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
.tabs button{background:transparent;color:var(--tx);border:1px solid var(--bd)}
.tabs button.on{background:var(--ac);color:#fff;border-color:var(--ac)}
.stor{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;font-size:14px}
.bar{height:8px;background:var(--bd);border-radius:4px;overflow:hidden;margin:4px 0}
.bar i{display:block;height:100%;background:var(--ac)}
.warn{color:var(--rd);font-weight:600}.mu{color:var(--mu);font-size:14px}
#step,#stepEdit{font-weight:600}.err{color:var(--rd)}
.tools{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.clips{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
.clip video{width:100%;aspect-ratio:9/16;background:#000;border-radius:8px}
.clip h3{margin:8px 0 2px;font-size:15px}.clip p{margin:0 0 6px;color:var(--mu);font-size:13px}
.clip a{color:var(--ac);font-size:14px;font-weight:600}
.clip .pick{display:flex;gap:6px;align-items:center;font-size:14px}.clip .pick input{width:auto}
</style></head><body><main>
<h1>Clipper</h1>
<div class="card"><div class="stor" id="stor"></div></div>
<div class="tabs" id="tabs">
<button data-t="buat" class="on">Buat klip</button>
<button data-t="edit">Edit video</button>
<button data-t="lib">Perpustakaan (SSD)</button>
<button data-t="arsip">Arsip (HDD)</button></div>
<div id="msg" class="mu" style="margin-bottom:10px"></div>

<section id="t-buat">
<div class="card"><form id="f">
<label>URL YouTube</label><input name="url" placeholder="https://www.youtube.com/watch?v=...">
<label>...atau pilih file video dari komputer</label><input type="file" name="file" accept="video/*">
<div class="row">
<div><label>Jumlah klip</label><input type="number" name="clips" value="3" min="1" max="20"></div>
<div><label>Durasi min (detik)</label><input type="number" name="lo" value="25"></div>
<div><label>Durasi maks (detik)</label><input type="number" name="hi" value="55"></div>
<div><label>Model Whisper</label><select name="model"><option>tiny</option><option selected>base</option><option>small</option><option>medium</option></select></div>
<div><label>Bahasa video</label><select name="lang"><option value="id">Indonesia</option><option value="en">Inggris</option><option value="">Otomatis</option></select></div>
</div>
<label style="display:flex;gap:8px;align-items:center;color:var(--tx)"><input type="checkbox" name="delete_src" checked style="width:auto">Hapus video sumber setelah klip jadi</label>
<label>API key Anthropic (opsional)</label>
<input type="password" name="apikey" placeholder="sk-ant-... (kosongkan untuk heuristik)">
<div style="margin-top:16px"><button id="go">Mulai proses</button></div></form></div>
<div class="card" id="prog" hidden><div id="step"></div></div>
<div class="clips" id="out"></div>
</section>

<section id="t-edit" hidden>
<div class="card"><form id="fEdit">
<label>URL YouTube</label><input name="url" placeholder="https://www.youtube.com/watch?v=... (video pendek)">
<label>...atau pilih file video dari komputer</label><input type="file" name="file" accept="video/*">
<div class="row">
<div><label>Rasio output</label><select name="aspect">
<option value="9:16" selected>9:16 (TikTok/Reels)</option>
<option value="1:1">1:1 (Feed)</option>
<option value="16:9">16:9 (YouTube)</option>
</select></div>
<div><label>Warna</label><select name="color_grade">
<option value="none" selected>Original</option>
<option value="warm">Warm</option>
<option value="cool">Cool</option>
<option value="vintage">Vintage</option>
<option value="bw">Hitam-putih</option>
</select></div>
</div>
<label style="display:flex;gap:8px;align-items:center;color:var(--tx)"><input type="checkbox" name="sharpen" checked style="width:auto">Tajamkan video</label>
<label style="display:flex;gap:8px;align-items:center;color:var(--tx)"><input type="checkbox" name="vignette" style="width:auto">Vignette (gelap di tepi)</label>
<label style="display:flex;gap:8px;align-items:center;color:var(--tx)"><input type="checkbox" name="auto_subtitle" checked style="width:auto">Auto subtitle (dari Whisper)</label>
<label>Preset (isi otomatis)</label>
<select id="presetSel"><option value="">- pilih preset -</option><option value="tiktok">TikTok (warm + sub)</option><option value="cinematic">Cinematic (cool + vignette)</option><option value="podcast">Podcast (clean + sub)</option></select>
<label>BGM / musik latar (opsional)</label>
<input type="file" name="bgm" accept="audio/*">
<div class="row">
<div><label>Volume BGM (%)</label><input type="number" name="bgm_volume" value="30" min="0" max="100"></div>
<div><label>Watermark PNG (opsional)</label><input type="file" name="watermark" accept="image/*"></div>
<div><label>Posisi watermark</label><select name="watermark_position"><option value="top-left">Kiri atas</option><option value="top-right" selected>Kanan atas</option><option value="bottom-left">Kiri bawah</option><option value="bottom-right">Kanan bawah</option></select></div>
<div><label>Ukuran watermark (px)</label><input type="number" name="watermark_size" value="200" min="50" max="600"></div>
</div>
<div class="row">
<div><label>Model Whisper</label><select name="model"><option>tiny</option><option selected>base</option><option>small</option></select></div>
<div><label>Bahasa</label><select name="lang"><option value="id">Indonesia</option><option value="en">Inggris</option><option value="">Otomatis</option></select></div>
</div>
<label style="margin-top:16px">Text overlay (opsional)</label>
<div id="overlayList"></div>
<button type="button" class="sec" id="addOverlay" style="margin-top:8px">+ Tambah teks</button>
<div style="margin-top:16px">
<button type="button" class="sec" id="btnPreview">Preview frame</button>
<button id="goEdit">Mulai edit</button>
</div>
</form></div>
<div class="card" id="previewBox" hidden>
<div style="font-weight:600;margin-bottom:8px">Preview (frame pada detik ke-1)</div>
<img id="previewImg" style="max-width:100%;border-radius:8px;display:block">
<div class="mu" style="margin-top:6px">Subtitle & BGM tidak ditampilkan di preview. Ini hanya perkiraan visual.</div>
</div>
<div class="card" id="progEdit" hidden><div id="stepEdit"></div></div>
<div class="clips" id="outEdit"></div>
</section>

<section id="t-lib" hidden>
<div class="tools">
<button class="sec" data-all="lib">Pilih semua</button>
<button id="bArc">Arsipkan terpilih ke HDD</button>
<button class="del" id="bDelLib">Hapus terpilih</button></div>
<div class="clips" id="lib"></div></section>

<section id="t-arsip" hidden>
<div class="tools">
<button class="sec" data-all="arsip">Pilih semua</button>
<button class="del" id="bDelArc">Hapus terpilih</button></div>
<div class="clips" id="arsip"></div></section>

<script>
const $=s=>document.querySelector(s);
const fmt=b=>b>=1e9?(b/1e9).toFixed(1)+' GB':(b/1e6).toFixed(1)+' MB';
function el(t,txt,cls){const e=document.createElement(t);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e}
async function api(u,body){const r=await fetch(u,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:undefined);return r.json()}
function say(t,bad){const m=$('#msg');m.textContent=t;m.className=bad?'err':'mu'}

document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('#tabs button').forEach(x=>x.classList.toggle('on',x===b));
  ['buat','edit','lib','arsip'].forEach(k=>$('#t-'+k).hidden=(k!==b.dataset.t));
  if(b.dataset.t==='lib'||b.dataset.t==='arsip')loadLib();
});

function usage(name,u,extra){
  const d=el('div');d.append(el('b',name));
  const used=(u.total-u.free)/u.total*100,bar=el('div',null,'bar'),i=el('i');
  i.style.width=used.toFixed(0)+'%';bar.append(i);
  d.append(bar,el('span','Sisa '+fmt(u.free)+' dari '+fmt(u.total),(u.free<10e9?'warn':'mu')));
  if(extra)d.append(extra);return d;
}
async function loadStor(){
  const s=await api('/api/storage'),box=$('#stor');box.innerHTML='';
  box.append(usage('SSD (kerja)',s.ssd));
  const sel=el('select');
  sel.append(new Option('- pilih drive arsip -',''));
  s.drives.forEach(d=>sel.append(new Option(d.path+' ('+fmt(d.free)+' bebas)',d.path)));
  sel.value=s.drives.some(d=>d.path===s.drive)?s.drive:'';
  sel.onchange=async()=>{await api('/api/drive',{path:sel.value});loadStor();loadLib()};
  const cur=s.drives.find(d=>d.path===s.drive);
  const w=el('div');w.append(el('b','Drive arsip (HDD)'),el('div'),sel);
  if(cur)w.append(usage('',cur));
  else w.append(el('div','HDD belum dipilih atau belum terpasang.','mu'));
  box.append(w);
}

function card(c){
  const d=el('div',null,'card clip');
  const p=el('label',null,'pick');p.style.margin='0 0 6px';
  const cb=el('input');cb.type='checkbox';cb.dataset.id=c.id;p.append(cb,document.createTextNode('Pilih'));
  const v=el('video');v.src=c.url;v.controls=true;v.preload='metadata';
  const a=el('a','Unduh');a.href=c.url;a.download='';
  d.append(p,v,el('h3',c.title),el('p',fmt(c.size)+(c.reason?'. '+c.reason:'')),a);return d;
}
async function loadLib(){
  const r=await api('/api/library');
  [['lib',r.clips,'Belum ada klip di SSD.'],['arsip',r.arsip,'Arsip kosong.']].forEach(([id,items,empty])=>{
    const box=$('#'+id);box.innerHTML='';
    if(!items.length)box.append(el('p',empty,'mu'));
    items.forEach(c=>box.append(card(c)));
  });
}
const picked=id=>[...document.querySelectorAll('#'+id+' input[type=checkbox]:checked')].map(c=>c.dataset.id);
document.querySelectorAll('[data-all]').forEach(b=>b.onclick=()=>{
  const cbs=document.querySelectorAll('#'+b.dataset.all+' input[type=checkbox]');
  const on=[...cbs].some(c=>!c.checked);cbs.forEach(c=>c.checked=on)});
$('#bArc').onclick=async()=>{
  const ids=picked('lib');if(!ids.length)return say('Pilih klip dulu.',1);
  say('Memindahkan ke HDD...');
  const r=await api('/api/archive',{ids});
  if(r.error)return say(r.error,1);
  say(r.moved+' klip dipindahkan'+(r.failed?', '+r.failed+' gagal':'')+'.',r.failed);
  loadStor();loadLib();
};
async function del(id,where){
  const ids=picked(id);if(!ids.length)return say('Pilih klip dulu.',1);
  if(!confirm('Hapus '+ids.length+' klip?'))return;
  const r=await api('/api/delete',{where,ids});say(r.deleted+' klip dihapus.');loadStor();loadLib();
}
$('#bDelLib').onclick=()=>del('lib','ssd');
$('#bDelArc').onclick=()=>del('arsip','arsip');

/* ---- buat klip ---- */
const f=$('#f'),go=$('#go'),prog=$('#prog'),step=$('#step'),out=$('#out');let shown=0;
f.addEventListener('submit',async e=>{
  e.preventDefault();go.disabled=true;out.innerHTML='';shown=0;
  prog.hidden=false;step.className='';step.textContent='Mengirim...';
  const r=await fetch('/start',{method:'POST',body:new FormData(f)});
  const d=await r.json();
  if(!r.ok){step.className='err';step.textContent=d.error;go.disabled=false;return}
  poll(d.id);
});
async function poll(id){
  const s=await (await fetch('/status/'+id)).json();
  step.textContent=s.step;
  for(;shown<s.clips.length;shown++){
    const c=s.clips[shown],u='/files/'+id+'/'+c.file,div=el('div',null,'card clip');
    const v=el('video');v.src=u;v.controls=true;v.preload='metadata';
    const a=el('a','Unduh');a.href=u;a.download=c.file;
    div.append(v,el('h3',c.title),el('p',c.start+'s - '+c.end+'s. '+c.reason),a);out.append(div);
  }
  if(s.status==='running'){setTimeout(()=>poll(id),2000)}
  else{go.disabled=false;if(s.status==='error')step.className='err';loadStor()}
}

/* ---- edit video ---- */
const fE=$('#fEdit'),goE=$('#goEdit'),progE=$('#progEdit'),stepE=$('#stepEdit'),outE=$('#outEdit');
const PRESETS={tiktok:{cg:'warm',sh:true,vg:false,as:true},cinematic:{cg:'cool',sh:true,vg:true,as:false},podcast:{cg:'none',sh:true,vg:false,as:true}};
$('#presetSel').onchange=e=>{const p=PRESETS[e.target.value];if(!p)return;fE.color_grade.value=p.cg;fE.sharpen.checked=p.sh;fE.vignette.checked=p.vg;fE.auto_subtitle.checked=p.as;};
function addOverlayRow(){
  const div=el('div');div.className='card';div.style.padding='10px';div.style.marginTop='8px';
  div.innerHTML=`
    <div class="row">
      <div style="grid-column:1/-1"><label>Teks</label><input class="ov-text" placeholder="HOOK KEREN"></div>
      <div><label>Mulai (detik)</label><input type="number" class="ov-start" value="0" min="0" step="0.1"></div>
      <div><label>Selesai (detik)</label><input type="number" class="ov-end" value="3" min="0.5" step="0.1"></div>
      <div><label>Posisi</label><select class="ov-pos"><option value="top">Atas</option><option value="center">Tengah</option><option value="bottom" selected>Bawah</option></select></div>
      <div><label>Ukuran</label><input type="number" class="ov-size" value="60" min="20" max="200"></div>
      <div><label>Warna</label><input type="color" class="ov-color" value="#ffffff"></div>
    </div>
    <button type="button" class="del" style="margin-top:8px">Hapus</button>`;
  div.querySelector('.del').onclick=()=>div.remove();
  $('#overlayList').append(div);
}
$('#addOverlay').onclick=addOverlayRow;
fE.addEventListener('submit',async e=>{
  e.preventDefault();goE.disabled=true;outE.innerHTML='';stepE.className='';
  progE.hidden=false;stepE.textContent='Mengirim...';
  const fd=new FormData(fE);
  const overlays=[...document.querySelectorAll('#overlayList > div')].map(d=>({
    text:d.querySelector('.ov-text').value,
    start:parseFloat(d.querySelector('.ov-start').value)||0,
    end:parseFloat(d.querySelector('.ov-end').value)||3,
    position:d.querySelector('.ov-pos').value,
    size:parseInt(d.querySelector('.ov-size').value)||60,
    color:d.querySelector('.ov-color').value,
  })).filter(o=>o.text.trim());
  fd.append('text_overlays',JSON.stringify(overlays));
  const r=await fetch('/edit',{method:'POST',body:fd});
  const d=await r.json();
  if(!r.ok){stepE.className='err';stepE.textContent=d.error;goE.disabled=false;return}
  pollEdit(d.id);
});
async function pollEdit(id){
  const s=await (await fetch('/status/'+id)).json();
  stepE.textContent=s.step;
  if(s.status==='running'){setTimeout(()=>pollEdit(id),2000);return}
  goE.disabled=false;
  if(s.status==='error'){stepE.className='err';return}
  if(s.output){
    const u='/files/'+id+'/'+s.output,div=el('div',null,'card clip');
    const v=el('video');v.src=u;v.controls=true;v.preload='metadata';
    const a=el('a','Unduh video');a.href=u;a.download='edited.mp4';
    div.append(v,el('h3','Video siap upload'),a);outE.append(div);
    loadStor();
  }
}
$('#btnPreview').onclick=async()=>{
  const fd=new FormData(fE);
  const overlays=[...document.querySelectorAll('#overlayList > div')].map(d=>({
    text:d.querySelector('.ov-text').value,
    start:parseFloat(d.querySelector('.ov-start').value)||0,
    end:parseFloat(d.querySelector('.ov-end').value)||3,
    position:d.querySelector('.ov-pos').value,
    size:parseInt(d.querySelector('.ov-size').value)||60,
    color:d.querySelector('.ov-color').value,
  })).filter(o=>o.text.trim());
  fd.append('text_overlays',JSON.stringify(overlays));
  fd.append('preview_time','1');
  $('#btnPreview').disabled=true;
  say('Membuat preview...');
  try{
    const r=await fetch('/preview',{method:'POST',body:fd});
    const d=await r.json();
    if(!r.ok){say(d.error||'Preview gagal',1);return}
    $('#previewImg').src=d.url+'?t='+Date.now();
    $('#previewBox').hidden=false;
    say('Preview siap. Kalau sudah pas, klik "Mulai edit".');
  }catch(e){say('Preview error: '+e,1)}
  finally{$('#btnPreview').disabled=false}
};

loadStor();
</script></main></body></html>
"""

def _local_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    ip = _local_ip()
    print("=" * 50)
    print(f"Buka di browser (laptop): http://127.0.0.1:5000")
    print(f"Buka dari HP/device lain: http://{ip}:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000)

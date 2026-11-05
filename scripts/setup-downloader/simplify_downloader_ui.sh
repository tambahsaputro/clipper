#!/usr/bin/env bash
# simplify_downloader_ui.sh — hapus video player & thumbnail, fokus progress bar
set -euo pipefail
cd "$(dirname "$0")"

[ -f downloader.py ] || { echo "ERR: downloader.py tidak ada — jalankan setup_downloader.sh dulu"; exit 1; }

echo "▶ Patch UI di downloader.py…"
venv/bin/python - <<'PYEOF'
import re
from pathlib import Path

p = Path("downloader.py")
src = p.read_text()

new_ui = r'''_UI = r"""<!doctype html>
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
</script></body></html>"""'''

# Ganti blok _UI lama dengan yang baru (multiline, dari "_UI = r\"\"\"" ke "\"\"\"")
pattern = re.compile(r'_UI\s*=\s*r""".*?"""', re.DOTALL)
if not pattern.search(src):
    raise SystemExit("ERR: blok _UI tidak ditemukan")
new_src = pattern.sub(new_ui, src, count=1)
if new_src == src:
    print("SKIP: _UI sudah versi ringkas")
else:
    p.write_text(new_src)
    print(f"OK: _UI diganti ({len(src)} → {len(new_src)} bytes)")
PYEOF

echo "▶ Verifikasi sintaks…"
venv/bin/python -c "import ast; ast.parse(open('downloader.py').read()); print('syntax OK')"

echo "▶ Commit…"
git add downloader.py
if ! git diff --cached --quiet; then
  git commit -m "ui(downloader): ringkas — hapus video player & thumbnail, fokus progress bar"
  echo "✅ Commit dibuat"
else
  echo "ℹ️  Tidak ada perubahan"
fi

echo
echo "════════════════════════════════════════════════════"
echo "  ✅ Selesai. Restart:"
echo "     pkill -f 'python webapp.py' 2>/dev/null || true"
echo "     source venv/bin/activate && python webapp.py 2>&1 | tee -a dl.log"
echo "════════════════════════════════════════════════════"

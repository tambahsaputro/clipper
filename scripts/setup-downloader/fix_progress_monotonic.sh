#!/usr/bin/env bash
# fix_progress_monotonic.sh — progress tidak mundur saat multi-stream (video+audio)
set -euo pipefail
cd "$(dirname "$0")"
[ -f downloader.py ] || { echo "ERR: downloader.py tidak ada"; exit 1; }

echo "▶ Patch downloader.py…"
venv/bin/python - <<'PYEOF'
from pathlib import Path
p = Path("downloader.py")
src = p.read_text()

# ── 1. Ganti fungsi _hook: agregasi multi-stream ────────────
old_hook_start = "def _hook(jid: str):"
old_hook_end   = "\n\n\ndef _worker("
i = src.find(old_hook_start)
j = src.find(old_hook_end)
if i < 0 or j < 0:
    raise SystemExit("ERR: blok _hook tidak ditemukan")

new_hook = '''def _hook(jid: str, total_expected: list):
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
    return hook'''
src = src[:i] + new_hook + src[j:]

# ── 2. _worker: hitung total_expected dari requested_formats ─
old_call = '        try:\n            cur.update(status="extracting")'
new_call = '''        total_expected = [0]
        try:
            cur.update(status="extracting")'''
if old_call not in src:
    raise SystemExit("ERR: anchor 'cur.update(status=\"extracting\")' tidak ditemukan")
src = src.replace(old_call, new_call, 1)

# ── 3. Sisipkan perhitungan total_expected setelah extract_info ─
old_extract = '''                cur.update(
                    title=info.get("title") or "video",
                    thumbnail=info.get("thumbnail"),
                    duration=info.get("duration"),
                    uploader=info.get("uploader"),
                    status="downloading",
                )'''
new_extract = '''                # hitung total byte semua stream (video + audio)
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
                )'''
if old_extract not in src:
    raise SystemExit("ERR: blok setelah extract_info tidak ditemukan")
src = src.replace(old_extract, new_extract, 1)

# ── 4. Ganti pemanggilan progress_hooks (butuh total_expected) ─
# YDL opts dibuat sebelum extract; hook butuh total_expected yang diisi belakangan.
# Solusi: inisialisasi total_expected sebelum ydl_opts, lalu edit baris hook.
old_opts = '''        ydl_opts = {
            "outtmpl": str(out / "%(title).80s.%(ext)s"),
            "progress_hooks": [_hook(jid)],'''
new_opts = '''        total_expected = [0]
        ydl_opts = {
            "outtmpl": str(out / "%(title).80s.%(ext)s"),
            "progress_hooks": [_hook(jid, total_expected)],'''
if old_opts not in src:
    raise SystemExit("ERR: ydl_opts tidak ditemukan")
src = src.replace(old_opts, new_opts, 1)

# hapus deklarasi total_expected duplikat (yang di step 2)
src = src.replace('        total_expected = [0]\n        try:\n            cur.update(status="extracting")',
                  '        try:\n            cur.update(status="extracting")', 1)

# ── 5. listing(): paksa percent=100 untuk item 'done' dari disk ─
old_listing = '''                    items.append({**json.loads(m.read_text()),
                                  "jid": d.name, "status": "done"})'''
new_listing = '''                    rec = json.loads(m.read_text())
                    rec.setdefault("percent", 100.0)
                    items.append({**rec, "jid": d.name, "status": "done"})'''
if old_listing not in src:
    raise SystemExit("ERR: blok listing disk tidak ditemukan")
src = src.replace(old_listing, new_listing, 1)

# ── 6. Saat job selesai (done), pastikan percent=100 ────────
old_done = '            cur.update(meta)\n            _log(jid, f"done:'
new_done = '            meta["percent"] = 100.0\n            cur.update(meta)\n            _log(jid, f"done:'
if old_done in src:
    src = src.replace(old_done, new_done, 1)

p.write_text(src)
print("OK: downloader.py dipatch (monotonic + histori percent)")
PYEOF

echo "▶ Verifikasi sintaks…"
venv/bin/python -c "import ast; ast.parse(open('downloader.py').read()); print('syntax OK')"

echo "▶ Commit…"
git add downloader.py
if ! git diff --cached --quiet; then
  git commit -m "fix(downloader): progress monotonik (agregasi multi-stream) + persen histori=100 untuk done"
  echo "✅ Commit dibuat"
else
  echo "ℹ️  Tidak ada perubahan"
fi

echo
echo "════════════════════════════════════════════════════"
echo "  ✅ Restart server:"
echo "     systemctl --user restart clipper    (kalau pakai systemd)"
echo "  atau:"
echo "     pkill -f 'python webapp.py'; sleep 1"
echo "     source venv/bin/activate && nohup python webapp.py >> dl.log 2>&1 & disown"
echo "════════════════════════════════════════════════════"

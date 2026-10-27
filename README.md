# Clipper

Tool pemotong + editor video pendek otomatis, dengan web UI.

## Fitur
- Potong: video panjang (YouTube/URL/lokal) jadi klip vertikal 9:16 + subtitle
- Edit: video pendek, tambah text overlay, color grade, BGM, watermark, auto subtitle
- Perpustakaan: kelola klip di SSD, arsip ke HDD

## Instalasi
  python3 -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt
  sudo apt install ffmpeg

## Pakai
  python webapp.py
  buka http://127.0.0.1:5000

## CLI (mode potong saja)
  python clipper.py video.mp4 --clips 3 --model base --lang id --out output/

## Struktur
- clipper.py  : mesin potong (transkrip + pilih highlight + render)
- editor.py   : mesin edit (text/effect/BGM/watermark)
- webapp.py   : web UI (Flask)
- runs/       : output per-job (gitignored)

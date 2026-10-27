#!/usr/bin/env python3
"""
clipper.py - Tool sederhana untuk membuat klip pendek vertikal (9:16) dari video panjang.

Alur: download (yt-dlp) -> transkrip (faster-whisper) -> pilih highlight
      (Claude API kalau ada ANTHROPIC_API_KEY, kalau tidak pakai heuristik)
      -> potong + crop 9:16 + subtitle (ffmpeg)

Contoh:
  python clipper.py "https://youtube.com/watch?v=XXXX" --clips 5
  python clipper.py video.mp4 --clips 3 --min 25 --max 50 --lang id
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

MODEL = "claude-sonnet-4-5"  # nama model Claude yang valid


def run(cmd, **kw):
    subprocess.run(cmd, check=True, **kw)


def get_video(src, workdir):
    if Path(src).exists():
        return Path(src).resolve()
    print("[1/4] Download video...")
    run([
        "yt-dlp", "-f", "bv*[height<=1080]+ba/b[height<=1080]",
        "--merge-output-format", "mp4",
        "-o", str(workdir / "source.%(ext)s"), src,
    ])
    return workdir / "source.mp4"


def load_audio(video):
    """Baca audio lewat ffmpeg (16 kHz mono) tanpa memakai PyAV."""
    import numpy as np
    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(video),
         "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
        check=True, stdout=subprocess.PIPE,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32)


def transcribe(video, workdir, size, lang):
    # Cache di-keyed oleh model & bahasa, supaya ganti --model/--lang
    # tidak diam-diam memakai transkrip lama.
    tag = f"{size}-{lang or 'auto'}"
    cache = workdir / f"transcript-{tag}.json"
    if cache.exists():
        print(f"[2/4] Pakai transkrip cache ({cache.name}).")
        return json.loads(cache.read_text())
    print(f"[2/4] Transkrip dengan Whisper ({size})... ini bisa lama di CPU.")
    from faster_whisper import WhisperModel
    model = WhisperModel(size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(load_audio(video), vad_filter=True, language=lang)
    segs = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments]
    cache.write_text(json.dumps(segs, ensure_ascii=False, indent=1))
    return segs


def pick_with_llm(segs, n, lo, hi):
    import anthropic
    client = anthropic.Anthropic()
    transcript = "\n".join(
        f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segs
    )[:150000]
    prompt = f"""Berikut transkrip video dengan timestamp (detik).
Pilih {n} momen terbaik untuk dijadikan klip pendek viral (TikTok/Reels/Shorts).
Kriteria: hook kuat di 3 detik pertama, punya konteks sendiri, emosional/lucu/mengejutkan/berisi insight, selesai di titik natural.
Durasi tiap klip antara {lo} sampai {hi} detik. Klip tidak boleh tumpang tindih.

Balas HANYA dengan JSON array, format:
[{{"start": 12.0, "end": 55.0, "title": "judul singkat", "reason": "kenapa menarik"}}]

TRANSKRIP:
{transcript}"""
    msg = client.messages.create(
        model=MODEL, max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text
    match = re.search(r"\[.*\]", text, re.S)
    return json.loads(match.group(0))


def pick_heuristic(segs, n, lo, hi):
    """Fallback tanpa AI: pilih jendela waktu dengan kepadatan bicara & tanda seru/tanya tertinggi."""
    cands = []
    for i, s in enumerate(segs):
        start = s["start"]
        words, bonus, end = 0, 0, s["end"]
        for t in segs[i:]:
            if t["end"] - start > hi:
                break
            words += len(t["text"].split())
            bonus += t["text"].count("?") * 3 + t["text"].count("!") * 3
            end = t["end"]
        dur = end - start
        if dur >= lo and dur > 0:
            cands.append({"start": start, "end": end,
                          "score": words / dur + bonus / 10})
    cands.sort(key=lambda c: c["score"], reverse=True)
    chosen = []
    for c in cands:
        if all(c["end"] <= x["start"] or c["start"] >= x["end"] for x in chosen):
            chosen.append(c)
        if len(chosen) == n:
            break
    return [{"start": c["start"], "end": c["end"],
             "title": f"clip {i+1}", "reason": "heuristik"} for i, c in enumerate(chosen)]


def fmt_ts(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02}:{int(m):02}:{int(s):02},{int((s % 1) * 1000):03}"


def write_srt(segs, start, end, path):
    lines, k = [], 1
    for s in segs:
        if s["end"] <= start or s["start"] >= end:
            continue
        a = max(s["start"], start) - start
        b = min(s["end"], end) - start
        lines.append(f"{k}\n{fmt_ts(a)} --> {fmt_ts(b)}\n{s['text']}\n")
        k += 1
    path.write_text("\n".join(lines), encoding="utf-8")


def make_clip(video, segs, pick, out_dir, idx):
    start, end = float(pick["start"]), float(pick["end"])
    srt = out_dir / f"clip{idx}.srt"
    write_srt(segs, start, end, srt)
    # FontSize 24 lebih pas untuk kanvas 1080x1920 (12 terlalu kecil).
    vf = (
        "crop=ih*9/16:ih,scale=1080:1920,"
        f"subtitles={srt.name}:force_style='Alignment=2,FontSize=24,Bold=1,Outline=2,MarginV=200'"
    )
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", str(start), "-i", str(video), "-t", str(end - start),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "160k",
        f"clip{idx}.mp4",
    ], cwd=out_dir)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="URL video atau path file lokal")
    ap.add_argument("--clips", type=int, default=5)
    ap.add_argument("--min", type=int, default=25, dest="lo")
    ap.add_argument("--max", type=int, default=55, dest="hi")
    ap.add_argument("--model", default="small", help="tiny/base/small/medium")
    ap.add_argument("--lang", default=None, help="mis. id, en (kosong = auto)")
    ap.add_argument("--out", default="output")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    video = get_video(args.source, out)
    segs = transcribe(video, out, args.model, args.lang)

    print("[3/4] Memilih highlight...")
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            picks = pick_with_llm(segs, args.clips, args.lo, args.hi)
        except Exception as e:
            print(f"  AI gagal ({e}), pakai heuristik.")
            picks = pick_heuristic(segs, args.clips, args.lo, args.hi)
    else:
        print("  ANTHROPIC_API_KEY tidak ada, pakai heuristik.")
        picks = pick_heuristic(segs, args.clips, args.lo, args.hi)

    if len(picks) < args.clips:
        print(f"  ! hanya dapat {len(picks)} klip dari {args.clips} yang diminta "
              f"(coba turunkan --min atau naikkan --max).")

    (out / "picks.json").write_text(json.dumps(picks, ensure_ascii=False, indent=1))

    print("[4/4] Render klip...")
    ok, fail = 0, 0
    for i, p in enumerate(picks, 1):
        print(f"  clip{i}: {p['start']:.0f}s-{p['end']:.0f}s  {p.get('title','')}")
        try:
            make_clip(video, segs, p, out, i)
            ok += 1
        except subprocess.CalledProcessError as e:
            print(f"    clip{i} GAGAL: {e}")
            fail += 1

    print(f"\nSelesai. Berhasil: {ok}, gagal: {fail}. Hasil ada di folder: {out.resolve()}")


if __name__ == "__main__":
    sys.exit(main())

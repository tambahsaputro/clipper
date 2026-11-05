#!/usr/bin/env python3
"""editor.py - Fitur edit video pendek: text overlay, efek, auto subtitle."""
import subprocess
from pathlib import Path
import clipper


def _run_tolerant(cmd, cwd=None, expect_path=None):
    """Run ffmpeg, tolerate SIGABRT at cleanup if output file is valid.

    Build ffmpeg ini crash di src/fftools/ffmpeg_dec.c:597 saat -frames:v 1
    (stop mid-stream), tapi file output sudah ditulis sebelum crash.
    """
    res = subprocess.run(cmd, cwd=cwd, capture_output=True)
    if res.returncode == 0:
        return
    target = expect_path
    if target is None and cmd:
        target = (Path(cwd) / cmd[-1]) if cwd else Path(cmd[-1])
    if res.returncode == -6 and target and target.exists() and target.stat().st_size > 0:
        print(f"  [ffmpeg crash at cleanup tolerated - output OK: {target.name}]")
        return
    err = res.stderr.decode(errors="replace")[-800:]
    raise RuntimeError(f"ffmpeg exit {res.returncode}: {err}")


def font_path():
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]:
        if Path(p).exists():
            return p
    try:
        out = subprocess.check_output(
            ["fc-match", "-f", "%{file}", "sans:bold"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        if out and Path(out).exists():
            return out
    except Exception:
        pass
    return None


def color_hex(c):
    c = (c or "white").strip()
    return ("0x" + c[1:]) if c.startswith("#") else c


def preview_frame(src, params, out_dir, time=1.0, name="preview.png"):
    """Extract 1 frame at `time` with visual filters + watermark applied."""
    src = Path(src).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    aspect = params.get("aspect", "9:16")
    aw, ah = {"9:16": (1080, 1920), "1:1": (1080, 1080),
              "16:9": (1920, 1080)}.get(aspect, (1080, 1920))

    filters = [
        f"scale={aw}:{ah}:force_original_aspect_ratio=increase",
        f"crop={aw}:{ah}",
    ]
    cg = params.get("color_grade", "none")
    if cg == "warm":
        filters += ["eq=contrast=1.05:saturation=1.15:brightness=0.02",
                    "colorbalance=rs=0.08:gs=0.02:bs=-0.08"]
    elif cg == "cool":
        filters += ["eq=contrast=1.05:saturation=1.1:brightness=-0.01",
                    "colorbalance=rs=-0.08:gs=0:bs=0.1"]
    elif cg == "vintage":
        filters += ["eq=contrast=1.1:saturation=0.75:brightness=0.03:gamma=1.1",
                    "colorbalance=rs=0.06:gs=0:bs=-0.06"]
    elif cg == "bw":
        filters += ["hue=s=0", "eq=contrast=1.1"]

    if params.get("sharpen"):
        filters.append("unsharp=5:5:0.8:5:5:0.0")
    if params.get("vignette"):
        filters.append("vignette=PI/5")

    for i, t in enumerate(params.get("text_overlays", [])):
        text = str(t.get("text", "")).strip()
        if not text:
            continue
        start = float(t.get("start", 0))
        end = float(t.get("end", 3))
        if not (start <= time <= end):
            continue
        font = font_path()
        if not font:
            break
        txt_file = out_dir / f"prev_txt_{i}.txt"
        txt_file.write_text(text, encoding="utf-8")
        size = int(t.get("size", 60))
        color = color_hex(t.get("color", "#FFFFFF"))
        pos = t.get("position", "bottom")
        x = "(w-text_w)/2"
        if pos == "top":
            y = "h*0.1"
        elif pos == "center":
            y = "(h-text_h)/2"
        else:
            y = "h*0.82-text_h"
        filters.append(
            f"drawtext=fontfile='{font}'"
            f":textfile='{txt_file.name}'"
            f":expansion=none"
            f":fontcolor={color}"
            f":fontsize={size}"
            f":x={x}:y={y}"
            f":box=1:boxcolor=0x000000@0.45:boxborderw=14"
        )

    out_file = out_dir / name
    wm = params.get("watermark")
    # Step 1: extract frame (SIGABRT at cleanup tolerated via _run_tolerant)
    tmp_frame = out_dir / f"_tmp_{name}"
    _run_tolerant(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-ss", str(time), "-i", str(src),
         "-an",
         "-vf", ",".join(filters),
         "-frames:v", "1", "-update", "1",
         tmp_frame.name],
        cwd=out_dir,
        expect_path=tmp_frame,
    )

    if wm:
        # Step 2: composite watermark (image -> image, stable)
        wm_size = int(params.get("watermark_size", 200))
        wm_pos = params.get("watermark_position", "top-right")
        pos_map = {
            "top-left": "20:20",
            "top-right": "W-w-20:20",
            "bottom-left": "20:H-h-20",
            "bottom-right": "W-w-20:H-h-20",
        }
        xy = pos_map.get(wm_pos, "W-w-20:20")
        _run_tolerant(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-i", tmp_frame.name,
             "-i", str(wm),
             "-filter_complex",
             f"[1:v]scale={wm_size}:-1[wm];[0:v][wm]overlay={xy}:format=auto",
             "-frames:v", "1", "-update", "1",
             out_file.name],
            cwd=out_dir,
            expect_path=out_file,
        )
        tmp_frame.unlink(missing_ok=True)
    else:
        tmp_frame.rename(out_file)

    return out_file


def edit_video(src, params, out_dir, on_progress=None):
    def step(msg):
        if on_progress:
            on_progress(msg)

    src = Path(src).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    aspect = params.get("aspect", "9:16")
    aw, ah = {"9:16": (1080, 1920), "1:1": (1080, 1080),
              "16:9": (1920, 1080)}.get(aspect, (1080, 1920))

    kb = params.get("ken_burns", "none")
    if kb != "none":
        w2, h2 = aw * 2, ah * 2
        filters = [
            f"scale={w2}:{h2}:force_original_aspect_ratio=increase",
            f"crop={w2}:{h2}",
        ]
        if kb == "zoom-in":
            zexpr = "min(1+on/500,1.5)"
        elif kb == "zoom-out":
            zexpr = "max(1.5-on/500,1.0)"
        else:  # pan-right
            zexpr = "1.3"
        if kb == "pan-right":
            xexpr = "iw/2-(iw/zoom/2)+t*50"
            yexpr = "ih/2-(ih/zoom/2)"
        else:
            xexpr = "iw/2-(iw/zoom/2)"
            yexpr = "ih/2-(ih/zoom/2)"
        filters.append(
            f"zoompan=z='{zexpr}':x='{xexpr}':y='{yexpr}':d=1:s={aw}x{ah}:fps=30"
        )
    else:
        filters = [
            f"scale={aw}:{ah}:force_original_aspect_ratio=increase",
            f"crop={aw}:{ah}",
        ]

    cg = params.get("color_grade", "none")
    if cg == "warm":
        filters += ["eq=contrast=1.05:saturation=1.15:brightness=0.02",
                    "colorbalance=rs=0.08:gs=0.02:bs=-0.08"]
    elif cg == "cool":
        filters += ["eq=contrast=1.05:saturation=1.1:brightness=-0.01",
                    "colorbalance=rs=-0.08:gs=0:bs=0.1"]
    elif cg == "vintage":
        filters += ["eq=contrast=1.1:saturation=0.75:brightness=0.03:gamma=1.1",
                    "colorbalance=rs=0.06:gs=0:bs=-0.06"]
    elif cg == "bw":
        filters += ["hue=s=0", "eq=contrast=1.1"]

    if params.get("sharpen"):
        filters.append("unsharp=5:5:0.8:5:5:0.0")
    if params.get("vignette"):
        filters.append("vignette=PI/5")

    for i, t in enumerate(params.get("text_overlays", [])):
        text = str(t.get("text", "")).strip()
        if not text:
            continue
        font = font_path()
        if not font:
            step("Font tidak ditemukan; text overlay dilewati.")
            break
        txt_file = out_dir / f"txt_overlay_{i}.txt"
        txt_file.write_text(text, encoding="utf-8")
        start = float(t.get("start", 0))
        end = float(t.get("end", 3))
        size = int(t.get("size", 60))
        color = color_hex(t.get("color", "#FFFFFF"))
        pos = t.get("position", "bottom")
        x = "(w-text_w)/2"
        if pos == "top":
            y = "h*0.1"
        elif pos == "center":
            y = "(h-text_h)/2"
        else:
            y = "h*0.82-text_h"
        filters.append(
            f"drawtext=fontfile='{font}'"
            f":textfile='{txt_file.name}'"
            f":expansion=none"
            f":fontcolor={color}"
            f":fontsize={size}"
            f":x={x}:y={y}"
            f":box=1:boxcolor=0x000000@0.45:boxborderw=14"
            f":enable='between(t,{start:.2f},{end:.2f})'"
        )

    if params.get("auto_subtitle"):
        step("Membuat transkrip untuk subtitle...")
        try:
            segs = clipper.transcribe(src, out_dir,
                                      params.get("model", "base"),
                                      params.get("lang", "id"))
            srt = out_dir / "subtitle.srt"
            clipper.write_srt(segs, 0, 1e9, srt)
            filters.append(
                f"subtitles={srt.name}:force_style="
                f"'Alignment=2,FontSize=24,Bold=1,Outline=2,MarginV=200'"
            )
        except Exception as e:
            print(f"  Subtitle gagal: {e}")

    bgm = params.get("bgm")
    bgm_vol = float(params.get("bgm_volume", 0.3))
    watermark = params.get("watermark")
    wm_pos = params.get("watermark_position", "top-right")
    wm_size = int(params.get("watermark_size", 200))

    if not bgm and not watermark:
        step("Merender video...")
        out_file = out_dir / "edited.mp4"
        clipper.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(src),
            "-vf", ",".join(filters),
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart",
            out_file.name,
        ], cwd=out_dir)
        return out_file

    step("Merender video (BGM/watermark)...")
    inputs = ["-i", str(src)]
    next_idx = 1
    bgm_idx = None
    wm_idx = None
    if bgm:
        inputs += ["-i", str(bgm)]
        bgm_idx = next_idx
        next_idx += 1
    if watermark:
        inputs += ["-i", str(watermark)]
        wm_idx = next_idx
        next_idx += 1
    fc = [f"[0:v]{','.join(filters)}[vbase]"]
    v_label = "vbase"
    if wm_idx is not None:
        fc.append(f"[{wm_idx}:v]scale={wm_size}:-1[wm]")
        pos_map = {
            "top-left": "20:20",
            "top-right": "W-w-20:20",
            "bottom-left": "20:H-h-20",
            "bottom-right": "W-w-20:H-h-20",
        }
        xy = pos_map.get(wm_pos, "W-w-20:20")
        fc.append(f"[{v_label}][wm]overlay={xy}[vout]")
        v_label = "vout"
    a_label = "0:a"
    if bgm_idx is not None:
        fc.append(f"[{bgm_idx}:a]volume={bgm_vol}[abgm]")
        fc.append(f"[0:a][abgm]amix=inputs=2:duration=first:dropout_transition=0[aout]")
        a_label = "aout"
    out_file = out_dir / "edited.mp4"
    clipper.run(
        ["ffmpeg", "-y", "-loglevel", "error"] + inputs + [
            "-filter_complex", ";".join(fc),
            "-map", f"[{v_label}]", "-map", f"[{a_label}]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", "-shortest",
            out_file.name,
        ],
        cwd=out_dir,
    )
    return out_file

"""Video remix with ffmpeg.

Every upload gets a small, near-invisible letter pushed to the edge of the frame.
Generation 0 is otherwise the original video; each later generation (after the video
"burns out") adds a slightly stronger change: zoom, speed, trimmed start, colour shift.
"""
import os
import random
import shutil
import string
import subprocess

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def _font():
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return f.replace(":", "\\:")
    raise RuntimeError("no font found for ffmpeg drawtext")


def _has_audio(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
         "-of", "csv=p=0", path], capture_output=True, text=True)
    return bool(out.stdout.strip())


def _size(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0:s=x", path], capture_output=True, text=True, check=True)
    return out.stdout.strip().splitlines()[0].replace("x", ":")


def remix(src, dst, generation=0, rng=None):
    rng = rng or random.Random()
    gen = max(0, generation)

    letter = rng.choice(string.ascii_letters)
    # Letter sits at a random spot along the left or right edge, mostly outside the frame.
    side_x = rng.choice(["-tw*0.6", "w-tw*0.4"])
    y = f"h*{rng.uniform(0.15, 0.85):.3f}"
    alpha = rng.uniform(0.25, 0.4)

    vf = []
    af = []
    trim = 0.0
    if gen >= 1:
        zoom = 1 + 0.04 * gen
        speed = 1 + 0.03 * gen
        trim = min(0.25 * gen, 1.0)
        vf += [
            f"scale=iw*{zoom:.3f}:ih*{zoom:.3f}",
            f"crop=iw/{zoom:.3f}:ih/{zoom:.3f}",
            f"scale={_size(src)}",
            f"eq=brightness={0.015 * gen:.3f}:saturation={1 + 0.04 * gen:.3f}",
            f"setpts=PTS/{speed:.3f}",
        ]
        af.append(f"atempo={speed:.3f}")
    vf += [
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        f"drawtext=fontfile='{_font()}':text='{letter}':fontsize=h*0.03:"
        f"fontcolor=white@{alpha:.2f}:x={side_x}:y={y}",
    ]

    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if trim:
        cmd += ["-ss", f"{trim:.2f}"]
    cmd += ["-i", src, "-vf", ",".join(vf)]
    if _has_audio(src):
        if af:
            cmd += ["-af", ",".join(af)]
        cmd += ["-c:a", "aac", "-b:a", "160k"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-r", "30", "-movflags", "+faststart", dst]
    subprocess.run(cmd, check=True)
    return {"letter": letter, "generation": gen}


if __name__ == "__main__":
    import sys
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found")
    print(remix(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 0))

"""Optional ffmpeg support: convert videos the browser can't play, and save rotated/cropped copies.

ffmpeg is found on PATH, or via the `imageio-ffmpeg` package (pip install imageio-ffmpeg),
which bundles a static build. Without it, videos still play when the browser supports the codec.
"""
import os
import shutil
import subprocess
import threading

from library import DATA

PLAYABLE = DATA / "video"
_jobs = {}            # key -> {"state": "running"|"done"|"error", "progress": 0..1, "error": str, "path": str}
_lock = threading.Lock()
_ffmpeg = None


def ffmpeg_exe():
    global _ffmpeg
    if _ffmpeg is None:
        exe = shutil.which("ffmpeg")
        if not exe:
            try:
                import imageio_ffmpeg
                exe = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                exe = ""
        _ffmpeg = exe
    return _ffmpeg or None


def playable_path(mid):
    return PLAYABLE / f"{mid}.mp4"


def job(key):
    with _lock:
        j = _jobs.get(key)
        return dict(j) if j else None


def _run(key, args, out, duration):
    """Run ffmpeg writing to a temp file, tracking progress; rename to `out` when done."""
    tmp = f"{out}.part"  # not a video extension, so the folder scanner ignores it
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           *args, "-f", "mp4", "-progress", "pipe:1", tmp]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in p.stdout:
            if line.startswith("out_time_us=") and duration:
                try:
                    t = int(line.split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                with _lock:
                    _jobs[key]["progress"] = max(0.0, min(0.99, t / duration))
        err = p.stderr.read()
        if p.wait() != 0:
            raise RuntimeError(err.strip().splitlines()[-1] if err.strip() else f"ffmpeg exited {p.returncode}")
        os.replace(tmp, out)
        with _lock:
            _jobs[key].update(state="done", progress=1.0, path=str(out))
    except Exception as e:
        print(f"[video] {key}: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        with _lock:
            _jobs[key].update(state="error", error=str(e))


def _start(key, args, out, src):
    if not ffmpeg_exe():
        raise RuntimeError("ffmpeg is not available. Install it with: pip install imageio-ffmpeg")
    with _lock:
        j = _jobs.get(key)
        if j and j["state"] == "running":
            return
        _jobs[key] = {"state": "running", "progress": 0.0, "error": None, "path": None}
    threading.Thread(target=_run, args=(key, args, out, _duration(src)), daemon=True).start()


def _duration(path):
    import cv2
    cap = cv2.VideoCapture(path)
    try:
        fps, n = cap.get(cv2.CAP_PROP_FPS), cap.get(cv2.CAP_PROP_FRAME_COUNT)
        return n / fps if fps and n and n > 0 else 0
    finally:
        cap.release()


_H264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]


def start_playable(media):
    """Convert to browser-friendly H.264/AAC MP4 (cached by media id). Edits are not baked in."""
    out = playable_path(media["id"])
    key = f"play:{media['id']}"
    if out.exists():
        with _lock:
            _jobs[key] = {"state": "done", "progress": 1.0, "error": None, "path": str(out)}
        return key
    PLAYABLE.mkdir(parents=True, exist_ok=True)
    _start(key, ["-i", media["path"], "-map", "0:v:0", "-map", "0:a:0?", *_H264], out, media["path"])
    return key


def edit_filter(rotation, crop):
    """ffmpeg -vf chain for clockwise quarter turns, then a crop in rotated, normalized coords."""
    f = {1: ["transpose=clock"], 2: ["hflip", "vflip"], 3: ["transpose=cclock"]}.get(rotation % 4, [])
    if crop:
        x, y, w, h = crop
        f.append(f"crop=trunc(iw*{w}/2)*2:trunc(ih*{h}/2)*2:trunc(iw*{x}):trunc(ih*{y})")
    f.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    return ",".join(f)


def start_export(media, crop):
    """Write <name>_edited.mp4 next to the original with rotation/crop applied."""
    stem, _ = os.path.splitext(media["path"])
    n, out = 1, f"{stem}_edited.mp4"
    while os.path.exists(out):
        n += 1
        out = f"{stem}_edited{n}.mp4"
    key = f"export:{media['id']}"
    _start(key, ["-i", media["path"], "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "0",
                 "-vf", edit_filter(media["rotation"], crop), "-metadata:s:v:0", "rotate=0", *_H264],
           out, media["path"])
    return key

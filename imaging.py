"""Image decoding, thumbnails, edit rendering and export."""
import os

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageOps  # noqa: E402

Image.MAX_IMAGE_PIXELS = 300_000_000
DETECT_SIDE = 1600

# rotation = number of 90-degree clockwise turns
_ROT = {1: Image.Transpose.ROTATE_270, 2: Image.Transpose.ROTATE_180, 3: Image.Transpose.ROTATE_90}


def load_oriented(path, max_side=None):
    """Open an image with EXIF orientation applied, optionally downscaled cheaply."""
    im = Image.open(path)
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
    if max_side:
        im.draft("RGB", (max_side, max_side))
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        im = im.convert("RGB")
    if max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return im


def load_for_detection(path):
    """Return (bgr array at <=1600px, full width, full height, exif) with orientation applied."""
    with Image.open(path) as raw:
        W, H = raw.size
        exif = raw.getexif()
        if exif.get(274, 1) in (5, 6, 7, 8):
            W, H = H, W
    im = load_oriented(path, DETECT_SIDE)
    bgr = np.ascontiguousarray(np.asarray(im)[:, :, ::-1])
    return bgr, W, H, exif


def video_frame(path):
    cap = cv2.VideoCapture(path)
    try:
        n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        if n > 10:
            cap.set(cv2.CAP_PROP_POS_FRAMES, min(30, int(n // 3)))
        ok, frame = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
        if not ok:
            raise IOError("cannot decode video")
        return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    finally:
        cap.release()


def _frame_at(cap, t):
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000)
    ok, frame = cap.read()
    return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)) if ok else None


def video_frame_at(path, t, max_side=DETECT_SIDE):
    """The frame at `t` seconds (orientation applied by OpenCV), downscaled to max_side."""
    cap = cv2.VideoCapture(path)
    try:
        im = _frame_at(cap, t)
    finally:
        cap.release()
    if im is None:
        raise IOError(f"cannot decode video frame at {t:.2f}s")
    im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return im


def video_face_frames(path, max_side=DETECT_SIDE):
    """[(seconds, PIL image)] for face detection: one frame near the start and one near the end."""
    cap = cv2.VideoCapture(path)
    try:
        fps, n = cap.get(cv2.CAP_PROP_FPS), cap.get(cv2.CAP_PROP_FRAME_COUNT)
        dur = n / fps if fps and n and n > 0 else 0
        if dur < 2:
            times = [dur / 2]
        else:
            times = [min(0.5, dur * 0.1), dur - min(0.5, dur * 0.1)]
        out = []
        for i, t in enumerate(times):
            im = _frame_at(cap, t)
            if im is None and i:  # seeking near the end can fail; back off a little
                t = dur * 0.9
                im = _frame_at(cap, t)
            if im is not None:
                im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                out.append((round(t, 3), im))
        if not out:
            raise IOError("cannot decode video")
        return out
    finally:
        cap.release()


def load_face_source(path, frame=None):
    """The image faces were detected on: the photo, or the video frame at `frame` seconds."""
    return video_frame_at(path, frame) if frame is not None else load_oriented(path, DETECT_SIDE)


def video_info(path):
    """(width, height, creation timestamp or None) for a video."""
    cap = cv2.VideoCapture(path)
    try:
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        cap.release()
    return w, h, mp4_creation_time(path)


def mp4_creation_time(path):
    """Read creation_time from the mvhd box of a QuickTime/MP4 file (seconds since 1904, UTC)."""
    import struct
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            end = f.tell()
            pos = 0
            while pos + 8 <= end:                      # top-level boxes: find moov
                f.seek(pos)
                size, typ = struct.unpack(">I4s", f.read(8))
                hdr = 8
                if size == 1:
                    size, hdr = struct.unpack(">Q", f.read(8))[0], 16
                elif size == 0:
                    size = end - pos
                if size < hdr:
                    return None
                if typ == b"moov":
                    inner, stop = pos + hdr, pos + size
                    while inner + 8 <= stop:
                        f.seek(inner)
                        isize, ityp = struct.unpack(">I4s", f.read(8))
                        if ityp == b"mvhd":
                            version = f.read(1)[0]
                            f.read(3)
                            secs = struct.unpack(">Q" if version == 1 else ">I", f.read(8 if version == 1 else 4))[0]
                            ts = secs - 2082844800
                            return float(ts) if 315532800 < ts < 4102444800 else None  # 1980..2100
                        if isize < 8:
                            return None
                        inner += isize
                    return None
                pos += size
    except (OSError, struct.error, IndexError):
        return None
    return None


def cv2_gray(bgr):
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)


def dhash_gray(gray):
    """64-bit difference hash of a grayscale array: robust to resizing and re-compression."""
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA).astype(np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return int(np.packbits(bits).view(">u8")[0])


def dhash_pil(im):
    return dhash_gray(np.asarray(im.convert("L")))


def read_meta_date(path, kind):
    """(date stored in the file, its source) from the EXIF or QuickTime/MP4 header, or (None, None).
    Reads headers only. See library.parse_exif_date for the sources."""
    if kind == "video":
        ts = mp4_creation_time(path)
        return ts, "video" if ts else None
    from library import parse_exif_date
    with Image.open(path) as im:
        return parse_exif_date(im.getexif())


def apply_edits(im, rotation=0, crop=None):
    rotation = (rotation or 0) % 4
    if rotation:
        im = im.transpose(_ROT[rotation])
    if crop:
        x, y, w, h = crop
        W, H = im.size
        box = (round(x * W), round(y * H), round((x + w) * W), round((y + h) * H))
        if box[2] - box[0] >= 2 and box[3] - box[1] >= 2:
            im = im.crop(box)
    return im


def make_thumb(media, out_path, size=320):
    if media["kind"] == "video":
        im = video_frame(media["path"])
        im.thumbnail((size * 2, size * 2))
    else:
        im = load_oriented(media["path"], size * 2)
    import json
    crop = json.loads(media["crop"]) if media["crop"] else None
    im = apply_edits(im, media["rotation"], crop)
    im.thumbnail((size, size), Image.Resampling.LANCZOS)
    _save_atomic(im, out_path, quality=85)


def make_face_thumb(media_path, face, out_path, size=160):
    frame = face["frame"] if "frame" in face.keys() else None
    make_face_thumb_from(load_face_source(media_path, frame), face, out_path, size)


def make_face_thumb_from(im, face, out_path, size=160):
    W, H = im.size
    cx, cy = (face["x"] + face["w"] / 2) * W, (face["y"] + face["h"] / 2) * H
    side = max(face["w"] * W, face["h"] * H) * 1.5
    box = (int(cx - side / 2), int(cy - side / 2), int(cx + side / 2), int(cy + side / 2))
    _save_atomic(im.crop(box).resize((size, size), Image.Resampling.LANCZOS), out_path, quality=88)


def export_edited(media, crop):
    """Write an edited full-resolution copy next to the original. Returns the new path."""
    src = media["path"]
    with Image.open(src) as raw:
        exif = raw.getexif()
        icc = raw.info.get("icc_profile")
    im = load_oriented(src)
    im = apply_edits(im, media["rotation"], crop)
    stem, _ = os.path.splitext(src)
    n, out = 1, f"{stem}_edited.jpg"
    while os.path.exists(out):
        n += 1
        out = f"{stem}_edited{n}.jpg"
    exif[274] = 1  # pixels are already upright
    kwargs = {"quality": 93, "exif": exif.tobytes()}
    if icc:
        kwargs["icc_profile"] = icc
    im.save(out, "JPEG", **kwargs)
    return out


def render_shareable(media, out_path, max_side=None):
    """A JPEG of a photo with its rotation/crop applied, optionally shrunk (e.g. to email size)."""
    import json
    crop = json.loads(media["crop"]) if media["crop"] else None
    im = apply_edits(load_oriented(media["path"]), media["rotation"], crop)
    if max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    _save_atomic(im, out_path, quality=88)


def _save_atomic(im, out_path, quality):
    tmp = f"{out_path}.{os.getpid()}.{id(im)}.tmp"
    im.save(tmp, "JPEG", quality=quality)
    os.replace(tmp, out_path)

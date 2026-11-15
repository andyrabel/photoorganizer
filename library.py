"""Photo library: SQLite index, folder scanning and background face/metadata worker."""
import glob
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DB_PATH = DATA / "library.db"
THUMBS = DATA / "thumbs"
FACE_THUMBS = DATA / "faces"
CONFIG = DATA / "config.json"

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".3gp", ".avi", ".mkv", ".webm", ".wmv"}
SKIP_DIRS = {".picasaoriginals", "$recycle.bin", "@eadir", ".thumbnails"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS media(
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  folder TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  size INTEGER, mtime REAL, taken REAL,
  width INTEGER, height INTEGER,
  rotation INTEGER NOT NULL DEFAULT 0,
  crop TEXT,
  edit_ver INTEGER NOT NULL DEFAULT 0,
  scanned INTEGER NOT NULL DEFAULT 0,
  missing INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS media_folder ON media(folder);
CREATE INDEX IF NOT EXISTS media_scanned ON media(scanned);
CREATE TABLE IF NOT EXISTS people(
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL COLLATE NOCASE,
  cover_face INTEGER,
  created REAL);
CREATE TABLE IF NOT EXISTS faces(
  id INTEGER PRIMARY KEY,
  media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  x REAL, y REAL, w REAL, h REAL,
  size_px REAL, score REAL,
  embedding BLOB,
  person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
  ignored INTEGER NOT NULL DEFAULT 0,
  manual INTEGER NOT NULL DEFAULT 0,
  frame REAL);  -- seconds into the video the face was found at; NULL for photos
CREATE INDEX IF NOT EXISTS faces_media ON faces(media_id);
CREATE INDEX IF NOT EXISTS faces_person ON faces(person_id);
CREATE TABLE IF NOT EXISTS face_rejects(
  face_id INTEGER, person_id INTEGER, PRIMARY KEY(face_id, person_id));
CREATE TABLE IF NOT EXISTS clip(
  media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
  embedding BLOB);
"""

_local = threading.local()


def db():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


def init():
    for d in (DATA, THUMBS, FACE_THUMBS):
        d.mkdir(parents=True, exist_ok=True)
    conn = db()
    conn.executescript(SCHEMA)
    if "frame" not in {r["name"] for r in conn.execute("PRAGMA table_info(faces)")}:
        # videos used to be scanned for metadata only; scan them again for faces
        conn.execute("ALTER TABLE faces ADD COLUMN frame REAL")
        conn.execute("UPDATE media SET scanned=0 WHERE kind='video' AND scanned=1")


# ---------------------------------------------------------------- config / roots

def default_roots():
    roots = []
    for p in sorted(glob.glob("/mnt/c/Users/*/Pictures")):
        user = p.split("/")[4].lower()
        if user in ("public", "default", "default user", "all users") or user.startswith("defaultuser"):
            continue
        roots.append(p)
    home_pics = Path.home() / "Pictures"
    if home_pics.is_dir():
        roots.append(str(home_pics))
    return roots


def load_config():
    if CONFIG.exists():
        return json.loads(CONFIG.read_text())
    cfg = {"roots": default_roots()}
    save_config(cfg)
    return cfg


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, indent=2))


def normalize_path(p):
    """Accept Windows paths like C:\\Users\\me\\Pictures and map them into WSL."""
    p = p.strip().strip('"')
    if len(p) >= 2 and p[1] == ":":
        p = "/mnt/" + p[0].lower() + p[2:].replace("\\", "/")
    return os.path.normpath(os.path.expanduser(p))


# ---------------------------------------------------------------- indexing

class State:
    indexing = False
    indexed_at = 0.0
    priority_folder = None
    faces_version = 0          # bumps whenever face rows are added/removed
    assign_version = 0         # bumps whenever names change
    clip_version = 0           # bumps whenever describe-a-photo embeddings are added
    wake = threading.Event()
    clip_wake = threading.Event()


state = State()


def kind_of(name):
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    return None


def index_roots():
    """Walk all roots and sync the media table (fast: stat only)."""
    if state.indexing:
        return
    state.indexing = True
    try:
        roots = load_config()["roots"]
        conn = db()
        known = {r["path"]: (r["size"], r["mtime"], r["missing"])
                 for r in conn.execute("SELECT path,size,mtime,missing FROM media")}
        seen = set()
        new_rows, changed = [], []
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames
                               if not d.startswith(".") and d.lower() not in SKIP_DIRS]
                for fn in filenames:
                    k = kind_of(fn)
                    if not k or fn.startswith("._"):
                        continue
                    full = os.path.join(dirpath, fn)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    seen.add(full)
                    old = known.get(full)
                    if old is None:
                        new_rows.append((full, dirpath, fn, k, st.st_size, st.st_mtime, st.st_mtime))
                    elif old[0] != st.st_size or abs((old[1] or 0) - st.st_mtime) > 1 or old[2]:
                        changed.append((st.st_size, st.st_mtime, full))
                if len(new_rows) >= 500:
                    _flush(conn, new_rows)
        _flush(conn, new_rows)
        if changed:
            conn.execute("BEGIN")
            for size, mtime, path in changed:
                conn.execute("UPDATE media SET size=?, mtime=?, missing=0, scanned=0 WHERE path=?",
                             (size, mtime, path))
                conn.execute("DELETE FROM faces WHERE manual=0 AND person_id IS NULL AND media_id="
                             "(SELECT id FROM media WHERE path=?)", (path,))
                conn.execute("DELETE FROM clip WHERE media_id=(SELECT id FROM media WHERE path=?)", (path,))
            conn.execute("COMMIT")
            state.clip_version += 1
        gone = [p for p in known if p not in seen and not known[p][2]]
        if gone:
            conn.execute("BEGIN")
            conn.executemany("UPDATE media SET missing=1 WHERE path=?", [(p,) for p in gone])
            conn.execute("COMMIT")
        state.indexed_at = time.time()
        state.wake.set()
        state.clip_wake.set()
    finally:
        state.indexing = False


def _flush(conn, rows):
    if not rows:
        return
    conn.execute("BEGIN")
    conn.executemany(
        "INSERT OR IGNORE INTO media(path,folder,name,kind,size,mtime,taken) VALUES(?,?,?,?,?,?,?)", rows)
    conn.execute("COMMIT")
    rows.clear()


# ---------------------------------------------------------------- background worker

def parse_exif_date(exif):
    try:
        sub = exif.get_ifd(0x8769)
        val = sub.get(36867) or sub.get(36868) or exif.get(306)
        if val:
            return datetime.strptime(str(val).strip()[:19], "%Y:%m:%d %H:%M:%S").timestamp()
    except Exception:
        pass
    return None


def next_pending(conn, exclude=()):
    ex = (" AND id NOT IN (%s)" % ",".join("?" * len(exclude))) if exclude else ""
    pf = state.priority_folder
    if pf:
        row = conn.execute("SELECT id FROM media WHERE scanned=0 AND missing=0 AND folder=?" + ex + " LIMIT 1",
                           (pf, *exclude)).fetchone()
        if row:
            return row["id"]
    row = conn.execute("SELECT id FROM media WHERE scanned=0 AND missing=0" + ex +
                       " ORDER BY taken DESC LIMIT 1", tuple(exclude)).fetchone()
    return row["id"] if row else None


VIDEO_SAME_FACE = 0.5  # SFace cosine above which two faces in one video are the same person
_claim_lock = threading.Lock()
_claimed = set()


def process_media(mid, engine):
    """Read metadata + detect faces for one media row."""
    from PIL import Image
    from imaging import load_for_detection, video_face_frames, video_info
    conn = db()
    m = conn.execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()
    if m is None:
        return
    try:
        if m["kind"] == "video":
            full_w, full_h, taken = video_info(m["path"])
            # (seconds, bgr) for a frame near the start and one near the end
            frames = [(t, np.ascontiguousarray(np.asarray(im)[:, :, ::-1]))
                      for t, im in video_face_frames(m["path"])]
        else:
            bgr, full_w, full_h, exif = load_for_detection(m["path"])
            taken = parse_exif_date(exif) if exif is not None else None
            frames = [(None, bgr)]
        found, kept = [], []
        for t, bgr in frames:
            dh, dw = bgr.shape[:2]
            for (x, y, w, h, score, emb) in engine.detect(bgr):
                # the same person in both video frames: keep only the first sighting
                if t is not None and any(float(emb @ k) >= VIDEO_SAME_FACE for k in kept):
                    continue
                kept.append(emb)
                found.append((t, bgr, {"x": x / dw, "y": y / dh, "w": w / dw, "h": h / dh}, min(w, h), score, emb))
        conn.execute("BEGIN")
        conn.execute("UPDATE media SET width=?, height=?, taken=COALESCE(?, taken), scanned=1 WHERE id=?",
                     (full_w, full_h, taken, mid))
        new_faces = []
        for t, bgr, face, size, score, emb in found:
            cur = conn.execute(
                "INSERT INTO faces(media_id,x,y,w,h,size_px,score,embedding,frame) VALUES(?,?,?,?,?,?,?,?,?)",
                (mid, face["x"], face["y"], face["w"], face["h"], float(size), float(score),
                 emb.astype(np.float32).tobytes(), t))
            new_faces.append((cur.lastrowid, face, bgr))
        conn.execute("COMMIT")
        if new_faces:  # cut face thumbnails now, while the decoded image is in memory
            from imaging import make_face_thumb_from
            pils = {}
            for fid, face, bgr in new_faces:
                try:
                    pil = pils.get(id(bgr)) or pils.setdefault(id(bgr), Image.fromarray(np.ascontiguousarray(bgr[:, :, ::-1])))
                    make_face_thumb_from(pil, face, FACE_THUMBS / f"{fid}.jpg")
                except Exception:
                    pass
        if found:
            state.faces_version += 1
    except Exception as e:  # unreadable / corrupt file
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.execute("UPDATE media SET scanned=-1 WHERE id=?", (mid,))
        print(f"[scan] failed {m['path']}: {e}")


def worker_loop():
    from faces import FaceEngine
    engine = FaceEngine()
    conn = db()
    while True:
        with _claim_lock:
            mid = next_pending(conn, tuple(_claimed))
            if mid is not None:
                _claimed.add(mid)
        if mid is None:
            state.wake.wait(10)
            state.wake.clear()
            continue
        try:
            process_media(mid, engine)
        finally:
            with _claim_lock:
                _claimed.discard(mid)


def backfill_face_thumbs():
    """Cut any missing face thumbnails (one image decode per photo) so People views load fast."""
    from imaging import load_face_source, make_face_thumb_from
    rows = db().execute("""SELECT f.id, f.x, f.y, f.w, f.h, f.frame, f.media_id, m.path FROM faces f
                           JOIN media m ON m.id=f.media_id WHERE f.ignored=0
                           ORDER BY f.person_id IS NULL, f.media_id""").fetchall()
    by_media = {}
    for r in rows:
        if not (FACE_THUMBS / f"{r['id']}.jpg").exists():
            by_media.setdefault((r["media_id"], r["frame"]), []).append(r)
    for faces in by_media.values():
        try:
            im = load_face_source(faces[0]["path"], faces[0]["frame"])
            for f in faces:
                out = FACE_THUMBS / f"{f['id']}.jpg"
                if not out.exists():
                    make_face_thumb_from(im, f, out)
        except Exception as e:
            print(f"[faces] thumbnail backfill failed for {faces[0]['path']}: {e}")


def start_background(workers=2):
    threading.Thread(target=index_roots, daemon=True).start()
    threading.Thread(target=backfill_face_thumbs, daemon=True).start()
    for _ in range(workers):
        threading.Thread(target=worker_loop, daemon=True).start()
    import semantic
    semantic.start_background()


def status():
    conn = db()
    r = conn.execute("""SELECT
        SUM(missing=0) total,
        SUM(missing=0 AND kind='image') images,
        SUM(missing=0 AND scanned!=0) scanned,
        SUM(missing=0 AND kind='video') videos
        FROM media""").fetchone()
    faces = conn.execute("SELECT COUNT(*) c FROM faces WHERE ignored=0").fetchone()["c"]
    from semantic import progress
    return {"search": progress(), "indexing": state.indexing, "total": r["total"] or 0, "images": r["images"] or 0,
            "scanned": r["scanned"] or 0, "videos": r["videos"] or 0, "faces": faces,
            "roots": load_config()["roots"]}

"""Library tidy-up tools: photos filed in the wrong year folder, and duplicate files.

Both run as background jobs (they may read file headers or whole files the first time) and keep
their last result in memory. Moving and removing files hold library.fs_lock so the folder indexer
never sees a half-finished move.
"""
import hashlib
import os
import re
import shutil
import threading
import time
from collections import Counter
from datetime import datetime

import numpy as np

import library as lib
from library import THUMBS, db, root_of, signed64, state


# ---------------------------------------------------------------- jobs

class Job:
    """One background run at a time; progress and the last result are kept for polling."""

    def __init__(self, fn):
        self.fn = fn
        self.lock = threading.Lock()
        self.state, self.phase, self.done, self.total = "idle", "", 0, 0
        self.result, self.error, self.finished = None, None, None

    def start(self):
        with self.lock:
            if self.state == "running":
                return
            self.state, self.phase, self.done, self.total, self.error = "running", "Starting", 0, 0, None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            result = self.fn(self)
            with self.lock:
                self.result, self.state, self.finished = result, "done", time.time()
        except Exception as e:
            print(f"[tidy] job failed: {e}")
            with self.lock:
                self.state, self.error = "error", str(e)

    def step(self, phase, done, total):
        self.phase, self.done, self.total = phase, done, total

    def status(self):
        with self.lock:
            return {"state": self.state, "phase": self.phase, "done": self.done, "total": self.total,
                    "error": self.error, "finished": self.finished}


# ---------------------------------------------------------------- moving files

def _free_path(path):
    """`path`, or `name (2).ext`, `name (3).ext`, ... if something is already there."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


def _move_file(src, dest_dir):
    """Move a file into dest_dir (created if needed), never overwriting. Returns the new path."""
    os.makedirs(dest_dir, exist_ok=True)
    dest = _free_path(os.path.join(dest_dir, os.path.basename(src)))
    try:
        os.rename(src, dest)
    except OSError:  # different drive
        shutil.move(src, dest)
    return dest


def _remove_empty_dirs(folder, stop):
    """Remove `folder` and its parents while they are empty, never going above `stop`."""
    stop = os.path.normpath(stop)
    folder = os.path.normpath(folder)
    while folder != stop and folder.startswith(stop + os.sep):
        try:
            os.rmdir(folder)  # fails (harmlessly) unless empty
        except OSError:
            return
        folder = os.path.dirname(folder)


def _set_path(conn, mid, new_path):
    # a stale row for a file that used to be at new_path would block the UNIQUE path
    conn.execute("DELETE FROM media WHERE path=? AND missing=1 AND id!=?", (new_path, mid))
    conn.execute("UPDATE media SET path=?, folder=?, name=? WHERE id=?",
                 (new_path, os.path.dirname(new_path), os.path.basename(new_path), mid))


def _acquire_fs():
    if not lib.fs_lock.acquire(timeout=120):
        raise RuntimeError("The library is busy indexing folders. Try again in a minute.")


# ================================================================ wrong year folder

YEAR_SEG = re.compile(r"^(?:19|20)\d\d$")
MONTH_SEG = re.compile(r"^(?:0?[1-9]|1[0-2])$")
# 20180812, 2018-08-12, 2018_08_12 (IMG_20180812_..., PXL_2021..., IMG-20190812-WA0001, Screenshot 2020-01-02)
NAME_DATE = re.compile(r"(?<!\d)((?:19|20)\d\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")
YEAR_WORD = re.compile(r"(?<!\d)((?:19|20)\d\d)(?!\d)")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
MONTH_WORD = re.compile(r"\b(" + "|".join(MONTHS) + r")[a-z]*\b", re.I)

# How much each kind of evidence is trusted (0..1)
W_CAMERA = 0.9        # EXIF date taken
W_VIDEO = 0.8         # QuickTime/MP4 creation time (stored in UTC)
W_DEFAULT_CLOCK = 0.3  # a camera date that looks like an unset clock (1980, Jan 1 00:00, ...)
W_NEW_YEAR = 0.35     # within a day of New Year: time zones may legitimately put it in either year
W_FILE_NAME = 0.7
W_FOLDER_NAME = 0.6   # a year in an event folder's name, e.g. "Birthday Nov 2011"
W_SIBLINGS = 0.7      # (files with no date of their own) most other files in the same folder agree
W_MODIFIED = 0.2      # file modified time: often just the date it was copied, so it never suggests a year alone

HIGH, MEDIUM = 0.75, 0.5


def year_folder(rel):
    """(index of the deepest year segment, year) for a folder path relative to its root, or None."""
    parts = rel.split("/")
    for i in range(len(parts) - 1, -1, -1):
        if YEAR_SEG.match(parts[i]):
            return i, int(parts[i])
    return None


def _fmt(ts):
    d = datetime.fromtimestamp(ts)
    return f"{d.day} {d:%b %Y}"


def _evidence(m, rel_parts, yi, siblings):
    """[(year, month or None, weight, label)] for one media row inside a year folder.
    siblings: Counter of metadata years of the other files in the same folder."""
    now_year = datetime.now().year
    ok = lambda y: 1980 <= y <= now_year  # noqa: E731
    folder_year = int(rel_parts[yi])
    ev = []
    if m["meta_date"]:
        d = datetime.fromtimestamp(m["meta_date"])
        w = W_VIDEO if m["kind"] == "video" else W_CAMERA
        what = "Video recorded" if m["kind"] == "video" else "Camera date"
        note = ""
        if d.year < 1995 or (d.month == 1 and d.day == 1 and d.hour == 0 and d.minute < 5):
            w, note = W_DEFAULT_CLOCK, " (looks like the camera clock was not set)"
        elif abs(d.year - folder_year) == 1 and ((d.month == 12 and d.day == 31) or (d.month == 1 and d.day == 1)):
            w, note = W_NEW_YEAR, " (New Year, so the time zone may decide the year)"
        if ok(d.year):
            ev.append((d.year, d.month, w, f"{what}: {_fmt(m['meta_date'])}{note}"))
    nd = NAME_DATE.search(m["name"])
    if nd and ok(int(nd.group(1))):
        ev.append((int(nd.group(1)), int(nd.group(2)), W_FILE_NAME, f"Date in file name: {nd.group(0)}"))
    # event folders below the year (and month) folder, e.g. 2019/11/Birthday Nov 2011
    below = rel_parts[yi + 1:]
    if below and MONTH_SEG.match(below[0]):
        below = below[1:]
    for seg in reversed(below):
        years = {int(y) for y in YEAR_WORD.findall(seg) if ok(int(y))}
        if len(years) == 1:
            y = years.pop()
            mw = MONTH_WORD.search(seg)
            ev.append((y, MONTHS[mw.group(1).lower()[:3]] if mw else None, W_FOLDER_NAME,
                       f"Year in folder name: “{seg}”"))
            break
    if not m["meta_date"] and sum(siblings.values()) >= 3:
        top, n = siblings.most_common(1)[0]
        total = sum(siblings.values())
        if n / total >= 0.8:
            ev.append((top, None, W_SIBLINGS, f"{n} of {total} other dated files in this folder are from {top}"))
    if m["mtime"]:
        d = datetime.fromtimestamp(m["mtime"])
        if ok(d.year):
            ev.append((d.year, d.month, W_MODIFIED, f"File last modified: {_fmt(m['mtime'])}"))
    return ev


def _support(ev, year):
    """Combined belief that `year` is right, from independent pieces of evidence."""
    miss = 1.0
    for y, _, w, _ in ev:
        if y == year:
            miss *= 1 - w
    return 1 - miss


def folder_years(conn, folder):
    """Counter of metadata years of the files in one folder."""
    return Counter(datetime.fromtimestamp(r[0]).year for r in conn.execute(
        "SELECT meta_date FROM media WHERE folder=? AND missing=0 AND meta_date IS NOT NULL", (folder,)))


def analyze(m, roots, folder_counts=None):
    """The year-check verdict for one media row, or None if it is not in a year folder or looks right.
    folder_counts: folder_years() of the row's folder (looked up if not given)."""
    root = root_of(m["folder"], roots)
    if not root:
        return None
    rel = os.path.relpath(m["folder"], root)
    yf = year_folder(rel) if rel != "." else None
    if not yf:
        return None
    yi, folder_year = yf
    parts = rel.split("/")
    counts = Counter(folder_counts if folder_counts is not None else folder_years(db(), m["folder"]))
    own = datetime.fromtimestamp(m["meta_date"]).year if m["meta_date"] else None
    if own is not None and counts[own] > 0:
        counts[own] -= 1  # the other files, not this one
    ev = _evidence(m, parts, yi, counts)
    if not ev:
        return None
    scores = {y: _support(ev, y) for y in {e[0] for e in ev}}
    # best year; ties go to the year backed by the strongest single piece of evidence
    best = max(scores, key=lambda y: (scores[y], max(e[2] for e in ev if e[0] == y)))
    if best == folder_year or all(e[2] == W_MODIFIED for e in ev if e[0] == best):
        return None
    confidence = scores[best] * (1 - scores.get(folder_year, 0.0))
    # month for year/month layouts: from the strongest evidence for that year that has one
    month = next((e[1] for e in sorted(ev, key=lambda e: -e[2]) if e[0] == best and e[1]), None)
    note = None
    if own is not None and sum(counts.values()) >= 3:
        top, n = counts.most_common(1)[0]
        if top != folder_year and n / sum(counts.values()) >= 0.8:
            note = f"Most other dated files in this folder ({n} of {sum(counts.values())}) are from {top}, " \
                   f"so the whole folder may be in the wrong place."
    return {
        "id": m["id"], "folder_year": folder_year, "note": note, "year": best, "confidence": round(confidence, 3),
        "level": "high" if confidence >= HIGH else "medium" if confidence >= MEDIUM else "low",
        "evidence": [{"label": e[3], "year": e[0], "weight": e[2], "agrees": e[0] == best}
                     for e in sorted(ev, key=lambda e: -e[2])],
        "target": target_folder(root, parts, yi, best, month),
        "rel": rel,
    }


def target_folder(root, parts, yi, year, month):
    """Swap the year folder (and a month folder right below it) for the suggested ones.
    Event folders below are kept: 2019/11/Birthday Nov 2011 -> 2011/11/Birthday Nov 2011."""
    new = parts[:yi] + [str(year)]
    rest = parts[yi + 1:]
    if rest and MONTH_SEG.match(rest[0]):
        old = rest[0]
        new.append(f"{month:0{len(old)}d}" if month else old)
        rest = rest[1:]
    return os.path.join(root, *new, *rest)


def _read_missing_meta(job, rows):
    """Read dates from the headers of files whose metadata has not been read yet."""
    from imaging import read_meta_date
    todo = [r for r in rows if not r["meta_read"]]
    conn = db()
    for i, r in enumerate(todo):
        if i % 20 == 0:
            job.step("Reading photo dates", i, len(todo))
        try:
            d = read_meta_date(r["path"], r["kind"])
        except Exception:
            d = None
        conn.execute("UPDATE media SET meta_date=?, meta_read=1 WHERE id=?", (d, r["id"]))
    return len(todo)


def run_year_check(job):
    roots = lib.load_config()["roots"]
    conn = db()

    def in_year_folders():
        return [r for r in conn.execute("SELECT id, path, folder, name, kind, mtime, meta_date, meta_read "
                                        "FROM media WHERE missing=0")
                if (rt := root_of(r["folder"], roots)) and year_folder(os.path.relpath(r["folder"], rt))]
    rows = in_year_folders()
    if _read_missing_meta(job, rows):
        rows = in_year_folders()
    job.step("Comparing dates with folders", 0, len(rows))
    by_folder = {}
    for r in rows:
        if r["meta_date"]:
            by_folder.setdefault(r["folder"], Counter())[datetime.fromtimestamp(r["meta_date"]).year] += 1
    found = [a for r in rows if (a := analyze(r, roots, by_folder.get(r["folder"], Counter())))]
    found.sort(key=lambda a: (-a["confidence"], a["rel"]))
    return {"items": found, "checked": len(rows)}


def move_to_year(ids):
    """Move each media file to its suggested year folder. Returns [{id, ok, path|error}]."""
    roots = lib.load_config()["roots"]
    conn = db()
    out = []
    _acquire_fs()
    try:
        for mid in ids:
            m = conn.execute("SELECT * FROM media WHERE id=? AND missing=0", (mid,)).fetchone()
            a = analyze(m, roots) if m else None
            if not a:
                out.append({"id": mid, "ok": False, "error": "No longer needs moving"})
                continue
            try:
                if not os.path.exists(m["path"]):
                    raise FileNotFoundError("The file is no longer there")
                new = _move_file(m["path"], a["target"])
                _set_path(conn, mid, new)
                # tidy up folders this emptied, but keep the year folder itself
                year_dir = os.path.join(root_of(m["folder"], roots), *a["rel"].split("/")[:year_folder(a["rel"])[0] + 1])
                _remove_empty_dirs(m["folder"], year_dir)
                out.append({"id": mid, "ok": True, "path": new})
            except Exception as e:
                out.append({"id": mid, "ok": False, "error": str(e)})
    finally:
        lib.fs_lock.release()
    _forget(year_job, {o["id"] for o in out if o["ok"]})
    return out


# ================================================================ duplicates

SIMILAR_BITS = 4       # dHash bits (of 64) that may differ for two files to count as look-alikes
ASPECT_TOLERANCE = 0.03
COPY_NAME = re.compile(r"(copy|kopie|\(\d+\)|_edited\d*|[-_ ]\d$)", re.I)


def _hash_file(path):
    h = hashlib.blake2b(digest_size=20)
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _dhash_for(r):
    from imaging import dhash_pil, load_oriented, video_frame
    thumb = THUMBS / f"{r['id']}_0.jpg"  # an unedited thumbnail is already on disk: much faster to read
    if thumb.exists():
        from PIL import Image
        with Image.open(thumb) as im:
            return dhash_pil(im)
    return dhash_pil(video_frame(r["path"]) if r["kind"] == "video" else load_oriented(r["path"], 256))


def _fill_hashes(job):
    conn = db()
    # look-alike hashes (photos get them during the face scan; this covers videos and unscanned files)
    todo = conn.execute("SELECT id, path, kind FROM media WHERE missing=0 AND dhash IS NULL").fetchall()
    for i, r in enumerate(todo):
        if i % 10 == 0:
            job.step("Fingerprinting photos and videos", i, len(todo))
        try:
            conn.execute("UPDATE media SET dhash=? WHERE id=?", (signed64(_dhash_for(r)), r["id"]))
        except Exception as e:
            print(f"[duplicates] cannot read {r['path']}: {e}")
    # exact copies must have the same size, so only files that share a size are read in full
    todo = conn.execute("""SELECT id, path FROM media WHERE missing=0 AND sha IS NULL AND size IN
                           (SELECT size FROM media WHERE missing=0 GROUP BY size HAVING COUNT(*) > 1)""").fetchall()
    for i, r in enumerate(todo):
        job.step("Comparing files byte by byte", i, len(todo))
        try:
            conn.execute("UPDATE media SET sha=? WHERE id=?", (_hash_file(r["path"]), r["id"]))
        except OSError as e:
            print(f"[duplicates] cannot read {r['path']}: {e}")


class _Groups:
    def __init__(self):
        self.parent = {}

    def find(self, a):
        self.parent.setdefault(a, a)
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)


def _similar_pairs(rows):
    """Pairs of rows (same kind) whose perceptual hashes differ by at most SIMILAR_BITS bits."""
    ids = np.array([r["id"] for r in rows], dtype=np.int64)
    hs = np.array([r["dhash"] for r in rows], dtype=np.int64).view(np.uint64)
    aspect = np.array([(r["width"] / r["height"]) if r["width"] and r["height"] else 0.0 for r in rows])
    block = max(8, 8_000_000 // max(1, len(rows)))  # bounds memory to ~64 MB per step
    for s in range(0, len(rows), block):
        d = np.bitwise_count(hs[s:s + block, None] ^ hs[None, s:])  # only pairs (i, j) with j >= s
        ii, jj = np.nonzero(d <= SIMILAR_BITS)
        ii, jj = ii + s, jj + s
        keep = jj > ii
        for i, j in zip(ii[keep], jj[keep]):
            a, b = aspect[i], aspect[j]
            if a and b and abs(a - b) / max(a, b) > ASPECT_TOLERANCE:
                continue  # same picture cropped differently, or a different shot
            yield int(ids[i]), int(ids[j])


def _keep_order(m, roots):
    """Sort key: the copy to keep comes first."""
    in_right_year = False
    rt = root_of(m["folder"], roots)
    yf = year_folder(os.path.relpath(m["folder"], rt)) if rt else None
    if yf and m["meta_date"]:
        in_right_year = datetime.fromtimestamp(m["meta_date"]).year == yf[1]
    return (-((m["width"] or 0) * (m["height"] or 0)),         # highest resolution
            bool(COPY_NAME.search(os.path.splitext(m["name"])[0])),  # not "photo (2).jpg" / "copy"
            not in_right_year,                                  # already in the right year folder
            -(m["size"] or 0),                                  # least compressed
            len(m["path"]), m["path"])


def run_duplicates(job):
    _fill_hashes(job)
    job.step("Grouping duplicates", 0, 0)
    roots = lib.load_config()["roots"]
    rows = db().execute("""SELECT id, path, folder, name, kind, size, width, height, meta_date, dhash, sha
                           FROM media WHERE missing=0""").fetchall()
    by_id = {r["id"]: r for r in rows}
    g = _Groups()
    by_sha = {}
    for r in rows:
        if r["sha"]:
            by_sha.setdefault(r["sha"], []).append(r["id"])
    for ids in by_sha.values():
        for other in ids[1:]:
            g.union(ids[0], other)
    for kind in ("image", "video"):
        hashed = [r for r in rows if r["kind"] == kind and r["dhash"] is not None]
        for a, b in _similar_pairs(hashed):
            g.union(a, b)
    groups = {}
    for mid in list(g.parent):
        groups.setdefault(g.find(mid), []).append(mid)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        ms = sorted((by_id[i] for i in members), key=lambda m: _keep_order(m, roots))
        exact = len({m["sha"] for m in ms}) == 1 and ms[0]["sha"] is not None
        out.append({
            "kind": "exact" if exact else "similar",
            "keep": ms[0]["id"],
            "items": [{"id": m["id"], "path": m["path"], "size": m["size"], "sha": m["sha"],
                       "same_as_keep": bool(m["sha"]) and m["sha"] == ms[0]["sha"]} for m in ms],
            "extra_bytes": sum(m["size"] or 0 for m in ms[1:]),
        })
    out.sort(key=lambda g: (g["kind"] != "exact", -g["extra_bytes"]))
    return {"groups": out}


def _trash_dir(root):
    return os.path.join(root, ".removed-duplicates")  # starts with "." so the indexer skips it


def remove_duplicates(ids):
    """Move duplicates into the hidden holding folder of their root. Refuses to remove every file of a group."""
    res = dup_job.result
    if not res:
        raise ValueError("Run Find duplicates first")
    ids = set(ids)
    group_of = {it["id"]: g for g in res["groups"] for it in g["items"]}
    for mid in ids:
        g = group_of.get(mid)
        if g is None:
            raise ValueError("That file is not in the current list of duplicates")
        if all(it["id"] in ids for it in g["items"]):
            raise ValueError("Keep at least one file from each group")
    roots = lib.load_config()["roots"]
    conn = db()
    out = []
    _acquire_fs()
    try:
        for mid in ids:
            m = conn.execute("SELECT * FROM media WHERE id=? AND missing=0", (mid,)).fetchone()
            root = root_of(m["folder"], roots) if m else ""
            if not root:
                out.append({"id": mid, "ok": False, "error": "File not found in the library"})
                continue
            try:
                rel = os.path.relpath(m["folder"], root)
                dest = _move_file(m["path"], os.path.normpath(os.path.join(_trash_dir(root), rel)))
                conn.execute("UPDATE media SET missing=1 WHERE id=?", (mid,))
                conn.execute("INSERT INTO removed(media_id, orig_path, trash_path, size, removed) VALUES(?,?,?,?,?)",
                             (mid, m["path"], dest, m["size"], time.time()))
                out.append({"id": mid, "ok": True})
            except Exception as e:
                out.append({"id": mid, "ok": False, "error": str(e)})
    finally:
        lib.fs_lock.release()
    _forget(dup_job, {o["id"] for o in out if o["ok"]})
    state.clip_version += 1
    return out


def removed_summary():
    r = db().execute("SELECT COUNT(*) n, COALESCE(SUM(size), 0) bytes FROM removed").fetchone()
    return {"count": r["n"], "bytes": r["bytes"]}


def restore_removed():
    """Put every removed duplicate back where it was."""
    conn = db()
    restored, failed = 0, []
    _acquire_fs()
    try:
        for r in conn.execute("SELECT * FROM removed").fetchall():
            if not os.path.exists(r["trash_path"]):
                conn.execute("DELETE FROM removed WHERE id=?", (r["id"],))
                continue
            if os.path.exists(r["orig_path"]):
                failed.append(f"{r['orig_path']} already exists")
                continue
            os.makedirs(os.path.dirname(r["orig_path"]), exist_ok=True)
            try:
                os.rename(r["trash_path"], r["orig_path"])
            except OSError:
                shutil.move(r["trash_path"], r["orig_path"])
            conn.execute("UPDATE media SET missing=0 WHERE id=? AND path=?", (r["media_id"], r["orig_path"]))
            conn.execute("DELETE FROM removed WHERE id=?", (r["id"],))
            _remove_empty_dirs(os.path.dirname(r["trash_path"]), os.path.dirname(_trash_root(r["trash_path"])))
            restored += 1
    finally:
        lib.fs_lock.release()
    state.clip_version += 1
    dup_job.result = None  # groups changed; the list must be rebuilt
    return {"restored": restored, "failed": failed}


def empty_removed():
    """Permanently delete every removed duplicate."""
    conn = db()
    deleted = 0
    for r in conn.execute("SELECT * FROM removed").fetchall():
        try:
            os.remove(r["trash_path"])
            deleted += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[duplicates] cannot delete {r['trash_path']}: {e}")
            continue
        conn.execute("DELETE FROM removed WHERE id=?", (r["id"],))
        conn.execute("DELETE FROM media WHERE id=? AND missing=1", (r["media_id"],))
        _remove_empty_dirs(os.path.dirname(r["trash_path"]), os.path.dirname(_trash_root(r["trash_path"])))
    state.faces_version += 1
    return {"deleted": deleted}


def _trash_root(trash_path):
    """The .removed-duplicates folder that holds trash_path (removed too once it is empty)."""
    head = trash_path
    while os.path.basename(head) != ".removed-duplicates" and head != os.path.dirname(head):
        head = os.path.dirname(head)
    return head


def _forget(job, ids):
    """Drop moved/removed files from a job's cached result so the lists stay accurate."""
    res = job.result
    if not res or not ids:
        return
    if "items" in res:
        res["items"] = [a for a in res["items"] if a["id"] not in ids]
    if "groups" in res:
        groups = []
        for g in res["groups"]:
            items = [it for it in g["items"] if it["id"] not in ids]
            if len(items) >= 2:
                keep = next((it for it in items if it["id"] == g["keep"]), items[0])
                groups.append(dict(g, items=items, keep=keep["id"],
                                   extra_bytes=sum(it["size"] or 0 for it in items) - (keep["size"] or 0)))
        res["groups"] = groups


year_job = Job(run_year_check)
dup_job = Job(run_duplicates)

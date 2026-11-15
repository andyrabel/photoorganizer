"""Library tidy-up tools: photos filed in the wrong year folder, and duplicate files (junk.py uses the holding area).

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
# 07-29-08_1828.jpg: month-day-year_time, as older phones named their pictures
NAME_DATE_MDY = re.compile(r"(?<!\d)(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])-(\d\d)_\d{4}(?!\d)")
# a year written as a word in a file name ("Emmas Bday Jun-1995", "headshot 2003"), not a counter like IMG_2005
NAME_YEAR = re.compile(r"(?:^|(?<=[\s(,-]))((?:19|20)\d\d)(?!\d)")
YEAR_WORD = re.compile(r"(?<!\d)((?:19|20)\d\d)(?!\d)")
DECADE_WORD = re.compile(r"(?<!\d)((?:19|20)\d)0'?s\b", re.I)  # "1980s"
MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
               "november", "december"]
MONTHS = {m[:3]: i + 1 for i, m in enumerate(MONTH_NAMES)}
# a month only counts when a year follows it ("Oct 2026", "Sept-2008"), so "May" or "Mar" inside other words do not
MONTH_YEAR = re.compile(r"\b(" + "|".join(MONTH_NAMES) + r"|sept|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)"
                        r"[\s,._-]*'?((?:19|20)\d\d)(?!\d)", re.I)

# How much each kind of evidence is trusted (0..1)
W_CAMERA = 0.9        # EXIF date taken
W_VIDEO = 0.8         # QuickTime/MP4 creation time (stored in UTC)
W_SAVED = 0.4         # EXIF "last saved" date only: often when the picture was edited or copied
W_SCANNED = 0.15      # a scanner's date, or a digital date on a pre-digital photo: when it was scanned, not taken
W_DEFAULT_CLOCK = 0.3  # a camera date that looks like an unset clock (1980, Jan 1 00:00, ...)
W_NEW_YEAR = 0.35     # within a day of New Year: time zones may legitimately put it in either year
W_FILE_NAME = 0.7
W_FILE_NAME_MDY = 0.6  # 07-29-08_1828: two-digit year
W_NAME_YEAR = 0.5     # a year written in the file name
W_FOLDER_NAME = 0.6   # a year in an event folder's name, e.g. "Birthday Nov 2011"
W_SIBLINGS = 0.7      # (files with no date of their own) most other files in the same folder agree
W_MODIFIED = 0.2      # file modified time: often just the date it was copied, so it never suggests a year alone

DIGITAL_ERA = 1998    # before this, a digital date ten or more years later is a scan of a print

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


def _now_year():
    return datetime.now().year


def _meta_ok(y):
    return 1980 <= y <= _now_year()


def _name_ok(y):
    return 1900 <= y <= _now_year()


def _own_evidence(m, era_year=None):
    """[(year, month or None, weight, label)] from the file itself: its metadata date and its name.
    era_year: the year the file is believed to be from (its year folder, or a decade in a folder name);
    a digital date long after a pre-digital year is treated as the date the print was scanned."""
    ev = []
    if m["meta_date"]:
        d = datetime.fromtimestamp(m["meta_date"])
        src = m["meta_src"] or ("video" if m["kind"] == "video" else "camera")
        w, what, note = {
            "camera": (W_CAMERA, "Camera date", ""),
            "screen": (W_CAMERA, "Screenshot taken", ""),
            "device": (W_VIDEO, "Date taken", ""),
            "video": (W_VIDEO, "Video recorded", ""),
            "saved": (W_SAVED, "Date the file was saved", " (may be when it was edited or copied)"),
            "scan": (W_SCANNED, "Scanner date", " (when it was scanned, not taken)"),
        }.get(src, (W_CAMERA, "Camera date", ""))
        if src in ("camera", "video", "device"):
            if d.year < 1995 or (d.month == 1 and d.day == 1 and d.hour == 0 and d.minute < 5):
                w, note = W_DEFAULT_CLOCK, " (looks like the camera clock was not set)"
            elif era_year and abs(d.year - era_year) == 1 and ((d.month == 12 and d.day == 31) or (d.month == 1 and d.day == 1)):
                w, note = W_NEW_YEAR, " (New Year, so the time zone may decide the year)"
        if era_year and era_year < DIGITAL_ERA and d.year >= era_year + 10 and w > W_SCANNED:
            w, note = W_SCANNED, f" (a digital date on a {era_year} photo: probably when the print was scanned or photographed)"
        if _meta_ok(d.year):
            ev.append((d.year, d.month, w, f"{what}: {_fmt(m['meta_date'])}{note}"))
    stem = os.path.splitext(m["name"])[0]
    nd = NAME_DATE.search(stem)
    md = NAME_DATE_MDY.search(stem)
    if nd and _meta_ok(int(nd.group(1))):
        ev.append((int(nd.group(1)), int(nd.group(2)), W_FILE_NAME, f"Date in file name: {nd.group(0)}"))
    elif md:
        yy = int(md.group(3))
        y = 2000 + yy if 2000 + yy <= _now_year() else 1900 + yy
        if _meta_ok(y):
            ev.append((y, int(md.group(1)), W_FILE_NAME_MDY, f"Date in file name: {md.group(0)[:8]}"))
    else:
        years = {int(y) for y in NAME_YEAR.findall(stem) if _name_ok(int(y))}
        if len(years) == 1:
            y = years.pop()
            mw = MONTH_YEAR.search(stem)
            ev.append((y, MONTHS[mw.group(1).lower()[:3]] if mw and int(mw.group(2)) == y else None, W_NAME_YEAR,
                       f"Year in file name: “{m['name']}”"))
    return ev


def _segment_year(seg):
    """(year, month or None) written in one folder name, if it names exactly one year."""
    years = {int(y) for y in YEAR_WORD.findall(seg) if _name_ok(int(y))}
    if len(years) != 1:
        return None
    y = years.pop()
    mw = MONTH_YEAR.search(seg)
    return y, (MONTHS[mw.group(1).lower()[:3]] if mw and int(mw.group(2)) == y else None)


def _evidence(m, rel_parts, yi, siblings):
    """[(year, month or None, weight, label)] for one media row inside a year folder.
    siblings: Counter of metadata years of the other files in the same folder."""
    folder_year = int(rel_parts[yi])
    ev = _own_evidence(m, folder_year)
    # event folders below the year (and month) folder, e.g. 2019/11/Birthday Nov 2011
    below = rel_parts[yi + 1:]
    if below and MONTH_SEG.match(below[0]):
        below = below[1:]
    for seg in reversed(below):
        sy = _segment_year(seg)
        if sy:
            ev.append((sy[0], sy[1], W_FOLDER_NAME, f"Year in folder name: “{seg}”"))
            break
    if not m["meta_date"] and sum(siblings.values()) >= 3:
        top, n = siblings.most_common(1)[0]
        total = sum(siblings.values())
        if n / total >= 0.8:
            ev.append((top, None, W_SIBLINGS, f"{n} of {total} other dated files in this folder are from {top}"))
    if m["mtime"]:
        d = datetime.fromtimestamp(m["mtime"])
        if _meta_ok(d.year):
            ev.append((d.year, d.month, W_MODIFIED, f"File last modified: {_fmt(m['mtime'])}"))
    return ev


def _support(ev, year):
    """Combined belief that `year` is right, from independent pieces of evidence."""
    miss = 1.0
    for y, _, w, _ in ev:
        if y == year:
            miss *= 1 - w
    return 1 - miss


def _level(confidence):
    return "high" if confidence >= HIGH else "medium" if confidence >= MEDIUM else "low"


def _strong_year(m):
    """The year the file's own (trustworthy) metadata gives, for counting what the rest of a folder says."""
    if m["meta_date"] and (m["meta_src"] or "camera") in ("camera", "video", "screen", "device"):
        return datetime.fromtimestamp(m["meta_date"]).year
    return None


def folder_years(conn, folder):
    """Counter of metadata years of the files in one folder."""
    return Counter(y for r in conn.execute(
        "SELECT meta_date, meta_src FROM media WHERE folder=? AND missing=0 AND meta_date IS NOT NULL", (folder,))
        if (y := _strong_year(r)))


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
    own = _strong_year(m)
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
        "level": _level(confidence),
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


# ---------------------------------------------------------------- whole folders

FOLDER_SAMPLE = 6  # thumbnails shown per folder suggestion


def _year_layouts(rows, roots):
    """{root: (base folder that holds the year folders, uses month folders?)} - the layout most files follow."""
    bases = {}
    for r in rows:
        rt = root_of(r["folder"], roots)
        rel = os.path.relpath(r["folder"], rt) if rt else "."
        yf = year_folder(rel) if rel != "." else None
        if not yf:
            continue
        parts = rel.split("/")
        base = os.path.join(rt, *parts[:yi]) if (yi := yf[0]) else rt
        monthly = len(parts) > yi + 1 and bool(MONTH_SEG.match(parts[yi + 1]))
        c = bases.setdefault(rt, {}).setdefault(base, [0, 0])
        c[0] += 1
        c[1] += monthly
    out = {}
    for rt, by_base in bases.items():
        base, (n, monthly) = max(by_base.items(), key=lambda kv: kv[1][0])
        out[rt] = (base, monthly > n / 2)
    return out


def _year_guess(files, name_ev, era=None):
    """Best (year, month, confidence, evidence labels, years seen) for a set of files that belong together.
    name_ev: [(year, month, weight, label)] from folder names. era: (first, last) year range a folder name allows."""
    votes, months, kinds, n_dated = Counter(), {}, Counter(), 0
    for m in files:
        ev = _own_evidence(m, era[0] if era else None)
        if not ev:
            continue
        y, mo, w, label = max(ev, key=lambda e: e[2])
        if era and not era[0] <= y <= era[1]:
            w = min(w, W_SCANNED)  # a date outside the decade the folder names: when it was scanned or copied
        votes[y] += w
        n_dated += 1
        if mo:
            months.setdefault(y, Counter())[mo] += 1
        kinds[label.split(":")[0]] += 1
    labels, support = [], {}
    total = sum(votes.values())
    for y, v in votes.items():
        share = v / total
        support[y] = share * (1 - 0.5 ** v)  # more agreeing files -> surer, up to the share that agrees
    if votes:
        top = votes.most_common(4)
        n = len(files)
        kinds_txt = " / ".join(k.lower() for k, _ in kinds.most_common(2))
        if len(votes) == 1:
            labels.append((top[0][0], f"{n_dated} of {n} files have a date from {top[0][0]} ({kinds_txt})"))
        else:
            spread = ", ".join(f"{y} ({sum(1 for m in files if _file_year(m, era) == y)})" for y, _ in top)
            labels.append((top[0][0], f"{n_dated} of {n} files have dates ({kinds_txt}), from several years: {spread}"
                                     f"{' …' if len(votes) > 4 else ''}"))
    for y, mo, w, label in name_ev:
        support[y] = 1 - (1 - support.get(y, 0.0)) * (1 - w)
        labels.append((y, label))
        if mo:
            months.setdefault(y, Counter())[mo] += 3
    mtime_only = False
    if not support:
        mt = Counter(datetime.fromtimestamp(m["mtime"]).year for m in files if m["mtime"])
        if mt:
            y, c = mt.most_common(1)[0]
            support[y] = W_MODIFIED * c / sum(mt.values())
            labels.append((y, f"No dates in the files or folder name; {c} of {len(files)} files were last changed in {y} "
                              f"(often just when they were copied)"))
            mtime_only = True
    if not support:
        return None
    ranked = sorted(support.items(), key=lambda kv: -kv[1])
    best, s = ranked[0]
    runner = ranked[1][1] if len(ranked) > 1 else 0.0
    confidence = s * (1 - runner)
    month = months[best].most_common(1)[0][0] if best in months else None
    if era and not era[0] <= best <= era[1]:
        confidence *= 0.5
    return {"year": best, "month": month, "confidence": round(confidence, 3), "mtime_only": mtime_only,
            "evidence": [{"label": t, "year": y, "agrees": y == best} for y, t in labels],
            "years": sorted(votes)}


def _sort_year(m, era=None):
    """The year a file can be sorted into on its own: only from trustworthy evidence (camera, file name date)."""
    ev = [e for e in _own_evidence(m, era[0] if era else None) if e[2] >= W_FILE_NAME_MDY]
    return max(ev, key=lambda e: e[2])[0] if ev else None


def _sort_month(m):
    ev = [e for e in _own_evidence(m) if e[2] >= W_FILE_NAME_MDY and e[1]]
    return max(ev, key=lambda e: e[2])[1] if ev else None


def _file_year(m, era=None):
    ev = _own_evidence(m, era[0] if era else None)
    return max(ev, key=lambda e: e[2])[0] if ev else None


def _folder_name_evidence(segs):
    """Year (or decade / year range) evidence from folder names, deepest first.
    Returns (name evidence list, era range or None)."""
    for seg in reversed(segs):
        sy = _segment_year(seg)
        if sy:
            return [(sy[0], sy[1], W_FOLDER_NAME, f"Year in folder name: “{seg}”")], None
        years = sorted({int(y) for y in YEAR_WORD.findall(seg) if _name_ok(int(y))})
        if len(years) == 2 and years[1] - years[0] <= 20:
            return [], (years[0], years[1])
        dec = DECADE_WORD.search(seg)
        if dec:
            d = int(dec.group(1)) * 10
            return [], (d, d + 9)
    return [], None


def _outside_units(rows, roots):
    """Folders that are not inside a year folder, as {folder: [rows of it and its subfolders]}.
    A folder that holds year folders (like PhotoSync or the root itself) is a container, not a unit, and a
    subfolder belongs to the nearest ancestor that has pictures of its own."""
    direct = {}
    for r in rows:
        direct.setdefault(r["folder"], []).append(r)
    rels = {}
    for f in direct:
        rt = root_of(f, roots)
        if rt:
            rels[f] = (rt, os.path.relpath(f, rt))
    containers = set()
    for f, (rt, rel) in rels.items():
        yf = year_folder(rel) if rel != "." else None
        if yf:  # every folder above a year segment holds year folders
            parts = rel.split("/")
            containers.add(rt)
            for i in range(1, yf[0] + 1):
                containers.add(os.path.join(rt, *parts[:i]))
    units = {}
    for f in sorted(rels, key=len):
        rt, rel = rels[f]
        if rel == "." or f in containers or year_folder(rel):
            continue
        parent = next((u for u in units if f.startswith(u + "/")), None)
        if parent:
            units[parent].extend(direct[f])
        else:
            units[f] = list(direct[f])
    return units, containers


def _rename_label(seg, new_year, new_month):
    """Folder name with its year (and month) words changed, keeping the way the month was written."""
    def month_word(old, mo):
        full = MONTH_NAMES[mo - 1]
        if old.lower() == "sept":
            w = "sept" if mo == 9 else full[:3]
        elif len(old) == 3:
            w = full[:3]
        else:
            w = full
        return w.upper() if old.isupper() else w.capitalize() if old[0].isupper() else w

    def repl(mt):
        mo = new_month or MONTHS[mt.group(1).lower()[:3]]
        return mt.group(0).replace(mt.group(1), month_word(mt.group(1), mo), 1).replace(mt.group(2), str(new_year))
    if MONTH_YEAR.search(seg):
        return MONTH_YEAR.sub(repl, seg, count=1)
    return YEAR_WORD.sub(str(new_year), seg, count=1)


def analyze_folders(rows, roots):
    """Folder-level suggestions:
    - outside: a folder with no year folder above it: best guess of its year, and where it would go
    - name: an event folder whose name gives a year or month that its photos disagree with"""
    out = []
    units, containers = _outside_units(rows, roots)
    layouts = _year_layouts(rows, roots)
    for folder, files in units.items():
        rt = root_of(folder, roots)
        segs = os.path.relpath(folder, rt).split("/")
        name_ev, era = _folder_name_evidence(segs)
        g = _year_guess(files, name_ev, era)
        sample = [m["id"] for m in sorted(files, key=lambda m: (m["kind"] != "image", m["name"]))[:FOLDER_SAMPLE]]
        item = {"kind": "outside", "path": folder, "count": len(files), "sample": sample,
                "has_subfolders": any(m["folder"] != folder for m in files)}
        if era:
            item["era"] = f"{era[0]}–{era[1]}"
        if not g:
            out.append(dict(item, year=None, confidence=0, level="none",
                            evidence=[{"label": "No dates in the files, their names or the folder name", "year": None,
                                       "agrees": False}]))
            continue
        base, monthly = layouts.get(rt, (rt, False))
        parts = [base, str(g["year"])]
        if monthly and g["month"]:
            parts.append(f"{g['month']:02d}")
        target = os.path.join(*parts, os.path.basename(folder))
        item.update(year=g["year"], confidence=g["confidence"], level=_level(g["confidence"]),
                    evidence=g["evidence"], years=g["years"], target=target, mtime_only=g["mtime_only"])
        # a dump of many years (a phone's camera roll, an iCloud export): sort each file by its own date instead
        split = Counter(y for m in files if (y := _sort_year(m, era)))
        if len(split) >= 2 and split.most_common(1)[0][1] / len(files) < 0.7 and not name_ev:
            item.update(kind="split", split=sorted(split.items()), undated=len(files) - sum(split.values()),
                        target=os.path.join(base, "<year>", os.path.basename(folder)))
        out.append(item)
    # event folders inside year folders whose names disagree with their photos
    by_folder = {}
    for r in rows:
        by_folder.setdefault(r["folder"], []).append(r)
    for folder, files in by_folder.items():
        rt = root_of(folder, roots)
        rel = os.path.relpath(folder, rt) if rt else "."
        yf = year_folder(rel) if rel != "." else None
        if not yf:
            continue
        parts = rel.split("/")
        below = parts[yf[0] + 1:]
        if below and MONTH_SEG.match(below[0]):
            below = below[1:]
        if not below:
            continue
        seg = below[-1]
        sy = _segment_year(seg)
        if not sy:
            continue
        dated = [datetime.fromtimestamp(m["meta_date"]) for m in files
                 if m["meta_date"] and (m["meta_src"] or "camera") in ("camera", "video", "device")]
        if len(dated) < 3:
            continue
        (y, n_y), = Counter(d.year for d in dated).most_common(1)
        months = Counter(d.month for d in dated if d.year == y)
        mo, n_mo = months.most_common(1)[0]
        share_y, share_mo = n_y / len(dated), n_mo / len(dated)
        new_year, new_month, why = sy[0], sy[1], []
        if y != sy[0] and share_y >= 0.8:
            new_year = y
            why.append(f"{n_y} of {len(dated)} camera dates are from {y}, but the name says {sy[0]}")
        if sy[1] and share_mo >= 0.8 and mo != sy[1]:
            new_month = mo
            why.append(f"{n_mo} of {len(dated)} camera dates are in {MONTH_NAMES[mo - 1].capitalize()} {y}, "
                       f"but the name says {MONTH_NAMES[sy[1] - 1].capitalize()}")
        if not why:
            continue
        new_seg = _rename_label(seg, new_year, new_month if sy[1] else None)
        if new_seg == seg:
            continue
        share = share_y if new_year != sy[0] else share_mo
        confidence = round(share * (1 - 0.5 ** len(dated)), 3)
        out.append({"kind": "name", "path": folder, "count": len(files), "year": new_year, "name_year": sy[0],
                    "confidence": confidence, "level": _level(confidence),
                    "evidence": [{"label": t, "year": new_year, "agrees": True} for t in why] +
                                [{"label": f"Folder name: “{seg}”", "year": sy[0], "agrees": False}],
                    "target": os.path.join(os.path.dirname(folder), new_seg), "new_name": new_seg,
                    "sample": [m["id"] for m in sorted(files, key=lambda m: m["name"])[:FOLDER_SAMPLE]]})
    out.sort(key=lambda f: (f["kind"] == "name", -f["confidence"], f["path"]))
    return out


def _read_missing_meta(job, rows):
    """Read dates from the headers of files whose metadata has not been read yet."""
    from imaging import read_meta_date
    # a date with no recorded source was read by an older version: read it again to learn where it came from
    todo = [r for r in rows if not r["meta_read"] or (r["meta_date"] and r["meta_src"] is None and r["kind"] == "image")]
    conn = db()
    for i, r in enumerate(todo):
        if i % 20 == 0:
            job.step("Reading photo dates", i, len(todo))
        try:
            d, src = read_meta_date(r["path"], r["kind"])
        except Exception:
            d, src = None, None
        conn.execute("UPDATE media SET meta_date=?, meta_src=?, meta_read=1 WHERE id=?", (d, src, r["id"]))
    return len(todo)


def run_year_check(job):
    roots = lib.load_config()["roots"]
    conn = db()

    def in_roots():
        return [r for r in conn.execute("SELECT id, path, folder, name, kind, mtime, meta_date, meta_src, meta_read "
                                        "FROM media WHERE missing=0") if root_of(r["folder"], roots)]
    rows = in_roots()
    if _read_missing_meta(job, rows):
        rows = in_roots()
    job.step("Comparing dates with folders", 0, len(rows))
    in_years = [r for r in rows if (rel := os.path.relpath(r["folder"], root_of(r["folder"], roots))) != "."
                and year_folder(rel)]
    by_folder = {}
    for r in in_years:
        if (y := _strong_year(r)):
            by_folder.setdefault(r["folder"], Counter())[y] += 1
    found = [a for r in in_years if (a := analyze(r, roots, by_folder.get(r["folder"], Counter())))]
    found.sort(key=lambda a: (-a["confidence"], a["rel"]))
    job.step("Guessing years for folders", 0, 0)
    folders = analyze_folders(rows, roots)
    return {"items": found, "checked": len(in_years), "folders": folders,
            "outside_files": sum(f["count"] for f in folders if f["kind"] == "outside")}


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


def _move_folder_rows(conn, old, new):
    """Point every media row in folder `old` (and its subfolders) at `new`."""
    n = len(old)
    conn.execute("BEGIN")
    # stale rows for files that used to be at the destination would block the UNIQUE path
    conn.execute("DELETE FROM media WHERE missing=1 AND (folder=? OR substr(folder, 1, ?)=?)", (new, len(new) + 1, new + "/"))
    conn.execute("""UPDATE media SET path = ? || substr(path, ?), folder = ? || substr(folder, ?)
                    WHERE folder=? OR substr(folder, 1, ?)=?""",
                 (new, n + 1, new, n + 1, old, n + 1, old + "/"))
    conn.execute("COMMIT")


def _split_folder(conn, folder, roots):
    """Move each dated file of a many-year folder to <year folder base>/<year>[/<month>]/<folder name>/<subfolders>.
    Files without a trustworthy date stay. Returns (files moved, files left)."""
    rt = root_of(folder, roots)
    rows = conn.execute("SELECT * FROM media WHERE missing=0 AND (folder=? OR substr(folder, 1, ?)=?)",
                        (folder, len(folder) + 1, folder + "/")).fetchall()
    every = conn.execute("SELECT folder FROM media WHERE missing=0").fetchall()
    base, monthly = _year_layouts(every, roots).get(rt, (rt, False))
    moved = kept = 0
    for m in rows:
        y = _sort_year(m)
        if not y or not os.path.exists(m["path"]):
            kept += 1
            continue
        parts = [base, str(y)]
        if monthly and (mo := _sort_month(m)):
            parts.append(f"{mo:02d}")
        sub = os.path.relpath(m["folder"], folder)
        dest_dir = os.path.normpath(os.path.join(*parts, os.path.basename(folder), sub))
        _set_path(conn, m["id"], _move_file(m["path"], dest_dir))
        _remove_empty_dirs(m["folder"], rt)
        moved += 1
    return moved, kept


def apply_folders(paths):
    """Move (outside a year folder) or rename (name disagrees with its photos) whole folders, as the last
    year check suggested. Returns [{path, ok, new|error}]."""
    res = year_job.result
    if not res:
        raise ValueError("Run the year check first")
    by_path = {f["path"]: f for f in res.get("folders", [])}
    roots = lib.load_config()["roots"]
    conn = db()
    out, done = [], []
    _acquire_fs()
    try:
        for p in paths:
            f = by_path.get(p)
            if not f or not f.get("target"):
                out.append({"path": p, "ok": False, "error": "No suggestion for this folder"})
                continue
            if any(p.startswith(d + "/") or d.startswith(p + "/") for d, _ in done):
                out.append({"path": p, "ok": False, "error": "A folder around it was just moved: check again"})
                continue
            if f["kind"] == "split":
                try:
                    n, kept = _split_folder(conn, p, roots)
                    done.append((p, p))
                    out.append({"path": p, "ok": True, "new": None, "moved": n, "kept": kept})
                except Exception as e:
                    out.append({"path": p, "ok": False, "error": str(e)})
                continue
            try:
                if not os.path.isdir(p):
                    raise FileNotFoundError("The folder is no longer there")
                dest = _free_path(f["target"])
                if dest.startswith(p + "/"):
                    raise ValueError("Cannot move a folder into itself")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                try:
                    os.rename(p, dest)
                except OSError:
                    shutil.move(p, dest)
                _move_folder_rows(conn, p, dest)
                rt = root_of(p, roots)
                if rt:
                    _remove_empty_dirs(os.path.dirname(p), rt)
                done.append((p, dest))
                out.append({"path": p, "ok": True, "new": dest})
            except Exception as e:
                out.append({"path": p, "ok": False, "error": str(e)})
    finally:
        lib.fs_lock.release()
    # suggestions in or around the moved folders are stale now
    if done:
        under = lambda f, d: f == d or f.startswith(d + "/")  # noqa: E731
        res["folders"] = [f for f in res["folders"] if not any(under(f["path"], d) for d, _ in done)]
        moved = set()
        for _, new in done:
            moved |= {r[0] for r in conn.execute("SELECT id FROM media WHERE folder=? OR substr(folder, 1, ?)=?",
                                                 (new, len(new) + 1, new + "/"))}
        res["items"] = [a for a in res["items"] if a["id"] not in moved]
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


TRASH = {"duplicate": ".removed-duplicates", "junk": ".removed-junk"}  # "." so the indexer skips them


def _trash_dir(root, reason):
    return os.path.join(root, TRASH[reason])


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
    out = move_to_holding(ids, "duplicate")
    _forget(dup_job, {o["id"] for o in out if o["ok"]})
    return out


def move_to_holding(ids, reason):
    """Move files into the hidden holding folder (per reason) of their root, so they can be put back."""
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
                dest = _move_file(m["path"], os.path.normpath(os.path.join(_trash_dir(root, reason), rel)))
                conn.execute("UPDATE media SET missing=1 WHERE id=?", (mid,))
                conn.execute("INSERT INTO removed(media_id, orig_path, trash_path, size, removed, reason) "
                             "VALUES(?,?,?,?,?,?)", (mid, m["path"], dest, m["size"], time.time(), reason))
                out.append({"id": mid, "ok": True})
            except Exception as e:
                out.append({"id": mid, "ok": False, "error": str(e)})
    finally:
        lib.fs_lock.release()
    state.clip_version += 1
    return out


def removed_summary(reason="duplicate"):
    r = db().execute("SELECT COUNT(*) n, COALESCE(SUM(size), 0) bytes FROM removed WHERE reason=?", (reason,)).fetchone()
    return {"count": r["n"], "bytes": r["bytes"]}


def restore_removed(reason="duplicate"):
    """Put every file the tool for `reason` removed back where it was."""
    conn = db()
    restored, failed = 0, []
    _acquire_fs()
    try:
        for r in conn.execute("SELECT * FROM removed WHERE reason=?", (reason,)).fetchall():
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
    if reason == "duplicate":
        dup_job.result = None  # groups changed; the list must be rebuilt
    return {"restored": restored, "failed": failed}


def empty_removed(reason="duplicate"):
    """Permanently delete every file the tool for `reason` removed."""
    conn = db()
    deleted = 0
    for r in conn.execute("SELECT * FROM removed WHERE reason=?", (reason,)).fetchall():
        try:
            os.remove(r["trash_path"])
            deleted += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[tidy] cannot delete {r['trash_path']}: {e}")
            continue
        conn.execute("DELETE FROM removed WHERE id=?", (r["id"],))
        conn.execute("DELETE FROM media WHERE id=? AND missing=1", (r["media_id"],))
        _remove_empty_dirs(os.path.dirname(r["trash_path"]), os.path.dirname(_trash_root(r["trash_path"])))
    state.faces_version += 1
    return {"deleted": deleted}


def _trash_root(trash_path):
    """The hidden holding folder that holds trash_path (removed too once it is empty)."""
    head = trash_path
    while os.path.basename(head) not in TRASH.values() and head != os.path.dirname(head):
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

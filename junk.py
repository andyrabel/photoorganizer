"""Junk finder: pictures that are not personal photos (screenshots, paperwork, memes and web graphics),
with book artwork (drawings, illustrations, book covers) kept apart so it is never removed
by mistake.

Uses the CLIP embeddings the describe-a-photo search already has (zero-shot: each picture is compared with
short descriptions of each kind of picture), plus clues CLIP cannot see: phone-screen sizes, file names,
whether a camera recorded the picture, and named people in it.
"""
import os
import re
import threading
import time
from collections import Counter

import numpy as np

import library as lib
from library import db, root_of
from tidy import Job, _forget, move_to_holding

# category -> descriptions. "personal" is what the others are measured against.
PROMPTS = {
    "personal": [
        "a family photo of people", "a candid snapshot of friends", "a selfie", "a photo of children playing",
        "a blurry photo of a person", "a dark photo of people at night", "a vacation photo of a landscape",
        "a photo of a city street", "a photo of a pet", "a photo of food on a plate", "a photo of a house",
        "a photo of a garden", "a photo taken at a party", "a photo of a wedding", "a photo of the beach",
        "an old family photograph", "a vintage black and white photo of people", "a scanned old photo print",
        "a photo of a church service", "a baby photo",
    ],
    "screenshot": [
        "a screenshot of a phone screen", "a screenshot of a website", "a screenshot of a text message conversation",
        "a screenshot of an app", "a screenshot of a computer screen", "an error message dialog box",
        "a photo of a computer screen showing settings",
    ],
    "document": [
        "a photo of a receipt", "a scanned document", "a photo of a printed page of text", "a handwritten note",
        "a photo of a form or letter", "a photo of a business card", "a photo of a ticket", "a QR code",
        "a photo of a restaurant menu", "a photo of an information sign with a lot of text", "a page of a book",
    ],
    "graphic": [
        "a meme with text", "an internet meme", "a quote on a plain background", "a logo", "clip art",
        "an advertisement graphic", "a diagram or chart", "an emoji or smiley icon",
    ],
    "book": [
        "a children's book illustration", "a drawing", "a watercolor painting", "a cartoon character",
        "a pencil sketch", "a book cover", "a page from a picture book", "a digital illustration",
    ],
}
LABELS = {
    "screenshot": "Screenshot or photo of a screen",
    "document": "Document, receipt or sign",
    "graphic": "Meme, logo or web graphic",
    "book": "Illustration or book art",
}
JUNK = ("screenshot", "document", "graphic")
SCALE = 100.0  # CLIP's logit scale

# Portrait-orientation screen sizes of phones and tablets (landscape is checked too)
SCREENS = {(640, 1136), (750, 1334), (1080, 1920), (1242, 2208), (1125, 2436), (828, 1792), (1242, 2688),
           (1170, 2532), (1284, 2778), (1179, 2556), (1290, 2796), (1206, 2622), (1320, 2868), (1080, 2340),
           (1080, 2400), (1440, 3200), (1440, 3088), (1536, 2048), (1668, 2224), (1668, 2388), (1620, 2160),
           (1640, 2360), (2048, 2732), (1488, 2266), (720, 1280), (720, 1600), (1080, 2280), (1440, 2960)}
SCREEN_NAME = re.compile(r"screen ?shot|screen ?capture|fullscreen capture|snapshot_|^capture", re.I)
SCREEN_FOLDER = re.compile(r"screenshot|screen captures?", re.I)
GRAPHIC_FOLDER = re.compile(r"backgrounds?|wallpapers?|smileys|emoji|icons?|logos?|clip ?art|memes?|saved pictures", re.I)

HIGH, MEDIUM, SHOW = 0.85, 0.6, 0.45
BOOK_SHOW = 0.6

_text = {"key": None, "mat": None, "labels": None}
_text_lock = threading.Lock()


def _prompt_matrix(eng):
    with _text_lock:
        if _text["key"] != id(eng):
            labels, embs = [], []
            for cat, ps in PROMPTS.items():
                for p in ps:
                    labels.append(cat)
                    embs.append(eng.embed_text(p))
            _text.update(key=id(eng), mat=np.stack(embs), labels=labels)
        return _text["mat"], _text["labels"]


def category_probs(mat, eng):
    """(categories, probabilities [n_images x n_categories]) for normalized CLIP image embeddings."""
    tmat, labels = _prompt_matrix(eng)
    logits = SCALE * (mat @ tmat.T)
    logits -= logits.max(axis=1, keepdims=True)
    p = np.exp(logits)
    p /= p.sum(axis=1, keepdims=True)
    cats = list(PROMPTS)
    idx = np.array([cats.index(lb) for lb in labels])
    out = np.zeros((len(mat), len(cats)), dtype=np.float32)
    for ci in range(len(cats)):
        out[:, ci] = p[:, idx == ci].sum(axis=1)
    return cats, out


def _level(c):
    return "high" if c >= HIGH else "medium" if c >= MEDIUM else "low"


def _noisy_or(p, w):
    return 1 - (1 - p) * (1 - w)


def judge(m, probs, cats, named):
    """One picture's verdict: (category, confidence, reasons) or None for a personal photo.
    m: media row; probs: its category probabilities; named: names of people tagged in it."""
    p = dict(zip(cats, (float(x) for x in probs)))
    reasons = []
    camera = bool(m["meta_date"]) and (m["meta_src"] in ("camera", "video") or m["kind"] == "video")
    w, h = m["width"] or 0, m["height"] or 0
    ext = os.path.splitext(m["name"])[1].lower()
    folder_name = os.path.basename(m["folder"])
    # clues that are not in the picture itself
    if m["meta_src"] == "screen":
        p["screenshot"] = _noisy_or(p["screenshot"], 0.95)
        reasons.append("The phone marked it as a screenshot")
    elif m["kind"] == "image" and not camera and ((w, h) in SCREENS or (h, w) in SCREENS):
        p["screenshot"] = _noisy_or(p["screenshot"], 0.9 if ext == ".png" else 0.6)
        reasons.append(f"{w}×{h} is a phone or tablet screen size, and no camera recorded it")
    if SCREEN_NAME.search(m["name"]):
        p["screenshot"] = _noisy_or(p["screenshot"], 0.85)
        reasons.append("File name says it is a screen capture")
    if SCREEN_FOLDER.search(folder_name):
        p["screenshot"] = _noisy_or(p["screenshot"], 0.6)
        reasons.append(f"In a folder called “{folder_name}”")
    if GRAPHIC_FOLDER.search(folder_name):
        p["graphic"] = _noisy_or(p["graphic"], 0.5)
        reasons.append(f"In a folder called “{folder_name}”")
    if ext in (".bmp", ".gif") and not camera:
        p["graphic"] = _noisy_or(p["graphic"], 0.25)
        p["screenshot"] = _noisy_or(p["screenshot"], 0.25)
    if camera:
        # a camera took it: a photo of a sign, a screen or a page, not a downloaded graphic
        p["graphic"] *= 0.6
        p["screenshot"] *= 0.85
    elif m["kind"] == "image":
        p["graphic"] = _noisy_or(p["graphic"], 0.15)
    if named:
        for c in ("document", "graphic"):
            p[c] *= 0.4
        p["screenshot"] *= 0.7
    # a screenshot of a video call with the grandchildren is still a family picture
    personal = float(probs[cats.index("personal")])
    for c in JUNK:
        p[c] *= 1 - 0.6 * personal
    cat = max((c for c in p if c != "personal"), key=lambda c: p[c])
    conf = p[cat]
    if cat == "book":
        if conf < BOOK_SHOW:
            return None
    elif conf < SHOW:
        return None
    clip_part = f"Looks like: {LABELS[cat].lower()} ({round(100 * float(probs[cats.index(cat)]))}% by picture content)"
    reasons.insert(0, clip_part)
    if camera:
        reasons.append("Taken with a camera or phone")
    elif m["kind"] == "image":
        reasons.append("No camera information in the file")
    if named:
        reasons.append("People tagged in it: " + ", ".join(sorted(named)[:3]))
    if cat != "book" and personal >= 0.3:
        reasons.append(f"Partly looks like a personal photo ({round(100 * personal)}%)")
    return cat, conf, reasons


def run_junk(job):
    import semantic
    if not semantic.available():
        raise RuntimeError("The junk finder needs the describe-a-photo model files - see README.md")
    from tidy import _read_missing_meta
    _read_missing_meta(job, db().execute(
        "SELECT id, path, kind, meta_read, meta_date, meta_src FROM media WHERE missing=0 AND "
        "(meta_read=0 OR (kind='image' AND meta_date IS NOT NULL AND meta_src IS NULL))").fetchall())
    job.step("Loading picture analysis", 0, 0)
    eng = semantic.engine()
    ids, mat = semantic.index.get()
    prog = semantic.progress()
    roots = lib.load_config()["roots"]
    conn = db()
    rows = {r["id"]: r for r in conn.execute(
        "SELECT id, name, folder, kind, width, height, meta_date, meta_src FROM media WHERE missing=0")}
    marks = {r["media_id"]: r["mark"] for r in conn.execute("SELECT media_id, mark FROM junk_marks")}
    named = {}
    for mid, name in conn.execute("""SELECT f.media_id, p.name FROM faces f JOIN people p ON p.id=f.person_id
                                     WHERE f.ignored=0"""):
        named.setdefault(mid, set()).add(name)
    items = []
    step = 2000
    for s in range(0, len(ids), step):
        job.step("Looking at pictures", s, len(ids))
        cats, probs = category_probs(mat[s:s + step], eng)
        for k, mid in enumerate(ids[s:s + step]):
            mid = int(mid)
            m = rows.get(mid)
            if m is None or not root_of(m["folder"], roots):
                continue
            mark = marks.get(mid)
            v = judge(m, probs[k], cats, named.get(mid, ()))
            if mark == "book":
                v = ("book", v[1] if v and v[0] == "book" else 1.0, (v[2] if v else []) + ["You marked it as book art"])
            elif mark == "keep" or v is None:
                continue
            cat, conf, reasons = v
            items.append({"id": mid, "category": cat, "confidence": round(conf, 3), "level": _level(conf),
                          "reasons": reasons, "marked": mark})
    items.sort(key=lambda a: (a["category"] == "book", -a["confidence"]))
    counts = Counter(a["category"] for a in items)
    return {"items": items, "counts": counts, "checked": len(ids),
            "unanalyzed": max(0, prog["total"] - prog["done"])}


junk_job = Job(run_junk)


def mark(ids, value):
    """Remember a decision: 'keep' (a personal photo), 'book' (book art: always keep), or None to forget it."""
    conn = db()
    conn.execute("BEGIN")
    for mid in ids:
        if value:
            conn.execute("INSERT OR REPLACE INTO junk_marks(media_id, mark, marked) VALUES(?,?,?)",
                         (mid, value, time.time()))
        else:
            conn.execute("DELETE FROM junk_marks WHERE media_id=?", (mid,))
    conn.execute("COMMIT")
    res = junk_job.result
    if res:
        ids = set(ids)
        if value == "keep":
            _forget(junk_job, ids)
        else:
            for a in res["items"]:
                if a["id"] in ids:
                    a["marked"] = value
                    if value == "book":
                        a["category"] = "book"
        res["counts"] = Counter(a["category"] for a in res["items"])


def remove(ids):
    """Move junk to the hidden .removed-junk folder. Book art is refused, even if asked."""
    protected = {r[0] for r in db().execute(
        "SELECT media_id FROM junk_marks WHERE mark='book' AND media_id IN (%s)" % ",".join("?" * len(ids)), ids)} \
        if ids else set()
    out = [{"id": i, "ok": False, "error": "Marked as book art"} for i in ids if i in protected]
    out += move_to_holding([i for i in ids if i not in protected], "junk")
    _forget(junk_job, {o["id"] for o in out if o["ok"]})
    res = junk_job.result
    if res:
        res["counts"] = Counter(a["category"] for a in res["items"])
    return out

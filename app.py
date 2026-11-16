"""Photo Organizer — a Picasa-style local photo browser. Run: python3 app.py"""
import argparse
import json
import mimetypes
import os
import threading
import time

from flask import Flask, abort, jsonify, request, send_file

import junk
import library as lib
import tidy
import video
from library import FACE_THUMBS, THUMBS, db, root_of, state

app = Flask(__name__, static_folder="static", static_url_path="/static")

VIDEO_MIME = {".mov": "video/mp4", ".m4v": "video/mp4", ".3gp": "video/3gpp"}


def bump_assign():
    state.assign_version += 1


def media_row(mid):
    m = db().execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()
    if m is None:
        abort(404)
    return m


def known_taken(ts):
    """A photo date, or None when it is in the future (a camera clock that was never set):
    such photos are shown as "year unknown" instead of under a year that has not happened yet."""
    return ts if ts and ts <= future_cutoff() else None


def future_cutoff():
    return time.time() + 86400   # a day of slack for time zones


def media_json(m):
    return {"id": m["id"], "name": m["name"], "folder": m["folder"], "kind": m["kind"],
            "taken": known_taken(m["taken"]), "width": m["width"], "height": m["height"],
            "rotation": m["rotation"], "crop": json.loads(m["crop"]) if m["crop"] else None,
            "ver": m["edit_ver"],
            **({"converted": video.playable_path(m["id"]).exists()} if m["kind"] == "video" else {})}


# ---------------------------------------------------------------- pages

@app.get("/")
def index():
    return send_file("static/index.html")


# ---------------------------------------------------------------- library

@app.get("/api/status")
def api_status():
    return jsonify({**lib.status(), "ffmpeg": bool(video.ffmpeg_exe())})


@app.post("/api/rescan")
def api_rescan():
    threading.Thread(target=lib.index_roots, daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/roots")
def api_add_root():
    path = lib.normalize_path(request.json.get("path", ""))
    if not os.path.isdir(path):
        return jsonify(error=f"Folder not found: {path}"), 400
    cfg = lib.load_config()
    if path not in cfg["roots"]:
        cfg["roots"].append(path)
        lib.save_config(cfg)
    threading.Thread(target=lib.index_roots, daemon=True).start()
    return jsonify(ok=True, path=path)


@app.delete("/api/roots")
def api_remove_root():
    path = request.json.get("path")
    cfg = lib.load_config()
    cfg["roots"] = [r for r in cfg["roots"] if r != path]
    lib.save_config(cfg)
    threading.Thread(target=lib.index_roots, daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/folders")
def api_folders():
    roots = lib.load_config()["roots"]
    rows = db().execute("""SELECT folder, COUNT(*) n, MIN(CASE WHEN taken<=:c THEN taken END) first,
                           MAX(CASE WHEN taken<=:c THEN taken END) last, SUM(kind='video') videos
                           FROM media WHERE missing=0 GROUP BY folder""", {"c": future_cutoff()}).fetchall()
    out = []
    for r in rows:
        root = root_of(r["folder"], roots)
        if not root:
            continue
        out.append({"path": r["folder"], "root": root, "rel": os.path.relpath(r["folder"], root),
                    "count": r["n"], "videos": r["videos"], "first": r["first"], "last": r["last"]})
    return jsonify(folders=out, roots=roots)


@app.get("/api/media")
def api_media():
    conn = db()
    folder, person = request.args.get("folder"), request.args.get("person")
    if folder:
        state.priority_folder = folder
        state.wake.set()
        rows = conn.execute("SELECT * FROM media WHERE folder=? AND missing=0 ORDER BY taken, name",
                            (folder,)).fetchall()
    elif person:
        rows = conn.execute("""SELECT DISTINCT m.* FROM media m JOIN faces f ON f.media_id=m.id
                               WHERE f.person_id=? AND f.ignored=0 AND m.missing=0
                               ORDER BY m.taken IS NULL OR m.taken>?, m.taken DESC, m.name""",
                            (int(person), future_cutoff())).fetchall()
    else:
        abort(400)
    return jsonify(items=[media_json(m) for m in rows])


@app.get("/api/search")
def api_search():
    """Describe-a-photo search: rank photos by how well they match a free-text description."""
    import semantic
    q = (request.args.get("q") or "").strip()
    if not q:
        abort(400)
    if not semantic.available():
        return jsonify(error="Describe-a-photo search needs the CLIP model files - see README.md"), 503
    hits = semantic.search(q)
    if not hits:
        return jsonify(items=[])
    rows = {m["id"]: m for m in db().execute(
        "SELECT * FROM media WHERE missing=0 AND id IN (%s)" % ",".join("?" * len(hits)), [h[0] for h in hits])}
    return jsonify(items=[dict(media_json(rows[mid]), score=round(s, 3)) for mid, s in hits if mid in rows])


@app.get("/api/media/<int:mid>")
def api_media_detail(mid):
    m = media_row(mid)
    if m["scanned"] == 0:
        from faces import shared_engine
        engine, lock = shared_engine()
        with lock:
            if db().execute("SELECT scanned FROM media WHERE id=?", (mid,)).fetchone()["scanned"] == 0:
                lib.process_media(mid, engine)
        m = media_row(mid)
    return jsonify(media=media_json(m), path=m["path"], faces=faces_for_media(mid))


def faces_for_media(mid):
    from faces import best_people_for
    rows = db().execute("""SELECT f.id, f.x, f.y, f.w, f.h, f.frame, f.person_id, f.manual, p.name
                           FROM faces f LEFT JOIN people p ON p.id=f.person_id
                           WHERE f.media_id=? AND f.ignored=0 ORDER BY f.frame, f.x""", (mid,)).fetchall()
    unnamed = [r["id"] for r in rows if r["person_id"] is None]
    sugg = best_people_for(unnamed) if unnamed else {}
    names = {r["id"]: r["name"] for r in db().execute("SELECT id, name FROM people")}
    out = []
    for r in rows:
        d = dict(r)
        d["suggest"] = [{"person_id": p, "name": names.get(p), "score": round(s, 3)}
                        for p, s in sugg.get(r["id"], [])]
        out.append(d)
    return out


@app.get("/api/media/<int:mid>/file")
def api_media_file(mid):
    m = media_row(mid)
    ext = os.path.splitext(m["path"])[1].lower()
    mime = VIDEO_MIME.get(ext) or mimetypes.guess_type(m["path"])[0] or "application/octet-stream"
    return send_file(m["path"], mimetype=mime, conditional=True, max_age=3600)


@app.get("/api/media/<int:mid>/play")
def api_play(mid):
    """The browser-friendly converted copy of a video (see /convert)."""
    media_row(mid)
    out = video.playable_path(mid)
    if not out.exists():
        abort(404)
    return send_file(out, mimetype="video/mp4", conditional=True, max_age=3600)


@app.post("/api/media/<int:mid>/convert")
def api_convert(mid):
    m = media_row(mid)
    if m["kind"] != "video":
        return jsonify(error="Not a video"), 400
    try:
        key = video.start_playable(m)
    except RuntimeError as e:
        return jsonify(error=str(e)), 400
    return jsonify(video.job(key))


@app.get("/api/media/<int:mid>/convert")
def api_convert_status(mid):
    return jsonify(video.job(f"play:{mid}") or {"state": "none"})


@app.get("/api/media/<int:mid>/thumb")
def api_thumb(mid):
    from imaging import make_thumb
    m = media_row(mid)
    out = THUMBS / f"{mid}_{m['edit_ver']}.jpg"
    if not out.exists():
        try:
            make_thumb(m, out)
        except Exception as e:
            print(f"[thumb] {m['path']}: {e}")
            return send_file("static/broken.svg", mimetype="image/svg+xml", max_age=60)
    return send_file(out, mimetype="image/jpeg", max_age=31536000)


@app.post("/api/media/<int:mid>/edit")
def api_edit(mid):
    m = media_row(mid)
    body = request.json
    rotation = int(body.get("rotation", m["rotation"])) % 4
    crop = body.get("crop", json.loads(m["crop"]) if m["crop"] else None)
    if crop:
        crop = [max(0.0, min(1.0, float(v))) for v in crop]
        if crop[2] < 0.01 or crop[3] < 0.01 or crop == [0, 0, 1, 1]:
            crop = None
    db().execute("UPDATE media SET rotation=?, crop=?, edit_ver=edit_ver+1 WHERE id=?",
                 (rotation, json.dumps(crop) if crop else None, mid))
    for old in THUMBS.glob(f"{mid}_*.jpg"):
        old.unlink(missing_ok=True)
    return jsonify(media=media_json(media_row(mid)))


@app.post("/api/media/<int:mid>/export")
def api_export(mid):
    from imaging import export_edited
    m = media_row(mid)
    crop = json.loads(m["crop"]) if m["crop"] else None
    if m["kind"] == "video":
        try:
            key = video.start_export(m, crop)
        except RuntimeError as e:
            return jsonify(error=str(e)), 400
        return jsonify(video.job(key))
    out = export_edited(m, crop)
    register_copy(m, out, "image")
    return jsonify(state="done", path=out)


@app.get("/api/media/<int:mid>/export")
def api_export_status(mid):
    """Progress of a video export; the finished file is added to the library once."""
    m = media_row(mid)
    j = video.job(f"export:{mid}") or {"state": "none"}
    if j["state"] == "done":
        register_copy(m, j["path"], "video")
    return jsonify(j)


def register_copy(m, out, kind):
    st = os.stat(out)
    db().execute("INSERT OR IGNORE INTO media(path,folder,name,kind,size,mtime,taken) VALUES(?,?,?,?,?,?,?)",
                 (out, os.path.dirname(out), os.path.basename(out), kind, st.st_size, st.st_mtime,
                  m["taken"] or st.st_mtime))
    state.wake.set()
    state.clip_wake.set()


@app.post("/api/media/<int:mid>/faces")
def api_add_face(mid):
    """Manually tag a face the detector missed. box = [x, y, w, h] normalized, original orientation."""
    import numpy as np
    from faces import shared_engine
    from imaging import load_for_detection
    m = media_row(mid)
    x, y, w, h = (float(v) for v in request.json["box"])
    bgr, *_ = load_for_detection(m["path"])
    H, W = bgr.shape[:2]
    engine, lock = shared_engine()
    with lock:
        emb, score = engine.embed_region(bgr, (x * W, y * H, w * W, h * H))
    conn = db()
    cur = conn.execute(
        "INSERT INTO faces(media_id,x,y,w,h,size_px,score,embedding,manual) VALUES(?,?,?,?,?,?,?,?,1)",
        (mid, x, y, w, h, min(w * W, h * H), score,
         emb.astype(np.float32).tobytes() if emb is not None else None))
    state.faces_version += 1
    fid = cur.lastrowid
    name = (request.json.get("name") or "").strip()
    if name:
        assign_face(fid, person_by_name(name))
    return jsonify(face_id=fid, faces=faces_for_media(mid))


# ---------------------------------------------------------------- faces & people

def person_by_name(name):
    conn = db()
    row = conn.execute("SELECT id FROM people WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO people(name, created) VALUES(?,?)", (name, time.time())).lastrowid


def assign_face(fid, pid):
    conn = db()
    conn.execute("UPDATE faces SET person_id=?, ignored=0 WHERE id=?", (pid, fid))
    conn.execute("DELETE FROM face_rejects WHERE face_id=? AND person_id=?", (fid, pid))
    conn.execute("UPDATE people SET cover_face=? WHERE id=? AND cover_face IS NULL", (fid, pid))
    bump_assign()


def resolve_person(body):
    if body.get("person_id"):
        return int(body["person_id"])
    name = (body.get("name") or "").strip()
    if not name:
        abort(400)
    return person_by_name(name)


@app.post("/api/faces/assign")
def api_assign():
    body = request.json
    pid = resolve_person(body)
    conn = db()
    conn.execute("BEGIN")
    for fid in body["face_ids"]:
        assign_face(int(fid), pid)
    conn.execute("COMMIT")
    return jsonify(ok=True, person_id=pid)


@app.post("/api/faces/<int:fid>/unassign")
def api_unassign(fid):
    conn = db()
    row = conn.execute("SELECT person_id FROM faces WHERE id=?", (fid,)).fetchone()
    if row and row["person_id"]:
        conn.execute("INSERT OR IGNORE INTO face_rejects(face_id, person_id) VALUES(?,?)", (fid, row["person_id"]))
        conn.execute("UPDATE people SET cover_face=NULL WHERE cover_face=?", (fid,))
    conn.execute("UPDATE faces SET person_id=NULL WHERE id=?", (fid,))
    bump_assign()
    return jsonify(ok=True)


@app.post("/api/faces/reject")
def api_reject():
    body = request.json
    conn = db()
    conn.execute("BEGIN")
    for fid in body["face_ids"]:
        conn.execute("INSERT OR IGNORE INTO face_rejects(face_id, person_id) VALUES(?,?)",
                     (int(fid), int(body["person_id"])))
    conn.execute("COMMIT")
    bump_assign()
    return jsonify(ok=True)


@app.post("/api/faces/ignore")
def api_ignore():
    """'Not a face' / 'ignore this person'."""
    conn = db()
    conn.execute("BEGIN")
    for fid in request.json["face_ids"]:
        conn.execute("UPDATE faces SET ignored=1, person_id=NULL WHERE id=?", (int(fid),))
        conn.execute("UPDATE people SET cover_face=NULL WHERE cover_face=?", (int(fid),))
    conn.execute("COMMIT")
    state.faces_version += 1
    bump_assign()
    return jsonify(ok=True)


@app.get("/api/faces/<int:fid>")
def api_face(fid):
    f = db().execute("SELECT id, media_id, person_id, frame FROM faces WHERE id=?", (fid,)).fetchone()
    if f is None:
        abort(404)
    return jsonify(dict(f))


@app.get("/api/faces/<int:fid>/thumb")
def api_face_thumb(fid):
    from imaging import make_face_thumb
    out = FACE_THUMBS / f"{fid}.jpg"
    if not out.exists():
        f = db().execute("SELECT f.*, m.path FROM faces f JOIN media m ON m.id=f.media_id WHERE f.id=?",
                         (fid,)).fetchone()
        if f is None:
            abort(404)
        try:
            make_face_thumb(f["path"], f, out)
        except Exception as e:
            print(f"[face] {fid}: {e}")
            return send_file("static/broken.svg", mimetype="image/svg+xml", max_age=60)
    return send_file(out, mimetype="image/jpeg", max_age=31536000)


@app.get("/api/people")
def api_people():
    rows = db().execute("""SELECT p.id, p.name,
             COALESCE(p.cover_face, MIN(f.id)) cover,
             COUNT(DISTINCT f.media_id) n
           FROM people p LEFT JOIN faces f ON f.person_id=p.id AND f.ignored=0
           GROUP BY p.id ORDER BY p.name COLLATE NOCASE""").fetchall()
    return jsonify(people=[dict(r) for r in rows])


@app.patch("/api/people/<int:pid>")
def api_rename_person(pid):
    body = request.json
    conn = db()
    if "name" in body:
        name = body["name"].strip()
        other = conn.execute("SELECT id FROM people WHERE name=? AND id!=?", (name, pid)).fetchone()
        if other:  # renaming onto an existing name merges the two people
            conn.execute("UPDATE faces SET person_id=? WHERE person_id=?", (other["id"], pid))
            conn.execute("DELETE FROM people WHERE id=?", (pid,))
            bump_assign()
            return jsonify(ok=True, person_id=other["id"], merged=True)
        conn.execute("UPDATE people SET name=? WHERE id=?", (name, pid))
    if "cover_face" in body:
        conn.execute("UPDATE people SET cover_face=? WHERE id=?", (int(body["cover_face"]), pid))
    bump_assign()
    return jsonify(ok=True, person_id=pid)


@app.delete("/api/people/<int:pid>")
def api_delete_person(pid):
    conn = db()
    conn.execute("UPDATE faces SET person_id=NULL WHERE person_id=?", (pid,))
    conn.execute("DELETE FROM face_rejects WHERE person_id=?", (pid,))
    conn.execute("DELETE FROM people WHERE id=?", (pid,))
    bump_assign()
    return jsonify(ok=True)


@app.get("/api/people/<int:pid>/suggestions")
def api_suggestions(pid):
    from faces import suggestions_for_person
    return jsonify(suggestions=suggestions_for_person(pid))


@app.get("/api/people/<int:pid>/faces")
def api_person_faces(pid):
    rows = db().execute("SELECT id, media_id FROM faces WHERE person_id=? AND ignored=0 ORDER BY id DESC",
                        (pid,)).fetchall()
    return jsonify(faces=[dict(r) for r in rows])


@app.get("/api/unknown")
def api_unknown():
    from faces import unknown_clusters
    names = {r["id"]: r["name"] for r in db().execute("SELECT id, name FROM people")}
    out = []
    for c in unknown_clusters():
        d = dict(c)
        if "suggest" in d:
            d["suggest"] = dict(d["suggest"], name=names.get(d["suggest"]["person_id"]))
        out.append(d)
    return jsonify(clusters=out)


# ---------------------------------------------------------------- tidy-up tools

def _with_media(ids):
    """{id: media_json} for the given ids that are still in the library."""
    ids = list(ids)
    rows = {}
    for i in range(0, len(ids), 900):  # SQLite variable limit
        chunk = ids[i:i + 900]
        rows.update({m["id"]: media_json(m) for m in db().execute(
            "SELECT * FROM media WHERE missing=0 AND id IN (%s)" % ",".join("?" * len(chunk)), chunk)})
    return rows


@app.get("/api/years")
def api_years_status():
    """Year check: progress, and the files that look like they are in the wrong year folder."""
    j = tidy.year_job
    out = j.status()
    if j.result:
        media = _with_media(a["id"] for a in j.result["items"])
        out["checked"] = j.result["checked"]
        out["items"] = [dict(a, media=media[a["id"]]) for a in j.result["items"] if a["id"] in media]
        folders = j.result.get("folders", [])
        sample = _with_media(i for f in folders for i in f["sample"])
        out["folders"] = [dict(f, sample=[sample[i] for i in f["sample"] if i in sample]) for f in folders]
    return jsonify(out)


@app.post("/api/years")
def api_years_start():
    tidy.year_job.start()
    return jsonify(tidy.year_job.status())


@app.post("/api/years/move")
def api_years_move():
    ids = [int(i) for i in request.json.get("ids", [])]
    try:
        results = tidy.move_to_year(ids)
    except RuntimeError as e:
        return jsonify(error=str(e)), 409
    return jsonify(results=results)


@app.post("/api/years/folders")
def api_years_folders():
    """Move folders that are outside the year folders into one, or fix a folder name, as suggested."""
    try:
        results = tidy.apply_folders([str(p) for p in request.json.get("paths", [])])
    except (ValueError, RuntimeError) as e:
        return jsonify(error=str(e)), 409
    return jsonify(results=results)


@app.get("/api/duplicates")
def api_dupes_status():
    j = tidy.dup_job
    out = j.status()
    out["removed"] = tidy.removed_summary()
    if j.result:
        media = _with_media(it["id"] for g in j.result["groups"] for it in g["items"])
        groups = []
        for g in j.result["groups"]:
            items = [dict(it, media=media[it["id"]]) for it in g["items"] if it["id"] in media]
            if len(items) >= 2:
                groups.append(dict(g, items=items))
        out["groups"] = groups
    return jsonify(out)


@app.post("/api/duplicates")
def api_dupes_start():
    tidy.dup_job.start()
    return jsonify(tidy.dup_job.status())


@app.post("/api/duplicates/remove")
def api_dupes_remove():
    try:
        results = tidy.remove_duplicates([int(i) for i in request.json.get("ids", [])])
    except (ValueError, RuntimeError) as e:
        return jsonify(error=str(e)), 400
    return jsonify(results=results, removed=tidy.removed_summary())


@app.post("/api/duplicates/restore")
def api_dupes_restore():
    try:
        r = tidy.restore_removed()
    except RuntimeError as e:
        return jsonify(error=str(e)), 409
    return jsonify(**r, removed=tidy.removed_summary())


@app.post("/api/duplicates/empty")
def api_dupes_empty():
    return jsonify(**tidy.empty_removed(), removed=tidy.removed_summary())


@app.get("/api/junk")
def api_junk_status():
    j = junk.junk_job
    out = j.status()
    out["removed"] = tidy.removed_summary("junk")
    if j.result:
        media = _with_media(a["id"] for a in j.result["items"])
        out.update(checked=j.result["checked"], unanalyzed=j.result["unanalyzed"],
                   items=[dict(a, media=media[a["id"]]) for a in j.result["items"] if a["id"] in media])
    return jsonify(out)


@app.post("/api/junk")
def api_junk_start():
    junk.junk_job.start()
    return jsonify(junk.junk_job.status())


@app.post("/api/junk/mark")
def api_junk_mark():
    value = request.json.get("mark")
    if value not in ("keep", "book", None):
        abort(400)
    junk.mark([int(i) for i in request.json.get("ids", [])], value)
    return jsonify(ok=True)


@app.post("/api/junk/remove")
def api_junk_remove():
    try:
        results = junk.remove([int(i) for i in request.json.get("ids", [])])
    except RuntimeError as e:
        return jsonify(error=str(e)), 409
    return jsonify(results=results, removed=tidy.removed_summary("junk"))


@app.post("/api/junk/restore")
def api_junk_restore():
    try:
        r = tidy.restore_removed("junk")
    except RuntimeError as e:
        return jsonify(error=str(e)), 409
    junk.junk_job.result = None  # files came back: the list must be rebuilt
    return jsonify(**r, removed=tidy.removed_summary("junk"))


@app.post("/api/junk/empty")
def api_junk_empty():
    return jsonify(**tidy.empty_removed("junk"), removed=tidy.removed_summary("junk"))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--workers", type=int, default=2, help="background face-scan threads")
    args = ap.parse_args()
    lib.init()
    lib.start_background(args.workers)
    print(f"Photo Organizer running at http://localhost:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()

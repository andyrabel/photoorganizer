"""Face detection (YuNet), embeddings (SFace) and similarity search / clustering."""
import os
import threading

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
import cv2  # noqa: E402
import numpy as np  # noqa: E402

from library import BASE, db, state  # noqa: E402

MODELS = BASE / "models"

# SFace cosine similarity: >= 0.363 is OpenCV's "same person" threshold.
SUGGEST_THRESHOLD = 0.40
CLUSTER_THRESHOLD = 0.50
MIN_FACE_PX = 24
CLUSTER_MIN_FACE_PX = 40

try:
    cv2.setLogLevel(0)
except Exception:
    pass


class FaceEngine:
    def __init__(self):
        for f in ("yunet.onnx", "sface.onnx"):
            if not (MODELS / f).exists():
                raise FileNotFoundError(f"Missing face model {MODELS / f} - see README.md for the download commands")
        self.det = cv2.FaceDetectorYN.create(str(MODELS / "yunet.onnx"), "", (320, 320), 0.8, 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(str(MODELS / "sface.onnx"), "")

    def _raw(self, bgr):
        h, w = bgr.shape[:2]
        self.det.setInputSize((w, h))
        _, faces = self.det.detect(bgr)
        return [] if faces is None else list(faces)

    def embed(self, bgr, row):
        feat = self.rec.feature(self.rec.alignCrop(bgr, row)).flatten().astype(np.float32)
        return feat / (np.linalg.norm(feat) + 1e-9)

    def detect(self, bgr, min_px=MIN_FACE_PX):
        """Return [(x, y, w, h, score, embedding)] in pixel coordinates of bgr."""
        h, w = bgr.shape[:2]
        out = []
        for f in self._raw(bgr):
            x, y, fw, fh = (float(v) for v in f[:4])
            if min(fw, fh) < min_px:
                continue
            x0, y0 = max(0.0, x), max(0.0, y)
            x1, y1 = min(float(w), x + fw), min(float(h), y + fh)
            out.append((x0, y0, x1 - x0, y1 - y0, float(f[-1]), self.embed(bgr, f)))
        return out

    def embed_region(self, bgr, box):
        """Embedding for a user-drawn box (pixels). Tries to find a real face inside it first."""
        x, y, w, h = box
        H, W = bgr.shape[:2]
        pad = 0.5
        cx0, cy0 = int(max(0, x - w * pad)), int(max(0, y - h * pad))
        cx1, cy1 = int(min(W, x + w * (1 + pad))), int(min(H, y + h * (1 + pad)))
        crop = np.ascontiguousarray(bgr[cy0:cy1, cx0:cx1])
        if crop.size and min(crop.shape[:2]) >= 16:
            scale = 1.0
            if max(crop.shape[:2]) < 200:   # upscale tiny regions so the detector can see them
                scale = 200 / max(crop.shape[:2])
                crop = cv2.resize(crop, None, fx=scale, fy=scale)
            best, best_iou = None, 0.0
            for f in self._raw(crop):
                fx, fy, fw, fh = f[0] / scale + cx0, f[1] / scale + cy0, f[2] / scale, f[3] / scale
                iou = _iou((x, y, w, h), (fx, fy, fw, fh))
                if iou > best_iou:
                    best, best_iou = f, iou
            if best is not None and best_iou > 0.2:
                row = best.copy()
                row[:14] = row[:14] / scale
                row[0:14:2] += cx0
                row[1:14:2] += cy0
                return self.embed(bgr, row), float(best[-1])
        # fall back: plain resized crop (no landmark alignment)
        x0, y0 = int(max(0, x)), int(max(0, y))
        x1, y1 = int(min(W, x + w)), int(min(H, y + h))
        if x1 - x0 < 4 or y1 - y0 < 4:
            return None, 0.0
        patch = cv2.resize(bgr[y0:y1, x0:x1], (112, 112))
        feat = self.rec.feature(patch).flatten().astype(np.float32)
        return feat / (np.linalg.norm(feat) + 1e-9), 0.0


def _iou(a, b):
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ix = max(0.0, min(ax0 + aw, bx0 + bw) - max(ax0, bx0))
    iy = max(0.0, min(ay0 + ah, by0 + bh) - max(ay0, by0))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


_engine = None
_engine_lock = threading.Lock()


def shared_engine():
    """Engine for request threads (the background workers own their own)."""
    global _engine
    if _engine is None:
        _engine = FaceEngine()
    return _engine, _engine_lock


# ---------------------------------------------------------------- in-memory embedding index

class FaceIndex:
    def __init__(self):
        self.version = -1
        self.ids = np.zeros(0, dtype=np.int64)
        self.mat = np.zeros((0, 128), dtype=np.float32)
        self.pos = {}
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            if self.version != state.faces_version:
                v = state.faces_version
                rows = db().execute(
                    "SELECT id, embedding FROM faces WHERE ignored=0 AND embedding IS NOT NULL").fetchall()
                self.ids = np.array([r["id"] for r in rows], dtype=np.int64)
                self.mat = (np.frombuffer(b"".join(r["embedding"] for r in rows), dtype=np.float32)
                            .reshape(-1, 128) if rows else np.zeros((0, 128), dtype=np.float32))
                self.pos = {int(i): k for k, i in enumerate(self.ids)}
                self.version = v
            return self.ids, self.mat, self.pos


index = FaceIndex()


def _assignments():
    return {r["id"]: r["person_id"] for r in
            db().execute("SELECT id, person_id FROM faces WHERE person_id IS NOT NULL AND ignored=0")}


def best_people_for(face_ids, limit=3):
    """For each face id, the most similar named people: {face_id: [(person_id, score)]}."""
    ids, mat, pos = index.get()
    assigned = _assignments()
    if not assigned:
        return {}
    a_ids = [f for f in assigned if f in pos]
    if not a_ids:
        return {}
    a_rows = mat[[pos[f] for f in a_ids]]
    a_people = np.array([assigned[f] for f in a_ids])
    out = {}
    for fid in face_ids:
        if fid not in pos:
            continue
        sims = a_rows @ mat[pos[fid]]
        best = {}
        for pid, s in zip(a_people, sims):
            if s > best.get(pid, -1):
                best[pid] = float(s)
        ranked = sorted(((p, s) for p, s in best.items() if s >= SUGGEST_THRESHOLD), key=lambda t: -t[1])
        out[fid] = [(int(p), s) for p, s in ranked[:limit]]
    return out


def suggestions_for_person(pid, limit=200):
    """Unnamed faces that look like person `pid`, best first."""
    ids, mat, pos = index.get()
    if not len(ids):
        return []
    conn = db()
    assigned = _assignments()
    mine = [pos[f] for f, p in assigned.items() if p == pid and f in pos]
    if not mine:
        return []
    rejected = {r["face_id"] for r in conn.execute("SELECT face_id FROM face_rejects WHERE person_id=?", (pid,))}
    scores = (mat @ mat[mine].T).max(axis=1)
    cand = [k for k in np.argsort(-scores)
            if scores[k] >= SUGGEST_THRESHOLD and int(ids[k]) not in assigned and int(ids[k]) not in rejected]
    if not cand:
        return []
    # drop candidates that look even more like somebody else who is already named
    others = [(pos[f], p) for f, p in assigned.items() if p != pid and f in pos]
    if others:
        o_rows = mat[[o[0] for o in others]]
        other_best = (mat[cand] @ o_rows.T).max(axis=1)
        cand = [k for k, ob in zip(cand, other_best) if scores[k] >= ob]
    cand = cand[:limit]
    meta = {r["id"]: r for r in conn.execute(
        "SELECT id, media_id FROM faces WHERE id IN (%s)" % ",".join("?" * len(cand)),
        [int(ids[k]) for k in cand])}
    return [{"face_id": int(ids[k]), "media_id": meta[int(ids[k])]["media_id"], "score": round(float(scores[k]), 3)}
            for k in cand if int(ids[k]) in meta]


_cluster_cache = {"key": None, "data": None}
_cluster_lock = threading.Lock()


def unknown_clusters(max_clusters=60, max_faces=15000):
    """Group unnamed faces into likely-same-person clusters (Picasa's <Unknown Person> groups)."""
    with _cluster_lock:
        key = (state.faces_version, state.assign_version)
        if _cluster_cache["key"] == key:
            return _cluster_cache["data"]
        ids, mat, pos = index.get()
        conn = db()
        rows = conn.execute(
            "SELECT id FROM faces WHERE person_id IS NULL AND ignored=0 AND embedding IS NOT NULL "
            "AND size_px >= ? AND score >= 0.85 ORDER BY size_px DESC LIMIT ?",
            (CLUSTER_MIN_FACE_PX, max_faces)).fetchall()
        fids = [r["id"] for r in rows if r["id"] in pos]
        clusters = []
        if fids:
            vecs = mat[[pos[f] for f in fids]]
            n = len(fids)
            cents = np.zeros((n, 128), dtype=np.float32)
            sums = np.zeros((n, 128), dtype=np.float32)
            members = []
            k = 0
            for i in range(n):
                v = vecs[i]
                if k:
                    s = cents[:k] @ v
                    j = int(np.argmax(s))
                    if s[j] >= CLUSTER_THRESHOLD:
                        members[j].append(fids[i])
                        sums[j] += v
                        cents[j] = sums[j] / (np.linalg.norm(sums[j]) + 1e-9)
                        continue
                sums[k] = v
                cents[k] = v
                members.append([fids[i]])
                k += 1
            order = sorted(range(k), key=lambda c: -len(members[c]))
            for c in order:
                if len(members[c]) < 2 or len(clusters) >= max_clusters:
                    break
                clusters.append({"faces": members[c], "centroid": cents[c]})
            # does a cluster look like someone already named?
            assigned = _assignments()
            a_ids = [f for f in assigned if f in pos]
            if a_ids and clusters:
                a_rows = mat[[pos[f] for f in a_ids]]
                a_people = [assigned[f] for f in a_ids]
                for cl in clusters:
                    sims = a_rows @ cl["centroid"]
                    j = int(np.argmax(sims))
                    if sims[j] >= SUGGEST_THRESHOLD:
                        cl["suggest"] = {"person_id": a_people[j], "score": round(float(sims[j]), 3)}
        for i, cl in enumerate(clusters):
            cl.pop("centroid", None)
            cl["id"] = i
            cl["count"] = len(cl["faces"])
        _cluster_cache.update(key=key, data=clusters)
        return clusters

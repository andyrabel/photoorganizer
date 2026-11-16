"""Describe-a-photo search: CLIP image/text embeddings (ONNX, runs locally) and similarity ranking."""
import json
import re
import threading
import time
import unicodedata
from functools import lru_cache

import numpy as np

from library import BASE, db, state

CLIP_DIR = BASE / "models" / "clip"
FILES = ("vision_model.onnx", "text_model.onnx", "tokenizer.json")
DIM = 512
SIDE = 224
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
BATCH = 16

# CLIP cosine similarities are small: ~0.15-0.2 for unrelated pairs, ~0.25-0.30 for good matches.
# No fixed cutoff separates them cleanly (it shifts per query), so results are ranked and trimmed.
MIN_SCORE = 0.21
SPREAD = 0.05      # keep results within this distance of the best match


def available():
    return all((CLIP_DIR / f).exists() for f in FILES)


# ---------------------------------------------------------------- tokenizer (CLIP byte-level BPE)

def _bytes_to_unicode():
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, map(chr, cs)))


class Tokenizer:
    SOT, EOT, MAX_LEN = 49406, 49407, 77
    # CLIP's pattern, with \p{L} -> [^\W\d_] and \p{N} -> \d for the stdlib re module
    PAT = re.compile(r"'s|'t|'re|'ve|'m|'ll|'d|[^\W\d_]+|\d|(?:[^\s\w]|_)+")

    def __init__(self, path):
        model = json.loads(path.read_text(encoding="utf-8"))["model"]
        self.vocab = model["vocab"]
        merges = [tuple(m.split(" ")) if isinstance(m, str) else tuple(m) for m in model["merges"]]
        self.ranks = {m: i for i, m in enumerate(merges)}
        self.byte_enc = _bytes_to_unicode()
        self.bpe = lru_cache(maxsize=4096)(self._bpe)

    def _bpe(self, token):
        word = list(token[:-1]) + [token[-1] + "</w>"]
        while len(word) > 1:
            pairs = [(self.ranks.get((a, b), 1 << 30), i) for i, (a, b) in enumerate(zip(word, word[1:]))]
            rank, at = min(pairs)
            if rank == 1 << 30:
                break
            a, b = word[at], word[at + 1]
            out, i = [], 0
            while i < len(word):
                if i < len(word) - 1 and word[i] == a and word[i + 1] == b:
                    out.append(a + b)
                    i += 2
                else:
                    out.append(word[i])
                    i += 1
            word = out
        return word

    def encode(self, text):
        text = re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip().lower()
        ids = [self.SOT]
        for piece in self.PAT.findall(text):
            enc = "".join(self.byte_enc[b] for b in piece.encode("utf-8"))
            ids.extend(self.vocab[t] for t in self.bpe(enc) if t in self.vocab)
        ids = ids[:self.MAX_LEN - 1] + [self.EOT]
        return np.array([ids], dtype=np.int64)


# ---------------------------------------------------------------- models

class ClipEngine:
    def __init__(self):
        import onnxruntime as ort
        missing = [f for f in FILES if not (CLIP_DIR / f).exists()]
        if missing:
            raise FileNotFoundError(f"Missing CLIP model files in {CLIP_DIR}: {', '.join(missing)} - see README.md")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 4
        self._ort, self._opts = ort, opts
        self.vision = ort.InferenceSession(str(CLIP_DIR / "vision_model.onnx"), opts, providers=["CPUExecutionProvider"])
        self._text = None
        self._text_lock = threading.Lock()
        self.tok = Tokenizer(CLIP_DIR / "tokenizer.json")

    @staticmethod
    def preprocess(pil):
        """Resize the short side to 224 (bicubic), center-crop 224x224, normalize -> CHW float32."""
        from PIL import Image
        w, h = pil.size
        s = SIDE / min(w, h)
        nw, nh = max(SIDE, round(w * s)), max(SIDE, round(h * s))
        im = pil.convert("RGB").resize((nw, nh), Image.Resampling.BICUBIC)
        left, top = (nw - SIDE) // 2, (nh - SIDE) // 2
        im = im.crop((left, top, left + SIDE, top + SIDE))
        arr = (np.asarray(im, dtype=np.float32) / 255.0 - MEAN) / STD
        return arr.transpose(2, 0, 1)

    def embed_images(self, pils):
        batch = np.stack([self.preprocess(p) for p in pils])
        out = self.vision.run(["image_embeds"], {"pixel_values": batch})[0]
        return out / (np.linalg.norm(out, axis=1, keepdims=True) + 1e-9)

    def embed_text(self, text):
        with self._text_lock:
            if self._text is None:
                self._text = self._ort.InferenceSession(str(CLIP_DIR / "text_model.onnx"), self._opts,
                                                        providers=["CPUExecutionProvider"])
        out = self._text.run(["text_embeds"], {"input_ids": self.tok.encode(text)})[0][0]
        return (out / (np.linalg.norm(out) + 1e-9)).astype(np.float32)


_engine = None
_engine_lock = threading.Lock()


def engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = ClipEngine()
        return _engine


# ---------------------------------------------------------------- background indexer

def _load(row):
    from imaging import load_oriented, video_frame
    if row["kind"] == "video":
        im = video_frame(row["path"])
        im.thumbnail((448, 448))
        return im
    return load_oriented(row["path"], 448)


def worker_loop():
    """Embed every photo (and a frame of every video) that has no CLIP embedding yet."""
    try:
        eng = engine()
    except Exception as e:
        print(f"[search] describe-a-photo search is off: {e}")
        return
    conn = db()
    while True:
        try:
            if not _embed_batch(conn, eng):
                state.clip_wake.wait(30)
                state.clip_wake.clear()
        except Exception as e:  # e.g. database busy: back off and retry
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            print(f"[search] indexing error: {e}")
            time.sleep(10)


def _embed_batch(conn, eng):
    """Embed the next batch of un-indexed items. Returns False when there is nothing left to do."""
    rows = conn.execute("""SELECT m.id, m.path, m.kind FROM media m LEFT JOIN clip c ON c.media_id=m.id
                           WHERE c.media_id IS NULL AND m.missing=0
                           ORDER BY m.kind='video', m.taken DESC LIMIT ?""", (BATCH,)).fetchall()
    if not rows:
        return False
    ok, pils, failed = [], [], []
    for r in rows:
        try:
            pils.append(_load(r))
            ok.append(r["id"])
        except Exception as e:  # unreadable file: record it so it is not retried until it changes
            print(f"[search] cannot read {r['path']}: {e}")
            failed.append(r["id"])
    embs = eng.embed_images(pils) if pils else []
    conn.execute("BEGIN")
    conn.executemany("INSERT OR REPLACE INTO clip(media_id, embedding) VALUES(?, ?)",
                     [(mid, e.astype(np.float32).tobytes()) for mid, e in zip(ok, embs)] +
                     [(mid, None) for mid in failed])
    conn.execute("COMMIT")
    state.clip_version += 1
    return True


def start_background():
    if available():
        threading.Thread(target=worker_loop, daemon=True).start()
    else:
        print(f"[search] CLIP models not found in {CLIP_DIR}; describe-a-photo search is off (see README.md)")


# ---------------------------------------------------------------- search

class ClipIndex:
    def __init__(self):
        self.version = -1
        self.ids = np.zeros(0, dtype=np.int64)
        self.mat = np.zeros((0, DIM), dtype=np.float32)
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            if self.version != state.clip_version:
                v = state.clip_version
                rows = db().execute("""SELECT c.media_id, c.embedding FROM clip c JOIN media m ON m.id=c.media_id
                                       WHERE c.embedding IS NOT NULL AND m.missing=0""").fetchall()
                self.ids = np.array([r["media_id"] for r in rows], dtype=np.int64)
                self.mat = (np.frombuffer(b"".join(r["embedding"] for r in rows), dtype=np.float32)
                            .reshape(-1, DIM) if rows else np.zeros((0, DIM), dtype=np.float32))
                self.version = v
            return self.ids, self.mat


index = ClipIndex()


def search(query, limit=200, within=None):
    """[(media_id, score)] best first for a free-text description.
    `within`: only rank these media ids (e.g. the photos of the people named in the search)."""
    ids, mat = index.get()
    if within is not None:
        keep = np.isin(ids, np.fromiter(within, dtype=np.int64))
        ids, mat = ids[keep], mat[keep]
    if not len(ids):
        return []
    q = engine().embed_text(query)
    scores = mat @ q
    order = np.argsort(-scores)[:limit]
    floor = max(MIN_SCORE, float(scores[order[0]]) - SPREAD)
    return [(int(ids[k]), float(scores[k])) for k in order if scores[k] >= floor]


def progress():
    r = db().execute("""SELECT COUNT(*) total, COUNT(c.media_id) done FROM media m
                        LEFT JOIN clip c ON c.media_id=m.id WHERE m.missing=0""").fetchone()
    return {"enabled": available(), "total": r["total"], "done": r["done"]}

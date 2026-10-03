"""Semantic fingerprints for tracks, via Ollama's nomic-embed-text.

Two jobs:
  1. Give every track a 768-d vector so "more like this" means something even
     for songs with no play history at all.
  2. Cluster those vectors into "vibes". Per-track context stats are hopelessly
     sparse -- you will never have thirty Tuesday-morning plays of one song --
     but per-vibe stats get dense fast, so that is where preference is learned.
"""
import hashlib
import json
import threading

import numpy as np
import requests

from . import db
from .config import load


def track_text(track):
    """The sentence we embed. Artist and title carry most of the signal;
    tags and album sharpen genre without drowning it."""
    parts = [
        (track.get("artist") or "").strip(),
        (track.get("title") or "").strip(),
    ]
    head = " - ".join([p for p in parts if p])
    extra = []
    if track.get("album"):
        extra.append(str(track["album"]))
    tags = track.get("tags")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []
    if tags:
        extra.append(", ".join(str(t) for t in tags))
    if track.get("year"):
        extra.append(str(track["year"]))
    return head + (". " + ". ".join(extra) if extra else "")


def text_hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def normalize(v):
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n else v


class Embedder:
    def __init__(self, cfg=None):
        cfg = cfg or load()
        self.cfg = cfg["ollama"]
        self.url = self.cfg["url"].rstrip("/")
        self.model = self.cfg["embed_model"]
        self.timeout = self.cfg.get("timeout", 25)
        self.available = None
        self.last_error = None
        self._lock = threading.Lock()

    def embed_text(self, text):
        """One vector, L2-normalised, or None if Ollama is not up."""
        if not text:
            return None
        try:
            r = requests.post(
                self.url + "/api/embeddings",
                json={"model": self.model, "prompt": text},
                timeout=self.timeout,
            )
            r.raise_for_status()
            vec = r.json().get("embedding")
            if not vec:
                self.available = False
                return None
            self.available = True
            return normalize(vec)
        except Exception as e:
            self.available = False
            self.last_error = str(e)
            return None

    # ------------------------------------------------------------ storage

    def get(self, conn, track_id):
        row = conn.execute(
            "SELECT vec FROM embeddings WHERE track_id = ?", (track_id,)
        ).fetchone()
        return db.unpack(row["vec"]) if row else None

    def ensure(self, conn, track_row, force=False):
        """Embed a track if it is not already embedded. Returns the vector."""
        track = db.track_dict(track_row) if not isinstance(track_row, dict) else track_row
        tid = track["id"]
        text = track_text(track)
        h = text_hash(text)
        if not force:
            row = conn.execute(
                "SELECT vec, text_hash FROM embeddings WHERE track_id = ?", (tid,)
            ).fetchone()
            if row and row["text_hash"] == h:
                return db.unpack(row["vec"])
        vec = self.embed_text(text)
        if vec is None:
            return None
        with conn:
            conn.execute(
                "INSERT INTO embeddings (track_id, model, dim, vec, text_hash, ts)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(track_id) DO UPDATE SET model=excluded.model,"
                " dim=excluded.dim, vec=excluded.vec, text_hash=excluded.text_hash,"
                " ts=excluded.ts",
                (tid, self.model, len(vec), db.pack(vec), h, db.now()),
            )
        return vec

    def backfill(self, conn, limit=None, progress=None):
        """Embed every track that lacks a current vector."""
        rows = conn.execute(
            "SELECT t.* FROM tracks t LEFT JOIN embeddings e ON e.track_id = t.id"
            " WHERE e.track_id IS NULL" + (f" LIMIT {int(limit)}" if limit else "")
        ).fetchall()
        done = 0
        for row in rows:
            if self.ensure(conn, row) is not None:
                done += 1
                if progress and done % 10 == 0:
                    progress(done, len(rows))
            else:
                break  # Ollama went away; stop rather than hammer it
        return done, len(rows)

    def matrix(self, conn, track_ids=None):
        """Return (ids, matrix) of stored embeddings, rows L2-normalised."""
        if track_ids:
            marks = ",".join("?" * len(track_ids))
            rows = conn.execute(
                f"SELECT track_id, vec FROM embeddings WHERE track_id IN ({marks})",
                list(track_ids),
            ).fetchall()
        else:
            rows = conn.execute("SELECT track_id, vec FROM embeddings").fetchall()
        if not rows:
            return [], np.zeros((0, 0), dtype=np.float32)
        ids = [r["track_id"] for r in rows]
        mat = np.vstack([db.unpack(r["vec"]) for r in rows])
        return ids, mat


# --------------------------------------------------------------- clustering

def kmeans(mat, k, iters=40, seed=0):
    """Spherical k-means. Vectors are unit-length, so cosine == dot product."""
    n = mat.shape[0]
    k = max(1, min(k, n))
    rng = np.random.default_rng(seed)

    # k-means++ style seeding: spread the initial centroids out.
    centroids = [mat[rng.integers(n)]]
    for _ in range(1, k):
        sims = np.max(mat @ np.vstack(centroids).T, axis=1)
        dist = np.clip(1.0 - sims, 1e-9, None)
        probs = dist / dist.sum()
        centroids.append(mat[rng.choice(n, p=probs)])
    C = np.vstack(centroids)

    labels = np.zeros(n, dtype=np.int32)
    for _ in range(iters):
        sims = mat @ C.T
        new = np.argmax(sims, axis=1).astype(np.int32)
        if np.array_equal(new, labels):
            break
        labels = new
        for j in range(k):
            members = mat[labels == j]
            if len(members):
                v = members.mean(axis=0)
                norm = np.linalg.norm(v)
                C[j] = v / norm if norm else C[j]
    sims = np.max(mat @ C.T, axis=1)
    return labels, C, sims


def rebuild_vibes(conn, k=24, seed=0):
    """Recluster every embedded track. Cheap enough to run after imports."""
    emb = Embedder()
    ids, mat = emb.matrix(conn)
    if len(ids) < 2:
        return 0
    k = max(2, min(k, len(ids) // 2 or 2))
    labels, C, sims = kmeans(mat, k, seed=seed)
    ts = db.now()
    with conn:
        conn.execute("DELETE FROM vibes")
        conn.execute("DELETE FROM track_vibe")
        for j in range(C.shape[0]):
            size = int((labels == j).sum())
            conn.execute(
                "INSERT INTO vibes (id, centroid, dim, label, size, updated)"
                " VALUES (?,?,?,?,?,?)",
                (j, db.pack(C[j]), int(C.shape[1]), "", size, ts),
            )
        conn.executemany(
            "INSERT INTO track_vibe (track_id, vibe_id, sim) VALUES (?,?,?)",
            [(int(ids[i]), int(labels[i]), float(sims[i])) for i in range(len(ids))],
        )
    label_vibes(conn)
    return int(C.shape[0])


def label_vibes(conn):
    """Name each vibe after the artists that dominate it. Purely cosmetic,
    but it makes 'why this song' readable without asking an LLM."""
    rows = conn.execute(
        "SELECT tv.vibe_id, t.artist FROM track_vibe tv"
        " JOIN tracks t ON t.id = tv.track_id"
    ).fetchall()
    from collections import Counter, defaultdict
    by_vibe = defaultdict(Counter)
    for r in rows:
        if r["artist"]:
            by_vibe[r["vibe_id"]][r["artist"].split(",")[0].strip()] += 1
    with conn:
        for vibe_id, counter in by_vibe.items():
            top = ", ".join(name for name, _ in counter.most_common(3))
            conn.execute("UPDATE vibes SET label = ? WHERE id = ?", (top, vibe_id))


def vibe_of(conn, track_id):
    row = conn.execute(
        "SELECT vibe_id FROM track_vibe WHERE track_id = ?", (track_id,)
    ).fetchone()
    return row["vibe_id"] if row else None


def assign_vibe(conn, track_id, vec):
    """Put a newly embedded track into the nearest existing vibe."""
    rows = conn.execute("SELECT id, centroid FROM vibes").fetchall()
    if not rows or vec is None:
        return None
    C = np.vstack([db.unpack(r["centroid"]) for r in rows])
    sims = C @ np.asarray(vec, dtype=np.float32)
    j = int(np.argmax(sims))
    vibe_id = rows[j]["id"]
    with conn:
        conn.execute(
            "INSERT INTO track_vibe (track_id, vibe_id, sim) VALUES (?,?,?)"
            " ON CONFLICT(track_id) DO UPDATE SET vibe_id=excluded.vibe_id,"
            " sim=excluded.sim",
            (track_id, vibe_id, float(sims[j])),
        )
    return vibe_id

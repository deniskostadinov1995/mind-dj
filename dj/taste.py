"""The scoring engine.

Seven signals decide what plays next:

  sound      does it *sound* like this moment wants: energy, brightness, punch
  affinity   do you like this track at all
  context    do you like this KIND of track in THIS kind of moment
  semantic   does it sit near what you actually finish at moments like this
  dormancy   you loved it once and have not heard it in months
  explore    you have never heard it
  fatigue    you have heard it too much, or too much of this artist lately

All of it is numpy and SQL. No model call happens on this path, because
Ollama takes fifty seconds to wake up and the next track cannot wait.
"""
import json
import math
from datetime import datetime, timezone

import numpy as np

from . import context as ctxmod
from . import query as qmod
from . import db, embed
from .config import load

# Explicit feedback is worth a lot more than an inferred signal.
FEEDBACK_VALUE = {"love": 3.0, "like": 1.5, "dislike": -2.5, "ban": -8.0, "not_now": -0.4}


def _parse(ts):
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _age_days(ts, now=None):
    d = _parse(ts)
    if not d:
        return None
    return max(0.0, ((now or datetime.now(timezone.utc)) - d).total_seconds() / 86400.0)


def _decay(days, half_life):
    if days is None:
        return 0.0
    return 0.5 ** (days / max(1e-6, half_life))


class Taste:
    def __init__(self, conn=None, cfg=None):
        # conn is accepted for call compatibility but deliberately not stored;
        # see db.connect() for why a connection must not cross threads.
        cfg = cfg or load()
        self.cfg = cfg["taste"]
        self.embedder = embed.Embedder(cfg)
        self._affinity_cache = {}
        self._centroid_cache = {}

    @property
    def conn(self):
        """Always the calling thread's connection -- see db.connect()."""
        return db.connect()

    def invalidate(self):
        self._affinity_cache.clear()
        self._centroid_cache.clear()

    # ------------------------------------------------------------ affinity

    def affinity(self, track_id, now=None):
        """How much you like this track, ignoring context. Roughly -1..+1."""
        if track_id in self._affinity_cache:
            return self._affinity_cache[track_id]
        hl = self.cfg["affinity_half_life_days"]
        liked = self.cfg["completion_liked"]
        skipped = self.cfg["completion_skipped"]
        total = 0.0

        for r in self.conn.execute(
            "SELECT completion, started_at, requested, outcome FROM plays WHERE track_id = ?",
            (track_id,),
        ):
            w = _decay(_age_days(r["started_at"], now), hl)
            c = r["completion"] or 0.0
            if r["outcome"] == "completed" or c >= liked:
                sig = 1.0
            elif c <= skipped:
                # An early skip is the loudest negative signal you ever give.
                sig = -1.2
            else:
                sig = 0.15
            if r["requested"]:
                sig += 1.0  # you went and asked for it by name
            total += sig * w

        for r in self.conn.execute(
            "SELECT kind, weight, ts FROM feedback WHERE track_id = ?", (track_id,)
        ):
            w = _decay(_age_days(r["ts"], now), hl * 2)  # opinions age slower
            total += FEEDBACK_VALUE.get(r["kind"], 0.0) * (r["weight"] or 1.0) * w

        # Squash so a track played fifty times cannot swamp everything else.
        val = math.tanh(total / 3.0)
        self._affinity_cache[track_id] = val
        return val

    # ------------------------------------------------------------- context

    def context_fit(self, track_id, ctx):
        """Learned preference for this track's vibe in this kind of moment.

        Walks the bucket hierarchy from narrow to broad. Each level is trusted
        in proportion to how much evidence it actually holds, so a bucket seen
        twice barely counts and one seen fifty times dominates.
        """
        vibe = embed.vibe_of(self.conn, track_id)
        if vibe is None:
            return 0.0
        buckets = ctxmod.buckets(ctx)
        if not buckets:
            return 0.0  # a moment with no known time teaches nothing
        rows = {
            r["bucket"]: r
            for r in self.conn.execute(
                "SELECT bucket, pos, neg FROM ctx_pref WHERE vibe_id = ? AND bucket IN"
                " (" + ",".join("?" * len(buckets)) + ")",
                [vibe] + buckets,
            )
        }
        num = den = 0.0
        for bucket, lw in zip(buckets, ctxmod.BUCKET_WEIGHTS):
            r = rows.get(bucket)
            if not r:
                continue
            pos, neg = r["pos"] or 0.0, r["neg"] or 0.0
            n = pos + neg
            if n <= 0:
                continue
            strength = n / (n + 3.0)          # shrink towards zero when thin
            level = (pos - neg) / (n + 1.0)   # -1..+1
            num += level * lw * strength
            den += lw * strength
        if den:
            return num / den
        # Nothing has ever been learned about this exact moment. Rather than
        # shrug, borrow from the moments most like it -- Tuesday morning can
        # reasonably lean on what Wednesday morning taught us. Discounted,
        # because borrowed evidence is weaker than evidence.
        return 0.6 * self._borrowed_context_fit(vibe, buckets[0])

    def _borrowed_context_fit(self, vibe, want):
        """Similarity-weighted preference from other buckets for this vibe."""
        rows = self.conn.execute(
            "SELECT bucket, pos, neg FROM ctx_pref WHERE vibe_id = ?", (vibe,)
        ).fetchall()
        num = den = 0.0
        for r in rows:
            sim = ctxmod.bucket_similarity(want, r["bucket"])
            if sim <= 0.35:
                continue
            pos, neg = r["pos"] or 0.0, r["neg"] or 0.0
            n = pos + neg
            if n <= 0:
                continue
            strength = n / (n + 3.0)
            level = (pos - neg) / (n + 1.0)
            num += level * sim * strength
            den += sim * strength
        return num / den if den else 0.0

    # ------------------------------------------------------------ semantic

    def moment_centroid(self, ctx, limit=500):
        """The average sound of what you actually finish at moments like this.

        Each past play is weighted by how similar its context was to now, how
        recent it is, and whether you saw it through. The result is a single
        vector we can measure any candidate against, including songs with no
        history at all.
        """
        key = "|".join(ctxmod.buckets(ctx)[:2])
        if key in self._centroid_cache:
            return self._centroid_cache[key]

        rows = self.conn.execute(
            "SELECT p.track_id, p.completion, p.started_at, p.ctx, p.outcome, e.vec"
            " FROM plays p JOIN embeddings e ON e.track_id = p.track_id"
            " ORDER BY p.started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        if not rows:
            self._centroid_cache[key] = None
            return None

        hl = self.cfg["affinity_half_life_days"]
        liked = self.cfg["completion_liked"]
        acc, wsum = None, 0.0
        for r in rows:
            try:
                pctx = json.loads(r["ctx"] or "{}")
            except Exception:
                pctx = {}
            sim = ctxmod.similarity(pctx, ctx)
            if sim <= 0.15:
                continue
            recency = _decay(_age_days(r["started_at"]), hl)
            quality = 1.0 if (r["outcome"] == "completed" or (r["completion"] or 0) >= liked) \
                else ((r["completion"] or 0) - 0.4)
            w = sim * recency * quality
            if w <= 0:
                continue
            v = db.unpack(r["vec"]) * w
            acc = v if acc is None else acc + v
            wsum += w

        if acc is None or wsum <= 0:
            self._centroid_cache[key] = None
            return None
        vec = embed.normalize(acc / wsum)
        self._centroid_cache[key] = vec
        return vec

    def semantic_fit(self, vec, centroid):
        if vec is None or centroid is None:
            return 0.0
        return float(np.dot(vec, centroid))

    # -------------------------------------------------------------- sound

    def feature_target(self, ctx, limit=400):
        """The *sound* you actually finish at moments like this.

        The semantic centroid above knows which artists sit near each other;
        this knows whether you want something loud and bright or calm and warm
        right now, which is the question a DJ is really answering at eleven at
        night. Same weighting: context similarity, recency, and whether you
        saw the track through.
        """
        key = "f|" + "|".join(ctxmod.buckets(ctx)[:2])
        if key in self._centroid_cache:
            return self._centroid_cache[key]

        rows = self.conn.execute(
            "SELECT p.completion, p.started_at, p.ctx, p.outcome,"
            " f.energy, f.brightness, f.dynamics, f.punch"
            " FROM plays p JOIN features f ON f.track_id = p.track_id"
            " ORDER BY p.started_at DESC LIMIT ?", (limit,),
        ).fetchall()
        if not rows:
            self._centroid_cache[key] = None
            return None

        hl = self.cfg["affinity_half_life_days"]
        liked = self.cfg["completion_liked"]
        acc, wsum = np.zeros(4, dtype=np.float64), 0.0
        for r in rows:
            try:
                pctx = json.loads(r["ctx"] or "{}")
            except Exception:
                continue
            sim = ctxmod.similarity(pctx, ctx)
            if sim <= 0.15:
                continue
            good = (r["outcome"] == "completed"
                    or (r["completion"] or 0) >= liked)
            if not good:
                continue  # only learn the sound of what you actually kept
            w = sim * _decay(_age_days(r["started_at"]), hl)
            if w <= 0:
                continue
            acc += w * np.array([r["energy"] or 0.0, r["brightness"] or 0.0,
                                 r["dynamics"] or 0.0, r["punch"] or 0.0])
            wsum += w

        target = (acc / wsum).astype(np.float32) if wsum > 0 else None
        self._centroid_cache[key] = target
        return target

    @staticmethod
    def feature_fit(vec, target):
        """How close a track's sound is to what the moment wants. -1..+1.

        Energy is weighted hardest because it is what people actually notice;
        being wrong about brightness is a nuance, being wrong about energy
        means the wrong song entirely.
        """
        if vec is None or target is None:
            return 0.0
        weights = np.array([0.55, 0.20, 0.10, 0.15], dtype=np.float32)
        dist = float(np.sqrt(np.sum(weights * (vec - target) ** 2)))
        return float(np.clip(1.0 - 2.6 * dist, -1.0, 1.0))

    # ------------------------------------------------------- time dynamics

    def last_played(self, track_id):
        r = self.conn.execute(
            "SELECT MAX(started_at) AS t FROM plays WHERE track_id = ?", (track_id,)
        ).fetchone()
        return r["t"] if r else None

    def dormancy(self, track_id, affinity, last_played):
        """The 'you loved this and forgot it existed' bonus.

        Deliberately gated on affinity: resurfacing something you skipped
        eight months ago is not a pleasant surprise, it is a bad memory.
        """
        if affinity <= 0.05 or not last_played:
            return 0.0
        days = _age_days(last_played) or 0.0
        sweet = self.cfg["dormancy_sweet_spot_days"]
        return affinity * min(1.0, days / max(1.0, sweet))

    def fatigue(self, track_id, artist, recent_artists, recent_track_days):
        """Penalty for over-rotation, of the song and of the artist."""
        pen = 0.0
        days = recent_track_days.get(track_id)
        if days is not None:
            # Steep for the first few days, gone after about three weeks.
            pen += 1.6 * math.exp(-days / 5.0)
        key = (artist or "").split(",")[0].strip().lower()
        if key:
            pen += 0.45 * min(3, recent_artists.get(key, 0))
        return pen

    # --------------------------------------------------------------- score

    def _recent_maps(self, days=21, session_artists=None):
        since = datetime.now(timezone.utc).timestamp() - days * 86400
        track_days, artists = {}, {}
        for r in self.conn.execute(
            "SELECT p.track_id, p.started_at, t.artist FROM plays p"
            " JOIN tracks t ON t.id = p.track_id ORDER BY p.started_at DESC LIMIT 800"
        ):
            d = _age_days(r["started_at"])
            if d is None or d > days:
                continue
            if r["track_id"] not in track_days:
                track_days[r["track_id"]] = d
            key = (r["artist"] or "").split(",")[0].strip().lower()
            if key:
                artists[key] = artists.get(key, 0) + 1
        for a in (session_artists or []):
            key = (a or "").split(",")[0].strip().lower()
            if key:
                artists[key] = artists.get(key, 0) + 2
        return track_days, artists

    def rank(self, track_ids, ctx, session_artists=None, exclude=(), limit=None,
             discovery_bias=None, energy_steer=None, era=None, terms=None):
        """Score candidates for this moment. Returns rows sorted best-first."""
        if not track_ids:
            return []
        w = self.cfg
        centroid = self.moment_centroid(ctx)
        target = self.feature_target(ctx)
        if energy_steer is not None:
            # "something calmer" overrides what history would have wanted,
            # without discarding the rest of the sound profile.
            base = (target if target is not None
                    else np.array([0.5, 0.5, 0.4, 0.4], dtype=np.float32))
            target = base.copy()
            target[0] = float(np.clip(energy_steer, 0.0, 1.0))
        track_days, recent_artists = self._recent_maps(session_artists=session_artists)
        min_replay = w["min_replay_hours"] / 24.0
        explore_w = w["w_explore"] if discovery_bias is None else w["w_explore"] * discovery_bias

        term_list = [t for t in (terms or "").lower().split() if len(t) > 2]
        # Artists you have actually played. Used to spot a namespace
        # collision: searching "chalga" turns up a Hungarian band literally
        # called Chalga, which matches the word perfectly and is not remotely
        # the genre. An act whose name simply *is* the search term, and whom
        # you have never played, is almost always that.
        played_artists = set()
        if term_list:
            # "Listened to" has to mean actually listened to. Counting any
            # play at all let a track that was started and skipped after two
            # seconds vouch for its own artist, which is exactly backwards.
            played_artists = {
                (r["artist"] or "").split(",")[0].strip().lower()
                for r in self.conn.execute(
                    "SELECT DISTINCT t.artist FROM tracks t"
                    " JOIN plays p ON p.track_id = t.id"
                    " WHERE t.artist <> '' AND (p.outcome = 'completed'"
                    "   OR p.completion >= ?)", (self.cfg["completion_liked"],))
            }
        ids = [t for t in track_ids if t not in set(exclude)]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT id, video_id, title, artist, album, duration_sec, thumbnail,"
            f" year, banned FROM tracks WHERE id IN ({marks})", ids
        ).fetchall()

        vec_rows = self.conn.execute(
            f"SELECT track_id, vec FROM embeddings WHERE track_id IN ({marks})", ids
        ).fetchall()
        vecs = {r["track_id"]: db.unpack(r["vec"]) for r in vec_rows}

        feat_rows = self.conn.execute(
            f"SELECT track_id, energy, brightness, dynamics, punch"
            f" FROM features WHERE track_id IN ({marks})", ids
        ).fetchall()
        feats = {r["track_id"]: np.array(
            [r["energy"] or 0.0, r["brightness"] or 0.0,
             r["dynamics"] or 0.0, r["punch"] or 0.0], dtype=np.float32)
            for r in feat_rows}

        out = []
        for r in rows:
            tid = r["id"]
            if r["banned"]:
                continue
            last = self.last_played(tid)
            if last is not None:
                age = _age_days(last)
                if age is not None and age < min_replay:
                    continue  # heard it too recently, full stop

            aff = self.affinity(tid)
            cfit = self.context_fit(tid, ctx)
            sfit = self.semantic_fit(vecs.get(tid), centroid)
            dorm = self.dormancy(tid, aff, last)
            unheard = last is None
            expl = 1.0 if unheard else 0.0
            fat = self.fatigue(tid, r["artist"], recent_artists, track_days)
            sound = self.feature_fit(feats.get(tid), target)

            # When you have asked for something specific, that outranks what
            # the hour would otherwise suggest -- including for the unheard
            # tracks, which is the whole point of asking.
            focus = 0.0
            if era or term_list:
                row = {"title": r["title"], "album": r["album"],
                       "year": r["year"]}
                focus += 1.6 * qmod.era_evidence(row, era)
                if term_list:
                    hay = f"{r['title']} {r['artist']} {r['album']}".lower()
                    hits = sum(1 for t in term_list if t in hay)
                    focus += 1.2 * (hits / len(term_list))
                    artist_key = (r["artist"] or "").split(",")[0].strip().lower()
                    if (artist_key and artist_key in term_list
                            and artist_key not in played_artists):
                        # Decisive, not a nudge: these are definitively the
                        # wrong results, and the text embedding actively
                        # favours them because the name IS the query.
                        focus -= 3.0

            total = (focus
                     + w.get("w_sound", 0.9) * sound
                     + w["w_affinity"] * aff
                     + w["w_context"] * cfit
                     + w["w_semantic"] * sfit
                     + w["w_dormancy"] * dorm
                     + explore_w * expl
                     - w["w_fatigue"] * fat)

            out.append({
                "track_id": tid,
                "video_id": r["video_id"],
                "title": r["title"],
                "artist": r["artist"],
                "album": r["album"],
                "duration_sec": r["duration_sec"],
                "thumbnail": r["thumbnail"],
                "score": round(total, 4),
                "unheard": unheard,
                "last_played": last,
                "energy": (round(float(feats[tid][0]), 3)
                           if tid in feats else None),
                "parts": {
                    "sound": round(sound, 3),
                    "affinity": round(aff, 3),
                    "context": round(cfit, 3),
                    "semantic": round(sfit, 3),
                    "dormancy": round(dorm, 3),
                    "explore": round(expl, 3),
                    "fatigue": round(-fat, 3),
                    "focus": round(focus, 3),
                },
            })

        out.sort(key=lambda d: d["score"], reverse=True)
        return out[:limit] if limit else out

    # -------------------------------------------------------------- learning

    def record(self, track_id, ctx, completion, outcome, requested=False):
        """Fold one finished play into the learned context preferences."""
        vibe = embed.vibe_of(self.conn, track_id)
        if vibe is None:
            return
        liked, skipped = self.cfg["completion_liked"], self.cfg["completion_skipped"]
        if outcome == "completed" or completion >= liked:
            signal = 1.0
        elif completion <= skipped:
            signal = -1.0
        else:
            return  # ambiguous middle: teaches nothing, so record nothing
        if requested:
            signal = max(signal, 1.0) * 1.5

        ts = db.now()
        with self.conn:
            for bucket, lw in zip(ctxmod.buckets(ctx), ctxmod.BUCKET_WEIGHTS):
                pos = lw * signal if signal > 0 else 0.0
                neg = lw * -signal if signal < 0 else 0.0
                self.conn.execute(
                    "INSERT INTO ctx_pref (vibe_id, bucket, pos, neg, updated)"
                    " VALUES (?,?,?,?,?)"
                    " ON CONFLICT(vibe_id, bucket) DO UPDATE SET"
                    " pos = pos + excluded.pos, neg = neg + excluded.neg,"
                    " updated = excluded.updated",
                    (vibe, bucket, pos, neg, ts),
                )
        self.invalidate()

    def record_feedback(self, track_id, kind, ctx):
        """Explicit thumbs teach the context model directly and immediately."""
        db.add_feedback(self.conn, track_id, kind, ctx, ctxmod.primary_bucket(ctx))
        vibe = embed.vibe_of(self.conn, track_id)
        val = FEEDBACK_VALUE.get(kind, 0.0)
        if vibe is not None and val:
            ts = db.now()
            with self.conn:
                for bucket, lw in zip(ctxmod.buckets(ctx), ctxmod.BUCKET_WEIGHTS):
                    mag = lw * min(2.0, abs(val)) * 0.5
                    self.conn.execute(
                        "INSERT INTO ctx_pref (vibe_id, bucket, pos, neg, updated)"
                        " VALUES (?,?,?,?,?)"
                        " ON CONFLICT(vibe_id, bucket) DO UPDATE SET"
                        " pos = pos + excluded.pos, neg = neg + excluded.neg,"
                        " updated = excluded.updated",
                        (vibe, bucket, mag if val > 0 else 0.0,
                         mag if val < 0 else 0.0, ts),
                    )
        self.invalidate()


def rebuild_ctx_pref(conn, cfg=None):
    """Recompute every learned context preference from the play log.

    This has to exist because vibe ids are not stable: re-clustering renumbers
    them, so preferences learned against the old numbering would silently end
    up attached to the wrong clusters. Rather than try to migrate them, we
    throw them away and replay history, which is cheap and always correct.
    Call this after embed.rebuild_vibes, and after any bulk import.
    """
    cfg = cfg or load()
    tcfg = cfg["taste"]
    liked, skipped = tcfg["completion_liked"], tcfg["completion_skipped"]

    vibes = {r["track_id"]: r["vibe_id"]
             for r in conn.execute("SELECT track_id, vibe_id FROM track_vibe")}
    if not vibes:
        return 0

    acc = {}

    def bump(vibe, bucket, lw, signal):
        cell = acc.setdefault((vibe, bucket), [0.0, 0.0])
        if signal > 0:
            cell[0] += lw * signal
        else:
            cell[1] += lw * -signal

    rows = conn.execute(
        "SELECT track_id, completion, outcome, ctx, requested FROM plays"
    ).fetchall()
    used = 0
    for r in rows:
        vibe = vibes.get(r["track_id"])
        if vibe is None:
            continue
        completion = r["completion"] or 0.0
        if r["outcome"] == "completed" or completion >= liked:
            signal = 1.0
        elif completion <= skipped:
            signal = -1.0
        else:
            continue
        if r["requested"]:
            signal = max(signal, 1.0) * 1.5
        try:
            ctx = json.loads(r["ctx"] or "{}")
        except Exception:
            continue
        if not ctx:
            continue
        for bucket, lw in zip(ctxmod.buckets(ctx), ctxmod.BUCKET_WEIGHTS):
            bump(vibe, bucket, lw, signal)
        used += 1

    for r in conn.execute("SELECT track_id, kind, weight, ctx FROM feedback"):
        vibe = vibes.get(r["track_id"])
        val = FEEDBACK_VALUE.get(r["kind"], 0.0)
        if vibe is None or not val:
            continue
        try:
            ctx = json.loads(r["ctx"] or "{}")
        except Exception:
            continue
        if not ctx:
            continue
        for bucket, lw in zip(ctxmod.buckets(ctx), ctxmod.BUCKET_WEIGHTS):
            mag = lw * min(2.0, abs(val)) * 0.5
            bump(vibe, bucket, 1.0, mag if val > 0 else -mag)

    ts = db.now()
    with conn:
        conn.execute("DELETE FROM ctx_pref")
        conn.executemany(
            "INSERT INTO ctx_pref (vibe_id, bucket, pos, neg, updated) VALUES (?,?,?,?,?)",
            [(v, b, p, n, ts) for (v, b), (p, n) in acc.items()],
        )
    return used


def explain(row, ctx):
    """One plain line saying why this track surfaced. Built from the numbers,
    so it is always honest and never waits on a model."""
    p = row["parts"]
    if p.get("focus", 0) > 0.8:
        return "matches what you asked for"
    if p.get("sound", 0) > 0.55 and row.get("energy") is not None:
        level = ("calm" if row["energy"] < 0.35 else "gentle" if row["energy"] < 0.55
                 else "lively" if row["energy"] < 0.75 else "high-energy")
        return f"{level}, which is what you keep at moments like this"
    if row.get("unheard"):
        if p["semantic"] > 0.35:
            return "new to you, and it sits right next to what you have been finishing lately"
        return "new to you"
    if p["dormancy"] > 0.35:
        last = row.get("last_played")
        days = _age_days(last)
        if days and days > 30:
            return f"you liked this and have not heard it in {int(days)} days"
        return "you liked this and have not heard it in a while"
    if p["context"] > 0.3:
        return f"this is what you go for at this time of day ({ctx.get('part')})"
    if p["affinity"] > 0.4:
        return "a favourite of yours"
    if p["semantic"] > 0.3:
        return "close to what you have been playing lately"
    return "fits the moment"

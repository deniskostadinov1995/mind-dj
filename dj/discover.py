"""Finding songs you have not heard.

The DJ can only ever choose from what it knows about, so something has to keep
widening the library. Three sources, in descending order of trust:

  radio      YouTube Music's own station for a track you already love
  neighbours top songs by artists related to artists you already love
  moods      YouTube Music's mood shelves, used only to break out of a rut

Everything harvested is stored and embedded, then ranked by the same scoring
engine as the rest of the library. Nothing gets played just because it is new.
"""
import random
from datetime import datetime, timezone

from . import db, embed
from .catalog import Catalog


class Prospector:
    def __init__(self, conn=None, taste=None, cfg=None, catalog=None, embedder=None):
        self.taste = taste
        self.cfg = cfg
        self.cat = catalog or Catalog(cfg)
        self.emb = embedder or embed.Embedder(cfg)
        self.last_error = None
        self.last_run = None
        self.harvested = 0

    @property
    def conn(self):
        """Always the calling thread's connection -- see db.connect()."""
        return db.connect()

    # ------------------------------------------------------------- seeding

    def seed_tracks(self, ctx, n=6):
        """Pick the tracks whose neighbourhoods are worth exploring right now.

        Weighted by how well they score in the current context, so at midnight
        we go prospecting around the things you play at midnight.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT p.track_id FROM plays p"
            " JOIN tracks t ON t.id = p.track_id"
            " WHERE t.banned = 0 AND p.completion >= ?"
            " ORDER BY p.started_at DESC LIMIT 300",
            (self.cfg["taste"]["completion_liked"],),
        ).fetchall()
        ids = [r["track_id"] for r in rows]
        if not ids:
            ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM tracks WHERE banned = 0 ORDER BY RANDOM() LIMIT 40")]
        if not ids:
            return []
        ranked = self.taste.rank(ids, ctx, limit=40)
        if not ranked:
            return []
        # Mostly the best fits, with a little randomness so we do not keep
        # mining the same three songs forever.
        top = ranked[: max(n * 3, 12)]
        random.shuffle(top)
        return top[:n]

    # ----------------------------------------------------------- harvesting

    def harvest(self, ctx, budget=3, per_seed=30):
        """Pull new candidate tracks into the library. Returns how many are new."""
        added = 0
        seeds = self.seed_tracks(ctx, n=budget)
        for s in seeds:
            for t in self.cat.radio(s["video_id"], limit=per_seed):
                if self._store(t):
                    added += 1
        self.harvested += added
        self.last_run = datetime.now(timezone.utc).isoformat()
        return added

    def harvest_neighbours(self, ctx, artists=2, per_artist=8):
        """Widen sideways: artists adjacent to the ones you actually finish."""
        added = 0
        seeds = self.seed_tracks(ctx, n=artists)
        for s in seeds:
            primary = (s.get("artist") or "").split(",")[0].strip()
            if not primary:
                continue
            for name in self.cat.related_artists(primary, limit=3):
                for t in self.cat.artist_top(name, limit=per_artist):
                    if self._store(t):
                        added += 1
        self.harvested += added
        return added

    def harvest_mood(self, mood, limit=40):
        """Deliberate rut-breaker, driven from the UI or a voice request."""
        added = 0
        for t in self.cat.mood_playlist_tracks(mood, limit=limit):
            if self._store(t):
                added += 1
        return added

    def harvest_query(self, query, limit=15):
        """Direct search, for when you ask for something by name."""
        added, ids = 0, []
        for t in self.cat.search(query, limit=limit):
            tid = self._store(t, return_id=True)
            if tid:
                ids.append(tid)
                added += 1
            else:
                row = db.get_track_by_video(self.conn, t["video_id"])
                if row:
                    ids.append(row["id"])
        return added, ids

    def _store(self, track, return_id=False):
        """Insert a harvested track if it is genuinely new. Embeds immediately
        so it can be scored on the very next selection pass."""
        if not track or not track.get("video_id"):
            return None if return_id else False
        if db.get_track_by_video(self.conn, track["video_id"]):
            return None if return_id else False
        # Same song under a different id? Then it is not new.
        if db.find_by_ident(self.conn, track.get("artist", ""), track["title"]):
            return None if return_id else False

        tid = db.upsert_track(
            self.conn, track["video_id"], track["title"], track.get("artist", ""),
            track.get("album", ""), track.get("duration_sec", 0),
            track.get("thumbnail", ""), track.get("year"), source="discover",
        )
        vec = self.emb.ensure(self.conn, db.get_track(self.conn, tid))
        if vec is not None:
            embed.assign_vibe(self.conn, tid, vec)
        return tid if return_id else True

    # ------------------------------------------------------------ selection

    def pool(self, ctx, want_unheard=None, cap=1200):
        """Candidate track ids for the next selection pass.

        Deliberately generous: ranking is cheap, and a wide pool is what lets
        a forgotten favourite from two years ago beat today's obvious choice.
        """
        rows = self.conn.execute(
            "SELECT id FROM tracks WHERE banned = 0 AND unplayable = 0"
            " ORDER BY RANDOM() LIMIT ?", (cap,)
        ).fetchall()
        ids = [r["id"] for r in rows]

        # Always include everything you have actually liked, regardless of the
        # random sample, so favourites can never be crowded out by noise.
        loved = self.conn.execute(
            "SELECT DISTINCT p.track_id FROM plays p WHERE p.completion >= ?"
            " ORDER BY p.started_at DESC LIMIT 400",
            (self.cfg["taste"]["completion_liked"],),
        ).fetchall()
        ids.extend(r["track_id"] for r in loved)
        return list(dict.fromkeys(ids))

    def unheard_count(self):
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tracks t"
            " WHERE t.banned = 0 AND NOT EXISTS"
            " (SELECT 1 FROM plays p WHERE p.track_id = t.id)"
        ).fetchone()
        return row["n"] if row else 0

    def ensure_supply(self, ctx, floor=150):
        """Top the library up when the well of unheard songs runs low."""
        n = self.unheard_count()
        if n >= floor:
            return 0
        try:
            added = self.harvest(ctx, budget=3)
            if added < 20:
                added += self.harvest_neighbours(ctx, artists=2)
            return added
        except Exception as e:
            self.last_error = str(e)
            return 0

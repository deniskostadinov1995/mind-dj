"""The DJ itself: what plays now, what plays next, and what that teaches.

One background thread watches the active sink, notices when a track ends or
gets skipped, writes the play to the database with the context it started in,
folds that into the learned preferences, and picks the next track.

The context is captured when a track *starts*, not when it ends, because that
is the moment the choice was made and the moment we want to learn about.
"""
import random
import threading
import time
import uuid
from datetime import datetime, timezone

from . import context as ctxmod
from . import query as qmod
from . import audio, db, embed
from .brain import Brain, fallback_request
from .catalog import Catalog
from .config import load
from .discover import Prospector
from .ha import HA
from .sinks.airplay_sink import AirPlaySink
from .sinks.ha_sinks import AlexaSink, CastSink, WebOSSink, WebSink
from .sinks.local_sink import LocalSink
from .taste import Taste, explain


class Player:
    #: No track may be declared finished before this many seconds have really
    #: passed. Nothing legitimate ends that fast, and it bounds the damage of
    #: anything that goes wrong upstream of here.
    MIN_PLAY_SECONDS = 3.0

    def __init__(self, cfg=None, on_event=None):
        self.cfg = cfg or load()
        db.init()
        self.ha = HA(self.cfg)
        self.cat = Catalog(self.cfg)
        self.emb = embed.Embedder(self.cfg)
        self.taste = Taste(self.conn, self.cfg)
        self.brain = Brain(self.cfg)
        self.prospector = Prospector(self.conn, self.taste, self.cfg,
                                     catalog=self.cat, embedder=self.emb)
        self.on_event = on_event or (lambda *_a, **_k: None)

        self.sinks = {}
        self._build_sinks()
        default = self.cfg["sinks"].get("default", "pc")
        self.active = [default] if default in self.sinks else ["pc"]
        self.timeline_t0 = None      # wall clock the track began
        self.sync_tolerance = self.cfg["sinks"].get("sync_tolerance", 1.5)
        # Seconds to leave a device alone after correcting it, so a once-a-
        # second loop cannot thrash a Cast device that takes a moment to seek.
        self._sync_cooldown = self.cfg["sinks"].get("sync_cooldown", 8.0)
        self._sync_state = {}

        self.session_id = uuid.uuid4().hex[:12]
        self.queue = []
        self.history = []
        self.current = None
        self.started_at = None
        self.start_ctx = None
        self.playing = False
        self.paused = False
        self.mode_override = None
        self.discovery_bias = 1.0
        self.last_reason = ""
        self.moment_name = None
        # Set by a spoken or typed request ("something calmer"); cleared when
        # the mood it was asked for has had its run.
        self.energy_steer = None
        self._steer_until = 0.0
        # A locked-on request: what you asked for, and until when.
        self.focus = None
        self.stream_base = None      # set by the server once it knows its LAN IP

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._last_ctx = None
        self._ctx_at = 0.0
        self._paused_at = 0.0
        self._recent_finishes = []
        self._recovery = {}
        #: Per-output head start, in seconds: how far into the song to begin
        #: an output that takes a while to get going. Learned as it plays.
        self._start_lag = {}

    @property
    def conn(self):
        """Always the calling thread's connection -- see db.connect()."""
        return db.connect()

    # ---------------------------------------------------------------- setup

    def _build_sinks(self):
        s = self.cfg["sinks"]
        deps = {"ha": self.ha}
        if s.get("pc", {}).get("enabled", True):
            self.sinks["pc"] = LocalSink(s.get("pc", {}), deps)
        if s.get("sony", {}).get("enabled", True):
            self.sinks["sony"] = CastSink(s["sony"], deps)
        if s.get("lg", {}).get("enabled", True):
            # AirPlay carries our own clean audio; the webOS app plays video
            # with ads and cannot be synced. Prefer AirPlay whenever the TV
            # has been paired for it.
            lg_cfg = s["lg"]
            # AirPlaySink picks its own route per play (AirPlay when the TV is
            # awake, the webOS YouTube app otherwise), so it is always the one
            # we build when a host is configured.
            self.sinks["lg"] = (AirPlaySink(lg_cfg, deps) if lg_cfg.get("host")
                                else WebOSSink(lg_cfg, deps))
        if s.get("echo", {}).get("enabled", True):
            self.sinks["echo"] = AlexaSink(s["echo"], deps)
        if s.get("web", {}).get("enabled", True):
            self.sinks["web"] = WebSink(s.get("web", {}), deps)

    @property
    def sink(self):
        """The primary sink: the one we time the track and learn from.

        With several outputs running at once we still need a single source of
        truth for "where are we in this song", and it has to be the one whose
        answer is worth having -- not merely the first one that has an answer.

        The order you tapped the outputs in is not evidence of anything, but
        it used to decide this. Choosing the TV before the PC handed the job
        of timing every song to a device on the far side of the network that
        we poll through Home Assistant, while the machine actually playing
        the audio, whose position we know exactly, sat there unconsulted.
        """
        best, best_trust = None, -1
        for name in self.active:
            s = self.sinks.get(name)
            if s is not None and s.reports_position and s.position_trust > best_trust:
                best, best_trust = s, s.position_trust
        if best is not None:
            return best
        for name in self.active:
            s = self.sinks.get(name)
            if s is not None:
                return s
        return None

    @property
    def sink_name(self):
        s = self.sink
        return s.name if s else (self.active[0] if self.active else "")

    def active_sinks(self):
        return [self.sinks[n] for n in self.active if n in self.sinks]

    def set_sink(self, name):
        return self.set_sinks([name])

    def set_sinks(self, names):
        """Choose which outputs play. More than one plays them together."""
        names = [n for n in (names or []) if n in self.sinks]
        if not names:
            return False
        with self._lock:
            was_playing = self.playing
            current = self.current
            position = self.position() if was_playing else 0.0
            leaving = [self.sinks[n] for n in self.active if n not in names]
            for s in leaving:
                try:
                    s.stop()
                except Exception:
                    pass
            self.active = names
            # Moving the music between rooms should not look like you skipped
            # the song, so pick it up where it left off instead of restarting.
            if was_playing and current:
                self._start_on_sinks(current, at=position, restart_timeline=False)
        self.emit("sink", {"sink": self.sink_name, "active": self.active})
        return True

    def position(self):
        """Where we are in the current track.

        The same figure the tick uses, so the page, the Home Assistant sensor
        and the end-of-track check cannot disagree with each other. A reading
        the output will vouch for wins; otherwise our own clock does, because
        an output that has just been handed the song mid-way has nothing
        useful to say yet and saying zero makes the position leap backwards.
        """
        s = self.sink
        if s is None or not self.current:
            return (s.position() if s else 0.0) or 0.0
        wall = self.elapsed()
        try:
            verified = s.remote_position()
        except Exception:
            verified = None
        if verified is None:
            return wall
        return max(0.0, min(verified, wall + 2.0))

    def elapsed(self):
        """Seconds since the current track began, by our own clock.

        Every output is aligned to this, and it is the one figure here that
        cannot be mistaken: we set it when the track started and carry it
        forward when you unpause. Nothing can be further into a song than the
        song has been playing, which makes this the ceiling on any position a
        device reports and the fallback whenever a device will not say.
        """
        if self.timeline_t0 is None:
            return 0.0
        if self.paused:
            return max(0.0, getattr(self, "_paused_at", 0.0) or 0.0)
        return max(0.0, time.time() - self.timeline_t0)

    # -------------------------------------------------------------- context

    def context(self, max_age=10):
        """Current moment, cached briefly so a tick does not hammer HA."""
        if self._last_ctx and (time.time() - self._ctx_at) < max_age:
            return self._last_ctx
        ctx = ctxmod.snapshot(self.ha, override_mode=self.mode_override)
        self._last_ctx = ctx
        self._ctx_at = time.time()
        return ctx

    # ------------------------------------------------------------ selection

    def pick(self, n=1, ctx=None, exclude=(), energy_steer=None):
        """Choose the next n tracks for this moment."""
        ctx = ctx or self.context()
        focus = self.focus if self.focus_active() else None
        if focus:
            # Top the focus up before choosing, so later picks and the unheard
            # ones come from the same request rather than the general library.
            if len(focus["track_ids"]) < 12:
                self._fill_focus()
            pool = list(focus["track_ids"])
            if len(pool) < 6:
                # Not enough came back; widen to library tracks that still
                # match, rather than playing nothing.
                pool += self.prospector.pool(ctx)
        else:
            pool = self.prospector.pool(ctx)
        skip = set(exclude) | {t["track_id"] for t in self.queue}
        if self.current:
            skip.add(self.current["track_id"])
        skip.update(self.history[-25:])

        ranked = self.taste.rank(
            pool, ctx,
            session_artists=[t.get("artist") for t in self.history_tracks(12)],
            exclude=skip, limit=60, discovery_bias=self.discovery_bias,
            energy_steer=(energy_steer if energy_steer is not None
                          else self.energy_steer),
            era=focus["era"] if focus else None,
            terms=focus["terms"] if focus else None,
        )
        if not ranked:
            return []

        want_new = self.cfg["taste"]["discovery_ratio"]
        chosen = []
        # Sequencing: a DJ does not just pick good songs, it orders them. Start
        # from whatever is playing and move in small steps, drifting the way the
        # moment wants -- down as the evening winds down, up at a party.
        last_energy = None
        if self.current and self.current.get("energy") is not None:
            last_energy = self.current["energy"]
        drift = self._energy_drift(ctx)

        for _ in range(n):
            remaining = [r for r in ranked if r["track_id"] not in
                         {c["track_id"] for c in chosen}]
            if not remaining:
                break
            # Softmax-ish pick from the top so it does not become a fixed
            # playlist, then honour the discovery ratio when it is due.
            want_unheard = random.random() < want_new
            bucket = [r for r in remaining[:25] if r["unheard"] == want_unheard]
            pool_ = bucket or remaining[:12]
            weights = []
            for r in pool_:
                w = max(0.05, r["score"] - pool_[-1]["score"] + 0.1)
                if last_energy is not None and r.get("energy") is not None:
                    want = min(1.0, max(0.0, last_energy + drift))
                    jump = abs(r["energy"] - want)
                    # A gentle preference, not a rule: big leaps get quieter,
                    # they do not get banned.
                    w *= max(0.25, 1.0 - 1.4 * jump)
                weights.append(w)
            pick = random.choices(pool_, weights=weights, k=1)[0]
            pick["reason"] = explain(pick, ctx)
            chosen.append(pick)
            if pick.get("energy") is not None:
                last_energy = pick["energy"]
        return chosen

    @staticmethod
    def _energy_drift(ctx):
        """Which way the energy should move between songs, per track."""
        activity = ctx.get("activity")
        if activity in ("wind_down", "sleep"):
            return -0.05
        if activity in ("party", "workout"):
            return 0.04
        if activity == "focus":
            return 0.0
        return 0.0

    def history_tracks(self, n=10):
        if not self.history:
            return []
        ids = self.history[-n:]
        marks = ",".join("?" * len(ids))
        return [dict(r) for r in self.conn.execute(
            f"SELECT id AS track_id, title, artist FROM tracks WHERE id IN ({marks})", ids
        )]

    def fill_queue(self, target=None):
        target = target or self.cfg["taste"]["queue_target"]
        if len(self.queue) >= target:
            return 0
        picks = self.pick(n=target - len(self.queue))
        self.queue.extend(picks)
        if picks:
            self.emit("queue", {"queue": self.queue})
            self.prefetch()
        return len(picks)

    # ------------------------------------------------------------- playback

    def start(self, track=None):
        with self._lock:
            if self.current:
                self._finish_current("stopped")
            if track is None:
                if not self.queue:
                    self.fill_queue()
                if not self.queue:
                    self.emit("error", {"message": "nothing to play yet"})
                    return False
                track = self.queue.pop(0)
            return self._begin(track)

    def _url_for(self, name, video_id):
        """The right address for this sink to fetch audio from."""
        if name == "sony":
            # The Sony's Cast receiver will not play our m4a -- it loads it,
            # reports no error and never starts -- so this one output is fed
            # the transcoded MP3 instead. See CastSink for the full story.
            return self._stream_url(video_id, mp3=True)
        if name == "web":
            # Fetched by another process, so it has to be an address on the
            # network carrying the access token, not localhost.
            return self._stream_url(video_id)
        return self.cat.stream_url(video_id)

    def _start_on_sinks(self, track, at=0.0, restart_timeline=True):
        """Start this track on every active sink, aligned to one timeline."""
        if restart_timeline:
            self.timeline_t0 = time.time() - (at or 0.0)
        # A new track means any pending correction measurement is meaningless,
        # and every device needs its settling time again.
        now = time.time()
        for st in self._sync_state.values():
            st["at"], st["target"] = now, None
            # One head-start adjustment per track, from a settled measurement.
            st["tuned"], st["bias"], st["n"] = False, 0.0, 0
        self._recovery = {}
        started, failed = [], []
        for name in list(self.active):
            sink = self.sinks.get(name)
            if sink is None:
                continue
            url = None
            if sink.streams_from_us:
                url = self._url_for(name, track["video_id"])
                if not url:
                    failed.append((name, "could not resolve a stream"))
                    continue
            # A device that is always three seconds late is not drifting,
            # it is starting late. Begin it that far into the song and it
            # lands in step, instead of being interrupted and corrected once
            # a minute for the rest of the evening.
            offset = max(0.0, at + self._start_lag.get(name, 0.0)) \
                if sink.can_sync else 0.0
            try:
                ok = sink.play(track, url, start_at=offset)
            except Exception as e:
                ok, sink.last_error = False, str(e)
            (started if ok else failed).append(
                name if ok else (name, sink.last_error or "play failed"))
        for name, why in failed:
            self.emit("error", {"message": f"{name}: {why}"})
        return started

    def _begin(self, track):
        if not self.active_sinks():
            self.emit("error", {"message": "no output selected"})
            return False

        started = self._start_on_sinks(track, at=0.0, restart_timeline=True)
        if not started:
            if "pc" in self.active:
                self._mark_unplayable(track["track_id"])
            return False

        self.current = track
        self.started_at = datetime.now(timezone.utc)
        self.start_ctx = self.context(max_age=0)
        self.playing = True
        self.paused = False
        self.last_reason = track.get("reason", "")
        self._ensure_thread()
        self.prefetch()
        self.emit("now", self.state())
        return True

    def prefetch(self, depth=2):
        """Resolve upcoming stream URLs in the background.

        yt-dlp takes about 1.7s per track. Doing that when you press Skip
        means 1.7s of silence; doing it while the current song plays means
        none, because Catalog.stream_url caches and the next call is a hit.
        """
        upcoming = [(t["video_id"], t.get("track_id"))
                    for t in self.queue[:depth] if t.get("video_id")]
        if not upcoming:
            return

        def work():
            for vid, tid in upcoming:
                if self._stop.is_set():
                    return
                try:
                    info = self.cat.stream_info(vid)
                except Exception:
                    continue
                if not info or not info.get("url"):
                    continue
                # Measure it while we have the stream open anyway. Doing this
                # here means the library analyses itself as you listen, with no
                # bulk run hammering YouTube.
                try:
                    conn = db.connect()
                    if tid and not db.get_features(conn, tid):
                        feat = audio.measure(info["url"], info.get("headers"))
                        if feat:
                            db.save_features(conn, tid, feat)
                            self.taste.invalidate()
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True, name="dj-prefetch").start()

    def _publish_stream_base(self):
        """Tell sinks that fetch audio themselves where to find us."""
        token = (self.cfg["server"].get("access_token") or "").strip()
        for sink in self.sinks.values():
            if hasattr(sink, "stream_base"):
                sink.stream_base = self.stream_base
                sink.token = token

    def _stream_url(self, video_id, lan=False, mp3=False):
        """The URL an external device should fetch audio from.

        Must carry the access token as a query parameter: a Cast device or a
        TV has no cookie and sends no headers of ours, so with a token set it
        would simply get a 403 and play silence.
        """
        base = self.stream_base
        if not base:
            return None
        path = f"/audio/{video_id}.mp3" if mp3 else f"/stream/{video_id}"
        token = (self.cfg["server"].get("access_token") or "").strip()
        if token:
            return f"{base}{path}?key={token}"
        return f"{base}{path}"

    def _mark_unplayable(self, track_id):
        with self.conn:
            self.conn.execute("UPDATE tracks SET unplayable = 1 WHERE id = ?", (track_id,))

    def _finish_current(self, outcome, listened=None):
        """Write the finished play down and learn from it."""
        if not self.current or not self.started_at:
            return
        track = self.current
        sink = self.sink
        if listened is None:
            # Time since the track started, not whatever the output claims:
            # a device reporting someone else's position would otherwise
            # record a skip after two seconds as a full and happy listen.
            listened = self.elapsed()
            if not listened and sink:
                listened = sink.position() or 0.0
        duration = track.get("duration_sec") or 0
        if duration <= 0 and sink and hasattr(sink, "duration"):
            duration = sink.duration() or 0
        listened = max(0.0, min(listened, duration or listened))

        ctx = self.start_ctx or self.context()
        completion = (listened / duration) if duration else 0.0
        if outcome == "completed" and duration and completion < 0.5:
            outcome = "stopped"

        db.log_play(
            self.conn, track["track_id"],
            self.started_at.isoformat(), datetime.now(timezone.utc).isoformat(),
            listened_sec=listened, duration_sec=duration, outcome=outcome,
            sink=self.sink_name, session_id=self.session_id, ctx=ctx,
            bucket=ctxmod.primary_bucket(ctx), requested=int(track.get("requested", 0)),
        )
        # Sinks that cannot report position give us a weak signal at best, so
        # only teach the context model from ones that actually know.
        #
        # "not_now" is deliberately excluded: skipping because you are not in
        # the mood for a song you like is not evidence against that kind of
        # music at this hour, and treating it as such was teaching the model
        # the opposite of what you meant.
        teaches = outcome not in ("not_now",)
        if teaches and (sink is None or sink.reports_position
                        or outcome == "skipped"):
            self.taste.record(track["track_id"], ctx, completion, outcome,
                              requested=bool(track.get("requested")))
        if outcome == "not_now":
            self.taste.record_feedback(track["track_id"], "not_now", ctx)
        self.history.append(track["track_id"])
        self.current = None
        self.started_at = None
        self.emit("played", {"track": track, "completion": round(completion, 3),
                             "outcome": outcome})

    def _advance(self, auto=False):
        # A track that "finishes" by itself in a couple of seconds means
        # something is lying to us about position, and the failure mode is
        # ugly: the queue empties in under a minute while every track is
        # written down as a hundred percent listen, teaching the model from
        # plays that never happened. Better to stop and say so.
        #
        # Only tracks that ended on their own count. Pressing skip four times
        # because none of them is the right song is an ordinary thing to do
        # and must not look like a fault.
        if auto:
            now = time.time()
            self._recent_finishes = [t for t in self._recent_finishes
                                     if now - t < 60.0]
            self._recent_finishes.append(now)
            if len(self._recent_finishes) >= 4 \
                    and now - self._recent_finishes[0] < 30.0:
                self._recent_finishes = []
                self.playing = False
                self._all("stop")
                self.emit("error", {"message": "tracks kept ending the moment "
                                    "they started, so I stopped rather than "
                                    "burn through the queue. Check the output."})
                return False
        else:
            self._recent_finishes = []

        if not self.queue:
            self.fill_queue()
        if not self.queue:
            self.playing = False
            self.emit("idle", {})
            return False
        return self._begin(self.queue.pop(0))

    def _all(self, method, *args):
        """Apply an action to every active output, ignoring individual failures."""
        for sink in self.active_sinks():
            try:
                getattr(sink, method)(*args)
            except Exception as e:
                sink.last_error = str(e)

    def next(self, reason="skipped"):
        """Move on. reason is "skipped" (I do not want this), "not_now" (fine
        song, wrong moment) or "stopped" (neither, e.g. changing rooms)."""
        with self._lock:
            if self.current:
                self._finish_current(reason)
                self._all("stop")
            return self._advance()

    def stop(self):
        with self._lock:
            if self.current:
                self._finish_current("stopped")
            self._all("stop")
            self.playing = False
            self.paused = False
            self.timeline_t0 = None
        self.emit("now", self.state())

    def pause(self):
        with self._lock:
            if self.playing and not self.paused:
                self._all("pause")
                self.paused = True
                self._paused_at = self.position()
        self.emit("now", self.state())

    def resume(self):
        with self._lock:
            if self.paused:
                self._all("resume")
                self.paused = False
                # The shared clock stood still while paused; move its origin
                # forward so everyone lines up again on the same offset.
                if self.timeline_t0 is not None:
                    self.timeline_t0 = time.time() - getattr(self, "_paused_at", 0.0)
            elif not self.playing:
                self.start()
        self.emit("now", self.state())

    def set_volume(self, level):
        self._all("set_volume", level)
        return True

    # ------------------------------------------------------------- feedback

    def feedback(self, kind, track_id=None):
        tid = track_id or (self.current or {}).get("track_id")
        if not tid:
            return False
        ctx = self.start_ctx or self.context()
        self.taste.record_feedback(tid, kind, ctx)
        if kind in ("dislike", "ban", "not_now") and self.current \
                and tid == self.current["track_id"]:
            self.next(reason="skipped")
        self.emit("feedback", {"track_id": tid, "kind": kind})
        return True

    # ---------------------------------------------------------------- focus

    def set_focus(self, text, minutes=None):
        """Lock the DJ onto what you just asked for.

        Without this, a request only steers the next few tracks: the queue
        tops itself up from the whole library and drifts straight back to
        business as usual. A focus keeps every later pick -- and every newly
        discovered track -- inside the same request, which is what "chalga
        2000-2010" is asking for.
        """
        parsed = qmod.parse(text)
        if not parsed["terms"] and not parsed["era"]:
            return None
        minutes = minutes or self.cfg["taste"].get("focus_minutes", 60)
        queries = qmod.expand(parsed["terms"], parsed["era"])
        queries = self._ground_queries(parsed, queries)
        self.focus = {
            "terms": parsed["terms"],
            "era": parsed["era"],
            "queries": queries,
            "label": qmod.describe(parsed),
            "track_ids": [],
            "until": time.time() + minutes * 60,
            "exhausted": False,
        }
        self.taste.invalidate()
        return self.focus

    def _ground_queries(self, parsed, queries):
        """Anchor a genre word to the artists you actually listen to.

        Searching "chalga 2003" returns, among other things, a Hungarian band
        literally called Chalga -- the word matched a name, not a genre. Your
        own library already knows what the word means to you, so when enough
        of it matches the term, the artists you keep are searched by name for
        the era instead. That also fixes the reverse problem: a term the
        library has never heard of falls through unchanged, so asking for
        something genuinely new still works.
        """
        terms = (parsed.get("terms") or "").strip()
        era = parsed.get("era")
        if not terms or not era:
            return queries

        artists = self._artists_like(terms)

        if not artists:
            # Your history has no opinion about this word; leave the plain era
            # queries alone rather than inventing a grounding that is not there.
            return queries

        lo, hi = era
        mid = (lo + hi) // 2
        grounded = []
        for a in artists[:4]:
            grounded.append(f"{a} {lo}" if lo == hi else f"{a} {mid}")
        # Keep a couple of the literal ones too, so a genuinely new artist
        # from the right era can still surface.
        return grounded + queries[:2]

    def _artists_like(self, terms, want=4):
        """The artists a word actually means *to you*.

        Play history is the discriminator, and it has to be: searching
        "chalga" turns up a Hungarian band literally called Chalga, whose
        tracks sit closer to the word in embedding space than real chalga does
        because the artist name is the word. What separates them is that you
        have played Azis and Preslava for years and have never played that
        band once. So only artists with listening history can ground a search;
        a word your history has no opinion about grounds nothing, and the
        plain era queries are used instead.
        """
        like = f"%{terms.lower()}%"
        # An artist you actually listen to, named outright, beats inference.
        liked = self.cfg["taste"]["completion_liked"]
        exact = self.conn.execute(
            "SELECT t.artist, COUNT(p.id) plays FROM tracks t"
            " JOIN plays p ON p.track_id = t.id"
            " WHERE t.artist <> '' AND LOWER(t.artist) LIKE ?"
            "   AND (p.outcome = 'completed' OR p.completion >= ?)"
            " GROUP BY t.artist ORDER BY plays DESC LIMIT 1", (like, liked)
        ).fetchone()
        if exact and exact["plays"] >= 2:
            return [exact["artist"].split(",")[0].strip()]

        import numpy as np
        vec = self.emb.embed_text(terms)
        if vec is None:
            return []

        # Only consider tracks you have actually played, so the comparison is
        # against your taste rather than against everything ever harvested.
        rows = self.conn.execute(
            "SELECT DISTINCT t.id, t.artist FROM tracks t"
            " JOIN plays p ON p.track_id = t.id WHERE t.artist <> ''"
            "   AND (p.outcome = 'completed' OR p.completion >= ?)", (liked,)
        ).fetchall()
        if len(rows) < 15:
            return []
        played = {r["id"]: r["artist"] for r in rows}
        ids, mat = self.emb.matrix(self.conn, list(played))
        if not len(ids):
            return []
        sims = mat @ np.asarray(vec, dtype=np.float32)
        best = float(sims.max())
        # Absolute bar: this embedding rates almost any two music strings as
        # broadly similar, so anything under this is the library shrugging.
        if best < 0.62:
            return []
        scores = {}
        for i, tid in enumerate(ids):
            if sims[i] < best - 0.08:
                continue
            name = (played.get(int(tid)) or "").split(",")[0].strip()
            if name:
                scores[name] = scores.get(name, 0.0) + float(sims[i])
        return [n for n, _ in sorted(scores.items(), key=lambda kv: -kv[1])][:want]

    def clear_focus(self):
        if self.focus:
            self.focus = None
            self.taste.invalidate()
            self.emit("focus", {"focus": None})
        return True

    def focus_active(self):
        if not self.focus:
            return False
        if time.time() > self.focus["until"]:
            self.focus = None
            self.taste.invalidate()
            self.emit("focus", {"focus": None})
            return False
        return True

    def _fill_focus(self, want=25):
        """Pull more of what was asked for, cycling through the era queries."""
        f = self.focus
        if not f or f["exhausted"]:
            return 0
        found = list(f["track_ids"])
        added = 0
        for q in f["queries"]:
            if len(found) >= want:
                break
            try:
                _new, ids = self.prospector.harvest_query(q, limit=15)
            except Exception:
                continue
            added += len(ids)
            for tid in ids:
                if tid not in found:
                    found.append(tid)
        f["track_ids"] = found
        # Nothing new came back from any query: stop hammering YouTube for a
        # thing it does not have, and say so rather than silently drifting.
        if not added and not found:
            f["exhausted"] = True
        return added

    def request(self, text):
        """Handle a typed or spoken request. Returns what it decided to do."""
        ctx = self.context()
        parsed = None
        if self.brain.enabled and self.brain.available is not False:
            parsed = self.brain.parse_request(text, ctx)
        if not parsed:
            parsed = fallback_request(text)
        if not parsed or not (parsed.get("search") or parsed.get("mood")):
            return {"ok": False, "message": "could not make sense of that"}

        # "something calmer" should bend what gets picked next, not only
        # what gets searched for. It lapses after a while so one request does
        # not colour the rest of the evening.
        if parsed.get("energy") is not None:
            self.energy_steer = float(parsed["energy"])
            self._steer_until = time.time() + 45 * 60
            self.taste.invalidate()

        # Read the era out of what was typed before anything is searched:
        # "chalga 2000-2010" is a period, not three words.
        focus = self.set_focus(text)
        if focus:
            self.emit("focus", {"focus": self.focus_state()})

        found = []
        for q in (focus["queries"] if focus else (parsed.get("search") or [])):
            _added, ids = self.prospector.harvest_query(q, limit=12)
            found.extend(ids)
        if not found and parsed.get("mood"):
            self.prospector.harvest_mood(parsed["mood"], limit=40)
            found = [r["id"] for r in self.conn.execute(
                "SELECT id FROM tracks ORDER BY id DESC LIMIT 40")]
        if not found:
            return {"ok": False, "message": "found nothing for that"}

        if focus:
            focus["track_ids"] = list(dict.fromkeys(list(focus["track_ids"]) + found))
        ranked = self.taste.rank(found, ctx, limit=8,
                                 era=focus["era"] if focus else None,
                                 terms=focus["terms"] if focus else None)
        for r in ranked:
            r["requested"] = 1
            r["reason"] = "you asked for it"
        with self._lock:
            if focus:
                # Replace the queue rather than prepending to it. Anything
                # already lined up was chosen for a different question, and
                # leaving it behind is what made a specific request drift back
                # into general listening a few tracks later.
                self.queue = ranked[:8]
            else:
                self.queue = ranked[:6] + self.queue
        self.emit("queue", {"queue": self.queue})
        if not self.playing:
            self.start()
        else:
            self.next(reason="stopped")
        return {"ok": True, "parsed": parsed, "queued": len(ranked[:6]),
                "focus": self.focus_state()}

    def play_video(self, video_id, requested=True):
        """Play one specific track now."""
        row = db.get_track_by_video(self.conn, video_id)
        if not row:
            meta = self.cat.probe(video_id)
            if not meta:
                return False
            tid = db.upsert_track(self.conn, video_id, meta["title"], meta["artist"],
                                  meta["album"], meta["duration_sec"],
                                  meta["thumbnail"], meta["year"], source="manual")
            vec = self.emb.ensure(self.conn, db.get_track(self.conn, tid))
            if vec is not None:
                embed.assign_vibe(self.conn, tid, vec)
            row = db.get_track(self.conn, tid)
        track = {
            "track_id": row["id"], "video_id": row["video_id"], "title": row["title"],
            "artist": row["artist"], "album": row["album"],
            "duration_sec": row["duration_sec"], "thumbnail": row["thumbnail"],
            "reason": "you asked for it", "requested": int(requested),
            "energy": (db.get_features(self.conn, row["id"]) or {}).get("energy"),
            "parts": {},
        }
        return self.start(track)

    # ----------------------------------------------------------------- loop

    def _ensure_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="dj-loop")
        self._thread.start()

    def _run(self):
        last_supply = 0.0
        while not self._stop.is_set():
            try:
                self._tick()
                if (self.energy_steer is not None
                        and time.time() > self._steer_until):
                    # A request to change the mood should fade, not become the
                    # permanent setting for the rest of the evening.
                    self.energy_steer = None
                    self.taste.invalidate()
                if time.time() - last_supply > 300:
                    last_supply = time.time()
                    threading.Thread(
                        target=self.prospector.ensure_supply,
                        args=(self.context(),), daemon=True,
                    ).start()
            except Exception as e:
                self.emit("error", {"message": f"loop: {e}"})
            self._stop.wait(1.0)

    def _tick(self):
        with self._lock:
            if not self.playing or self.paused or not self.current:
                return
            sink = self.sink
            if sink is None:
                return
            duration = self.current.get("duration_sec") or 0
            wall = self.elapsed()

            # Prefer a reading the output will actually vouch for, and cap it
            # at the wall clock either way. A device cannot be further into a
            # song than the song has been playing, and refusing to believe
            # otherwise is what stops a stale reading from ending every track
            # the instant it starts.
            try:
                verified = sink.remote_position()
            except Exception:
                verified = None
            if verified is not None:
                pos = min(verified, wall + 2.0)
            else:
                pos = min(sink.position() or 0.0, wall + 2.0)

            done = False
            if duration and wall >= self.MIN_PLAY_SECONDS:
                if verified is not None:
                    # Treat the last two seconds as finished; streams often
                    # stop a hair short of the nominal duration.
                    done = pos >= duration - 2.0
                else:
                    # Nobody is reporting, so the clock decides. It runs a
                    # little ahead of a device that spent time buffering, so
                    # give the tail a second rather than clipping it.
                    done = wall >= duration - 1.0

            if not done and sink.finished():
                if sink.end_signal_reliable:
                    # The player process exited, or the stream we were pushing
                    # ran out. That is a fact rather than an inference -- but
                    # a fact about the stream, not necessarily about the song.
                    # Arriving a long way short of the end means the audio was
                    # cut off, so try to pick it up again before giving up.
                    if duration and wall < duration - 5.0 \
                            and self._recover(sink, pos):
                        done = False
                    else:
                        done = wall > self.MIN_PLAY_SECONDS
                        if done:
                            pos = min(pos, wall)
                elif wall > 15.0 and (not duration or wall >= duration * 0.5):
                    # An inference from a polled state string. Only once the
                    # song has plausibly run its course.
                    done = True

            if done:
                listened = min(pos if verified is not None else wall,
                               duration) if duration else max(pos, wall)
                self._finish_current("completed", listened=listened)
                self._advance(auto=True)
            else:
                self._resync(pos)
                self.emit("tick", {"position": round(pos, 1), "duration": duration})

        if len(self.queue) < 3:
            self.fill_queue()

    def _recover(self, sink, at):
        """Restart an output whose stream died, where it left off.

        Google signs its audio URLs with an expiry and will drop a connection
        outright when it decides it has served enough, and when that happens
        ffplay simply exits -- which from the outside is indistinguishable
        from a track reaching its end. Treating the two the same is how a
        song could vanish a minute before it was over and be written down as
        played in full.

        So resolve the audio afresh, bypassing the cached URL that just died,
        and carry on from the same second. Twice per track: if it will not
        stay up after that, the track really is over.
        """
        if not self.current:
            return False
        tries = self._recovery.get(sink.name, 0)
        if tries >= 2:
            return False
        self._recovery[sink.name] = tries + 1
        vid = self.current.get("video_id")
        if not vid:
            return False
        try:
            if not self.cat.stream_url(vid, force=True):
                return False
            url = self._url_for(sink.name, vid) if sink.streams_from_us else None
            ok = sink.play(self.current, url, start_at=max(0.0, at))
        except Exception as e:
            sink.last_error = str(e)
            return False
        if ok:
            self.emit("error", {"message": f"{sink.name}: stream dropped, "
                                           f"picked it up at {int(at)}s"})
        return bool(ok)

    def _resync(self, primary_pos):
        """Nudge the other outputs back into line with the primary one.

        Only sinks that can actually seek take part. The Echo is playing
        Amazon's own copy of the song -- a different file, different length,
        no seek and no position -- so it is left alone by design rather than
        fought with.
        """
        if len(self.active) < 2 or self.paused:
            return
        primary = self.sink
        now = time.time()
        for sink in self.active_sinks():
            if sink is primary or not sink.can_sync:
                continue

            # Give a freshly started or freshly seeked device time to settle.
            # Cast in particular reports a stale position for a few seconds
            # after a load, which once produced a phantom 60 second drift.
            last = self._sync_state.setdefault(
                sink.name, {"at": 0.0, "lag": 0.0, "target": None, "bias": 0.0})
            if now - last["at"] < self._sync_cooldown:
                continue

            # A stalled sink freezes its position, which reads as huge drift.
            # Seeking it would just restart the stall in a loop. And a sink
            # that cannot vouch for its reading is left alone entirely:
            # correcting a device against a guess moves one that was already
            # where it should be.
            try:
                if not sink.is_playing():
                    continue
                remote = sink.remote_position()
                if remote is None:
                    continue
                drift = primary_pos - remote
            except Exception:
                continue

            # A device cannot really be eight seconds out while playing the
            # same song we started a moment ago. Readings like that come from
            # a receiver still reporting the position of what it was playing
            # before, and both acting on one and learning from one make things
            # worse -- the correction chases a number that was never true.
            if abs(drift) > 8.0:
                continue

            # Learn how far behind this device lands after a correction, and
            # aim that far ahead next time. Without this the Sony sat at a
            # constant two seconds late: every seek was measured from a
            # position that had already moved on by the time it took effect.
            if last["target"] is not None:
                observed = max(-5.0, min(5.0, drift))
                last["lag"] = 0.7 * last["lag"] + 0.3 * observed
                last["target"] = None

            # Learn the standing offset whether or not we correct for it. A
            # gap small enough to leave alone is still a gap you can hear
            # between two speakers in one room, and it costs nothing to start
            # the next track already compensated.
            #
            # What is measured here is the error that remains *after* the head
            # start already applied, so it has to be added to that head start
            # rather than replace it. Setting the compensation equal to the
            # residual instead means a track that comes out perfectly aligned
            # reports no error, the compensation decays to nothing, and the
            # next track is late all over again -- which is exactly what it
            # did: three seconds late, then aligned, then three seconds late.
            last["bias"] = 0.7 * last.get("bias", 0.0) + 0.3 * drift
            last["n"] = last.get("n", 0) + 1
            if not last.get("tuned") and last["n"] >= 4:
                last["tuned"] = True
                # One bounded step per track. Enough to converge over a few
                # songs, too small for a single noisy measurement to undo
                # what several good ones established.
                step = max(-1.5, min(1.5, 0.6 * last["bias"]))
                self._start_lag[sink.name] = max(0.0, min(
                    8.0, self._start_lag.get(sink.name, 0.0) + step))

            tolerance = self.sync_tolerance
            if getattr(sink, "seek_is_disruptive", False):
                tolerance = max(tolerance, self.sync_tolerance * 2.5)
            if abs(drift) > tolerance:
                aim = primary_pos + max(0.0, min(4.0, last["lag"] + 0.4))
                try:
                    sink.seek(aim)
                    last["at"], last["target"] = now, aim
                    self.emit("resync", {"sink": sink.name,
                                         "drift": round(drift, 2),
                                         "lag": round(last["lag"], 2)})
                except Exception as e:
                    sink.last_error = str(e)

    def sync_report(self):
        """How far apart the outputs are right now. For the UI and for tuning."""
        primary = self.sink
        if primary is None:
            return []
        base = self.position() if self.current else (primary.position() or 0.0)
        ceiling = self.elapsed() + 2.0 if self.current else None
        out = []
        for sink in self.active_sinks():
            try:
                pos = sink.position() or 0.0
            except Exception:
                pos = 0.0
            if ceiling is not None:
                pos = min(pos, ceiling)
            out.append({
                "sink": sink.name,
                # How far into the song this output is started, to make up for
                # how long it takes to get going. Learned as it plays.
                "head_start": round(self._start_lag.get(sink.name, 0.0), 2),
                "position": round(pos, 2),
                "drift": round(pos - base, 2) if sink is not primary else 0.0,
                "syncable": sink.can_sync,
                "primary": sink is primary,
            })
        return out

    def shutdown(self):
        self._stop.set()
        for s in self.sinks.values():
            try:
                if hasattr(s, "shutdown"):
                    s.shutdown()
                else:
                    s.stop()
            except Exception:
                pass

    # ---------------------------------------------------------------- state

    def focus_state(self):
        if not self.focus_active():
            return None
        f = self.focus
        return {
            "label": f["label"],
            "terms": f["terms"],
            "era": list(f["era"]) if f["era"] else None,
            "pool": len(f["track_ids"]),
            "minutes_left": max(0, int((f["until"] - time.time()) / 60)),
        }

    def emit(self, event, payload):
        try:
            self.on_event(event, payload)
        except Exception:
            pass

    def state(self):
        ctx = self.context()
        sink = self.sink
        pos = sink.position() if (sink and self.current) else 0
        return {
            "playing": self.playing,
            "paused": self.paused,
            "sink": self.sink_name,
            "active": list(self.active),
            "sinks": {n: s.available() for n, s in self.sinks.items()},
            "syncable": {n: s.can_sync for n, s in self.sinks.items()},
            "sync": self.sync_report() if len(self.active) > 1 else [],
            "current": self.current,
            "position": round(pos or 0, 1),
            "queue": self.queue[:10],
            "reason": self.last_reason,
            "moment": {
                "text": ctxmod.describe(ctx),
                "name": self.moment_name,
                "activity": ctx.get("activity"),
                "bucket": ctxmod.buckets(ctx)[1],
                "override": bool(self.mode_override),
                "ha": ctx.get("ha"),
            },
            "discovery_bias": self.discovery_bias,
            "focus": self.focus_state(),
            "stats": db.stats(self.conn),
        }

"""Bootstrapping: teach the DJ what you already played, before day one.

Four sources, all optional:
  * your YouTube Music account itself - history, likes, library, playlists
  * a Google Takeout YouTube / YouTube Music history
  * the party-jukebox library already on this machine
  * a Spotify "Extended streaming history" export

Everything is resolved through YouTube Music so it lands on the same ids the
DJ will actually play, and matched by folded title/artist so the same song
does not enter twice under two different video ids.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import context as ctxmod
from . import db
from .catalog import Catalog


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _synth_context(when, activity, home=True, time_known=True):
    """Reconstruct roughly what the moment looked like for a historical play.

    We cannot recover presence sensors from months ago, so we record only what
    the clock genuinely tells us and mark the rest absent. context.similarity
    weighs shared keys only, so missing fields cost nothing.

    Some sources -- YouTube Music's history among them -- tell us only that you
    played something "Today" or "This week", with no time of day. Passing
    time_known=False leaves every clock field out rather than inventing one.
    Such a play still counts towards affinity, but contributes nothing to the
    time-of-day model, which is the honest outcome: inventing an hour would
    teach the DJ a pattern that never happened.
    """
    ctx = {
        "ts": when.isoformat(timespec="seconds"),
        "season": ctxmod.season_of(when.month),
        "home": home,
        "activity": activity,
        "ha": False,
        "synthetic": True,
        "time_known": bool(time_known),
    }
    if time_known:
        ctx.update({
            "hour": when.hour,
            "minute": when.minute,
            "dow": when.weekday(),
            "weekend": when.weekday() >= 5,
            "part": ctxmod.part_of_day(when.hour),
            "dark": when.hour >= 20 or when.hour < 7,
        })
    return ctx


def _resolve(conn, cat, artist, title, duration_hint=0, cache=None):
    """Find or create the track row for a name we read out of an export."""
    existing = db.find_by_ident(conn, artist, title)
    if existing:
        return existing["id"]

    key = db.ident_for(artist, title)
    if cache is not None and key in cache:
        return cache[key]

    hit = cat.best_match(artist, title)
    if not hit:
        if cache is not None:
            cache[key] = None
        return None
    # The search may have landed on a song we already hold under another name.
    already = db.get_track_by_video(conn, hit["video_id"])
    tid = db.upsert_track(
        conn, hit["video_id"], hit["title"], hit["artist"], hit.get("album", ""),
        hit.get("duration_sec") or duration_hint, hit.get("thumbnail", ""),
        hit.get("year"), source="seed",
    ) if not already else already["id"]
    if cache is not None:
        cache[key] = tid
    return tid


# ------------------------------------------------------------ party jukebox

def import_jukebox(conn, path, activity="party", progress=None, resolve=True):
    """Import jukebox_library.json.

    Those plays were a New Year party, so every one of them is filed under the
    'party' activity. Without that the DJ would conclude you want pop-folk at
    nine on a Tuesday morning, which is exactly the failure we are avoiding.
    """
    p = Path(path)
    if not p.exists():
        return {"error": f"not found: {path}"}
    data = json.loads(p.read_text(encoding="utf-8"))
    library = data.get("library", data)
    cat = Catalog() if resolve else None
    cache = {}
    stats = {"tracks": 0, "plays": 0, "unmatched": 0, "total": len(library)}

    # Re-importing must not stack a second copy of the same history on top of
    # the first, which would double every play count.
    with conn:
        conn.execute("DELETE FROM plays WHERE session_id = ?", ("seed-jukebox",))

    for i, (video_id, rec) in enumerate(library.items()):
        raw_title = rec.get("title") or ""
        ident = db.ident_for("", raw_title)
        artist = ident.split("|")[0] if "|" in ident else ""
        title = ident.split("|")[-1] or raw_title

        tid = None
        if resolve:
            tid = _resolve(conn, cat, artist, title,
                           _dur(rec.get("duration")), cache)
        if tid is None:
            # Fall back to the YouTube id we already have; it still plays.
            existing = db.get_track_by_video(conn, video_id)
            tid = existing["id"] if existing else db.upsert_track(
                conn, video_id, title or raw_title, artist,
                duration_sec=_dur(rec.get("duration")),
                thumbnail=rec.get("thumbnail", ""),
                tags=rec.get("genres") or [], source="jukebox",
            )
            if resolve:
                stats["unmatched"] += 1
        stats["tracks"] += 1

        first = _parse_ts(rec.get("first_played"))
        last = _parse_ts(rec.get("last_played"))
        count = max(1, int(rec.get("play_count") or 1))
        dur = _dur(rec.get("duration")) or 200
        for when in _spread(first, last, count):
            ctx = _synth_context(when, activity)
            db.log_play(
                conn, tid, _iso(when), _iso(when + timedelta(seconds=dur)),
                listened_sec=dur, duration_sec=dur, outcome="completed",
                sink="jukebox", session_id="seed-jukebox", ctx=ctx,
                bucket=ctxmod.primary_bucket(ctx), requested=1,
            )
            stats["plays"] += 1
        if progress and i % 10 == 0:
            progress(i + 1, len(library))
    return stats


def _dur(text):
    if not text:
        return 0
    try:
        parts = [int(x) for x in str(text).split(":")]
    except ValueError:
        return 0
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def _parse_ts(text):
    if not text:
        return None
    try:
        d = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _spread(first, last, count):
    """Place N plays between two timestamps we actually know about.

    Only the first and last are real; the rest are inferred from play_count.
    Spreading them uniformly across the span would invent listening at five in
    the morning and over lunch, and the context model would faithfully learn
    that you want pop-folk at dawn. So inferred plays are anchored to the
    times of day we actually observed, jittered by up to an hour, and kept
    inside the known window.
    """
    now = datetime.now(timezone.utc)
    first = first or last or now
    last = last or first
    if count <= 1 or last <= first:
        return [last]
    if count == 2:
        return [first, last]

    anchors = [first.hour * 60 + first.minute, last.hour * 60 + last.minute]
    span_days = max(1, (last - first).days)
    out = [first, last]
    for i in range(count - 2):
        base = (first + timedelta(days=i % span_days)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        # Deterministic jitter of roughly +/- an hour, so re-imports are stable.
        minutes = anchors[i % 2] + ((i * 37) % 121) - 60
        out.append(min(max(base + timedelta(minutes=minutes), first), last))
    return sorted(out)


# ----------------------------------------------------------------- spotify

def import_spotify(conn, path, progress=None, min_ms=30000):
    """Import a Spotify extended streaming history export.

    Accepts a directory of Streaming_History_Audio_*.json, a single json file,
    or the older StreamingHistory*.json shape.
    """
    files = _json_files(path, ("streaming_history", "streaminghistory", "endsong"))
    if not files:
        return {"error": f"no Spotify history json under {path}"}
    cat = Catalog()
    cache = {}
    stats = {"rows": 0, "plays": 0, "skipped": 0, "unmatched": 0, "files": len(files)}

    for f in files:
        try:
            rows = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        for i, row in enumerate(rows):
            stats["rows"] += 1
            artist = (row.get("master_metadata_album_artist_name")
                      or row.get("artistName") or "")
            title = (row.get("master_metadata_track_name")
                     or row.get("trackName") or "")
            if not title:
                continue
            ms = row.get("ms_played", row.get("msPlayed", 0)) or 0
            when = _parse_ts(row.get("ts") or row.get("endTime"))
            if not when:
                continue

            tid = _resolve(conn, cat, artist, title, cache=cache)
            if tid is None:
                stats["unmatched"] += 1
                continue
            track = db.get_track(conn, tid)
            dur = track["duration_sec"] or 210
            listened = ms / 1000.0
            # A skip is as informative as a play; keep both, drop only noise.
            if listened < 5:
                stats["skipped"] += 1
                continue
            outcome = "completed" if listened >= 0.8 * dur else (
                "skipped" if listened < 0.35 * dur else "stopped")
            ctx = _synth_context(when.astimezone(), "idle")
            db.log_play(conn, tid, _iso(when), _iso(when + timedelta(seconds=listened)),
                        listened_sec=listened, duration_sec=dur, outcome=outcome,
                        sink="spotify", session_id="seed-spotify", ctx=ctx,
                        bucket=ctxmod.primary_bucket(ctx))
            stats["plays"] += 1
            if progress and stats["rows"] % 50 == 0:
                progress(stats["rows"], None)
    return stats


# -------------------------------------------------- youtube music account

# What get_history() gives us instead of a timestamp. We know the day, near
# enough, but never the time of day -- see the time_known note below.
_PLAYED_DAYS = {
    "today": 0, "yesterday": 1, "this week": 3, "last week": 9,
    "this month": 18, "last month": 45,
}


def _played_days_ago(label, index=0):
    """Turn "Today" / "This week" into a rough age in days."""
    key = (label or "").strip().lower()
    base = _PLAYED_DAYS.get(key)
    if base is None:
        for k, v in _PLAYED_DAYS.items():
            if k in key:
                base = v
                break
    if base is None:
        base = 60
    # History comes back newest-first, so nudge later entries slightly older.
    # This only orders them; it never invents a clock time.
    return base + index * 0.01


def import_ytmusic(conn, progress=None, history=True, liked=True,
                   library=True, playlists=True, limit=1000):
    """Read your own YouTube Music account: history, likes, library, playlists.

    This is the good stuff, and it needs no export request and no waiting.
    Requires dj.cli auth to have been run first.

    A deliberate limitation: YouTube Music reports history as "Today",
    "Yesterday", "This week" -- the day, roughly, but never the hour. So these
    plays are recorded with time_known=False and feed affinity only. They tell
    the DJ *what* you like, and the live listening you do from here on is what
    teaches it *when*. Anything else would be fabrication.
    """
    from .ytauth import auth_file
    if not auth_file():
        return {"error": "not signed in - run: python -m dj.cli auth"}

    from ytmusicapi import YTMusic
    try:
        yt = YTMusic(auth_file())
    except Exception as e:
        return {"error": f"credential unusable: {e}"}

    cat = Catalog()
    now = datetime.now(timezone.utc)
    stats = {"history": 0, "liked": 0, "library": 0, "playlists": 0,
             "tracks_added": 0, "skipped": 0, "errors": []}

    # Re-running must replace what it wrote last time, not stack another copy.
    with conn:
        conn.execute("DELETE FROM plays WHERE session_id = ?", ("seed-ytmusic",))
        conn.execute(
            "DELETE FROM feedback WHERE json_extract(ctx, '$.source') = ?",
            ("ytmusic",))

    def store(item, source):
        """Get or create the track row for a ytmusicapi item."""
        from .catalog import to_track
        t = to_track(item)
        if not t or not cat.plausible(t):
            stats["skipped"] += 1
            return None
        row = db.get_track_by_video(conn, t["video_id"])
        if row:
            return row["id"]
        dup = db.find_by_ident(conn, t["artist"], t["title"])
        if dup:
            return dup["id"]
        stats["tracks_added"] += 1
        return db.upsert_track(
            conn, t["video_id"], t["title"], t["artist"], t.get("album", ""),
            t.get("duration_sec", 0), t.get("thumbnail", ""), t.get("year"),
            source=source,
        )

    # ---- history: real plays, but with no time of day
    if history:
        try:
            items = yt.get_history() or []
        except Exception as e:
            items, _ = [], stats["errors"].append(f"history: {e}")
        for i, item in enumerate(items[:limit]):
            tid = store(item, "ytm-history")
            if tid is None:
                continue
            when = now - timedelta(days=_played_days_ago(item.get("played"), i))
            track = db.get_track(conn, tid)
            dur = track["duration_sec"] or 210
            ctx = _synth_context(when, "idle", time_known=False)
            db.log_play(conn, tid, _iso(when), _iso(when + timedelta(seconds=dur)),
                        listened_sec=dur * 0.9, duration_sec=dur,
                        outcome="completed", sink="ytmusic",
                        session_id="seed-ytmusic", ctx=ctx,
                        bucket=ctxmod.primary_bucket(ctx))
            stats["history"] += 1
            if progress and stats["history"] % 25 == 0:
                progress(stats["history"], None)

    # ---- liked songs: the strongest explicit signal you can give
    if liked:
        try:
            data = yt.get_liked_songs(limit=limit) or {}
            items = data.get("tracks") or []
        except Exception as e:
            items, _ = [], stats["errors"].append(f"liked: {e}")
        for item in items:
            tid = store(item, "ytm-liked")
            if tid is None:
                continue
            db.add_feedback(conn, tid, "love",
                            ctx={"source": "ytmusic", "why": "liked on YouTube Music"},
                            bucket="")
            stats["liked"] += 1

    # ---- library: weaker, but it is still a deliberate act of keeping
    if library:
        try:
            items = yt.get_library_songs(limit=limit) or []
        except Exception as e:
            items, _ = [], stats["errors"].append(f"library: {e}")
        for item in items:
            tid = store(item, "ytm-library")
            if tid is None:
                continue
            db.add_feedback(conn, tid, "like",
                            ctx={"source": "ytmusic", "why": "in your library"},
                            bucket="")
            stats["library"] += 1

    # ---- playlists: you put these together on purpose
    if playlists:
        try:
            pls = yt.get_library_playlists(limit=50) or []
        except Exception as e:
            pls, _ = [], stats["errors"].append(f"playlists: {e}")
        for pl in pls:
            pid = pl.get("playlistId")
            if not pid or pid.upper() == "LM":  # liked songs, already handled
                continue
            try:
                data = yt.get_playlist(pid, limit=200) or {}
            except Exception:
                continue
            for item in (data.get("tracks") or []):
                tid = store(item, "ytm-playlist")
                if tid is None:
                    continue
                db.add_feedback(
                    conn, tid, "like", weight=0.6,
                    ctx={"source": "ytmusic",
                         "why": f"in your playlist {pl.get('title', '')}"},
                    bucket="")
                stats["playlists"] += 1

    return stats


# ---------------------------------------------------------------- takeout

_WATCHED = re.compile(r"^(watched|гледахте|Watched)\s+", re.I)


def import_takeout(conn, path, progress=None, music_only=True):
    """Import Google Takeout YouTube / YouTube Music watch history."""
    files = _json_files(path, ("watch-history", "watch_history", "history"))
    if not files:
        return {"error": f"no Takeout watch-history json under {path}"}
    cat = Catalog()
    cache = {}
    stats = {"rows": 0, "plays": 0, "unmatched": 0, "files": len(files)}

    for f in files:
        try:
            rows = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        for row in rows:
            stats["rows"] += 1
            url = row.get("titleUrl") or ""
            # Takeout tags each row with the product it came from. "YouTube
            # Music" rows are music by definition; plain "YouTube" rows are
            # mostly not, so they are excluded unless you ask for them. Pass
            # music_only=False if you listen through regular YouTube -- every
            # row still has to resolve to a plausible song on YouTube Music
            # before it counts, which throws out most of the noise.
            if music_only and (row.get("header") or "") != "YouTube Music":
                continue
            title = _WATCHED.sub("", row.get("title") or "").strip()
            if not title:
                continue
            when = _parse_ts(row.get("time"))
            if not when:
                continue
            vid = ""
            m = re.search(r"[?&]v=([\w-]{11})", url)
            if m:
                vid = m.group(1)
            subs = row.get("subtitles") or []
            artist = (subs[0].get("name", "") if subs else "").replace(" - Topic", "")

            tid = None
            if vid:
                existing = db.get_track_by_video(conn, vid)
                if existing:
                    tid = existing["id"]
            if tid is None:
                tid = _resolve(conn, cat, artist, title, cache=cache)
            if tid is None:
                stats["unmatched"] += 1
                continue

            track = db.get_track(conn, tid)
            dur = track["duration_sec"] or 210
            ctx = _synth_context(when.astimezone(), "idle")
            # Takeout records that you opened it, not how long you stayed.
            db.log_play(conn, tid, _iso(when), _iso(when + timedelta(seconds=dur)),
                        listened_sec=dur * 0.85, duration_sec=dur, outcome="completed",
                        sink="takeout", session_id="seed-takeout", ctx=ctx,
                        bucket=ctxmod.primary_bucket(ctx))
            stats["plays"] += 1
            if progress and stats["rows"] % 100 == 0:
                progress(stats["rows"], None)
    return stats


def _json_files(path, name_hints):
    p = Path(path)
    if p.is_file():
        return [p]
    if not p.exists():
        return []
    out = []
    for root, _dirs, names in os.walk(p):
        for n in names:
            if not n.lower().endswith(".json"):
                continue
            low = n.lower()
            if any(h in low for h in name_hints):
                out.append(Path(root) / n)
    return sorted(out)

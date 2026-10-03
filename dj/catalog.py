"""YouTube Music as the catalogue, yt-dlp as the tap.

ytmusicapi gives us search plus, more importantly, the *graph*: every track
has a 50-song radio and every artist has related artists. That graph is what
we mine for songs you have never heard. yt-dlp turns a video id into a direct
audio URL we can hand to mpv, a Cast device or a phone.
"""
import threading
import time

from ytmusicapi import YTMusic

from .config import load

_STREAM_TTL = 3 * 3600  # Google's URLs outlive this, but not by a lot.


def _consented_session():
    """A requests session that has already agreed to Google's cookie notice.

    From an EU address, a plain GET of music.youtube.com returns a consent
    interstitial instead of the page. ytmusicapi reads a visitor id out of
    that page on startup, so it gets HTML where it expects JSON and every
    later call dies with "Expecting value: line 1 column 1". It is
    intermittent, which makes it look like throttling. These two cookies are
    what the browser sets once you click through, and they make it go away.
    """
    import requests
    session = requests.Session()
    for name, value in (("SOCS", "CAI"), ("CONSENT", "YES+1")):
        session.cookies.set(name, value, domain=".youtube.com")
    return session



# YouTube keeps changing which player client will hand over an audio stream,
# and getting it wrong is not a slow path -- it is no playback at all. So we
# try a few in order and remember which one worked.
#
# Measured 2026-08-30: the plain web client answered "Sign in to confirm you're
# not a bot" while unauthenticated, and "Requested format is not available"
# once given cookies. The android client worked immediately, with no cookies.
_STRATEGIES = [
    {"extractor_args": {"youtube": {"player_client": ["android"]}}},
    {"extractor_args": {"youtube": {"player_client": ["ios"]}}},
    {"extractor_args": {"youtube": {"player_client": ["web_safari"]}}},
    {"use_cookies": True},
    {},
]
_preferred = 0


def _remember_strategy(index):
    global _preferred
    _preferred = index


def _ytdlp_strategies():
    """The strategies to try, best-known-first."""
    order = list(range(len(_STRATEGIES)))
    order.sort(key=lambda i: (i != _preferred, i))
    return [_STRATEGIES[i] for i in order]


def _extract(video_id, strategy):
    """One yt-dlp attempt. Returns info dict or None."""
    import yt_dlp
    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    extra = dict(strategy)
    if extra.pop("use_cookies", False):
        try:
            from .ytauth import cookiefile
            jar = cookiefile()
            if not jar:
                return None
            opts["cookiefile"] = jar
        except Exception:
            return None
    opts.update(extra)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False)
    except Exception:
        return None


def _dur_to_sec(text):
    if not text:
        return 0
    parts = str(text).split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def _artists(item):
    return ", ".join(
        a.get("name", "") for a in (item.get("artists") or []) if a.get("name")
    ).strip(", ")


def _thumb(item):
    # Search results carry "thumbnails" (a list); watch-playlist results carry
    # "thumbnail", which is sometimes a list and sometimes a single dict.
    thumbs = item.get("thumbnails") or item.get("thumbnail") or []
    if isinstance(thumbs, dict):
        thumbs = thumbs.get("thumbnails") or [thumbs]
    if not thumbs:
        return ""
    last = thumbs[-1]
    return last.get("url", "") if isinstance(last, dict) else ""


def _duration(item):
    """Seconds, from whichever field this endpoint happens to use.

    Search returns duration/duration_seconds; get_watch_playlist returns
    "length" instead. Reading only the first pair silently gave every
    radio-harvested track a duration of zero, which left the progress bar
    stuck at 0:00 and made completion impossible to compute.
    """
    for key in ("duration_seconds", "lengthSeconds"):
        val = item.get(key)
        if isinstance(val, (int, float)) and val:
            return int(val)
        if isinstance(val, str) and val.isdigit():
            return int(val)
    for key in ("duration", "length"):
        secs = _dur_to_sec(item.get(key))
        if secs:
            return secs
    return 0


def _album(item):
    alb = item.get("album")
    if isinstance(alb, dict):
        return alb.get("name", "")
    return alb or ""


def to_track(item):
    """Normalise a ytmusicapi result into our shape, or None if unusable."""
    vid = item.get("videoId")
    title = (item.get("title") or "").strip()
    if not vid or not title:
        return None
    return {
        "video_id": vid,
        "title": title,
        "artist": _artists(item),
        "album": _album(item),
        "duration_sec": _duration(item),
        "thumbnail": _thumb(item),
        "year": item.get("year"),
    }


class Catalog:
    def __init__(self, cfg=None):
        cfg = cfg or load()
        self.cfg = cfg["catalog"]
        self._yt = None
        self._lock = threading.Lock()
        self._streams = {}      # video_id -> (url, expires_at)
        self._radio_cache = {}  # video_id -> (tracks, expires_at)
        self._meta = {}         # video_id -> {url, headers, filesize}
        self.last_error = None
        self.authenticated = False

    @property
    def yt(self):
        """The YouTube Music client, signed in to your account when possible.

        Search and radio work fine anonymously. Signing in additionally unlocks
        your own history, likes and library, which is what dj.cli seed ytmusic
        reads. If the credential is missing or stale we fall back to anonymous
        rather than failing, so playback never depends on being signed in.
        """
        if self._yt is None:
            with self._lock:
                if self._yt is None:
                    from .ytauth import auth_file
                    kwargs = {"language": self.cfg.get("language", "en"),
                              "location": self.cfg.get("location", "BG"),
                              "requests_session": _consented_session()}
                    path = auth_file()
                    if path:
                        try:
                            self._yt = YTMusic(path, **kwargs)
                            self.authenticated = True
                        except Exception as e:
                            self.last_error = f"ytmusic auth unusable ({e}); anonymous"
                    if self._yt is None:
                        self._yt = YTMusic(**kwargs)
        return self._yt

    def reset_client(self):
        """Throw away the YouTube Music session and build a fresh one.

        The client is long-lived, and its session can go sour -- a burst of
        requests earns a brief throttle and every call afterwards comes back
        as HTML rather than JSON, surfacing as "Expecting value: line 1
        column 1". A new client recovers immediately, so anything that smells
        like a dead session retries once with one.
        """
        with self._lock:
            self._yt = None
            self.authenticated = False

    @staticmethod
    def _looks_like_dead_session(err):
        text = str(err).lower()
        return ("expecting value" in text or "json" in text
                or "html" in text or "403" in text or "429" in text)

    def _retrying(self, what, fn, *args, **kwargs):
        """Run a catalogue call, once more with a fresh client if it looks stale."""
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if not self._looks_like_dead_session(e):
                self.last_error = f"{what}: {e}"
                return None
            self.reset_client()
            try:
                return fn(*args, **kwargs)
            except Exception as e2:
                self.last_error = f"{what}: {e2}"
                return None

    def plausible(self, track):
        """Filter out interviews, hour-long mixes and 20-second stings."""
        if not track:
            return False
        d = track.get("duration_sec") or 0
        if d and not (self.cfg["min_duration"] <= d <= self.cfg["max_duration"]):
            return False
        return True

    # ------------------------------------------------------------- lookups

    def search(self, query, limit=10, kind="songs"):
        res = self._retrying(f"search({query!r})",
                             lambda: self.yt.search(query, filter=kind, limit=limit))
        if not res:
            return []
        out = []
        for item in res:
            t = to_track(item)
            if t and self.plausible(t):
                out.append(t)
        return out[:limit]

    def best_match(self, artist, title):
        """Find the YouTube Music entry for a song we only know by name."""
        query = f"{artist} {title}".strip()
        hits = self.search(query, limit=5)
        return hits[0] if hits else None

    def radio(self, video_id, limit=40):
        """The 50-ish song station YouTube Music builds around one track.

        This is the single richest discovery source we have: it already knows
        that Emanuela sits next to Galena and Tsvetelina Yaneva.
        """
        cached = self._radio_cache.get(video_id)
        if cached and cached[1] > time.time():
            return cached[0]
        data = self._retrying(f"radio({video_id})",
                              lambda: self.yt.get_watch_playlist(videoId=video_id,
                                                                 limit=limit))
        if not data:
            return []
        out = []
        for item in (data.get("tracks") or []):
            t = to_track(item)
            if t and t["video_id"] != video_id and self.plausible(t):
                out.append(t)
        self._radio_cache[video_id] = (out, time.time() + 1800)
        return out

    def related_artists(self, artist_name, limit=10):
        try:
            hits = self.yt.search(artist_name, filter="artists", limit=1) or []
            if not hits:
                return []
            artist = self.yt.get_artist(hits[0]["browseId"])
        except Exception as e:
            self.last_error = f"related_artists({artist_name!r}): {e}"
            return []
        rel = ((artist.get("related") or {}).get("results") or [])
        return [a.get("title") for a in rel[:limit] if a.get("title")]

    def artist_top(self, artist_name, limit=10):
        """Popular songs by an artist, for widening within a taste we know."""
        return self.search(artist_name, limit=limit)

    def mood_playlist_tracks(self, mood_title, limit=30):
        """Pull from YouTube Music's own mood shelves (Chill, Focus, Party...)."""
        try:
            cats = self.yt.get_mood_categories()
        except Exception as e:
            self.last_error = f"mood_categories: {e}"
            return []
        params = None
        for _section, entries in (cats or {}).items():
            for entry in entries:
                if entry.get("title", "").lower() == mood_title.lower():
                    params = entry.get("params")
                    break
            if params:
                break
        if not params:
            return []
        try:
            playlists = self.yt.get_mood_playlists(params) or []
        except Exception as e:
            self.last_error = f"mood_playlists({mood_title}): {e}"
            return []
        out = []
        for pl in playlists[:3]:
            try:
                data = self.yt.get_playlist(pl["playlistId"], limit=limit)
            except Exception:
                continue
            for item in (data.get("tracks") or []):
                t = to_track(item)
                if t and self.plausible(t):
                    out.append(t)
            if len(out) >= limit:
                break
        return out[:limit]

    def details(self, video_id):
        """Exact metadata for one video id, straight from YouTube Music."""
        data = self._retrying(f"details({video_id})",
                              lambda: self.yt.get_song(video_id)) or {}
        vd = data.get("videoDetails") or {}
        if not vd:
            return None
        secs = vd.get("lengthSeconds")
        try:
            secs = int(secs)
        except (TypeError, ValueError):
            secs = 0
        thumbs = ((vd.get("thumbnail") or {}).get("thumbnails")) or []
        return {
            "video_id": vd.get("videoId") or video_id,
            "title": vd.get("title") or "",
            "artist": vd.get("author") or "",
            "duration_sec": secs,
            "thumbnail": thumbs[-1]["url"] if thumbs else "",
        }

    # ------------------------------------------------------------ playback

    def stream_info(self, video_id, force=False):
        """Everything needed to fetch the audio: url, headers, size.

        The headers matter. Google throttles googlevideo requests that do not
        look like the client yt-dlp negotiated with, and a throttled stream
        arrives at barely twice real time, which is not enough for a Cast
        device to keep its buffer full.
        """
        self.stream_url(video_id, force=force)
        return self._meta.get(video_id)

    def stream_url(self, video_id, force=False):
        """Direct audio URL via yt-dlp, cached until it goes stale."""
        hit = self._streams.get(video_id)
        if hit and not force and hit[1] > time.time():
            return hit[0]
        try:
            import yt_dlp
        except ImportError:
            self.last_error = "yt-dlp is not installed"
            return None
        for attempt, extra in enumerate(_ytdlp_strategies()):
            info = _extract(video_id, extra)
            if info is None:
                continue
            url = info.get("url")
            chosen = info
            if not url:
                for f in info.get("formats", []):
                    if f.get("acodec") not in (None, "none") and f.get("url"):
                        url, chosen = f["url"], f
            if not url:
                continue
            # Remember what worked; YouTube keeps moving this and retrying
            # every strategy on every track would be painfully slow.
            _remember_strategy(attempt)
            self._streams[video_id] = (url, time.time() + _STREAM_TTL)
            self._meta[video_id] = {
                "url": url,
                "headers": dict(chosen.get("http_headers")
                                or info.get("http_headers") or {}),
                "filesize": (chosen.get("filesize")
                             or chosen.get("filesize_approx") or 0),
                "duration": int(info.get("duration") or 0),
                "expires": time.time() + _STREAM_TTL,
            }
            return url
        self.last_error = f"no playable stream for {video_id}"
        return None

    def probe(self, video_id):
        """Metadata without committing to playback. Returns dict or None."""
        for attempt, extra in enumerate(_ytdlp_strategies()):
            info = _extract(video_id, extra)
            if not info:
                continue
            _remember_strategy(attempt)
            return {
                "video_id": video_id,
                "title": info.get("track") or info.get("title") or "",
                "artist": info.get("artist") or info.get("uploader") or "",
                "album": info.get("album") or "",
                "duration_sec": int(info.get("duration") or 0),
                "thumbnail": info.get("thumbnail") or "",
                "year": info.get("release_year"),
            }
        self.last_error = f"probe({video_id}): nothing worked"
        return None

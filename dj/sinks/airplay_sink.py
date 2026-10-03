"""Audio to an AirPlay receiver, via pyatv's RAOP support.

This is the good path for the LG. The alternative -- launching the TV's own
YouTube app -- plays video and serves YouTube's ads; AirPlay carries our own
clean audio, exactly like Google Cast does for the Sony.

Three things had to line up to make it work, and each one failed first:

  * Everything past /info on port 7000 answers 403 until the device is paired,
    and AirPlay 2 pairing is a real cryptographic handshake. pyatv does it.
  * pyatv reports PlayUrl as "Available" on this TV, but using it fails with
    RTSP 501 Not Implemented. The URL-playback API is advertised and absent.
    stream_file (RAOP) is the one that actually carries audio.
  * pyatv decodes with miniaudio, which cannot read AAC -- our normal m4a
    stream dies with "failed to decode file". Hence /audio/<id>.mp3, which
    transcodes on the fly.

pyatv is asyncio and the player is threads, so this owns a loop on its own
thread and hands work to it. stream_file runs for the length of the track, so
it lives as a task we cancel rather than a call we wait on.
"""
import asyncio
import threading
import time

from . import Sink, WallClock


class AirPlaySink(Sink):
    name = "lg"
    #: pyatv reports Position as Unavailable on this receiver, so the wall
    #: clock is the only position we have. It is accurate enough: playback is
    #: linear from a known offset.
    reports_position = False
    #: The stream is ours and we push it ourselves, so the sending task
    #: finishing really is the end of the track.
    end_signal_reliable = True
    streams_from_us = True
    #: Seeking is done by restarting the transcode at a new offset, the same
    #: trick the local ffplay sink uses, so this can be held in a group.
    #: A seek here means dropping the RAOP session and starting a new
    #: transcode, so it is heard as a gap. Correct only when badly out.
    seek_is_disruptive = True

    @property
    def can_sync(self):
        """Only the AirPlay route can be held in step.

        The webOS fallback launches the TV's YouTube app, which starts from
        zero and takes no further instruction.
        """
        return self._route() == "airplay"

    def __init__(self, cfg, deps=None):
        super().__init__(cfg, deps)
        # The TV is often asleep, and which route works changes with it, so the
        # choice is made per play rather than once at startup -- otherwise
        # starting the server while the TV was off would leave us stuck on the
        # worse route until a restart.
        from .ha_sinks import WebOSSink
        self._fallback = WebOSSink(cfg, deps)
        self.host = cfg.get("host") or ""
        self.volume = cfg.get("volume", 30)
        self.stream_base = None      # set by the player once it knows its IP
        self.token = ""

        self.clock = WallClock()
        self._offset = 0.0
        self._video_id = None
        self._loop = None
        self._thread = None
        self._task = None
        self._atv = None
        self._lock = threading.RLock()
        self._playing = False
        self._reachable = False
        self._reach_at = 0.0
        self._using_fallback = False

    # ------------------------------------------------------------ event loop

    def _ensure_loop(self):
        if self._loop and self._loop.is_running():
            return
        ready = threading.Event()

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, daemon=True,
                                        name="airplay-loop")
        self._thread.start()
        ready.wait(5)

    def _submit(self, coro, timeout=None):
        """Run a coroutine on our loop from a normal thread."""
        self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        if timeout is None:
            return fut
        try:
            return fut.result(timeout)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return None

    # ------------------------------------------------------------ discovery

    def paired(self):
        if not self.host:
            return False
        from ..airplay import load_creds
        creds = load_creds()
        return any(creds.values()) if creds else False

    def _route(self):
        """Which way we would send audio right now."""
        if self.host and self.paired() and self._cheap_reachable():
            return "airplay"
        if self._fallback.available():
            return "webos"
        return None

    def _cheap_reachable(self):
        now = time.time()
        if now - self._reach_at < 5.0:
            return self._reachable
        self._reachable, self._reach_at = self._probe(), now
        return self._reachable

    def available(self):
        """Paired *and* actually awake.

        A TV in standby drops off the network completely, so claiming to be
        available would put a dead output in the picker and fail on play.
        The socket probe is cheap and cached for a few seconds so the state
        endpoint, which the page polls once a second, does not pay for it.
        """
        return self._route() is not None

    def _probe(self):
        import socket
        sock = socket.socket()
        sock.settimeout(1.0)
        try:
            sock.connect((self.host, 7000))
            return True
        except Exception:
            return False
        finally:
            sock.close()

    def _url(self, video_id, start_at=0.0):
        if not self.stream_base:
            return None
        url = f"{self.stream_base}/audio/{video_id}.mp3"
        params = []
        if self.token:
            params.append(f"key={self.token}")
        if start_at > 0.1:
            params.append(f"ss={start_at:.2f}")
        return url + ("?" + "&".join(params) if params else "")

    # -------------------------------------------------------------- playback

    async def _connect(self):
        from ..airplay import connect
        atv, err = await connect(self.host)
        if err:
            raise RuntimeError(err)
        return atv

    async def _run_stream(self, url):
        atv = None
        try:
            atv = await self._connect()
            self._atv = atv
            try:
                await atv.audio.set_volume(float(self.volume))
            except Exception:
                pass
            # Returns only when the track finishes or the task is cancelled.
            await atv.stream.stream_file(url)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
        finally:
            self._playing = False
            self._atv = None
            if atv is not None:
                try:
                    atv.close()
                except Exception:
                    pass

    def play(self, track, url, start_at=0.0):
        vid = track.get("video_id")
        if not vid:
            self.last_error = "no video id"
            return False
        if self._route() == "webos":
            # TV asleep for AirPlay, but reachable over webOS: at least get
            # sound out of it, accepting video and ads.
            self._using_fallback = True
            return self._fallback.play(track, url, start_at)
        self._using_fallback = False
        target = self._url(vid, start_at)
        if not target:
            self.last_error = "no reachable stream address yet"
            return False
        with self._lock:
            self._cancel()
            self._video_id = vid
            self._offset = start_at or 0.0
            self._task = self._submit(self._run_stream(target))
            self._playing = True
            self.clock.start()
        # Give it a moment to fail loudly rather than silently doing nothing.
        time.sleep(0.4)
        if self._task is not None and self._task.done():
            exc = self._task.exception() if not self._task.cancelled() else None
            if exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._playing = False
                return False
        return True

    def _cancel(self, wait=True):
        """Stop the current stream, and by default wait for it to let go.

        Opening a second RAOP session while the first is still tearing down
        makes the receiver refuse the new one with
        DecodeError('failed to init decoder'), so a seek that does not wait
        here fails every single time.
        """
        task, self._task = self._task, None
        self._playing = False
        if task is None or task.done():
            return
        try:
            self._loop.call_soon_threadsafe(task.cancel)
        except Exception:
            return
        if wait:
            deadline = time.time() + 3.0
            while time.time() < deadline and not task.done():
                time.sleep(0.05)

    def stop(self):
        if self._using_fallback:
            return self._fallback.stop()
        with self._lock:
            self._cancel()
            self.clock.reset()
            self._offset = 0.0

    def pause(self):
        # RAOP has no pause here, so remember where we are and drop the stream.
        with self._lock:
            self._offset = self.position()
            self._cancel()
            self.clock.pause()

    def resume(self):
        if self._using_fallback:
            return self._fallback.resume()
        with self._lock:
            if self._playing:
                return
            self.seek(self._offset)

    def seek(self, seconds):
        # There is no seek in RAOP; we restart the transcode at a new offset,
        # which is what ?ss= on the mp3 endpoint is for.
        with self._lock:
            if not self._video_id:
                return False
            return self.play({"video_id": self._video_id}, None,
                             start_at=max(0.0, seconds))

    def set_volume(self, level):
        if self._using_fallback:
            return self._fallback.set_volume(level)
        self.volume = max(0, min(100, int(level)))
        atv = self._atv
        if atv is not None:
            self._submit(atv.audio.set_volume(float(self.volume)), timeout=4)
        return True

    def position(self):
        if self._using_fallback:
            return self._fallback.position()
        if not self._playing:
            return self._offset
        return self._offset + self.clock.elapsed()

    def finished(self):
        task = self._task
        return task is not None and task.done()

    def is_playing(self):
        return self._playing and not self.finished()

    def shutdown(self):
        self.stop()
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass

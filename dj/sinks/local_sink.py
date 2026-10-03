"""Playback on this PC.

Uses ffplay, from the ffmpeg install already on this machine, rather than mpv.

That is a deliberate choice and worth explaining. mpv here is the Microsoft
Store build, which runs inside an AppContainer with a virtualised object
namespace, so the --input-ipc-server named pipe it creates is invisible to any
process outside the sandbox. No IPC means no pause, no position and no
end-of-track signal, which is most of what a sink is for. ffplay is a normal
winget install with no sandbox, so we can actually manage it.

Without IPC the model is one process per track:
  position   wall clock, which is exact because playback is linear from zero
  finished   the process exits by itself when the track ends
  pause      stop and remember the offset
  resume     start again with -ss at that offset

If you ever install a non-Store mpv, point sinks.pc.binary at its exe and this
class will use it instead, with the same process-per-track model.
"""
import os
import subprocess
import threading
import time
from shutil import which

from . import Sink, WallClock


class LocalSink(Sink):
    name = "pc"
    reports_position = True
    can_sync = True
    #: The one output we actually own. We start the process, playback runs
    #: linearly from a known offset, and the process exiting is the end of
    #: the track. Nothing else here comes close, so when the PC is playing it
    #: is the clock everything else is measured against.
    position_trust = 3
    end_signal_reliable = True

    def __init__(self, cfg, deps=None):
        super().__init__(cfg, deps)
        self.volume = cfg.get("volume", 80)
        self.binary = cfg.get("binary") or ""
        self.proc = None
        self.clock = WallClock()
        self._url = None
        self._offset = 0.0        # where in the track this process started
        self._lock = threading.RLock()

    # ------------------------------------------------------------ discovery

    def _player(self):
        """(path, kind) for whatever we can actually drive."""
        if self.binary and os.path.exists(self.binary):
            kind = "mpv" if "mpv" in os.path.basename(self.binary).lower() else "ffplay"
            return self.binary, kind
        ff = which("ffplay")
        if ff:
            return ff, "ffplay"
        mpv = which("mpv")
        if mpv:
            return mpv, "mpv"
        return None, None

    def available(self):
        return self._player()[0] is not None

    def _argv(self, path, kind, url, start_at):
        if kind == "ffplay":
            argv = [path, "-nodisp", "-autoexit", "-loglevel", "error",
                    "-volume", str(int(self.volume))]
            if start_at > 0.5:
                argv += ["-ss", f"{start_at:.2f}"]
            return argv + [url]
        argv = [path, "--no-video", "--no-terminal", "--force-window=no",
                f"--volume={int(self.volume)}"]
        if start_at > 0.5:
            argv += [f"--start={start_at:.2f}"]
        return argv + [url]

    # -------------------------------------------------------------- control

    def _spawn(self, url, start_at=0.0):
        path, kind = self._player()
        if not path:
            self.last_error = "neither ffplay nor mpv is available"
            return False
        self._kill()
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.proc = subprocess.Popen(
                self._argv(path, kind, url, start_at),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=flags,
            )
        except Exception as e:
            self.last_error = f"{kind}: {e}"
            return False
        self._offset = start_at
        self.clock.start()
        return True

    def _kill(self):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def play(self, track, url, start_at=0.0):
        if not url:
            self.last_error = "no stream url"
            return False
        with self._lock:
            self._url = url
            if not self._spawn(url, start_at):
                return False
            # ffplay exits immediately on an unreachable stream, so give it a
            # moment and check it is still alive before calling this a success.
            # Kept short: this sits directly on the press-Skip-to-sound path.
            time.sleep(0.12)
            if self.proc.poll() is not None:
                self.last_error = "player exited immediately (bad stream?)"
                return False
            return True

    def seek(self, seconds):
        with self._lock:
            if not self._url:
                return False
            return self._spawn(self._url, max(0.0, seconds))

    def stop(self):
        with self._lock:
            self._kill()
            self.clock.reset()
            self._offset = 0.0

    def pause(self):
        with self._lock:
            if self.proc is None:
                return
            self._offset = self.position()
            self._kill()
            self.clock.pause()

    def resume(self):
        with self._lock:
            if self.proc is not None or not self._url:
                return
            self._spawn(self._url, self._offset)

    def set_volume(self, level):
        with self._lock:
            self.volume = max(0, min(100, int(level)))
            # ffplay fixes volume at launch, so to change it now we restart at
            # the current offset. The UI only fires this on slider release.
            if self.proc is not None and self.proc.poll() is None and self._url:
                self._spawn(self._url, self.position())

    def position(self):
        if self.proc is None:
            return self._offset
        return self._offset + self.clock.elapsed()

    def remote_position(self):
        """Exact whenever something is actually playing."""
        return None if self.proc is None else self.position()

    def finished(self):
        """The process exiting on its own is the end-of-track signal."""
        return self.proc is not None and self.proc.poll() is not None

    def shutdown(self):
        self.stop()

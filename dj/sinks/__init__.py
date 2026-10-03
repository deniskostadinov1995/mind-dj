"""Output targets.

A sink knows how to start a track somewhere and, if it can, say how far in it
is. They differ a lot in what they can report:

  pc     real audio on this machine, exact position, full control
  web    real audio in a browser, position reported by the page
  cast   real audio on the Sony, position polled from Home Assistant
  webos  launches the track in the LG YouTube app, coarse control
  alexa  tells Alexa to play it from Amazon Music, no position at all

Anything that cannot report position falls back to a wall clock, which is
good enough to tell a finished song from one you walked out on.
"""
import time


class Sink:
    name = "base"
    #: Can this sink tell us where in the track it is?
    reports_position = False
    #: Does audio actually come from our own stream, or does the device fetch
    #: its own copy from somewhere else entirely?
    streams_from_us = True
    #: Can this sink be started at an offset and nudged back into line?
    #: False means it can be started alongside others but never held in step --
    #: the Echo, for instance, plays Amazon Music's own copy of the song, which
    #: is a different file of a different length that we cannot seek.
    can_sync = False
    #: Seeking this sink tears down and rebuilds its connection, which is
    #: audible. Such sinks get a wider tolerance: a small drift is far less
    #: annoying than a gap in the music every few seconds.
    seek_is_disruptive = False
    #: How much the position this sink reports can be believed, used to pick
    #: which output times the track when several are playing at once. The PC
    #: knows exactly -- we started the process ourselves and playback is
    #: linear from zero. A TV polled through Home Assistant is a guess about
    #: a device that may not even be playing our audio, so it must never
    #: outrank a sink that knows.
    position_trust = 0
    #: Whether finished() is a real end-of-track signal or an inference. The
    #: PC's player process exiting is a fact; a Cast device reading "idle"
    #: for a moment is not, and acting on it cuts songs off.
    end_signal_reliable = False

    def __init__(self, cfg, deps=None):
        self.cfg = cfg
        self.deps = deps or {}
        self.last_error = None

    def available(self):
        return True

    def play(self, track, url, start_at=0.0):
        """Begin playing. start_at lets a latecomer join a group mid-track."""
        raise NotImplementedError

    def seek(self, seconds):
        """Jump to a position. Only meaningful when can_sync is True."""
        return False

    def is_playing(self):
        """Whether audio is actually flowing right now.

        A sink that has stalled reports a frozen position, which the sync loop
        would otherwise read as enormous drift and 'correct' -- restarting it
        over and over. Saying so plainly avoids that.
        """
        return True

    def stop(self):
        pass

    def pause(self):
        pass

    def resume(self):
        pass

    def set_volume(self, level):
        pass

    def position(self):
        """Seconds into the current track, or None if unknown."""
        return None

    def remote_position(self):
        """A *verified* reading from the device, or None when we cannot get one.

        Deliberately separate from position(). position() must always return
        a usable number so the UI has something to show, which means falling
        back to an estimate. Synchronising against an estimate is worse than
        not synchronising at all -- it moves a device that was already in the
        right place -- so the sync loop asks this instead and skips any sink
        that honestly answers "I do not know".
        """
        return None

    def finished(self):
        """True when the sink is confident the track ended on its own."""
        return False


class WallClock:
    """Position fallback for sinks that cannot report their own."""

    def __init__(self):
        self.started = None
        self.paused_at = None
        self.offset = 0.0

    def start(self, at=0.0):
        """Begin timing. `at` is where in the track we are starting.

        An output that joins a song already in progress is not at zero, and
        a clock that says it is will report the position falling backwards
        the moment the music moves between rooms.
        """
        self.started = time.time() - max(0.0, at or 0.0)
        self.offset = 0.0
        self.paused_at = None

    def pause(self):
        if self.started and self.paused_at is None:
            self.paused_at = time.time()

    def resume(self):
        if self.paused_at is not None:
            self.offset += time.time() - self.paused_at
            self.paused_at = None

    def elapsed(self):
        if not self.started:
            return 0.0
        end = self.paused_at or time.time()
        return max(0.0, end - self.started - self.offset)

    def reset(self):
        self.started = None
        self.paused_at = None
        self.offset = 0.0

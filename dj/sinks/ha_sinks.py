"""Playback through Home Assistant: the Sony, the LG and the Echo.

These three are not equal and it is worth being blunt about why:

  Sony (Google Cast) takes any HTTP URL, so it gets real audio from our own
  stream endpoint and reports position back through HA. It is the best of the
  three by a distance.

  LG (webOS) will not fetch an arbitrary stream, but it can be told to open a
  YouTube video id in its YouTube app. Since our catalogue is YouTube, that
  works out neatly. Control is coarse and position is unreliable.

  Echo cannot accept a stream, so the DJ says the words instead:
  alexa_devices.send_text_command with "play <title> by <artist>", and Amazon
  Music finds its own copy. This works, but it is completely blind. The
  alexa_devices integration exposes only volume and mute for this device: the
  media_player reads "idle" the entire time it is playing, carries no media
  attributes, and the voice-event entity fires only for wake-word
  interactions. So there is nothing to poll and nothing to verify against --
  the track is timed on the wall clock, and because reports_position is False
  the player deliberately does not teach the context model from these plays.

  Two details that are easy to get wrong: the service is addressed by
  device_id, not entity_id, and its argument is text_command, not text.
"""
import threading
import time
from datetime import datetime, timezone

from . import Sink, WallClock


class HAMediaSink(Sink):
    """Common plumbing for anything driven through media_player services."""

    def __init__(self, cfg, deps=None):
        super().__init__(cfg, deps)
        self.entity = cfg.get("entity")
        self.ha = (deps or {}).get("ha")
        self.clock = WallClock()
        #: Wall time we handed this device the current track. Any position
        #: report stamped before this belongs to whatever it was doing
        #: beforehand, not to us.
        self._started_wall = 0.0
        #: Set once we have seen a real "playing" report. Until then the
        #: device reading "idle" means it has not started yet, not that the
        #: track has ended.
        self._seen_playing = False

    def _mark_started(self, at=0.0):
        self._started_wall = time.time()
        self._seen_playing = False
        self.clock.start(at)

    def _fresh(self, max_age=2.0):
        """This entity's current state, read fresh rather than from the
        thirty-second cache the rest of the app is happy with."""
        return self.ha.entity(self.entity, max_age=max_age) if self.ha else None

    def available(self):
        if not (self.ha and self.ha.enabled and self.entity):
            return False
        state = self.ha.state(self.entity)
        return state not in (None, "unavailable")

    def _call(self, service, data=None):
        if not self.ha:
            return False
        payload = {"entity_id": self.entity}
        payload.update(data or {})
        ok = self.ha.call("media_player", service, payload)
        if not ok:
            self.last_error = self.ha.last_error
        return ok

    def stop(self):
        self.clock.reset()
        self._seen_playing = False
        self._started_wall = 0.0
        return self._call("media_stop")

    def pause(self):
        self.clock.pause()
        return self._call("media_pause")

    def resume(self):
        self.clock.resume()
        return self._call("media_play")

    def set_volume(self, level):
        return self._call("volume_set", {"volume_level": max(0.0, min(1.0, level / 100.0))})

    def remote_position(self):
        """A position we are willing to swear belongs to the current track.

        Home Assistant does not stream position updates: it stores the last
        known media_position with a media_position_updated_at stamp and
        leaves you to add the elapsed time yourself. What it does not do is
        forget. A TV that spent the last hour on something else still reports
        that hour, and reading it raw is how every song we started looked
        finished the instant it began -- the set was an hour into a film, so
        it was past the end of a three minute track before the first note.

        Hence the stamp check: a reading from before we handed this device
        the track is not ours, and the honest answer is None.
        """
        s = self._fresh()
        if not s:
            return None
        a = s.get("attributes") or {}
        pos = a.get("media_position")
        stamped = a.get("media_position_updated_at")
        if not isinstance(pos, (int, float)) or not stamped:
            return None
        try:
            when = datetime.fromisoformat(str(stamped).replace("Z", "+00:00"))
        except Exception:
            return None
        if when.timestamp() < self._started_wall - 1.0:
            return None            # belongs to whatever played before us
        if s.get("state") != "playing":
            return None            # loading, paused or stopped: not advancing
        drift = (datetime.now(timezone.utc) - when).total_seconds()
        if not (0 <= drift < 60):
            return None
        self._seen_playing = True
        return max(0.0, float(pos) + drift)

    def position(self):
        """Best estimate of where this device is, always a number."""
        remote = self.remote_position()
        return remote if remote is not None else self.clock.elapsed()

    def is_playing(self):
        s = self._fresh()
        return bool(s) and s.get("state") == "playing"

    def finished(self):
        """Whether the device stopped of its own accord.

        Only meaningful once it has actually been seen playing. A Cast device
        reads "off" or "idle" for the first seconds after being handed a URL,
        and taking that as the end of the track skips every song on arrival.
        """
        if not (self.ha and self._seen_playing):
            return False
        s = self._fresh()
        return bool(s) and s.get("state") in ("idle", "off", "standby")


class CastSink(HAMediaSink):
    """Sony TV over Google Cast. Fetches audio from our own stream endpoint.

    It must be handed MP3, not the m4a everything else gets. This receiver
    accepts an AAC-in-MP4 stream without complaint -- it launches, it takes
    the title and artwork, it reports no error to Home Assistant and nothing
    appears in any log -- and then sits at position zero forever. That is the
    "TV just loading" that never resolved. The identical track offered as
    MP3 starts playing within a second, so the audio goes through the
    transcoding endpoint on the way to this one output.

    Seeking follows from that. The MP3 is generated on the fly and has no
    length, so media_seek has nothing to seek within; instead the track is
    re-cast starting at the offset, which is why seeking here is disruptive
    and the sync loop gives it a wider tolerance than it gives the others.
    """
    name = "sony"
    reports_position = True
    can_sync = True
    #: Above nothing and below everything: a real reading, but of a device on
    #: the other side of the network that we poll rather than own.
    position_trust = 1
    #: Seeking means casting the whole thing again, which is audible.
    seek_is_disruptive = True

    def __init__(self, cfg, deps=None):
        super().__init__(cfg, deps)
        self._pending_seek = 0.0
        self._track = None
        self._base_url = None
        #: Bumped on every play, so a delayed check can tell whether the cast
        #: it was asked to confirm is still the one we care about.
        self._epoch = 0
        #: Where in the song the current cast was told to begin. The receiver
        #: counts from the start of what it was sent, not the start of the
        #: song, so this has to be added back on.
        self._start_offset = 0.0

    def available(self):
        # A Cast device sits in "off" when it is idle but is still perfectly
        # castable, so unlike the base class we only treat it as unavailable
        # when Home Assistant has genuinely lost sight of it.
        if not (self.ha and self.ha.enabled and self.entity):
            return False
        return self.ha.state(self.entity) not in (None, "unavailable")

    def play(self, track, url, start_at=0.0):
        if not url:
            self.last_error = "no stream url"
            return False
        # Deliberately no turn_on first. It looks like the careful thing to
        # do and it is the opposite: casting to this set while it is off
        # works every time, but waking it and casting a moment later races
        # the receiver launching against the load and the load loses --
        # silently, leaving the TV sitting on the title of a song it never
        # starts. play_media wakes it perfectly well on its own.
        start_at = max(0.0, start_at or 0.0)
        self._track = track
        self._base_url = url
        self._start_offset = start_at
        # The transcoder starts wherever we ask, which is how this output
        # joins a song already in progress.
        if start_at > 0.5:
            url = "%s%sss=%.2f" % (url, "&" if "?" in url else "?", start_at)
        # The Cast device pulls this itself, so it must be an address reachable
        # from the TV on the LAN, not localhost, and it must carry the access
        # token because the TV has no cookie of ours.
        ok = self._call("play_media", {
            "media_content_id": url,
            "media_content_type": "audio/mpeg",
            "extra": {
                "metadata": {
                    "metadataType": 3,
                    "title": track.get("title", ""),
                    "artist": track.get("artist", ""),
                    "images": ([{"url": track["thumbnail"]}]
                               if track.get("thumbnail") else []),
                }
            },
        })
        if ok:
            self._epoch += 1
            self._mark_started(start_at)
            # Sometimes the receiver takes the command, launches, shows the
            # title -- and never starts, which is the "TV is just loading"
            # this output was reported for. It happens most on the first cast
            # after the set has been asleep. Nothing reports the failure, so
            # the only way to know is to look, and the only cure is to ask
            # again.
            threading.Timer(7.0, self._confirm,
                            args=(self._epoch, url, track)).start()
        return ok

    def stop(self):
        # Invalidate any pending confirmation, or a stop issued within a few
        # seconds of starting would be undone by the retry firing afterwards
        # and casting the track all over again.
        self._epoch += 1
        return super().stop()

    def pause(self):
        self._epoch += 1
        return super().pause()

    def _confirm(self, epoch, url, track):
        """Check the cast actually started, and repeat it once if it did not."""
        if epoch != self._epoch:
            return                      # we have moved on to something else
        state = (self._fresh(max_age=0.5) or {}).get("state")
        if state == "playing":
            return
        self.last_error = f"cast did not start ({state}); asking again"
        ok = self._call("play_media", {
            "media_content_id": url,
            "media_content_type": "audio/mpeg",
            "extra": {"metadata": {
                "metadataType": 3,
                "title": track.get("title", ""),
                "artist": track.get("artist", ""),
                "images": ([{"url": track["thumbnail"]}]
                           if track.get("thumbnail") else []),
            }},
        })
        if ok:
            # Do not restart the clock: the song has been running on the other
            # outputs all along, and this one is simply late joining it.
            self._seen_playing = False

    def seek(self, seconds):
        """Cast the track again from `seconds` in.

        media_seek is not an option: what this device is playing is a stream
        being encoded as it goes, with no length and nothing to seek within.
        """
        if not (self._track and self._base_url):
            return False
        return self.play(self._track, self._base_url, start_at=max(0.0, seconds))

    def remote_position(self):
        """Add back the offset the cast was started at.

        The receiver counts from the beginning of what it was sent, so during
        a track that began forty seconds in, it reports zero and means forty.
        """
        pos = super().remote_position()
        return None if pos is None else pos + self._start_offset


class WebOSSink(HAMediaSink):
    """LG TV, driven over its own protocol rather than through Home Assistant.

    HA's webostv integration could not hold a connection to this set -- the
    entity sat at "unavailable" and every service call was accepted and
    dropped. Talking to the TV directly on port 3000 works reliably, so that
    is the primary path and HA is only a fallback for installs where the
    integration is healthy and no host is configured.

    Note that media_player.play_media would have been the wrong door anyway:
    webostv only accepts the "channel" and "app" media types, so handing it a
    URL is rejected outright.

    The track plays in the TV's own YouTube app, which means video and, unlike
    every other sink, whatever ads YouTube decides to show.
    """
    name = "lg"
    reports_position = False
    streams_from_us = False

    def __init__(self, cfg, deps=None):
        super().__init__(cfg, deps)
        self.host = cfg.get("host") or ""
        self._client = None

    @property
    def client(self):
        if self._client is None and self.host:
            from ..webos import WebOSClient
            self._client = WebOSClient(self.host)
        return self._client

    def paired(self):
        if not self.host:
            return False
        from ..webos import load_keys
        return bool(load_keys().get(self.host))

    def available(self):
        if self.paired():
            return True
        return super().available()

    def play(self, track, url, start_at=0.0):
        # start_at is ignored: the TV's YouTube app always starts from zero.
        vid = track.get("video_id")
        if not vid:
            self.last_error = "no video id"
            return False

        if self.paired():
            _, err = self.client.launch_youtube(vid)
            if not err:
                self._mark_started()
                return True
            self.last_error = f"webos: {err}"
            # Fall through and let Home Assistant try, in case it is healthy.

        if self.ha and self.ha.state(self.entity) in ("off", "standby"):
            self._call("turn_on")
        ok = self.ha.call("webostv", "command", {
            "entity_id": self.entity,
            "command": "system.launcher/launch",
            "payload": {"id": "youtube.leanback.v4", "contentId": vid},
        }) if self.ha else False
        if ok:
            self._mark_started()
        elif self.ha:
            self.last_error = self.ha.last_error
        return ok

    def stop(self):
        self.clock.reset()
        if self.paired():
            _, err = self.client.media_stop()
            if not err:
                return True
        return super().stop()

    def pause(self):
        self.clock.pause()
        if self.paired():
            _, err = self.client.media_pause()
            if not err:
                return True
        return super().pause()

    def resume(self):
        self.clock.resume()
        if self.paired():
            _, err = self.client.media_play()
            if not err:
                return True
        return super().resume()

    def set_volume(self, level):
        if self.paired():
            # webOS volume is 0-100, same scale as ours.
            _, err = self.client.set_volume(max(0, min(100, int(level))))
            if not err:
                return True
        return super().set_volume(level)

    def position(self):
        # The TV's YouTube app reports nothing back, by either route.
        return self.clock.elapsed()

    def finished(self):
        """True once YouTube is no longer in the foreground.

        Not before the app has had time to come up, though: the TV keeps
        reporting the previous app for several seconds after a launch, which
        would read as "the track is over" the moment it starts.
        """
        if time.time() - self._started_wall < 12.0:
            return False
        if self.paired():
            info, err = self.client.foreground_app()
            if not err and info:
                return "youtube" not in str(info.get("appId", "")).lower()
        return False


class AlexaSink(Sink):
    """Echo Dot, driven by speaking a command at it.

    Fire and forget. We learn nothing about whether it actually played, so
    every track here is timed on the wall clock and any completion signal
    from this sink is treated as weak evidence elsewhere.
    """
    name = "echo"
    reports_position = False
    streams_from_us = False

    def __init__(self, cfg, deps=None):
        super().__init__(cfg, deps)
        self.entity = cfg.get("entity")
        self.ha = (deps or {}).get("ha")
        self.clock = WallClock()

    def available(self):
        if not (self.ha and self.ha.enabled and self.entity):
            return False
        return self.ha.state(self.entity) not in (None, "unavailable")

    def play(self, track, url, start_at=0.0):
        # start_at is ignored: Alexa plays Amazon's own copy from the top.
        artist = (track.get("artist") or "").split(",")[0].strip()
        title = track.get("title") or ""
        phrase = f"play {title} by {artist}" if artist else f"play {title}"

        ok = self._say(phrase)
        # Genuinely fire and forget. The alexa_devices integration exposes only
        # volume and mute for this device: while the Echo is happily playing,
        # its media_player still reads "idle" with no media attributes, and the
        # voice-event entity fires for wake-word interactions only. There is no
        # signal here to confirm against, so a delivered command is the best
        # evidence available and we time the track on the wall clock.
        if ok:
            self.clock.start()
        return ok

    def _say(self, phrase):
        """Speak a command at the Echo.

        Addressed by *device*, and the argument is text_command, not text.
        """
        if not self.ha:
            return False
        device = self.ha.device_id(self.entity)
        if not device:
            self.last_error = f"no device_id for {self.entity}"
            return False
        ok = self.ha.call("alexa_devices", "send_text_command", {
            "device_id": device, "text_command": phrase,
        })
        if not ok:
            self.last_error = self.ha.last_error
        return ok

    # Control has to travel the same road playback did: by talking to Alexa.
    #
    # The obvious route cannot work. This entity advertises exactly five
    # capabilities -- VOLUME_SET, VOLUME_MUTE, PLAY_MEDIA, VOLUME_STEP and
    # STOP -- and PAUSE is not among them, so media_player.media_pause was
    # refused and the music simply carried on. Nor could it ever have worked:
    # what is playing is Amazon Music, started by voice, and this entity is
    # not the thing playing it. Saying "pause" reaches the same place the
    # "play ..." did, which is the only place that is actually listening.

    def stop(self):
        self.clock.reset()
        said = self._say("stop")
        # STOP is one of the few things the entity does claim to support, so
        # send that as well. Belt and braces: neither is reliable alone.
        self.ha.call("media_player", "media_stop", {"entity_id": self.entity})
        return said

    def pause(self):
        self.clock.pause()
        return self._say("pause")

    def resume(self):
        self.clock.resume()
        return self._say("resume")

    def set_volume(self, level):
        return self.ha.call("media_player", "volume_set", {
            "entity_id": self.entity, "volume_level": max(0.0, min(1.0, level / 100.0)),
        })

    def position(self):
        return self.clock.elapsed()


class WebSink(Sink):
    """The browser tab is the speaker. The page reports its own position."""
    name = "web"
    reports_position = True
    can_sync = True
    #: Second only to the PC: an exact reading, but from a tab that can be
    #: closed or backgrounded without telling us.
    position_trust = 2
    end_signal_reliable = True

    def __init__(self, cfg, deps=None):
        super().__init__(cfg, deps)
        self.clock = WallClock()
        self._reported = None
        self._reported_at = 0.0
        self._ended = False
        # Bumped whenever the page must jump; it only acts on a change, so a
        # poll loop cannot fight the listener scrubbing manually.
        self.seek_to = 0.0
        self.seek_epoch = 0

    def available(self):
        return True

    def play(self, track, url, start_at=0.0):
        # Nothing to do server-side: the page polls /api/state, sees a new
        # video id and a seek_to, and moves its own audio element.
        self.clock.start(start_at or 0.0)
        self._reported = None
        self._reported_at = 0.0
        self._ended = False
        self.seek_to = start_at or 0.0
        self.seek_epoch += 1
        return True

    def seek(self, seconds):
        self.seek_to = max(0.0, seconds)
        self.seek_epoch += 1
        return True

    def report(self, position, ended=False):
        """Called from the web client as it plays."""
        self._reported = position
        self._reported_at = time.time()
        self._ended = bool(ended)

    def remote_position(self):
        """Only while the page is still talking to us. A closed tab leaves its
        last reading behind, and a frozen number reads as enormous drift."""
        if self._reported is None or time.time() - self._reported_at > 5.0:
            return None
        return self._reported

    def position(self):
        fresh = self.remote_position()
        return fresh if fresh is not None else self.clock.elapsed()

    def finished(self):
        return self._ended

    def stop(self):
        self.clock.reset()
        self._ended = False

    def pause(self):
        self.clock.pause()

    def resume(self):
        self.clock.resume()

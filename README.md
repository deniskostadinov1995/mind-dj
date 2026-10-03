# 🎧 mind-dj

**A personal DJ that learns what you like, *when* you like it, and plays it on whichever speaker you're near.** It runs entirely on your own PC: no subscription, no cloud service, no account.

![Python](https://img.shields.io/badge/python-3.13-3776AB)
![Flask](https://img.shields.io/badge/Flask-SocketIO-000000)
![Home Assistant](https://img.shields.io/badge/Home%20Assistant-optional-41BDF5)
![Ollama](https://img.shields.io/badge/Ollama-optional-black)
![License](https://img.shields.io/badge/license-MIT-green)

- 🧠 **Learns context, not just taste**: time of day, who's home, which rooms have motion, lights, sleep mode, weather.
- 🎵 **YouTube Music catalogue** (via `ytmusicapi` + `yt-dlp`), mined for songs you've never heard but will probably like.
- 🔊 **Plays anywhere**: this PC, any phone/browser, Google Cast TVs, AirPlay receivers, webOS TVs and Amazon Echo, even **several at once, kept in sync**.
- 🗣️ **Ask for things** in plain English or Bulgarian: *"chalga 2000-2010"*, *"something chill for cooking"*.
- ⏭️ **Skip** vs **Not now**: tells "I don't like this" apart from "great song, wrong moment".
- 🏠 **Home Assistant integration** both ways: it reads presence and context, and publishes now-playing sensors. Voice control through Assist works too.
- 🌍 **Away from home**: token-protected access through an automatic Cloudflare quick tunnel.

---

## What it actually does

Every time a track plays, it writes down what was playing, how much of it you
listened to, and **what the moment looked like** — the hour, the day, whether
you were home, which rooms had motion, whether the lights were low, sleep mode,
the weather. When you skip something twenty seconds in, that is recorded too,
and a skip is the loudest signal you ever give it.

Seven things decide what plays next:

| signal | what it asks |
|---|---|
| **sound** | does it *sound* like this moment wants — energy, brightness, punch |
| **affinity** | do you like this track at all |
| **context** | do you like this *kind* of track in *this kind of moment* |
| **semantic** | does it sound like what you actually finish at moments like this |
| **dormancy** | you loved it once and have not heard it in months |
| **explore** | you have never heard it |
| **fatigue** | you have heard it, or this artist, too much lately |

None of that involves a language model. Scoring is numpy and SQL, and takes
about ten milliseconds, because the next track cannot wait fifty seconds for
Ollama to load a model.

### The bit that makes it work on small data

Per-track context statistics are hopeless — you will never have thirty
Tuesday-morning plays of one specific song. So tracks are clustered by their
embedding into ~24 **vibes**, and preference is learned per *vibe* per
*context*, which gets dense fast.

Context is recorded at several levels of specificity at once:

```
late|home|party|wknd     ← narrow, needs a lot of history
late|home|party
late|home
party
late                     ← broad, useful immediately
```

Each level is trusted in proportion to the evidence it holds, so the DJ leans
on broad patterns early and narrow ones later, with no hand-tuning. There is
deliberately **no catch-all level** — when it genuinely knows nothing about
right now, `context` scores zero and says so, rather than pretending.

---

---

## Getting started

### Requirements

- **Windows** (the PC output uses `ffplay`; the rest is cross-platform Python)
- **Python 3.13**
- **ffmpeg** (`ffplay` + `ffmpeg`) on `PATH`: `winget install Gyan.FFmpeg`
- *Optional:* **Home Assistant** (context + Cast/Echo outputs), **Ollama** with `nomic-embed-text` and a chat model such as `qwen3:8b`, and **cloudflared** for remote access

### Install

```bash
git clone https://github.com/deniskostadinov1995/mind-dj.git
cd mind-dj
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy config.example.json config.json
```

Edit `config.json`. Anything you leave out falls back to the defaults in [`dj/config.py`](dj/config.py):

| Section | What to set |
|---|---|
| `server` | `port`, an `access_token` (required before exposing it to the internet), `tunnel` |
| `homeassistant` | `url`, and the `context_entities` that exist in *your* home (presence, motion, lights, weather, sleep mode…). Set `enabled: false` to run without HA. |
| `ollama` | `url` and models, or `enabled: false` |
| `sinks` | Which outputs exist (`pc`, `web`, `sony` = any Google Cast device, `lg` = AirPlay/webOS TV, `echo` = Amazon Echo) and their HA entities |

The Home Assistant token is read from the `HA_TOKEN` environment variable, or from the file in `homeassistant.token_file` (default `data/ha-token.txt`).

`truststore` is a hard requirement if antivirus software intercepts TLS (AVG/Avast Web Shield re-sign certificates with their own root, which `certifi` doesn't trust). It makes Python use the Windows certificate store instead.

### Check everything, then run

```bash
.venv\Scripts\python -m dj.cli doctor    # database, Home Assistant, Ollama, YouTube search, yt-dlp, ffplay, every sink
run.bat                                    # starts the server -> http://<this-pc>:5055
```

Open `http://<this-pc>:5055` on your PC or phone.

---

## Teaching it

```bash
.venv\Scripts\python -m dj.cli auth          # sign in to YouTube Music
.venv\Scripts\python -m dj.cli seed ytmusic  # history, likes, library, playlists
.venv\Scripts\python -m dj.cli seed jukebox
.venv\Scripts\python -m dj.cli seed takeout "C:\path\to\Takeout"
.venv\Scripts\python -m dj.cli seed spotify "C:\path\to\my_spotify_data"
.venv\Scripts\python -m dj.cli harvest 3     # pull in unheard songs
.venv\Scripts\python -m dj.cli suggest 10    # what would it play right now
```

### Your YouTube Music account (start here)

`dj.cli auth` reads your account directly — no export request, no waiting. It
writes `data/ytmusic_headers.txt` and prints what to paste into it: open
music.youtube.com, F12 → Network, filter `/youtubei/v1/`, click a request,
**Copy request headers**. Run `dj.cli auth` again and it converts them into
`data/ytmusic_auth.json`, which is what gets used from then on. Both files are
git-ignored; the headers file contains a session cookie for your account, so
treat it like a password and delete it once the credential exists.

`dj.cli auth status` says who you are signed in as, `dj.cli auth forget`
removes it. If the credential goes stale, the catalogue silently falls back to
anonymous — search and playback keep working, only account reads stop.

Then `seed ytmusic` pulls in four things:

| source | recorded as |
|---|---|
| listening history | plays (affinity) |
| liked songs | `love` |
| library songs | `like` |
| your playlists | `like`, at 0.6 weight |

**One honest limitation.** YouTube Music reports history as "Today",
"Yesterday", "This week" — the day, roughly, but never the hour. Those plays
are stored with `time_known=False`, which produces *no* context buckets at all.
They teach the DJ **what** you like; they teach it nothing about **when**.
Inventing an hour would be fabrication, and would poison the one signal that
makes this feel like mind-reading.

If you want the *when* from history too, Google Takeout is the only source with
real timestamps — request **YouTube and YouTube Music → history** and run
`seed takeout` when it arrives. Until then, the "when" comes from your live
listening, which starts building from the first track you play.

`seed jukebox` imports the play history of [Party Jukebox](https://github.com/deniskostadinov1995/party-jukebox)
(set `seed.jukebox_library` to its `jukebox_library.json`). Those plays are filed under the **party** activity, because a party is not everyday taste — otherwise the DJ would conclude you want Емануела at nine
on a Tuesday morning.

Spotify's *extended streaming history* is supported too if you ever want it;
it has per-play timestamps and listen duration, which is the richest of the
lot. It takes a few days to arrive.

### Re-clustering

Vibe ids are renumbered whenever clusters are rebuilt, which would orphan every
learned preference. So `maintain.recluster` always rebuilds the clusters *and*
replays history into the context model. Use `dj.cli refresh`; never rebuild
vibes on their own.

---

---

## Where the music comes from

YouTube Music, via `ytmusicapi` for the catalogue and `yt-dlp` for audio.
Chosen over Spotify because it actually has Bulgarian pop-folk, needs no
subscription, and — critically — exposes a **graph**: every track has a 50-song
radio and every artist has related artists. That graph is what gets mined for
songs you have never heard.

Songs are identified by folded artist + title, not by video id, because
YouTube and YouTube Music hand out different ids for the same song.

---

---

## Where it plays

| target | how | notes |
|---|---|---|
| **This PC** | `ffplay`, one process per track | exact position, pause, resume |
| **Phone / any browser** | the page streams `/stream/<id>` itself | works anywhere the server is reachable |
| **Sony TV** | Google Cast, fed from `/audio/<id>.mp3` | real audio; **MP3, not m4a** — see below |
| **LG TV** | AirPlay when awake, else webOS YouTube app | AirPlay gives clean audio and syncs |
| **Echo Dot** | `alexa_devices.send_text_command` | works, but totally blind — see below |

**About the Echo.** It cannot accept an audio stream, so the DJ says the words
instead: it picks the track and tells Alexa "play X by Y", and Amazon Music
finds its own copy. This works. Two details are easy to get wrong and both
failed silently until fixed: the service is addressed by **`device_id`, not
`entity_id`**, and its argument is **`text_command`, not `text`**.

**Pausing it has to be spoken too.** The obvious route does not merely fail,
it cannot work. This entity advertises exactly five capabilities —
`VOLUME_SET`, `VOLUME_MUTE`, `PLAY_MEDIA`, `VOLUME_STEP`, `STOP` — and `PAUSE`
is not among them, so `media_player.media_pause` came back **HTTP 500** and
the music carried on regardless. Nor could it have worked even if it were
supported: what is playing is a music service's own copy, started by voice,
and this entity is not the thing playing it. So pause, resume and stop are
spoken at Alexa, exactly as play is.

Worth knowing: this Echo's default music provider is **Spotify**, not Amazon
Music — it answered one request with "I can't find the song … on Spotify". A
track Spotify does not carry will silently play nothing while the DJ believes
it is playing. The wall-clock timing below is what keeps that from being
recorded as a listen you enjoyed.

It is, however, completely blind. The `alexa_devices` integration exposes only
volume and mute for this device: the `media_player` reads `idle` the whole time
it is playing, carries no media attributes, and the voice-event entity fires
only for wake-word interactions. So there is nothing to poll and nothing to
verify against. Tracks are timed on the wall clock, and because
`reports_position` is False the player deliberately never teaches the context
model from an Echo play (`player.py`, `_finish_current`) — otherwise it would
be recording completions it cannot actually observe.

**About the Sony.** Four things it needed, all fixed, and all worth knowing
if you add another Cast device:

* **It will not play our m4a.** This is the one that cost the most, because
  the failure is entirely silent. Hand this receiver an AAC-in-MP4 stream and
  it accepts the command, launches, shows the title and the artwork, returns
  HTTP 200 to Home Assistant, writes nothing to any log — and then sits at
  position zero for as long as you care to watch. That is the "the TV was just
  loading" that never resolved. The same track offered as MP3 starts within a
  second, so the Sony is fed the transcoding endpoint, `/audio/<id>.mp3`, with
  `media_content_type: audio/mpeg`.

  The cost is that a stream being encoded as it goes has no length, so
  `media_seek` has nothing to seek within. Seeking the Sony therefore means
  casting the track again from `?ss=<offset>`, which is audible, which is why
  it is marked `seek_is_disruptive` and given a wider tolerance than the rest.

* **Never `turn_on` before casting.** This looks like the careful thing to do
  and is the opposite. Casting to this set while it is *off* wakes it and
  plays, every time. Waking it first and casting a moment later races the
  receiver launch against the load — and the load loses, silently, leaving the
  TV showing the title of a song it never starts. Removing that one call is
  what fixed playing to a TV that had been asleep.

* **Check that it actually started.** Unlike the Echo, there is a real signal
  here, so `CastSink` looks again seven seconds later and re-sends once if the
  device is not `playing`.

* The access token must be *in the stream URL*. A Cast device has no cookie
  and sends none of our headers, so with a token set it simply got a 403 and
  played silence. Google also throttles a single long GET from googlevideo to
  roughly twice real time, so the proxy pulls upstream in 2 MB Range chunks
  using the headers yt-dlp negotiated — 31 KB/s to 1.4 MB/s.

**About the LG.** Home Assistant's webostv integration could not hold a
connection to this set -- the entity sat at `unavailable` and every service
call was accepted with a 200 and quietly discarded. So the TV is driven
directly, two ways, chosen per track depending on whether it is awake:

* **AirPlay** (preferred). Clean audio from our own stream, no ads, and it can
  be held in sync. Pair once:

  ```bash
  .venv\Scripts\python -m dj.cli airplay pair <tv-ip>   # accept the PIN on screen
  .venv\Scripts\python -m dj.cli airplay caps <tv-ip>
  ```

* **webOS** (fallback). Launches the TV's own YouTube app over its `ssap://`
  websocket on port 3000. Video, YouTube's ads, and cannot be synced.

  ```bash
  .venv\Scripts\python -m dj.cli lg pair
  .venv\Scripts\python -m dj.cli lg status
  ```

Getting AirPlay working took three separate discoveries, each of which looked
like the answer and was not:

1. Everything past `/info` on port 7000 answers **403** until the device is
   paired, and AirPlay 2 pairing is a real cryptographic handshake. pyatv does
   it; do not attempt it by hand.
2. pyatv reports `PlayUrl` as **Available** on this TV, but calling it fails
   with `RTSP 501 Not Implemented`. The API is advertised and absent.
   `stream_file` (RAOP) is the one that carries audio.
3. pyatv decodes with miniaudio, which **cannot read AAC** -- our normal m4a
   stream dies with `failed to decode file`. Hence `/audio/<id>.mp3`, which
   transcodes on the fly and takes `?ss=` so an output can join mid-track.

Two further traps worth knowing. Cancelling a RAOP session and immediately
opening another makes the receiver reject the new one with
`DecodeError('failed to init decoder')`, so a seek must wait for the old
session to let go. And a TV in standby vanishes from the network entirely, so
the sink probes the socket before claiming to be available -- otherwise a dead
output sits in the picker and fails on play.

**Finding the TV:** identify it by **MAC vendor**, not by an open port. A Tuya
device on this network also listens on 3000 and answers with its own binary
protocol, which is a good way to waste an hour.

---

---

## Asking for something

"chalga 2000" means chalga from 2000. "chalga 2000-2010" means the decade
between them. `dj/query.py` reads the era before anything is searched, in
English or Bulgarian: `2000`, `2000-2010`, `2015 2020`, `90s`, `2000s`,
`90-те`, `2000те`, `от 2000 до 2010`.

**Stored year metadata is useless for this and is not used as a filter.** The
album literally called *"Azis 2014"* reports `year: 2023`, and yt-dlp agrees,
because both record when it reached YouTube rather than when the music is
from. Filtering on it would exclude exactly what you asked for. What works
instead:

* **the era goes into the search** — "chalga 2005" returns mid-2000s Azis
  while "chalga 2020" returns current tracks, because YouTube's own index
  understands it
* **a range is sampled, not asked once** — 2000-2010 searches 2000, 2003,
  2006, 2009 and "2000s", so the whole period is represented
* **years in titles and album names are trusted**, since that is where
  "Планета Пайнер 2005" and "Azis 2014" put them honestly

### Staying on what you asked for

A request used to steer only the next few tracks; the queue then topped itself
up from the whole library and drifted back to normal within a song or two.
Now a request sets a **focus** — shown in the app with a Clear button, and
lasting an hour by default (`taste.focus_minutes`). While it is on, the queue
is *replaced* rather than prepended to, every later pick comes from the same
request, and when the pool runs low it is topped up with more of the same
search. So the unheard tracks match what you asked for too, which is the whole
point of asking.

### Two traps this hit, both worth knowing

**Namespace collisions.** Searching "chalga" turns up a Hungarian band
literally called Chalga. It matched the word perfectly, and the text embedding
actively *preferred* it — those tracks scored 0.91 semantic similarity against
0.775 for real Azis, because the artist name is the query. So an act whose name
simply *is* the search term, and that you have never actually listened to, is
penalised decisively.

**"Listened to" has to mean listened to.** That penalty first failed because a
track started and skipped after two seconds had recorded a play, which was
enough to vouch for its own artist. Anything reasoning about which artists you
like now requires a completed play or 80% listened -- an early skip is evidence
against an artist, not for one.

Where your history *does* have a clear opinion, the search is grounded in it:
"azis 2005" searches the artist you actually play, while "jazz 1960s" -- which
your library knows nothing about -- is left as a plain era search rather than
being bent towards chalga.

---

---

## Home Assistant

**Out** — published over the REST API, no custom integration needed:

- `sensor.dj_now_playing` — with `reason`, `sink`, `position`, `video_id`
- `sensor.dj_moment` — what it thinks you are doing, and why
- `binary_sensor.dj_playing`
- `sensor.dj_library`

**In** — presence per room, sleep mode, lights, weather, who is home, all fed
into every choice.

**Control** — copy [`homeassistant/mind_dj.yaml`](homeassistant/mind_dj.yaml) into your config. It adds
`rest_command`s, JARVIS voice tools (`DjPlayMusic`, `DjRequestMusic`, `DjSkip`,
`DjLove`, `DjPlayHere`…) as `intent_script` entries — since sentence triggers do not fire reliably behind a
conversation agent — and three optional automations, all shipped disabled.

---

---

## Your phone, outside the house

Set `server.access_token` in `config.json` (git-ignored) to a long random string.
Every route then returns 403 without it -- including `/stream/<id>`, so the audio is covered too. Present it
as `?key=<token>`, an `X-DJ-Key` header, or the cookie that `?key=` leaves
behind (good for a year).

`server.tunnel: true` brings up a **Cloudflare quick tunnel** alongside the
server, so the app is reachable anywhere with no account, no domain and no
port forwarding. Verified end to end: 403 without the token from the public
internet, 200 with it, audio streaming at ~100-150 KB/s against the ~16 KB/s
realtime needs.

Quick tunnels are disposable: cloudflared drops one occasionally and each new
one gets a different address. So it is supervised -- the process is restarted
when it dies, and every new address is published three ways:

* printed in the server console
* pushed to Home Assistant as **`sensor.dj_remote_url`** (with the full
  tokenised link in its `link` attribute, so it is one tap from a dashboard)
* shown in the app itself, with a Copy button, under "Away from home"

Because of that churn, do not bookmark the address on your phone -- open Home
Assistant and follow the sensor, or copy it from the app while you are on the
home wifi. A stable hostname would need a domain in Cloudflare, which is the
only thing a named tunnel buys over this.

Run it standalone instead, if you would rather:

```bash
.venv\Scripts\python -m dj.cli tunnel
```

It refuses to start when `access_token` is empty, because a tunnel makes this
reachable from the whole internet and an unprotected DJ on the internet is
someone else's speakers.

---

---

## Ollama

Used for three things, none of them on the critical path:

- naming the moment ("Sunday night, winding down")
- turning "something chill for cooking" into search terms
- the occasional line of DJ patter

`nomic-embed-text` does the embeddings, `qwen3:8b` the text. **If Ollama is not
running, everything still works** — requests fall back to keyword parsing and
the moment gets a plain description.

---

---

## Under the hood

The rest of this README covers the engineering details: how sound is measured, how outputs are kept in sync, and the bugs that shaped the design.

## What a track sounds like

The embeddings are built from text, so they know Galena sits near Emanuela but
cannot tell a ballad from a banger. Since "what do I want at eleven at night"
is mostly a question about *energy*, every track is also measured: ffmpeg
decodes a minute from the middle and numpy does the rest. No librosa, nothing
new to install.

Four numbers steer selection — `energy`, `brightness`, `dynamics`, `punch` —
and the DJ learns the *sound* you keep at each kind of moment, the same way it
learns which artists you keep. Measured spread:

| | energy | brightness |
|---|---|---|
| ambient pad | 0.20 | 0.09 |
| jazz | 0.66 | 0.60 |
| slow rock | 0.69 | 0.36 |
| pop / pop-folk | 0.90–0.93 | 0.78–0.89 |

Every scale is calibrated against real measurements, not guessed: the obvious
mappings squash everything into the top of the range, because modern masters
are all loud. Spectral flux turned out to be the best single predictor of felt
energy and spans more than an order of magnitude, so it carries the most
weight.

**Tempo is measured but deliberately excluded from scoring.** Checked against
tracks whose tempo is not in doubt, it got them wrong often enough — a slow
ballad read as 136bpm, a fast pop-folk track as 86 — that including it would
feed noise into every comparison. Its confidence score proved no more reliable.
It is stored for later, when beat-matched transitions might want it.

Analysis happens by itself: the prefetcher measures whatever is about to play,
so the library analyses as you listen. `dj.cli analyze [n] [jobs]` does a bulk
run if you are impatient — keep `jobs` low, see the yt-dlp note below.

## Sequencing

A DJ orders songs, it does not just pick them. Each next track is nudged
towards the energy of the one before it, drifting the way the moment wants:
down as the evening winds down, up at a party, level while you work. It is a
preference, not a rule — a big leap gets quieter, not banned. Measured mean
step between consecutive tracks in wind-down: **0.09**.

## Skip, versus not now

A skip used to be one undifferentiated negative, which conflated "I don't like
this" with "love it, wrong moment" and taught the context model the opposite
of what you meant. There are now two buttons:

* **Skip** — counts against the song, as before
* **Not now** — moves on, records a much softer signal, and teaches the
  context model *nothing*

## Borrowing from moments it does know

`context_fit` used to return exactly zero for a bucket it had never seen.
Honest, but wasteful: Tuesday morning should be able to lean on Wednesday
morning. It now falls back to a similarity-weighted blend over the buckets it
does have, discounted to 0.6 because borrowed evidence is weaker than
evidence. Measured: `late|home|idle` ≈ `evening|home|idle` at 0.80,
≈ `morning|home|focus` at 0.27.

## Who else is in the room

What you want alone is not what you want with company. Two rooms occupied at
once, or a device on the guest wifi
(e.g. a router's guest-network device count sensor), marks the moment
as company — which feeds the sound and taste targets without exploding the
bucket space.

---

## Playing everywhere at once

Tap several outputs, or hit **Everywhere**. They play the same track on one
shared timeline, and the ones that can seek are held in step automatically.

How closely they hold depends on what the device is:

| output | synced | why |
|---|---|---|
| this pc | yes | ffplay restarts at `-ss <offset>`; exact |
| this device (browser) | yes | the page sets `audio.currentTime` |
| sony tv | yes | re-cast at an offset; audible, so corrected less often |
| lg tv | **no** | the YouTube app starts from zero and takes no instruction |
| echo dot | **no** | Alexa plays Amazon Music's *own copy* — a different file, no seek, no position |

Measured PC↔Sony: within about **1.5s**, steady.

Two non-obvious fixes got it there. The first: correcting to "wherever the
primary is right now" leaves a device permanently late, because the seek
itself takes time to land. So each output learns its own correction lag from
the drift observed after its last seek and aims that far ahead.

The second is the useful one. A device that is *consistently* three seconds
late is not drifting at all — it is **starting** late, and the cure is not to
keep interrupting it but to begin it three seconds further into the song.
`_start_lag` learns that head start per output and applies it at the next
track.

That has one trap in it, and the first attempt fell in. What gets measured
once the compensation is applied is the error that *remains*, so it has to be
added to the head start rather than replace it. Setting the head start equal
to the residual means a track that comes out perfectly aligned reports no
error, the compensation decays to nothing, and the next track is late all over
again — which is precisely what it did: three seconds late, then aligned, then
three seconds late. It is an integrator, not an average. It moves in bounded
steps, once per track, and ignores any drift over 8s as a bad reading rather
than learning from it.

There is also a settling window after any start or seek, because Cast reports
a stale position for a few seconds afterwards, which once looked like a
60-second drift.

Unsyncable outputs are still started, and marked with a dot in the UI and
"cannot sync" in the drift readout. Nothing pretends otherwise.

Tuning lives in `sinks.sync_tolerance` (default 1.5s before correcting) and
`sinks.sync_cooldown` (8s between corrections to one device). Tightening the
tolerance buys closer sync at the cost of more re-buffering on the Sony.

Switching outputs mid-song hands the track over at its current position rather
than restarting it, so moving the music between rooms is not recorded as a skip.

---

## Knowing when a song has ended

This sounds like the least interesting part of a music player and it was the
source of the worst bug in it, so it is worth writing down.

Songs were ending early — sometimes a minute before the end, sometimes the
instant they started. The cause was that the DJ asked the output where it was
and believed the answer. Home Assistant remembers the last known
`media_position` of whatever a device played, and it does not forget. The Sony
had spent the evening on Plex, an hour into a film, so it answered **1074
seconds** — and every three-minute track the DJ started was already past its
end before the first note. It advanced through eight tracks in forty-five
seconds.

The damage was worse than the noise, because the old code recorded
`listened = duration` whenever it called a track finished. So eight songs
nobody heard went into the history as complete, unskipped, wholehearted
listens, and the taste model learned from them. Fourteen such plays were found
and deleted, and the learned preferences rebuilt from the 287 honest ones.

Five things now stand between a bad reading and a skipped song:

1. **An output is allowed to say it does not know.** `remote_position()`
   returns a number only if the reading is stamped *after* we started this
   track and the device says it is playing. Otherwise `None`, and the caller
   falls back to something it can trust. The sync loop skips any output that
   will not vouch for its position — correcting a device against a guess moves
   one that was already in the right place.

2. **The wall clock is a ceiling.** Nothing can be further into a song than
   the song has been playing. `Player.elapsed()` is set when the track starts
   and moves forward when you unpause, and every reported position is capped
   at it. This alone makes the original failure impossible.

3. **The most trustworthy output keeps time.** The primary used to be
   whichever output appeared first in the list — which is the order you tapped
   the chips in. Tapping the TV before the PC handed the timing job to a
   device on the far side of the network while the machine actually playing
   the audio, whose position we know exactly, sat unconsulted. Outputs now
   carry a `position_trust` and the highest wins.

4. **A run of instant finishes stops the music.** Four self-ended tracks in
   thirty seconds means something is lying, so it stops and says so rather
   than burning the queue and the history. Pressing skip four times is
   ordinary and exempt.

5. **A stream that dies is not a song that ended.** Google's signed URLs
   expire and its throttling occasionally cuts a connection, and when that
   happens `ffplay` exits — which from outside looks exactly like reaching the
   end. Arriving more than five seconds short now re-resolves the audio and
   picks up at the same second, twice per track, before giving up.

---

## Speed

Pressing Skip used to take nine to eighteen seconds. It now takes about half
a second. Three things were wrong:

* **The Home Assistant bridge published on the playback path.** Four REST
  POSTs per event, two events per skip, and HA — running in VirtualBox —
  answers in 25ms most of the time but intermittently takes 2.3 seconds. The
  bridge now runs on its own thread and is only *nudged* when something
  happens, so sensors stay fresh without anything waiting on them.
* **yt-dlp ran when you pressed the button.** Resolving a stream takes about
  1.7s. The next two tracks in the queue are now resolved in the background
  while the current one plays, so by the time you skip, the URL is cached.
* **Radio results were being read with the wrong field names.** ytmusicapi
  returns `duration`/`thumbnails` from `search` but `length`/`thumbnail` from
  `get_watch_playlist`, so every track harvested from a radio was stored with
  a duration of 0 and no artwork — which left the progress bar frozen at 0:00
  and made completion impossible to compute for those tracks. `dj.cli fixmeta`
  repaired the 163 already stored that way.
* **yt-dlp got the IP flagged.** Enough requests and YouTube starts answering
  "Sign in to confirm you're not a bot", which stops playback, not just
  background work. The fix was not cookies (they made it worse: the web client
  then returned "Requested format is not available"). It was the **android
  player client**, which works with no authentication at all. The resolver now
  tries several clients in order and remembers which one worked. Keep bulk
  `analyze` jobs low for the same reason.
* **One SQLite connection was shared by five threads.** That is not safe even
  with `check_same_thread=False`: concurrent `execute()` calls interleave and
  corrupt cursor state, which showed up as `fetchone()` returning `None` from
  a `COUNT(*)` that cannot be empty. Connections are now thread-local, which
  WAL mode makes cheap.

Selection itself was never the problem — scoring 300 candidates takes 20ms.

---

---

## Layout

```
dj/config.py       settings and defaults
dj/db.py           SQLite schema, identity folding
dj/ha.py           Home Assistant client
dj/context.py      the moment, and the bucket hierarchy
dj/catalog.py      YouTube Music + yt-dlp
dj/embed.py        embeddings, vibe clustering
dj/taste.py        scoring and learning
dj/discover.py     finding unheard songs
dj/brain.py        optional Ollama layer
dj/player.py       queue, progression, learning hooks
dj/server.py       API, stream proxy, HA bridge
dj/sinks/          pc, sony, lg, echo, web
data/mind-dj.sqlite3   everything it knows about you
```

Back up `data/mind-dj.sqlite3`. That file is the DJ.

## License

[MIT](LICENSE) © Denis Kostadinov

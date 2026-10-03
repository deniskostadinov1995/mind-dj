"""HTTP + websocket front end, the audio proxy, and the Home Assistant bridge.

The stream endpoint proxies audio through this process rather than handing out
Google's URLs directly. That costs a little bandwidth but buys a lot: one
address that works identically for the browser on your phone, the Cast device
on the LAN, and the player on this machine, with Range support so seeking
without leaking short-lived signed URLs to every client.
"""
import socket
import subprocess
import threading
import time
from shutil import which

import requests
from flask import (Flask, Response, jsonify, render_template, request,
                   stream_with_context)
from flask_socketio import SocketIO

from . import context as ctxmod
from . import db
from .config import load
from .player import Player

cfg = load()
app = Flask(__name__, static_folder="../static", template_folder="../templates")
app.config["SECRET_KEY"] = cfg["server"].get("secret", "mind-dj")
# Jinja caches compiled templates in memory, so without this a tweak to the
# page only shows up after a full restart.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

player = None

# Shared secret. Empty means no gate, which is fine on the LAN but must not be
# left that way behind a public tunnel: without it, anyone holding the URL can
# drive your speakers and read your listening history. Set server.access_token
# in config.json, then open the app once as ?key=<token> and the cookie keeps
# you signed in.
ACCESS_TOKEN = (cfg["server"].get("access_token") or "").strip()
_COOKIE = "mind_dj_key"


@app.before_request
def _gate():
    if not ACCESS_TOKEN:
        return None
    supplied = (request.args.get("key")
                or request.headers.get("X-DJ-Key")
                or request.cookies.get(_COOKIE))
    if supplied == ACCESS_TOKEN:
        return None
    return Response("forbidden", status=403, mimetype="text/plain")


@app.after_request
def _remember(resp):
    if ACCESS_TOKEN and request.args.get("key") == ACCESS_TOKEN:
        resp.set_cookie(_COOKIE, ACCESS_TOKEN, max_age=60 * 60 * 24 * 365,
                        httponly=True, samesite="Lax")
    return resp


bridge = None
tunnel_sup = None

# Events worth pushing to Home Assistant the instant they happen. "tick" is
# excluded on purpose: it fires every second, and an automation waiting on
# binary_sensor.dj_playing should not have to wait out the poll interval.
_HA_EVENTS = {"now", "played", "idle", "sink", "feedback"}


def _emit(event, payload):
    try:
        socketio.emit(event, payload)
    except Exception:
        pass
    if event in _HA_EVENTS and bridge is not None:
        # Only *ask* for a publish. Doing the POSTs here would put them on the
        # playback path: Home Assistant usually answers in 25ms but spikes to
        # over two seconds, and at four POSTs per event that turned a skip into
        # a ten second wait. The bridge thread does the talking instead.
        bridge.request()


def lan_ip():
    """The address other devices on the network can reach us on."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ------------------------------------------------------------------- pages

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/manifest.webmanifest")
def manifest():
    return jsonify({
        "name": "mind-dj",
        "short_name": "mind-dj",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0b0d10",
        "theme_color": "#0b0d10",
        "icons": [],
    })


# --------------------------------------------------------------------- api

@app.route("/api/state")
def api_state():
    return jsonify(player.state())


@app.route("/api/play", methods=["POST"])
def api_play():
    body = request.get_json(silent=True) or {}
    if body.get("video_id"):
        ok = player.play_video(body["video_id"])
    else:
        ok = player.start()
    return jsonify({"ok": bool(ok), "state": player.state()})


@app.route("/api/pause", methods=["POST"])
def api_pause():
    player.pause()
    return jsonify({"ok": True})


@app.route("/api/resume", methods=["POST"])
def api_resume():
    player.resume()
    return jsonify({"ok": True})


@app.route("/api/next", methods=["POST"])
def api_next():
    """Skip. reason distinguishes "not this" from "not right now"."""
    body = request.get_json(silent=True) or {}
    reason = body.get("reason", "skipped")
    if reason not in ("skipped", "not_now", "stopped"):
        reason = "skipped"
    player.next(reason)
    return jsonify({"ok": True, "state": player.state()})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    player.stop()
    return jsonify({"ok": True})


@app.route("/api/volume", methods=["POST"])
def api_volume():
    body = request.get_json(silent=True) or {}
    player.set_volume(int(body.get("level", 80)))
    return jsonify({"ok": True})


@app.route("/api/sink", methods=["POST"])
def api_sink():
    """Choose the output(s). Pass "sink" for one, or "sinks" for several."""
    body = request.get_json(silent=True) or {}
    names = body.get("sinks")
    if names is None:
        names = [body.get("sink", "pc")]
    ok = player.set_sinks(names)
    return jsonify({"ok": ok, "state": player.state()})


@app.route("/api/tunnel")
def api_tunnel():
    """Where the DJ can be reached from outside, if a tunnel is up."""
    if tunnel_sup is None:
        return jsonify({"enabled": False})
    return jsonify({"enabled": True, **tunnel_sup.status()})


@app.route("/api/focus", methods=["POST"])
def api_focus():
    """Set or clear what the DJ is locked onto."""
    body = request.get_json(silent=True) or {}
    if body.get("clear"):
        player.clear_focus()
    elif body.get("text"):
        player.set_focus(body["text"])
    return jsonify({"ok": True, "focus": player.focus_state()})


@app.route("/api/sync")
def api_sync():
    return jsonify({"active": player.active, "sync": player.sync_report()})


@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    body = request.get_json(silent=True) or {}
    ok = player.feedback(body.get("kind", "like"), body.get("track_id"))
    return jsonify({"ok": bool(ok)})


@app.route("/api/request", methods=["POST"])
def api_request():
    body = request.get_json(silent=True) or {}
    return jsonify(player.request(body.get("text", "")))


@app.route("/api/mode", methods=["POST"])
def api_mode():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode")
    player.mode_override = mode if mode in ctxmod.ACTIVITIES else None
    player.taste.invalidate()
    return jsonify({"ok": True, "state": player.state()})


@app.route("/api/discovery", methods=["POST"])
def api_discovery():
    body = request.get_json(silent=True) or {}
    try:
        player.discovery_bias = max(0.0, min(3.0, float(body.get("bias", 1.0))))
    except (TypeError, ValueError):
        pass
    return jsonify({"ok": True, "discovery_bias": player.discovery_bias})


@app.route("/api/queue")
def api_queue():
    return jsonify({"queue": player.queue})


@app.route("/api/suggest")
def api_suggest():
    n = int(request.args.get("n", 10))
    ctx = player.context()
    picks = player.pick(n=n, ctx=ctx)
    return jsonify({"moment": ctxmod.describe(ctx), "picks": picks})


@app.route("/api/seek", methods=["GET"])
def api_seek():
    """Where the browser tab should be, if it is one of the outputs."""
    sink = player.sinks.get("web")
    return jsonify({
        "seek_to": getattr(sink, "seek_to", 0.0),
        "epoch": getattr(sink, "seek_epoch", 0),
        "active": "web" in player.active,
    })


@app.route("/api/report", methods=["POST"])
def api_report():
    """The web player telling us where it is in the track."""
    body = request.get_json(silent=True) or {}
    sink = player.sinks.get("web")
    if sink and hasattr(sink, "report"):
        sink.report(float(body.get("position", 0)), bool(body.get("ended")))
    return jsonify({"ok": True})


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    return jsonify({"results": player.cat.search(q, limit=12)})


@app.route("/api/history")
def api_history():
    rows = player.conn.execute(
        "SELECT p.started_at, p.completion, p.outcome, p.bucket, t.title, t.artist,"
        " t.video_id FROM plays p JOIN tracks t ON t.id = p.track_id"
        " ORDER BY p.started_at DESC LIMIT 60"
    ).fetchall()
    return jsonify({"history": [dict(r) for r in rows]})


@app.route("/api/insights")
def api_insights():
    """What the DJ thinks it has learned. Useful for trusting or correcting it."""
    conn = player.conn
    by_part = conn.execute(
        "SELECT bucket, COUNT(*) n, AVG(completion) c FROM plays"
        " WHERE bucket <> '' GROUP BY bucket ORDER BY n DESC LIMIT 12"
    ).fetchall()
    vibes = conn.execute(
        "SELECT v.id, v.label, v.size,"
        " (SELECT SUM(pos) FROM ctx_pref cp WHERE cp.vibe_id = v.id) pos,"
        " (SELECT SUM(neg) FROM ctx_pref cp WHERE cp.vibe_id = v.id) neg"
        " FROM vibes v ORDER BY v.size DESC LIMIT 15"
    ).fetchall()
    top = conn.execute(
        "SELECT t.title, t.artist, COUNT(*) n, AVG(p.completion) c"
        " FROM plays p JOIN tracks t ON t.id = p.track_id"
        " GROUP BY p.track_id ORDER BY n DESC LIMIT 15"
    ).fetchall()
    return jsonify({
        "buckets": [dict(r) for r in by_part],
        "vibes": [dict(r) for r in vibes],
        "top": [dict(r) for r in top],
        "stats": db.stats(conn),
    })


# ------------------------------------------------------------------ stream

# Google throttles a single long GET from googlevideo down to roughly twice
# real time, which is not enough headroom for a Cast device and makes it give
# up mid-song. Pulling the same bytes as a series of bounded Range requests
# runs at full speed. This is the same trick yt-dlp uses via --http-chunk-size.
_CHUNK = 2 * 1024 * 1024


def _parse_range(header, total):
    """Return (start, end) inclusive for a single-range request, or None."""
    if not header or not header.startswith("bytes=") or not total:
        return None
    spec = header[6:].split(",")[0].strip()
    try:
        if spec.startswith("-"):
            length = int(spec[1:])
            return max(0, total - length), total - 1
        start_s, _, end_s = spec.partition("-")
        start = int(start_s)
        end = int(end_s) if end_s else total - 1
        return start, min(end, total - 1)
    except ValueError:
        return None


@app.route("/audio/<video_id>.mp3")
def audio_mp3(video_id):
    """The same audio, transcoded to MP3 on the fly.

    AirPlay receivers are fed through pyatv, which decodes with miniaudio, and
    miniaudio cannot read AAC -- handing it our normal m4a stream fails with
    "failed to decode file". MP3 it can read, and unlike WAV it streams
    happily over HTTP without a meaningful length in the header.

    ?ss=<seconds> starts partway in, which is how an AirPlay output joins a
    group already in progress.
    """
    info = player.cat.stream_info(video_id)
    if not info or not info.get("url"):
        return Response("no stream", status=404)
    try:
        start = max(0.0, float(request.args.get("ss", 0)))
    except (TypeError, ValueError):
        start = 0.0

    ff = which("ffmpeg")
    if not ff:
        return Response("ffmpeg not found", status=500)
    cmd = [ff, "-hide_banner", "-loglevel", "error"]
    if start > 0.1:
        cmd += ["-ss", f"{start:.2f}"]
    headers = info.get("headers") or {}
    if headers.get("User-Agent"):
        cmd += ["-user_agent", headers["User-Agent"]]
    cmd += ["-i", info["url"], "-vn", "-f", "mp3", "-b:a", "192k", "-"]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)

    def pump():
        try:
            while True:
                chunk = proc.stdout.read(32 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    resp = Response(stream_with_context(pump()), content_type="audio/mpeg")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/stream/<video_id>")
def stream(video_id):
    info = player.cat.stream_info(video_id)
    if not info or not info.get("url"):
        return Response("no stream", status=404)

    up_headers = dict(info.get("headers") or {})
    up_headers.pop("Range", None)
    total = int(info.get("filesize") or 0)

    # Without a known size we cannot serve ranges properly, so ask upstream.
    if not total:
        try:
            probe = requests.get(info["url"], headers={**up_headers, "Range": "bytes=0-1"},
                                 stream=True, timeout=15)
            cr = probe.headers.get("Content-Range", "")
            probe.close()
            if "/" in cr:
                total = int(cr.rsplit("/", 1)[1])
        except Exception:
            total = 0

    rng = _parse_range(request.headers.get("Range"), total)
    if rng:
        start, end = rng
        status, length = 206, end - start + 1
    else:
        start, end = 0, (total - 1 if total else None)
        status, length = 200, (total or None)

    def pump():
        pos, url = start, info["url"]
        stale_retried = False
        while end is None or pos <= end:
            stop = pos + _CHUNK - 1
            if end is not None:
                stop = min(stop, end)
            try:
                r = requests.get(url, stream=True, timeout=25,
                                 headers={**up_headers, "Range": f"bytes={pos}-{stop}"})
                if r.status_code in (403, 410) and not stale_retried:
                    # Signed URLs expire; resolve once more and carry on.
                    stale_retried = True
                    r.close()
                    fresh = player.cat.stream_info(video_id, force=True)
                    if not fresh:
                        return
                    url = fresh["url"]
                    continue
                got = 0
                for block in r.iter_content(64 * 1024):
                    if block:
                        got += len(block)
                        yield block
                r.close()
            except Exception:
                return
            if not got:
                return
            pos += got
            if end is None and got < _CHUNK:
                return

    resp = Response(stream_with_context(pump()), status=status,
                    content_type=info.get("headers", {}).get("Content-Type")
                    or "audio/mp4")
    if length is not None:
        resp.headers["Content-Length"] = str(length)
    if status == 206:
        resp.headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Cache-Control"] = "no-store"
    return resp


# --------------------------------------------------------------- ha bridge

class HABridge(threading.Thread):
    """Publishes what the DJ is doing back into Home Assistant.

    These are REST-set states, so they appear immediately and can be used in
    automations and on dashboards without any custom integration.
    """

    def __init__(self, player, interval=15):
        super().__init__(daemon=True, name="ha-bridge")
        self.player = player
        self.interval = interval
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.last_publish = 0.0
        self.slowest = 0.0

    def run(self):
        while not self._stop.is_set():
            # Wake either on demand or on the heartbeat, whichever comes first.
            self._wake.wait(timeout=self.interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                t0 = time.time()
                self.publish()
                self.slowest = max(self.slowest, time.time() - t0)
                self.last_publish = time.time()
            except Exception:
                pass

    def request(self):
        """Ask for a publish as soon as possible. Never blocks the caller."""
        self._wake.set()

    def publish(self):
        ha = self.player.ha
        if not ha.enabled:
            return
        st = self.player.state()
        cur = st.get("current") or {}
        now_playing = (f"{cur.get('artist')} - {cur.get('title')}"
                       if cur else "idle")
        ha.set_state("sensor.dj_now_playing", now_playing[:250], {
            "friendly_name": "DJ now playing",
            "icon": "mdi:music-note",
            "title": cur.get("title", ""),
            "artist": cur.get("artist", ""),
            "video_id": cur.get("video_id", ""),
            "thumbnail": cur.get("thumbnail", ""),
            "reason": st.get("reason", ""),
            "sink": st.get("sink"),
            "position": st.get("position"),
            "duration": cur.get("duration_sec", 0),
        })
        ha.set_state("sensor.dj_moment", st["moment"]["activity"] or "unknown", {
            "friendly_name": "DJ moment",
            "icon": "mdi:brain",
            "description": st["moment"]["text"],
            "name": st["moment"].get("name") or "",
            "bucket": st["moment"]["bucket"],
        })
        ha.set_state("binary_sensor.dj_playing", "on" if st["playing"] else "off", {
            "friendly_name": "DJ playing",
            "device_class": "sound",
            "icon": "mdi:disc-player",
        })
        stats = st.get("stats") or {}
        ha.set_state("sensor.dj_library", stats.get("tracks", 0), {
            "friendly_name": "DJ library size",
            "icon": "mdi:library-music",
            "unit_of_measurement": "tracks",
            **{k: v for k, v in stats.items()},
        })

    def stop(self):
        self._stop.set()
        self._wake.set()


# ------------------------------------------------------------------ sockets

@socketio.on("connect")
def on_connect():
    socketio.emit("now", player.state())


@socketio.on("report")
def on_report(data):
    sink = player.sinks.get("web")
    if sink and hasattr(sink, "report"):
        sink.report(float((data or {}).get("position", 0)),
                    bool((data or {}).get("ended")))


# --------------------------------------------------------------------- main

def create():
    global player, bridge, tunnel_sup
    player = Player(cfg, on_event=_emit)
    port = cfg["server"]["port"]
    player.stream_base = f"http://{lan_ip()}:{port}"
    player._publish_stream_base()

    # Warm the chat model in the background so the first narration is not a
    # fifty second wait. Nothing on the playback path depends on it finishing.
    if cfg["ollama"].get("enabled", True):
        player.brain.warm()

    bridge = HABridge(player)
    bridge.start()

    # Quick tunnels get a new address every time they come up, so rather than
    # make that a chore, supervise one and publish wherever it lands.
    if cfg["server"].get("tunnel"):
        from . import tunnel as tunnelmod
        token = (cfg["server"].get("access_token") or "").strip()
        if not token:
            print("  tunnel: refusing without server.access_token", flush=True)
        elif not tunnelmod.available():
            print("  tunnel: cloudflared not installed", flush=True)
        else:
            tunnel_sup = tunnelmod.Supervisor(
                port=port, ha=player.ha, token=token,
                on_url=lambda u: print(f"  reachable at {u}/?key={token}",
                                       flush=True))
            tunnel_sup.start()
    player.fill_queue()
    return app, socketio, player


def stop_all():
    if tunnel_sup is not None:
        tunnel_sup.stop()
    if bridge is not None:
        bridge.stop()
    if player is not None:
        player.shutdown()


def main():
    _app, sio, p = create()
    host, port = cfg["server"]["host"], cfg["server"]["port"]
    print(f"  mind-dj on http://{lan_ip()}:{port}  (sink={p.sink_name})", flush=True)
    print(f"  library: {db.stats(p.conn)}", flush=True)
    sio.run(_app, host=host, port=port, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()

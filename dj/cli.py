"""Command line for setup and maintenance.

    python -m dj.cli stats
    python -m dj.cli auth               sign in to YouTube Music (then: auth status)
    python -m dj.cli seed ytmusic       your history, likes, library, playlists
    python -m dj.cli seed jukebox
    python -m dj.cli seed takeout  "C:\\path\\to\\Takeout" [all]
    python -m dj.cli seed spotify  "C:\\path\\to\\my_spotify_data"
    python -m dj.cli refresh            embed anything new, then recluster
    python -m dj.cli harvest [rounds]   pull unheard songs from the YTM graph
    python -m dj.cli suggest [n]        what would it play right now
    python -m dj.cli fixmeta            repair missing durations/artwork
    python -m dj.cli analyze [n] [jobs] measure energy/brightness of tracks
    python -m dj.cli lg pair            pair directly with the LG TV
    python -m dj.cli tunnel             reach it from outside the house
    python -m dj.cli doctor             check every moving part
    python -m dj.cli serve
"""
import sys
import time

from . import context as ctxmod
from . import db, embed, maintain, seed
from .brain import Brain
from .catalog import Catalog
from .config import load
from .ha import HA


def _p(*a):
    print(*a, flush=True)


def cmd_stats(cfg, args):
    conn = db.init()
    for k, v in db.stats(conn).items():
        _p(f"  {k:10} {v}")
    unheard = conn.execute(
        "SELECT COUNT(*) n FROM tracks t WHERE t.banned = 0"
        " AND NOT EXISTS (SELECT 1 FROM plays p WHERE p.track_id = t.id)"
    ).fetchone()["n"]
    _p(f"  {'unheard':10} {unheard}")


def cmd_seed(cfg, args):
    what = args[0] if args else "jukebox"
    conn = db.init()
    t0 = time.time()

    def prog(i, n):
        _p(f"   ... {i}{'/' + str(n) if n else ''}  ({time.time() - t0:.0f}s)")

    if what == "jukebox":
        path = args[1] if len(args) > 1 else cfg["seed"]["jukebox_library"]
        res = seed.import_jukebox(conn, path,
                                  activity=cfg["seed"]["jukebox_context_bucket"],
                                  progress=prog)
    elif what in ("ytmusic", "youtube", "yt"):
        res = seed.import_ytmusic(conn, progress=prog)
    elif what == "spotify":
        if len(args) < 2:
            _p("  need a path to the Spotify export folder"); return
        res = seed.import_spotify(conn, args[1], progress=prog)
    elif what == "takeout":
        if len(args) < 2:
            _p("  need a path to the Takeout folder"); return
        # "all" also imports plain YouTube watches, not just YouTube Music.
        music_only = "all" not in args[2:]
        res = seed.import_takeout(conn, args[1], progress=prog,
                                  music_only=music_only)
    else:
        _p(f"  unknown source {what!r}; use ytmusic, jukebox, spotify or takeout")
        return

    _p(f"  {res}")
    if isinstance(res, dict) and res.get("error"):
        return  # nothing was imported, so nothing needs re-clustering
    _p("  refreshing embeddings and clusters")
    _p(f"  {maintain.refresh(conn, cfg, progress=prog)}")


def cmd_auth(cfg, args):
    """Sign in to YouTube Music so we can read your history and likes."""
    from . import ytauth

    if args and args[0] == "status":
        info, err = ytauth.verify()
        if err:
            _p(f"  not signed in ({err})")
        else:
            name = (info or {}).get("accountName") or "unknown account"
            _p(f"  signed in as {name}")
        return

    if args and args[0] == "forget":
        for f in (ytauth.AUTH_PATH, ytauth.HEADERS_PATH):
            if f.exists():
                f.unlink()
                _p(f"  removed {f}")
        return

    path, err = ytauth.setup_from_headers()
    if err:
        _p(f"  {err}")
        _p(ytauth.INSTRUCTIONS)
        return
    info, verr = ytauth.verify()
    if verr:
        _p(f"  saved {path}, but it does not work: {verr}")
        return
    name = (info or {}).get("accountName") or "your account"
    _p(f"  signed in as {name}")
    _p(f"  credential saved to {path}")
    _p("  you can delete data/ytmusic_headers.txt now")
    _p("\n  next:  python -m dj.cli seed ytmusic")


def cmd_fixmeta(cfg, args):
    """Fill in durations and artwork that were stored as blank.

    Tracks harvested from a radio came back with duration 0 and no thumbnail,
    because get_watch_playlist names those fields "length" and "thumbnail"
    while search calls them "duration"/"thumbnails". The reader handles both
    now; this repairs what the old one already wrote.
    """
    conn = db.init()
    cat = Catalog(cfg)
    rows = conn.execute(
        "SELECT id, video_id, title FROM tracks"
        " WHERE duration_sec IS NULL OR duration_sec <= 0 OR thumbnail = ''"
    ).fetchall()
    _p(f"  {len(rows)} tracks need repair")
    fixed = failed = 0
    for i, r in enumerate(rows, 1):
        d = cat.details(r["video_id"])
        if not d or not d.get("duration_sec"):
            failed += 1
            continue
        with conn:
            conn.execute(
                "UPDATE tracks SET duration_sec = ?,"
                " thumbnail = CASE WHEN thumbnail = '' THEN ? ELSE thumbnail END"
                " WHERE id = ?",
                (d["duration_sec"], d.get("thumbnail", ""), r["id"]),
            )
        fixed += 1
        if i % 25 == 0:
            _p(f"   ... {i}/{len(rows)}  fixed={fixed} failed={failed}")
    _p(f"  fixed {fixed}, could not resolve {failed}")


def cmd_airplay(cfg, args):
    """Pair with and probe AirPlay receivers (the LG and the Sony)."""
    import asyncio
    from . import airplay

    sub = args[0] if args else "info"
    host = args[1] if len(args) > 1 else cfg["sinks"].get("lg", {}).get("host")

    if sub == "scan":
        for d in asyncio.run(airplay.scan()):
            svc = ", ".join(f"{x.protocol.name}:{x.port}" for x in d.services)
            _p(f"  {d.name:<22} {d.address}   {svc}")
        return

    if sub == "info":
        d = asyncio.run(airplay.describe(host))
        if not d:
            _p(f"  nothing at {host}"); return
        _p(f"  {d['name']} @ {d['address']}  ({d['identifier']})")
        for s in d["services"]:
            _p(f"     {s['protocol']:<10} port={s['port']:<6} "
               f"pairing={s['pairing']:<10} creds={s['have_credentials']}")
        return

    if sub == "pair":
        _p(f"  pairing with {host}")
        _p(f"  A PIN WILL APPEAR ON THE TV. Write it into:")
        _p(f"     {airplay.PIN_PATH}")
        creds, err = asyncio.run(airplay.pair(host))
        _p(f"  {'paired, credentials stored' if creds else 'failed: ' + str(err)}")
        return

    if sub == "pin":
        airplay.PIN_PATH.write_text((args[1] if len(args) > 1 else "").strip(),
                                    encoding="utf-8")
        _p(f"  wrote PIN to {airplay.PIN_PATH}")
        return

    if sub == "caps":
        caps, err = asyncio.run(airplay.capabilities(host))
        _p(f"  {err}" if err else "")
        for k, v in (caps or {}).items():
            _p(f"     {k:<12} {v}")
        return

    if sub == "play":
        url = args[2] if len(args) > 2 else None
        if not url:
            _p("  usage: airplay play <host> <url>"); return
        ok, err = asyncio.run(airplay.play_url(host, url))
        _p("  playing" if ok else f"  failed: {err}")
        return

    _p("  usage: airplay [scan|info|pair|pin <digits>|caps|play <host> <url>]")


def cmd_analyze(cfg, args):
    """Measure what tracks sound like: energy, brightness, dynamics, punch.

    Each track needs a yt-dlp resolve plus an ffmpeg decode, so this is the
    slow one. It runs a few at a time and can be stopped and resumed -- only
    unanalysed tracks are touched. The player also analyses whatever it is
    about to play, so this is a head start rather than a prerequisite.
    """
    from concurrent.futures import ThreadPoolExecutor
    from . import audio

    if not audio.available():
        _p("  ffmpeg not found; cannot analyse audio")
        return
    limit = int(args[0]) if args and args[0].isdigit() else None
    workers = int(args[1]) if len(args) > 1 and args[1].isdigit() else 4

    conn = db.init()
    rows = conn.execute(
        "SELECT t.id, t.video_id, t.title FROM tracks t"
        " LEFT JOIN features f ON f.track_id = t.id"
        " WHERE f.track_id IS NULL AND t.banned = 0 AND t.unplayable = 0"
        # Things you have actually played matter most, so measure them first.
        " ORDER BY (SELECT COUNT(*) FROM plays p WHERE p.track_id = t.id) DESC,"
        " t.id DESC" + (f" LIMIT {limit}" if limit else "")
    ).fetchall()
    _p(f"  {len(rows)} tracks to analyse, {workers} at a time")
    if not rows:
        return

    cat = Catalog(cfg)
    done = {"ok": 0, "fail": 0}
    t0 = time.time()

    def work(row):
        try:
            info = cat.stream_info(row["video_id"])
            if not info or not info.get("url"):
                return row, None
            return row, audio.measure(info["url"], info.get("headers"))
        except Exception:
            return row, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (row, feat) in enumerate(pool.map(work, rows), 1):
            if feat and db.save_features(db.connect(), row["id"], feat):
                done["ok"] += 1
            else:
                done["fail"] += 1
            if i % 25 == 0:
                rate = i / max(1e-6, time.time() - t0)
                left = (len(rows) - i) / max(rate, 1e-6)
                _p(f"   ... {i}/{len(rows)}  ok={done['ok']} fail={done['fail']}"
                   f"  ~{left/60:.0f}m left")
    _p(f"  analysed {done['ok']}, failed {done['fail']}, in {(time.time()-t0)/60:.1f}m")


def cmd_tunnel(cfg, args):
    """Expose the DJ to the internet through Cloudflare, and say where."""
    from . import tunnel
    from .ha import HA

    if not tunnel.available():
        _p("  cloudflared is not installed")
        return
    token = (cfg["server"].get("access_token") or "").strip()
    if not token:
        _p("  REFUSING: server.access_token is empty in config.json.")
        _p("  A tunnel makes this reachable from the whole internet, and")
        _p("  without a token anyone with the URL drives your speakers.")
        return

    ha = HA(cfg)
    t = tunnel.QuickTunnel(port=cfg["server"]["port"],
                           on_url=lambda u: tunnel.publish(ha, u, token))
    _p("  starting a quick tunnel ...")
    url, err = t.start()
    if err:
        _p(f"  failed: {err}")
        return
    _p("")
    _p("  open this on your phone:")
    _p(f"     {url}/?key={token}")
    _p("")
    _p("  also published to Home Assistant as sensor.dj_remote_url")
    _p("  (this address changes each restart; Ctrl+C to stop)")
    try:
        while t.proc and t.proc.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        t.stop()
        _p("")
        _p("  tunnel closed")


def cmd_lg(cfg, args):
    """Pair with and drive the LG TV directly, bypassing Home Assistant."""
    from . import webos

    sub = args[0] if args else "status"
    host = cfg["sinks"].get("lg", {}).get("host") or ""

    if sub == "find":
        _p("  scanning for LG TVs (checking the webOS handshake, not just an"
           " open port) ...")
        for ip in webos.discover():
            _p(f"    {ip}")
        return

    if not host:
        _p("  no host configured. Set sinks.lg.host in config.json, or run:"
           "  python -m dj.cli lg find")
        return
    client = webos.WebOSClient(host)

    if sub == "pair":
        _p(f"  pairing with {host}")
        _p("  ACCEPT THE PROMPT ON THE TV SCREEN within 60 seconds ...")
        key, err = client.register(prompt_timeout=60)
        _p(f"  {'paired, key stored' if key else 'failed: ' + str(err)}")
        return

    if sub == "status":
        info, err = client.foreground_app()
        if err:
            _p(f"  {host}: {err}")
        else:
            _p(f"  {host}: app={info.get('appId')} running")
        vol, verr = client.volume()
        if not verr:
            _p(f"  volume={vol.get('volume')} muted={vol.get('muted')}")
        return

    if sub == "toast":
        msg = " ".join(args[1:]) or "hello from mind-dj"
        _, err = client.toast(msg)
        _p(f"  {'sent' if not err else err}")
        return

    if sub == "play":
        vid = args[1] if len(args) > 1 else "G7MQKWiG1QQ"
        _, err = client.launch_youtube(vid)
        _p(f"  {'launched ' + vid if not err else err}")
        return

    _p("  usage: lg [find|pair|status|toast <msg>|play <videoId>]")


def cmd_refresh(cfg, args):
    conn = db.init()
    t0 = time.time()
    res = maintain.refresh(conn, cfg,
                           progress=lambda d, n: _p(f"   ... {d}/{n}"))
    _p(f"  {res}  in {time.time() - t0:.0f}s")


def cmd_harvest(cfg, args):
    from .discover import Prospector
    from .taste import Taste
    rounds = int(args[0]) if args else 3
    conn = db.init()
    ha = HA(cfg)
    ctx = ctxmod.snapshot(ha)
    tst = Taste(conn, cfg)
    pros = Prospector(conn, tst, cfg)
    _p(f"  moment: {ctxmod.describe(ctx)}")
    total = 0
    for i in range(rounds):
        t0 = time.time()
        n = pros.harvest(ctx, budget=3)
        m = pros.harvest_neighbours(ctx, artists=2) if n < 15 else 0
        total += n + m
        _p(f"  round {i + 1}: +{n} from radios, +{m} from related artists"
           f"  ({time.time() - t0:.0f}s)")
    _p(f"  harvested {total} new tracks")
    _p(f"  {maintain.refresh(conn, cfg)}")
    cmd_stats(cfg, [])


def cmd_suggest(cfg, args):
    from .player import Player
    n = int(args[0]) if args else 10
    p = Player(cfg)
    ctx = p.context()
    _p(f"  moment : {ctxmod.describe(ctx)}")
    _p(f"  reading: {ctx['activity']}   bucket: {ctxmod.buckets(ctx)[1]}")
    _p("")
    for r in p.pick(n=n, ctx=ctx):
        tag = "NEW" if r["unheard"] else "   "
        _p(f"  {tag} {r['score']:+.2f}  {(r['artist'] or '?')[:22]:<22}"
           f" {r['title'][:34]:<34} {r['reason']}")
    p.shutdown()


def cmd_doctor(cfg, args):
    _p("  checking every moving part\n")
    ok = True

    conn = db.init()
    s = db.stats(conn)
    _p(f"  database    OK   {s['tracks']} tracks, {s['plays']} plays,"
       f" {s['embedded']} embedded, {s['vibes']} vibes")

    ha = HA(cfg)
    if ha.ping():
        st = ha.states()
        _p(f"  home asst   OK   {len(st)} entities at {ha.url}")
        missing = [e for e in cfg["homeassistant"]["context_entities"].values()
                   if e not in st]
        if missing:
            _p(f"              WARN missing context entities: {', '.join(missing)}")
    else:
        ok = False
        _p(f"  home asst   DOWN {ha.last_error}")

    emb = embed.Embedder(cfg)
    v = emb.embed_text("test")
    if v is not None:
        _p(f"  embeddings  OK   {cfg['ollama']['embed_model']}, {len(v)} dims")
    else:
        ok = False
        _p(f"  embeddings  DOWN {emb.last_error}")

    brain = Brain(cfg)
    _p(f"  chat model  {'OK  ' if brain.ping() else 'DOWN'} {cfg['ollama']['chat_model']}"
       f" (optional - only used for narration and requests)")

    from . import ytauth
    info, aerr = ytauth.verify()
    if info:
        _p(f"  yt account  OK   signed in as "
           f"{info.get('accountName') or 'your account'}")
    else:
        _p(f"  yt account  --   not signed in ({aerr})."
           f" Run: python -m dj.cli auth")

    cat = Catalog(cfg)
    hits = cat.search("Galena", limit=1)
    if hits:
        _p(f"  youtube     OK   search returned {hits[0]['title']!r}")
        url = cat.stream_url(hits[0]["video_id"])
        _p(f"  yt-dlp      {'OK   resolved an audio stream' if url else 'DOWN ' + str(cat.last_error)}")
        if not url:
            ok = False
    else:
        ok = False
        _p(f"  youtube     DOWN {cat.last_error}")

    from shutil import which
    _p(f"  ffplay      {'OK' if which('ffplay') else 'MISSING (needed for PC playback)'}")

    from .player import Player
    p = Player(cfg)
    for name, sink in p.sinks.items():
        _p(f"  sink {name:<7}{'OK  ' if sink.available() else 'unavailable'}")
    p.shutdown()

    _p("\n  " + ("all good" if ok else "some parts need attention"))


def cmd_serve(cfg, args):
    from .server import main
    main()


COMMANDS = {
    "stats": cmd_stats, "seed": cmd_seed, "refresh": cmd_refresh,
    "auth": cmd_auth, "lg": cmd_lg, "airplay": cmd_airplay, "fixmeta": cmd_fixmeta, "analyze": cmd_analyze, "tunnel": cmd_tunnel,
    "harvest": cmd_harvest, "suggest": cmd_suggest, "doctor": cmd_doctor,
    "serve": cmd_serve,
}


def main():
    cfg = load()
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        _p(__doc__)
        return
    fn = COMMANDS.get(args[0])
    if not fn:
        _p(f"unknown command {args[0]!r}\n")
        _p(__doc__)
        return
    fn(cfg, args[1:])


if __name__ == "__main__":
    main()

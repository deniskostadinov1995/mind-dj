"""Reading the moment.

Turns clock + Home Assistant state into a context snapshot, a guess at what
you are doing, and a hierarchy of buckets the taste engine learns against.

The bucket hierarchy is the trick that makes this work on small data. A play
at 22:40 on a Sunday counts as evidence for the narrow bucket
"late|home|wind_down", and also for "late|home", "late", and "*". Early on,
only the coarse levels have enough plays to say anything; as history builds,
the narrow ones take over. Nothing has to be tuned by hand for that to happen.
"""
from datetime import datetime

PARTS = [
    (0, "night"),      # 00:00-05:59
    (6, "dawn"),       # 06:00-08:59
    (9, "morning"),    # 09:00-11:59
    (12, "midday"),    # 12:00-14:59
    (15, "afternoon"), # 15:00-17:59
    (18, "evening"),   # 18:00-21:59
    (22, "late"),      # 22:00-23:59
]

ACTIVITIES = ["away", "sleep", "wind_down", "focus", "active", "idle", "party", "workout"]


#: How close two parts of the day are, for borrowing evidence between them.
#: Evening and late are neighbours; morning and night are not.
_PART_ORDER = ["night", "dawn", "morning", "midday", "afternoon", "evening", "late"]


def part_distance(a, b):
    """0 for the same part, 1 for adjacent, rising from there. Wraps midnight."""
    try:
        i, j = _PART_ORDER.index(a), _PART_ORDER.index(b)
    except ValueError:
        return len(_PART_ORDER)
    d = abs(i - j)
    return min(d, len(_PART_ORDER) - d)


def bucket_similarity(a, b):
    """How alike two bucket keys are, 0..1.

    Lets a moment with no history of its own borrow from the moments most
    like it. Without this the DJ knows nothing about Tuesday morning even
    though it has plenty of evidence about Wednesday morning.
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    pa, pb = a.split("|"), b.split("|")
    score, total = 0.0, 0.0
    # part of day
    total += 3.0
    score += 3.0 * max(0.0, 1.0 - part_distance(pa[0], pb[0]) / 2.0)
    # home/away, then activity, then weekday/weekend
    for idx, weight in ((1, 2.0), (2, 2.5), (3, 1.0)):
        if len(pa) > idx and len(pb) > idx:
            total += weight
            if pa[idx] == pb[idx]:
                score += weight
    return score / total if total else 0.0


def part_of_day(hour):
    name = PARTS[0][1]
    for start, label in PARTS:
        if hour >= start:
            name = label
    return name


def season_of(month):
    return {12: "winter", 1: "winter", 2: "winter",
            3: "spring", 4: "spring", 5: "spring",
            6: "summer", 7: "summer", 8: "summer"}.get(month, "autumn")


def _on(value):
    return str(value).lower() in ("on", "true", "home", "open", "playing", "detected")


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def snapshot(ha=None, now=None, override_mode=None):
    """Build the current context. Works with HA absent, just less richly."""
    now = now or datetime.now()
    ent = {}
    if ha is not None and getattr(ha, "enabled", False):
        try:
            cfg_ents = ha.cfg.get("context_entities", {})
            states = ha.states()
            for key, eid in cfg_ents.items():
                s = states.get(eid)
                ent[key] = s["state"] if s else None
        except Exception:
            ent = {}

    hour = now.hour
    lux = _num(ent.get("living_room_lux"))
    bed_lux = _num(ent.get("bedroom_lux"))
    home = ent.get("person") is None or _on(ent.get("person"))
    sleep_mode = _on(ent.get("sleep_mode"))
    lr = _on(ent.get("living_room_motion"))
    h1 = _on(ent.get("hallway1_motion"))
    h2 = _on(ent.get("hallway2_motion"))
    bed = _on(ent.get("bedroom_presence")) or _on(ent.get("bedroom_presence2"))
    sun_up = _on(ent.get("sun")) or str(ent.get("sun")) == "above_horizon"
    moving = sum([lr, h1, h2])

    guests = _num(ent.get("guest_devices"), 0) or 0
    ctx = {
        "ts": now.isoformat(timespec="seconds"),
        "hour": hour,
        "minute": now.minute,
        "dow": now.weekday(),
        "weekend": now.weekday() >= 5,
        "part": part_of_day(hour),
        "season": season_of(now.month),
        "home": bool(home),
        "sleep_mode": sleep_mode,
        "winter_mode": _on(ent.get("winter_mode")),
        "workout": _on(ent.get("workout")),
        "living_room": lr,
        "hallway": h1 or h2,
        "bedroom": bed,
        "rooms_active": moving,
        "lux": lux,
        "bedroom_lux": bed_lux,
        "dark": (lux is not None and lux < 30) or (lux is None and not sun_up),
        "sun_up": sun_up,
        "guests": int(guests),
        # Two rooms occupied at once means you are not alone, and so does a
        # device on the guest network. What you want with company is not what
        # you want by yourself, so this feeds the sound and taste targets.
        "company": bool(guests > 0 or moving >= 2),
        "weather": ent.get("weather"),
        "bedroom_temp": _num(ent.get("bedroom_temp")),
        "ha": bool(ent),
    }
    ctx["activity"] = override_mode or infer_activity(ctx)
    ctx["override"] = bool(override_mode)
    return ctx


def infer_activity(ctx):
    """A grounded guess at what you are doing. No cleverness for its own sake."""
    if not ctx["home"]:
        return "away"
    if ctx["workout"]:
        return "workout"
    if ctx["sleep_mode"] or (ctx["part"] in ("night", "late") and ctx["bedroom"]
                             and not ctx["living_room"]):
        return "sleep"
    if ctx["part"] in ("evening", "late") and ctx["dark"]:
        return "wind_down"
    if not ctx["weekend"] and ctx["part"] in ("morning", "midday", "afternoon"):
        return "focus"
    if ctx["rooms_active"] >= 2:
        return "active"
    return "idle"


def buckets(ctx):
    """Context keys from most specific to most general.

    Evidence is recorded against every level, so a brand-new narrow bucket
    still inherits what the broader ones already know.
    """
    # Some imported history only records *that* you played something, not when
    # in the day. Rather than invent an hour and teach the model a lie, such a
    # play produces no buckets at all: it still counts towards affinity, but it
    # says nothing about what you like at particular moments.
    if not ctx.get("part"):
        return []

    part, act = ctx["part"], ctx.get("activity") or "idle"
    where = "home" if ctx.get("home", True) else "away"
    day = "wknd" if ctx.get("weekend") else "week"
    # Note there is deliberately no catch-all "*" level. A bucket that matches
    # every moment is not context, it is popularity, and affinity already
    # measures that. Including it made context_fit report a confident match in
    # moments nothing had ever been learned about. When we genuinely know
    # nothing about now, context_fit should say zero and let the other signals
    # carry the choice.
    return [
        f"{part}|{where}|{act}|{day}",
        f"{part}|{where}|{act}",
        f"{part}|{where}",
        f"{act}",
        f"{part}",
    ]


# Weight of each level when blending learned preference. Specific levels lead,
# but only once they actually carry evidence (see taste.context_fit).
BUCKET_WEIGHTS = [1.00, 0.80, 0.55, 0.45, 0.35]


def primary_bucket(ctx):
    """The narrowest bucket, or "" for a moment whose time we do not know."""
    b = buckets(ctx)
    return b[0] if b else ""


def describe(ctx):
    """A short human line for the UI. No LLM needed for the common case."""
    bits = []
    dow = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    bits.append(f"{dow[ctx['dow']]} {ctx['hour']:02d}:{ctx['minute']:02d}")
    if not ctx["home"]:
        bits.append("out of the house")
    else:
        if ctx["activity"] == "sleep":
            bits.append("in bed")
        elif ctx["living_room"]:
            bits.append("in the living room")
        elif ctx["hallway"]:
            bits.append("moving around")
        if ctx["dark"]:
            bits.append("lights low")
    if ctx["weather"]:
        bits.append(str(ctx["weather"]))
    return ", ".join(bits)


def similarity(a, b):
    """How alike two contexts are, 0..1. Used to weight historical plays."""
    if not a or not b:
        return 0.0
    score, total = 0.0, 0.0

    def cmp(key, weight, eq=None):
        nonlocal score, total
        if key not in a or key not in b:
            return
        total += weight
        av, bv = a.get(key), b.get(key)
        if eq(av, bv) if eq else (av == bv):
            score += weight

    # Time of day matters most, and wraps around midnight.
    if "hour" in a and "hour" in b:
        total += 3.0
        d = abs(a["hour"] - b["hour"])
        d = min(d, 24 - d)
        score += 3.0 * max(0.0, 1.0 - d / 6.0)

    cmp("part", 2.0)
    cmp("activity", 2.5)
    cmp("home", 1.5)
    cmp("company", 1.5)
    cmp("weekend", 1.0)
    cmp("dark", 1.0)
    cmp("season", 0.5)
    return score / total if total else 0.0

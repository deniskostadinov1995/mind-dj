"""Configuration: file-backed, with sane defaults.

Override anything in config.json next to the project root (see config.example.json).
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
CONFIG_PATH = ROOT / "config.json"

DEFAULTS = {
    "server": {
        "host": "0.0.0.0",
        "port": 5055,
        "secret": "mind-dj",
        # Leave blank for LAN-only use. Set a long random string before
        # exposing the app through a tunnel -- see the README.
        "access_token": "",
        # Bring up a Cloudflare quick tunnel with the server, so the
        # app is reachable away from home. Needs access_token set;
        # the address changes on each restart and is published to
        # Home Assistant as sensor.dj_remote_url.
        "tunnel": False,
    },

    "ollama": {
        "url": "http://localhost:11434",
        "embed_model": "nomic-embed-text",
        "chat_model": "qwen3:8b",
        # Keep the chat model resident so narration isn't a 50s cold start.
        "keep_alive": "30m",
        # The LLM is garnish, never the spine. If it's down we still DJ.
        "enabled": True,
        "timeout": 25,
    },

    "homeassistant": {
        "url": "http://homeassistant.local:8123",
        "token_file": str(DATA / "ha-token.txt"),
        "enabled": True,
        "poll_seconds": 30,
        # Entities read to build the context vector.
        "context_entities": {
            "person": "person.you",
            "weather": "weather.forecast_home",
            "sleep_mode": "input_boolean.bedroom_sleep_mode",
            "winter_mode": "input_boolean.winter_mode",
            "living_room_motion": "binary_sensor.living_room_motion",
            "hallway1_motion": "binary_sensor.hallway_motion",
            "hallway2_motion": "binary_sensor.hallway2_motion",
            "living_room_lux": "sensor.living_room_illuminance",
            "bedroom_presence": "binary_sensor.bedroom_presence",
            "bedroom_presence2": "binary_sensor.bedroom_presence_2",
            "bedroom_lux": "sensor.bedroom_illuminance",
            "bedroom_temp": "sensor.bedroom_climate_temperature",
            "sun": "sun.sun",
            "workout": "input_boolean.ac_boost_active",
            # Devices on the guest wifi: a decent proxy for visitors.
            "guest_devices": "sensor.router_guest_network_devices",
        },
        # Sensors mind-dj publishes back into HA.
        "publish_prefix": "sensor.dj",
    },

    "catalog": {
        "language": "en",
        "location": "BG",
        # Tracks shorter/longer than this are almost never songs.
        "min_duration": 60,
        "max_duration": 900,
    },

    "sinks": {
        "default": "pc",
        # How far an output may drift, in seconds, before it is
        # seeked back into line with the primary one.
        "sync_tolerance": 1.5,
        # Seconds to leave an output alone after correcting it.
        "sync_cooldown": 8.0,
        # binary: leave blank to use ffplay. Point it at a non-Store mpv.exe
        # if you install one; the Store build cannot be controlled (see
        # dj/sinks/local_sink.py).
        "pc": {"enabled": True, "binary": "", "volume": 80},
        "sony": {"enabled": True, "entity": "media_player.living_room_tv"},
        "lg": {"enabled": True, "entity": "media_player.lg_tv"},
        "echo": {"enabled": True, "entity": "media_player.kitchen_echo"},
        "web": {"enabled": True},
    },

    "taste": {
        # Scoring weights. Tune these; they are the personality of the DJ.
        "w_sound": 0.90,         # does it *sound* like this moment wants
        "w_affinity": 1.00,      # do you like this track at all
        "w_context": 1.30,       # do you like this KIND of track in THIS moment
        "w_semantic": 0.80,      # embedding fit to what you love in this moment
        "w_dormancy": 0.70,      # "haven't heard it in ages" bonus
        "w_explore": 0.55,       # unheard-track bonus
        "w_fatigue": 1.40,       # penalty for over-rotation / same artist
        # Half-life in days for how fast old plays stop counting.
        "affinity_half_life_days": 120,
        # A play counts as "liked" above this completion fraction.
        "completion_liked": 0.80,
        "completion_skipped": 0.35,
        # Don't replay anything heard within this many hours.
        "min_replay_hours": 6,
        # Dormancy peaks around here (days since last play).
        "dormancy_sweet_spot_days": 90,
        # Fraction of the queue reserved for things you've never heard.
        "discovery_ratio": 0.30,
        # How long a typed request keeps steering the queue.
        "focus_minutes": 60,
        "queue_target": 12,
        "vibe_count": 24,
    },

    "seed": {
        # Path to a Party Jukebox jukebox_library.json to import (dj.cli seed jukebox).
        "jukebox_library": "",
        # Those plays were a New Year party, not everyday taste.
        "jukebox_context_bucket": "party",
    },
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    cfg = DEFAULTS
    if CONFIG_PATH.exists():
        try:
            cfg = _merge(DEFAULTS, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[config] {CONFIG_PATH} unreadable ({e}); using defaults")
    return cfg


def save(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def ha_token(cfg=None):
    cfg = cfg or load()
    env = os.environ.get("HA_TOKEN")
    if env:
        return env.strip()
    p = cfg["homeassistant"].get("token_file")
    if p and Path(p).exists():
        return Path(p).read_text(encoding="utf-8").strip()
    return None

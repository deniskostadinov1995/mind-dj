"""The optional narrator.

Ollama is used for three things only: naming the moment, turning a sentence
like "something chill for cooking" into search terms, and writing the odd
line of DJ patter. It is never asked what to play next.

That is a deliberate limit. A cold qwen3:8b takes about fifty seconds to load
on this machine, and the next track cannot wait fifty seconds. Everything on
the critical path is numpy. If Ollama is down, every function here returns
None and the caller falls back to something deterministic.
"""
import json
import re
import threading
import time

import requests

from . import context as ctxmod
from .config import load

_JSON = re.compile(r"\{.*\}", re.S)


class Brain:
    def __init__(self, cfg=None):
        cfg = cfg or load()
        self.cfg = cfg["ollama"]
        self.url = self.cfg["url"].rstrip("/")
        self.model = self.cfg["chat_model"]
        self.enabled = bool(self.cfg.get("enabled", True))
        self.timeout = self.cfg.get("timeout", 25)
        self.keep_alive = self.cfg.get("keep_alive", "30m")
        self.available = None
        self.last_error = None
        self._moment_cache = {}
        self._warming = False
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- core

    def _generate(self, prompt, system=None, num_predict=160, timeout=None):
        if not self.enabled:
            return None
        body = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "keep_alive": self.keep_alive,
            "options": {"num_predict": num_predict, "temperature": 0.6},
        }
        if system:
            body["system"] = system
        try:
            r = requests.post(self.url + "/api/generate", json=body,
                              timeout=timeout or self.timeout)
            r.raise_for_status()
            self.available = True
            return (r.json().get("response") or "").strip()
        except Exception as e:
            self.available = False
            self.last_error = str(e)
            return None

    def _json(self, prompt, system=None, num_predict=200):
        raw = self._generate(prompt, system, num_predict)
        if not raw:
            return None
        m = _JSON.search(raw)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None

    def warm(self, background=True):
        """Load the model into VRAM so the first real call is not a 50s wait."""
        if not self.enabled or self._warming:
            return
        def _run():
            self._warming = True
            try:
                self._generate("ok", num_predict=1, timeout=180)
            finally:
                self._warming = False
        if background:
            threading.Thread(target=_run, daemon=True, name="brain-warm").start()
        else:
            _run()

    def ping(self):
        try:
            r = requests.get(self.url + "/api/tags", timeout=4)
            self.available = r.ok
            return r.ok
        except Exception as e:
            self.available, self.last_error = False, str(e)
            return False

    # ------------------------------------------------------------- moments

    def name_moment(self, ctx):
        """A short human phrase for the current moment, cached per bucket."""
        key = ctxmod.buckets(ctx)[1]
        with self._lock:
            hit = self._moment_cache.get(key)
        if hit and hit[1] > time.time():
            return hit[0]

        facts = ctxmod.describe(ctx)
        out = self._generate(
            f"Moment: {facts}. Activity guess: {ctx.get('activity')}.\n"
            "Name the musical mood of this moment in at most four words. "
            "No punctuation, no explanation, just the phrase.",
            system="You name musical moods. You reply with a short phrase and nothing else.",
            num_predict=20,
        )
        if not out:
            return None
        out = out.strip().strip('".').split("\n")[0][:48]
        with self._lock:
            self._moment_cache[key] = (out, time.time() + 1800)
        return out

    # ------------------------------------------------------------ requests

    def parse_request(self, text, ctx=None):
        """Turn a spoken or typed request into something we can search for.

        Returns a dict with any of: mood, energy (0..1), search (list of
        query strings), artists, exclude, mode. Never raises.
        """
        if not text:
            return None
        moods = "Chill, Commute, Energize, Feel good, Focus, Party, Romance, Sleep, Workout"
        data = self._json(
            f'Listener said: "{text}"\n'
            f"Current moment: {ctxmod.describe(ctx) if ctx else 'unknown'}\n\n"
            "Return ONLY a JSON object with these keys:\n"
            '  "search": list of 1-3 YouTube Music search queries that would find '
            "fitting songs (include artist names if named)\n"
            f'  "mood": one of [{moods}] or null\n'
            '  "energy": number 0..1\n'
            '  "artists": list of artist names explicitly asked for\n'
            '  "exclude": list of artists or genres to avoid\n',
            system="You convert music requests into search parameters. "
                   "You reply with one JSON object and nothing else.",
            num_predict=220,
        )
        if not isinstance(data, dict):
            return None
        out = {
            "search": [s for s in (data.get("search") or []) if isinstance(s, str)][:3],
            "mood": data.get("mood") if isinstance(data.get("mood"), str) else None,
            "artists": [s for s in (data.get("artists") or []) if isinstance(s, str)][:5],
            "exclude": [s for s in (data.get("exclude") or []) if isinstance(s, str)][:5],
        }
        try:
            out["energy"] = max(0.0, min(1.0, float(data.get("energy"))))
        except (TypeError, ValueError):
            out["energy"] = None
        return out

    def narrate(self, track, reason, ctx):
        """One line of patter. Purely decorative; the honest reason is computed."""
        line = self._generate(
            f"Track: {track.get('artist')} - {track.get('title')}\n"
            f"Moment: {ctxmod.describe(ctx)}\n"
            f"Why it was chosen: {reason}\n\n"
            "Write one short sentence, under 15 words, as a radio DJ would say "
            "before playing it. No quotes, no emoji.",
            system="You are a warm, understated radio DJ. One sentence only.",
            num_predict=48,
        )
        return line.split("\n")[0].strip('"').strip() if line else None


def fallback_request(text):
    """Deterministic request parsing for when Ollama is not running.

    Crude on purpose: strip the filler words and hand what is left to search.
    """
    if not text:
        return None
    t = text.strip().lower()
    for lead in ("play ", "put on ", "i want ", "give me ", "something ",
                 "пусни ", "искам ", "дай ми "):
        if t.startswith(lead):
            t = t[len(lead):]
    t = re.sub(r"\b(some|a bit of|please|now|music|songs?)\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return {"search": [t] if t else [], "mood": None, "artists": [],
            "exclude": [], "energy": None} if t else None

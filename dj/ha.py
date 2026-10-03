"""Home Assistant REST client.

Deliberately forgiving: if HA is unreachable the DJ must keep working with a
thinner context rather than fall over. Every call returns a value or None.
"""
import threading
import time

import requests

from .config import ha_token, load


class HA:
    def __init__(self, cfg=None):
        cfg = cfg or load()
        self.cfg = cfg["homeassistant"]
        self.url = self.cfg["url"].rstrip("/")
        self.token = ha_token(cfg)
        self.enabled = bool(self.cfg.get("enabled") and self.token)
        self.timeout = 8
        self._states = {}
        self._fetched = 0.0
        self._devices = {}
        self._entity_cache = {}
        self._lock = threading.Lock()
        self.last_error = None
        self.online = None

    # ---------------------------------------------------------------- core

    def _headers(self):
        return {
            "Authorization": "Bearer " + (self.token or ""),
            "Content-Type": "application/json",
        }

    def states(self, max_age=None):
        """All entity states as {entity_id: state_dict}, cached briefly."""
        if not self.enabled:
            return {}
        max_age = self.cfg.get("poll_seconds", 30) if max_age is None else max_age
        with self._lock:
            if self._states and (time.time() - self._fetched) < max_age:
                return self._states
        try:
            r = requests.get(self.url + "/api/states", headers=self._headers(),
                             timeout=self.timeout)
            r.raise_for_status()
            data = {s["entity_id"]: s for s in r.json()}
            with self._lock:
                self._states, self._fetched = data, time.time()
            self.online, self.last_error = True, None
            return data
        except Exception as e:
            self.online, self.last_error = False, str(e)
            with self._lock:
                return self._states  # stale is better than nothing

    def state(self, entity_id, default=None, max_age=None):
        s = self.states(max_age=max_age).get(entity_id)
        return s["state"] if s else default

    def entity(self, entity_id, max_age=2.0):
        """One entity, read fresh, without pulling the whole state machine.

        Position polling needs a reading that is current to a second or two,
        and /api/states returns several hundred entities. Asking for the one
        we care about is cheap enough to do on every tick, and the difference
        matters: a stale media_position is indistinguishable from a real one,
        so a cached reading is not a slightly worse answer, it is a wrong one.
        """
        if not self.enabled:
            return None
        now = time.time()
        with self._lock:
            hit = self._entity_cache.get(entity_id)
        if hit and (now - hit[0]) < max_age:
            return hit[1]
        try:
            r = requests.get(f"{self.url}/api/states/{entity_id}",
                             headers=self._headers(), timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            self.last_error = f"state {entity_id}: {e}"
            return hit[1] if hit else None
        with self._lock:
            self._entity_cache[entity_id] = (now, data)
        return data

    def attr(self, entity_id, key, default=None):
        s = self.states().get(entity_id)
        return (s.get("attributes", {}) or {}).get(key, default) if s else default

    def call(self, domain, service, data=None):
        """Call a service. Returns True on success."""
        if not self.enabled:
            return False
        try:
            r = requests.post(
                f"{self.url}/api/services/{domain}/{service}",
                headers=self._headers(), json=data or {}, timeout=self.timeout,
            )
            r.raise_for_status()
            return True
        except Exception as e:
            self.last_error = f"{domain}.{service}: {e}"
            return False

    def set_state(self, entity_id, state, attributes=None):
        """Push a state into HA. Used to publish the DJ's own sensors.

        These are REST-set states: they show up immediately and survive until
        HA restarts, which is fine for a live now-playing readout.
        """
        if not self.enabled:
            return False
        try:
            r = requests.post(
                f"{self.url}/api/states/{entity_id}",
                headers=self._headers(),
                json={"state": str(state)[:255], "attributes": attributes or {}},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return True
        except Exception as e:
            self.last_error = f"set_state {entity_id}: {e}"
            return False

    def render(self, template):
        """Evaluate a Jinja template server-side. Returns the string or None."""
        if not self.enabled:
            return None
        try:
            r = requests.post(self.url + "/api/template", headers=self._headers(),
                              json={"template": template}, timeout=self.timeout)
            r.raise_for_status()
            return r.text.strip()
        except Exception as e:
            self.last_error = f"template: {e}"
            return None

    def device_id(self, entity_id):
        """The device a given entity belongs to, cached.

        Some services -- alexa_devices among them -- are addressed by device
        rather than entity, and the REST API does not expose the device
        registry, so we ask the template engine.
        """
        if entity_id in self._devices:
            return self._devices[entity_id]
        val = self.render("{{ device_id('" + entity_id + "') }}")
        if val in ("None", "", None):
            val = None
        self._devices[entity_id] = val
        return val

    def ping(self):
        try:
            r = requests.get(self.url + "/api/", headers=self._headers(), timeout=5)
            self.online = r.ok
            return r.ok
        except Exception as e:
            self.online, self.last_error = False, str(e)
            return False

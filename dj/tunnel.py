"""Reaching the DJ from outside the house, via Cloudflare.

Two ways, and which one you want depends on whether you own a domain that
Cloudflare manages:

  quick   no account, no setup, works immediately. The address is random and
          changes every time it restarts, so the URL is published to Home
          Assistant as sensor.dj_remote_url -- open that on your phone and
          you always have the current one.

  named   a stable hostname like dj.yourdomain.com. Needs `cloudflared tunnel
          login` once (it opens a browser) and a domain already in Cloudflare.

Either way the app's own access token is what actually protects it: the
tunnel makes it reachable from the whole internet, and an unprotected DJ on
the internet is someone else's speakers.
"""
import re
import subprocess
import threading
import time
from shutil import which

URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def available():
    return which("cloudflared") is not None


class QuickTunnel:
    """A trycloudflare.com tunnel, with its URL scraped from the log."""

    def __init__(self, port=5055, on_url=None):
        self.port = port
        self.on_url = on_url
        self.url = None
        self.proc = None
        self._thread = None
        self._stop = threading.Event()

    def start(self, timeout=40):
        if not available():
            return None, "cloudflared is not installed"
        self.proc = subprocess.Popen(
            [which("cloudflared"), "tunnel", "--no-autoupdate",
             "--url", f"http://localhost:{self.port}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
        )
        self._thread = threading.Thread(target=self._read, daemon=True,
                                        name="tunnel-log")
        self._thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline and not self.url:
            if self.proc.poll() is not None:
                return None, "cloudflared exited before giving us a URL"
            time.sleep(0.3)
        return (self.url, None) if self.url else (None, "timed out waiting for a URL")

    def _read(self):
        for line in self.proc.stdout:
            if self._stop.is_set():
                return
            if not self.url:
                m = URL_PATTERN.search(line)
                if m:
                    self.url = m.group(0)
                    if self.on_url:
                        try:
                            self.on_url(self.url)
                        except Exception:
                            pass

    def stop(self):
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass


class Supervisor(threading.Thread):
    """Keeps a quick tunnel alive and keeps everyone told where it is.

    Quick tunnels are disposable by design: cloudflared drops one now and
    again, and every new one gets a different address. Restarting it by hand
    and hunting for the new URL is exactly the sort of chore that makes a
    feature go unused, so this watches the process, brings it back, and
    republishes the address whenever it changes.
    """

    def __init__(self, port=5055, ha=None, token="", on_url=None,
                 retry_seconds=15):
        super().__init__(daemon=True, name="tunnel-supervisor")
        self.port = port
        self.ha = ha
        self.token = token
        self.on_url = on_url
        self.retry_seconds = retry_seconds
        self.url = None
        self.started_at = None
        self.restarts = 0
        self.last_error = None
        self._tunnel = None
        self._stop = threading.Event()

    def _announce(self, url):
        if url == self.url:
            return
        self.url = url
        self.started_at = time.time()
        publish(self.ha, url, self.token)
        if self.on_url:
            try:
                self.on_url(url)
            except Exception:
                pass

    def run(self):
        while not self._stop.is_set():
            self._tunnel = QuickTunnel(self.port, on_url=self._announce)
            url, err = self._tunnel.start()
            if err:
                self.last_error = err
                self.url = None
                if self._stop.wait(self.retry_seconds):
                    return
                self.restarts += 1
                continue
            self.last_error = None
            # Sit with it until cloudflared gives up, then build a new one.
            while not self._stop.is_set():
                proc = self._tunnel.proc
                if proc is None or proc.poll() is not None:
                    break
                self._stop.wait(2)
            if self._stop.is_set():
                return
            self.restarts += 1
            self.url = None

    def stop(self):
        self._stop.set()
        if self._tunnel:
            self._tunnel.stop()

    def status(self):
        return {
            "url": self.url,
            "link": f"{self.url}/?key={self.token}" if self.url and self.token else self.url,
            "restarts": self.restarts,
            "up_seconds": int(time.time() - self.started_at) if self.started_at else 0,
            "error": self.last_error,
        }


def publish(ha, url, token=""):
    """Put the current address into Home Assistant so a phone can find it."""
    if not (ha and ha.enabled and url):
        return False
    full = f"{url}/?key={token}" if token else url
    return ha.set_state("sensor.dj_remote_url", url[:250], {
        "friendly_name": "DJ remote address",
        "icon": "mdi:cloud-lock",
        "url": url,
        # The full link, token included, so it is one tap from the dashboard.
        "link": full,
    })

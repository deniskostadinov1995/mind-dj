"""Talking to an LG webOS TV directly, without Home Assistant in the way.

Home Assistant's webostv integration could not hold a connection to this TV,
so this speaks the TV's own protocol: a plain websocket on port 3000 carrying
"ssap://" requests. Pairing is a one-time prompt on the TV screen; the key it
returns is stored and reused forever after.

Only simple_websocket is needed, which is already present as a flask-socketio
dependency, so this adds nothing to install.
"""
import json
import ssl
import threading
import time

from .config import DATA

KEY_PATH = DATA / "webos_key.json"

# The manifest every third-party LG remote presents. The TV checks its shape,
# not its provenance, and refuses to pair without one.
MANIFEST = {
    "manifestVersion": 1,
    "appVersion": "1.1",
    "signed": {
        "created": "20140509",
        "appId": "com.lge.test",
        "vendorId": "com.lge",
        "localizedAppNames": {"": "mind-dj"},
        "localizedVendorNames": {"": "LG Electronics"},
        "permissions": ["TEST_SECURE", "CONTROL_INPUT_TEXT",
                        "CONTROL_MOUSE_AND_KEYBOARD", "READ_INSTALLED_APPS",
                        "READ_LGE_SDX", "READ_NOTIFICATIONS", "SEARCH",
                        "WRITE_SETTINGS", "WRITE_NOTIFICATION_ALERT",
                        "CONTROL_POWER", "READ_CURRENT_CHANNEL",
                        "READ_RUNNING_APPS", "READ_UPDATE_INFO",
                        "UPDATE_FROM_REMOTE_APP", "READ_LGE_TV_INPUT_EVENTS",
                        "READ_TV_CURRENT_TIME"],
        "serial": "2f930e2d2cfe083771f68e4fe7bb07",
    },
    "permissions": [
        "LAUNCH", "LAUNCH_WEBAPP", "APP_TO_APP", "CLOSE", "TEST_OPEN",
        "TEST_PROTECTED", "CONTROL_AUDIO", "CONTROL_DISPLAY",
        "CONTROL_INPUT_JOYSTICK", "CONTROL_INPUT_MEDIA_RECORDING",
        "CONTROL_INPUT_MEDIA_PLAYBACK", "CONTROL_INPUT_TV", "CONTROL_POWER",
        "READ_APP_STATUS", "READ_CURRENT_CHANNEL", "READ_INPUT_DEVICE_LIST",
        "READ_NETWORK_STATE", "READ_RUNNING_APPS", "READ_TV_CHANNEL_LIST",
        "WRITE_NOTIFICATION_TOAST", "READ_POWER_STATE", "READ_COUNTRY_INFO",
    ],
    "signatures": [{
        "signatureVersion": 1,
        "signature": (
            "eyJhbGdvcml0aG0iOiJSU0EtU0hBMjU2Iiwia2V5SWQiOiJ0ZXN0LXNpZ25pbmctY2Vy"
            "dCIsInNpZ25hdHVyZVZlcnNpb24iOjF9.hrVRgjCwXVvE2OOSpDZ58hR+59aFNwYDyjQ"
            "gKk3auukd7pcegmE2CzPCa0bJ0ZsRAcKkCTJrWo5iDzNhMBWRyaMOv5zWSrthlf7G128"
            "qvIlpMT0YNY+n/FaOHE73uLrS/g7swl3/qH/BGFG2Hu4RlL48eb3lLKqTt2xKHdCs6Cd"
            "4RMfJPYnzgvI4BNrFUKsjkcu+WD4OO2A27Pq1n50cMchmcaXadJhGrOqH5YmHdOCj5NS"
            "HzJYrsW0HPlpuAx/ECMeIZYDh6RMqaFM2DXzdKX9NmmyqzJ3o/0lkk/N97gfVRLW5hA2"
            "9yeAwaCViZNCP8iC9aO0q9fQojoa7NQnAtw=="
        ),
    }],
}

YOUTUBE_APP = "youtube.leanback.v4"


def load_keys():
    if KEY_PATH.exists():
        try:
            return json.loads(KEY_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_key(host, key):
    keys = load_keys()
    keys[host] = key
    KEY_PATH.write_text(json.dumps(keys, indent=2), encoding="utf-8")


class WebOSError(Exception):
    pass


class WebOSClient:
    """A single, short-lived connection to the TV.

    Deliberately not long-lived: the TV drops idle sockets and reconnecting
    costs about 200ms, which is nothing next to a track change. Every public
    method opens, does its work, and closes.
    """

    def __init__(self, host, timeout=8):
        self.host = host
        self.timeout = timeout
        self.ws = None
        self._id = 0
        self._lock = threading.Lock()
        self.last_error = None

    # ------------------------------------------------------------ transport

    def _open(self):
        import simple_websocket
        first_error = None
        # The trailing slash matters: wsproto rejects an empty request target
        # with "Illegal target characters".
        for url, kwargs in (
            (f"ws://{self.host}:3000/", {}),
            (f"wss://{self.host}:3001/", {"ssl_context": _insecure_ssl()}),
        ):
            try:
                self.ws = simple_websocket.Client(url, **kwargs)
                return True
            except Exception as e:
                # Keep the first (plain ws) failure: the wss fallback fails
                # with an unrelated SSL quirk that only obscures the cause.
                if not first_error:
                    first_error = f"{url}: {e}"
        self.last_error = first_error or "no route to the TV"
        return False

    def _close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.ws = None

    def _send(self, obj):
        self.ws.send(json.dumps(obj))

    def _recv(self, want_id=None, timeout=None, accept_types=None):
        """Read until a message matching want_id arrives, or we time out."""
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            try:
                raw = self.ws.receive(timeout=remaining)
            except Exception as e:
                raise WebOSError(f"receive failed: {e}")
            if raw is None:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if want_id and msg.get("id") != want_id:
                continue
            if accept_types and msg.get("type") not in accept_types:
                continue
            return msg
        return None

    # -------------------------------------------------------------- pairing

    def register(self, prompt_timeout=60):
        """Pair with the TV. Shows a prompt on screen the first time only.

        Returns (client_key, error). An existing key re-registers silently.
        """
        if not self._open():
            return None, f"cannot reach {self.host}: {self.last_error}"
        try:
            key = load_keys().get(self.host)
            payload = {"forcePairing": False, "pairingType": "PROMPT",
                       "manifest": MANIFEST}
            if key:
                payload["client-key"] = key
            self._send({"type": "register", "id": "register_0", "payload": payload})

            # First reply acknowledges; "registered" arrives once accepted.
            msg = self._recv(want_id="register_0", timeout=6)
            if msg is None:
                return None, "TV did not answer the registration request"
            if msg.get("type") == "error":
                return None, f"TV refused: {msg.get('error')}"
            if msg.get("type") == "registered":
                got = (msg.get("payload") or {}).get("client-key")
                if got:
                    save_key(self.host, got)
                return got or key, None

            # Waiting on the human to press Yes on the TV.
            msg = self._recv(want_id="register_0", timeout=prompt_timeout,
                             accept_types={"registered", "error"})
            if msg is None:
                return None, "timed out waiting for the prompt to be accepted"
            if msg.get("type") == "error":
                return None, f"pairing rejected: {msg.get('error')}"
            got = (msg.get("payload") or {}).get("client-key")
            if not got:
                return None, "TV registered but returned no client key"
            save_key(self.host, got)
            return got, None
        finally:
            self._close()

    # ------------------------------------------------------------- requests

    def request(self, uri, payload=None, timeout=None):
        """Send one ssap:// request on a fresh, authenticated connection."""
        with self._lock:
            key = load_keys().get(self.host)
            if not key:
                return None, "not paired - run: python -m dj.cli lg pair"
            if not self._open():
                return None, f"cannot reach {self.host}: {self.last_error}"
            try:
                self._send({"type": "register", "id": "register_0",
                            "payload": {"forcePairing": False,
                                        "pairingType": "PROMPT",
                                        "client-key": key,
                                        "manifest": MANIFEST}})
                msg = self._recv(want_id="register_0", timeout=8,
                                 accept_types={"registered", "error"})
                if msg is None or msg.get("type") != "registered":
                    return None, "TV would not accept the stored key (re-pair)"

                self._id += 1
                req_id = f"req_{self._id}"
                self._send({"type": "request", "id": req_id, "uri": uri,
                            "payload": payload or {}})
                reply = self._recv(want_id=req_id, timeout=timeout or self.timeout)
                if reply is None:
                    return None, f"no reply to {uri}"
                if reply.get("type") == "error":
                    return None, reply.get("error") or "error"
                return reply.get("payload") or {}, None
            except WebOSError as e:
                return None, str(e)
            finally:
                self._close()

    # ------------------------------------------------------------- commands

    def launch_youtube(self, video_id):
        return self.request("ssap://system.launcher/launch",
                            {"id": YOUTUBE_APP, "contentId": video_id})

    def foreground_app(self):
        return self.request("ssap://com.webos.applicationManager/getForegroundAppInfo")

    def volume(self):
        return self.request("ssap://audio/getVolume")

    def set_volume(self, level):
        return self.request("ssap://audio/setVolume", {"volume": int(level)})

    def media_play(self):
        return self.request("ssap://media.controls/play")

    def media_pause(self):
        return self.request("ssap://media.controls/pause")

    def media_stop(self):
        return self.request("ssap://media.controls/stop")

    def toast(self, message):
        return self.request("ssap://system.notifications/createToast",
                            {"message": message})

    def power_off(self):
        return self.request("ssap://system/turnOff")

    def sw_info(self):
        return self.request("ssap://com.webos.service.update/getCurrentSWInformation")


def _insecure_ssl():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def discover(timeout=1.0, subnet=None):
    """Find TVs by looking for an open webOS control port on the LAN."""
    import socket
    from concurrent.futures import ThreadPoolExecutor

    if not subnet:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            subnet = ".".join(s.getsockname()[0].split(".")[:3])
        finally:
            s.close()

    def probe(n):
        ip = f"{subnet}.{n}"
        for port in (3000, 3001):
            sock = socket.socket()
            sock.settimeout(timeout)
            try:
                sock.connect((ip, port))
                return ip
            except Exception:
                continue
            finally:
                sock.close()
        return None

    with ThreadPoolExecutor(max_workers=64) as pool:
        found = [ip for ip in pool.map(probe, range(1, 255)) if ip]
    return found

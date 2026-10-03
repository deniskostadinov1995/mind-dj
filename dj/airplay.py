"""AirPlay, via pyatv.

Why this exists: the LG plays through its own YouTube app, which means video
and YouTube's ads. The Sony gets clean audio because Google Cast will fetch an
arbitrary URL. AirPlay is the only route that could give the LG the same clean
audio, since the TV is a genuine AirPlay 2 receiver (it identifies itself as
AirTunes/377.25.06, AirPlay 2.3.1).

The catch is that everything past /info on port 7000 answers 403 until the
device is paired, and AirPlay 2 pairing is a real cryptographic handshake --
not something to reimplement by hand. pyatv already does it, so it does it.

Pairing needs a PIN that appears on the TV screen, which is awkward to collect
from a non-interactive process. So `begin_pairing` writes its state to disk and
waits for the PIN to show up in a file; whoever has eyes on the TV drops it
there. Once paired, the credentials live in data/airplay.json and no prompt
ever appears again.
"""
import asyncio
import json
import time
from pathlib import Path

from .config import DATA

CREDS_PATH = DATA / "airplay.json"
PIN_PATH = DATA / "airplay_pin.txt"


def load_creds():
    if CREDS_PATH.exists():
        try:
            return json.loads(CREDS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_creds(identifier, protocol, credentials):
    creds = load_creds()
    creds.setdefault(identifier, {})[protocol] = credentials
    CREDS_PATH.write_text(json.dumps(creds, indent=2), encoding="utf-8")


_CONF_CACHE = {}
_CONF_TTL = 300


async def scan(host=None, timeout=5):
    """Find AirPlay receivers. Returns pyatv config objects."""
    import ipaddress
    import pyatv
    loop = asyncio.get_running_loop()
    kwargs = {"timeout": timeout}
    if host:
        kwargs["hosts"] = [ipaddress.ip_address(host)]
    return await pyatv.scan(loop, **kwargs)


async def reachable(host, timeout=1.5):
    """Is the receiver actually awake? A TV in standby drops off entirely."""
    try:
        fut = asyncio.open_connection(host, 7000)
        _r, w = await asyncio.wait_for(fut, timeout=timeout)
        w.close()
        return True
    except Exception:
        return False


async def cached_conf(host, timeout=5):
    """A scanned config, remembered for a while.

    Scanning takes seconds and happens on every connect, which is far too slow
    to sit between tracks. The result only changes when the TV moves address.
    """
    hit = _CONF_CACHE.get(host)
    if hit and hit[1] > time.time():
        return hit[0]
    found = await scan(host=host, timeout=timeout)
    conf = found[0] if found else None
    if conf is not None:
        _CONF_CACHE[host] = (conf, time.time() + _CONF_TTL)
    return conf


async def describe(host):
    """What a receiver offers, and whether we already hold credentials."""
    found = await scan(host=host)
    if not found:
        return None
    conf = found[0]
    creds = load_creds().get(conf.identifier, {})
    return {
        "name": conf.name,
        "address": str(conf.address),
        "identifier": conf.identifier,
        "services": [
            {"protocol": s.protocol.name, "port": s.port,
             "pairing": s.pairing.name if s.pairing else "?",
             "have_credentials": s.protocol.name in creds}
            for s in conf.services
        ],
    }


async def pair(host, protocol_name="AirPlay", pin_timeout=300):
    """Pair with a receiver, collecting the PIN from PIN_PATH.

    Returns (credentials, error).
    """
    import pyatv
    from pyatv.const import Protocol

    loop = asyncio.get_running_loop()
    found = await scan(host=host)
    if not found:
        return None, f"no AirPlay receiver at {host}"
    conf = found[0]
    protocol = getattr(Protocol, protocol_name)

    if not any(s.protocol is protocol for s in conf.services):
        have = ", ".join(s.protocol.name for s in conf.services)
        return None, f"{conf.name} does not offer {protocol_name} (has: {have})"

    PIN_PATH.write_text("", encoding="utf-8")
    pairing = await pyatv.pair(conf, protocol, loop)
    try:
        await pairing.begin()
        if pairing.device_provides_pin:
            deadline = time.time() + pin_timeout
            pin = ""
            while time.time() < deadline:
                try:
                    pin = PIN_PATH.read_text(encoding="utf-8").strip()
                except Exception:
                    pin = ""
                if pin.isdigit() and len(pin) >= 4:
                    break
                await asyncio.sleep(1)
            if not (pin.isdigit() and len(pin) >= 4):
                return None, ("timed out waiting for the PIN. Write the four "
                              f"digits shown on the TV into {PIN_PATH}")
            pairing.pin(int(pin))
        else:
            pairing.pin(None)

        await pairing.finish()
        if not pairing.has_paired:
            return None, "the receiver rejected the pairing"
        creds = pairing.service.credentials
        save_creds(conf.identifier, protocol_name, creds)
        return creds, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    finally:
        try:
            await pairing.close()
        except Exception:
            pass
        try:
            PIN_PATH.unlink()
        except Exception:
            pass


async def connect(host):
    """Connect using stored credentials. Returns (atv, error)."""
    import pyatv
    from pyatv.const import Protocol

    loop = asyncio.get_running_loop()
    # Check the socket first: scanning an absent device burns the full timeout
    # and returns the same "not found" we can establish in milliseconds.
    if not await reachable(host):
        return None, f"{host} is not responding (TV asleep or off?)"
    conf = await cached_conf(host)
    if conf is None:
        _CONF_CACHE.pop(host, None)
        return None, f"no AirPlay receiver at {host}"
    creds = load_creds().get(conf.identifier, {})
    if not creds:
        return None, "not paired - run: python -m dj.cli airplay pair"
    for name, credential in creds.items():
        try:
            conf.set_credentials(getattr(Protocol, name), credential)
        except Exception:
            pass
    try:
        atv = await pyatv.connect(conf, loop)
        return atv, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


async def capabilities(host):
    """What can we actually do once connected? Audio streaming or not."""
    atv, err = await connect(host)
    if err:
        return None, err
    try:
        feats = atv.features
        from pyatv.const import FeatureName, FeatureState
        interesting = ["StreamFile", "PlayUrl", "Volume", "SetVolume",
                       "Play", "Pause", "Stop", "Title", "Position"]
        out = {}
        for name in interesting:
            try:
                fn = getattr(FeatureName, name)
                out[name] = feats.get_feature(fn).state.name
            except Exception:
                out[name] = "Unknown"
        return out, None
    finally:
        atv.close()


async def play_url(host, url):
    """Hand a URL to the receiver to play. Needs the PlayUrl feature."""
    atv, err = await connect(host)
    if err:
        return False, err
    try:
        await atv.stream.play_url(url)
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        atv.close()

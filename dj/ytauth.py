"""Signing in to your own YouTube Music account.

Without auth we can search the public catalogue. With it we can read what you
have actually been listening to: your history, your likes, your library and
your playlists. That is the difference between guessing your taste and being
told it.

Two ways in:

  browser headers  copy the request headers from music.youtube.com once.
                   Nothing to register, works immediately, and the credential
                   lasts as long as the browser session does (months).

  oauth            needs a Google Cloud OAuth client of type "TV and Limited
                   Input". More setup, but survives browser logouts.

The browser method is the one worth using here, so it is what `dj.cli auth`
walks through.
"""
from pathlib import Path

import re

from .config import DATA

# A legal HTTP header name; anything else in the pasted block is noise.
_HEADER_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+")

AUTH_PATH = DATA / "ytmusic_auth.json"
HEADERS_PATH = DATA / "ytmusic_headers.txt"

INSTRUCTIONS = f"""
  Signing in to YouTube Music
  ---------------------------
  1. Open https://music.youtube.com in Chrome, signed in to the account you
     actually listen with.
  2. Press F12 to open DevTools, then click the Network tab.
  3. Click anything in the page (a song, a playlist) so requests appear.
  4. In the filter box type:   /youtubei/v1/
  5. Click any request in the list (for example "next" or "browse").
  6. Find the "Request Headers" section. Right-click it and choose
     Copy -> Copy request headers.
        (Firefox: right-click the request -> Copy -> Copy Request Headers)
  7. Paste the whole thing into this file, save it, and run this command again:

        {HEADERS_PATH}

  The headers include a cookie for your account, so treat that file the way you
  would treat a password. It is git-ignored, and you can delete it once this
  has run - only {AUTH_PATH.name} is needed afterwards.
"""


def is_authenticated():
    return AUTH_PATH.exists()


def auth_file():
    return str(AUTH_PATH) if AUTH_PATH.exists() else None


def normalise_headers(raw):
    """Turn whatever DevTools copied into "name: value" lines.

    Chrome's older "Copy request headers" produced "name: value" pairs, which
    ytmusicapi parses. Current Chrome copies the name and the value on
    *separate lines*, and feeding that in produces a credential file full of
    nonsense -- header names paired with the wrong values, entries like
    "Decoded": "}" -- which then fails every single API call while looking
    perfectly well-formed on disk. So handle both shapes here.
    """
    lines = [ln.rstrip("\r") for ln in (raw or "").splitlines()]
    lines = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        return ""

    # If a decent share of lines already look like "name: value", trust that.
    paired = sum(1 for ln in lines if ":" in ln.lstrip(":"))
    if paired >= max(3, len(lines) * 0.6):
        return "\n".join(lines)

    # Otherwise assume alternating name / value lines.
    out, i = [], 0
    while i < len(lines) - 1:
        name, value = lines[i].strip(), lines[i + 1].strip()
        i += 2
        if not name or name.startswith(":"):
            continue  # HTTP/2 pseudo-headers are not real headers
        if not _HEADER_NAME.fullmatch(name):
            # DevTools also pastes explanatory comments and decoded payload
            # blobs; anything that is not a legal header name is noise.
            continue
        out.append(f"{name}: {value}")
    return "\n".join(out)


def complete_headers(text):
    """Add the headers ytmusicapi insists on but the copied request may lack.

    Authentication actually rides on the SAPISID cookie; x-goog-authuser just
    says which signed-in account to act as, and x-origin is a fixed constant.
    Requiring the user to hunt for a request that happens to carry both is a
    pointless obstacle when we can supply them.
    """
    present = {ln.split(":", 1)[0].strip().lower()
               for ln in text.splitlines() if ":" in ln}
    extra = []
    if "x-goog-authuser" not in present:
        extra.append("x-goog-authuser: 0")
    if "x-origin" not in present:
        extra.append("x-origin: https://music.youtube.com")
    return "\n".join([text] + extra) if extra else text


def setup_from_headers(headers_raw=None):
    """Turn copied browser headers into a reusable credential file."""
    from ytmusicapi import setup

    if headers_raw is None:
        if not HEADERS_PATH.exists():
            HEADERS_PATH.write_text(
                "# Paste your copied request headers below this line, save,\n"
                "# then run:  python -m dj.cli auth\n\n", encoding="utf-8")
            return None, f"created {HEADERS_PATH} - paste your headers into it"
        headers_raw = HEADERS_PATH.read_text(encoding="utf-8")

    body = complete_headers(normalise_headers(headers_raw))
    if not body:
        return None, f"{HEADERS_PATH} has no headers in it yet"
    if "cookie" not in body.lower():
        return None, ("those headers have no Cookie line - make sure you used "
                      "Copy request headers, not Copy response headers")

    try:
        setup(filepath=str(AUTH_PATH), headers_raw=body)
    except Exception as e:
        return None, f"ytmusicapi rejected those headers: {e}"
    return str(AUTH_PATH), None


COOKIE_PATH = DATA / "yt_cookies.txt"


def cookie_header():
    """The Cookie line from the stored credential, if there is one."""
    if not AUTH_PATH.exists():
        return ""
    try:
        import json
        data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    except Exception:
        return ""
    for key, value in (data or {}).items():
        if key.lower() == "cookie" and isinstance(value, str):
            return value
    return ""


def write_cookiefile():
    """Export the session cookies for yt-dlp, in Netscape format.

    Unauthenticated yt-dlp gets rate limited hard, and past some volume
    YouTube starts answering "Sign in to confirm you're not a bot" -- which
    stops playback dead, not just background work. Handing it the same session
    the rest of the app already uses avoids that entirely.

    Returns the path, or None when we have no cookies to give.
    """
    raw = cookie_header()
    if not raw:
        return None
    lines = ["# Netscape HTTP Cookie File",
             "# Written by mind-dj from your YouTube Music credential.", ""]
    # Far enough out that yt-dlp does not discard them as expired.
    expiry = 2147483647
    for part in raw.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if not name:
            continue
        secure = "TRUE" if name.startswith("__Secure") or name.startswith("__Host") else "FALSE"
        lines.append("\t".join([".youtube.com", "TRUE", "/", secure,
                                str(expiry), name, value]))
    COOKIE_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(COOKIE_PATH)


def cookiefile():
    """Path to a usable cookie file, writing it if needed."""
    if COOKIE_PATH.exists():
        return str(COOKIE_PATH)
    return write_cookiefile()


def verify():
    """Confirm the credential works and say whose account it is."""
    if not AUTH_PATH.exists():
        return None, "not signed in"
    try:
        from ytmusicapi import YTMusic
        yt = YTMusic(str(AUTH_PATH))
        info = yt.get_account_info()
        return info, None
    except Exception as e:
        return None, str(e)

"""Understanding what you typed.

"chalga 2000" means chalga from 2000. "chalga 2000-2010" means the decade
between them. That reading has to happen before anything is searched, because
the era is the most useful thing in the sentence and the least likely to
survive being handed to a general search.

An important discovery shapes all of this: **stored year metadata cannot be
trusted for era**. The album literally called "Azis 2014" reports year 2023,
and yt-dlp agrees, because both are reporting when it was uploaded to YouTube
rather than when the music is from. Filtering on that field would exclude
precisely the tracks you asked for.

What does work is putting the era into the search itself -- "chalga 2005"
returns mid-2000s Azis while "chalga 2020" returns current tracks -- and
reading years out of titles and album names, where "Планета Пайнер 2005" and
"Azis 2014" say plainly what decade they belong to.
"""
import re

#: Years outside this are typos or track numbers, not eras.
MIN_YEAR, MAX_YEAR = 1950, 2035

_RANGE = re.compile(r"\b(19\d{2}|20\d{2})\s*(?:-|–|—|to|до|until)\s*(19\d{2}|20\d{2})\b")
# Two bare years next to each other read as a span too: "pop 2015 2020".
_RANGE_BARE = re.compile(r"\b(19\d{2}|20\d{2})\s+(19\d{2}|20\d{2})\b")
_DECADE_LONG = re.compile(r"\b(19\d0|20\d0)\s*(?:s|'s|s\.|те|-те| те)\b", re.I)
_DECADE_SHORT = re.compile(r"\b'?(\d0)\s*(?:s|'s|те|-те)\b", re.I)
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")

# Words that carry no search value once the era is extracted.
_FILLER = re.compile(
    r"\b(from|the|music|songs?|tracks?|play|some|of|era|period|between|"
    r"год(?:ина)?|песни|музика|от|до|през)\b", re.I)


def parse(text):
    """Pull an era and clean search terms out of free text.

    Returns {"terms": str, "era": (lo, hi) or None, "raw": str}.
    """
    raw = (text or "").strip()
    if not raw:
        return {"terms": "", "era": None, "raw": raw}

    era = None
    working = raw

    m = _RANGE.search(working) or _RANGE_BARE.search(working)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        era = (lo, hi)
        working = working[:m.start()] + " " + working[m.end():]
    else:
        m = _DECADE_LONG.search(working)
        if m:
            base = int(m.group(1))
            era = (base, base + 9)
            working = working[:m.start()] + " " + working[m.end():]
        else:
            m = _DECADE_SHORT.search(working)
            if m:
                two = int(m.group(1))
                # "90s" is the 1990s; "00s" and "10s" are 2000s and 2010s.
                base = 1900 + two if two >= 40 else 2000 + two
                era = (base, base + 9)
                working = working[:m.start()] + " " + working[m.end():]
            else:
                m = _YEAR.search(working)
                if m:
                    y = int(m.group(1))
                    era = (y, y)
                    working = working[:m.start()] + " " + working[m.end():]

    if era:
        lo = max(MIN_YEAR, min(MAX_YEAR, era[0]))
        hi = max(MIN_YEAR, min(MAX_YEAR, era[1]))
        era = (lo, hi)

    terms = _FILLER.sub(" ", working)
    terms = re.sub(r"\s+", " ", terms).strip(" -–—,")
    return {"terms": terms, "era": era, "raw": raw}


def expand(terms, era, limit=5):
    """Search queries that actually reach the era you asked for.

    A single "chalga 2000-2010" query returns whatever one page YouTube feels
    like; sampling across the span pulls from the whole period instead, which
    is what a decade request means.
    """
    terms = (terms or "").strip()
    if not era:
        return [terms] if terms else []

    lo, hi = era
    if lo == hi:
        base = [f"{terms} {lo}".strip(), f"{terms} хитове {lo}".strip()]
        return [q for q in base if q][:limit]

    span = hi - lo
    if span <= 3:
        years = list(range(lo, hi + 1))
    else:
        # Endpoints plus evenly spaced points inside, so a decade is sampled
        # rather than represented by whichever year search happens to favour.
        step = max(1, span // (limit - 2))
        years = sorted({lo, hi, *range(lo + step, hi, step)})[:limit - 1]

    out = [f"{terms} {y}".strip() for y in years]
    decade = (lo // 10) * 10
    if decade >= 1950:
        out.append(f"{terms} {decade}s".strip())
    return [q for q in out if q][:limit]


_TITLE_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")


def era_evidence(track, era):
    """How well a track matches an era, -1..+1, from what we can actually see.

    Years found in the title or album name are trusted, because that is where
    "Планета Пайнер 2005" and "Azis 2014" put them. The stored `year` field is
    only worth a nudge: it usually records the upload, not the release.
    Absence of evidence scores zero -- most tracks say nothing either way, and
    excluding them would empty the queue.
    """
    if not era:
        return 0.0
    lo, hi = era
    text = f"{track.get('title') or ''} {track.get('album') or ''}"
    found = [int(y) for y in _TITLE_YEAR.findall(text)
             if MIN_YEAR <= int(y) <= MAX_YEAR]
    if found:
        if any(lo <= y <= hi for y in found):
            return 1.0
        # A year is stated and it is not the one asked for. How wrong it is
        # matters: 2006 for a 2000-2005 request is a near miss, 2024 is not.
        nearest = min(abs(y - lo) if y < lo else abs(y - hi) for y in found)
        return max(-1.0, -0.25 - nearest / 12.0)

    year = track.get("year")
    try:
        year = int(year)
    except (TypeError, ValueError):
        return 0.0
    if MIN_YEAR <= year <= MAX_YEAR and lo <= year <= hi:
        return 0.35
    return 0.0


def describe(parsed):
    """A short phrase for the UI, so you can see how it read you."""
    era = parsed.get("era")
    terms = parsed.get("terms") or "anything"
    if not era:
        return terms
    lo, hi = era
    return f"{terms}, {lo}" if lo == hi else f"{terms}, {lo}-{hi}"

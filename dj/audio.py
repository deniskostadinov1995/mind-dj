"""What a track actually sounds like.

The embeddings elsewhere are built from text -- artist, title, album, tags --
so they know that Galena sits near Emanuela but cannot tell a slow ballad from
a banger. For a DJ that is the wrong axis to be missing: "what do I want at
eleven at night" is mostly a question about energy, not genre.

So each track gets measured. ffmpeg decodes a chunk from the middle to mono
PCM and numpy does the rest -- no librosa, no numba, nothing new to install.

Four numbers steer selection, all 0..1:

  energy      how activating it feels: busyness, then loudness, then beat rate
  brightness  spectral centroid; treble-heavy vs dark and warm
  dynamics    how much the loudness moves; steady club track vs live band
  punch       onset strength; how hard the hits land

Tempo is measured and stored, but excluded from scoring -- see KEYS below.
"""
import subprocess
from shutil import which

import numpy as np

SAMPLE_RATE = 22050
#: Analyse a window from the middle. Intros and outros lie about a song.
WINDOW_SECONDS = 60
SKIP_SECONDS = 30


def available():
    return which("ffmpeg") is not None


def decode(url, seconds=WINDOW_SECONDS, skip=SKIP_SECONDS, headers=None):
    """Decode part of a track to mono float32 PCM. Returns None on failure."""
    ff = which("ffmpeg")
    if not ff or not url:
        return None
    cmd = [ff, "-hide_banner", "-loglevel", "error"]
    ua = (headers or {}).get("User-Agent")
    if ua:
        cmd += ["-user_agent", ua]
    cmd += ["-ss", str(skip), "-t", str(seconds), "-i", url,
            "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=90).stdout
    except Exception:
        return None
    if len(out) < SAMPLE_RATE * 2 * 5:      # under five seconds is not enough
        # Maybe the track is shorter than the skip; try again from the top.
        if skip:
            return decode(url, seconds, 0, headers)
        return None
    pcm = np.frombuffer(out, dtype=np.int16).astype(np.float32) / 32768.0
    return pcm


def _frames(pcm, size=1024, hop=512):
    n = 1 + (len(pcm) - size) // hop
    if n <= 0:
        return np.zeros((0, size), dtype=np.float32)
    idx = np.arange(size)[None, :] + hop * np.arange(n)[:, None]
    return pcm[idx]


def analyse(pcm):
    """Turn PCM into the five numbers. Returns a dict, or None if unusable."""
    if pcm is None or len(pcm) < SAMPLE_RATE:
        return None

    frames = _frames(pcm)
    if not len(frames):
        return None
    window = np.hanning(frames.shape[1]).astype(np.float32)
    spec = np.abs(np.fft.rfft(frames * window, axis=1))
    freqs = np.fft.rfftfreq(frames.shape[1], 1.0 / SAMPLE_RATE)

    # Every scale below is calibrated against real measurements rather than
    # guessed, because the obvious mappings compress everything into the top
    # of the range. Reference points, measured:
    #   ambient pad   -19dB   607Hz   flux   12
    #   slow rock     -11dB  1052Hz   flux  104
    #   jazz          -12dB  1740Hz   flux  102
    #   pop-folk       -8dB  3159Hz   flux  346

    # --- loudness. Modern masters are all squashed loud, so the usable range
    # is far narrower than dBFS suggests: about -22 to -7.
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
    db = 20.0 * np.log10(rms + 1e-12)
    loud = float(np.percentile(db, 90))
    loudness = float(np.clip((loud + 22.0) / 15.0, 0.0, 1.0))

    # --- dynamics: spread between the loud and quiet moments.
    quiet = float(np.percentile(db, 10))
    dynamics = float(np.clip((loud - quiet) / 30.0, 0.0, 1.0))

    # --- brightness: spectral centroid, log-scaled because hearing is.
    total = spec.sum(axis=1) + 1e-12
    centroid = float(np.mean((spec * freqs).sum(axis=1) / total))
    brightness = float(np.clip(np.log2(max(centroid, 250.0) / 500.0) / 3.0, 0.0, 1.0))

    # --- onsets: rising spectral flux is where the beats live.
    flux = np.diff(spec, axis=0)
    flux[flux < 0] = 0.0
    onset = flux.sum(axis=1)
    if len(onset) < 16:
        return None
    flux_mean = float(onset.mean())
    # This turns out to be the single best discriminator of felt energy, and
    # it spans more than an order of magnitude, hence the log.
    flux_n = float(np.clip((np.log10(flux_mean + 1.0) - 0.9) / 1.7, 0.0, 1.0))

    norm = (onset - onset.mean()) / (onset.std() or 1.0)
    tempo_bpm, tempo_conf = _tempo(norm)
    fps = SAMPLE_RATE / 512.0
    peaks = int(np.sum((norm[1:-1] > norm[:-2]) & (norm[1:-1] > norm[2:])
                       & (norm[1:-1] > 0.6)))
    density = float(np.clip(peaks / (len(norm) / fps) / 6.0, 0.0, 1.0))
    punch = float(np.clip(np.percentile(norm, 95) / 4.0, 0.0, 1.0))

    # Felt energy is busyness first, then loudness, then how often it hits.
    energy = float(np.clip(0.45 * flux_n + 0.35 * loudness + 0.20 * density,
                           0.0, 1.0))

    return {
        "energy": round(energy, 4),
        "tempo": round(tempo_bpm, 1),
        "tempo_confidence": round(tempo_conf, 3),
        "brightness": round(brightness, 4),
        "dynamics": round(dynamics, 4),
        "punch": round(punch, 4),
        "loudness": round(loudness, 4),
    }


def _tempo(onset, hop=512, lo=55, hi=200, centre=120.0, spread=1.0):
    """Beats per minute from the onset envelope.

    Plain autocorrelation picks the strongest periodicity, which is very often
    a half or double of the tempo people would actually clap -- it read a slow
    ballad as 136 and a chalga track as 60. Weighting each candidate by a
    log-normal prior around 120bpm resolves the octave the way a listener
    does, which is the standard fix (Ellis, 2007).
    """
    if len(onset) < 16:
        return 0.0, 0.0
    # A little smoothing; raw flux is spiky enough to confuse the correlation.
    kernel = np.hanning(5)
    kernel /= kernel.sum()
    env = np.convolve(onset - onset.mean(), kernel, mode="same")

    corr = np.correlate(env, env, mode="full")[len(env) - 1:]
    if len(corr) < 4:
        return 0.0, 0.0
    fps = SAMPLE_RATE / hop
    lag_lo = max(1, int(fps * 60.0 / hi))
    lag_hi = min(len(corr) - 1, int(fps * 60.0 / lo))
    if lag_hi <= lag_lo:
        return 0.0, 0.0

    lags = np.arange(lag_lo, lag_hi + 1)
    bpms = 60.0 * fps / lags
    prior = np.exp(-0.5 * (np.log2(bpms / centre) / spread) ** 2)
    score = corr[lag_lo:lag_hi + 1] * prior
    best = int(np.argmax(score))
    # Confidence: how much the winning period stands out from the rest. A
    # track with a firm beat gives a sharp peak; ambient pads and rubato
    # ballads give a flat curve and a number not worth believing.
    med = float(np.median(score)) or 1e-9
    confidence = float(np.clip((score[best] / med - 1.0) / 6.0, 0.0, 1.0))
    return float(bpms[best]), confidence


def measure(url, headers=None):
    """Decode and analyse in one step."""
    return analyse(decode(url, headers=headers))


#: What actually goes into the scoring vector.
#:
#: Tempo is deliberately absent. Measured against tracks whose tempo is not in
#: doubt it got them wrong often enough -- a slow ballad read as 136bpm, a fast
#: pop-folk track as 86 -- that including it would feed noise into every
#: comparison. It is still stored and shown, with a confidence alongside, and
#: it would be the right input for beat-matched transitions later; it is just
#: not trustworthy enough to steer selection.
KEYS = ("energy", "brightness", "dynamics", "punch")


def to_vector(feat):
    """The trustworthy features, as a comparable 0..1 vector."""
    if not feat:
        return None
    return np.array([float(feat.get(k, 0.0) or 0.0) for k in KEYS],
                    dtype=np.float32)


def describe(feat):
    """A short human phrase, for 'why this song'."""
    if not feat:
        return ""
    e = feat.get("energy", 0)
    level = ("calm" if e < 0.35 else "gentle" if e < 0.55
             else "lively" if e < 0.75 else "high-energy")
    bits = [level]
    # Tempo is deliberately not mentioned: neither it nor its confidence
    # proved trustworthy enough to say out loud.
    if (feat.get("brightness") or 0) < 0.25:
        bits.append("warm")
    elif (feat.get("brightness") or 0) > 0.75:
        bits.append("bright")
    return ", ".join(bits)

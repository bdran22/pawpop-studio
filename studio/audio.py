#!/usr/bin/env python3
"""PAWPOP audio module (rebuilt 2026-11-23).

Original-synth music bed + procedural SFX + VO ducking.
Zero external media: everything is synthesized with numpy, so there are no
copyright / Content-ID risks on YouTube.

Interface (used by ep003_assemble.py and ep004_assemble.py):
    aud.SR                          -> int sample rate
    aud.load_wav_mono(path)         -> float32 mono array (resampled to SR)
    aud.music_bed(chapters, total)  -> float32 array, len == int(total*SR)+SR
        chapters: [{t: start_sec, mode: warm|comedy|dramatic|bright|rain|
                    lullaby|outro, intensity: 0..1}, ...]
    aud.build_sfx(sfx_list, total)  -> float32 array, len == int(total*SR)+SR
        sfx_list: [{kind: squeak|whoosh|woof|boop|knock|purr|ding|chime|thud|
                    sparkle, at: sec}, ...]
    aud.rms_env(track)              -> envelope, len == len(track)
    aud.duck_gain(env)              -> 0..1 gain curve, len == len(env)
    aud.write_wav(path, stereo)     -> int16 PCM stereo wav
"""
import math
import wave

import numpy as np

SR = 48000
N = 2 ** 12  # RNG base for deterministic renders


# ----------------------------------------------------------------- wav IO
def load_wav_mono(src):
    """Read any PCM wav (8/16/24/32-bit int or 32-bit float), mono-mix,
    resample to SR, return float32 in [-1, 1]."""
    with wave.open(str(src), "rb") as w:
        ch, sw, fr, nf = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(nf)
    if sw == 2:
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        try:
            x = np.frombuffer(raw, dtype=np.float32).astype(np.float32)
            if np.abs(x).max() > 2.0:  # actually 32-bit int
                x = x / 2147483648.0
        except ValueError:
            x = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sw == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        x = ((b[:, 0].astype(np.int32)) | (b[:, 1].astype(np.int32) << 8)
             | (b[:, 2].astype(np.int32) << 16))
        x = np.where(x >= 1 << 23, x - (1 << 24), x).astype(np.float32) / (1 << 23)
    else:  # 8-bit unsigned
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if fr != SR and len(x):
        t_old = np.linspace(0.0, 1.0, len(x), endpoint=False)
        new_n = max(1, int(round(len(x) * SR / fr)))
        x = np.interp(np.linspace(0.0, 1.0, new_n, endpoint=False), t_old, x).astype(np.float32)
    return np.asarray(x, dtype=np.float32)


def write_wav(path, stereo):
    """stereo: (n, 2) float array in [-1, 1] -> 16-bit PCM wav at SR."""
    y = np.clip(np.asarray(stereo, dtype=np.float64), -1.0, 1.0)
    pcm = (y * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


# ------------------------------------------------------------- synth core
def _freq(semi):
    """Semitone offset from A4 -> frequency."""
    return 440.0 * 2.0 ** (semi / 12.0)


def _tone(f, n, amp=1.0, detune=0.004, brightness=0.35):
    """Soft additive tone with light detune + harmonics."""
    t = np.arange(n, dtype=np.float64) / SR
    y = (np.sin(2 * np.pi * f * t)
         + brightness * np.sin(2 * np.pi * f * 2 * t + 0.31)
         + brightness * 0.45 * np.sin(2 * np.pi * f * 3 * t + 0.77))
    if detune:
        y += 0.55 * np.sin(2 * np.pi * f * (1 + detune) * t + 1.1)
    return (amp * y / 2.2).astype(np.float32)


def _decay(n, tau):
    t = np.arange(n, dtype=np.float64) / SR
    return np.exp(-t / tau).astype(np.float32)


def _fade(n, fi, fo):
    w = np.ones(n, dtype=np.float32)
    a, b = min(fi, n), min(fo, n)
    if a > 0:
        w[:a] *= np.linspace(0, 1, a) ** 1.5
    if b > 0:
        w[-b:] *= np.linspace(1, 0, b) ** 1.5
    return w


def _lp_fast(x, alpha):
    """Vectorized one-pole lowpass via lfilter-equivalent (IIR scan)."""
    from scipy.signal import lfilter
    return lfilter([alpha], [1, -(1 - alpha)], x).astype(np.float32)


try:
    import scipy  # noqa: F401
    _lowpass = _lp_fast
except Exception:  # pragma: no cover
    def _lowpass(x, alpha):
        k = max(2, int(SR / (2 * math.pi * max(80.0, alpha * 8000))))
        kern = np.hanning(k)
        kern /= kern.sum()
        return np.convolve(x, kern, mode="same").astype(np.float32)


# --------------------------------------------------------------- music bed
# mode -> (bpm, pad chords as semitone triads around C, pluck scale, style)
MODES = {
    "warm":     dict(bpm=92,  root=-9, chords=[(0, 4, 7), (7, 11, 14), (-3, 0, 4), (-7, -3, 0)],
                     scale=(0, 2, 4, 7, 9), style="pad_pluck"),
    "comedy":   dict(bpm=126, root=-9, chords=[(0, 4, 7), (-7, -3, 0), (0, 4, 7), (7, 11, 14)],
                     scale=(0, 2, 4, 7, 9, 10), style="bounce"),
    "dramatic": dict(bpm=76,  root=-12, chords=[(-3, 0, 4), (-7, -3, 0), (-14, -10, -7), (-17, -13, -10)],
                     scale=(0, 2, 3, 5, 7, 8, 10), style="dark_pad"),
    "bright":   dict(bpm=108, root=-9, chords=[(0, 4, 7), (-3, 0, 4), (-7, -3, 0), (7, 11, 14)],
                     scale=(0, 2, 4, 7, 9, 12), style="sparkle"),
    "rain":     dict(bpm=70,  root=-12, chords=[(-3, 0, 4, 11), (-7, -3, 0, 7), (0, 4, 7, 14), (-5, -1, 2, 9)],
                     scale=(0, 2, 3, 5, 7, 10), style="rain_pad"),
    "lullaby":  dict(bpm=66,  root=-9, chords=[(0, 4, 7, 11), (-7, -3, 0, 4), (-3, 0, 4, 10), (7, 11, 14)],
                     scale=(0, 2, 4, 7, 9), style="music_box"),
    "outro":    dict(bpm=84,  root=-9, chords=[(0, 4, 7), (-7, -3, 0), (0, 4, 7), (7, 11, 14)],
                     scale=(0, 2, 4, 5, 7, 9, 12), style="music_box"),
}


def _seg_music(mode, intensity, seconds, seed):
    """Render one chapter segment of music (mono float32)."""
    m = MODES.get(mode, MODES["warm"])
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    out = np.zeros(n, dtype=np.float32)
    beat = 60.0 / m["bpm"]
    bar = 4.0 * beat
    chords = m["chords"]
    root = m["root"]  # semitone offset from A4 for C3-ish pad root
    n_bars = max(1, int(math.ceil(seconds / bar)) + 1)
    g = 0.55 + 0.55 * float(np.clip(intensity, 0.0, 1.0))  # chapter loudness

    for b in range(n_bars):
        t0 = b * bar
        if t0 >= seconds:
            break
        chord = chords[b % len(chords)]
        # --- pad layer -------------------------------------------------
        pn = int(min(bar * 1.02, seconds - t0) * SR)
        if pn > 256:
            pad = np.zeros(pn, dtype=np.float32)
            for semi in chord:
                pad += _tone(_freq(root + semi + 12), pn, amp=0.16,
                             detune=0.005, brightness=0.22)
            pad *= _fade(pn, int(0.6 * SR), int(0.5 * SR))
            i0 = int(t0 * SR)
            pad = pad[: max(0, len(out) - i0)]
            if len(pad):
                out[i0:i0 + len(pad)] += pad * g
        # --- pluck / melody layer --------------------------------------
        if m["style"] in ("pad_pluck", "bounce", "sparkle", "music_box"):
            steps = 8 if m["style"] != "music_box" else 4
            step = bar / steps
            for s in range(steps):
                ts = t0 + s * step
                if ts >= seconds:
                    break
                if m["style"] == "bounce" and s % 2 == 0 and rng.random() < 0.55:
                    continue  # staccato gaps keep it bouncy
                p = 0.72 if m["style"] == "sparkle" else 0.5
                if rng.random() > p:
                    continue
                semi = chord[int(rng.integers(len(chord)))] + m["root"] + 24
                semi += int(rng.choice(m["scale"])) if rng.random() < 0.6 else 0
                if m["style"] == "music_box":
                    semi += 12
                dur = step * (1.6 if m["style"] != "bounce" else 0.9)
                dnn = int(dur * SR)
                if dnn < 128:
                    continue
                pluck = _tone(_freq(semi), dnn, amp=0.11, detune=0.0,
                              brightness=0.5) * _decay(dnn, dur * 0.45)
                if m["style"] == "music_box":
                    pluck = pluck * 0.8 + 0.25 * np.roll(pluck, int(0.004 * SR))[:dnn]
                i0 = int(ts * SR)
                pluck = pluck[: max(0, len(out) - i0)]
                if len(pluck):
                    out[i0:i0 + len(pluck)] += pluck * g
        # --- bass layer -------------------------------------------------
        if m["style"] in ("pad_pluck", "bounce", "dark_pad", "bounce2"):
            bn = int(bar * SR)
            i0 = int(t0 * SR)
            bass = _tone(_freq(chord[0] + m["root"] - 12), min(bn, max(0, len(out) - i0)),
                         amp=0.09, detune=0.0, brightness=0.12)
            bass *= _fade(len(bass), int(0.05 * SR), int(0.2 * SR))
            if len(bass):
                out[i0:i0 + len(bass)] += bass * g
        # --- sparkle layer ----------------------------------------------
        if m["style"] == "sparkle" and rng.random() < 0.6:
            ts = t0 + rng.random() * bar
            if ts < seconds:
                semi = m["root"] + 36 + int(rng.choice(m["scale"]))
                dnn = int(0.8 * SR)
                bell = _tone(_freq(semi), dnn, amp=0.05, detune=0.001,
                             brightness=0.15) * _decay(dnn, 0.35)
                i0 = int(ts * SR)
                bell = bell[: max(0, len(out) - i0)]
                if len(bell):
                    out[i0:i0 + len(bell)] += bell * g

    # rain ambience
    if m["style"] == "rain_pad":
        rng2 = np.random.default_rng(seed + 77)
        noise = rng2.standard_normal(n).astype(np.float32) * 0.014
        noise = _lowpass(noise, 0.12) - _lowpass(noise, 0.04)
        out += noise * _fade(n, int(1.2 * SR), int(1.2 * SR))

    return out * _fade(n, int(0.8 * SR), int(0.8 * SR))


def music_bed(chapters, total):
    """Full-length music bed following chapter moods with crossfades."""
    n = int(total * SR) + SR
    bed = np.zeros(n, dtype=np.float32)
    chs = sorted(list(chapters or []), key=lambda c: float(c.get("t", 0.0)))
    if not chs:
        chs = [{"t": 0.0, "mode": "warm", "intensity": 0.6}]
    XF = 1.0
    for i, c in enumerate(chs):
        t0 = float(c.get("t", 0.0))
        t1 = float(chs[i + 1]["t"]) if i + 1 < len(chs) else total
        t1 = max(t0 + 4.0, min(t1, total))
        seg_n = int((t1 - t0 + XF) * SR)
        if seg_n < 512:
            continue
        seg = _seg_music(c.get("mode", "warm"), c.get("intensity", 0.6), (t1 - t0 + XF), N + i * 101)
        i0 = int(t0 * SR)
        seg = seg[: max(0, n - i0)]
        if len(seg):
            bed[i0:i0 + len(seg)] += seg  # overlap-add = natural crossfade
    peak = np.abs(bed).max()
    if peak > 1e-9:
        bed *= min(1.0, 0.85 / peak)
    return bed


# --------------------------------------------------------------------- SFX
def _sfx_one(kind, rng):
    """Return a single synthesized SFX as float32 array."""
    if kind == "squeak":
        n = int(0.34 * SR)
        t = np.arange(n) / SR
        u = np.clip(t / 0.17, 0.0, 1.0)
        f = 620 + 780 * np.sin(np.pi * u) ** 2
        ph = 2 * np.pi * np.cumsum(f) / SR
        y = np.sin(ph + 2.2 * np.sin(2 * np.pi * 28 * t))
        y = y * _fade(n, int(0.02 * SR), int(0.1 * SR))
        return (0.5 * y).astype(np.float32)

    if kind == "whoosh":
        n = int(1.1 * SR)
        x = rng.standard_normal(n).astype(np.float32)
        lo = _lowpass(x, 0.10)
        y = (lo - _lowpass(lo, 0.05)) * 1.6
        y *= np.hanning(n).astype(np.float32) ** 1.4
        return (0.42 * y).astype(np.float32)

    if kind == "woof":
        n = int(0.30 * SR)
        t = np.arange(n) / SR
        f = 150 * np.exp(-t * 5.5) + 85
        ph = 2 * np.pi * np.cumsum(f) / SR
        y = (np.sin(ph) + 0.4 * np.sin(2 * ph) + 0.2 * np.sin(3 * ph)) / 1.6
        bite = int(0.02 * SR)
        y[:bite] += 0.5 * rng.standard_normal(bite) * np.linspace(1, 0, bite)
        y *= _fade(n, int(0.01 * SR), int(0.12 * SR))
        return (0.6 * y).astype(np.float32)

    if kind == "boop":
        n = int(0.14 * SR)
        t = np.arange(n) / SR
        f = 430 - 160 * (t / 0.14)
        ph = 2 * np.pi * np.cumsum(f) / SR
        y = np.sin(ph) * _fade(n, int(0.012 * SR), int(0.06 * SR))
        return (0.42 * y).astype(np.float32)

    if kind == "knock":
        n = int(0.22 * SR)
        t = np.arange(n) / SR
        thump = np.sin(2 * np.pi * 115 * np.exp(-t * 9)) * _decay(n, 0.05)
        click = np.zeros(n, dtype=np.float32)
        cn = int(0.006 * SR)
        click[:cn] = rng.standard_normal(cn) * np.linspace(1, 0, cn)
        y = thump * 1.1 + _lowpass(click, 0.25) * 0.7
        y *= _fade(n, 0, int(0.1 * SR))
        return (0.62 * y).astype(np.float32)

    if kind == "purr":
        n = int(2.4 * SR)
        t = np.arange(n) / SR
        x = rng.standard_normal(n).astype(np.float32)
        x = _lowpass(x, 0.055)
        y = x * (0.55 + 0.45 * np.sin(2 * np.pi * 23 * t))
        y *= _fade(n, int(0.25 * SR), int(0.4 * SR))
        return (0.5 * y / (np.abs(y).max() + 1e-9)).astype(np.float32)

    if kind == "ding":
        n = int(1.3 * SR)
        t = np.arange(n) / SR
        y = (np.sin(2 * np.pi * 1245 * t) + 0.4 * np.sin(2 * np.pi * 1868 * t)
             + 0.15 * np.sin(2 * np.pi * 2490 * t))
        y *= _decay(n, 0.35)
        y *= _fade(n, 0, int(0.3 * SR))
        return (0.34 * y).astype(np.float32)

    if kind == "chime":
        total = int(1.6 * SR)
        y = np.zeros(total, dtype=np.float32)
        for j, semi in enumerate((9, 13, 16)):  # A6, C#7, E7-ish arpeggio
            nn = int(1.0 * SR)
            t = np.arange(nn) / SR
            note = (np.sin(2 * np.pi * _freq(semi + 27) * t)
                    + 0.3 * np.sin(2 * np.pi * _freq(semi + 27) * 2 * t)) * _decay(nn, 0.3)
            i0 = int(j * 0.11 * SR)
            note = note[: max(0, len(y) - i0)]
            if len(note):
                y[i0:i0 + len(note)] += note
        y *= _fade(total, 0, int(0.35 * SR))
        return (0.3 * y).astype(np.float32)

    if kind == "thud":
        n = int(0.4 * SR)
        t = np.arange(n) / SR
        f = 78 * np.exp(-t * 6.0) + 42
        ph = 2 * np.pi * np.cumsum(f) / SR
        y = np.sin(ph) * _decay(n, 0.12)
        y *= _fade(n, int(0.008 * SR), int(0.15 * SR))
        return (0.6 * y).astype(np.float32)

    if kind == "sparkle":
        total = int(1.5 * SR)
        y = np.zeros(total, dtype=np.float32)
        for j in range(6):
            semi = int(rng.choice((0, 2, 4, 7, 9, 12)))
            nn = int(0.55 * SR)
            t = np.arange(nn) / SR
            f = _freq(39 + semi)
            note = (np.sin(2 * np.pi * f * t) + 0.25 * np.sin(2 * np.pi * f * 2.01 * t)) \
                * _decay(nn, 0.16)
            i0 = int(j * 0.09 * SR)
            note = note[: max(0, len(y) - i0)]
            if len(note):
                y[i0:i0 + len(note)] += 0.16 * note
        return y.astype(np.float32)

    # fallback: soft boop
    return _sfx_one("boop", rng)


SFX_AMP = {
    "squeak": 0.5, "whoosh": 0.42, "woof": 0.5, "boop": 0.42, "knock": 0.55,
    "purr": 0.4, "ding": 0.36, "chime": 0.34, "thud": 0.5, "sparkle": 0.34,
}


def build_sfx(sfx_list, total):
    n = int(total * SR) + SR
    out = np.zeros(n, dtype=np.float32)
    rng = np.random.default_rng(N + 5)
    for e in (sfx_list or []):
        kind = str(e.get("kind", "boop"))
        at = float(e.get("at", 0.0))
        y = _sfx_one(kind, rng)
        i0 = int(at * SR)
        y = y[: max(0, n - i0)]
        if len(y) > 0:
            out[i0:i0 + len(y)] += y * SFX_AMP.get(kind, 0.4)
    return out


# ----------------------------------------------------------------- ducking
def rms_env(track):
    """20 ms block RMS envelope interpolated back to full length."""
    x = np.asarray(track, dtype=np.float32)
    hop = SR // 50
    nblk = len(x) // hop
    if nblk < 2:
        return np.zeros(len(x), dtype=np.float32)
    blocks = x[: nblk * hop].reshape(nblk, hop)
    env = np.sqrt((blocks.astype(np.float64) ** 2).mean(axis=1))
    env = np.interp(np.arange(len(x)), np.arange(nblk) * hop + hop / 2, env)
    return env.astype(np.float32)


def duck_gain(env, lookahead=0.18, smooth=0.45):
    """Music/sfx gain: 1.0 in silence, ~0.3 under VO speech."""
    e = np.asarray(env, dtype=np.float32)
    hi = np.percentile(e, 97)
    if hi < 1e-6:
        return np.ones_like(e)
    norm = np.clip(e / (hi * 1.15), 0.0, 1.0)
    target = 1.0 - 0.72 * (norm ** 0.7)
    target = np.clip(target, 0.28, 1.0)
    k = max(3, int(smooth * SR))
    kern = np.hanning(k)
    kern /= kern.sum()
    g = np.convolve(target, kern, mode="same")
    # lead the duck slightly before speech onset
    sh = int(lookahead * SR)
    if sh > 0:
        g = np.minimum(g, np.concatenate([g[sh:], np.full(sh, g[-1])]))
    return g.astype(np.float32)

"""Software sound mixer: decode, spatialize, and mix Quake voices. Pure stdlib.

Platform-agnostic -- this never touches the OS. It produces interleaved 16-bit
stereo samples on demand via Mixer.mix(nframes); a platform audio backend
(mac.py's CoreAudioBackend, and later windows/linux siblings) owns the OS output
stream and pulls from the mixer on its realtime callback, so the engine stays
portable.

The mix is a port of Quake's S_PaintChannels / SND_Spatialize. Sounds are decoded
+ resampled to the output rate ONCE at precache, so the realtime path just steps
one source sample per output sample and adds -- no resampling, no allocation per
sound.

    sound(entity, channel, sample, vol, atten)   -> start_sound(...)
    ambientsound(pos, sample, vol, atten)         -> start_sound(loop=True)

Spatialization matches Quake: distance attenuation fades a sound to silence at
1000/atten units, and the stereo split comes from the dot of the listener's
right-vector with the direction to the source. Channels store their world
origin so set_listener() can re-pan them every frame as the player moves.

A backend marks the mixer live by setting `ok = True` once its stream is open;
until then (or with no backend, e.g. an unsupported platform) the mixer is muted
and every entry point is a cheap no-op, so the game runs silently anywhere.
"""

import io
import threading
import wave
from array import array

OUT_RATE = 11025           # output sample rate (matches 188/190 Quake sounds)
OUT_CHANNELS = 2           # stereo
MAX_CHANNELS = 16          # simultaneous voices before we steal the oldest
NOMINAL_CLIP = 1000.0      # units at which atten=1 sounds reach silence
MASTER_VOL = 0.7           # overall gain


def _decode_wav(data):
    """RIFF/WAVE bytes -> (array('h') mono signed-16 @ OUT_RATE). Handles the
    two formats present in Quake paks: 8-bit unsigned and 16-bit signed mono."""
    w = wave.open(io.BytesIO(data))
    nch, width, rate, n = (w.getnchannels(), w.getsampwidth(),
                            w.getframerate(), w.getnframes())
    raw = w.readframes(n)
    w.close()

    # to signed-16 mono
    if width == 1:                          # 8-bit unsigned -> signed-16
        src = array("h", bytes(n * nch * 2))
        for i in range(n * nch):
            src[i] = (raw[i] - 128) << 8
    elif width == 2:                        # 16-bit signed little-endian
        src = array("h")
        src.frombytes(raw)
    else:
        raise ValueError(f"unsupported sample width {width}")
    if nch == 2:                            # average to mono
        src = array("h", [(src[2 * i] + src[2 * i + 1]) >> 1 for i in range(n)])

    if rate == OUT_RATE:
        return src
    # linear resample to OUT_RATE
    out_n = int(n * OUT_RATE / rate)
    out = array("h", bytes(out_n * 2))
    step = rate / OUT_RATE
    for i in range(out_n):
        p = i * step
        j = int(p)
        frac = p - j
        a = src[j]
        b = src[j + 1] if j + 1 < n else a
        out[i] = int(a + (b - a) * frac)
    return out


AMBIENT_SOUNDS = ("ambience/water1.wav", "ambience/wind2.wav")
AMBIENT_LEVEL = 0.3         # ambient_level cvar: peak ambient volume
AMBIENT_FADE = 100.0 / 255  # ambient_fade cvar: volume ramp per second


class Mixer:
    def __init__(self):
        self.sounds = {}                    # name -> array('h')
        self.channels = []                  # active voices
        self.lock = threading.Lock()
        self.listener = (0.0, 0.0, 0.0)
        self.right = (0.0, 1.0, 0.0)
        self.ok = False                     # a backend flips this on once live
        self._ambients = [None, None]       # dedicated water/sky loop voices

    # ---- the realtime mix (called by the platform backend) ------------------
    def mix(self, nframes):
        """Sum every active voice into `nframes` of interleaved int16 stereo.

        Returns an array('h') of length nframes * OUT_CHANNELS, clamped to int16.
        Runs on the backend's audio thread; never raises -- on any error it
        returns silence so a backend's stream can't be killed by the mixer."""
        try:
            F = nframes
            out = [0] * (F * OUT_CHANNELS)
            with self.lock:
                for ch in self.channels:
                    s = ch["samples"]
                    n = len(s)
                    pos = ch["pos"]
                    lv = ch["lv"]
                    rv = ch["rv"]
                    loop = ch["loop"]
                    i = 0
                    while i < F:
                        if pos >= n:
                            if loop:
                                pos = 0
                                if n == 0:
                                    break
                            else:
                                ch["done"] = True
                                break
                        v = s[pos]
                        out[2 * i] += (v * lv) >> 8
                        out[2 * i + 1] += (v * rv) >> 8
                        pos += 1
                        i += 1
                    ch["pos"] = pos
                if any(c.get("done") for c in self.channels):
                    self.channels = [c for c in self.channels if not c.get("done")]
            # clamp to int16
            for k in range(F * OUT_CHANNELS):
                x = out[k]
                if x > 32767:
                    out[k] = 32767
                elif x < -32768:
                    out[k] = -32768
            return array("h", out)
        except Exception as e:              # never let an exception kill audio
            print(f"snd: mix error {e}")
            return array("h", bytes(nframes * OUT_CHANNELS * 2))

    # ---- public API ---------------------------------------------------------
    def precache(self, name, data):
        if not self.ok or name in self.sounds:
            return
        try:
            self.sounds[name] = _decode_wav(data)
        except Exception as e:
            print(f"snd: cannot decode {name}: {e}")

    def set_listener(self, origin, right):
        """Listener position + right-vector; re-pans all positioned voices."""
        if not self.ok:
            return
        self.listener = origin
        self.right = right
        with self.lock:
            for ch in self.channels:
                if ch["origin"] is not None:
                    ch["lv"], ch["rv"] = self._spatialize(
                        ch["origin"], ch["vol"], ch["atten"])

    def start_sound(self, ent, channel, name, vol, atten, origin, loop=False):
        if not self.ok:
            return
        s = self.sounds.get(name)
        if s is None:
            return
        if atten == 0.0 or origin is None:
            lv = rv = int(min(1.0, vol) * MASTER_VOL * 256)
            origin = None                   # non-positional: don't re-pan
        else:
            lv, rv = self._spatialize(origin, vol, atten)
            if lv <= 0 and rv <= 0 and not loop:
                return                      # inaudible one-shot: skip
        ch = {"samples": s, "pos": 0, "lv": lv, "rv": rv, "loop": loop,
              "ent": ent, "chan": channel, "origin": origin, "vol": vol,
              "atten": atten, "done": False}
        with self.lock:
            # a non-auto channel replaces the entity's previous sound on it
            if channel != 0:
                self.channels = [c for c in self.channels
                                 if not (c["ent"] == ent and c["chan"] == channel)]
            if len(self.channels) >= MAX_CHANNELS:
                # steal the oldest voice, but never a dedicated ambient
                for i, c in enumerate(self.channels):
                    if c not in self._ambients:
                        self.channels.pop(i)
                        break
            self.channels.append(ch)

    def update_ambients(self, levels, dt):
        """S_UpdateAmbientSounds: ramp the dedicated water/sky loops toward
        the listener leaf's qbsp-baked ambient levels (0-255 bytes), at the
        ambient fade rate -- water murmurs near water, wind under sky."""
        if not self.ok:
            return
        for i, name in enumerate(AMBIENT_SOUNDS):
            target = AMBIENT_LEVEL * (min(255, levels[i]) / 255.0)
            ch = self._ambients[i]
            if ch is None:
                if target <= 0.0:
                    continue
                s = self.sounds.get(name)
                if s is None:
                    continue
                ch = {"samples": s, "pos": 0, "lv": 0, "rv": 0, "loop": True,
                      "ent": -1, "chan": -1, "origin": None, "vol": 0.0,
                      "atten": 0.0, "done": False}
                self._ambients[i] = ch
                with self.lock:
                    self.channels.append(ch)
            vol = ch["vol"]
            if vol < target:
                vol = min(target, vol + AMBIENT_FADE * dt)
            elif vol > target:
                vol = max(target, vol - AMBIENT_FADE * dt)
            ch["vol"] = vol
            ch["lv"] = ch["rv"] = int(vol * MASTER_VOL * 256)

    def stop_all(self):
        if not self.ok:
            return
        with self.lock:
            self.channels = []
            self._ambients = [None, None]

    # ---- spatialization (Quake SND_Spatialize) ------------------------------
    def _spatialize(self, origin, vol, atten):
        lx, ly, lz = self.listener
        dx, dy, dz = origin[0] - lx, origin[1] - ly, origin[2] - lz
        d = (dx * dx + dy * dy + dz * dz) ** 0.5
        if d > 1e-6:
            dx /= d
            dy /= d
            dz /= d
        dist = d * atten / NOMINAL_CLIP
        near = 1.0 - dist
        if near <= 0.0:
            return 0, 0
        dot = dx * self.right[0] + dy * self.right[1] + dz * self.right[2]
        master = min(1.0, vol) * MASTER_VOL
        right = master * near * (1.0 + dot)
        left = master * near * (1.0 - dot)
        lv = int(max(0.0, left) * 256)
        rv = int(max(0.0, right) * 256)
        return lv, rv


# ---- standalone self-test (silent: no audio device) ------------------------
# Decodes a few pak sounds and mixes one buffer, exercising everything but the
# OS stream. For an AUDIBLE test, run a platform backend instead (python3 mac.py).
#   python3 -m quake.snd [sound/path.wav ...]
if __name__ == "__main__":
    import sys
    from quake.pak import Pak

    pak = Pak("quake-shareware/id1/pak0.pak")
    names = sys.argv[1:] or ["sound/weapons/rocket1.wav",
                             "sound/weapons/sgun1.wav"]
    m = Mixer()
    m.ok = True                             # enable decode/mix without a backend
    for n in names:
        if n in pak.files:
            m.precache(n, pak.read(n))
            print("precached", n, len(m.sounds.get(n, [])), "samples")
        else:
            print("not in pak:", n)
    m.set_listener((0, 0, 0), (0, 1, 0))
    for n in names:
        if n in m.sounds:
            m.start_sound(1, 0, n, 1.0, 1.0, (0.0, 800.0, 0.0))
    buf = m.mix(512)
    peak = max((abs(x) for x in buf), default=0)
    print(f"mixed {len(buf)} samples, peak {peak}")
    print("done")

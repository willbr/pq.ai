"""Software sound mixer feeding macOS CoreAudio directly through ctypes.

No third-party deps: ctypes calls the system AudioToolbox framework, and the
mixing is plain Python -- a port of Quake's S_PaintChannels / SND_Spatialize.

One AudioQueue output stream (16-bit stereo, 11025 Hz) runs a callback on a
CoreAudio thread. The callback sums every active channel's samples into the
buffer. Sounds are decoded + resampled to the output rate ONCE at precache, so
the realtime path just steps one source sample per output sample and adds --
no resampling, no allocation per sound.

    sound(entity, channel, sample, vol, atten)   -> start_sound(...)
    ambientsound(pos, sample, vol, atten)         -> start_sound(loop=True)

Spatialization matches Quake: distance attenuation fades a sound to silence at
1000/atten units, and the stereo split comes from the dot of the listener's
right-vector with the direction to the source. Channels store their world
origin so set_listener() can re-pan them every frame as the player moves.
"""

import ctypes
import io
import threading
import wave
from array import array
from ctypes import (CFUNCTYPE, POINTER, Structure, byref, c_double, c_int32,
                    c_uint32, c_void_p)

OUT_RATE = 11025           # output sample rate (matches 188/190 Quake sounds)
OUT_CHANNELS = 2           # stereo
FRAMES_PER_BUF = 512       # ~46 ms per buffer
NUM_BUFFERS = 3            # queued ahead; underrun-safe, ~70 ms typical latency
MAX_CHANNELS = 16          # simultaneous voices before we steal the oldest
NOMINAL_CLIP = 1000.0      # units at which atten=1 sounds reach silence
MASTER_VOL = 0.7           # overall gain

# ---- CoreAudio (AudioToolbox) types via ctypes -----------------------------
_FMT_LPCM = 0x6C70636D                     # 'lpcm'  (FourCharCode, big-endian)
_FLAG_SIGNED_INT = 0x4                      # kAudioFormatFlagIsSignedInteger
_FLAG_PACKED = 0x8                          # kAudioFormatFlagIsPacked


class _ASBD(Structure):                     # AudioStreamBasicDescription
    _fields_ = [("mSampleRate", c_double),
                ("mFormatID", c_uint32),
                ("mFormatFlags", c_uint32),
                ("mBytesPerPacket", c_uint32),
                ("mFramesPerPacket", c_uint32),
                ("mBytesPerFrame", c_uint32),
                ("mChannelsPerFrame", c_uint32),
                ("mBitsPerChannel", c_uint32),
                ("mReserved", c_uint32)]


class _AQBuffer(Structure):                 # AudioQueueBuffer
    _fields_ = [("mAudioDataBytesCapacity", c_uint32),
                ("mAudioData", c_void_p),
                ("mAudioDataByteSize", c_uint32),
                ("mUserData", c_void_p),
                ("mPacketDescriptionCapacity", c_uint32),
                ("mPacketDescriptions", c_void_p),
                ("mPacketDescriptionCount", c_uint32)]


_AQBufferRef = POINTER(_AQBuffer)
_AQRef = c_void_p
# void cb(void *user, AudioQueueRef aq, AudioQueueBufferRef buf)
_CALLBACK = CFUNCTYPE(None, c_void_p, _AQRef, _AQBufferRef)

_LIB = "/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox"


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


class Mixer:
    def __init__(self):
        self.sounds = {}                    # name -> array('h')
        self.channels = []                  # active voices
        self.lock = threading.Lock()
        self.listener = (0.0, 0.0, 0.0)
        self.right = (0.0, 1.0, 0.0)
        self.ok = False
        try:
            self._open_stream()
            self.ok = True
        except Exception as e:              # no audio device / headless -> silent
            print(f"snd: audio unavailable ({e}); running muted")

    # ---- CoreAudio setup ----------------------------------------------------
    def _open_stream(self):
        at = ctypes.CDLL(_LIB)
        at.AudioQueueNewOutput.argtypes = [POINTER(_ASBD), _CALLBACK, c_void_p,
                                           c_void_p, c_void_p, c_uint32,
                                           POINTER(_AQRef)]
        at.AudioQueueNewOutput.restype = c_int32
        at.AudioQueueAllocateBuffer.argtypes = [_AQRef, c_uint32,
                                                POINTER(_AQBufferRef)]
        at.AudioQueueAllocateBuffer.restype = c_int32
        at.AudioQueueEnqueueBuffer.argtypes = [_AQRef, _AQBufferRef, c_uint32,
                                               c_void_p]
        at.AudioQueueEnqueueBuffer.restype = c_int32
        at.AudioQueueStart.argtypes = [_AQRef, c_void_p]
        at.AudioQueueStart.restype = c_int32
        at.AudioQueueStop.argtypes = [_AQRef, c_uint32]
        at.AudioQueueStop.restype = c_int32
        self._at = at

        fmt = _ASBD(mSampleRate=float(OUT_RATE), mFormatID=_FMT_LPCM,
                    mFormatFlags=_FLAG_SIGNED_INT | _FLAG_PACKED,
                    mBytesPerPacket=2 * OUT_CHANNELS, mFramesPerPacket=1,
                    mBytesPerFrame=2 * OUT_CHANNELS,
                    mChannelsPerFrame=OUT_CHANNELS, mBitsPerChannel=16,
                    mReserved=0)
        self._fmt = fmt                     # keep alive

        self._cb = _CALLBACK(self._fill)    # keep the trampoline alive (vital)
        self._queue = _AQRef()
        # NULL run loop + mode => callback fires on an internal CoreAudio thread
        err = at.AudioQueueNewOutput(byref(fmt), self._cb, None, None, None, 0,
                                     byref(self._queue))
        if err:
            raise OSError(f"AudioQueueNewOutput failed ({err})")

        bufsize = FRAMES_PER_BUF * OUT_CHANNELS * 2
        self._buffers = []
        for _ in range(NUM_BUFFERS):
            ref = _AQBufferRef()
            err = at.AudioQueueAllocateBuffer(self._queue, bufsize, byref(ref))
            if err:
                raise OSError(f"AudioQueueAllocateBuffer failed ({err})")
            self._buffers.append(ref)
            self._fill(None, self._queue, ref)   # prime with silence + enqueue
        err = at.AudioQueueStart(self._queue, None)
        if err:
            raise OSError(f"AudioQueueStart failed ({err})")

    # ---- the realtime mix callback -----------------------------------------
    def _fill(self, user, aq, buf):
        try:
            F = FRAMES_PER_BUF
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
            # clamp to int16 and pack
            for k in range(F * OUT_CHANNELS):
                x = out[k]
                if x > 32767:
                    out[k] = 32767
                elif x < -32768:
                    out[k] = -32768
            data = array("h", out).tobytes()
            ctypes.memmove(buf.contents.mAudioData, data, len(data))
            buf.contents.mAudioDataByteSize = len(data)
            self._at.AudioQueueEnqueueBuffer(aq, buf, 0, None)
        except Exception as e:              # never let an exception kill audio
            print(f"snd: mix error {e}")

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
                self.channels.pop(0)        # steal the oldest voice
            self.channels.append(ch)

    def stop_all(self):
        if not self.ok:
            return
        with self.lock:
            self.channels = []

    def shutdown(self):
        if not self.ok:
            return
        try:
            self._at.AudioQueueStop(self._queue, 1)
        except Exception:
            pass

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


# ---- standalone self-test: python3 snd.py [sound/path.wav ...] --------------
if __name__ == "__main__":
    import sys
    import time
    from pak import Pak

    pak = Pak("quake-shareware/id1/pak0.pak")
    names = sys.argv[1:] or ["sound/weapons/rocket1.wav",
                             "sound/weapons/sgun1.wav",
                             "sound/player/death1.wav"]
    m = Mixer()
    for n in names:
        if n in pak.files:
            m.precache(n, pak.read(n))
            print("precached", n, len(m.sounds.get(n, [])), "samples")
        else:
            print("not in pak:", n)
    # pan each one across the stereo field as it plays
    m.set_listener((0, 0, 0), (0, 1, 0))
    for i, n in enumerate(names):
        if n in m.sounds:
            side = -800 if i % 2 == 0 else 800
            print("playing", n, "(left)" if side < 0 else "(right)")
            m.start_sound(1, 0, n, 1.0, 1.0, (0.0, float(side), 0.0))
            time.sleep(1.2)
    time.sleep(0.5)
    m.shutdown()
    print("done")

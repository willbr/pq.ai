"""macOS audio backend: feed the Quake mixer's samples to CoreAudio via ctypes.

Lives OUTSIDE the `quake` engine package because it is platform-specific. The
engine's quake.snd.Mixer produces samples with no OS dependency; this backend
owns the actual output stream. Windows/Linux get sibling backend files, and
main.py picks one by sys.platform.

One AudioQueue output stream (16-bit stereo, 11025 Hz) runs a callback on a
CoreAudio thread; the callback pulls FRAMES_PER_BUF frames from the mixer and
copies them into the queue buffer. Constructing CoreAudioBackend(mixer) opens the
stream and, on success, sets mixer.ok = True; if no audio device is available it
prints a warning and leaves the mixer muted so the game still runs.
"""

import ctypes
from ctypes import (CFUNCTYPE, POINTER, Structure, byref, c_double, c_int32,
                    c_uint32, c_void_p)

from quake.snd import OUT_RATE, OUT_CHANNELS

FRAMES_PER_BUF = 512       # ~46 ms per buffer
NUM_BUFFERS = 3            # queued ahead; underrun-safe, ~70 ms typical latency

# ---- CoreAudio (AudioToolbox) types via ctypes -----------------------------
_FMT_LPCM = 0x6C70636D                      # 'lpcm'  (FourCharCode, big-endian)
_FLAG_SIGNED_INT = 0x4                       # kAudioFormatFlagIsSignedInteger
_FLAG_PACKED = 0x8                           # kAudioFormatFlagIsPacked


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


class CoreAudioBackend:
    """Opens a CoreAudio AudioQueue and drives it from a quake.snd.Mixer.

    On success sets mixer.ok = True so the mixer starts accepting/mixing voices;
    on failure (no device, headless) prints a warning and leaves it muted."""

    def __init__(self, mixer):
        self.mixer = mixer
        try:
            self._open_stream()
            mixer.ok = True
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
            self._fill(None, self._queue, ref)   # prime with samples + enqueue
        err = at.AudioQueueStart(self._queue, None)
        if err:
            raise OSError(f"AudioQueueStart failed ({err})")

    # ---- the realtime callback: pull from the mixer, hand to CoreAudio ------
    def _fill(self, user, aq, buf):
        try:
            data = self.mixer.mix(FRAMES_PER_BUF).tobytes()
            ctypes.memmove(buf.contents.mAudioData, data, len(data))
            buf.contents.mAudioDataByteSize = len(data)
            self._at.AudioQueueEnqueueBuffer(aq, buf, 0, None)
        except Exception as e:              # never let an exception kill audio
            print(f"snd: enqueue error {e}")

    def shutdown(self):
        try:
            self._at.AudioQueueStop(self._queue, 1)
        except Exception:
            pass


# ---- standalone audible self-test: python3 mac.py [sound/path.wav ...] ------
if __name__ == "__main__":
    import sys
    import time
    from quake.pak import Pak
    from quake.snd import Mixer

    pak = Pak("quake-shareware/id1/pak0.pak")
    names = sys.argv[1:] or ["sound/weapons/rocket1.wav",
                             "sound/weapons/sgun1.wav",
                             "sound/player/death1.wav"]
    m = Mixer()
    backend = CoreAudioBackend(m)
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
    backend.shutdown()
    print("done")

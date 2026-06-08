"""Windows audio backend: feed the Quake mixer's samples to winmm waveOut via ctypes.

Lives OUTSIDE the `quake` engine package because it is platform-specific -- a
sibling to mac.py's CoreAudioBackend. The engine's quake.snd.Mixer produces
samples with no OS dependency; this backend owns the actual output stream, and
main.py picks one by sys.platform.

A small pool of WAVEHDR buffers (16-bit stereo, 11025 Hz) is kept queued on the
default waveOut device (WAVE_MAPPER). The device is opened with CALLBACK_EVENT,
so it signals a Win32 auto-reset event each time a buffer finishes playing; a
daemon feeder thread waits on that event, refills every finished buffer from the
mixer, and re-submits it -- so the device never runs dry. winmm forbids calling
waveOut* from inside a callback, hence the feeder-thread design (rather than
mac.py's refill-from-the-callback approach).

Constructing WinmmBackend(mixer) opens the device and, on success, sets
mixer.ok = True; if no device is available it prints a warning and leaves the
mixer muted so the game still runs.
"""

import ctypes
import threading
from ctypes import POINTER, byref, c_char_p, c_void_p, sizeof
from ctypes import wintypes

from quake.snd import OUT_RATE, OUT_CHANNELS

FRAMES_PER_BUF = 512       # ~46 ms per buffer
NUM_BUFFERS = 4            # queued ahead; underrun-safe, ~185 ms of slack

# ---- winmm / waveOut constants ----------------------------------------------
WAVE_FORMAT_PCM = 1
WAVE_MAPPER = 0xFFFFFFFF                      # the default output device
CALLBACK_EVENT = 0x00050000                  # dwCallback is a Win32 event handle
WHDR_DONE = 0x00000001                        # set by the driver when a buffer ends
WAIT_TIMEOUT_MS = 100                         # cap the event wait so shutdown is prompt


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", wintypes.WORD),
                ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD)]


class WAVEHDR(ctypes.Structure):
    pass


# dwUser / reserved are DWORD_PTR (pointer-sized); lpNext is a pointer -- model
# all three as c_void_p so the struct layout is correct on 32- and 64-bit.
WAVEHDR._fields_ = [("lpData", c_char_p),
                    ("dwBufferLength", wintypes.DWORD),
                    ("dwBytesRecorded", wintypes.DWORD),
                    ("dwUser", c_void_p),
                    ("dwFlags", wintypes.DWORD),
                    ("dwLoops", wintypes.DWORD),
                    ("lpNext", c_void_p),
                    ("reserved", c_void_p)]

HWAVEOUT = wintypes.HANDLE


class WinmmBackend:
    """Opens a winmm waveOut stream and drives it from a quake.snd.Mixer.

    On success sets mixer.ok = True so the mixer starts accepting/mixing voices;
    on failure (no device, headless) prints a warning and leaves it muted."""

    def __init__(self, mixer):
        self.mixer = mixer
        self.running = False
        self._hwaveout = HWAVEOUT()
        self._event = None
        self._thread = None
        try:
            self._open_stream()
            mixer.ok = True
        except Exception as e:              # no audio device / headless -> silent
            print(f"snd: audio unavailable ({e}); running muted")

    # ---- winmm setup --------------------------------------------------------
    def _open_stream(self):
        self._winmm = ctypes.WinDLL("winmm")
        self._k32 = ctypes.WinDLL("kernel32")
        winmm, k32 = self._winmm, self._k32

        winmm.waveOutOpen.argtypes = [POINTER(HWAVEOUT), wintypes.UINT,
                                      POINTER(WAVEFORMATEX), c_void_p, c_void_p,
                                      wintypes.DWORD]
        winmm.waveOutOpen.restype = wintypes.UINT
        for fn in (winmm.waveOutPrepareHeader, winmm.waveOutUnprepareHeader,
                   winmm.waveOutWrite):
            fn.argtypes = [HWAVEOUT, POINTER(WAVEHDR), wintypes.UINT]
            fn.restype = wintypes.UINT
        winmm.waveOutReset.argtypes = [HWAVEOUT]
        winmm.waveOutClose.argtypes = [HWAVEOUT]

        # auto-reset event the driver signals as each buffer finishes
        k32.CreateEventW.argtypes = [c_void_p, wintypes.BOOL, wintypes.BOOL,
                                     wintypes.LPCWSTR]
        k32.CreateEventW.restype = wintypes.HANDLE
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        self._event = k32.CreateEventW(None, False, False, None)
        if not self._event:
            raise OSError("CreateEventW failed")

        fmt = WAVEFORMATEX(
            wFormatTag=WAVE_FORMAT_PCM, nChannels=OUT_CHANNELS,
            nSamplesPerSec=OUT_RATE,
            nAvgBytesPerSec=OUT_RATE * OUT_CHANNELS * 2,
            nBlockAlign=OUT_CHANNELS * 2, wBitsPerSample=16, cbSize=0)
        self._fmt = fmt                     # keep alive

        err = winmm.waveOutOpen(byref(self._hwaveout), WAVE_MAPPER, byref(fmt),
                                self._event, None, CALLBACK_EVENT)
        if err:
            raise OSError(f"waveOutOpen failed ({err})")

        # allocate, prepare, prime and queue every buffer up front
        bufsize = FRAMES_PER_BUF * OUT_CHANNELS * 2
        self._bufs = []                     # ctypes byte buffers (kept alive)
        self._headers = []                  # WAVEHDR structs (kept alive)
        for _ in range(NUM_BUFFERS):
            buf = ctypes.create_string_buffer(bufsize)
            hdr = WAVEHDR(lpData=ctypes.cast(buf, c_char_p),
                          dwBufferLength=bufsize)
            err = winmm.waveOutPrepareHeader(self._hwaveout, byref(hdr),
                                             sizeof(hdr))
            if err:
                raise OSError(f"waveOutPrepareHeader failed ({err})")
            self._bufs.append(buf)
            self._headers.append(hdr)
            self._fill_and_write(buf, hdr)

        self.running = True
        self._thread = threading.Thread(target=self._feed, daemon=True)
        self._thread.start()

    # ---- buffer refill ------------------------------------------------------
    def _fill_and_write(self, buf, hdr):
        """Pull one buffer of samples from the mixer and (re)submit it. The
        driver clears WHDR_DONE on waveOutWrite and sets it again when done."""
        data = self.mixer.mix(FRAMES_PER_BUF).tobytes()
        ctypes.memmove(buf, data, len(data))
        hdr.dwBufferLength = len(data)
        self._winmm.waveOutWrite(self._hwaveout, byref(hdr), sizeof(hdr))

    # ---- the feeder thread: wait, refill finished buffers, repeat -----------
    def _feed(self):
        wait = self._k32.WaitForSingleObject
        while self.running:
            wait(self._event, WAIT_TIMEOUT_MS)
            if not self.running:
                break
            try:
                for buf, hdr in zip(self._bufs, self._headers):
                    if hdr.dwFlags & WHDR_DONE:
                        self._fill_and_write(buf, hdr)
            except Exception as e:          # never let an exception kill audio
                print(f"snd: enqueue error {e}")

    def shutdown(self):
        self.running = False
        try:
            self._winmm.waveOutReset(self._hwaveout)   # flush, mark buffers done
            if self._thread is not None:
                self._thread.join(timeout=0.5)
            for hdr in getattr(self, "_headers", []):
                self._winmm.waveOutUnprepareHeader(self._hwaveout, byref(hdr),
                                                   sizeof(hdr))
            self._winmm.waveOutClose(self._hwaveout)
        except Exception:
            pass


# ---- standalone audible self-test: python win.py [sound/path.wav ...] -------
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
    backend = WinmmBackend(m)
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

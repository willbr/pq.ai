"""macOS CoreAudio teardown (mac.py): stop + dispose the AudioQueue cleanly.

The CoreAudio callback runs on its own thread; on quit the queue must be stopped
SYNCHRONOUSLY and disposed, or the callback can fire into half-freed ctypes
state and segfault (the nondeterministic crash CLAUDE.md warns about). These
tests drive the teardown logic with a fake AudioToolbox so they run on any
platform -- no real audio device, no _open_stream / CDLL load.

Pins:
  - shutdown() stops (immediate flag = 1) THEN disposes (flag = 1), in order;
  - shutdown() is idempotent (host calls it on quit, atexit is the backstop);
  - the realtime callback bails the instant shutdown has begun.
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

import mac


class FakeAT:
    """Records the AudioToolbox calls teardown makes, in order."""
    def __init__(self):
        self.calls = []

    def AudioQueueStop(self, q, immediate):
        self.calls.append(("stop", immediate))
        return 0

    def AudioQueueDispose(self, q, immediate):
        self.calls.append(("dispose", immediate))
        return 0

    def AudioQueueEnqueueBuffer(self, aq, buf, n, p):
        self.calls.append(("enqueue",))
        return 0


def _backend():
    """A CoreAudioBackend with the stream faked out (no real CoreAudio)."""
    b = mac.CoreAudioBackend.__new__(mac.CoreAudioBackend)
    b.mixer = None
    b._closed = False
    b._at = FakeAT()
    b._queue = mac._AQRef()
    return b


def test_shutdown_stops_then_disposes_synchronously():
    b = _backend()
    b.shutdown()
    assert b._at.calls == [("stop", 1), ("dispose", 1)], \
        f"expected synchronous stop then dispose, got {b._at.calls}"
    assert b._closed is True


def test_shutdown_is_idempotent():
    b = _backend()
    b.shutdown()
    b.shutdown()                # atexit backstop after an explicit host call
    assert b._at.calls == [("stop", 1), ("dispose", 1)], \
        f"second shutdown must be a no-op, got {b._at.calls}"


def test_shutdown_tolerates_unopened_stream():
    # _open_stream failed (no device): no _queue/_at, shutdown must not raise
    b = mac.CoreAudioBackend.__new__(mac.CoreAudioBackend)
    b._closed = False
    b.shutdown()
    assert b._closed is True


def test_callback_bails_once_closed():
    b = _backend()
    b.shutdown()
    b._at.calls.clear()
    b._fill(None, b._queue, None)   # would deref a NULL buffer if it didn't bail
    assert b._at.calls == [], "callback ran after shutdown (would touch a dead queue)"


if __name__ == "__main__":
    test_shutdown_stops_then_disposes_synchronously()
    test_shutdown_is_idempotent()
    test_shutdown_tolerates_unopened_stream()
    test_callback_bails_once_closed()
    print("OK")

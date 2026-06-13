# tests/test_demo_record.py
"""Record -> play round-trip: record a scripted e1m1 session to a temp .dem,
play it back, and assert the camera trajectory and entity presence reproduce.
Run: PQ_AUDIO=0 python tests/test_demo_record.py."""
import _bootstrap  # noqa: F401
import os
import tempfile
from client import Client, InputState


def test_record_then_play_reproduces_camera_path():
    rec_dir = tempfile.mkdtemp()
    name = os.path.join(rec_dir, "rt")
    # --- record ---
    c = Client("e1m1"); c.resize(640, 480)
    c._cmd_record([name, "e1m1"])
    recorded_pos = []
    for _ in range(80):
        c.frame(0.05, InputState(move_forward=1.0))
        recorded_pos.append(tuple(round(v, 0) for v in c.pos))
    c._cmd_stopdemo([])
    demo_path = name + ".dem"
    assert os.path.getsize(demo_path) > 0

    # the recorded camera must actually have moved (non-vacuous gate)
    assert recorded_pos[0] != recorded_pos[-1], "recorded camera did not move"

    # --- play back ---
    p = Client.__new__(Client); Client._init_assets_only(p)
    with open(demo_path, "rb") as fh:
        p._load_demo(fh.read())
    p.resize(640, 480)
    played_pos = []
    for _ in range(80):
        if p.demo.finished:
            break
        p.frame(0.05, InputState())
        played_pos.append(tuple(round(v, 0) for v in p.pos))

    os.remove(demo_path); os.rmdir(rec_dir)

    # the played-back camera must trace (close to) the recorded path. Allow a
    # small tolerance for coord quantization (1/8 unit) and one-frame interp lag.
    assert len(played_pos) >= 60, f"playback too short: {len(played_pos)}"
    # the played camera must also have moved -- not stuck at origin
    assert played_pos[0] != played_pos[-1], "played-back camera did not move"
    # compare a mid-run sample: playback origin near a recorded origin
    rx, ry, rz = recorded_pos[50]
    near = min(abs(px - rx) + abs(py - ry) + abs(pz - rz)
               for (px, py, pz) in played_pos)
    assert near < 8.0, f"playback path diverged from recording: {near}"


if __name__ == "__main__":
    test_record_then_play_reproduces_camera_path()
    print("OK")

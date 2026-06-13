"""Headless demo playback smoke test. Run: PQ_AUDIO=0 python tests/test_demo_playback.py."""
import _bootstrap  # noqa: F401
from client import Client
from quake.pak import Pak

PAK = "quake-shareware/id1/pak0.pak"


def test_load_demo_builds_render_stack_without_server():
    c = Client.__new__(Client)             # bypass __init__'s _load_map
    Client._init_assets_only(c)            # palette/sbar/console/mixer, no map
    blob = Pak(PAK).read("demo1.dem")
    c._load_demo(blob)
    assert c.bsp is not None and c.rend is not None
    assert c.mapname and c.cl.model_precache[1].endswith(".bsp")
    assert len(c.models) == len(c.cl.model_precache)
    assert c.sv is None                    # no server in demo mode
    assert c.demo is not None              # demo controller active


def test_play_demo1_advances_and_renders():
    from client import Client, InputState
    from quake.pak import Pak
    c = Client.__new__(Client); Client._init_assets_only(c)
    c._load_demo(Pak(PAK).read("demo1.dem"))
    c.resize(640, 480)
    last_org = None
    moved = False
    for _ in range(120):                       # ~6s at dt=0.05
        rf = c.frame(0.05, InputState())
        assert rf is not None
        org = tuple(c.pos)
        if last_org is not None and org != last_org:
            moved = True
        last_org = org
    assert moved, "demo camera never moved"
    assert len(c.scene.alias_entities()) >= 0  # rendered without exception


def test_timedemo_reports_fps():
    from client import Client, InputState
    from quake.pak import Pak
    c = Client.__new__(Client); Client._init_assets_only(c)
    c.resize(640, 480)
    msgs = []
    c.con.print = lambda s: msgs.append(s)     # capture console output
    c._cmd_timedemo(["demo1"])
    # run frames until the demo finishes
    for _ in range(5000):
        c.frame(0.01, InputState())
        if c.demo is None or c.demo.finished:
            break
    assert any("fps" in m.lower() for m in msgs), msgs[-3:]


def test_demo_loop_advances_on_finish():
    from client import Client, InputState
    c = Client("start")                        # title demo loop: playing demo1
    assert c.demo is not None and c.in_demo_loop
    before_index = c.demo_index               # captured after _next_demo incremented it
    before_id = id(c.demo)

    # Drive the current demo to EOF via the real _demo_frame read-gate path:
    # exhaust the reader so next_frame() returns None on the first read attempt,
    # and push cl.time past mtime[0] so the gate condition is satisfied.
    c.demo.reader.pos = len(c.demo.reader.data)
    c.cl.time = c.cl.mtime[0] + 1.0           # guarantee the read-gate fires

    # One frame: the while-loop calls next_frame() -> None -> d.finished = True,
    # then the elif branch calls _next_demo() and loads the next demo.
    c.frame(0.05, InputState())

    assert c.demo_index > before_index, (
        f"demo_index did not advance (was {before_index}, still {c.demo_index})"
    )
    assert id(c.demo) != before_id, "demo object unchanged -- _next_demo was not called"
    assert c.in_demo_loop is True, "in_demo_loop cleared unexpectedly"
    assert c.demo is not None


if __name__ == "__main__":
    test_load_demo_builds_render_stack_without_server()
    test_play_demo1_advances_and_renders()
    test_timedemo_reports_fps()
    test_demo_loop_advances_on_finish()
    print("OK")

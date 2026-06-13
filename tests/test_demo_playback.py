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


if __name__ == "__main__":
    test_load_demo_builds_render_stack_without_server()
    test_play_demo1_advances_and_renders()
    print("OK")

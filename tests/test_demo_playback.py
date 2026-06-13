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


if __name__ == "__main__":
    test_load_demo_builds_render_stack_without_server()
    print("OK")

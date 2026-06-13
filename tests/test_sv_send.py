"""Server serialization tests (quake/sv_send.py): baselines, datagram build,
and message build->bytes checks on the real shareware stack.
Run muted: PQ_AUDIO=0 python tests/test_sv_send.py -> prints OK.

The full build->parse round-trips (clientdata/datagram/serverinfo) land once
quake/cl_parse.py exists; until then these assert the writer emits well-formed
bytes (right svc id, non-empty payload)."""
import _bootstrap  # noqa: F401
from quake.pak import Pak
from quake.bsp import Bsp
from quake.progs import Progs
from quake.sv import Server
from quake.physics import Physics

PAK = "quake-shareware/id1/pak0.pak"


def _boot():
    pak = Pak(PAK)
    b = Bsp(pak.read("maps/e1m1.bsp"))
    sv = Server(Progs(pak.read("progs.dat")), bsp=b,
                mapname="maps/e1m1.bsp", skill=1, pak=pak)
    sv.phys = Physics(b)
    sv.load_level()
    sv.spawn_player((480.0, 0.0, 100.0), (0.0, 0.0, 0.0))
    for _ in range(3):
        sv.run_frame(0.1)
    return sv


def test_baselines_snapshot_spawned_entities():
    sv = _boot()
    sv.create_baseline()
    assert sv.baselines, "no baselines created"
    # world is edict 0; the first real entity has a modelindex baseline
    some = next(b for b in sv.baselines.values())
    assert hasattr(some, "modelindex") and hasattr(some, "origin")


def test_write_entities_emits_parseable_updates():
    from quake.msg import MsgWriter
    from quake.sv_send import write_entities_to_client
    sv = _boot()
    sv.create_baseline()
    w = MsgWriter()
    write_entities_to_client(sv, w, (480.0, 0.0, 100.0))
    # at least one update command byte with the U_SIGNAL high bit set
    assert w.data, "no entity bytes written"
    assert w.data[0] & 0x80, "first entity command must have U_SIGNAL high bit"


def test_clientdata_writes_svc_id():
    from quake.msg import MsgWriter
    from quake.sv_send import write_clientdata_to_message
    sv = _boot()
    w = MsgWriter()
    write_clientdata_to_message(sv, w)
    assert w.data, "no clientdata bytes written"
    assert w.data[0] == 15, "clientdata must start with svc_clientdata (15)"


def test_serverinfo_writes_svc_id():
    from quake.msg import MsgWriter
    from quake.sv_send import write_serverinfo
    sv = _boot()
    sv.create_baseline()
    w = MsgWriter()
    write_serverinfo(sv, w)
    assert w.data, "no serverinfo bytes written"
    assert w.data[0] == 11, "serverinfo must start with svc_serverinfo (11)"


def test_build_datagram_writes_svc_time():
    from quake.msg import MsgWriter
    from quake.sv_send import build_datagram
    sv = _boot()
    sv.create_baseline()
    w = MsgWriter()
    build_datagram(sv, w)
    assert w.data, "no datagram bytes written"
    assert w.data[0] == 7, "datagram must start with svc_time (7)"


if __name__ == "__main__":
    test_baselines_snapshot_spawned_entities()
    test_write_entities_emits_parseable_updates()
    test_clientdata_writes_svc_id()
    test_serverinfo_writes_svc_id()
    test_build_datagram_writes_svc_time()
    print("OK")

"""Server serialization tests (quake/sv_send.py): baselines, datagram build,
and message build->bytes checks on the real shareware stack.
Run muted: PQ_AUDIO=0 python tests/test_sv_send.py -> prints OK.

The full build->parse round-trips (clientdata/datagram/serverinfo) land once
quake/cl_parse.py exists; until then these assert the writer emits well-formed
bytes (right svc id, non-empty payload)."""
import _bootstrap  # noqa: F401
from quake import protocol as P
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
    assert w.data[0] == P.svc_clientdata, "clientdata must start with svc_clientdata (15)"


def test_serverinfo_writes_svc_id():
    from quake.msg import MsgWriter
    from quake.sv_send import write_serverinfo
    sv = _boot()
    sv.create_baseline()
    w = MsgWriter()
    write_serverinfo(sv, w)
    assert w.data, "no serverinfo bytes written"
    assert w.data[0] == P.svc_serverinfo, "serverinfo must start with svc_serverinfo (11)"


def test_build_datagram_writes_svc_time():
    from quake.msg import MsgWriter
    from quake.sv_send import build_datagram
    sv = _boot()
    sv.create_baseline()
    w = MsgWriter()
    build_datagram(sv, w)
    assert w.data, "no datagram bytes written"
    assert w.data[0] == P.svc_time, "datagram must start with svc_time (7)"


def test_clientdata_roundtrips_health():
    from quake.msg import MsgWriter, MsgReader
    from quake.sv_send import write_clientdata_to_message
    from quake.cl_parse import ClientState
    sv = _boot()
    w = MsgWriter()
    write_clientdata_to_message(sv, w)       # writes svc_clientdata + payload
    r = MsgReader(bytes(w.data))
    assert r.byte() == P.svc_clientdata
    cl = ClientState()
    cl.parse_clientdata(r)
    assert cl.stats[0] == sv.player_health()  # STAT_HEALTH
    assert r.at_end, "clientdata writer/parser byte counts disagree"


def test_build_datagram_parses_into_cl():
    from quake.msg import MsgWriter, MsgReader
    from quake.sv_send import build_datagram, write_serverinfo
    from quake.cl_parse import ClientState
    sv = _boot()
    sv.create_baseline()
    cl = ClientState()
    # signon first: precache lists + baselines so the client can resolve models
    sw = MsgWriter(); write_serverinfo(sv, sw)
    cl.parse_message(MsgReader(bytes(sw.data)))
    assert cl.model_precache[1].endswith(".bsp")      # world model
    # then a frame datagram
    w = MsgWriter(); build_datagram(sv, w)
    cl.parse_message(MsgReader(bytes(w.data)))
    assert any(e and e.model for e in cl.entities), "no entity linked"


if __name__ == "__main__":
    test_baselines_snapshot_spawned_entities()
    test_write_entities_emits_parseable_updates()
    test_clientdata_writes_svc_id()
    test_serverinfo_writes_svc_id()
    test_build_datagram_writes_svc_time()
    test_clientdata_roundtrips_health()
    test_build_datagram_parses_into_cl()
    print("OK")

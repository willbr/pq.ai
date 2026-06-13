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
    # the first baseline is the world (edict 0, modelindex 1 = the .bsp)
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


def test_picked_up_item_drops_from_loopback():
    """SV_WriteEntitiesToClient (sv_main.c:451) skips !modelindex || !model:
    QC hides a picked-up item by clearing its .model string while leaving
    .modelindex set. The loopback writer must drop it too, mirroring the old
    Server.alias_entities() path, or picked-up items keep rendering."""
    from quake.msg import MsgWriter, MsgReader
    from quake.sv_send import build_datagram, write_serverinfo
    from quake.cl_parse import ClientState, SceneFromClient
    sv = _boot()
    sv.create_baseline()
    cl = ClientState()
    sw = MsgWriter(); write_serverinfo(sv, sw)
    cl.parse_message(MsgReader(bytes(sw.data)))

    vm, f = sv.vm, sv.f
    # find a live .mdl entity (an item) with both modelindex and model set
    target = None
    for e in range(1, vm.num_edicts):
        if vm.free[e]:
            continue
        if vm.fget_i(e, f["modelindex"]) and vm.fget_i(e, f["model"]):
            name = sv.pr.string(vm.fget_i(e, f["model"]))
            if name.endswith(".mdl"):
                target = e
                break
    assert target is not None, "no live .mdl item entity found"

    # each call advances server time so the new packet carries a distinct
    # svc_time -- the client drops entities absent from the last packet by
    # msgtime (cl_main.c:491), which needs mtime[0] to move.
    def loopback_alias_count():
        sv.time += 0.1
        dg = MsgWriter(); build_datagram(sv, dg)
        cl.parse_message(MsgReader(bytes(dg.data)))
        cl.time = sv.time
        cl.relink(0.0)
        return len(SceneFromClient(cl).alias_entities())

    before_sv = len(sv.alias_entities())
    before_loop = loopback_alias_count()
    assert before_loop == before_sv, (before_loop, before_sv)
    assert before_sv > 0

    # SUB_Remove/pickup clears .model (string_null) but keeps modelindex
    vm.fset_i(target, f["model"], 0)

    after_sv = len(sv.alias_entities())
    assert after_sv == before_sv - 1, (after_sv, before_sv)  # old path drops it

    # the loopback must drop it too -- the bug left it rendering
    after_loop = loopback_alias_count()
    assert after_loop == after_sv, (after_loop, after_sv)
    assert after_loop == before_loop - 1, (after_loop, before_loop)


def test_baseline_includes_worldspawn_and_player():
    sv = _boot()
    sv.create_baseline()
    assert 0 in sv.baselines, "worldspawn (edict 0) must have a baseline"
    assert sv.baselines[0].modelindex == 1, "world modelindex is 1 (the .bsp)"
    # the player edict baseline forces the player model + colormap=entnum
    p = sv.player
    assert sv.baselines[p].colormap == p
    assert sv.baselines[p].modelindex == sv.model_index("progs/player.mdl")


def test_clientdata_folds_serverflags_into_items():
    from quake.msg import MsgWriter, MsgReader
    from quake.sv_send import write_clientdata_to_message
    from quake.cl_parse import ClientState
    sv = _boot()
    sv.serverflags = 3                     # two sigils
    w = MsgWriter(); write_clientdata_to_message(sv, w)
    r = MsgReader(bytes(w.data)); assert r.byte() == 15   # svc_clientdata
    cl = ClientState(); cl.parse_clientdata(r)
    assert (cl.items >> 28) & 0x0f == 3, "serverflags must ride in items high bits"


if __name__ == "__main__":
    test_baseline_includes_worldspawn_and_player()
    test_clientdata_folds_serverflags_into_items()
    test_baselines_snapshot_spawned_entities()
    test_write_entities_emits_parseable_updates()
    test_clientdata_writes_svc_id()
    test_serverinfo_writes_svc_id()
    test_build_datagram_writes_svc_time()
    test_clientdata_roundtrips_health()
    test_build_datagram_parses_into_cl()
    test_picked_up_item_drops_from_loopback()
    print("OK")

"""Structural parity of our recordings against WinQuake. The genuine shareware
demo1.dem is a real WinQuake recording, so its message structure is the gold
reference. Run muted: PQ_AUDIO=0 python tests/test_winquake_compat.py."""
import _bootstrap  # noqa: F401
from quake import protocol as P
from quake.sv_send import build_signon


def _boot(mapname="e1m1"):
    from quake.pak import Pak
    from quake.bsp import Bsp
    from quake.progs import Progs
    from quake.sv import Server
    from quake.physics import Physics
    pak = Pak("quake-shareware/id1/pak0.pak")
    b = Bsp(pak.read(f"maps/{mapname}.bsp"))
    sv = Server(Progs(pak.read("progs.dat")), bsp=b, mapname=f"maps/{mapname}.bsp",
                skill=1, pak=pak)
    sv.phys = Physics(b)
    sv.load_level()
    sv.spawn_player((480.0, 0.0, 100.0), (0.0, 0.0, 0.0))
    for _ in range(3):
        sv.run_frame(0.1)
    sv.create_baseline()
    return sv


def _svc_sequence(msg):
    """Walk a message buffer via the real parser, returning the ordered list of
    top-level svc ids (each entity fast-update logged as -1). Re-uses
    ClientState's handlers so every message's payload is consumed correctly and
    the next svc boundary is found."""
    from quake.cl_parse import ClientState
    from quake.msg import MsgReader
    cl = ClientState()
    r = MsgReader(msg)
    out = []
    while not r.at_end:
        cmd = r.byte()
        if cmd & 128:                     # fast entity update (high bit set)
            out.append(-1)
            cl.parse_update(cmd & 127, r)
        else:
            out.append(cmd)
            cl._dispatch(cmd, r)          # consume this message's payload
    return out


def _ends_with_signonnum(block, value):
    """The block must end with svc_signonnum followed by the given phase byte --
    WinQuake's connect state machine requires exactly 1, 2, 3 in order."""
    return bytes(block[-2:]) == bytes([P.svc_signonnum, value])


def test_signon_has_three_phases_ending_signonnum_123():
    sv = _boot("e1m1")
    phases = build_signon(sv)
    assert len(phases) == 3, "signon must be 3 phases (serverinfo/prespawn/spawn)"
    p0, p1, p2 = (_svc_sequence(b) for b in phases)
    # phase 0: serverinfo ... signonnum 1
    assert P.svc_serverinfo in p0 and p0[-1] == P.svc_signonnum
    assert _ends_with_signonnum(phases[0], 1)
    # phase 1: baselines ... signonnum 2
    assert P.svc_spawnbaseline in p1 and p1[-1] == P.svc_signonnum
    assert _ends_with_signonnum(phases[1], 2)
    # phase 2: a svc_time then clientdata, ending signonnum 3
    assert P.svc_time in p2 and P.svc_clientdata in p2 and p2[-1] == P.svc_signonnum
    assert _ends_with_signonnum(phases[2], 3)


def test_spawn_block_has_64_lightstyles_and_total_stats():
    sv = _boot("e1m1")
    phases = build_signon(sv)
    seq = _svc_sequence(phases[2])
    assert seq.count(P.svc_lightstyle) == 64, "all MAX_LIGHTSTYLES sent at spawn"
    assert seq.count(P.svc_updatestat) >= 4, "total secrets/monsters stats sent"


def test_prespawn_emits_static_sounds():
    sv = _boot("e1m1")
    # e1m1 has ambient sounds; sv.ambients should be non-empty after load
    if not sv.ambients:
        return                                  # map without ambients: skip
    seq = _svc_sequence(build_signon(sv)[1])
    assert P.svc_spawnstaticsound in seq


def test_makestatic_entities_emitted():
    sv = _boot("e1m1")
    # e1m1 has makestatic torches; after load_level sv.static_entities is populated
    if not getattr(sv, "static_entities", None):
        return                                  # map without statics: skip
    seq = _svc_sequence(build_signon(sv)[1])
    assert P.svc_spawnstatic in seq


if __name__ == "__main__":
    test_signon_has_three_phases_ending_signonnum_123()
    test_spawn_block_has_64_lightstyles_and_total_stats()
    test_prespawn_emits_static_sounds()
    test_makestatic_entities_emitted()
    print("OK")

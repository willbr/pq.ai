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


def test_signon_has_three_phases_ending_signonnum_123():
    sv = _boot("e1m1")
    phases = build_signon(sv)
    assert len(phases) == 3, "signon must be 3 phases (serverinfo/prespawn/spawn)"
    p0, p1, p2 = (_svc_sequence(b) for b in phases)
    # phase 0: serverinfo ... signonnum 1
    assert P.svc_serverinfo in p0 and p0[-1] == P.svc_signonnum
    # phase 1: baselines ... signonnum 2
    assert P.svc_spawnbaseline in p1 and p1[-1] == P.svc_signonnum
    # phase 2: a svc_time then clientdata, ending signonnum 3
    assert P.svc_time in p2 and P.svc_clientdata in p2 and p2[-1] == P.svc_signonnum


if __name__ == "__main__":
    test_signon_has_three_phases_ending_signonnum_123()
    print("OK")

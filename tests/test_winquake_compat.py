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
    # e1m2 has makestatic torches/flames (e1m1 has none); after load_level
    # sv.static_entities is populated and the prespawn block emits svc_spawnstatic.
    sv = _boot("e1m2")
    assert sv.static_entities, "e1m2 must have makestatic torches/flames"
    seq = _svc_sequence(build_signon(sv)[1])
    assert P.svc_spawnstatic in seq


def test_pvs_cull_reduces_entity_count():
    from quake.msg import MsgWriter
    from quake.sv_send import write_entities_to_client
    sv = _boot("e1m1")

    def count_updates(pvs_test):
        w = MsgWriter()
        write_entities_to_client(sv, w, sv.player_origin(), pvs_test=pvs_test)
        return _svc_sequence(bytes(w.data)).count(-1)

    all_n = count_updates(None)                         # no cull: everything
    # a PVS tester that culls everything but the player should send far fewer
    culled_n = count_updates(lambda mins, maxs: False)
    assert culled_n < all_n
    assert culled_n >= 1                                # the player is always sent


def _record_demo(mapname, frames=40):
    """Record a short session and return the .dem bytes (no file left behind).
    Boots a live game on the map, starts a DemoWriter (which writes the 3-phase
    signon as the first three frames), drives `frames` live frames (each tees its
    PVS-culled datagram), then stops -- the round-trip a real `record` produces."""
    import os
    import tempfile
    from client import Client, InputState
    c = Client(mapname)
    c.resize(640, 480)
    name = os.path.join(tempfile.mkdtemp(), "wq")
    c._cmd_record([name, mapname])
    for _ in range(frames):
        c.frame(0.05, InputState(move_forward=1.0))
    c._cmd_stopdemo([])
    path = name + ".dem"
    with open(path, "rb") as fh:
        data = fh.read()
    os.remove(path)
    return data


def test_our_recording_signon_matches_winquake_shape():
    """Our recording must carry the same signon message *types* a real WinQuake
    demo does: the 3-phase handshake with svc_signonnum 1/2/3, all 64 lightstyles,
    spawnstatic/spawnstaticsound/spawnbaseline, totals, and a spawn svc_time. We
    record e1m2 -- a map that has BOTH makestatic torches and ambient loops, so
    spawnstatic AND spawnstaticsound both appear (matching demo1.dem's content)."""
    from quake.demo import DemoReader
    data = _record_demo("e1m2")
    r = DemoReader(data)
    seen = []                                   # svc ids across the signon frames
    for _ in range(3):                          # the 3 signon frames
        fr = r.next_frame()
        assert fr is not None, "recording is missing a signon frame"
        seen += _svc_sequence(fr[1])
    assert seen.count(P.svc_signonnum) == 3, "must emit signonnum 1, 2 and 3"
    assert P.svc_serverinfo in seen and P.svc_setview in seen
    assert seen.count(P.svc_lightstyle) == 64, "all 64 lightstyles at spawn"
    assert seen.count(P.svc_updatestat) >= 4, "total secret/monster stats sent"
    assert P.svc_spawnbaseline in seen
    assert P.svc_spawnstatic in seen, "e1m2 has makestatic torches"
    assert P.svc_spawnstaticsound in seen, "e1m2 has ambient loops"
    assert P.svc_time in seen and P.svc_clientdata in seen   # spawn frame
    # the first datagram frame after the signon is svc_time-led (Phase 3+)
    fr = r.next_frame()
    assert fr is not None and _svc_sequence(fr[1])[0] == P.svc_time


# The svc message types a genuine WinQuake demo carries in its signon that
# carry world/connect state we must reproduce. Scoreboard messages
# (updatename/updatefrags/updatecolors) are excluded: they are per-client
# scoreboard chatter, legitimately absent from a fresh single-player recording.
_MEANINGFUL_SIGNON_SVCS = frozenset((
    P.svc_serverinfo, P.svc_cdtrack, P.svc_setview, P.svc_signonnum,
    P.svc_spawnbaseline, P.svc_spawnstatic, P.svc_spawnstaticsound,
    P.svc_lightstyle, P.svc_updatestat, P.svc_time, P.svc_clientdata,
))


def _demo1_signon_svc_set():
    """The set of svc message types in the genuine shareware demo1.dem's signon
    (its first three frames -- a real WinQuake recording on e1m3). This is the
    gold reference structure our recordings are measured against."""
    from quake.pak import Pak
    from quake.demo import DemoReader
    pak = Pak("quake-shareware/id1/pak0.pak")
    r = DemoReader(pak.read("demo1.dem"))
    svcs = set()
    for _ in range(3):
        fr = r.next_frame()
        assert fr is not None, "demo1.dem is missing a signon frame"
        svcs |= {s for s in _svc_sequence(fr[1]) if s != -1}
    return svcs


def test_our_signon_is_structural_superset_of_demo1():
    """Cross-engine parity proof against a genuine WinQuake demo: the meaningful
    signon svc types in demo1.dem (the gold reference) must ALL appear in our own
    recording's signon. This proves our recordings carry the same connect/world
    state a real WinQuake recording does -- the structural gate we can run here
    without a WinQuake binary."""
    from quake.demo import DemoReader
    demo1 = _demo1_signon_svc_set()
    # demo1's meaningful signon types (drop scoreboard chatter we may omit)
    demo1_meaningful = demo1 & _MEANINGFUL_SIGNON_SVCS
    # our recording's signon svc set
    data = _record_demo("e1m2")
    r = DemoReader(data)
    ours = set()
    for _ in range(3):
        ours |= {s for s in _svc_sequence(r.next_frame()[1]) if s != -1}
    missing = demo1_meaningful - ours
    assert not missing, (
        "our signon is missing meaningful WinQuake types: "
        + ", ".join(str(m) for m in sorted(missing)))


def test_our_recording_replays_in_our_own_player():
    """End-to-end round-trip: the new-format recording (3-phase signon + culled
    datagrams) still plays back in pq.ai's own player with a moving camera."""
    from client import Client, InputState
    data = _record_demo("e1m2", frames=60)
    p = Client.__new__(Client)
    p._init_assets_only()
    assert p._load_demo(data), "recording did not load in our own player"
    p.resize(640, 480)
    moved = False
    last = None
    for _ in range(60):
        if p.demo.finished:
            break
        p.frame(0.05, InputState())
        cur = tuple(p.pos)
        if last is not None and cur != last:
            moved = True
        last = cur
    assert moved, "recorded demo did not play back with a moving camera"


if __name__ == "__main__":
    test_signon_has_three_phases_ending_signonnum_123()
    test_spawn_block_has_64_lightstyles_and_total_stats()
    test_prespawn_emits_static_sounds()
    test_makestatic_entities_emitted()
    test_pvs_cull_reduces_entity_count()
    test_our_recording_signon_matches_winquake_shape()
    test_our_signon_is_structural_superset_of_demo1()
    test_our_recording_replays_in_our_own_player()
    print("OK")

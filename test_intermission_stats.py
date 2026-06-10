"""Regression test: intermission exposes the end-of-level stats overlay.

Quake's Sbar_IntermissionOverlay draws three rows once intermission_running is
set: the completed time (minutes:seconds), secrets found / total, and monsters
killed / total. pq froze the camera at the spot but never surfaced these, so the
host had nothing to draw -- the intermission screen was blank.

This exercises Server.intermission_stats(), the seam the host reads to build the
overlay:
  - None during normal play.
  - Once the exit is reached, it reports the QC tallies (found_secrets/
    total_secrets, killed_monsters/total_monsters) and the frozen completion
    time (intermission_exittime - 5, the level time when the exit was hit).
"""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.progs import Progs
from quake.sv import Server

PAK = "quake-shareware/id1/pak0.pak"


def _boot():
    pak = Pak(PAK)
    sv = Server(Progs(pak.read("progs.dat")), bsp=Bsp(pak.read("maps/e1m1.bsp")),
                mapname="maps/e1m1.bsp", skill=1)
    sv.load_level()
    return sv


def _find_exit(sv):
    vm, f, p = sv.vm, sv.f, sv.pr
    for e in range(1, vm.num_edicts):
        if vm.free[e]:
            continue
        if p.string(vm.fget_i(e, f["classname"])) == "trigger_changelevel":
            return e
    raise AssertionError("no trigger_changelevel on e1m1")


def test_intermission_reports_stats():
    sv = _boot()
    vm, f = sv.vm, sv.f
    sv.spawn_player((100.0, 100.0, 100.0), (0.0, 0.0, 0.0))
    pl = sv.player

    # no stats outside intermission
    assert sv.intermission_stats() is None

    # e1m1 spawned its monster/secret totals at load
    total_monsters = int(sv.gget_f("total_monsters"))
    total_secrets = int(sv.gget_f("total_secrets"))
    assert total_monsters > 0, "e1m1 should have monsters"

    # advance the clock a little so the completed time isn't trivially zero
    while sv.time < 12.0:
        sv.run_frame(0.1)
    enter_time = sv.time

    # walk into the exit: fire its touch with the player as `other`
    exit_e = _find_exit(sv)
    tf = vm.fget_i(exit_e, f["touch"])
    sv.gset_i("self", exit_e)
    sv.gset_i("other", pl)
    sv.gset_f("time", sv.time)
    vm.execute(tf)

    # execute_changelevel is a scheduled think; a couple of frames lets it fire
    for _ in range(3):
        sv.run_frame(0.1)

    assert sv.intermission_active()
    st = sv.intermission_stats()
    assert st is not None, "intermission should report stats"

    assert st["total_monsters"] == total_monsters
    assert st["total_secrets"] == total_secrets
    assert st["monsters"] == int(sv.gget_f("killed_monsters"))
    assert st["secrets"] == int(sv.gget_f("found_secrets"))
    # killed/found never exceed the totals
    assert 0 <= st["monsters"] <= st["total_monsters"]
    assert 0 <= st["secrets"] <= st["total_secrets"]

    # completed time is frozen at the moment the exit was reached (exittime - 5),
    # which is the level time when changelevel fired -- right around enter_time.
    assert abs(st["time"] - enter_time) <= 1, \
        f"completed time {st['time']} should be ~{enter_time:.0f}s"


def test_client_renders_intermission_overlay():
    """The host turns intermission_stats() into Sbar_IntermissionOverlay's three
    centered rows. Drive Client.frame with intermission forced and assert the
    panel appears with the time formatted m:ss and the slash-separated tallies."""
    import client
    from client import Client, InputState

    c = Client("e1m1")
    sv = c.sv
    sv.gset_f("intermission_running", 1.0)
    sv.intermission_time = 83.0                 # 1:23
    sv.gset_f("found_secrets", 2.0); sv.gset_f("total_secrets", 4.0)
    sv.gset_f("killed_monsters", 15.0); sv.gset_f("total_monsters", 30.0)
    c.intermission = True

    rf = c.frame(0.05, InputState())
    panel = [o for o in rf.overlays if "LEVEL COMPLETE" in o[2]]
    assert panel, "intermission panel missing from overlays"
    x, y, text, rgb, anchor = panel[0]
    assert anchor == "center"
    assert "Time      1:23" in text
    assert "Secrets   2 / 4" in text
    assert "Kills     15 / 30" in text


if __name__ == "__main__":
    test_intermission_reports_stats()
    test_client_renders_intermission_overlay()
    print("OK")

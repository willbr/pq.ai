"""aim(entity, missilespeed) vertical autoaim (PF_aim, pr_cmds.c).

When the straight v_forward trace doesn't land on a damageable entity, aim()
scans every takedamage == DAMAGE_AIM entity inside the sv_aim cone (0.93 dot)
that the shooter can actually trace to, and returns a direction that keeps
v_forward's horizontal heading but pitches vertically onto the best target.
That's what lets the 1996 shotgun hit a soldier on a ledge without freelook.
"""

import math

from quake.pak import Pak
from quake.bsp import Bsp
from quake.physics import Physics
from quake.progs import Progs, OFS_PARM0, OFS_RETURN
from quake.sv import Server

PAK = "quake-shareware/id1/pak0.pak"


def _boot():
    pak = Pak(PAK)
    bsp = Bsp(pak.read("maps/e1m1.bsp"))
    sv = Server(Progs(pak.read("progs.dat")), bsp=bsp, mapname="maps/e1m1.bsp",
                skill=1, physics=Physics(bsp), pak=pak)
    sv.load_level()
    return sv


def _find_monsters(sv):
    out = []
    for e in range(1, sv.vm.num_edicts):
        if sv.vm.free[e]:
            continue
        cn = sv.pr.string(sv.vm.fget_i(e, sv.f["classname"]))
        if cn == "monster_army":
            sv.vm.fset_f(e, sv.f["nextthink"], 1.0e9)   # no AI during the test
            out.append(e)
    assert out, "no monster_army on e1m1 at skill 1"
    return out


def _call_aim(sv, ent, speed=1000.0):
    sv.vm.gi[OFS_PARM0] = ent
    sv.vm.gf[OFS_PARM0 + 3] = speed
    sv._pf_aim()
    return (sv.vm.gf[OFS_RETURN], sv.vm.gf[OFS_RETURN + 1],
            sv.vm.gf[OFS_RETURN + 2])


def test_aim_pitches_onto_visible_monster():
    sv = _boot()
    # let walkmonster_start_go run: it's a deferred think that sets the
    # monster's takedamage = DAMAGE_AIM, which aim() filters on
    for _ in range(10):
        sv.run_frame(0.1)
    f, vm = sv.f, sv.vm
    # stand the player 160 units from a soldier, raised 48 so a level shot
    # sails over its head, on a side where the aim ray (eye at spot+20 to the
    # bbox centre) reaches THAT soldier -- not a wall or a second soldier
    spot = fwd = mon = None
    for cand_mon in _find_monsters(sv):
        assert vm.fget_f(cand_mon, f["takedamage"]) == 2.0   # DAMAGE_AIM
        mx, my, mz = vm.fget_v(cand_mon, f["origin"])
        cz = mz + 0.5 * (vm.fget_v(cand_mon, f["mins"])[2]
                         + vm.fget_v(cand_mon, f["maxs"])[2])
        for dx, dy in ((160.0, 0.0), (-160.0, 0.0), (0.0, 160.0), (0.0, -160.0)):
            cand = (mx + dx, my + dy, mz + 48.0)
            eye = (cand[0], cand[1], cand[2] + 20.0)
            res = sv._move_trace(eye, (mx, my, cz), 0, 0)
            if res[5] == cand_mon and not res[4]:           # hit it, not startsolid
                spot, mon = cand, cand_mon
                n = math.hypot(dx, dy)
                fwd = (-dx / n, -dy / n, 0.0)
                break
        if spot:
            break
    assert spot is not None, "no soldier with a clear aim ray?"

    sv.spawn_player(spot, (0.0, 0.0, 0.0))
    sv.gset_v("v_forward", fwd)

    dx, dy, dz = _call_aim(sv, sv.player)

    assert dz < -0.1, f"aim did not pitch down onto the monster (z {dz:.3f})"
    # horizontal heading is preserved (vertical-only assist), vector normalised
    horiz = math.hypot(dx, dy)
    assert abs(dx / horiz - fwd[0]) < 1e-5 and abs(dy / horiz - fwd[1]) < 1e-5
    assert abs(math.sqrt(dx * dx + dy * dy + dz * dz) - 1.0) < 1e-5


def test_aim_returns_v_forward_with_no_target():
    sv = _boot()
    sv.spawn_player((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    sv.gset_v("v_forward", (0.0, 0.0, 1.0))     # straight up: nothing there
    assert _call_aim(sv, sv.player) == (0.0, 0.0, 1.0)


if __name__ == "__main__":
    test_aim_pitches_onto_visible_monster()
    test_aim_returns_v_forward_with_no_target()
    print("OK")

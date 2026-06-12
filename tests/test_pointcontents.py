"""pointcontents(vector) builtin (PF_pointcontents -> SV_PointContents).

QC uses it for the rules that depend on what a point is inside of: the
lightning gun discharging underwater (W_FireLightning), fish/scrag water
checks, teleporter destinations. The stub returned CONTENTS_EMPTY for
everything, so those rules silently never fired.
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

from quake.pak import Pak
from quake.bsp import Bsp
from quake.physics import Physics, CONTENTS_EMPTY, CONTENTS_SOLID, CONTENTS_WATER
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


def _pointcontents(sv, pt):
    """Call the builtin the way the VM does: vector in PARM0, float return."""
    sv.vm.gf[OFS_PARM0], sv.vm.gf[OFS_PARM0 + 1], sv.vm.gf[OFS_PARM0 + 2] = pt
    sv._pf_pointcontents()
    return int(sv.vm.gf[OFS_RETURN])


def test_pointcontents_reports_real_contents():
    sv = _boot()
    spawn, _yaw = sv.bsp.find_spawn()
    eye = (spawn[0], spawn[1], spawn[2] + 30.0)
    assert _pointcontents(sv, eye) == CONTENTS_EMPTY

    # far below the world is solid void
    assert _pointcontents(sv, (spawn[0], spawn[1], -20000.0)) == CONTENTS_SOLID

    # e1m1 (Slipgate Complex) has water and slime pools; find a liquid point
    # so the builtin's answer is exercised on one (W_FireLightning's
    # discharge check is `pointcontents(...) <= CONTENTS_WATER`)
    (x0, y0, z0), (x1, y1, z1) = sv.bsp.models[0]["mins"], sv.bsp.models[0]["maxs"]
    liquid = None
    for x in range(int(x0), int(x1), 96):
        for y in range(int(y0), int(y1), 96):
            for z in range(int(z0), int(z1), 64):
                c = sv.phys.point_contents_0((float(x), float(y), float(z)))
                if c <= CONTENTS_WATER and c >= -5:     # water/slime/lava
                    liquid = ((float(x), float(y), float(z)), c)
                    break
            if liquid:
                break
        if liquid:
            break
    assert liquid is not None, "no liquid found on e1m1?"
    pt, contents = liquid
    assert _pointcontents(sv, pt) == contents <= CONTENTS_WATER


if __name__ == "__main__":
    test_pointcontents_reports_real_contents()
    print("OK")

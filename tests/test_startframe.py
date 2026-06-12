"""QC StartFrame runs every server frame (SV_Physics, sv_phys.c).

id's StartFrame re-reads the teamplay and skill cvars into the QC globals and
bumps framecount -- it's how a mid-game `skill 3` actually takes effect at the
next level's spawns, and how mods hook per-frame logic.
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

from quake.pak import Pak
from quake.bsp import Bsp
from quake.progs import Progs
from quake.sv import Server

PAK = "quake-shareware/id1/pak0.pak"


def test_startframe_rereads_skill_cvar():
    pak = Pak(PAK)
    sv = Server(Progs(pak.read("progs.dat")),
                bsp=Bsp(pak.read("maps/e1m1.bsp")),
                mapname="maps/e1m1.bsp", skill=1)
    sv.load_level()
    assert sv.gget_f("skill") == 1.0

    sv.cvars["skill"] = 3.0          # console `skill 3` mid-game
    sv.run_frame(0.05)

    assert sv.gget_f("skill") == 3.0, "StartFrame did not re-read the skill cvar"


if __name__ == "__main__":
    test_startframe_rereads_skill_cvar()
    print("OK")

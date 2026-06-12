"""e1m3 ceiling crusher (func_train 235) vs. a player on the brush-model stairs.

The staircase at the top of e1m3 is built from brush-model entities (it raises
when its button is pressed); unraised, it's a flat brush-model floor at z~72.
The ceiling crusher descends onto it. SV_PushMove must crush the player against
that floor -- but the "is the shoved entity stuck?" test originally checked only
WORLD solid (the real floor is far below at z=-8), so the crusher shoved the
player straight DOWN through the brush stairs and they survived at -8 instead of
dying. SV_TestEntityPosition tests every solid (world + brush + box) bar the
pusher; with that, the player is pinned on the stairs and squished.

Also guards the interaction with the stuck-escape: a player genuinely crushed
between the roof and the brush stairs must NOT slip free through them (the escape
skips only box solids, keeping brush floors).
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

import os

os.environ.setdefault("PQ_AUDIO", "0")

import client
from client import Client, InputState


def test_crusher_kills_player_on_the_brush_stairs():
    c = Client("e1m3")
    c.resize(320, 240)
    vm, f = c.sv.vm, c.sv.f
    # stand at the top of the (unraised) stairs, under the descending crusher
    c.pos = [-699.0, -1255.0, 72.0]
    c.yaw = 0.0
    c.pitch = 0.0
    c.sv.update_player((c.pos[0], c.pos[1], c.pos[2]), (0.0, 0.0, 0.0))
    z0 = c.pos[2]
    min_z = z0
    died = False
    for _ in range(200):
        c.frame(0.1, InputState())
        min_z = min(min_z, c.pos[2])
        if c.sv.player_health() <= 0:
            died = True
            break
    assert died, f"crusher did not kill the player (hp {c.sv.player_health():.0f})"
    # and they must have been crushed on the stairs, not shoved far down through
    # them to the world floor (~-8) first
    assert min_z > z0 - 24.0, \
        f"player was pushed down through the brush stairs (z {z0:.0f} -> {min_z:.0f})"


if __name__ == "__main__":
    test_crusher_kills_player_on_the_brush_stairs()
    print("OK")

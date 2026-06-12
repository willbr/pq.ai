"""Leaf-gated ambient sound channels (S_UpdateAmbientSounds, snd_dma.c).

qbsp bakes per-leaf ambient volumes (water, sky, slime, lava) into the BSP;
each frame the mixer ramps two dedicated looping channels
(ambience/water1.wav, ambience/wind2.wav) toward the listener leaf's levels
at the ambient fade rate, so you hear water near water and wind under sky --
and silence in a quiet corridor, instead of ambients playing everywhere.
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

import os

os.environ.setdefault("PQ_AUDIO", "0")

from quake.pak import Pak
from quake.bsp import Bsp
from quake import snd

PAK = "quake-shareware/id1/pak0.pak"


def test_bsp_keeps_leaf_ambient_levels():
    bsp = Bsp(Pak(PAK).read("maps/e1m1.bsp"))
    assert len(bsp.leaf_ambients) == len(bsp.leafs)
    assert any(a[0] > 0 for a in bsp.leaf_ambients), "no water ambience baked?"
    assert any(a[1] > 0 for a in bsp.leaf_ambients), "no sky ambience baked?"


def _mixer_with_ambients():
    pak = Pak(PAK)
    m = snd.Mixer()
    m.ok = True
    for name in snd.AMBIENT_SOUNDS:
        m.precache(name, pak.read("sound/" + name))
    return m


def test_ambient_levels_ramp_toward_leaf_target():
    m = _mixer_with_ambients()
    m.update_ambients((255, 0), 0.1)        # in a watery leaf
    water = m._ambients[0]
    assert water is not None and 0 < water["vol"] < 0.3, \
        "water ambient must fade in, not snap"
    assert m._ambients[1] is None or m._ambients[1]["vol"] == 0.0
    for _ in range(20):
        m.update_ambients((255, 0), 0.1)
    assert abs(water["vol"] - 0.3) < 1e-6   # ambient_level cvar default
    for _ in range(30):
        m.update_ambients((0, 0), 0.1)      # walked away from the water
    assert water["vol"] == 0.0


def test_ambient_channels_survive_voice_stealing():
    m = _mixer_with_ambients()
    m.update_ambients((255, 255), 0.5)
    for i in range(snd.MAX_CHANNELS + 4):   # flood with one-shots
        m.start_sound(i + 10, 0, snd.AMBIENT_SOUNDS[0], 1.0, 0.0, None)
    assert m._ambients[0] in m.channels, "ambient channel was stolen"
    assert m._ambients[1] in m.channels


def test_channel_zero_never_overrides():
    m = _mixer_with_ambients()
    m.start_sound(5, 0, snd.AMBIENT_SOUNDS[0], 1.0, 0.0, None)
    m.start_sound(5, 0, snd.AMBIENT_SOUNDS[0], 1.0, 0.0, None)
    auto = [c for c in m.channels if c["ent"] == 5]
    assert len(auto) == 2, "channel 0 must never replace, only add"


if __name__ == "__main__":
    test_bsp_keeps_leaf_ambient_levels()
    test_ambient_levels_ramp_toward_leaf_target()
    test_ambient_channels_survive_voice_stealing()
    test_channel_zero_never_overrides()
    print("OK")

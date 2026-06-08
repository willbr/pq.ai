"""Regression test: animated/special surfaces -- sky, liquids, +N textures.

The textured z-buffer renderer drew every surface as a static, directionally-
shaded texture. Quake has three kinds of moving surface the renderer ignored:

  - sky*       -- the cloud layer scrolls.
  - *liquids   -- water/lava/slime/teleporters sine-warp ("turbulent"), and all
                  TEX_SPECIAL surfaces (no lightmap) draw full-bright.
  - +N frames  -- animated wall textures (buttons, screens) cycle at 5 Hz.

This checks the classification and the per-frame state that drive them. The
pixel-level warp/scroll is exercised by render_zbuffer running clean with a
time argument; the visual diff is camera-dependent and not asserted here.

Driven against the real shareware textures on e1m1.
"""

import math
from pak import Pak
from bsp import Bsp
from render import Renderer

PAK = "quake-shareware/id1/pak0.pak"


def _renderer():
    pak = Pak(PAK)
    pb = pak.read("gfx/palette.lmp")
    palette = [(pb[i * 3], pb[i * 3 + 1], pb[i * 3 + 2]) for i in range(256)]
    b = Bsp(pak.read("maps/e1m1.bsp"))
    return Bsp(pak.read("maps/e1m1.bsp")), Renderer(b, palette)


def test_classification():
    b, r = _renderer()
    names = [t[0] if t else "" for t in b.textures]
    sky = {names[mt] for mt in range(len(names)) if r.is_sky[mt]}
    turb = {names[mt] for mt in range(len(names)) if r.is_turb[mt]}
    # e1m1 ships sky4, *water0/*slime0/*teleport, and several +N chains.
    assert sky == {"sky4"}, sky
    assert turb == {"*water0", "*slime0", "*teleport"}, turb
    assert any(r.tex_anim[mt] is not None for mt in range(len(names))), "no +N chains"
    # a +N chain is ordered by frame digit and shared by all its frames
    chains = {tuple(r.tex_anim[mt]) for mt in range(len(names)) if r.tex_anim[mt]}
    for chain in chains:
        digits = [names[mt][1] for mt in chain]
        assert digits == sorted(digits), digits        # +0, +1, +2, ...
        for mt in chain:
            assert r.tex_anim[mt] == list(chain)        # every frame points at it


def test_special_faces_are_full_bright():
    b, r = _renderer()
    # every sky/turb face carries the 1x1 full-bright (255) luxel, not a shade
    specials = [fi for fi in range(len(r.face_verts))
                if r.face_sky[fi] or r.face_turb[fi]]
    assert specials, "no special faces found"
    for fi in specials:
        lmw, lmh, _, _, lux, has_real = r.face_lm[fi]
        assert not has_real and lmw == 1 and lmh == 1
        assert lux[0] == 255, (fi, lux[0])


def test_plus_textures_cycle_at_5hz():
    b, r = _renderer()
    fi = next(f for f in r.anim_faces if len(r.face_anim[f]) >= 4)
    frames = r.face_anim[fi]
    # 5 Hz: frame index = int(time*5) % len. Distinct times -> distinct frames.
    r._animate_surfaces(0.0)
    rgb0 = r.face_tex[fi][2]
    r._animate_surfaces(0.2)               # one frame later
    rgb1 = r.face_tex[fi][2]
    r._animate_surfaces(len(frames) / 5.0)  # exactly one full cycle from 0
    rgb_wrapped = r.face_tex[fi][2]
    assert rgb0 is not rgb1, "texture did not advance"
    assert rgb0 is rgb_wrapped, "cycle did not wrap to frame 0"


def test_render_runs_with_time():
    b, r = _renderer()
    org, yaw = b.find_spawn()
    eye = (org[0], org[1], org[2] + 22)
    # two ticks: must not raise, and must return a full-size framebuffer
    (fb0, w, h), _ = r.render_zbuffer(eye, yaw, 0.0, textured=True, time=0.0)
    (fb1, _, _), _ = r.render_zbuffer(eye, yaw, 0.0, textured=True, time=1.7)
    assert len(fb0) == w * h * 3 and len(fb1) == w * h * 3


if __name__ == "__main__":
    test_classification()
    test_special_faces_are_full_bright()
    test_plus_textures_cycle_at_5hz()
    test_render_runs_with_time()
    print("OK")

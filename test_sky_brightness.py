"""Sky must render UNLIT -- raw composited texels, no colormap row.

WinQuake's sky span fill writes the sky source byte straight to the
framebuffer (D_DrawSkyScans8, d_sky.c:125: `*pdest++ = r_skysource[...]`),
i.e. the raw palette index, never shaded through gfx/colormap.lmp. The
renderer used to push sky texels through colormap row 0 -- the 2x OVERBRIGHT
row (row 31 is identity) -- so the sky came out roughly twice as bright as
real Quake (faults.md: e1m1 252,858,-200 sky brightness).

Strategy: render a sky view twice -- once with the composited sky tile
replaced by a sentinel index to mask exactly which pixels the sky fill owns,
once normally -- then assert every sky pixel holds a raw tile index and the
sky's mean luminance matches the raw tile, not the overbright mapping.
"""
import os

os.environ.setdefault("PQ_AUDIO", "0")

from quake.pak import Pak
from quake.bsp import Bsp
from quake.render import Renderer

PAK = "quake-shareware/id1/pak0.pak"
SENTINEL = 251


def _boot():
    pak = Pak(PAK)
    pb = pak.read("gfx/palette.lmp")
    palette = [(pb[i * 3], pb[i * 3 + 1], pb[i * 3 + 2]) for i in range(256)]
    colormap = pak.read("gfx/colormap.lmp")[:64 * 256]
    b = Bsp(pak.read("maps/e1m1.bsp"))
    r = Renderer(b, palette, colormap)
    r.resize(800, 600)
    return r, palette, colormap


def test_sky_pixels_are_raw_unlit_texels():
    r, palette, colormap = _boot()
    # faults.md viewpoint, looking up at the sky over the slime gully
    eye = (252.0, 858.0, -178.0)
    styles = [256] * 64
    view = dict(textured=True, lightstyles=styles, time=0.5)

    # pass 1: sentinel sky tile -> which pixels does the sky fill own?
    tiles = r._make_sky(0.5)                    # build the real tiles first
    real = {mt: t for mt, t in tiles.items()}
    r._sky_tiles = {mt: (128, 128, bytes((SENTINEL,)) * (128 * 128))
                    for mt in real}
    r.sky_split = {}                            # freeze: _make_sky returns as-is
    (fb_mask, w, h), _ = r.render_zbuffer(eye, 90.0, -55.0, **view)
    sky_px = [i for i, p in enumerate(fb_mask) if p == SENTINEL]
    assert len(sky_px) > 2000, ("expected a big patch of sky", len(sky_px))

    # pass 2: real tiles -> the sky pixels must be raw tile indices
    r._sky_tiles = real
    (fb, _, _), _ = r.render_zbuffer(eye, 90.0, -55.0, **view)
    raw = set()
    for _mt, (_w, _h, tile) in real.items():
        raw |= set(tile)
    bad = sum(1 for i in sky_px if fb[i] not in raw)
    assert bad == 0, (f"{bad}/{len(sky_px)} sky pixels not raw sky texels "
                      "(shaded through the colormap?)")

    # and the brightness must match the raw tile, not the 2x overbright row 0
    lum = sum(sum(palette[fb[i]]) for i in sky_px) / (3 * len(sky_px))
    raw_lum = sum(sum(palette[p]) for _mt, (_w2, _h2, t) in real.items()
                  for p in t) / (3 * 128 * 128 * len(real))
    over_lum = sum(sum(palette[colormap[p]]) for _mt, (_w2, _h2, t) in real.items()
                   for p in t) / (3 * 128 * 128 * len(real))
    assert abs(lum - raw_lum) < 25.0, (lum, raw_lum)
    assert lum < over_lum - 50.0, ("sky still overbright", lum, over_lum)


if __name__ == "__main__":
    test_sky_pixels_are_raw_unlit_texels()
    print("OK")

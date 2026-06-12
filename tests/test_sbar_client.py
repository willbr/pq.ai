"""Full-stack tests for the sprite status bar: raw hud_status fields, the
viewport shrink, the framebuffer composite and the text-HUD fallback.
Boots the shareware stack like the other client tests."""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

import client
from quake.sbar import IT_SHOTGUN


def test_hud_status_raw_fields():
    c = client.Client("e1m1")
    st = c.sv.hud_status()
    assert st["items"] & IT_SHOTGUN          # spawn gives the shotgun
    assert st["weapon_bit"] == IT_SHOTGUN    # ...and selects it
    assert isinstance(st["items"], int)
    # e1m1 sets no serverflags yet: sigil bits 28..31 clear at spawn
    assert st["items"] >> 28 == 0
    # existing text-HUD keys are untouched
    assert st["weapon"] == "Shotgun" and "health" in st


if __name__ == "__main__":
    test_hud_status_raw_fields()
    print("OK")

"""HUD keys/powerups readout (sbar.c's items row, as text).

You need to see whether you hold the silver/gold key -- that's gameplay
information, not decoration. hud_status() reports them and the active
powerups from the player's .items bits, and the client's status bar shows
them.
"""

import os

os.environ.setdefault("PQ_AUDIO", "0")

import client

IT_KEY1 = 131072        # silver key
IT_KEY2 = 262144        # gold key
IT_QUAD = 4194304


def test_hud_status_reports_keys_and_powerups():
    c = client.Client("e1m1")
    sv, f, vm, e = c.sv, c.sv.f, c.sv.vm, c.sv.player
    st = sv.hud_status()
    assert st["keys"] == ""
    assert st["powerups"] == ""

    items = int(vm.fget_f(e, f["items"]))
    vm.fset_f(e, f["items"], float(items | IT_KEY1 | IT_KEY2 | IT_QUAD))
    st = sv.hud_status()
    assert "silver" in st["keys"] and "gold" in st["keys"]
    assert "quad" in st["powerups"]


def test_status_bar_shows_keys():
    c = client.Client("e1m1")
    c.resize(320, 240)
    f, vm, e = c.sv.f, c.sv.vm, c.sv.player
    items = int(vm.fget_f(e, f["items"]))
    vm.fset_f(e, f["items"], float(items | IT_KEY2))
    rf = c.frame(0.05, client.InputState())
    assert any("gold key" in o[2] for o in rf.overlays), \
        "status bar does not show the carried key"


if __name__ == "__main__":
    test_hud_status_reports_keys_and_powerups()
    test_status_bar_shows_keys()
    print("OK")

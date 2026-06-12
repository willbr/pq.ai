"""Single-player pause: with the Escape menu or console open, the server does
not tick (WinQuake host.c Host_ServerFrame: "always pause in single player if
in console or menus") and the player doesn't move; closing it resumes. Boots
the real shareware stack like the other client tests."""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

import client


def _boot():
    c = client.Client("e1m1")
    c.resize(400, 300)
    c.mode = "wire"
    return c


def test_menu_open_pauses_server_and_player():
    c = _boot()
    c.menu.active = True
    t0 = c.sv.time
    pos0 = tuple(c.pos)
    for _ in range(5):
        rf = c.frame(0.05, client.InputState(move_forward=1.0))
    assert c.sv.time == t0, "server ticked while the menu was open"
    assert tuple(c.pos) == pos0, "player moved while the menu was open"
    assert rf.menu is not None       # the menu still renders while paused


def test_console_open_pauses_server():
    c = _boot()
    c.con.active = True
    t0 = c.sv.time
    for _ in range(5):
        c.frame(0.05, client.InputState())
    assert c.sv.time == t0, "server ticked while the console was open"


def test_closing_menu_resumes():
    c = _boot()
    c.menu.active = True
    c.frame(0.05, client.InputState())
    c.menu.active = False
    t0 = c.sv.time
    c.frame(0.05, client.InputState())
    assert c.sv.time > t0, "server did not resume after the menu closed"


if __name__ == "__main__":
    test_menu_open_pauses_server_and_player()
    test_console_open_pauses_server()
    test_closing_menu_resumes()
    print("OK")

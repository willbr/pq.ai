"""Tests for the pure overlay-menu state machine (quake/menu.py): navigation,
choice cycling firing its callback, action items firing on Enter, and the
view() the frontend draws. No boot -- the menu is pure stdlib."""

from quake.menu import Menu, ChoiceItem, ActionItem


def _menu():
    picked = []
    quit_flag = []
    res = ChoiceItem("Resolution",
                     [("240x160", (240, 160)), ("320x240", (320, 240)),
                      ("640x480", (640, 480))],
                     index=1, on_select=picked.append)
    back = ActionItem("Back", lambda: picked.append("back"))
    quit_item = ActionItem("Quit", lambda: quit_flag.append(True))
    return Menu("VIDEO OPTIONS", [res, back, quit_item]), picked, quit_flag


def test_navigation_wraps():
    m, _, _ = _menu()
    assert m.selected == 0
    m.key_up()                       # wraps to last
    assert m.selected == 2
    m.key_down()                     # wraps back to first
    assert m.selected == 0
    m.key_down()
    assert m.selected == 1


def test_choice_cycles_and_fires_on_select():
    m, picked, _ = _menu()
    # selected is the Resolution item (index 0), starting on 320x240 (option 1)
    m.key_right()
    assert m.items[0].index == 2 and picked[-1] == (640, 480)
    m.key_right()                    # wraps to first option
    assert m.items[0].index == 0 and picked[-1] == (240, 160)
    m.key_left()                     # wraps to last option
    assert m.items[0].index == 2 and picked[-1] == (640, 480)


def test_action_item_fires_on_enter():
    m, _, quit_flag = _menu()
    m.selected = 2                   # Quit
    m.key_enter()
    assert quit_flag == [True]


def test_escape_closes():
    m, _, _ = _menu()
    m.active = True
    m.key_escape()
    assert m.active is False


def test_view_reports_rows_and_selection():
    m, _, _ = _menu()
    m.selected = 1
    title, rows = m.view()
    assert title == "VIDEO OPTIONS"
    assert rows[0] == ("Resolution", "320x240", False)
    assert rows[1] == ("Back", "", True)
    assert rows[2] == ("Quit", "", False)


if __name__ == "__main__":
    test_navigation_wraps()
    test_choice_cycles_and_fires_on_select()
    test_action_item_fires_on_enter()
    test_escape_closes()
    test_view_reports_rows_and_selection()
    print("OK")

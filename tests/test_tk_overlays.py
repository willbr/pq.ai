"""tkinter frontend console/menu key routing.

main.py's route_console_key / route_menu_key map tkinter key events (keysym +
char) onto the pure Console / Menu state machines -- the tkinter counterpart to
win_gdi._console_key / _menu_key. Pure logic test: no Tk window, no pak, no
shareware data; drives real (pure) Console/Menu objects directly.
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

from main import route_console_key, route_menu_key
from quake.console import Console
from quake.menu import Menu, ChoiceItem, ActionItem


def _type(con, text):
    """Feed printable text to the console as tkinter would (keysym==char for
    letters, 'space' for the spacebar)."""
    for ch in text:
        route_console_key(con, "space" if ch == " " else ch, ch)


def test_console_typing_and_cursor():
    con = Console()
    _type(con, "map e1m1")
    assert con.input == "map e1m1", con.input
    assert con.cursor == len("map e1m1")


def test_console_named_keys_not_typed_as_text():
    # named editor keys must drive the editor, never insert their keysym as text
    con = Console()
    _type(con, "ab")
    route_console_key(con, "Left", "")
    route_console_key(con, "BackSpace", "\x08")    # deletes 'a', cursor before 'b'
    assert con.input == "b", con.input


def test_console_enter_executes_command():
    con = Console()
    hits = []
    con.register_command("ping", lambda a: hits.append(list(a)))
    _type(con, "ping pong")
    route_console_key(con, "Return", "\r")
    assert hits == [["pong"]], hits
    assert con.input == "" and con.cursor == 0


def test_console_escape_closes():
    con = Console()
    con.active = True
    handled = route_console_key(con, "Escape", "\x1b")
    assert handled is True and con.active is False


def test_console_history_recall():
    con = Console()
    _type(con, "one")
    route_console_key(con, "Return", "\r")
    route_console_key(con, "Up", "")
    assert con.input == "one", con.input


def test_console_swallows_everything():
    # every key while the console is open returns True (game must not see it)
    con = Console()
    for keysym, char in [("w", "w"), ("Tab", "\t"), ("Prior", ""), ("F", "F")]:
        assert route_console_key(con, keysym, char) is True


def test_menu_routing_cycle_and_activate():
    log = []
    m = Menu("T", [ChoiceItem("R", [("a", 1), ("b", 2)], 0, log.append),
                   ActionItem("Q", lambda: log.append("q"))])
    route_menu_key(m, "Right")       # cycle the choice -> value 2
    route_menu_key(m, "Down")        # move selection to Quit
    route_menu_key(m, "Return")      # activate Quit
    assert log == [2, "q"], log


def test_menu_escape_closes():
    m = Menu("T", [ActionItem("Q", lambda: None)])
    m.active = True
    handled = route_menu_key(m, "Escape")
    assert handled is True and m.active is False


def test_all():
    test_console_typing_and_cursor()
    test_console_named_keys_not_typed_as_text()
    test_console_enter_executes_command()
    test_console_escape_closes()
    test_console_history_recall()
    test_console_swallows_everything()
    test_menu_routing_cycle_and_activate()
    test_menu_escape_closes()


if __name__ == "__main__":
    test_all()
    print("OK")

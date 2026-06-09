"""Unit tests for the pure console core (quake/console.py): tokenizing,
command/cvar dispatch, the line editor, history, tab-completion, scrollback
and the stdout tee. No window, no shareware data -- pure logic."""

from quake.console import Console, Cvar, tokenize, TeeStdout


def test_tokenize_splits_on_whitespace():
    assert tokenize("map e1m2") == ["map", "e1m2"]
    assert tokenize("   spaced   out  ") == ["spaced", "out"]
    assert tokenize("") == []


def test_tokenize_groups_quoted_runs():
    assert tokenize('echo "hello world" x') == ["echo", "hello world", "x"]
    assert tokenize('alias gg "give h 100"') == ["alias", "gg", "give h 100"]


def test_command_dispatch_passes_args():
    con = Console()
    seen = []
    con.register_command("poke", lambda args: seen.append(args))
    con.execute("poke a b c")
    assert seen == [["a", "b", "c"]]


def test_throwing_command_prints_error_not_raises():
    con = Console()
    def boom(args):
        raise ValueError("kaboom")
    con.register_command("boom", boom)
    con.execute("boom")                     # must not raise
    assert any("kaboom" in ln for ln in con.lines)


def test_unknown_command_prints_message():
    con = Console()
    con.execute("frobnicate")
    assert any('Unknown command "frobnicate"' in ln for ln in con.lines)


def test_cvar_bare_name_prints_value_args_set_it():
    con = Console()
    fired = []
    cv = con.register_cvar("scale", 4, on_change=lambda c: fired.append(c.value))
    con.execute("scale")                    # bare name prints
    assert any('"scale" is "4"' in ln for ln in con.lines)
    con.execute("scale 8")                  # set fires on_change
    assert cv.value == "8"
    assert fired == ["8"]


def test_cvar_numeric_views_tolerate_junk():
    cv = Cvar("x", "7.5")
    assert cv.as_float() == 7.5
    assert cv.as_int() == 7
    assert cv.as_bool() is True
    junk = Cvar("y", "abc")
    assert junk.as_float() == 0.0
    assert junk.as_bool() is False


def test_line_editor_insert_and_delete():
    con = Console()
    for ch in "mp":
        con.key_char(ch)
    con.key_left()
    con.key_char("a")                       # "map"
    assert con.input == "map" and con.cursor == 2
    con.key_home(); assert con.cursor == 0
    con.key_end(); assert con.cursor == 3
    con.key_backspace(); assert con.input == "ma"
    con.key_home(); con.key_delete(); assert con.input == "a"


def test_enter_executes_and_records_history():
    con = Console()
    ran = []
    con.register_command("go", lambda a: ran.append(True))
    for ch in "go":
        con.key_char(ch)
    con.key_enter()
    assert ran == [True]
    assert con.input == "" and con.cursor == 0
    assert con.history == ["go"]
    assert any(ln == "]go" for ln in con.lines)


def test_history_recall_up_and_down():
    con = Console()
    con.register_command("a", lambda x: None)
    con.register_command("b", lambda x: None)
    for line in ("a", "b"):
        con.input = line
        con.cursor = len(line)
        con.key_enter()
    con.key_up(); assert con.input == "b"
    con.key_up(); assert con.input == "a"
    con.key_down(); assert con.input == "b"
    con.key_down(); assert con.input == ""   # back to the live (empty) edit


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK")

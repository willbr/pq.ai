"""Unit tests for the pure console core (quake/console.py): tokenizing,
command/cvar dispatch, the line editor, history, tab-completion, scrollback
and the stdout tee. No window, no shareware data -- pure logic."""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

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


def test_key_char_filters_control_and_multichar():
    con = Console()
    con.key_char("\n")          # control char -> ignored
    con.key_char("\x7f")        # DEL -> ignored
    con.key_char("ab")          # multi-char -> ignored
    con.key_char("\t")          # tab (control) -> ignored
    assert con.input == "" and con.cursor == 0
    con.key_char("x")           # a real char still works
    assert con.input == "x"


def test_recall_then_edit_reanchors_history():
    con = Console()
    con.register_command("map", lambda a: None)
    con.input = "map e1m1"; con.cursor = len(con.input)
    con.key_enter()
    con.key_up()                        # recall "map e1m1"
    assert con.input == "map e1m1"
    con.key_char("x")                   # edit it -> "map e1m1x"
    con.key_enter()
    assert con.history[-1] == "map e1m1x"
    assert con.hist_pos == len(con.history)
    con.key_up()                        # next recall returns the edited line
    assert con.input == "map e1m1x"


def test_key_up_with_empty_history_is_noop():
    con = Console()
    con.key_up()
    assert con.input == "" and con.cursor == 0


def test_tab_unique_prefix_completes():
    con = Console()
    con.register_command("noclip", lambda a: None)
    con.input = "nocl"; con.cursor = 4
    con.key_tab()
    assert con.input == "noclip "


def test_tab_ambiguous_lists_and_fills_common_prefix():
    con = Console()
    con.register_command("map", lambda a: None)
    con.register_command("material", lambda a: None)
    con.input = "ma"; con.cursor = 2
    con.key_tab()
    assert con.input == "ma"                 # common prefix is "ma" itself
    assert any("map" in ln and "material" in ln for ln in con.lines)


def test_tab_no_match_is_noop():
    con = Console()
    con.input = "zzz"; con.cursor = 3
    con.key_tab()
    assert con.input == "zzz"


def test_alias_expands():
    con = Console()
    got = []
    con.register_command("give", lambda a: got.append(a))
    con.register_alias("gg", "give h 100")
    con.execute("gg")
    assert got == [["h", "100"]]


def test_scrollback_caps_and_print_resets_scroll():
    con = Console()
    con.scroll = 5
    con.print("hello")
    assert con.scroll == 0
    for i in range(Console.MAX_LINES + 50):
        con.print(str(i))
    assert len(con.lines) == Console.MAX_LINES


def test_paging_clamps():
    con = Console()
    for i in range(100):
        con.print(str(i))
    con.key_pageup()
    assert con.scroll == Console.PAGE
    for _ in range(1000):
        con.key_pageup()
    assert con.scroll <= len(con.lines) - 1
    for _ in range(1000):
        con.key_pagedown()
    assert con.scroll == 0


def test_view_lines_returns_tail():
    con = Console(width=80)
    for i in range(10):
        con.print(f"line{i}")
    assert con.view_lines(3) == ["line7", "line8", "line9"]


def test_print_word_wraps_to_width():
    con = Console(width=10)
    con.print("aaaa bbbb cccc dddd")
    assert all(len(ln) <= 10 for ln in con.lines)
    assert len(con.lines) >= 2


def test_tee_stdout_forwards_complete_lines():
    import io
    sink = []
    real = io.StringIO()
    tee = TeeStdout(real, sink.append)
    tee.write("partial")
    assert sink == []                        # no newline yet
    tee.write(" line\nsecond\n")
    assert sink == ["partial line", "second"]
    assert real.getvalue() == "partial line\nsecond\n"


def test_view_lines_clamps_scroll_to_viewport():
    con = Console(width=80)
    for i in range(5):
        con.print(str(i))
    con.scroll = 999                         # absurd over-scroll
    # a height-3 viewport pins the oldest 3 lines and clamps scroll to total-n
    assert con.view_lines(3) == ["0", "1", "2"]
    assert con.scroll == 2                   # 5 lines - 3 viewport


def test_view_lines_empty_buffer():
    con = Console()
    assert con.view_lines(3) == []
    assert con.scroll == 0


def test_tee_stdout_only_forwards_from_owning_thread():
    import io
    import threading
    sink = []
    real = io.StringIO()
    tee = TeeStdout(real, sink.append)
    # a write from another thread reaches the stream but NOT the console sink
    t = threading.Thread(target=lambda: tee.write("bg line\n"))
    t.start()
    t.join()
    assert sink == []
    assert "bg line\n" in real.getvalue()
    # the owning thread (this one) still mirrors complete lines to the sink
    tee.write("fg line\n")
    assert sink == ["fg line"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK")

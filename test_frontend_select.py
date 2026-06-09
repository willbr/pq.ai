"""Tests for main.select_frontend: which frontend + map the CLI args select."""
import main


def test_default_on_windows_is_gdi():
    assert main.select_frontend(["e1m1"], "win32") == ("gdi", "e1m1")


def test_tk_flag_forces_tk_on_windows():
    assert main.select_frontend(["--tk", "e1m1"], "win32") == ("tk", "e1m1")
    assert main.select_frontend(["e1m1", "--tk"], "win32") == ("tk", "e1m1")


def test_non_windows_is_always_tk():
    assert main.select_frontend(["e1m1"], "darwin") == ("tk", "e1m1")
    assert main.select_frontend(["--tk", "e1m1"], "linux") == ("tk", "e1m1")


def test_default_map_when_none_given():
    assert main.select_frontend([], "win32") == ("gdi", "e1m1")
    assert main.select_frontend(["--tk"], "darwin") == ("tk", "e1m1")


if __name__ == "__main__":
    test_default_on_windows_is_gdi()
    test_tk_flag_forces_tk_on_windows()
    test_non_windows_is_always_tk()
    test_default_map_when_none_given()
    print("OK")

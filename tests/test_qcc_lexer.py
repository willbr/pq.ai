"""Unit tests for quake/qcc lexer (ports pr_lex.c PR_Lex)."""
import _bootstrap  # noqa: F401

from quake.qcc.lexer import Lexer, TT_NAME, TT_PUNCT, TT_IMMEDIATE, TT_EOF
from quake.qcc.types import TypeTable, ev_float, ev_string, ev_vector


def toks(src):
    lx = Lexer(src, "t.qc", TypeTable())
    out = []
    while True:
        lx.next()
        if lx.token_type == TT_EOF:
            break
        out.append((lx.token_type, lx.token))
    return out


def test_names_and_punct():
    assert toks("void foo;") == [
        (TT_NAME, "void"), (TT_NAME, "foo"), (TT_PUNCT, ";")]


def test_maximal_munch():
    assert toks("a <= b") == [
        (TT_NAME, "a"), (TT_PUNCT, "<="), (TT_NAME, "b")]


def test_comments_skipped():
    assert toks("a // gone\n b /* also gone */ c") == [
        (TT_NAME, "a"), (TT_NAME, "b"), (TT_NAME, "c")]


def test_float_immediate():
    lx = Lexer("3.5", "t.qc", TypeTable())
    lx.next()
    assert lx.token_type == TT_IMMEDIATE
    assert lx.immediate_type.type == ev_float
    assert lx.immediate == 3.5


def test_negative_immediate_gotcha():
    out = toks("a-5")
    assert out[0] == (TT_NAME, "a")
    assert out[1][0] == TT_IMMEDIATE


def test_string_escapes():
    lx = Lexer(r'"hi\nthere"', "t.qc", TypeTable())
    lx.next()
    assert lx.immediate_type.type == ev_string
    assert lx.immediate_string == "hi\nthere"


def test_vector_immediate():
    lx = Lexer("'1 -2 3.5'", "t.qc", TypeTable())
    lx.next()
    assert lx.immediate_type.type == ev_vector
    assert lx.immediate == (1.0, -2.0, 3.5)


def test_frame_macros():
    out = toks("$frame walk1 walk2\n $walk2")
    assert out[-1][0] == TT_IMMEDIATE
    lx = Lexer("$frame walk1 walk2\n $walk2", "t.qc", TypeTable())
    seq = []
    while True:
        lx.next()
        if lx.token_type == TT_EOF:
            break
        seq.append(lx.immediate if lx.token_type == TT_IMMEDIATE else lx.token)
    assert seq == [1.0]


if __name__ == "__main__":
    for fn in (test_names_and_punct, test_maximal_munch, test_comments_skipped,
               test_float_immediate, test_negative_immediate_gotcha,
               test_string_escapes, test_vector_immediate, test_frame_macros):
        fn()
    print("OK")

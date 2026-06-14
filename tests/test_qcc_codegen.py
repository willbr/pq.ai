"""Unit tests for quake/qcc codegen: opcode table integrity + emit."""
import _bootstrap  # noqa: F401

from quake.qcc.codegen import (OPCODES, OP_DONE, OP_MUL_F, OP_STORE_F,
                               OP_LOAD_F, OP_ADDRESS, OP_RETURN, OP_CALL0,
                               OP_BITOR, emit)
from quake.qcc.symbols import CompileState


def test_opcode_indices_match_names():
    assert OPCODES[OP_DONE].opname == "DONE"
    assert OPCODES[OP_MUL_F].opname == "MUL_F"
    assert OPCODES[OP_STORE_F].opname == "STORE_F"
    assert OPCODES[OP_LOAD_F].opname == "INDIRECT"   # the . load family
    assert OPCODES[OP_ADDRESS].opname == "ADDRESS"
    assert OPCODES[OP_RETURN].opname == "RETURN"
    assert OPCODES[OP_CALL0].opname == "CALL0"
    assert OPCODES[OP_BITOR].opname == "BITOR"


def test_emit_allocates_result_global():
    st = CompileState()
    a = st.parse_immediate(st.types.float, 2.0)
    b = st.parse_immediate(st.types.float, 3.0)
    before = st.numpr_globals
    c = emit(st, OPCODES[OP_MUL_F], a, b)
    assert c.ofs == before                  # result is a fresh temp global
    assert st.numpr_globals == before + 1   # never reused
    op, sa, sb, sc = st.statements[-1]
    assert op == OP_MUL_F and sa == a.ofs and sb == b.ofs and sc == c.ofs


def test_emit_store_no_result():
    st = CompileState()
    src = st.parse_immediate(st.types.float, 1.0)
    dst = st.get_def(st.types.float, "x", None, True)
    before = st.numpr_globals
    ret = emit(st, OPCODES[OP_STORE_F], src, dst)   # right-associative
    assert st.numpr_globals == before               # no temp allocated
    assert ret is src                                # store returns its rhs
    op, sa, sb, sc = st.statements[-1]
    assert op == OP_STORE_F and sa == src.ofs and sb == dst.ofs and sc == 0


if __name__ == "__main__":
    for fn in (test_opcode_indices_match_names, test_emit_allocates_result_global,
               test_emit_store_no_result):
        fn()
    print("OK")

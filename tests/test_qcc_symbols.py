"""Unit tests for quake/qcc type table, defs, immediate dedup, field offsets."""
import _bootstrap  # noqa: F401

from quake.qcc.types import (TypeTable, ev_float, ev_vector, ev_field,
                             ev_function, ev_void, type_size)
from quake.qcc.symbols import CompileState, RESERVED_OFS


def test_type_size():
    assert type_size == (1, 1, 1, 3, 1, 1, 1, 1)


def test_base_types_singletons():
    tt = TypeTable()
    assert tt.float is tt.float
    assert tt.float.type == ev_float
    assert tt.vector.type == ev_vector


def test_field_type_interned():
    tt = TypeTable()
    a = tt.field_of(tt.float)
    b = tt.field_of(tt.float)
    assert a is b                       # interned
    assert a.type == ev_field and a.aux_type is tt.float


def test_function_type_interned():
    tt = TypeTable()
    a = tt.function_of(tt.void, (tt.float,))
    b = tt.function_of(tt.void, (tt.float,))
    assert a is b
    assert a.type == ev_function and a.aux_type is tt.void
    assert a.parm_types == (tt.float,) and a.num_parms == 1


def test_reserved_offset():
    st = CompileState()
    assert RESERVED_OFS == 28
    assert st.numpr_globals == 28        # PR_BeginCompilation


def test_vector_autogen_members():
    st = CompileState()
    org = st.get_def(st.types.vector, "org", None, allocate=True)
    x = st.get_def(None, "org_x", None, allocate=False)
    y = st.get_def(None, "org_y", None, allocate=False)
    z = st.get_def(None, "org_z", None, allocate=False)
    assert x and y and z
    assert org.ofs == x.ofs              # parent shares first element's slot
    assert (y.ofs, z.ofs) == (x.ofs + 1, x.ofs + 2)
    assert st.numpr_globals == 28 + 3    # vector consumed 3 slots


def test_field_offset_allocation():
    st = CompileState()
    h = st.get_def(st.types.field_of(st.types.float), "health", None, True)
    assert st.gi(h.ofs) == 0
    assert st.size_fields == 1
    o = st.get_def(st.types.field_of(st.types.float), "origin2", None, True)
    assert st.gi(o.ofs) == 1
    assert st.size_fields == 2


def test_immediate_dedup():
    st = CompileState()
    a = st.parse_immediate(st.types.float, 5.0)
    b = st.parse_immediate(st.types.float, 5.0)   # same value -> same def/slot
    c = st.parse_immediate(st.types.float, 6.0)
    assert a is b and a.ofs == b.ofs
    assert c.ofs != a.ofs
    assert st.gf(a.ofs) == 5.0 and st.gf(c.ofs) == 6.0


if __name__ == "__main__":
    for fn in (test_type_size, test_base_types_singletons,
               test_field_type_interned, test_function_type_interned,
               test_reserved_offset, test_vector_autogen_members,
               test_field_offset_allocation, test_immediate_dedup):
        fn()
    print("OK")

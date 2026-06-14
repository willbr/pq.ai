"""Unit tests for quake/qcc type table, defs, immediate dedup, field offsets."""
import _bootstrap  # noqa: F401

from quake.qcc.types import (TypeTable, ev_float, ev_vector, ev_field,
                             ev_function, ev_void, type_size)


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


if __name__ == "__main__":
    for fn in (test_type_size, test_base_types_singletons,
               test_field_type_interned, test_function_type_interned):
        fn()
    print("OK")

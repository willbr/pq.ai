"""Codegen: the opcode table (pr_comp.c pr_opcodes) and PR_Statement (emit).

The OPCODES list is ordered identically to the OP_* enum in pr_comp.h, so the
list index IS the bytecode opcode (id computes `statement->op = op - pr_opcodes`).
Each entry's a/b/c types are etype ints, used by the parser for type-directed
opcode selection (pr_comp.c PR_Expression).

emit() mirrors PR_Statement: it appends a (op,a,b,c) statement; for ops with a
real result type (not void, not right-associative) it allocates ONE fresh global
for the result and never reuses it (this is the core byte-identity invariant)."""

from collections import namedtuple
from .types import (type_size, ev_void, ev_string, ev_float, ev_vector,
                    ev_entity, ev_field, ev_function, ev_pointer)

Op = namedtuple("Op", "name opname priority right_assoc a b c")

V, S, F, VEC, E, FLD, FN, P = (ev_void, ev_string, ev_float, ev_vector,
                               ev_entity, ev_field, ev_function, ev_pointer)

OPCODES = [
    Op("<DONE>",   "DONE",     -1, False, E,  FLD, V),
    Op("*",        "MUL_F",     2, False, F,  F,  F),
    Op("*",        "MUL_V",     2, False, VEC, VEC, F),
    Op("*",        "MUL_FV",    2, False, F,  VEC, VEC),
    Op("*",        "MUL_VF",    2, False, VEC, F,  VEC),
    Op("/",        "DIV",       2, False, F,  F,  F),
    Op("+",        "ADD_F",     3, False, F,  F,  F),
    Op("+",        "ADD_V",     3, False, VEC, VEC, VEC),
    Op("-",        "SUB_F",     3, False, F,  F,  F),
    Op("-",        "SUB_V",     3, False, VEC, VEC, VEC),
    Op("==",       "EQ_F",      4, False, F,  F,  F),
    Op("==",       "EQ_V",      4, False, VEC, VEC, F),
    Op("==",       "EQ_S",      4, False, S,  S,  F),
    Op("==",       "EQ_E",      4, False, E,  E,  F),
    Op("==",       "EQ_FNC",    4, False, FN, FN, F),
    Op("!=",       "NE_F",      4, False, F,  F,  F),
    Op("!=",       "NE_V",      4, False, VEC, VEC, F),
    Op("!=",       "NE_S",      4, False, S,  S,  F),
    Op("!=",       "NE_E",      4, False, E,  E,  F),
    Op("!=",       "NE_FNC",    4, False, FN, FN, F),
    Op("<=",       "LE",        4, False, F,  F,  F),
    Op(">=",       "GE",        4, False, F,  F,  F),
    Op("<",        "LT",        4, False, F,  F,  F),
    Op(">",        "GT",        4, False, F,  F,  F),
    Op(".",        "INDIRECT",  1, False, E,  FLD, F),
    Op(".",        "INDIRECT",  1, False, E,  FLD, VEC),
    Op(".",        "INDIRECT",  1, False, E,  FLD, S),
    Op(".",        "INDIRECT",  1, False, E,  FLD, E),
    Op(".",        "INDIRECT",  1, False, E,  FLD, FLD),
    Op(".",        "INDIRECT",  1, False, E,  FLD, FN),
    Op(".",        "ADDRESS",   1, False, E,  FLD, P),
    Op("=",        "STORE_F",   5, True,  F,  F,  F),
    Op("=",        "STORE_V",   5, True,  VEC, VEC, VEC),
    Op("=",        "STORE_S",   5, True,  S,  S,  S),
    Op("=",        "STORE_ENT", 5, True,  E,  E,  E),
    Op("=",        "STORE_FLD", 5, True,  FLD, FLD, FLD),
    Op("=",        "STORE_FNC", 5, True,  FN, FN, FN),
    Op("=",        "STOREP_F",  5, True,  P,  F,  F),
    Op("=",        "STOREP_V",  5, True,  P,  VEC, VEC),
    Op("=",        "STOREP_S",  5, True,  P,  S,  S),
    Op("=",        "STOREP_ENT", 5, True,  P,  E,  E),
    Op("=",        "STOREP_FLD", 5, True, P,  FLD, FLD),
    Op("=",        "STOREP_FNC", 5, True, P,  FN, FN),
    Op("<RETURN>", "RETURN",   -1, False, V,  V,  V),
    Op("!",        "NOT_F",    -1, False, F,  V,  F),
    Op("!",        "NOT_V",    -1, False, VEC, V,  F),
    Op("!",        "NOT_S",    -1, False, VEC, V,  F),
    Op("!",        "NOT_ENT",  -1, False, E,  V,  F),
    Op("!",        "NOT_FNC",  -1, False, FN, V,  F),
    Op("<IF>",     "IF",       -1, False, F,  F,  V),
    Op("<IFNOT>",  "IFNOT",    -1, False, F,  F,  V),
    Op("<CALL0>",  "CALL0",    -1, False, FN, V,  V),
    Op("<CALL1>",  "CALL1",    -1, False, FN, V,  V),
    Op("<CALL2>",  "CALL2",    -1, False, FN, V,  V),
    Op("<CALL3>",  "CALL3",    -1, False, FN, V,  V),
    Op("<CALL4>",  "CALL4",    -1, False, FN, V,  V),
    Op("<CALL5>",  "CALL5",    -1, False, FN, V,  V),
    Op("<CALL6>",  "CALL6",    -1, False, FN, V,  V),
    Op("<CALL7>",  "CALL7",    -1, False, FN, V,  V),
    Op("<CALL8>",  "CALL8",    -1, False, FN, V,  V),
    Op("<STATE>",  "STATE",    -1, False, F,  F,  V),
    Op("<GOTO>",   "GOTO",     -1, False, F,  V,  V),
    Op("&&",       "AND",       6, False, F,  F,  F),
    Op("||",       "OR",        6, False, F,  F,  F),
    Op("&",        "BITAND",    2, False, F,  F,  F),
    Op("|",        "BITOR",     2, False, F,  F,  F),
]

# named indices (must match OPCODES order == pr_comp.h enum)
# _idx.setdefault keeps the FIRST occurrence — correct for duplicate-named rows
# (INDIRECT has 6 entries; STORE_* etc. are unique)
_idx = {}
for _i, _op in enumerate(OPCODES):
    _idx.setdefault(_op.opname, _i)
(OP_DONE, OP_MUL_F, OP_LOAD_F, OP_ADDRESS, OP_STORE_F, OP_STORE_V, OP_RETURN,
 OP_IF, OP_IFNOT, OP_CALL0, OP_STATE, OP_GOTO, OP_NOT_F, OP_BITOR) = (
    _idx["DONE"], _idx["MUL_F"], _idx["INDIRECT"], _idx["ADDRESS"],
    _idx["STORE_F"], _idx["STORE_V"], _idx["RETURN"], _idx["IF"], _idx["IFNOT"],
    _idx["CALL0"], _idx["STATE"], _idx["GOTO"], _idx["NOT_F"], _idx["BITOR"])

TOP_PRIORITY = 6
NOT_PRIORITY = 4

OP_INDEX = {op: i for i, op in enumerate(OPCODES)}   # Op -> bytecode index


def emit(state, op, var_a, var_b):
    """PR_Statement: append (op,a,b,c); allocate a result global unless the op
    is void-result or right-associative. Returns the result Def (or var_a for
    right-associative ops, so chained '=' works)."""
    from .symbols import Def
    idx = OP_INDEX[op]
    a = var_a.ofs if var_a is not None else 0
    b = var_b.ofs if var_b is not None else 0
    if op.c == ev_void or op.right_assoc:
        c = 0
        var_c = None
    else:
        var_c = Def(state.types_for(op.c), None, state.numpr_globals)
        c = state.numpr_globals
        state.numpr_globals += type_size[op.c]
    state.statements.append((idx, a, b, c))
    state.statement_lines.append(state.cur_line)
    return var_a if op.right_assoc else var_c

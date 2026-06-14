"""The QuakeC parser + driver. Ports pr_comp.c (PR_Expression, PR_Term,
PR_ParseValue, PR_ParseFunctionCall, PR_ParseStatement, PR_ParseState,
PR_ParseImmediateStatements, PR_GetDef callers, PR_ParseDefs, PR_CompileFile)
and pr_lex.c PR_ParseType, plus qcc.c's progs.src driver loop.

Single accumulating unit: one CompileState is shared across every source file;
state is never reset, so defs.qc (listed first in progs.src) is visible to all
later files."""

import os
from .errors import QCCError
from .lexer import Lexer, TT_EOF, TT_NAME, TT_PUNCT, TT_IMMEDIATE
from .symbols import CompileState
from .types import (type_size, ev_void, ev_string, ev_float, ev_vector,
                    ev_entity, ev_field, ev_function, ev_pointer)
from .codegen import (OPCODES, OP_INDEX, OP_DONE, OP_LOAD_F, OP_ADDRESS,
                      OP_STORE_V, OP_RETURN, OP_IF, OP_IFNOT, OP_CALL0,
                      OP_STATE, OP_GOTO, TOP_PRIORITY, NOT_PRIORITY,
                      emit)


class Compiler:
    def __init__(self, state, lexer):
        self.st = state
        self.lx = lexer
        self.scope = None           # current function Def, or None
        self.locals_end = 0

    # --- lexer glue ---
    def err(self, msg):
        raise QCCError(self.lx.file, self.lx.line, msg)

    def lex(self):
        self.lx.next()
        self.st.cur_line = self.lx.line

    def check(self, s):
        if self.lx.token == s and self.lx.token_type in (TT_PUNCT, TT_NAME):
            self.lex()
            return True
        return False

    def expect(self, s):
        if self.lx.token != s:
            self.err(f"expected {s}, found {self.lx.token}")
        self.lex()

    def parse_name(self):
        if self.lx.token_type != TT_NAME:
            self.err("not a name")
        name = self.lx.token
        self.lex()
        return name

    def get_def(self, type, name, scope, allocate):
        try:
            return self.st.get_def(type, name, scope, allocate)
        except QCCError as e:
            raise QCCError(self.lx.file, self.lx.line, e.message) from None

    # --- PR_ParseType (pr_lex.c) ---
    def parse_type(self):
        t = self.st.types
        if self.check("."):
            return t.field_of(self.parse_type())
        name = self.lx.token
        base = {"float": t.float, "vector": t.vector, "entity": t.entity,
                "string": t.string, "void": t.void}.get(name)
        if base is None:
            self.err(f'"{name}" is not a type')
        self.lex()
        if not self.check("("):
            return base
        # function type
        parms = []
        num = 0
        if not self.check(")"):
            if self.check("..."):
                num = -1
            else:
                while True:
                    pt = self.parse_type()
                    pname = self.parse_name()
                    parms.append((pt, pname))
                    num += 1
                    if not self.check(","):
                        break
            self.expect(")")
        ftype = t.function_of(base, tuple(p[0] for p in parms), num)
        # stash parm names for PR_ParseImmediateStatements
        self._last_parm_names = [p[1] for p in parms]
        return ftype

    # --- immediates / values ---
    def parse_immediate(self):
        d = self.st.parse_immediate(self.lx.immediate_type,
                                    self._imm_value())
        self.lex()
        return d

    def _imm_value(self):
        it = self.lx.immediate_type
        if it is self.st.types.string:
            return self.lx.immediate_string
        return self.lx.immediate

    def parse_value(self):
        if self.lx.token_type == TT_IMMEDIATE:
            return self.parse_immediate()
        name = self.parse_name()
        d = self.get_def(None, name, self.scope, False)
        if d is None:
            self.err(f'Unknown value "{name}"')
        return d

    # --- PR_Term ---
    def term(self):
        st = self.st
        if self.check("!"):
            e = self.expression(NOT_PRIORITY)
            t = e.type.type
            op = {ev_float: "NOT_F", ev_string: "NOT_S", ev_entity: "NOT_ENT",
                  ev_vector: "NOT_V", ev_function: "NOT_FNC"}.get(t)
            if op is None:
                self.err("type mismatch for !")
            return emit(st, OPCODES[_op_by_name(op)], e, None)
        if self.check("("):
            e = self.expression(TOP_PRIORITY)
            self.expect(")")
            return e
        return self.parse_value()

    # --- PR_Expression (precedence climbing + type-directed opcode select) ---
    def expression(self, priority):
        if priority == 0:
            return self.term()
        e = self.expression(priority - 1)
        st = self.st
        while True:
            if priority == 1 and self.check("("):
                e = self.function_call(e)
                continue
            matched = None
            for i, op in enumerate(OPCODES):
                if op.priority != priority:
                    continue
                if self.lx.token != op.name or self.lx.token_type not in (TT_PUNCT,):
                    continue
                self.lex()
                if op.right_assoc:
                    # rewrite a trailing LOAD into ADDRESS (lvalue) -- pr_comp.c:479
                    last = st.statements[-1][0]
                    if OP_LOAD_F <= last < OP_LOAD_F + 6:
                        st.statements[-1] = (OP_ADDRESS,) + st.statements[-1][1:]
                        e = self._as_pointer(e)
                    e2 = self.expression(priority)
                else:
                    e2 = self.expression(priority - 1)
                op = self._select(op, e, e2)
                if op.right_assoc:
                    res = emit(st, op, e2, e)
                else:
                    res = emit(st, op, e, e2)
                # field access result type comes from the field's aux_type
                if op.opname == "INDIRECT":
                    res.type = e2.type.aux_type
                e = res
                matched = op
                break
            if matched is None:
                break
        return e

    def _select(self, op, e, e2):
        """Walk the same-named opcode rows until operand types match
        (pr_comp.c type-check loop)."""
        ta, tb = e.type.type, e2.type.type
        if op.name == ".":
            tc = e2.type.aux_type.type if e2.type.aux_type else -1
        else:
            tc = ev_void
        i = OP_INDEX[op]
        while True:
            cand = OPCODES[i]
            if cand.name != op.name:
                self.err(f"type mismatch for {op.name}")
            if (ta == cand.a and tb == cand.b
                    and (tc == ev_void or tc == cand.c)):
                return cand
            i += 1
            if i >= len(OPCODES):
                self.err(f"type mismatch for {op.name}")

    def _as_pointer(self, e):
        # give e a pointer type whose aux is its current type
        from .types import Type
        e.type = Type(ev_pointer, aux_type=e.type)
        return e

    # --- PR_ParseFunctionCall ---
    def function_call(self, func):
        st = self.st
        t = func.type
        if t.type != ev_function:
            self.err("not a function")
        arg = 0
        if not self.check(")"):
            while True:
                if t.num_parms != -1 and arg >= t.num_parms:
                    self.err("too many parameters")
                e = self.expression(TOP_PRIORITY)
                if t.num_parms != -1 and e.type is not t.parm_types[arg]:
                    self.err(f"type mismatch on parm {arg}")
                parm = st.def_parms[arg]
                parm.type = t.parm_types[arg] if t.num_parms != -1 else e.type
                emit(st, OPCODES[OP_STORE_V], e, parm)   # vector copy = everything
                arg += 1
                if not self.check(","):
                    break
            if t.num_parms != -1 and arg != t.num_parms:
                self.err("too few parameters")
            self.expect(")")
        if arg > 8:
            self.err("More than eight parameters")
        emit(st, OPCODES[OP_CALL0 + arg], func, None)
        st.def_ret.type = t.aux_type
        return st.def_ret

    # --- PR_ParseStatement (control flow with back-patching) ---
    def statement(self):
        st = self.st
        if self.check("{"):
            while not self.check("}"):
                self.statement()
            return
        if self.check("return"):
            if self.check(";"):
                emit(st, OPCODES[OP_RETURN], None, None)
                return
            e = self.expression(TOP_PRIORITY)
            self.expect(";")
            emit(st, OPCODES[OP_RETURN], e, None)
            return
        if self.check("while"):
            self.expect("(")
            top = len(st.statements)
            e = self.expression(TOP_PRIORITY)
            self.expect(")")
            patch = len(st.statements)
            emit(st, OPCODES[OP_IFNOT], e, None)
            self.statement()
            self._emit_goto(top - len(st.statements))
            self._patch_b(patch, len(st.statements) - patch)
            return
        if self.check("do"):
            top = len(st.statements)
            self.statement()
            self.expect("while")
            self.expect("(")
            e = self.expression(TOP_PRIORITY)
            self.expect(")")
            self.expect(";")
            self._emit_if(e, top - len(st.statements))
            return
        if self.check("local"):
            self.parse_defs()
            self.locals_end = st.numpr_globals
            return
        if self.check("if"):
            self.expect("(")
            e = self.expression(TOP_PRIORITY)
            self.expect(")")
            patch1 = len(st.statements)
            emit(st, OPCODES[OP_IFNOT], e, None)
            self.statement()
            if self.check("else"):
                patch2 = len(st.statements)
                emit(st, OPCODES[OP_GOTO], None, None)
                self._patch_b(patch1, len(st.statements) - patch1)
                self.statement()
                self._patch_a(patch2, len(st.statements) - patch2)
            else:
                self._patch_b(patch1, len(st.statements) - patch1)
            return
        self.expression(TOP_PRIORITY)
        self.expect(";")

    # statement back-patch helpers (statements are (op,a,b,c) tuples)
    def _patch_a(self, i, val):
        op, a, b, c = self.st.statements[i]
        self.st.statements[i] = (op, val, b, c)

    def _patch_b(self, i, val):
        op, a, b, c = self.st.statements[i]
        self.st.statements[i] = (op, a, val, c)

    def _emit_goto(self, rel):
        self.st.statements.append((OP_GOTO, rel, 0, 0))
        self.st.statement_lines.append(self.st.cur_line)

    def _emit_if(self, e, rel):
        self.st.statements.append((OP_IF, e.ofs, rel, 0))
        self.st.statement_lines.append(self.st.cur_line)

    # --- PR_ParseState ---
    def parse_state(self):
        if (self.lx.token_type != TT_IMMEDIATE
                or self.lx.immediate_type is not self.st.types.float):
            self.err("state frame must be a number")
        s1 = self.parse_immediate()
        self.expect(",")
        name = self.parse_name()
        d = self.get_def(self.st.types.function, name, None, True)
        self.expect("]")
        emit(self.st, OPCODES[OP_STATE], s1, d)

    # --- PR_ParseImmediateStatements (function body) ---
    def parse_function_body(self, ftype, parm_names):
        st = self.st
        if self.check("#"):
            if (self.lx.token_type != TT_IMMEDIATE
                    or self.lx.immediate_type is not st.types.float
                    or self.lx.immediate != int(self.lx.immediate)):
                self.err("Bad builtin immediate")
            builtin = int(self.lx.immediate)
            self.lex()
            return {"builtin": builtin, "code": 0, "parm_ofs": []}
        parm_ofs = []
        for i in range(ftype.num_parms):
            d = self.get_def(ftype.parm_types[i], parm_names[i], self.scope, True)
            parm_ofs.append(d.ofs)
        code = len(st.statements)
        if self.check("["):
            self.parse_state()
        self.expect("{")
        while not self.check("}"):
            self.statement()
        emit(st, OPCODES[OP_DONE], None, None)
        return {"builtin": 0, "code": code, "parm_ofs": parm_ofs}

    # --- PR_ParseDefs ---
    def parse_defs(self):
        st = self.st
        self._last_parm_names = []
        type = self.parse_type()
        parm_names = list(getattr(self, "_last_parm_names", []))
        if self.scope and type.type in (ev_field, ev_function):
            self.err("Fields and functions must be global")
        while True:
            name = self.parse_name()
            d = self.get_def(type, name, self.scope, True)
            if self.check("="):
                if d.initialized:
                    self.err(f"{name} redeclared")
                if type.type == ev_function:
                    locals_start = self.locals_end = st.numpr_globals
                    self.scope = d
                    f = self.parse_function_body(type, parm_names)
                    self.scope = None
                    d.initialized = True
                    st.set_gi(d.ofs, len(st.functions))      # G_FUNCTION = index
                    self._emit_function(d, f, type, locals_start)
                    if not self.check(","):
                        break
                    continue
                elif self.lx.immediate_type is not type:
                    self.err(f"wrong immediate type for {name}")
                d.initialized = True
                self._store_immediate(d, type)
                self.lex()
            if not self.check(","):
                break
        self.expect(";")

    def _store_immediate(self, d, type):
        it = self.lx.immediate_type
        if type.type == ev_string:
            st = self.st
            st.set_gi(d.ofs, st.copy_string(self.lx.immediate_string))
        elif type.type == ev_float:
            self.st.set_gf(d.ofs, self.lx.immediate)
        elif type.type == ev_vector:
            v = self.lx.immediate
            self.st.set_gf(d.ofs, v[0]); self.st.set_gf(d.ofs + 1, v[1])
            self.st.set_gf(d.ofs + 2, v[2])
        else:
            self.st.set_gi(d.ofs, int(self.lx.immediate))

    def _emit_function(self, d, f, type, locals_start):
        st = self.st
        first = -f["builtin"] if f["builtin"] else f["code"]
        parm_size = [type_size[type.parm_types[i].type]
                     for i in range(type.num_parms if type.num_parms != -1 else 0)]
        st.functions.append({
            "first_statement": first,
            "parm_start": locals_start,
            "locals": self.locals_end - locals_start,
            "s_name": st.copy_string(d.name),
            "s_file": st.cur_file,
            "numparms": type.num_parms,
            "parm_size": parm_size,
        })

    # --- PR_CompileFile ---
    def compile_file(self):
        self.lex()
        while self.lx.token_type != TT_EOF:
            self.scope = None
            self.parse_defs()


def _op_by_name(opname):
    for i, op in enumerate(OPCODES):
        if op.opname == opname:
            return i
    raise KeyError(opname)


def _parse_src(text):
    """cmdlib.c COM_Parse over progs.src: whitespace-delimited tokens, skipping
    // line comments. (No quoted strings appear in progs.src in practice.)"""
    tokens = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c <= " ":
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        start = i
        while i < n and text[i] > " ":
            i += 1
        tokens.append(text[start:i])
    return tokens


# --- driver (qcc.c main) ---
def compile_progs_src(path):
    base = os.path.dirname(path)
    with open(path) as f:
        tokens = _parse_src(f.read())                 # COM_Parse: skips // comments
    dest = tokens[0]                                  # output filename (ignored)
    files = tokens[1:]
    state = CompileState()
    for fname in files:
        with open(os.path.join(base, fname)) as fh:
            src = fh.read()
        state.cur_file = state.copy_string(fname)
        lx = Lexer(src, fname, state.types)
        Compiler(state, lx).compile_file()
    _finish(state)
    from .writer import write_progs
    return write_progs(state)


def _finish(state):
    # PR_FinishCompilation: every prototyped global function must be defined
    for d in state.defs:
        if (d.type is not None and d.type.type == ev_function
                and d.scope is None and not d.initialized):
            raise QCCError("?", 0, f"function {d.name} was not defined")

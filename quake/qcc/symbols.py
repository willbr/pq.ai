"""Compile state, defs, immediates. Ports pr_comp.c PR_GetDef / PR_ParseImmediate
and qcc.c CopyString / InitData.

CompileState is the single accumulating unit id keeps across all source files
(no reset between files). It holds:
  defs       insertion-ordered named defs + immediates (-> globaldefs lump)
  globals    float view + int view over one growable buffer (eval_t union)
  numpr_globals  next free global slot (incl. temps, which are NOT in `defs`)
  strings    NUL-separated blob, byte-indexed (string 0 is NUL)
  statements emitted dstatements (index 0 reserved/zero)
  functions  dfunctions (index 0 reserved/zero)
  size_fields running entity field-offset counter (-> entityfields)

Byte-identity invariants live here: temp slots are never reused; immediates are
deduped by a linear scan of `defs` in insertion order (first appearance fixes
the offset); vector defs auto-create _x/_y/_z."""

import struct

from .types import (TypeTable, type_size, ev_void, ev_vector, ev_field,
                    ev_function, ev_string, ev_float)

RESERVED_OFS = 28           # OFS_NULL + OFS_RETURN + 8*3 param slots
OFS_RETURN = 1
OFS_PARM0 = 4

_MAX_GLOBALS = 1 << 16      # grown on demand


class Def:
    __slots__ = ("type", "name", "ofs", "scope", "initialized")

    def __init__(self, type, name, ofs, scope=None, initialized=False):
        self.type = type
        self.name = name
        self.ofs = ofs
        self.scope = scope          # the function Def it's local to, or None
        self.initialized = initialized


class CompileState:
    def __init__(self):
        self.types = TypeTable()
        self.defs = []                          # insertion order
        self._by_name = {}                      # name -> [Def, ...] (scoping)
        self._buf = bytearray(_MAX_GLOBALS * 4)
        self._gf = memoryview(self._buf).cast("f")
        self._gi = memoryview(self._buf).cast("i")
        self.numpr_globals = RESERVED_OFS       # PR_BeginCompilation
        self.strings = bytearray(b"\x00")       # string 0 = NUL (InitData strofs=1)
        self.statements = [(0, 0, 0, 0)]        # index 0 reserved (InitData)
        self.statement_lines = [0]
        self.functions = [None]                 # index 0 reserved (InitData)
        self.size_fields = 0
        self.cur_file = ""                      # s_file string offset filename
        self.cur_line = 0                       # source line for statement_lines
        # PR_ParseImmediate / value plumbing
        self.def_ret = Def(None, "temp", OFS_RETURN)
        self.def_parms = [Def(None, "temp", OFS_PARM0 + 3 * i) for i in range(8)]

    # --- global accessors (eval_t union) ---
    def gf(self, o):
        return self._gf[o]

    def gi(self, o):
        return self._gi[o]

    def set_gf(self, o, v):
        self._gf[o] = v

    def set_gi(self, o, v):
        self._gi[o] = v

    def _alloc_globals(self, n):
        ofs = self.numpr_globals
        self.numpr_globals += n
        return ofs

    # --- strings (qcc.c CopyString) ---
    def copy_string(self, s):
        old = len(self.strings)
        self.strings += s.encode("latin-1") + b"\x00"
        return old

    # --- PR_GetDef (pr_comp.c) ---
    def get_def(self, type, name, scope, allocate):
        for d in self._by_name.get(name, ()):
            if d.scope is not None and d.scope is not scope:
                continue                        # different function's local
            if type is not None and d.type is not type:
                raise _mismatch(name)
            return d
        if not allocate:
            return None

        d = Def(type, name, self.numpr_globals, scope=scope)
        self.defs.append(d)
        self._by_name.setdefault(name, []).append(d)

        if type.type == ev_vector:
            for suf in ("_x", "_y", "_z"):
                self.get_def(self.types.float, name + suf, scope, True)
        else:
            self._alloc_globals(type_size[type.type])

        if type.type == ev_field:
            self.set_gi(d.ofs, self.size_fields)
            if type.aux_type.type == ev_vector:
                for suf in ("_x", "_y", "_z"):
                    self.get_def(self.types.floatfield, name + suf, scope, True)
            else:
                self.size_fields += type_size[type.aux_type.type]
        return d

    # --- PR_ParseImmediate (pr_comp.c) ---
    def parse_immediate(self, imm_type, value):
        """value: float, str, or (x,y,z). Dedup against existing constants."""
        for d in self.defs:
            if not d.initialized or d.type is not imm_type:
                continue
            if imm_type is self.types.string:
                if _str_at(self.strings, self.gi(d.ofs)) == value:
                    return d
            elif imm_type is self.types.float:
                # C compares G_FLOAT(cn->ofs) == pr_immediate._float, both 32-bit
                # floats. self.gf() is already float32; round `value` to match so
                # 0.1, 0.2 etc. (inexact in float32) dedup correctly.
                if self.gf(d.ofs) == _f32(value):
                    return d
            elif imm_type is self.types.vector:
                if (self.gf(d.ofs), self.gf(d.ofs + 1), self.gf(d.ofs + 2)) \
                        == (_f32(value[0]), _f32(value[1]), _f32(value[2])):
                    return d
        # allocate a new constant
        d = Def(imm_type, "IMMEDIATE", self.numpr_globals,
                scope=None, initialized=True)
        self.defs.append(d)
        self._by_name.setdefault("IMMEDIATE", []).append(d)
        self._alloc_globals(type_size[imm_type.type])
        if imm_type is self.types.string:
            self.set_gi(d.ofs, self.copy_string(value))
        elif imm_type is self.types.float:
            self.set_gf(d.ofs, value)
        else:                                    # vector
            self.set_gf(d.ofs, value[0])
            self.set_gf(d.ofs + 1, value[1])
            self.set_gf(d.ofs + 2, value[2])
        return d


    def types_for(self, etype):
        return (self.types.void, self.types.string, self.types.float,
                self.types.vector, self.types.entity, self.types.field,
                self.types.function, self.types.pointer)[etype]


def _mismatch(name):
    from .errors import QCCError
    return QCCError("?", 0, f"Type mismatch on redeclaration of {name}")


def _str_at(blob, ofs):
    end = blob.index(0, ofs)
    return blob[ofs:end].decode("latin-1")


_F32 = struct.Struct("<f")


def _f32(v):
    """Round a Python double to IEEE single precision (C float), as qccdos does."""
    return _F32.unpack(_F32.pack(v))[0]

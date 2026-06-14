"""QuakeC type system. Ports pr_lex.c type defs + PR_FindType interning.

etype_t (pr_comp.h): the 8 value kinds. type_size is slots-per-value (only
vector is 3). Complex types (field/function) are interned by structure so
identity comparison works in codegen, mirroring PR_FindType (pr_lex.c)."""

ev_void, ev_string, ev_float, ev_vector, ev_entity, ev_field, ev_function, ev_pointer = range(8)

type_size = (1, 1, 1, 3, 1, 1, 1, 1)


class Type:
    __slots__ = ("type", "aux_type", "parm_types", "num_parms")

    def __init__(self, type, aux_type=None, parm_types=(), num_parms=0):
        self.type = type
        self.aux_type = aux_type        # field value type / function return type
        self.parm_types = tuple(parm_types)
        self.num_parms = num_parms      # -1 = varargs

    def __repr__(self):
        return f"<Type {self.type} aux={self.aux_type and self.aux_type.type}>"


class TypeTable:
    """Owns the base type singletons and interns complex types."""

    def __init__(self):
        self.void = Type(ev_void)
        self.string = Type(ev_string)
        self.float = Type(ev_float)
        self.vector = Type(ev_vector)
        self.entity = Type(ev_entity)
        self.field = Type(ev_field)
        # type_function is a void() used for state forward-decls (pr_lex.c:49)
        self.function = Type(ev_function, aux_type=self.void)
        self.pointer = Type(ev_pointer)
        # type_floatfield (pr_lex.c:53) is a STANDALONE field type used only for
        # vector-field elements (_x/_y/_z); pr_comp.c passes &type_floatfield
        # directly and never links it into pr.types. So it is intentionally NOT
        # interned here: `floatfield is field_of(float)` is False, exactly as in
        # C, where parsed `.float` fields are a separate object. This never
        # affects output -- only etype ints reach progs.dat / opcode selection.
        self.floatfield = Type(ev_field, aux_type=self.float)
        # only type_function is interned at start (qcc.c:561 pr.types = &type_function)
        self._complex = [self.function]

    def _find(self, proto):
        for t in self._complex:
            if (t.type == proto.type and t.aux_type is proto.aux_type
                    and t.num_parms == proto.num_parms
                    and t.parm_types == proto.parm_types):
                return t
        self._complex.append(proto)
        return proto

    def field_of(self, aux):
        return self._find(Type(ev_field, aux_type=aux))

    def function_of(self, ret, parm_types, num_parms=None):
        n = len(parm_types) if num_parms is None else num_parms
        return self._find(Type(ev_function, aux_type=ret,
                               parm_types=parm_types, num_parms=n))

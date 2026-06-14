"""Pure-stdlib QuakeC compiler: compiles a progs.src manifest + its .qc files
into a version-6 progs.dat (the format quake/progs.py loads). A Pythonic
reimplementation of id's qcc (quake-source/quake-tools/qcc/). Byte-identical to
id's qccdos.exe on the same source -- see tests/test_qcc_compile.py.

Public API:
    compile_progs_src(path) -> bytes
"""
from .errors import QCCError

__all__ = ["compile_progs_src", "QCCError"]


def compile_progs_src(path):
    from .compiler import compile_progs_src as _impl
    return _impl(path)

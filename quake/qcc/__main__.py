"""CLI: python -m quake.qcc [-src DIR]   (mirrors qcc.c main).

Looks for progs.src in DIR (default cwd), compiles, writes the dest file named
on progs.src's first line (relative to DIR)."""

import os
import sys
from . import compile_progs_src


def main(argv):
    src_dir = "."
    if "-src" in argv:
        src_dir = argv[argv.index("-src") + 1]
    src_path = os.path.join(src_dir, "progs.src")
    with open(src_path) as f:
        dest = f.read().split()[0]
    data = compile_progs_src(src_path)
    out = os.path.join(src_dir, dest)
    with open(out, "wb") as f:
        f.write(data)
    print(f"wrote {out} ({len(data)} bytes)")


if __name__ == "__main__":
    main(sys.argv[1:])

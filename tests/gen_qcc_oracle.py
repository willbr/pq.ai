"""Regenerate the qcc oracle: compile id's v101qc with id's genuine qccdos.exe
under DOSBox-x, and copy the result to tests/data/progs_v101_oracle.dat.

This is the byte-identity ground truth for quake/qcc. Not run in CI (needs
DOSBox-x: `brew install dosbox-x`); the produced .dat is committed. v101qc and
its compiled output are id's GPLv2 release, freely redistributable.

Run:  python tests/gen_qcc_oracle.py
"""
import _bootstrap  # noqa: F401
import os, shutil, subprocess, tempfile

QCC_DIR = "quake-source/quake-tools/qcc"
OUT = "tests/data/progs_v101_oracle.dat"


def main():
    if not os.path.isdir(QCC_DIR):
        raise SystemExit("run `python setup.py` first to fetch quake-tools")
    # TemporaryDirectory so the scratch tree is cleaned up even if the DOSBox
    # run times out, qccdos.exe crashes, or the progs.dat check below bails.
    with tempfile.TemporaryDirectory(prefix="qccoracle") as work:
        shutil.copy(f"{QCC_DIR}/qccdos.exe", work)
        shutil.copy(f"{QCC_DIR}/cwsdpmi.exe", work)
        shutil.copytree(f"{QCC_DIR}/v101qc", f"{work}/v101qc")
        env = dict(os.environ, SDL_VIDEODRIVER="dummy")
        # output is forwarded (not captured) so DOSBox/qcc messages are visible.
        # progs.src says "../progs.dat" and cwd is v101qc, so it lands at work/.
        subprocess.run(
            ["dosbox-x", "-silent",
             "-c", f"mount c {work}", "-c", "c:", "-c", "cd v101qc",
             "-c", r"c:\qccdos.exe", "-c", "exit"],
            env=env, timeout=180, check=True)
        src = f"{work}/progs.dat"
        if not os.path.exists(src):
            raise SystemExit("qccdos.exe produced no progs.dat")
        os.makedirs("tests/data", exist_ok=True)
        shutil.copy(src, OUT)
    print(f"wrote {OUT} ({os.path.getsize(OUT)} bytes)")


if __name__ == "__main__":
    main()

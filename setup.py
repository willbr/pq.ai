#!/usr/bin/env python3
"""One-shot fetch of everything this repo needs but does not (and legally cannot)
ship: id Software's GPL source reference and the Quake shareware data.

Both live outside git (see .gitignore): the shareware is id copyright and must
not be redistributed, and the GPL source is read-only port reference that would
only pollute repo-wide search. This script reproduces, on any fresh checkout,
the exact layout the rest of the project expects:

    quake-source/WinQuake/        id's C engine            (id-Software/Quake)
    quake-source/qw-qc/           QuakeWorld QuakeC        (id-Software/Quake)
    quake-source/quake-tools/     qcc, QBSP, LIGHT, VIS…   (id-Software/Quake-Tools)
    quake-shareware/id1/pak0.pak  shareware game data      (archive.org mirror)

Pure standard library (urllib/zipfile/hashlib) — matching the project's
no-dependencies ethos; no git required. Idempotent: anything already present and
checksum-valid is left alone. Run it as many times as you like.

    python setup.py                 # fetch whatever is missing
    python setup.py --force         # re-fetch everything
    python setup.py --skip-source   # shareware data only
    python setup.py --skip-data     # GPL source only

The shareware download URL defaults to a public archive.org mirror (the shareware
is free to download — just not for us to rehost). Override it if the mirror moves:

    python setup.py --shareware-url https://example/quake106.zip
    QUAKE_SHAREWARE_URL=https://example/quake106.zip python setup.py
"""

import argparse
import hashlib
import io
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent

# --- GPL reference source (id-Software, GPLv2) ------------------------------
# GitHub default-branch zipballs; urllib follows the 302 to codeload.
GPL_ENGINE_URL = "https://github.com/id-Software/Quake/archive/refs/heads/master.zip"
GPL_TOOLS_URL = "https://github.com/id-Software/Quake-Tools/archive/refs/heads/master.zip"

# --- Shareware data (id copyright; free to download, not redistributable) ----
# archive.org item "msdos_Quake106_shareware". This copy carries ID1/PAK0.PAK
# directly in the zip (no DOS-installer unwrapping needed).
DEFAULT_SHAREWARE_URL = (
    "https://archive.org/download/msdos_Quake106_shareware/msdos_Quake106_shareware.zip"
)
PAK_MEMBER = "ID1/PAK0.PAK"          # path inside the shareware zip
PAK_SIZE = 18689235                  # bytes
PAK_MD5 = "5906e5998fc3d896ddaf5e6a62e03abb"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def download(url):
    """Fetch a URL into memory, streaming a coarse progress bar to stderr."""
    log(f"  GET {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "pq.ai-setup"})
    buf = io.BytesIO()
    # Live progress only on a real terminal; piped/captured runs stay quiet.
    show = sys.stderr.isatty()
    with urllib.request.urlopen(req) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        read = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            buf.write(chunk)
            read += len(chunk)
            if show and total:
                pct = read * 100 // total
                print(f"\r      {read >> 20:>4} / {total >> 20} MiB  {pct:3}%",
                      end="", file=sys.stderr, flush=True)
        if show and total:
            print(file=sys.stderr)
    return buf.getvalue()


def md5(data):
    return hashlib.md5(data).hexdigest()


# --- GPL source -------------------------------------------------------------

def fetch_gpl_source(force):
    dest = REPO / "quake-source"
    engine_done = (dest / "WinQuake").is_dir() and (dest / "qw-qc").is_dir()
    tools_done = (dest / "quake-tools").is_dir()
    if engine_done and tools_done and not force:
        log("GPL source: already present (quake-source/) — skipping.")
        return

    log("GPL source: fetching id Software's GPLv2 release into quake-source/ …")
    dest.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        if not engine_done or force:
            zipfile.ZipFile(io.BytesIO(download(GPL_ENGINE_URL))).extractall(tmp)
            root = next(p for p in tmp.iterdir() if p.name.startswith("Quake-"))
            # Engine + QuakeC rules + the licence notices.
            for name in ("WinQuake", "qw-qc"):
                _replace_tree(root / name, dest / name)
            for note in ("gnu.txt", "readme.txt"):
                if (root / note).is_file():
                    shutil.copy2(root / note, dest / note)
            log("  laid out quake-source/WinQuake/ and quake-source/qw-qc/")

        if not tools_done or force:
            zipfile.ZipFile(io.BytesIO(download(GPL_TOOLS_URL))).extractall(tmp)
            root = next(p for p in tmp.iterdir() if p.name.startswith("Quake-Tools-"))
            _replace_tree(root, dest / "quake-tools")
            log("  laid out quake-source/quake-tools/ (qcc, qutils, QuakeEd)")


def _replace_tree(src, dst):
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


# --- Shareware data ---------------------------------------------------------

def fetch_shareware(url, force):
    pak = REPO / "quake-shareware" / "id1" / "pak0.pak"
    if pak.is_file() and not force:
        if pak.stat().st_size == PAK_SIZE and md5(pak.read_bytes()) == PAK_MD5:
            log("Shareware data: pak0.pak already present and verified — skipping.")
            return
        log("Shareware data: existing pak0.pak failed verification — re-fetching.")

    log("Shareware data: downloading Quake shareware (id copyright; free download) …")
    zip_bytes = download(url)
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        member = _find_member(zf, PAK_MEMBER)
        data = zf.read(member)
    except (zipfile.BadZipFile, KeyError) as e:
        log(f"  ERROR: could not read pak0.pak from the archive: {e}")
        log("  The mirror may have changed. Pass --shareware-url with another source.")
        sys.exit(1)

    got = md5(data)
    if len(data) != PAK_SIZE or got != PAK_MD5:
        log(f"  ERROR: pak0.pak checksum mismatch (size={len(data)}, md5={got}).")
        log(f"  Expected size={PAK_SIZE}, md5={PAK_MD5}. Refusing to install.")
        sys.exit(1)

    pak.parent.mkdir(parents=True, exist_ok=True)
    pak.write_bytes(data)
    log(f"  wrote {pak.relative_to(REPO)} ({len(data) >> 20} MiB, verified).")


def _find_member(zf, target):
    """Match the pak member case-insensitively (mirrors vary ID1/ vs id1/)."""
    want = target.lower()
    for name in zf.namelist():
        if name.lower() == want:
            return name
    raise KeyError(target)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch GPL reference source + Quake shareware data.")
    ap.add_argument("--force", action="store_true", help="re-fetch even if present")
    ap.add_argument("--skip-source", action="store_true", help="skip the GPL reference source")
    ap.add_argument("--skip-data", action="store_true", help="skip the shareware data")
    ap.add_argument("--shareware-url",
                    default=os.environ.get("QUAKE_SHAREWARE_URL", DEFAULT_SHAREWARE_URL),
                    help="override the shareware zip URL (default: archive.org mirror)")
    args = ap.parse_args(argv)

    if not args.skip_source:
        fetch_gpl_source(args.force)
    if not args.skip_data:
        fetch_shareware(args.shareware_url, args.force)

    log("\nDone. Try:  python main.py e1m1")


if __name__ == "__main__":
    main()

"""Hand-installed software: /opt, /usr/local, and broken links in the bin directories."""
import os
from pathlib import Path

from ..config import read_os_release
from .base import PHASE_MANUAL, Step


def _ls(d):
    """Like `ls -1`: visible entries only."""
    return sorted(e for e in os.listdir(d) if not e.startswith(".")) if os.path.isdir(d) else []


def collect(ctx, snap):
    osr = read_os_release()
    u = os.uname()
    snap.write_lines("system.txt", [f'{k}="{v}"' for k, v in (
        ("PRETTY_NAME", osr.get("PRETTY_NAME", "")), ("ID", osr.get("ID", "")),
        ("VERSION_ID", osr.get("VERSION_ID", "")), ("KERNEL", u.release), ("ARCH", u.machine))])

    out = ["# /opt", *_ls("/opt"),
           "", "# /usr/local/bin (-> target)"]
    for f in sorted(Path("/usr/local/bin").glob("*")):
        if f.exists():
            out.append(f"{f.name} -> {os.path.realpath(f)}")
        elif f.is_symlink():
            out.append(f"{f.name} -> {os.readlink(f)}  (BROKEN LINK: target missing)")
    out += ["", "# /usr/local (top level)", *_ls("/usr/local")]
    snap.write_lines("opt-usr-local.txt", out)


def check(ctx, rep):
    rep.section("links")
    broken = 0
    for d in (Path("/usr/local/bin"), ctx.home / ".local/bin"):
        for f in sorted(d.glob("*")):
            if f.is_symlink() and not f.exists():
                broken += 1
                rep.fail(f"broken link: {f} -> {os.readlink(f)}")
    if not broken:
        rep.ok("links: /usr/local/bin and ~/.local/bin have no broken links")


def steps(ctx, catalog, items):
    """Free-text manual steps of the selected items (downloads, licences, ...)."""
    out = []
    for i in items:
        if i.manual:
            out.append(Step(PHASE_MANUAL, i.name, notes=[l for l in i.manual.strip().splitlines()], manual=True))
    return out

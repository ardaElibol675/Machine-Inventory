"""Dotfiles: files under $HOME listed in config.yaml `dotfiles` (globs allowed).

Stored as snapshot/dotfiles/<path relative to $HOME>, e.g. snapshot/dotfiles/.bashrc.
"""
import fnmatch
import glob
import os

from ..config import expand
from .base import PHASE_DOTFILES, Step, backup_install, missing_items

DIR = "dotfiles"


def _rel(ctx, pattern):
    p = str(expand(pattern))
    home = str(ctx.home)
    if not p.startswith(home + "/"):
        raise SystemExit(f"config.yaml dotfiles: {pattern} is not under $HOME")
    return p[len(home) + 1:]


def collect(ctx, snap):
    for pattern in ctx.cfg["dotfiles"]:
        for path in sorted(glob.glob(str(expand(pattern)))):
            if os.path.isfile(path):
                snap.copy(path, f"{DIR}/{_rel(ctx, path)}")


def saved(ctx):
    root = ctx.snapshot / DIR
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def _match(ctx, patterns):
    rels = [_rel(ctx, p) for p in patterns]
    return [f for f in saved(ctx) if any(fnmatch.fnmatch(f, r) for r in rels)]


def drift(ctx, catalog, rep):
    claimed = set(_match(ctx, catalog.claimed("dotfiles")))
    unclaimed = [f"~/{f}" for f in saved(ctx) if f not in claimed]
    if unclaimed:
        rep.warn(f"dotfiles: uncatalogued: {' '.join(unclaimed)}")
    else:
        rep.ok("dotfiles: every saved dotfile is catalogued")
    missing_items(ctx, catalog, rep, "dotfiles", lambda p: bool(_match(ctx, [p])), "dotfiles",
                  " (not on this machine, or not in config.yaml dotfiles)")


def steps(ctx, catalog, items):
    cmds, notes = [], []
    for i in items:
        files = _match(ctx, i.get("dotfiles"))
        if i.get("dotfiles") and not files:
            notes.append(f"{i.id}: no saved file matches {', '.join(i.get('dotfiles'))}")
        for f in files:
            src = ctx.snapshot / DIR / f
            mode = "755" if os.access(src, os.X_OK) else "644"
            cmds.append(backup_install(src, ctx.home / f, mode))
    if not (cmds or notes):
        return []
    return [Step(PHASE_DOTFILES, "dotfiles (existing files are kept as .bak-DATE)", cmds, notes)]


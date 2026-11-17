"""GNOME: shell extensions and selected dconf paths.

snapshot/dconf/<path>.ini holds `dconf dump <path>`, e.g. dconf/org.gnome.shell.ini for /org/gnome/shell/.
"""
from .. import shell
from .base import PHASE_DCONF, PHASE_MANUAL, Step, q

DCONF_DIR = "dconf"


def dconf_file(path):
    return f"{DCONF_DIR}/{path.strip('/').replace('/', '.')}.ini"


def collect(ctx, snap):
    if shell.have("gnome-extensions"):
        def _ext():
            all_ = sorted(shell.lines(["gnome-extensions", "list"]) or [])
            enabled = sorted(shell.lines(["gnome-extensions", "list", "--enabled"]) or [])
            user = sorted(shell.lines(["gnome-extensions", "list", "--user"]) or [])
            text = ["# enabled", *enabled, "", "# installed but disabled",
                    *[e for e in all_ if e not in enabled], "", "# user-installed", *user]
            snap.write_lines("packages/gnome-extensions.txt", text)
        snap.guarded("packages/gnome-extensions.txt", _ext)

    paths = ctx.cfg["dconf_paths"]
    if paths and shell.have("dconf"):
        def _dconf():
            for p in paths:
                out = shell.run(["dconf", "dump", p])
                if out.strip():
                    snap.write(dconf_file(p), out)
        snap.guarded(DCONF_DIR, _dconf)


def user_extensions(ctx):
    section, out = None, []
    try:
        text = (ctx.snapshot / "packages/gnome-extensions.txt").read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    for line in text.splitlines():
        if line.startswith("#"):
            section = line
        elif line.strip() and section == "# user-installed":
            out.append(line.strip())
    return out


def dconf_saved(ctx):
    """{dconf path: file} for the dumps in the snapshot, keyed by the configured paths."""
    out = {}
    for p in ctx.cfg["dconf_paths"]:
        f = ctx.snapshot / dconf_file(p)
        if f.exists():
            out[p] = f
    return out


def drift(ctx, catalog, rep):
    claimed = catalog.claimed("gnome_extensions")
    unclaimed = [e for e in user_extensions(ctx) if e not in claimed]
    if unclaimed:
        rep.warn(f"gnome: uncatalogued user extensions: {' '.join(unclaimed)}")
    claimed = catalog.claimed("dconf")
    unclaimed = [p for p in dconf_saved(ctx) if p not in claimed]
    if unclaimed:
        rep.warn(f"gnome: uncatalogued dconf paths: {' '.join(unclaimed)}")


def steps(ctx, catalog, items):
    out = []
    saved = dconf_saved(ctx)
    cmds, notes = [], []
    for i in items:
        for p in i.get("dconf"):
            if p in saved:
                cmds.append(f"dconf load {q(p)} < {q(saved[p])}")
            else:
                notes.append(f"{p}: no dump in the snapshot (empty, or not in config dconf_paths)")
    if cmds or notes:
        out.append(Step(PHASE_DCONF, "GNOME settings (dconf)", cmds, notes))
    exts = [e for i in items for e in i.get("gnome_extensions")]
    if exts:
        out.append(Step(PHASE_MANUAL, "GNOME extensions",
                        notes=["Install with Extension Manager (or extensions.gnome.org), then enable:",
                               *[f"  {e}" for e in exts]], manual=True))
    return out

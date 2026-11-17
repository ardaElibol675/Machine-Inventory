"""pip --user packages. Drift only reports catalogued packages that are missing:
dependencies would make an "uncatalogued" list useless."""
import re

from .. import shell
from .base import PHASE_PIP, Step, q, read_lines


def collect(ctx, snap):
    if not shell.have("pip3"):
        return
    snap.guarded("packages/pip-user.txt", lambda: snap.write_lines(
        "packages/pip-user.txt",
        sorted(shell.lines(["pip3", "freeze", "--user"]), key=lambda s: (s.upper(), s))))


def _name(spec):
    return re.split(r"[=<>!~\[ ;]", spec, 1)[0].lower().replace("_", "-")


def drift(ctx, catalog, rep):
    have = {_name(l) for l in read_lines(ctx.snapshot / "packages/pip-user.txt")}
    for item in catalog.all_items():
        miss = [p for p in item.get("pip") if _name(p) not in have]
        if miss and item.compatible(ctx.os):
            rep.warn(f"pip: catalog item '{item.id}' is missing: {' '.join(miss)}")


def steps(ctx, catalog, items):
    pkgs = [p for i in items for p in i.get("pip")]
    if not pkgs:
        return []
    return [Step(PHASE_PIP, f"pip --user packages ({len(pkgs)})",
                 [f"pip3 install --user {q(p)}" for p in pkgs], soft=True)]

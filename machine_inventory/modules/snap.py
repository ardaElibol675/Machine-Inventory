"""snap: installed snaps (bases and runtimes are ignored for restore and drift)."""
import re

from .. import shell
from .base import PHASE_SNAP, Step, read_lines

RUNTIME = re.compile(r"^(gnome-|gtk-common-themes|mesa-|snapd-desktop-integration|snap-store|bare$|core\d*$|snapd$)")


def collect(ctx, snap):
    if not shell.have("snap"):
        return

    def _do():
        rows = []
        for line in shell.run(["snap", "list"]).splitlines()[1:]:
            f = line.split()
            if len(f) >= 6:
                rows.append("\t".join([f[0], f[3], f[4], f[5]]))
        snap.write_lines("packages/snap.txt", sorted(rows))

    snap.guarded("packages/snap.txt", _do)


def apps(ctx):
    """Names of app snaps in the snapshot (no bases, no runtimes)."""
    out = []
    for line in read_lines(ctx.snapshot / "packages/snap.txt"):
        f = line.split("\t")
        notes = f[3] if len(f) > 3 else ""
        if "base" in notes or "snapd" in notes or RUNTIME.match(f[0]):
            continue
        out.append(f[0])
    return out


def drift(ctx, catalog, rep):
    claimed = {s.split()[0] for s in catalog.claimed("snap")}
    unclaimed = [s for s in apps(ctx) if s not in claimed]
    if unclaimed:
        rep.warn(f"snap: uncatalogued: {' '.join(unclaimed)}")
    else:
        rep.ok("snap: every app snap is catalogued")
    installed = set(apps(ctx))
    for item in catalog.all_items():
        miss = [s for s in item.get("snap") if s.split()[0] not in installed]
        if miss and item.compatible(ctx.os):
            rep.warn(f"snap: catalog item '{item.id}' is missing: {' '.join(miss)}")


def steps(ctx, catalog, items):
    # An entry may carry flags: "code --classic".
    cmds = [f"sudo snap install {s}" for i in items for s in i.get("snap")]
    return [Step(PHASE_SNAP, f"snaps ({len(cmds)})", cmds)] if cmds else []

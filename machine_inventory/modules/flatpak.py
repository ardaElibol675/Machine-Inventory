"""flatpak: installed apps and remotes. Restore installs from flathub."""
from .. import shell
from .base import PHASE_FLATPAK, Step, q, read_lines

FLATHUB = "https://dl.flathub.org/repo/flathub.flatpakrepo"


def collect(ctx, snap):
    if not shell.have("flatpak"):
        return
    snap.guarded("packages/flatpak.txt", lambda: snap.write_lines(
        "packages/flatpak.txt", sorted(shell.lines(["flatpak", "list", "--app", "--columns=application,origin"]))))
    snap.guarded("packages/flatpak-remotes.txt", lambda: snap.write_lines(
        "packages/flatpak-remotes.txt", sorted(shell.lines(["flatpak", "remotes", "--columns=name,url"]))))


def installed(ctx):
    return [l.split("\t")[0] for l in read_lines(ctx.snapshot / "packages/flatpak.txt")]


def drift(ctx, catalog, rep):
    claimed = catalog.claimed("flatpak")
    have = installed(ctx)
    unclaimed = [a for a in have if a not in claimed]
    if unclaimed:
        rep.warn(f"flatpak: uncatalogued: {' '.join(unclaimed)}")
    else:
        rep.ok("flatpak: every app is catalogued")
    for item in catalog.all_items():
        miss = [a for a in item.get("flatpak") if a not in have]
        if miss and item.compatible(ctx.os):
            rep.warn(f"flatpak: catalog item '{item.id}' is missing: {' '.join(miss)}")


def steps(ctx, catalog, items):
    apps = [a for i in items for a in i.get("flatpak")]
    if not apps:
        return []
    cmds = [f"flatpak remote-add --if-not-exists flathub {q(FLATHUB)}"]
    cmds += [f"flatpak install -y flathub {q(a)}" for a in apps]
    return [Step(PHASE_FLATPAK, f"flatpak apps ({len(apps)})", cmds)]

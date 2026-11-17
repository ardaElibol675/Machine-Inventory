"""VS Code extensions."""
from .. import shell
from .base import PHASE_VSCODE, Step, q, read_lines


def collect(ctx, snap):
    if not shell.have("code"):
        return
    snap.guarded("packages/vscode-extensions.txt", lambda: snap.write_lines(
        "packages/vscode-extensions.txt", sorted(shell.lines(["code", "--list-extensions"]))))


def drift(ctx, catalog, rep):
    have = read_lines(ctx.snapshot / "packages/vscode-extensions.txt")
    low = {e.lower() for e in have}
    claimed = {e.lower() for e in catalog.claimed("vscode")}
    unclaimed = [e for e in have if e.lower() not in claimed]
    if unclaimed:
        rep.warn(f"vscode: uncatalogued extensions: {' '.join(unclaimed)}")
    else:
        rep.ok("vscode: every extension is catalogued")
    for item in catalog.all_items():
        miss = [e for e in item.get("vscode") if e.lower() not in low]
        if miss and item.compatible(ctx.os):
            rep.warn(f"vscode: catalog item '{item.id}' is missing: {' '.join(miss)}")


def steps(ctx, catalog, items):
    exts = [e for i in items for e in i.get("vscode")]
    if not exts:
        return []
    return [Step(PHASE_VSCODE, f"VS Code extensions ({len(exts)})",
                 [f"code --install-extension {q(e)}" for e in exts])]

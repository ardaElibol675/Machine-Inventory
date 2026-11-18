"""General health: disk usage, home layout, state of the tool and inventory repositories."""
import os

from .. import shell
from .base import PHASE_POST, Step


def _git_state(rep, path, label, hint):
    top = shell.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], check=False)
    if top is None or os.path.realpath(top.strip()) != os.path.realpath(path):
        rep.warn(f"{label}: not a git repository yet ({hint})")
        return
    n = len((shell.run(["git", "-C", str(path), "status", "--porcelain"], check=False) or "").splitlines())
    if n:
        rep.warn(f"{label}: {n} uncommitted changes")
    else:
        rep.ok(f"{label}: clean")


def check(ctx, rep):
    rep.section("disk and layout")
    d = ctx.cfg["disk"]
    for mount in d.get("mounts", []):
        try:
            st = os.statvfs(mount)
        except OSError:
            continue
        used = st.f_blocks - st.f_bfree
        total = used + st.f_bavail
        pct = -(-used * 100 // total) if total else 0      # rounded up, like df
        if pct >= d.get("fail", 90):
            rep.fail(f"disk: {mount} is {pct}% full")
        elif pct >= d.get("warn", 85):
            rep.warn(f"disk: {mount} is {pct}% full")
        else:
            rep.ok(f"disk: {mount} is {pct}% full")

    allowed = set(ctx.cfg["home_layout"])
    if allowed:
        stray = sorted(e for e in os.listdir(ctx.home) if not e.startswith(".") and e not in allowed)
        for e in stray:
            rep.warn(f"home layout: ~/{e} is outside the layout ({', '.join(sorted(allowed))})")
        if not stray:
            rep.ok("home layout: tidy")

    rep.section("inventory repositories")
    _git_state(rep, ctx.tool_root, "tool repo", "see README")
    if ctx.data.exists():
        _git_state(rep, ctx.data, "inventory repo", f"cd {ctx.data.name} && git init")


def steps(ctx, catalog, items):
    cmds = [c for i in items for c in i.get("post")]
    return [Step(PHASE_POST, "post-install commands", cmds)] if cmds else []

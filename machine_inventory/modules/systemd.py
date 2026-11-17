"""systemd: your own unit files (system units no package owns, and user units)."""
from pathlib import Path

from .. import shell
from .base import PHASE_SYSTEMD, Step, q
from .etc import dpkg_owned_etc

SYSTEM_DIR = Path("/etc/systemd/system")
SUFFIXES = (".service", ".timer", ".path")


def _user_dir(ctx):
    return ctx.home / ".config/systemd/user"


def collect(ctx, snap):
    owned = dpkg_owned_etc()
    for f in sorted(SYSTEM_DIR.glob("*")):
        if f.suffix not in SUFFIXES or f.is_symlink() or not f.is_file():
            continue
        if f.name.startswith(("snap.", "snap-")) or str(f) in owned:
            continue
        snap.copy(f, f"systemd/system/{f.name}", mode=0o644)
    for f in sorted(_user_dir(ctx).glob("*")):
        if f.suffix in (".service", ".timer") and f.is_file():
            snap.copy(f, f"systemd/user/{f.name}", mode=0o644)


def saved(ctx):
    """['system/x.service', 'user/y.service', ...] from the snapshot."""
    out = []
    for scope in ("system", "user"):
        d = ctx.snapshot / "systemd" / scope
        if d.exists():
            out += [f"{scope}/{p.name}" for p in sorted(d.iterdir()) if p.is_file()]
    return out


def _state(scope, unit):
    # is-enabled / is-active exit non-zero for disabled / inactive units; we only want their output.
    base = "systemctl --user" if scope == "user" else "systemctl"
    enabled = (shell.run(f"{base} is-enabled {q(unit)} || true", check=False) or "").strip()
    active = (shell.run(f"{base} is-active {q(unit)} || true", check=False) or "").strip()
    return enabled or "unknown", active or "unknown"


def check(ctx, rep):
    rep.section("systemd")
    for scope in ("system", "user"):
        flag = "--user " if scope == "user" else ""
        failed = shell.run(f"systemctl {flag}--failed --no-legend --plain | awk '{{print $1}}'", check=False)
        failed = (failed or "").split()
        if failed:
            rep.fail(f"systemd ({scope}): failed units: {' '.join(failed)}")
        else:
            rep.ok(f"systemd ({scope}): no failed units")

    expected_off = set(ctx.cfg["expected_disabled"])
    for entry in saved(ctx):
        scope, unit = entry.split("/", 1)
        if not unit.endswith(".service"):
            continue
        enabled, active = _state(scope, unit)
        journal = f"journalctl {'--user ' if scope == 'user' else ''}-b -u {unit}"
        label = f"own unit ({scope}): {unit}"
        if unit in expected_off:
            if enabled == "enabled":
                rep.warn(f"{label} is enabled but listed in expected_disabled")
            else:
                rep.ok(f"{label} disabled on purpose ({enabled}, {active})")
        elif enabled != "enabled":
            rep.warn(f"{label} is not enabled ({enabled}, {active})")
        elif active == "failed":
            rep.fail(f"{label} failed ({journal})")
        else:
            # A oneshot unit is "inactive" after it finished; that is fine.
            rep.ok(f"{label} ({enabled}, {active})")


def drift(ctx, catalog, rep):
    claimed = catalog.claimed("systemd")
    unclaimed = [u for u in saved(ctx) if u not in claimed]
    if unclaimed:
        rep.warn(f"systemd: uncatalogued units: {' '.join(unclaimed)}")
    else:
        rep.ok("systemd: every own unit is catalogued")


def steps(ctx, catalog, items):
    cmds, enables, notes = [], [], []
    expected_off = set(ctx.cfg["expected_disabled"])
    have = set(saved(ctx))
    scopes = set()
    for i in items:
        for entry in i.get("systemd"):
            if entry not in have:
                notes.append(f"{entry} is not in the snapshot")
                continue
            scope, unit = entry.split("/", 1)
            scopes.add(scope)
            src = ctx.snapshot / "systemd" / entry
            if scope == "system":
                cmds.append(f"sudo install -m 644 {q(src)} {q(SYSTEM_DIR / unit)}")
            else:
                cmds.append(f"install -D -m 644 {q(src)} {q(_user_dir(ctx) / unit)}")
            if unit in expected_off:
                notes.append(f"{unit}: disabled on purpose (expected_disabled); installed, not enabled")
            else:
                enables.append(("sudo systemctl" if scope == "system" else "systemctl --user") + f" enable {q(unit)}")
    if "system" in scopes:
        cmds.append("sudo systemctl daemon-reload")
    if "user" in scopes:
        cmds.append("systemctl --user daemon-reload")
    cmds += enables
    if not (cmds or notes):
        return []
    return [Step(PHASE_SYSTEMD, "systemd units", cmds, notes)]


"""systemd: your own unit files (system units no package owns, and user units).

Units are saved as templates, not as plain copies: your home directory becomes @HOME@ and
`User=`/`Group=` lines naming you become @USER@/@GROUP@. Restore fills them in for the user
running it, so a unit like `ExecStart=@HOME@/Services/app/run.sh` works for any username.
"""
import os
import re
from pathlib import Path

from .. import shell
from .base import PHASE_SYSTEMD, Step, q
from .etc import dpkg_owned_etc

SYSTEM_DIR = Path("/etc/systemd/system")
SUFFIXES = (".service", ".timer", ".path")
HOME, USER, GROUP = "@HOME@", "@USER@", "@GROUP@"


def _user_dir(ctx):
    return ctx.home / ".config/systemd/user"


def to_template(text, home, user, group):
    """Replace this machine's home path and user/group names with placeholders."""
    text = re.sub(rf"^User={re.escape(user)}\s*$", f"User={USER}", text, flags=re.M)
    text = re.sub(rf"^Group={re.escape(group)}\s*$", f"Group={GROUP}", text, flags=re.M)
    return re.sub(re.escape(str(home)) + r"(?=[/\s'\"]|$)", HOME, text, flags=re.M)


def from_template(text, home, user, group):
    return text.replace(HOME, str(home)).replace(USER, user).replace(GROUP, group)


def _install_cmd(src, dst, sudo):
    """Shell line that fills in the placeholders for the user running restore and writes dst."""
    fill = (f"sed -e \"s|{HOME}|$HOME|g\" -e \"s|{USER}|$(id -un)|g\" -e \"s|{GROUP}|$(id -gn)|g\" {q(src)}")
    if sudo:
        return f"{fill} | sudo tee {q(dst)} >/dev/null"
    return f"mkdir -p {q(dst.parent)} && {fill} > {q(dst)}"


def collect(ctx, snap):
    import grp
    import pwd
    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name

    def save(f, rel):
        text = f.read_text(encoding="utf-8", errors="replace")
        snap.write(rel, to_template(text, ctx.home, user, group))

    owned = dpkg_owned_etc()
    for f in sorted(SYSTEM_DIR.glob("*")):
        if is_own_system_unit(f, owned):
            save(f, f"systemd/system/{f.name}")
    for f in sorted(_user_dir(ctx).glob("*")):
        if f.suffix in (".service", ".timer") and f.is_file():
            save(f, f"systemd/user/{f.name}")


def is_own_system_unit(f, owned=None):
    f = Path(f)
    if f.parent != SYSTEM_DIR or f.suffix not in SUFFIXES or f.is_symlink() or not f.is_file():
        return False
    if f.name.startswith(("snap.", "snap-")):
        return False
    return str(f) not in (owned if owned is not None else dpkg_owned_etc())


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
                cmds.append(_install_cmd(src, SYSTEM_DIR / unit, sudo=True))
            else:
                cmds.append(_install_cmd(src, _user_dir(ctx) / unit, sudo=False))
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
    return [Step(PHASE_SYSTEMD, "systemd units (@HOME@/@USER@ filled in for you)", cmds, notes)]


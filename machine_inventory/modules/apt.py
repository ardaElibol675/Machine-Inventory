"""apt: manually installed packages, install history, local .debs, extra sources and keyrings."""
import glob
import gzip
import os
import re
from pathlib import Path

from .. import shell
from .base import PHASE_APT, PHASE_SOURCES, Step, q, read_lines

SOURCES_DIR = Path("/etc/apt/sources.list.d")
INITIAL_STATUS = Path("/var/log/installer/initial-status.gz")
EXTENDED_STATES = Path("/var/lib/apt/extended_states")
SIGNED_BY = re.compile(r"signed-by=([^\]\s]+)|Signed-By:\s*(\S+)")


# --- facts -----------------------------------------------------------------

def installed_packages():
    """{(name, arch)} of installed packages, read from dpkg (works even if apt's lists are broken)."""
    out = shell.run(["dpkg-query", "-W", "-f=${Package}\t${Architecture}\t${db:Status-Abbrev}\n"])
    pkgs = set()
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2][:2] in ("ii", "hi"):
            pkgs.add((parts[0], parts[1]))
    return pkgs


def installed_names():
    return {p for p, _ in installed_packages()}


def manual_packages():
    """Same result as `apt-mark showmanual`, without needing apt's package lists."""
    native = shell.run(["dpkg", "--print-architecture"]).strip()
    auto = set()
    text = EXTENDED_STATES.read_text(encoding="utf-8") if EXTENDED_STATES.exists() else ""
    for block in text.split("\n\n"):
        d = dict(l.split(": ", 1) for l in block.splitlines() if ": " in l)
        if d.get("Auto-Installed") == "1":
            auto.add((d.get("Package"), d.get("Architecture")))
    out = set()
    for name, arch in installed_packages():
        key_arch = native if arch == "all" else arch
        if (name, arch) in auto or (name, key_arch) in auto:
            continue
        out.add(name if arch in (native, "all") else f"{name}:{arch}")
    return sorted(out)


def initial_packages():
    with gzip.open(INITIAL_STATUS, "rt", encoding="utf-8", errors="replace") as f:
        return {l.split()[1] for l in f if l.startswith("Package:")}


def history():
    """'date | command' for every install/remove/purge in apt's history, oldest first."""
    files = sorted(glob.glob("/var/log/apt/history.log*"), key=lambda p: os.stat(p).st_mtime)
    rows = []
    for path in files:
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as f:
                date = ""
                for line in f:
                    line = line.rstrip("\n")
                    if line.startswith("Start-Date:"):
                        parts = line.split()
                        date = parts[1] if len(parts) > 1 else ""
                    elif line.startswith("Commandline:"):
                        cmd = line[len("Commandline: "):]
                        if re.search(r"install|remove|purge", cmd) and "pkgDepCache::AutoInstall" not in cmd:
                            rows.append(f"{date} | {cmd}")
        except OSError:
            continue
    return rows


def local_packages():
    """Installed packages that no configured repository provides (needs readable apt lists)."""
    out = shell.run(["apt", "list", "--installed"])
    return sorted({l.split("/")[0] for l in out.splitlines() if "[installed,local]" in l})


def source_files():
    return sorted(p for p in SOURCES_DIR.glob("*") if p.suffix in (".list", ".sources") and p.is_file())


def keyrings_of(text):
    return [a or b for a, b in SIGNED_BY.findall(text)]


# --- collect ---------------------------------------------------------------

def collect(ctx, snap):
    manual = []

    def _manual():
        manual.extend(manual_packages())
        snap.write_lines("packages/apt-manual.txt", manual)

    snap.guarded("packages/apt-manual.txt", _manual)
    snap.guarded("packages/apt-history.txt", lambda: snap.write_lines("packages/apt-history.txt", history()))
    if INITIAL_STATUS.exists() and manual:
        snap.guarded("packages/apt-user-added.txt", lambda: snap.write_lines(
            "packages/apt-user-added.txt", [p for p in manual if p not in initial_packages()]))
    elif INITIAL_STATUS.exists():
        snap.carry_over("packages/apt-user-added.txt", "apt-manual could not be collected")
    snap.guarded("packages/apt-local.txt", lambda: snap.write_lines("packages/apt-local.txt", local_packages()))

    # Extra sources and the public keys they use; restored to the same paths.
    keyrings = set()
    for f in source_files():
        snap.copy(f, f"apt-sources/{f.name}", mode=0o644)
        keyrings.update(keyrings_of(f.read_text(encoding="utf-8", errors="replace")))
    snap.write_lines("apt-sources/keyrings.txt", sorted(keyrings))
    extra = [p for p in glob.glob("/etc/apt/trusted.gpg.d/*.gpg") if "/ubuntu-keyring-" not in p]
    for k in sorted(keyrings) + sorted(extra):
        if os.path.isfile(k):
            snap.copy(k, f"apt-sources/keyrings{k}", mode=0o644)


# --- check -----------------------------------------------------------------

def check(ctx, rep):
    rep.section("apt")
    sim = shell.run(["apt-get", "-s", "autoremove"], check=False)
    if sim is None:
        err = shell.run("apt-get -s autoremove 2>&1 >/dev/null | head -1", check=False) or ""
        rep.fail(f"apt: cannot read its package lists ({err.strip()}). Try: sudo apt update")
    else:
        rm = [l.split()[1] for l in sim.splitlines() if l.startswith("Remv")]
        if rm:
            rep.warn(f"apt: waiting for autoremove: {' '.join(rm)} (simulate first: apt-get -s autoremove)")
        else:
            rep.ok("apt: nothing to autoremove")

        known = set(ctx.cfg["known_local_debs"])
        try:
            unknown = [p for p in local_packages() if p not in known]
        except shell.CommandError as e:
            unknown = None
            rep.warn(f"apt: could not list local packages: {e}")
        if unknown:
            more = " ..." if len(unknown) > 6 else ""
            rep.warn(f"apt: {len(unknown)} installed packages have no repository (no updates): "
                     f"{' '.join(unknown[:6])}{more}")
        elif unknown is not None:
            rep.ok("apt: every package has a repository (known local .debs excepted)")

        up = shell.run(["apt", "list", "--upgradable"], check=False) or ""
        n = sum(1 for l in up.splitlines() if "/" in l)
        if n:
            rep.warn(f"apt: {n} packages can be upgraded (apt list --upgradable)")
        else:
            rep.ok("apt: no pending upgrades")

    rc = [l.split()[1] for l in (shell.run(["dpkg", "-l"], check=False) or "").splitlines()
          if l.startswith("rc ")]
    if rc:
        rep.warn(f"apt: config files left by removed packages (rc): {' '.join(rc)}")
    else:
        rep.ok("apt: no rc packages")

    missing = 0
    for f in source_files():
        for k in keyrings_of(f.read_text(encoding="utf-8", errors="replace")):
            if not os.path.isfile(k):
                missing += 1
                rep.fail(f"apt: source {f.name} points to a missing key: {k}")
    if not missing:
        rep.ok("apt: every source's key is present")


def drift(ctx, catalog, rep):
    added = read_lines(ctx.snapshot / "packages/apt-user-added.txt")
    claimed = catalog.claimed("apt")
    ignore = claimed | set(ctx.cfg["restore_exclude"]) | set(ctx.cfg["known_local_debs"])
    unclaimed = [p for p in added if p not in ignore]
    if unclaimed:
        rep.warn(f"apt: {len(unclaimed)} uncatalogued packages: {' '.join(unclaimed)}")
    else:
        rep.ok("apt: every user-added package is catalogued")
    try:
        installed = installed_names()
    except shell.CommandError as e:
        rep.warn(f"apt: could not read installed packages: {e}")
        return
    for item in catalog.all_items():
        if not item.compatible(ctx.os):
            continue
        miss = [p for p in item.get("apt") if p not in installed]
        if miss:
            rep.warn(f"apt: catalog item '{item.id}' is missing: {' '.join(miss)}")


# --- restore ---------------------------------------------------------------

def _rewrite_signed_by(text, keyring):
    """Add signed-by=<keyring> to one-line deb entries that have none (legacy apt-key repos)."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(("deb ", "deb-src ")) and "signed-by=" not in s:
            kind, rest = s.split(None, 1)
            if rest.startswith("["):
                opts, rest = rest[1:].split("]", 1)
                line = f"{kind} [{opts.strip()} signed-by={keyring}]{rest}"
            else:
                line = f"{kind} [signed-by={keyring}] {rest}"
        out.append(line)
    return out


def steps(ctx, catalog, items):
    out = []
    src_dir = ctx.snapshot / "apt-sources"
    source_ids = []
    for i in items:
        for s in i.get("apt_sources"):
            if s not in source_ids:
                source_ids.append(s)

    cmds, notes = [], []
    for sid in source_ids:
        s = catalog.apt_sources[sid]
        keyring = s.get("keyring")
        if s.get("key_url"):
            cmds.append(f"curl -fsSL {q(s['key_url'])} | sudo tee {q(keyring)} >/dev/null")
        elif keyring:
            saved = src_dir / ("keyrings" + keyring)
            if saved.exists():
                cmds.append(f"sudo install -D -m 644 {q(saved)} {q(keyring)}")
            else:
                notes.append(f"{sid}: keyring {keyring} is not in the snapshot")
        if s.get("line"):
            # A literal entry; `file` (optional) only names the target file.
            name = s.get("file") or f"{sid}.list"
            lines = _rewrite_signed_by(s["line"], keyring) if keyring else [s["line"]]
        else:
            name = s["file"]
            path = src_dir / name
            if not path.exists():
                notes.append(f"{sid}: {name} is not in the snapshot; skipped")
                continue
            if not (s.get("key_url") and name.endswith(".list")):
                cmds.append(f"sudo install -m 644 {q(path)} {q(SOURCES_DIR / name)}")
                continue
            lines = _rewrite_signed_by(path.read_text(encoding="utf-8"), keyring)
        cmds.append(f"printf '%s\\n' {' '.join(q(l) for l in lines)} | sudo tee {q(SOURCES_DIR / name)} >/dev/null")
    if cmds or notes:
        out.append(Step(PHASE_SOURCES, f"apt sources: {', '.join(source_ids)}", cmds, notes))

    exclude = set(ctx.cfg["restore_exclude"])
    pkgs = []
    for i in items:
        for p in i.get("apt"):
            if p not in exclude and p not in pkgs:
                pkgs.append(p)
    if pkgs or cmds:
        out.append(Step(PHASE_APT, "apt update", ["sudo apt update"]))
    if pkgs:
        out.append(Step(PHASE_APT, f"apt packages ({len(pkgs)})",
                        [f"{q(ctx.tool_root / 'mi')} apt-install " + " ".join(q(p) for p in pkgs)],
                        ["packages missing from this system's repositories are skipped with a warning; "
                         "if the batch fails, the rest are installed one by one"],
                        soft=True))
    return out


def located(pkgs):
    """The subset of `pkgs` that apt knows at all, or None if apt's lists cannot be read.

    `apt-cache policy` prints a "name:" block for every package it knows (virtual packages
    included) and nothing for unknown names.
    """
    out = shell.run(["apt-cache", "policy", *pkgs], check=False)
    if out is None:
        return None
    known = {l[:-1] for l in out.splitlines() if l and not l[0].isspace() and l.endswith(":")}
    return {p for p in pkgs if p in known}


def install_packages(pkgs, run=None, out=print, locate=located):
    """Install as many of `pkgs` as possible. Returns (skipped, failed).

    Unknown packages are skipped up front. If the remaining batch fails, every package is
    tried on its own, so one broken package never costs the whole list.
    """
    import subprocess
    run = run or (lambda args: subprocess.run(args).returncode)
    known = locate(pkgs)
    skipped = [] if known is None else [p for p in pkgs if p not in known]
    for p in skipped:
        out(f"WARN   {p}: not in this system's apt repositories; skipped")
    todo = [p for p in pkgs if p not in skipped]
    failed = []
    if todo and run(["sudo", "apt", "install", "-y", *todo]) != 0:
        out("WARN   installing the batch failed; trying the packages one by one")
        for p in todo:
            if run(["sudo", "apt", "install", "-y", p]) != 0:
                failed.append(p)
                out(f"WARN   {p}: could not be installed; skipped")
    installed = len(todo) - len(failed)
    out(f"apt: {installed} installed or already present, {len(skipped)} not found, {len(failed)} failed")
    return skipped, failed

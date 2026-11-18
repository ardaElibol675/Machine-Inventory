"""Shared types for modules.

A module may define any of:
    collect(ctx, snap)          write facts about this machine into the snapshot
    check(ctx, rep)             health checks (live system + snapshot)
    drift(ctx, catalog, rep)    snapshot vs catalog: uncatalogued and missing things
    steps(ctx, catalog, items)  restore steps for the selected catalog items
"""
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..shell import CommandError

# Restore phases, in the order they run.
PHASE_SOURCES = 10
PHASE_APT = 20
PHASE_SNAP = 30
PHASE_FLATPAK = 31
PHASE_VSCODE = 40
PHASE_PIP = 41
PHASE_REPOS = 50
PHASE_DOTFILES = 60
PHASE_SYSTEMD = 70
PHASE_ETC = 80
PHASE_DCONF = 85
PHASE_POST = 90
PHASE_MANUAL = 99


@dataclass
class Step:
    phase: int
    title: str
    commands: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    manual: bool = False          # printed only, never executed
    soft: bool = False            # a failure is recorded and skipped without asking (installs that
                                  # nothing else depends on); otherwise mi asks retry/skip/abort


# Exit code of a command that finished but skipped some of its work (e.g. `mi apt-install`).
PARTIAL = 3


class Report:
    """Collects OK / WARN / FAIL lines. FAIL makes `mi check` exit 1."""

    def __init__(self, quiet=False, out=print):
        self.quiet = quiet
        self.out = out
        self.failed = False
        self.counts = {"OK": 0, "WARN": 0, "FAIL": 0}

    def _emit(self, level, msg):
        self.counts[level] += 1
        if level == "OK" and self.quiet:
            return
        self.out(f"{level:<6} {msg}")

    def ok(self, msg):
        self._emit("OK", msg)

    def warn(self, msg):
        self._emit("WARN", msg)

    def fail(self, msg):
        self.failed = True
        self._emit("FAIL", msg)

    def section(self, title):
        if not self.quiet:
            self.out(f"\n## {title}")


class Snapshot:
    """Writes one new snapshot into a temporary directory.

    If a fact cannot be collected (a command fails), the file from the previous
    snapshot is carried over and a warning is recorded, so a broken tool never
    shows up as "everything was removed".
    """

    def __init__(self, root, previous):
        self.root = Path(root)
        self.previous = Path(previous)
        self.warnings = []
        self.written = []

    def path(self, rel):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def write(self, rel, text):
        if text and not text.endswith("\n"):
            text += "\n"
        self.path(rel).write_text(text, encoding="utf-8")
        self.written.append(rel)

    def write_lines(self, rel, lines):
        self.write(rel, "\n".join(lines))

    def copy(self, src, rel, mode=None):
        dst = self.path(rel)
        shutil.copyfile(src, dst)
        if mode is None:
            mode = 0o755 if os.access(src, os.X_OK) else 0o644
        os.chmod(dst, mode)

    def carry_over(self, rel, err):
        """Keep the previous version of `rel` (a file or a directory) after a failure."""
        self.warnings.append(f"{rel}: {err} (kept the previous version)")
        old = self.previous / rel
        if old.is_dir():
            shutil.copytree(old, self.root / rel, dirs_exist_ok=True)
        elif old.exists():
            self.path(rel)
            shutil.copy2(old, self.root / rel)

    def guarded(self, rel, fn):
        """Run fn(); on a command failure keep the previous `rel`."""
        try:
            fn()
        except (CommandError, OSError) as e:
            self.carry_over(rel, e)


def read_lines(path):
    """Non-empty, non-comment lines of a text file; [] if it does not exist."""
    try:
        with open(path, encoding="utf-8") as f:
            return [l.rstrip("\n") for l in f if l.strip() and not l.lstrip().startswith("#")]
    except FileNotFoundError:
        return []


def q(s):
    """Shell-quote."""
    import shlex
    return shlex.quote(str(s))


def backup_install(src, dst, mode="644", sudo=False):
    """Shell line: keep a differing existing file as <dst>.bak-YYYYMMDD, then install src."""
    s = "sudo " if sudo else ""
    return (f"if [ -e {q(dst)} ] && ! cmp -s {q(src)} {q(dst)}; then "
            f"{s}cp -a {q(dst)} {q(str(dst) + '.bak-')}$(date +%Y%m%d); fi && "
            f"{s}install -D -m {mode} {q(src)} {q(dst)}")


def missing_items(ctx, catalog, rep, key, present, label, hint=""):
    """Warn about catalog items (for this release and hardware) whose `key` entries are not on this machine.

    present(entry) -> bool. The mirror image of the "uncatalogued" checks: after a restore or an adopt it shows
    what the catalog promises but the snapshot does not have.
    """
    for item in catalog.all_items():
        if not item.compatible(ctx.os):
            continue
        miss = [e for e in item.get(key) if not present(e)]
        if miss:
            rep.warn(f"{label}: catalog item '{item.id}' is missing: {' '.join(miss)}{hint}")

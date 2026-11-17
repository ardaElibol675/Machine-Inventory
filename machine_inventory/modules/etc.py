"""/etc: conffiles you changed and files you added that no package owns.

snapshot/etc/changes.tsv   path <TAB> status [<TAB> note]
snapshot/etc/files/<path>  copies of the readable, non-secret ones (path relative to /etc)

Files that may hold secrets are never copied (built-in denylist + config etc_secret_patterns).
"""
import glob
import hashlib
import os
import re
import stat
from functools import lru_cache
from pathlib import Path

from .. import shell
from .base import PHASE_ETC, Step, backup_install, read_lines

MEDIA_INFO = Path("/var/log/installer/media-info")

# Generated or per-machine files that are not "your changes".
NOISE = (
    r"^/etc/(ssl/certs|alternatives|rc[0-6S]\.d|fonts|ca-certificates|ld\.so\.cache|apparmor\.d/(cache|local|tunables)"
    r"|console-setup|cups/(subscriptions|printers|classes)|machine-id|\.pwd\.lock|mtab|adjtime|localtime|timezone"
    r"|resolv\.conf|udev/hwdb\.bin|dkms|apt/(sources\.list\.d|keyrings|trusted\.gpg)|apt/sources\.list"
    r"|initramfs-tools/conf\.d/resume|\.java|printcap|sgml|xml|mailcap|odbcinst\.ini|locale\.gen|default/locale"
    r"|systemd/system/(snap[.-]|dbus-|display-manager|syslog|sudo)|systemd/user/snap\."
    r"|systemd/(system|user)/[^/]+\.(wants|requires)/|NetworkManager/system-connections"
    r"|(passwd|group|shadow|gshadow)-?$|pam\.d/common-|udev/rules\.d/70-snap\.|default/grub\.prime-backup|whoopsie$)"
)
BACKUP = r"(\.save|\.bak|\.old|~)$"
# Never copied: credentials, keys, network secrets, account databases.
SECRET = (
    r"^/etc/(pam\.d/|shadow|gshadow|passwd|group|subuid|subgid|sudoers|ssh/|NetworkManager/|wpa_supplicant|ppp/"
    r"|chatscripts/|brlapi\.key|fstab|crypttab|security/)|\.(key|pem)$|" + BACKUP
)

ADDED, MODIFIED, DELETED = "added", "modified", "deleted"
UNREADABLE = "modified? (unreadable, needs root)"


@lru_cache(maxsize=1)
def dpkg_owned_etc():
    owned = set()
    for path in glob.glob("/var/lib/dpkg/info/*.list"):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                owned.update(l.rstrip("\n") for l in f if l.startswith("/etc/"))
        except OSError:
            continue
    return frozenset(owned)


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def changed_conffiles():
    out = shell.run(["dpkg-query", "-W", "-f=${Conffiles}\n"])
    seen = {}
    for line in out.splitlines():
        f = line.split()
        if len(f) < 2 or not f[0].startswith("/etc/") or (len(f) > 2 and f[2] == "obsolete"):
            continue
        seen.setdefault(f[0], f[1])
    result = {}
    for path, md5 in seen.items():
        if not os.path.lexists(path):
            result[path] = DELETED
            continue
        try:
            if _md5(path) != md5:
                result[path] = MODIFIED
        except IsADirectoryError:
            continue
        except OSError:
            result[path] = UNREADABLE
    return result


def added_files(extra_ignore):
    """Regular files in /etc newer than the OS install that no package owns."""
    if not MEDIA_INFO.exists():
        return {}
    since = MEDIA_INFO.stat().st_mtime
    dev = os.stat("/etc").st_dev
    owned = dpkg_owned_etc()
    ignore = re.compile("|".join([NOISE, BACKUP] + list(extra_ignore)))
    result = {}
    for root, dirs, files in os.walk("/etc", onerror=lambda e: None):
        dirs[:] = [d for d in dirs if _same_dev(os.path.join(root, d), dev)]
        for name in files:
            p = os.path.join(root, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_mtime <= since:
                continue
            if p in owned or ignore.search(p):
                continue
            result[p] = ADDED
    return result


def _same_dev(path, dev):
    try:
        st = os.lstat(path)
        return stat.S_ISDIR(st.st_mode) and st.st_dev == dev
    except OSError:
        return False


def collect(ctx, snap):
    secret = re.compile("|".join([SECRET] + list(ctx.cfg["etc_secret_patterns"])))

    def _do():
        changes = changed_conffiles()
        for p, s in added_files(ctx.cfg["etc_ignore_patterns"]).items():
            changes.setdefault(p, s)
        rows = []
        for p in sorted(changes):
            status, note = changes[p], ""
            if status == DELETED:
                pass
            elif secret.search(p):
                note = "not copied (may contain secrets)"
            elif os.path.islink(p):
                note = f"symlink -> {os.readlink(p)}"
            elif os.path.isfile(p) and os.access(p, os.R_OK):
                snap.copy(p, f"etc/files/{p[len('/etc/'):]}", mode=0o644)
            else:
                note = "not copied (unreadable)"
            rows.append("\t".join([p, status] + ([note] if note else [])))
        snap.write_lines("etc/changes.tsv", rows)

    snap.guarded("etc", _do)


def changes(ctx):
    return [l.split("\t") for l in read_lines(ctx.snapshot / "etc/changes.tsv")]


def saved(ctx):
    root = ctx.snapshot / "etc/files"
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def check(ctx, rep):
    rep.section("/etc")
    clean = True
    for r in ctx.cfg["residue_checks"]:
        path = r.get("path")
        if not path or not os.path.lexists(path):
            continue
        if r.get("requires_command") and not shell.have(r["requires_command"]):
            clean = False
            rep.warn(f"/etc residue: {path} ({r.get('note') or r['requires_command'] + ' is not installed'})")
        elif r.get("requires_path") and not os.path.exists(r["requires_path"]):
            clean = False
            rep.warn(f"/etc residue: {path} ({r.get('note') or r['requires_path'] + ' does not exist'})")
    if clean:
        rep.ok("/etc: no residue of removed programs")


def drift(ctx, catalog, rep):
    claimed = catalog.claimed("etc")
    # Own unit files under /etc/systemd/system are handled by the systemd module.
    units = {f"systemd/{u}" for u in catalog.claimed("systemd") if u.startswith("system/")}
    unclaimed = [p for p in saved(ctx) if p not in claimed and p not in units]
    if unclaimed:
        rep.warn(f"/etc: uncatalogued changes: {' '.join(unclaimed)}")
    else:
        rep.ok("/etc: every saved change is catalogued")


def steps(ctx, catalog, items):
    cmds, notes = [], []
    have = set(saved(ctx))
    for i in items:
        for rel in i.get("etc"):
            if rel not in have:
                notes.append(f"/etc/{rel} is not in the snapshot; skipped")
                continue
            cmds.append(backup_install(ctx.snapshot / "etc/files" / rel, Path("/etc") / rel, sudo=True))
    if not (cmds or notes):
        return []
    return [Step(PHASE_ETC, "/etc files (existing files are kept as .bak-DATE)", cmds, notes)]

"""GNOME: shell extensions and selected dconf paths.

snapshot/dconf/<path>.ini holds `dconf dump <path>`, e.g. dconf/org.gnome.shell.ini for /org/gnome/shell/.
"""
import ast
import json
import re
import tempfile
import urllib.request
from pathlib import Path

from .. import shell
from .base import PHASE_DCONF, Step, q

EGO = "https://extensions.gnome.org"

DCONF_DIR = "dconf"


def dconf_file(path):
    return f"{DCONF_DIR}/{path.strip('/').replace('/', '.')}.ini"


def collect(ctx, snap):
    if shell.have("gnome-extensions"):
        def _ext():
            all_ = sorted(shell.lines(["gnome-extensions", "list"]) or [])
            enabled = sorted(shell.lines(["gnome-extensions", "list", "--enabled"]) or [])
            user = sorted(shell.lines(["gnome-extensions", "list", "--user"]) or [])
            text = ["# enabled", *enabled, "", "# installed but disabled",
                    *[e for e in all_ if e not in enabled], "", "# user-installed", *user]
            snap.write_lines("packages/gnome-extensions.txt", text)
        snap.guarded("packages/gnome-extensions.txt", _ext)

    paths = ctx.cfg["dconf_paths"]
    if paths and shell.have("dconf"):
        def _dconf():
            for p in paths:
                out = shell.run(["dconf", "dump", p])
                if out.strip():
                    snap.write(dconf_file(p), out)
        snap.guarded(DCONF_DIR, _dconf)


def user_extensions(ctx):
    section, out = None, []
    try:
        text = (ctx.snapshot / "packages/gnome-extensions.txt").read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    for line in text.splitlines():
        if line.startswith("#"):
            section = line
        elif line.strip() and section == "# user-installed":
            out.append(line.strip())
    return out


def dconf_saved(ctx):
    """{dconf path: file} for the dumps in the snapshot, keyed by the configured paths."""
    out = {}
    for p in ctx.cfg["dconf_paths"]:
        f = ctx.snapshot / dconf_file(p)
        if f.exists():
            out[p] = f
    return out


def drift(ctx, catalog, rep):
    claimed = catalog.claimed("gnome_extensions")
    unclaimed = [e for e in user_extensions(ctx) if e not in claimed]
    if unclaimed:
        rep.warn(f"gnome: uncatalogued user extensions: {' '.join(unclaimed)}")
    claimed = catalog.claimed("dconf")
    unclaimed = [p for p in dconf_saved(ctx) if p not in claimed]
    if unclaimed:
        rep.warn(f"gnome: uncatalogued dconf paths: {' '.join(unclaimed)}")


def steps(ctx, catalog, items):
    out = []
    saved = dconf_saved(ctx)
    cmds, notes = [], []
    for i in items:
        for p in i.get("dconf"):
            if p in saved:
                cmds.append(f"dconf load {q(p)} < {q(saved[p])}")
            else:
                notes.append(f"{p}: no dump in the snapshot (empty, or not in config dconf_paths)")
    if cmds or notes:
        out.append(Step(PHASE_DCONF, "GNOME settings (dconf)", cmds, notes))
    exts = [e for i in items for e in i.get("gnome_extensions")]
    if exts:
        # Before the dconf step, so extension settings land next to installed extensions.
        out.append(Step(PHASE_DCONF - 1, "GNOME extensions (from extensions.gnome.org)",
                        [f"{q(ctx.tool_root / 'mi')} gnome-extensions " + " ".join(q(e) for e in exts)],
                        ["installed for this user and enabled; GNOME loads them after you log out and back in",
                         "an extension without a build for this GNOME Shell version is skipped with a warning"],
                        soft=True))
    return out


# --- install (`mi gnome-extensions`, used by restore plans) -----------------

def shell_major(version_text):
    """'GNOME Shell 50.1' -> '50' (None if it cannot be read)."""
    m = re.search(r"(\d+)(?:\.\d+)*", version_text or "")
    return m.group(1) if m else None


def parse_strv(text):
    """A gsettings string array ("['a', 'b']" or "@as []") -> list."""
    text = (text or "").strip()
    if text.startswith("@as"):
        text = text[3:].strip()
    try:
        value = ast.literal_eval(text) if text else []
    except (ValueError, SyntaxError):
        return []
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else []


def format_strv(values):
    return "[" + ", ".join("'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'" for v in values) + "]"


def _fetch_json(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def _download(url, dest):
    with urllib.request.urlopen(url, timeout=120) as r:
        Path(dest).write_bytes(r.read())


def install_extensions(uuids, run=shell.run, fetch_json=_fetch_json, download=_download, out=print):
    """Install `uuids` from extensions.gnome.org for the running GNOME Shell and enable them.

    Returns the uuids that were not installed. Enabling goes through the org.gnome.shell
    enabled-extensions key, because a running shell only sees new extensions after a re-login.
    """
    major = shell_major(run(["gnome-shell", "--version"], check=False))
    if not major:
        out("WARN   gnome-shell not found; no extensions installed")
        return list(uuids)
    installed = set((run(["gnome-extensions", "list"], check=False) or "").split())
    missing, enable = [], []
    for uuid in uuids:
        if uuid in installed:
            out(f"OK     {uuid}: already installed")
            enable.append(uuid)
            continue
        try:
            info = fetch_json(f"{EGO}/extension-info/?uuid={uuid}")
        except Exception as e:                       # network, 404 for an unknown uuid, bad JSON
            out(f"WARN   {uuid}: extensions.gnome.org lookup failed ({e}); skipped")
            missing.append(uuid)
            continue
        if major not in (info.get("shell_version_map") or {}):
            out(f"WARN   {uuid}: no version for GNOME Shell {major} on extensions.gnome.org; skipped")
            missing.append(uuid)
            continue
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / f"{uuid}.zip"
            try:
                download(f"{EGO}/download-extension/{uuid}.shell-extension.zip?shell_version={major}", zip_path)
            except Exception as e:
                out(f"WARN   {uuid}: download failed ({e}); skipped")
                missing.append(uuid)
                continue
            if run(["gnome-extensions", "install", "--force", str(zip_path)], check=False) is None:
                out(f"WARN   {uuid}: gnome-extensions install failed; skipped")
                missing.append(uuid)
                continue
        out(f"OK     {uuid}: installed (version {info['shell_version_map'][major].get('version')})")
        enable.append(uuid)
    if enable:
        current = parse_strv(run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"], check=False))
        wanted = current + [u for u in enable if u not in current]
        if wanted != current:
            run(["gsettings", "set", "org.gnome.shell", "enabled-extensions", format_strv(wanted)], check=False)
        out(f"enabled: {' '.join(enable)} (log out and back in to load new extensions)")
    return missing

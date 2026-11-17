"""Tool paths and the user's data/config.yaml.

The tool never contains an absolute path: the tool root is found from this file's
location, data always lives in <tool root>/data (overridable with --data for tests),
and user paths are written with ~ and expanded at run time.
"""
import copy
import os
from pathlib import Path

import yaml

TOOL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = TOOL_ROOT / "data"
EXAMPLES = TOOL_ROOT / "examples" / "data"

DEFAULTS = {
    # Allowed top-level entries in $HOME. Empty list disables the check.
    "home_layout": [],
    # Files backed up into snapshot/dotfiles. Globs are allowed.
    "dotfiles": ["~/.bashrc", "~/.profile", "~/.gitconfig"],
    # dconf paths dumped into snapshot/dconf.ini.
    "dconf_paths": [],
    # Where to look for git repositories.
    "repo_scan": {
        "roots": ["~"],
        "depth": 5,
        "exclude": ["*/.cache/*", "*/.local/*", "*/snap/*", "*/build/*", "*/install/*", "*/node_modules/*"],
    },
    # Packages installed from your own .deb files; `mi check` does not warn about them.
    "known_local_debs": [],
    # Your own systemd units that are disabled on purpose (check does not warn, restore does not enable).
    "expected_disabled": [],
    # apt packages that are never installed by restore and never reported as uncatalogued.
    "restore_exclude": [],
    # Leftover config of removed programs:
    #   - {path: /etc/docker, requires_command: docker}
    #   - {path: /etc/foo.conf, requires_path: /opt/foo, note: "Foo SDK"}
    "residue_checks": [],
    # Extra regexes (on top of the built-in list) for /etc files that must never be copied.
    "etc_secret_patterns": [],
    # Extra regexes for /etc files to ignore completely.
    "etc_ignore_patterns": [],
    # Disk usage thresholds (percent) for these mount points.
    "disk": {"mounts": ["/", "/home"], "warn": 85, "fail": 90},
    # Words that must never appear in the public tool repo (checked by scripts/check-public.sh).
    "private_terms": [],
}


def expand(path):
    return Path(os.path.expandvars(os.path.expanduser(str(path))))


def tilde(path):
    """Write a path with ~ instead of $HOME."""
    s = str(path)
    home = str(Path.home())
    if s == home:
        return "~"
    if s.startswith(home + "/"):
        return "~" + s[len(home):]
    return s


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(data_dir):
    path = Path(data_dir) / "config.yaml"
    user = load_yaml(path) if path.exists() else {}
    unknown = set(user) - set(DEFAULTS)
    if unknown:
        raise SystemExit(f"{path}: unknown keys: {', '.join(sorted(unknown))}")
    return _merge(DEFAULTS, user)


class Context:
    """Everything a module needs: config, data paths, and the target system description."""

    def __init__(self, data_dir=None, os_release=None):
        self.tool_root = TOOL_ROOT
        self.data = Path(data_dir) if data_dir else DEFAULT_DATA
        self.cfg = load_config(self.data)
        self.snapshot = self.data / "snapshot"
        self.home = Path.home()
        self.os_release_path = os_release or "/etc/os-release"
        self._os = None

    @property
    def os(self):
        if self._os is None:
            self._os = read_os_release(self.os_release_path)
        return self._os


def read_os_release(path="/etc/os-release"):
    info = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                info[k] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return info

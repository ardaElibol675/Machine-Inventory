"""data/catalog.yaml: what the user wants on a machine, and why.

Structure:
    apt_sources: {id: {file|line, keyring, key_url}}
    groups:      [{id, name, items: [item, ...]}]
    config:      [item, ...]     offered as the group "config"
    known:       [item, ...]     seen on the machine, known, never restored
"""
import glob
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import load_yaml

# Item keys that hold lists of things to install or restore.
LIST_KEYS = (
    "apt_sources", "apt", "snap", "flatpak", "vscode", "pip",
    "gnome_extensions", "dconf", "dotfiles", "systemd", "etc", "repos", "post",
)
SCALAR_KEYS = ("id", "name", "why", "notes", "manual", "default", "compat", "requires_paths")
SOURCE_KEYS = ("file", "line", "keyring", "key_url")
CONFIG_GROUP = "config"


class CatalogError(ValueError):
    pass


@dataclass
class Item:
    id: str
    group: str
    name: str = ""
    why: str = ""
    notes: str = ""
    manual: str = ""
    default: bool = False
    compat: dict = field(default_factory=dict)
    requires_paths: list = field(default_factory=list)
    lists: dict = field(default_factory=dict)

    def get(self, key):
        return self.lists.get(key, [])

    def compatible(self, os_info):
        """Matches the target release (compat) and the hardware (requires_paths) of this machine."""
        return compat_matches(self.compat, os_info) and paths_present(self.requires_paths)

    def why_incompatible(self, os_info):
        if not compat_matches(self.compat, os_info):
            return "release " + ", ".join(f"{d} {' '.join(v) or 'any'}" for d, v in self.compat.items())
        missing = [p for p in self.requires_paths if not glob.glob(p)]
        return f"missing {', '.join(missing)}" if missing else ""


@dataclass
class Group:
    id: str
    name: str
    items: list


@dataclass
class Catalog:
    apt_sources: dict
    groups: list
    known: list

    def all_items(self):
        return [i for g in self.groups for i in g.items]

    def item(self, item_id):
        for i in self.all_items() + self.known:
            if i.id == item_id:
                return i
        return None

    def group(self, group_id):
        for g in self.groups:
            if g.id == group_id:
                return g
        return None

    def claimed(self, key, include_known=True):
        """Every value of `key` claimed by any item (restorable or known)."""
        items = self.all_items() + (self.known if include_known else [])
        return {v for i in items for v in i.get(key)}


def _version(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v)))


def _version_matches(spec, version):
    """spec: "22.04", ">=24.04", "<=24.04", ">22.04" or "<26.04"."""
    m = re.match(r"^(>=|<=|>|<)?\s*(.+)$", str(spec).strip())
    op, want = m.group(1), m.group(2)
    if not op:
        return str(version) == want
    have, want = _version(version), _version(want)
    if not have:
        return False
    return {">=": have >= want, "<=": have <= want, ">": have > want, "<": have < want}[op]


def compat_matches(compat, os_info):
    """compat: {ubuntu: ["22.04", ">=26.04"], debian: ["12"]}. Empty means "works everywhere".

    A distro listed without versions (ubuntu: []) matches any version of it.
    ID_LIKE is honoured, so `ubuntu: []` also matches Ubuntu derivatives; versions are only
    compared against the distro itself.
    """
    if not compat:
        return True
    ids = [os_info.get("ID", "")] + os_info.get("ID_LIKE", "").split()
    version = os_info.get("VERSION_ID", "")
    for distro, versions in compat.items():
        if distro in ids:
            if not versions:
                return True
            if distro == os_info.get("ID") and any(_version_matches(v, version) for v in versions):
                return True
    return False


def paths_present(patterns):
    """Hardware conditions: every glob must match something on this machine."""
    return all(glob.glob(p) for p in patterns)


def _as_list(value, where):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise CatalogError(f"{where}: expected a list")
    return [str(v) for v in value]


def _item(raw, group, where):
    if not isinstance(raw, dict) or "id" not in raw:
        raise CatalogError(f"{where}: every item needs an id")
    unknown = set(raw) - set(LIST_KEYS) - set(SCALAR_KEYS)
    if unknown:
        raise CatalogError(f"{where} '{raw['id']}': unknown keys: {', '.join(sorted(unknown))}")
    compat = raw.get("compat") or {}
    if not isinstance(compat, dict):
        raise CatalogError(f"{where} '{raw['id']}': compat must be a mapping like {{ubuntu: ['22.04']}}")
    compat = {k: _as_list(v, f"{where} '{raw['id']}' compat") for k, v in compat.items()}
    return Item(
        id=str(raw["id"]),
        group=group,
        name=str(raw.get("name") or raw["id"]),
        why=str(raw.get("why") or ""),
        notes=str(raw.get("notes") or ""),
        manual=str(raw.get("manual") or ""),
        default=bool(raw.get("default", False)),
        compat=compat,
        requires_paths=_as_list(raw.get("requires_paths"), f"{where} '{raw['id']}' requires_paths"),
        lists={k: _as_list(raw.get(k), f"{where} '{raw['id']}' {k}") for k in LIST_KEYS if raw.get(k)},
    )


def parse(data):
    sources = data.get("apt_sources") or {}
    for sid, s in sources.items():
        if not isinstance(s, dict) or not (s.get("file") or s.get("line")):
            raise CatalogError(f"apt_sources '{sid}': needs 'file' or 'line'")
        unknown = set(s) - set(SOURCE_KEYS)
        if unknown:
            raise CatalogError(f"apt_sources '{sid}': unknown keys: {', '.join(sorted(unknown))}")
        if s.get("key_url") and not s.get("keyring"):
            raise CatalogError(f"apt_sources '{sid}': key_url needs a keyring path to write the key to")

    groups = []
    for g in data.get("groups") or []:
        if "id" not in g:
            raise CatalogError("every group needs an id")
        items = [_item(r, g["id"], f"group '{g['id']}'") for r in g.get("items") or []]
        groups.append(Group(id=str(g["id"]), name=str(g.get("name") or g["id"]), items=items))
    if data.get("config"):
        items = [_item(r, CONFIG_GROUP, "config") for r in data["config"]]
        groups.append(Group(id=CONFIG_GROUP, name="System configuration", items=items))
    known = [_item(r, "known", "known") for r in data.get("known") or []]

    seen = set()
    for i in [i for g in groups for i in g.items] + known:
        if i.id in seen:
            raise CatalogError(f"duplicate item id: {i.id}")
        seen.add(i.id)
        for s in i.get("apt_sources"):
            if s not in sources:
                raise CatalogError(f"item '{i.id}': unknown apt source '{s}'")
    gids = [g.id for g in groups]
    if len(gids) != len(set(gids)):
        raise CatalogError("duplicate group id")
    clash = set(gids) & seen
    if clash:
        raise CatalogError(f"ids used for both a group and an item: {', '.join(sorted(clash))}")
    return Catalog(apt_sources=sources, groups=groups, known=known)


def load(data_dir):
    path = Path(data_dir) / "catalog.yaml"
    if not path.exists():
        raise SystemExit(f"{path} not found. Run `./mi init` first.")
    try:
        return parse(load_yaml(path))
    except CatalogError as e:
        raise SystemExit(f"{path}: {e}")

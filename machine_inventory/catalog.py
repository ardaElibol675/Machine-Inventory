"""catalog.yaml (in inventory/ or a migration target): what the user wants on a machine, and why.

Structure:
    apt_sources: {id: {file|line, keyring, key_url}}
    groups:      [{id, name, items: [item, ...]}]
    config:      [item, ...]     offered as the group "config"
    known:       [item, ...]     seen on the machine, known, never restored

`replaces: [ids]` marks an item as the alternative of other items (for example the
Ubuntu-archive build of an app whose upstream repository only exists for an older release).
`mi migrate init` uses it to decide incompatible items automatically.
"""
import glob
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import is_migration, load_yaml

# Item keys that hold lists of things to install or restore.
LIST_KEYS = (
    "apt_sources", "apt", "snap", "flatpak", "vscode", "pip",
    "gnome_extensions", "dconf", "dotfiles", "systemd", "etc", "repos", "post",
)
SCALAR_KEYS = ("id", "name", "why", "notes", "manual", "default", "compat", "requires_paths", "replaces")
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
    replaces: list = field(default_factory=list)
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
        replaces=_as_list(raw.get("replaces"), f"{where} '{raw['id']}' replaces"),
        lists={k: _as_list(raw.get(k), f"{where} '{raw['id']}' {k}") for k in LIST_KEYS if raw.get(k)},
    )


def parse(data, strict_refs=True):
    """strict_refs: every `replaces` id must exist in this catalog. A migration target's
    catalog names inventory items it does not contain, so it is parsed with strict_refs=False."""
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
    for i in [i for g in groups for i in g.items]:
        if i.id in i.replaces:
            raise CatalogError(f"item '{i.id}': replaces itself")
        missing = [r for r in i.replaces if r not in seen]
        if strict_refs and missing:
            raise CatalogError(f"item '{i.id}': replaces unknown items: {', '.join(missing)}")
    return Catalog(apt_sources=sources, groups=groups, known=known)


def load(data_dir):
    path = Path(data_dir) / "catalog.yaml"
    if not path.exists():
        raise SystemExit(f"{path} not found. Run `./mi init` first.")
    try:
        return parse(load_yaml(path), strict_refs=not is_migration(data_dir))
    except CatalogError as e:
        raise SystemExit(f"{path}: {e}")


# --- writing ---------------------------------------------------------------

class _Dumper(yaml.SafeDumper):
    """Readable YAML: multi-line strings as | blocks, short lists and mappings on one line."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)          # indent lists under their key

    def ignore_aliases(self, data):
        return True                                          # never write &anchors / *aliases


def _str(dumper, value):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


def _seq(dumper, value):
    flow = all(isinstance(v, (str, int, float, bool)) and "\n" not in str(v) for v in value)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", value, flow_style=flow)


class _Flow(dict):
    """A mapping written on one line (compat, apt sources)."""


_Dumper.add_representer(str, _str)
_Dumper.add_representer(list, _seq)
_Dumper.add_representer(_Flow, lambda d, v: d.represent_mapping("tag:yaml.org,2002:map", v, flow_style=True))


def item_dict(item):
    """Item -> the mapping it was parsed from (empty keys left out)."""
    out = {"id": item.id}
    if item.name and item.name != item.id:
        out["name"] = item.name
    if item.why:
        out["why"] = item.why
    if item.replaces:
        out["replaces"] = list(item.replaces)
    for k in LIST_KEYS:
        if item.get(k):
            out[k] = list(item.get(k))
    if item.compat:
        out["compat"] = _Flow({d: list(v) for d, v in item.compat.items()})
    if item.requires_paths:
        out["requires_paths"] = list(item.requires_paths)
    if item.default:
        out["default"] = True
    if item.notes:
        out["notes"] = item.notes
    if item.manual:
        out["manual"] = item.manual
    return out


def to_dict(cat):
    out = {}
    if cat.apt_sources:
        out["apt_sources"] = {sid: _Flow(s) for sid, s in cat.apt_sources.items()}
    groups = [g for g in cat.groups if g.id != CONFIG_GROUP]
    if groups:
        out["groups"] = [{"id": g.id, "name": g.name, "items": [item_dict(i) for i in g.items]} for g in groups]
    config = cat.group(CONFIG_GROUP)
    if config and config.items:
        out["config"] = [item_dict(i) for i in config.items]
    if cat.known:
        out["known"] = [item_dict(i) for i in cat.known]
    return out


def yaml_text(obj, header=""):
    """Any data -> YAML text in the same style as dump(), with an optional # header."""
    body = yaml.dump(obj, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=110)
    head = "".join(f"# {l}\n" if l else "#\n" for l in header.splitlines())
    return head + ("\n" if head else "") + body


Flow = _Flow


def dump(cat, header=""):
    """Catalog -> catalog.yaml text. parse(yaml.safe_load(dump(cat))) gives the same catalog."""
    return yaml_text(to_dict(cat), header)

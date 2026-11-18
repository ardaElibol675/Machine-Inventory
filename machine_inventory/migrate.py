"""Migration targets: the inventory turned into a ready-to-install plan for another release.

A migration target is an ordinary data directory (config.yaml, catalog.yaml, snapshot/) plus
migration.yaml, so `mi restore` and `mi render` work on it unchanged. It is self-contained:
its snapshot/ holds copies of only the files its items need, adapted to the target.

    migration/<ID>-<VERSION_ID>/
        migration.yaml      target, source, one decision per inventory item
        target-os-release   the target's os-release (compat checks, dry runs on the old machine)
        config.yaml         copy of the inventory's config.yaml
        catalog.yaml        only what goes onto the target
        INVENTORY.md        generated: catalog + decisions
        snapshot/           payload: dotfiles, etc, systemd, dconf, apt sources/keys, repos.tsv

Nothing here knows about a particular distribution or release: the target is whatever
os-release file it is given, and package archives are looked up in archives.py.
"""
import datetime
import re
import shutil
from pathlib import Path

import yaml

from . import archives, catalog as catalog_mod, shell
from .catalog import compat_matches
from .config import MIGRATION_FILE, TARGET_OS_RELEASE, Context, load_yaml, read_os_release
from .modules import apt, dotfiles, etc, gnome, repos, systemd
from .modules.base import Report
from .render import render

ACTIONS = ("keep", "replace", "drop", "add", "pending")
DECISION_KEYS = ("action", "by", "why", "todo")
TARGET_KEYS = ("ID", "VERSION_ID", "VERSION_CODENAME", "PRETTY_NAME", "ID_LIKE")

HEADER = """Migration target, created by `mi migrate init` and curated with the /migration skill.

decisions: one entry per inventory item (keyed by its id), plus one per item added here.
  action: keep      in catalog.yaml as it is (or adapted for the target)
          replace   not on the target; `by` names the catalog.yaml items that take its place
          drop      not on the target
          add       new item for the target only (not in the inventory)
          pending   not decided yet: `mi migrate check` fails until it is
  why:    the reason (required for everything but a plain keep)
  todo:   manual follow-up on the new machine (shown in INVENTORY.md)"""


class MigrationError(ValueError):
    pass


def target_name(os_info):
    return f"{os_info['ID']}-{os_info['VERSION_ID']}"


def load(mig_dir):
    """migration.yaml as a dict, validated."""
    path = Path(mig_dir) / MIGRATION_FILE
    if not path.exists():
        raise SystemExit(f"{path} not found: {mig_dir} is not a migration target")
    data = load_yaml(path)
    decisions = data.get("decisions") or {}
    if not isinstance(decisions, dict):
        raise SystemExit(f"{path}: decisions must be a mapping of item id -> decision")
    for iid, d in decisions.items():
        if not isinstance(d, dict):
            raise SystemExit(f"{path}: decision for '{iid}' must be a mapping like {{action: keep}}")
        unknown = set(d) - set(DECISION_KEYS)
        if unknown:
            raise SystemExit(f"{path}: decision for '{iid}': unknown keys: {', '.join(sorted(unknown))}")
        if d.get("action") not in ACTIONS:
            raise SystemExit(f"{path}: decision for '{iid}': action must be one of {', '.join(ACTIONS)}")
        if d.get("by") is not None and not isinstance(d["by"], list):
            d["by"] = [d["by"]]
    data["decisions"] = decisions
    return data


def _decisions_yaml(decisions):
    out = {}
    for iid, d in decisions.items():
        d = {k: d[k] for k in DECISION_KEYS if d.get(k)}
        out[iid] = catalog_mod.Flow(d) if set(d) == {"action"} else d
    return out


def save(mig_dir, data):
    data = dict(data, decisions=_decisions_yaml(data.get("decisions") or {}))
    (Path(mig_dir) / MIGRATION_FILE).write_text(catalog_mod.yaml_text(data, HEADER), encoding="utf-8")


# --- payload ---------------------------------------------------------------

def payload(ctx, cat, items):
    """Snapshot files that restoring `items` reads. Returns ([paths], [missing: str])."""
    files, missing = [], []

    def need(path, what):
        if path.exists():
            files.append(path)
        else:
            missing.append(what)

    saved_etc = set(etc.saved(ctx))
    saved_units = set(systemd.saved(ctx))
    saved_dconf = gnome.dconf_saved(ctx)
    known_repos = {r["path"]: r for r in repos.repos(ctx)}
    sources = []
    for i in items:
        for pattern in i.get("dotfiles"):
            found = dotfiles._match(ctx, [pattern])
            if not found:
                missing.append(f"{i.id}: no saved dotfile matches {pattern}")
            files += [ctx.snapshot / dotfiles.DIR / f for f in found]
        for rel in i.get("etc"):
            if rel in saved_etc:
                files.append(ctx.snapshot / "etc/files" / rel)
            else:
                missing.append(f"{i.id}: /etc/{rel} is not in the snapshot")
        for entry in i.get("systemd"):
            if entry in saved_units:
                files.append(ctx.snapshot / "systemd" / entry)
            else:
                missing.append(f"{i.id}: systemd unit {entry} is not in the snapshot")
        for p in i.get("dconf"):
            if p in saved_dconf:
                files.append(saved_dconf[p])
            else:
                missing.append(f"{i.id}: dconf {p} has no dump in the snapshot (or is not in config dconf_paths)")
        for path in i.get("repos"):
            r = known_repos.get(path)
            if not r or r["remote"] == "-":
                missing.append(f"{i.id}: repository {path} has no remote in snapshot/repos.tsv")
        for sid in i.get("apt_sources"):
            if sid not in sources:
                sources.append(sid)
    for sid in sources:
        for path, what in apt.source_payload(ctx, cat.apt_sources[sid]):
            need(path, f"apt source {sid}: {what} is not in the snapshot")
    return list(dict.fromkeys(files)), missing


def _repo_rows(ctx, items):
    wanted = {p for i in items for p in i.get("repos")}
    rows = []
    for line in (ctx.snapshot / "repos.tsv").read_text(encoding="utf-8").splitlines() \
            if (ctx.snapshot / "repos.tsv").exists() else []:
        if line.split("\t", 1)[0] in wanted:
            rows.append(line)
    return rows


# --- init ------------------------------------------------------------------

def _inventory_commit(path):
    top = shell.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], check=False)
    if top is None or Path(top.strip()).resolve() != Path(path).resolve():
        return None
    head = shell.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"], check=False)
    return head.strip() if head else None


def decide(cat, target):
    """Automatic decisions: {item id: decision}, and the items carried to the target.

    Compatible items (compat only: requires_paths is about the new machine's hardware and
    restore checks it there) are kept. An incompatible item is replaced when a kept item
    `replaces` it, otherwise it is pending.
    """
    kept = [i for i in cat.all_items() if compat_matches(i.compat, target)]
    kept_ids = {i.id for i in kept}
    decisions = {}
    for i in cat.all_items():
        if i.id in kept_ids:
            decisions[i.id] = {"action": "keep"}
            continue
        why = f"not compatible with {target.get('ID')} {target.get('VERSION_ID')} ({i.why_incompatible(target)})"
        by = [k.id for k in kept if i.id in k.replaces]
        if by:
            decisions[i.id] = {"action": "replace", "by": by, "why": f"{why}; replaced by its alternative"}
        else:
            decisions[i.id] = {"action": "pending", "why": why}
    return decisions, kept


def target_catalog(cat, kept):
    kept_ids = {i.id for i in kept}
    groups = []
    for g in cat.groups:
        items = [i for i in g.items if i.id in kept_ids]
        if items:
            groups.append(catalog_mod.Group(id=g.id, name=g.name, items=items))
    used = {s for i in kept for s in i.get("apt_sources")}
    sources = {sid: s for sid, s in cat.apt_sources.items() if sid in used}
    return catalog_mod.Catalog(apt_sources=sources, groups=groups, known=[])


def write_catalog(mig_dir, cat, target):
    header = (f"What goes onto {target.get('PRETTY_NAME') or target_name(target)}. Created by `mi migrate init` "
              f"from the inventory;\nedit it with the /migration skill and record every change in migration.yaml.\n"
              f"The item format is documented at the top of examples/inventory/catalog.yaml in the tool.")
    (Path(mig_dir) / "catalog.yaml").write_text(catalog_mod.dump(cat, header), encoding="utf-8")


def render_target(mig_dir):
    mig_dir = Path(mig_dir)
    text = render(catalog_mod.load(mig_dir), mig_dir / "snapshot", load(mig_dir))
    (mig_dir / "INVENTORY.md").write_text(text, encoding="utf-8")


def init(inventory, os_release_path, out_root, today=None, out=print):
    """Create <out_root>/<ID>-<VERSION_ID>/ from the inventory. Returns its path."""
    inventory = Path(inventory)
    if (inventory / MIGRATION_FILE).exists():
        raise MigrationError(f"{inventory} is a migration target; migrate from the inventory instead")
    if not (inventory / "catalog.yaml").exists():
        raise MigrationError(f"{inventory}/catalog.yaml not found")
    target = read_os_release(os_release_path)
    for k in ("ID", "VERSION_ID"):
        if not target.get(k):
            raise MigrationError(f"{os_release_path}: no {k}= line; write a complete os-release file for the target")
    dst = Path(out_root) / target_name(target)
    if dst.exists():
        raise MigrationError(f"{dst} already exists. Curate it with the /migration skill, "
                             f"or move it away to start over.")

    src = Context(data_dir=inventory, os_release=os_release_path)
    cat = catalog_mod.load(inventory)
    decisions, kept = decide(cat, target)
    files, missing = payload(src, cat, kept)
    system = read_os_release(src.snapshot / "system.txt")

    dst.mkdir(parents=True)
    shutil.copyfile(os_release_path, dst / TARGET_OS_RELEASE)
    if (inventory / "config.yaml").exists():
        shutil.copyfile(inventory / "config.yaml", dst / "config.yaml")
    for f in files:
        to = dst / "snapshot" / f.relative_to(src.snapshot)
        to.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, to)
    rows = _repo_rows(src, kept)
    if rows:
        (dst / "snapshot").mkdir(exist_ok=True)
        (dst / "snapshot/repos.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    write_catalog(dst, target_catalog(cat, kept), target)
    save(dst, {
        "target": {k: target[k] for k in TARGET_KEYS if target.get(k)},
        "source": {"system": system.get("PRETTY_NAME") or None, "arch": system.get("ARCH") or None,
                   "inventory_commit": _inventory_commit(inventory),
                   "created": (today or datetime.date.today()).isoformat()},
        "decisions": decisions,
    })
    render_target(dst)

    counts = {a: sum(1 for d in decisions.values() if d["action"] == a) for a in ACTIONS}
    out(f"Created {dst} for {target.get('PRETTY_NAME') or target_name(target)}")
    out("  decisions: " + ", ".join(f"{a} {n}" for a, n in counts.items() if n))
    out(f"  payload: {len(files)} files copied" + (f", {len(rows)} repositories" if rows else ""))
    for m in missing:
        out(f"  missing: {m}")
    out(f"Next: mi migrate check {dst.name}")
    return dst


# --- check -----------------------------------------------------------------

def check(mig_dir, inventory=None, fetch=None, cache=None, refresh=False, offline=False, out=print):
    """Everything still open in a migration target. Returns the Report (failed = open items)."""
    mig_dir = Path(mig_dir)
    data = load(mig_dir)
    decisions = data["decisions"]
    ctx = Context(data_dir=mig_dir)
    target = ctx.os
    if not target.get("ID"):
        target = {k: str(v) for k, v in (data.get("target") or {}).items()}
    tcat = catalog_mod.load(mig_dir)
    titems = tcat.all_items()
    tids = {i.id for i in titems}
    rep = Report(out=out)
    label = f"{target.get('ID')} {target.get('VERSION_ID')}"

    # 1. decisions
    rep.section("decisions (migration.yaml vs inventory and catalog.yaml)")
    open_before = rep.counts["FAIL"]
    if inventory and (Path(inventory) / "catalog.yaml").exists():
        icat = catalog_mod.load(inventory)
        iids = {i.id for i in icat.all_items()}
        for i in icat.all_items():
            d = decisions.get(i.id)
            if d is None:
                rep.fail(f"{i.id}: in the inventory but has no decision (added to the inventory later?)")
            elif d["action"] == "pending":
                rep.fail(f"{i.id}: pending ({d.get('why') or 'no reason given'})")
        for iid, d in decisions.items():
            if iid not in iids and d["action"] not in ("add",):
                rep.warn(f"{iid}: decided ({d['action']}) but no longer in the inventory")
    else:
        rep.warn("inventory not found: cannot look for inventory items without a decision")
        for iid, d in decisions.items():
            if d["action"] == "pending":
                rep.fail(f"{iid}: pending ({d.get('why') or 'no reason given'})")
    named = set()
    for iid, d in decisions.items():
        a = d["action"]
        named.add(iid)
        named.update(d.get("by") or [])
        if a in ("keep", "add") and iid not in tids:
            rep.fail(f"{iid}: decision is {a}, but catalog.yaml has no such item")
        elif a == "drop" and iid in tids:
            rep.fail(f"{iid}: decision is drop, but the item is still in catalog.yaml")
        elif a == "replace":
            by = d.get("by") or []
            if not by:
                rep.fail(f"{iid}: decision is replace, but `by` names no item")
            for b in by:
                if b not in tids:
                    rep.fail(f"{iid}: replaced by '{b}', which is not in catalog.yaml")
            if iid in tids:
                rep.fail(f"{iid}: decision is replace, but the item is still in catalog.yaml")
        if a not in ("keep", "pending") and not d.get("why"):
            rep.fail(f"{iid}: decision {a} has no `why`")
    for i in titems:
        if i.id not in named:
            rep.fail(f"{i.id}: in catalog.yaml but not in migration.yaml (record it as action: add, with a why)")
    if rep.counts["FAIL"] == open_before:
        rep.ok(f"decisions: all {len(decisions)} decided")

    # 2. compatibility and payload
    rep.section(f"catalog.yaml against {label}")
    bad = [i for i in titems if not compat_matches(i.compat, target)]
    for i in bad:
        rep.fail(f"{i.id}: not compatible with {label} ({i.why_incompatible(target)})")
    _, missing = payload(ctx, tcat, titems)
    for m in missing:
        rep.fail(f"payload: {m}")
    if not bad and not missing:
        rep.ok(f"catalog: all {len(titems)} items compatible, every referenced file is in snapshot/")

    # 3. package availability
    rep.section(f"apt packages in the {label} archive")
    with_apt = [i for i in titems if i.get("apt")]
    if not with_apt:
        rep.ok("no apt packages to check")
    elif offline:
        rep.warn("package check skipped (--offline)")
    else:
        _check_packages(rep, ctx, data, tcat, with_apt, target, fetch, cache, refresh)

    c = rep.counts
    out(f"\n{c['OK']} ok, {c['WARN']} to review, {c['FAIL']} open")
    return rep


def _check_packages(rep, ctx, data, tcat, items, target, fetch, cache, refresh):
    arch = archives.deb_arch((data.get("source") or {}).get("arch")
                             or read_os_release(ctx.snapshot / "system.txt").get("ARCH") or "x86_64")
    try:
        names, read = archives.packages(target, arch, fetch=fetch, cache=cache, refresh=refresh, log=rep.ok)
    except archives.Unsupported as e:
        rep.warn(f"package check skipped: {e}")
        return
    except archives.ArchiveError as e:
        rep.fail(f"package check failed: {e} (use --offline to skip it)")
        return
    exclude = set(ctx.cfg["restore_exclude"])
    clean = True
    for i in items:
        pkgs = [p for p in i.get("apt") if p not in exclude]
        absent = [p for p in pkgs if archives.base_name(p) not in names]
        if i.get("apt_sources"):
            notes = []
            for sid in i.get("apt_sources"):
                suites = apt.suites_of(apt.source_text(ctx, tcat.apt_sources[sid]))
                notes.append(f"{sid} (suite: {', '.join(suites) or '?'})")
            msg = f"{i.id}: third-party source {'; '.join(notes)}: check that it publishes " \
                  f"{target.get('VERSION_CODENAME') or label_of(target)}"
            if absent:
                msg += f"; not in the archive, so it must come from that source: {' '.join(absent)}"
            rep.warn(msg)
            clean = False
        elif absent:
            rep.fail(f"{i.id}: not in the {label_of(target)} archive ({arch}): {' '.join(absent)}")
            clean = False
    if clean:
        rep.ok(f"apt: every package is in the archive ({', '.join(read)})")
    else:
        rep.ok(f"apt: read {len(names)} package names from {', '.join(read)}")


def label_of(target):
    return f"{target.get('ID')} {target.get('VERSION_ID')}"


def ensure_os_matches(ctx):
    """A real restore of a migration target only runs on the release it was made for."""
    want = read_os_release(ctx.data / TARGET_OS_RELEASE)
    have = read_os_release("/etc/os-release")
    if (want.get("ID"), want.get("VERSION_ID")) != (have.get("ID"), have.get("VERSION_ID")):
        return (f"this migration target is for {label_of(want)}, but this machine runs "
                f"{have.get('PRETTY_NAME') or label_of(have)}. Restore it on the new machine "
                f"(a dry run works anywhere).")
    return None


ADOPTED = ("catalog.yaml", "config.yaml")
REPLACES_LINE = re.compile(r"^(\s*)replaces:\s*\[([^\]]*)\]\s*$")


def _drop_dangling_replaces(text):
    """A target's items may replace items of the old release; in an inventory those ids must exist.

    Rewrites flow-style `replaces: [a, b]` lines (the form `mi migrate init` and the skill write), keeping
    the rest of the file and its comments as they are.
    """
    ids = {i.id for i in catalog_mod.parse(yaml.safe_load(text), strict_refs=False).all_items()}
    out = []
    for line in text.splitlines(keepends=True):
        m = REPLACES_LINE.match(line.rstrip("\n"))
        if m:
            keep = [r.strip() for r in m.group(2).split(",") if r.strip() in ids]
            if not keep:
                continue
            line = f"{m.group(1)}replaces: [{', '.join(keep)}]\n"
        out.append(line)
    text = "".join(out)
    catalog_mod.parse(yaml.safe_load(text))     # strict: fails if a block-style list is still dangling
    return text


def adopt(mig_dir, inventory, os_release="/etc/os-release", force=False, today=None, out=print):
    """Make a migration target the starting point of the inventory on the machine it was installed on.

    Copies the target's catalog.yaml and config.yaml into the inventory, so the first scan of the new machine
    starts from the adapted catalog (post commands, notes, decisions) instead of the examples. Existing files
    that differ are kept as <name>.bak-YYYYMMDD. The snapshot is left alone: `mi export` replaces it.
    """
    mig_dir, inventory = Path(mig_dir), Path(inventory)
    if not (mig_dir / MIGRATION_FILE).exists():
        raise MigrationError(f"{mig_dir} is not a migration target (no {MIGRATION_FILE})")
    want = read_os_release(mig_dir / TARGET_OS_RELEASE)
    have = read_os_release(os_release)
    if not force and (want.get("ID"), want.get("VERSION_ID")) != (have.get("ID"), have.get("VERSION_ID")):
        raise MigrationError(f"{mig_dir.name} is for {label_of(want)}, but this machine runs "
                             f"{have.get('PRETTY_NAME') or label_of(have)}; adopt it on the machine it was "
                             f"installed on (or pass --force)")
    stamp = (today or datetime.date.today()).strftime("%Y%m%d")
    inventory.mkdir(parents=True, exist_ok=True)
    for name in ADOPTED:
        src, dst = mig_dir / name, inventory / name
        if not src.exists():
            raise MigrationError(f"{src} is missing")
        text = src.read_text(encoding="utf-8")
        if name == "catalog.yaml":
            try:
                text = _drop_dangling_replaces(text)
            except catalog_mod.CatalogError as e:
                raise MigrationError(f"{src}: {e}") from e
        if dst.exists():
            if dst.read_text(encoding="utf-8", errors="replace") == text:
                out(f"same:    {dst}")
                continue
            bak = dst.with_name(f"{name}.bak-{stamp}")
            dst.replace(bak)
            out(f"kept:    {bak}")
        dst.write_text(text, encoding="utf-8")
        out(f"adopted: {dst} (from {mig_dir.name})")

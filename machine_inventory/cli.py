"""mi: machine-inventory command line."""
import argparse
import filecmp
import os
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__, catalog as catalog_mod, migrate, plan, ui
from .config import EXAMPLES, MIGRATION_FILE, MIGRATION_ROOT, TOOL_ROOT, Context, default_data, migration_dir
from .modules import apt, base, each
from .render import render


def _ctx(args):
    return Context(data_dir=args.data, os_release=getattr(args, "os_release", None))


def _require_data(ctx):
    if not ctx.data.exists():
        raise SystemExit(f"{ctx.data} does not exist. Clone your inventory repo there, or run `./mi init`.")


def _refuse_migration(ctx, what):
    """export/check describe this machine; a migration target holds payload for another release."""
    if ctx.migration:
        raise SystemExit(f"{ctx.data} is a migration target ({MIGRATION_FILE}): it holds the payload for "
                         f"another release, not facts about this machine, so `mi {what}` does not apply. "
                         f"Use `mi migrate check {ctx.data.name}` or `mi migrate {ctx.data.name} --dry-run`.")


# --- init ------------------------------------------------------------------

def cmd_init(args):
    ctx = _ctx(args)
    _refuse_migration(ctx, "init")
    ctx.data.mkdir(parents=True, exist_ok=True)
    for name in ("config.yaml", "catalog.yaml"):
        dst = ctx.data / name
        if dst.exists():
            print(f"exists:  {dst}")
        else:
            shutil.copyfile(EXAMPLES / name, dst)
            print(f"created: {dst}")
    for rel, text in (("notes/pitfalls.md", "# Pitfalls on this machine\n\nLessons learned the hard way. "
                       "The /inventory skill reads this first and appends new ones.\n"),
                      ("reports/inventory-log.md", "# Inventory log\n\nEvery `/inventory` scan adds a dated "
                       "section at the top. Newest first.\n")):
        p = ctx.data / rel
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            print(f"created: {p}")
    print(f"Next: edit {ctx.data.name}/config.yaml, then run `./mi export` and `./mi check`.")
    return 0


# --- export ----------------------------------------------------------------

def _tree(root):
    root = Path(root)
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p for p in root.rglob("*") if p.is_file()}


def cmd_export(args):
    ctx = _ctx(args)
    _require_data(ctx)
    _refuse_migration(ctx, "export")
    tmp = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=ctx.data))
    try:
        snap = base.Snapshot(tmp, ctx.snapshot)
        for m, fn in each("collect"):
            name = m.__name__.rsplit(".", 1)[-1]
            print(f"  - {name}")
            fn(ctx, snap)
        old, new = _tree(ctx.snapshot), _tree(tmp)
        changed = sorted(f for f in set(old) & set(new) if not filecmp.cmp(old[f], new[f], shallow=False))
        if ctx.snapshot.exists():
            shutil.rmtree(ctx.snapshot)
        os.replace(tmp, ctx.snapshot)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    print(f"Snapshot written to {ctx.snapshot}")
    if not (added or removed or changed):
        print("No changes since the last export.")
    for label, files in (("added", added), ("removed", removed), ("changed", changed)):
        for f in files:
            print(f"  {label:<8} {f}")
    for w in snap.warnings:
        print(f"WARN   {w}")
    return 1 if snap.warnings else 0


# --- check -----------------------------------------------------------------

def cmd_check(args):
    ctx = _ctx(args)
    _require_data(ctx)
    _refuse_migration(ctx, "check")
    rep = base.Report(quiet=args.quiet)
    if not args.drift_only:
        for _, fn in each("check"):
            fn(ctx, rep)
    if not args.no_drift:
        cat = catalog_mod.load(ctx.data)
        rep.section("catalog drift (snapshot vs catalog.yaml)")
        for _, fn in each("drift"):
            fn(ctx, cat, rep)
    c = rep.counts
    print(f"\n{c['OK']} ok, {c['WARN']} warnings, {c['FAIL']} failures")
    return 1 if rep.failed else 0


# --- catalog / render ------------------------------------------------------

def cmd_catalog(args):
    ctx = _ctx(args)
    cat = catalog_mod.load(ctx.data)
    print(f"Target: {ctx.os.get('PRETTY_NAME', 'unknown')}   (* = default, ! = incompatible: hidden by restore)")
    for g in cat.groups:
        print(f"\n{g.id}  {g.name}")
        for i in g.items:
            ok = i.compatible(ctx.os)
            flag = ("*" if i.default else " ") + (" " if ok else "!")
            why = "" if ok else f"   ({i.why_incompatible(ctx.os)})"
            print(f"  {flag} {i.id:<28} {i.name}{why}")
    if cat.known:
        print(f"\nknown (never restored): {', '.join(i.id for i in cat.known)}")
    return 0


def cmd_render(args):
    ctx = _ctx(args)
    cat = catalog_mod.load(ctx.data)
    out = ctx.data / "INVENTORY.md"
    text = render(cat, ctx.snapshot, migrate.load(ctx.data) if ctx.migration else None)
    old = out.read_text(encoding="utf-8") if out.exists() else None
    out.write_text(text, encoding="utf-8")
    print(f"{'unchanged' if old == text else 'written'}: {out}")
    return 0


# --- restore ---------------------------------------------------------------

def cmd_restore(args):
    ctx = _ctx(args)
    _require_data(ctx)
    if ctx.migration and not args.dry_run:
        err = migrate.ensure_os_matches(ctx)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2
    cat = catalog_mod.load(ctx.data)
    tokens = [t for s in (args.select or []) for t in s.split(",")]
    if tokens or args.all_defaults:
        try:
            items, warnings = plan.select(cat, ctx.os, tokens, args.all_defaults, args.include_incompatible)
        except plan.SelectionError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        for w in warnings:
            print(f"WARN   {w}", file=sys.stderr)
    else:
        if not sys.stdin.isatty():
            print("error: no terminal; use --select or --all-defaults", file=sys.stderr)
            return 2
        items = ui.wizard(cat, ctx.os, args.include_incompatible,
                          use_whiptail=False if args.no_whiptail else None)
        if items is None:
            print("Cancelled.")
            return 1
    if not items:
        print("Nothing selected.")
        return 0

    steps = plan.build(ctx, cat, items)
    print(plan.describe(steps, items, ctx.os))
    if args.dry_run:
        print("\n(dry run: nothing was executed)")
        return 0
    if not args.yes:
        if input("\nRun this plan? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing done.")
            return 1
    finished, problems = plan.execute(steps, keep_going=args.keep_going)
    if not finished:
        return 1
    return base.PARTIAL if problems else 0


def cmd_apt_install(args):
    """Internal: used by restore plans. Installs what it can, warns about the rest."""
    skipped, failed = apt.install_packages(args.packages)
    return base.PARTIAL if skipped or failed else 0


def cmd_gnome_extensions(args):
    """Internal: used by restore plans. Installs and enables what it can, warns about the rest."""
    from .modules import gnome
    return base.PARTIAL if gnome.install_extensions(args.uuids) else 0


# --- migrate ---------------------------------------------------------------

def _inventory_dir(args):
    return Path(args.data) if args.data else default_data()


def cmd_migrate_init(args):
    try:
        migrate.init(_inventory_dir(args), args.os_release, Path(args.out) if args.out else MIGRATION_ROOT)
    except migrate.MigrationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


def _target_args(args):
    """Point args.data at the named migration target, or exit with a clear error."""
    target = migration_dir(args.name)
    if not (target / MIGRATION_FILE).exists():
        raise SystemExit(f"{target} is not a migration target (no {MIGRATION_FILE}); see `mi migrate init`")
    args.data = str(target)
    return args


def cmd_migrate_restore(args):
    return cmd_restore(_target_args(args))


def cmd_migrate_render(args):
    return cmd_render(_target_args(args))


def cmd_migrate_catalog(args):
    return cmd_catalog(_target_args(args))


def cmd_migrate_check(args):
    target = migration_dir(args.name)
    inventory = Path(args.data) if args.data else default_data()
    from .archives import cache_dir
    rep = migrate.check(target, inventory if inventory.exists() else None, cache=cache_dir(),
                        refresh=args.refresh, offline=args.offline)
    return 1 if rep.failed else 0


# --- install-skill ---------------------------------------------------------

def cmd_install_skill(args):
    skills = sorted(d for d in (TOOL_ROOT / "skills").iterdir() if (d / "SKILL.md").exists())
    if not skills:
        raise SystemExit(f"no skills found in {TOOL_ROOT / 'skills'}")
    home = Path.home() / ".claude" / "skills"
    for src in skills:
        dst = home / src.name
        if args.remove:
            if dst.is_symlink():
                dst.unlink()
                print(f"removed link: {dst}")
            else:
                print(f"no link at {dst}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink():
            dst.unlink()                          # re-point after the tool folder moved
        elif dst.exists():
            raise SystemExit(f"{dst} exists and is not a link. Look inside, then remove it by hand.")
        dst.symlink_to(src)
        print(f"linked: {dst} -> {src}")
    if not args.remove:
        print("Start a new Claude Code session and type " + " or ".join(f"/{s.name}" for s in skills) + ".")
    return 0


def _add_restore_args(s):
    s.add_argument("--dry-run", "-n", action="store_true", help="print the plan, run nothing")
    s.add_argument("--select", action="append", metavar="IDS",
                   help="comma-separated group ids (their default items) and item ids")
    s.add_argument("--all-defaults", action="store_true", help="every default item")
    s.add_argument("--include-incompatible", action="store_true",
                   help="also offer items not marked compatible with this system")
    s.add_argument("--no-whiptail", action="store_true", help="use numbered text prompts")
    s.add_argument("--yes", "-y", action="store_true", help="do not ask before running the plan")
    s.add_argument("--keep-going", action="store_true",
                   help="never ask on a failure: skip the command and list it at the end")


MIGRATE_ACTIONS = ("init", "check", "render", "catalog", "restore")


def _route_migrate(argv):
    """`mi migrate <target> ...` is the restore of a target: rewrite it to `mi migrate restore <target> ...`."""
    argv = list(argv)
    if "migrate" in argv:
        i = argv.index("migrate")
        rest = argv[i + 1:]
        if rest and not rest[0].startswith("-") and rest[0] not in MIGRATE_ACTIONS:
            argv.insert(i + 1, "restore")
    return argv


def main(argv=None):
    p = argparse.ArgumentParser(prog="mi", description="Snapshot this machine and restore it selectively.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--data", help="inventory directory (default: <tool>/inventory)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create inventory/ from the examples").set_defaults(fn=cmd_init)
    sub.add_parser("export", help="write a fresh snapshot of this machine into inventory/snapshot").set_defaults(
        fn=cmd_export)

    s = sub.add_parser("check", help="health checks and catalog drift (read-only)")
    s.add_argument("--quiet", "-q", action="store_true", help="only WARN and FAIL lines")
    s.add_argument("--no-drift", action="store_true", help="skip the catalog comparison")
    s.add_argument("--drift-only", action="store_true", help="only the catalog comparison")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("catalog", help="list groups and items")
    s.add_argument("--os-release", help="judge compatibility against this os-release file")
    s.set_defaults(fn=cmd_catalog)

    sub.add_parser("render", help="generate INVENTORY.md from catalog.yaml").set_defaults(fn=cmd_render)

    s = sub.add_parser("restore", help="choose groups and apps from the inventory, then install them")
    _add_restore_args(s)
    s.add_argument("--os-release", help="pretend the target is described by this os-release file")
    s.set_defaults(fn=cmd_restore)

    s = sub.add_parser("apt-install")            # internal, used by restore plans; not listed
    s.add_argument("packages", nargs="+")
    s.set_defaults(fn=cmd_apt_install)

    s = sub.add_parser("gnome-extensions")       # internal, used by restore plans; not listed
    s.add_argument("uuids", nargs="+")
    s.set_defaults(fn=cmd_gnome_extensions)

    s = sub.add_parser("migrate", help="plan a move to another release, then install it: mi migrate <target>",
                       description="Migration targets live in <tool>/migration/<target>. "
                                   "`mi migrate <target>` installs a target on the new machine "
                                   "(same options as restore, e.g. `mi migrate ubuntu-26.04 --dry-run`); "
                                   "init, check, render and catalog prepare and inspect targets.")
    msub = s.add_subparsers(dest="migrate_cmd", required=True, metavar="{<target>,init,check,render,catalog}")
    m = msub.add_parser("restore", prog="mi migrate")   # reached through `mi migrate <target>`, not listed
    m.add_argument("name", help="target name (e.g. ubuntu-26.04) or a path containing /")
    _add_restore_args(m)
    m.set_defaults(fn=cmd_migrate_restore)
    m = msub.add_parser("init", help="create migration/<ID>-<VERSION_ID>/ for the release an os-release "
                                     "file describes")
    m.add_argument("os_release", help="os-release file of the target (ID, VERSION_ID, VERSION_CODENAME, ...)")
    m.add_argument("--out", help="where targets live (default: <tool>/migration)")
    m.set_defaults(fn=cmd_migrate_init)
    m = msub.add_parser("check", help="list what is still open in a migration target (exit 1 if anything)")
    m.add_argument("name", help="target name (e.g. debian-13) or a path containing /")
    m.add_argument("--offline", action="store_true", help="skip the package archive check")
    m.add_argument("--refresh", action="store_true", help="download the archive indexes even if cached")
    m.set_defaults(fn=cmd_migrate_check)
    m = msub.add_parser("render", help="write INVENTORY.md of a target (decisions and catalog)")
    m.add_argument("name", help="target name (e.g. debian-13) or a path containing /")
    m.set_defaults(fn=cmd_migrate_render)
    m = msub.add_parser("catalog", help="list a target's groups and items")
    m.add_argument("name", help="target name (e.g. debian-13) or a path containing /")
    m.set_defaults(fn=cmd_migrate_catalog)

    s = sub.add_parser("install-skill", help="link the Claude Code skills (/inventory, /migration) into "
                                             "~/.claude/skills")
    s.add_argument("--remove", action="store_true")
    s.set_defaults(fn=cmd_install_skill)

    args = p.parse_args(_route_migrate(sys.argv[1:] if argv is None else argv))
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130

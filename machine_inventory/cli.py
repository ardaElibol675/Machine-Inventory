"""mi: machine-inventory command line."""
import argparse
import filecmp
import os
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__, catalog as catalog_mod, plan, ui
from .config import EXAMPLES, Context
from .modules import base, each
from .render import render


def _ctx(args):
    return Context(data_dir=args.data, os_release=getattr(args, "os_release", None))


def _require_data(ctx):
    if not ctx.data.exists():
        raise SystemExit(f"{ctx.data} does not exist. Clone your data repo there, or run `./mi init`.")


# --- init ------------------------------------------------------------------

def cmd_init(args):
    ctx = _ctx(args)
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
    print("Next: edit data/config.yaml, then run `./mi export` and `./mi check`.")
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
    print(f"Target: {ctx.os.get('PRETTY_NAME', 'unknown')}   (* = default, ! = not marked compatible)")
    for g in cat.groups:
        print(f"\n{g.id}  {g.name}")
        for i in g.items:
            flag = ("*" if i.default else " ") + (" " if i.compatible(ctx.os) else "!")
            print(f"  {flag} {i.id:<28} {i.name}")
    if cat.known:
        print(f"\nknown (never restored): {', '.join(i.id for i in cat.known)}")
    return 0


def cmd_render(args):
    ctx = _ctx(args)
    cat = catalog_mod.load(ctx.data)
    out = ctx.data / "INVENTORY.md"
    text = render(cat, ctx.snapshot)
    old = out.read_text(encoding="utf-8") if out.exists() else None
    out.write_text(text, encoding="utf-8")
    print(f"{'unchanged' if old == text else 'written'}: {out}")
    return 0


# --- restore ---------------------------------------------------------------

def cmd_restore(args):
    ctx = _ctx(args)
    _require_data(ctx)
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
    return 0 if plan.execute(steps) else 1


# --- install-skill ---------------------------------------------------------

def cmd_install_skill(args):
    ctx = _ctx(args)
    src = ctx.tool_root / "skills" / "inventory"
    dst = Path.home() / ".claude" / "skills" / "inventory"
    if args.remove:
        if dst.is_symlink():
            dst.unlink()
            print(f"removed link: {dst}")
        else:
            print(f"no link at {dst}")
        return 0
    if not (src / "SKILL.md").exists():
        raise SystemExit(f"{src}/SKILL.md not found")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink():
        dst.unlink()                          # re-point after the tool folder moved
    elif dst.exists():
        raise SystemExit(f"{dst} exists and is not a link. Look inside, then remove it by hand.")
    dst.symlink_to(src)
    print(f"linked: {dst} -> {src}")
    print("Start a new Claude Code session and type /inventory.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="mi", description="Snapshot this machine and restore it selectively.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--data", help="data directory (default: <tool>/data)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create data/ from the examples").set_defaults(fn=cmd_init)
    sub.add_parser("export", help="write a fresh snapshot of this machine into data/snapshot").set_defaults(
        fn=cmd_export)

    s = sub.add_parser("check", help="health checks and catalog drift (read-only)")
    s.add_argument("--quiet", "-q", action="store_true", help="only WARN and FAIL lines")
    s.add_argument("--no-drift", action="store_true", help="skip the catalog comparison")
    s.add_argument("--drift-only", action="store_true", help="only the catalog comparison")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("catalog", help="list groups and items")
    s.add_argument("--os-release", help="judge compatibility against this os-release file")
    s.set_defaults(fn=cmd_catalog)

    sub.add_parser("render", help="generate data/INVENTORY.md from catalog.yaml").set_defaults(fn=cmd_render)

    s = sub.add_parser("restore", help="choose groups and apps, then install them")
    s.add_argument("--dry-run", "-n", action="store_true", help="print the plan, run nothing")
    s.add_argument("--select", action="append", metavar="IDS",
                   help="comma-separated group ids (their default items) and item ids")
    s.add_argument("--all-defaults", action="store_true", help="every default item")
    s.add_argument("--include-incompatible", action="store_true",
                   help="also offer items not marked compatible with this system")
    s.add_argument("--os-release", help="pretend the target is described by this os-release file")
    s.add_argument("--no-whiptail", action="store_true", help="use numbered text prompts")
    s.add_argument("--yes", "-y", action="store_true", help="do not ask before running the plan")
    s.set_defaults(fn=cmd_restore)

    s = sub.add_parser("install-skill", help="link the /inventory skill into ~/.claude/skills")
    s.add_argument("--remove", action="store_true")
    s.set_defaults(fn=cmd_install_skill)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130

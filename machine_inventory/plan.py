"""Turn a selection of catalog items into an ordered restore plan, show it, and run it."""
import subprocess

from .modules import each
from .modules.base import PARTIAL


class SelectionError(ValueError):
    pass


def offered(catalog, os_info, include_incompatible=False):
    """[(group, [items])] the wizard offers on this system. Incompatible items are
    hidden, or kept (never pre-ticked) when include_incompatible is set."""
    out = []
    for g in catalog.groups:
        items = [i for i in g.items if include_incompatible or i.compatible(os_info)]
        if items:
            out.append((g, items))
    return out


def defaults(items, os_info):
    return [i for i in items if i.default and i.compatible(os_info)]


def select(catalog, os_info, tokens=None, all_defaults=False, include_incompatible=False):
    """Non-interactive selection.

    tokens: group ids (their default items) and item ids, e.g. ["robotics", "vscode"].
    all_defaults: every default item of every group.
    Returns (items, warnings).
    """
    chosen, warnings = [], []

    def add(i):
        if i not in chosen:
            chosen.append(i)

    if all_defaults:
        for g, items in offered(catalog, os_info, include_incompatible):
            for i in defaults(items, os_info):
                add(i)
    for tok in tokens or []:
        tok = tok.strip()
        if not tok:
            continue
        g = catalog.group(tok)
        if g:
            picked = defaults(g.items, os_info)
            if not picked:
                warnings.append(f"group '{tok}' has no default items for this system; name items explicitly")
            for i in picked:
                add(i)
            continue
        i = catalog.item(tok)
        if i is None or i.group == "known":
            raise SelectionError(f"unknown group or item: {tok}")
        if not i.compatible(os_info) and not include_incompatible:
            warnings.append(f"skipped '{tok}': incompatible with this system ({i.why_incompatible(os_info)}); "
                            f"use --include-incompatible to install it anyway")
            continue
        add(i)
    # Keep catalog order, so the plan reads like the catalog.
    order = {i.id: n for n, i in enumerate(catalog.all_items())}
    return sorted(chosen, key=lambda i: order[i.id]), warnings


def build(ctx, catalog, items):
    steps = []
    for _, fn in each("steps"):
        steps.extend(fn(ctx, catalog, items))
    return sorted(steps, key=lambda s: s.phase)        # stable: modules keep their own order


def describe(steps, items, os_info):
    name = os_info.get("PRETTY_NAME") or "unknown system"
    out = [f"Target system: {name}", f"Selected items ({len(items)}): " + ", ".join(i.id for i in items), ""]
    bad = [f"{i.id} ({i.why_incompatible(os_info)})" for i in items if not i.compatible(os_info)]
    if bad:
        out += [f"Incompatible with this system, installed anyway: {', '.join(bad)}", ""]
    n = 0
    for s in steps:
        if s.manual:
            continue
        n += 1
        out.append(f"[{n}] {s.title}")
        out += [f"    $ {c}" for c in s.commands]
        out += [f"    note: {x}" for x in s.notes]
    manual = [s for s in steps if s.manual]
    if manual:
        out += ["", "Manual steps (not run by mi):"]
        for s in manual:
            out.append(f"  - {s.title}")
            out += [f"      {x}" for x in s.notes]
    return "\n".join(out)


def execute(steps, ask=input, out=print, keep_going=False, run=None):
    """Run every command. Returns (finished, problems).

    A soft step (package installs) records a failure and moves on. Any other failure asks
    retry / skip / abort, unless keep_going is set. A command exiting with PARTIAL finished
    but skipped some of its work; it is recorded too.
    """
    run = run or (lambda c: subprocess.run(["bash", "-c", c]).returncode)
    problems = []
    for s in [s for s in steps if not s.manual]:
        out(f"\n=== {s.title}")
        for c in s.commands:
            while True:
                out(f"  + {c}")
                rc = run(c)
                if rc == 0:
                    break
                if rc == PARTIAL:
                    problems.append(f"{s.title}: some packages were skipped (see the warnings above)")
                    break
                out(f"  FAILED (exit {rc}): {c}")
                if s.soft or keep_going:
                    problems.append(f"{s.title}: failed and skipped: {c}")
                    break
                a = ask("  [r]etry, [s]kip, [a]bort? ").strip().lower()
                if a.startswith("r"):
                    continue
                if a.startswith("s"):
                    problems.append(f"{s.title}: skipped: {c}")
                    break
                out("Aborted.")
                return False, problems
    if problems:
        out("\nFinished with problems:")
        out("\n".join(f"  - {p}" for p in problems))
    return True, problems

"""Interactive selection: whiptail checklists, or numbered text prompts without whiptail."""
import shutil
import subprocess

from .plan import defaults, offered


def _whiptail(title, text, entries):
    """entries: [(tag, label, on)]. Returns selected tags, or None on cancel."""
    height = min(len(entries), 15)
    args = ["whiptail", "--title", title, "--separate-output", "--checklist", text,
            str(height + 8), "78", str(height)]
    for tag, label, on in entries:
        args += [tag, label[:60], "ON" if on else "OFF"]
    p = subprocess.run(args, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        return None
    return [t for t in p.stderr.split() if t]


def _text(title, text, entries, ask=input, out=print):
    state = [on for _, _, on in entries]
    while True:
        out(f"\n{title}: {text}")
        for n, ((tag, label, _), on) in enumerate(zip(entries, state), 1):
            out(f"  {n:2}. [{'x' if on else ' '}] {tag:<24} {label}")
        a = ask("Numbers to toggle (e.g. 1 3), Enter to accept, q to cancel: ").strip()
        if a.lower() == "q":
            return None
        if not a:
            return [tag for (tag, _, _), on in zip(entries, state) if on]
        for tok in a.replace(",", " ").split():
            if tok.isdigit() and 1 <= int(tok) <= len(entries):
                state[int(tok) - 1] = not state[int(tok) - 1]


def checklist(title, text, entries, use_whiptail=None, **kw):
    if use_whiptail is None:
        use_whiptail = shutil.which("whiptail") is not None
    if use_whiptail:
        return _whiptail(title, text, entries)
    return _text(title, text, entries, **kw)


def wizard(catalog, os_info, include_incompatible=False, use_whiptail=None, **kw):
    """Groups first, then the items of each chosen group. Returns items or None if cancelled."""
    groups = offered(catalog, os_info, include_incompatible)
    entries = [(g.id, f"{g.name} ({len(items)})", bool(defaults(items, os_info))) for g, items in groups]
    picked = checklist("machine-inventory", "Which groups do you want to restore?", entries, use_whiptail, **kw)
    if picked is None:
        return None
    chosen = []
    for g, items in groups:
        if g.id not in picked:
            continue
        entries = []
        for i in items:
            label = i.name + ("" if i.compatible(os_info) else "  [not tested here]")
            entries.append((i.id, label, i.default and i.compatible(os_info)))
        sel = checklist(g.name, "Which items?", entries, use_whiptail, **kw)
        if sel is None:
            return None
        chosen += [i for i in items if i.id in sel]
    return chosen

"""One module per source. To support another package manager, add a module with
collect/check/drift/steps (all optional, see base.py) and list it here."""
from . import apt, dotfiles, etc, flatpak, gnome, health, local, pip, repos, snap, systemd, vscode

# Order matters: export runs collect() in this order, check prints in this order.
MODULES = [apt, snap, flatpak, pip, vscode, gnome, dotfiles, systemd, etc, repos, local, health]


def each(fn_name):
    for m in MODULES:
        fn = getattr(m, fn_name, None)
        if fn:
            yield m, fn

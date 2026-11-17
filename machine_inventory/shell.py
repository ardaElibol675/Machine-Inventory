"""Small helpers around subprocess. All commands run with LC_ALL=C for stable output."""
import os
import shutil
import subprocess

ENV = dict(os.environ, LC_ALL="C", LANG="C")


class CommandError(RuntimeError):
    pass


def have(name):
    return shutil.which(name) is not None


def run(args, check=True, input=None):
    """Run a command (list, or str for bash -c) and return stdout as text.

    With check=True a missing program or a non-zero exit raises CommandError.
    With check=False it returns None instead.
    """
    if isinstance(args, str):
        args = ["bash", "-c", args]
    try:
        p = subprocess.run(args, input=input, capture_output=True, text=True, env=ENV)
    except FileNotFoundError as e:
        if check:
            raise CommandError(f"{args[0]}: not found") from e
        return None
    if p.returncode != 0:
        if check:
            msg = [l for l in (p.stderr or p.stdout).strip().splitlines()
                   if l.strip() and not l.startswith("WARNING: apt does not have a stable CLI")]
            raise CommandError(f"{' '.join(args)}: exit {p.returncode}: {msg[0] if msg else ''}")
        return None
    return p.stdout


def lines(args, check=True):
    out = run(args, check=check)
    if out is None:
        return None
    return [l for l in out.splitlines() if l.strip()]

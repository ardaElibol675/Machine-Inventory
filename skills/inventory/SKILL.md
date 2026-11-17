---
name: inventory
description: Scans this Debian/Ubuntu machine with machine-inventory and keeps the user's private inventory (data/catalog.yaml, INVENTORY.md, reports) in sync with reality. Finds what changed since the last scan, investigates health problems, catalogues new packages, and suggests commands. `/inventory restore` plans a selective restore on a new machine. Never installs anything without explicit approval and never runs git. Runs when the user types /inventory.
disable-model-invocation: true
---

# /inventory

Two modes:
- **`/inventory`** (scan): find what changed on this machine, check its health, and update the user's catalog.
- **`/inventory restore`**: on a new machine, help the user pick what to install and explain the plan.

Goals: the user knows what is on the machine and why, and can rebuild it from the inventory after a reinstall.

## Language

Write every file, name and identifier in English (catalog entries, notes, reports, commit-ready text).
Reply to the user in their language.

## Find the tool and the data

This skill is a symlink created by `mi install-skill`. Resolve it; never assume a path:

```bash
TOOL="$(cd "$(dirname "$(readlink -f ~/.claude/skills/inventory/SKILL.md)")/../.." && pwd)"   # <tool>/skills/inventory -> <tool>
DATA="$TOOL/data"
```

`$TOOL/mi` is the CLI. `$DATA` is the user's private data repo. If `$DATA` does not exist, tell the user to clone their data repo there or run `$TOOL/mi init`, and stop.

| Path (in `$DATA`) | Written by | What |
|---|---|---|
| `snapshot/` | `mi export` | Facts about the machine: package lists, apt sources and keys, `/etc` changes and safe copies, dotfiles, systemd units, dconf, repos. Never edit by hand. |
| `catalog.yaml` | **you**, with the user | What the user wants and why, grouped into apps. Source of truth for restore. |
| `INVENTORY.md` | `mi render` | Generated from the catalog. Never edit by hand. |
| `config.yaml` | the user (you may propose) | Personal settings: dotfiles, dconf paths, expected-disabled units, residue checks, ... |
| `notes/pitfalls.md` | **you** | Machine-specific lessons. Read it first; append new lessons. |
| `reports/inventory-log.md` | **you** | Dated log of every scan, newest first. |
| `reports/cleanup.md` | **you** | Open cleanup suggestions. |

The catalog format is documented at the top of `$TOOL/examples/data/catalog.yaml`.

## Scan mode (`/inventory`)

### 1. Read the pitfalls
Read `$DATA/notes/pitfalls.md` before anything else and keep it in mind for every finding.

### 2. Find what changed
Do not trust git; the data repo may be stale or not a repo yet.
- Copy the current snapshot first: `cp -a "$DATA/snapshot" <scratch>/snapshot.before` (session scratchpad, or `mktemp -d`).
- Run `"$TOOL/mi" export`. It prints added/removed/changed files and warnings. A warning means a fact could not be collected and the previous file was kept: investigate it.
- Diff: `diff -ru <scratch>/snapshot.before "$DATA/snapshot"`.
- If the data repo is a git repo, also look at `git -C "$DATA" status --short` and `git -C "$DATA" log -1 --format=%cr`.

No changes? Say so and continue with step 3; health problems still matter.

### 3. Health and drift
Run `"$TOOL/mi" check`. It prints health checks, then the drift between snapshot and catalog. For every FAIL and WARN find the cause; do not just repeat the line:
- **Failed unit:** `journalctl -b -u <unit>`; did it work on the previous boot (`journalctl -b -1 -u <unit>`)?
- **apt cannot read its lists:** look at the permissions in `/var/lib/apt/lists/`; suggest `sudo apt update`.
- **Package without a repository:** which repository was removed?
- **Waiting for autoremove:** old kernels are normal. Warn loudly if a driver or a kernel-module metapackage is in the list.
- **/etc residue:** is the owning program really gone?

### 4. Update the catalog
- **Uncatalogued** (installed, but no item claims it): put it into the right item, or create an item in the right group, with `name` and `why`. Libraries normally belong to the item that needs them; leftovers of removed programs go into `known` with a `why`. apt pulls dependencies itself, so do not catalogue automatic dependencies.
- **Missing** (an item's package is not installed): it was removed. Ask the user whether to drop it from the catalog; if they removed it on purpose, also log it.
- **New `/etc` file:** read it, explain what it does, and propose an item (`etc:` + `post:` if a service must restart) or a `known` entry. Add a restorable item only after the user agrees.
- **Never invent a purpose.** Look at the package description (`apt-cache show`, `flatpak info`) and at `snapshot/packages/apt-history.txt` (which command installed it, together with what). Write a careful guess, mark it `(purpose not confirmed)` in `why`, set `default: false`, and ask the user. A confident wrong entry about someone's system is worse than an empty one.
- **Compatibility:** for every new or changed item ask "does this work on other releases and other hardware?" Add `compat` when it depends on a release (an apt source naming a suite, a package that newer releases dropped, X11-only or PulseAudio-only setups, dconf dumps from one GNOME version) and `requires_paths` when it depends on hardware. If a newer release has an alternative, add it as a separate item with the opposite version range. Prefer `post:` commands that edit a package's config in place (`sed`) over copying the whole file with `etc:`. Check `"$TOOL/mi" catalog --os-release <file>` against the releases the user cares about (sample files are in `$TOOL/tests/fixtures/`).
- **Hand-installed software** (`snapshot/opt-usr-local.txt` changed): add or update an item with `manual:` steps (version, where it came from, how to install).
- **Home layout:** if the check reports stray entries, suggest where they could go (see `home_layout` in `config.yaml`).

Then run `"$TOOL/mi" render` and `"$TOOL/mi" check --drift-only` again; the drift should be gone or explained.

### 5. Log the scan
Add a section at the **top** of `$DATA/reports/inventory-log.md`:

```markdown
## YYYY-MM-DD

**Changes** (or "No changes since the last scan.")
- Added: ...
- Removed: ...
- /etc: ...

**Health**
- FAIL: ... → cause: ... → suggestion: ...
- WARN: ...

**Suggested commands** (each in its own ```bash block, with what it does)

**Questions for the user** (open `(purpose not confirmed)` entries)
```

If you learned a new machine-specific lesson (something that broke, and why), append it to `notes/pitfalls.md`.

### 6. Summarise for the user
Short: what changed, what is wrong, which files you updated. Suggested commands in separate `bash` blocks. Ask the open questions one by one. Remind them that reviewing and committing the data repo is theirs (`git -C data diff`, `git add -A`, `git commit`, `git push`).

## Restore mode (`/inventory restore`)

1. Read `notes/pitfalls.md`. Run `"$TOOL/mi" catalog` to see groups and items on this system (`*` default, `!` not marked compatible).
2. Ask the user which groups and items they want. Explain incompatible items (wrong release, hardware-specific ones like drivers or laptop fixes).
3. Show the plan: `"$TOOL/mi" restore --dry-run --select <ids>` (group ids select their default items; item ids select one item). Walk the user through apt sources, packages, config files that will be overwritten (existing files are kept as `.bak-DATE`), and the manual steps.
4. Only after the user explicitly approves this exact plan, run `"$TOOL/mi" restore --select <ids> --yes` in the foreground so they see sudo prompts. If a step fails, `mi` asks to retry, skip, or abort; explain the failure before they answer.
5. Afterwards go through the manual steps with them, then suggest `"$TOOL/mi" export && "$TOOL/mi" check`.

## Limits, and why

- **Scan mode only edits files in `$DATA`.** This is an inventory tool, not a system administrator: no installing or removing packages, no enabling or restarting services, no writing to `/etc`. Give the user commands instead; anything needing sudo is theirs to run.
- **Restore mode installs only after explicit approval of the shown plan**, and only what the plan shows.
- **No git operations** (commit, push, init). The user decides what goes into their repos.
- **The tool repo is public.** Never put personal data (paths under the home directory, names, emails, hostnames, project names) into `$TOOL`; it belongs in `$DATA`. If you change the tool, run `"$TOOL/scripts/check-public.sh"`.
- **Secrets:** `mi export` never copies sensitive `/etc` files (pam, shadow, sudoers, ssh, NetworkManager connections, fstab, keys, ...). Still, after every scan run `grep -rniE 'token|passw|secret|api[_-]?key|BEGIN .*PRIVATE' "$DATA/snapshot" --exclude-dir=keyrings`. If something shows up, tell the user and propose a pattern for `etc_secret_patterns` in `config.yaml`. A private repo is still no place for secrets.
- **Simulate before suggesting removals:** `apt-get -s purge --autoremove <packages>`. Call out drivers, kernel modules, desktop metapackages, or big frameworks in the result. "Looks unused" does not mean nothing depends on it.
- **Unsure whether something is still used?** Say so. Finding the residue of a removed program is better than breaking a working one.

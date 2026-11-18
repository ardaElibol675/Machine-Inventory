---
name: inventory
description: Scans this Debian/Ubuntu machine with machine-inventory and keeps the user's private inventory (inventory/catalog.yaml, INVENTORY.md, reports, README.md) in sync with reality. Finds what changed since the last scan, investigates health problems, catalogues new packages, and suggests commands. `/inventory restore` plans a selective restore on a new machine. Never installs anything without explicit approval and never runs git. Runs when the user types /inventory.
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

## Find the tool and the inventory

This skill is a symlink created by `mi install-skill`. Resolve it; never assume a path:

```bash
TOOL="$(cd "$(dirname "$(readlink -f ~/.claude/skills/inventory/SKILL.md)")/../.." && pwd)"   # <tool>/skills/inventory -> <tool>
INVENTORY="$TOOL/inventory"
```

`$TOOL/mi` is the CLI. `$INVENTORY` is the user's private inventory repo (any name on their git host; it is always cloned as `inventory/`). If it does not exist but `$TOOL/data` does, the folder has its old name: tell the user to run `mv "$TOOL/data" "$TOOL/inventory"` and stop. If neither exists, tell the user to clone their inventory repo there (`git clone <their-repo> "$TOOL/inventory"`) or run `$TOOL/mi init`, and stop.

**Migrated machine.** Read `ID` and `VERSION_ID` from `/etc/os-release`. If `$TOOL/migration/<ID>-<VERSION_ID>/migration.yaml` exists and either `$INVENTORY` does not exist or its `snapshot/system.txt` describes another release, this machine was installed from that migration target. Start the inventory from the target instead of the examples (its catalog already has the adapted items, post commands and notes): tell the user, and with their OK run `"$TOOL/mi" migrate adopt <ID>-<VERSION_ID>`. It copies the target's `catalog.yaml` and `config.yaml` into `$INVENTORY` (differing files are kept as `<name>.bak-YYYYMMDD`; notes, reports and git history stay), then continue with the scan. Never run `mi init` over a migrated machine; it would throw that work away.

After an adopt, read the `.bak-YYYYMMDD` files. If they were written for this machine (an earlier scan of this release, not the old release's inventory), carry over what the target could not know: `known` entries, items for things set up on this machine only, and `config.yaml` settings that describe it. Tell the user what you carried over. A `.bak` of the old release only shows what changed; take nothing from it without asking.

Moving to another release or distribution is not this skill's job: that is `/migration`, which reads this inventory and writes ready-to-install plans into `$TOOL/migration/`.

| Path (in `$INVENTORY`) | Written by | What |
|---|---|---|
| `snapshot/` | `mi export` | Facts about the machine: package lists, apt sources and keys, `/etc` changes and safe copies, dotfiles, systemd units, dconf, repos. Never edit by hand. |
| `catalog.yaml` | **you**, with the user | What the user wants and why, grouped into apps. Source of truth for restore. |
| `INVENTORY.md` | `mi render` | Generated from the catalog. Never edit by hand. |
| `config.yaml` | the user (you may propose) | Personal settings: dotfiles, dconf paths, expected-disabled units, residue checks, ... |
| `notes/pitfalls.md` | **you** | Machine-specific lessons. Read it first; append new lessons. |
| `reports/inventory-log.md` | **you** | Dated log of every scan, newest first. |
| `reports/cleanup.md` | **you** | Open cleanup suggestions. |
| `README.md` | **you** | What this inventory holds and how to restore it on a new machine (step 6). |

The catalog format is documented at the top of `$TOOL/examples/inventory/catalog.yaml`.

## Scan mode (`/inventory`)

### 1. Read the pitfalls
Read `$INVENTORY/notes/pitfalls.md` before anything else and keep it in mind for every finding.

### 2. Find what changed
Do not trust git; the inventory repo may be stale or not a repo yet.
- Copy the current snapshot first: `cp -a "$INVENTORY/snapshot" <scratch>/snapshot.before` (session scratchpad, or `mktemp -d`).
- Run `"$TOOL/mi" export`. It prints added/removed/changed files and warnings. A warning means a fact could not be collected and the previous file was kept: investigate it.
- Diff: `diff -ru <scratch>/snapshot.before "$INVENTORY/snapshot"`.
- If the inventory repo is a git repo, also look at `git -C "$INVENTORY" status --short` and `git -C "$INVENTORY" log -1 --format=%cr`.

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
- **Missing** (an item's package is not installed): it was removed. Ask the user whether to drop it from the catalog; if they removed it on purpose, also log it. Right after `mi migrate adopt`, missing items are usually ones the user did not pick during the migration: list them in one batch and ask which to drop.
- **Items `mi check` cannot see:** it compares packages, flatpaks, snaps, VS Code and GNOME extensions, dconf dumps, dotfiles, `/etc` files, systemd units and repos. An item that only has `post:` or `manual:` (a vendor installer, a driver chosen at install time, a setting changed in place) needs a look by hand: check its effect (a binary, a file, a setting) and ask the user if it is unclear.
- **Adopted items:** keep their `post:` commands, `notes` and `manual:` steps (they were researched for this release); drop `replaces:` entries whose old item is gone. Do not turn a working `post:` into a `manual:` step.
- **New `/etc` file:** read it, explain what it does, and propose an item (`etc:` + `post:` if a service must restart) or a `known` entry. Add a restorable item only after the user agrees.
- **Never invent a purpose.** Look at the package description (`apt-cache show`, `flatpak info`) and at `snapshot/packages/apt-history.txt` (which command installed it, together with what). Write a careful guess, mark it `(purpose not confirmed)` in `why`, set `default: false`, and ask the user. A confident wrong entry about someone's system is worse than an empty one.
- **Compatibility:** for every new or changed item ask "does this work on other releases and other hardware?" Add `compat` when it depends on a release (an apt source naming a suite, a package that newer releases dropped, X11-only or PulseAudio-only setups, dconf dumps from one GNOME version) and `requires_paths` when it depends on hardware. If a newer release has an alternative, add it as a separate item with the opposite version range and `replaces: [<old item id>]`, so `mi migrate init` knows the two belong together. Prefer `post:` commands that edit a package's config in place (`sed`) over copying the whole file with `etc:`. Check `"$TOOL/mi" catalog --os-release <file>` against the releases the user cares about (sample files are in `$TOOL/tests/fixtures/`).
- **Hand-installed software** (`snapshot/opt-usr-local.txt` changed): add or update an item. Prefer `post:` commands when the vendor has an unattended installer (safe to run twice); keep `manual:` for what needs the user (a licence sign-in, a download behind a login) and say where it came from and which version.
- **Home layout:** if the check reports stray entries, suggest where they could go (see `home_layout` in `config.yaml`).

Then run `"$TOOL/mi" render` and `"$TOOL/mi" check --drift-only` again; the drift should be gone or explained.

### 5. Log the scan
Add a section at the **top** of `$INVENTORY/reports/inventory-log.md`:

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

### 6. Write the inventory README
Write or refresh `$INVENTORY/README.md` from what is in the repo now (never from memory). The README lives in the user's private repo, so give complete commands with the real clone URLs: read `git -C "$TOOL" remote get-url origin` and `git -C "$INVENTORY" remote get-url origin`. The tool repo is public: write an scp-style URL (`git@host:owner/repo.git`) in its `https://host/owner/repo.git` form so it clones without keys. Write the inventory repo's URL as it is (if it is an SSH URL, say the user needs their SSH key on that host first). Only if a repo has no remote, write `<tool-repo-url>` or `<your-inventory-repo>`; never ask the user for a URL just for the README.
- one paragraph: which machine this is (`snapshot/system.txt`), the date of the latest scan, and how many groups and items the catalog has (`"$TOOL/mi" catalog`);
- the groups with one line each (what they are for), and a pointer to `INVENTORY.md` for the details;
- **Restore on a new machine** (same release), as separate `bash` blocks:
  `git clone <tool repo URL> machine-inventory && cd machine-inventory`,
  `git clone <inventory repo URL> inventory`, `./bootstrap.sh`, `./mi install-skill`,
  `./mi restore --dry-run`, `./mi restore` (or `/inventory restore` in Claude Code);
- items that need manual steps or specific hardware (`manual:`, `requires_paths`), so nobody is surprised on the new machine;
- **Moving to another release or distribution:** one line pointing to `/migration` and the migration repo.

### 7. Summarise for the user
Short: what changed, what is wrong, which files you updated. Suggested commands in separate `bash` blocks. Ask the open questions one by one. Remind them that reviewing and committing the inventory repo is theirs (`git -C "$INVENTORY" diff`, `git add -A`, `git commit`, `git push`).

## Restore mode (`/inventory restore`)

0. Compare this machine (`/etc/os-release`) with the inventory's snapshot (`$INVENTORY/snapshot/system.txt`). If the release or distribution differs and `$TOOL/migration/<ID>-<VERSION_ID>/` exists for this machine, restore that migration target instead: use `"$TOOL/mi" migrate catalog <ID>-<VERSION_ID>` for `catalog` and `"$TOOL/mi" migrate <ID>-<VERSION_ID>` wherever the steps below say `"$TOOL/mi" restore` (same options), and read its `migration.yaml` todos and `$TOOL/migration/README.md`. If the release differs and there is no target, tell the user that `/migration` on the old machine prepares one; restoring the inventory directly only offers the items marked compatible.
1. Read `notes/pitfalls.md`. Run `"$TOOL/mi" catalog` to see groups and items on this system (`*` default, `!` not marked compatible).
2. Ask the user which groups and items they want. Explain incompatible items (wrong release, hardware-specific ones like drivers or laptop fixes).
3. Show the plan: `"$TOOL/mi" restore --dry-run --select <ids>` (group ids select their default items; item ids select one item). Walk the user through apt sources, packages, config files that will be overwritten (existing files are kept as `.bak-DATE`), and the manual steps.
4. Only after the user explicitly approves this exact plan, run `"$TOOL/mi" restore --select <ids> --yes` in the foreground so they see sudo prompts. If a step fails, `mi` asks to retry, skip, or abort; explain the failure before they answer.
5. Afterwards go through the manual steps with them, then suggest `"$TOOL/mi" export && "$TOOL/mi" check`.

## Limits, and why

- **Scan mode only edits files in `$INVENTORY`.** This is an inventory tool, not a system administrator: no installing or removing packages, no enabling or restarting services, no writing to `/etc`. Give the user commands instead; anything needing sudo is theirs to run.
- **Restore mode installs only after explicit approval of the shown plan**, and only what the plan shows.
- **No git operations** (commit, push, init). The user decides what goes into their repos.
- **The tool repo is public.** Never put personal data (paths under the home directory, names, emails, hostnames, project names) into `$TOOL`; it belongs in `$INVENTORY`. If you change the tool, run `"$TOOL/scripts/check-public.sh"`.
- **Secrets:** `mi export` never copies sensitive `/etc` files (pam, shadow, sudoers, ssh, NetworkManager connections, fstab, keys, ...). Still, after every scan run `grep -rniE 'token|passw|secret|api[_-]?key|BEGIN .*PRIVATE' "$INVENTORY/snapshot" --exclude-dir=keyrings`. If something shows up, tell the user and propose a pattern for `etc_secret_patterns` in `config.yaml`. A private repo is still no place for secrets.
- **Simulate before suggesting removals:** `apt-get -s purge --autoremove <packages>`. Call out drivers, kernel modules, desktop metapackages, or big frameworks in the result. "Looks unused" does not mean nothing depends on it.
- **Unsure whether something is still used?** Say so. Finding the residue of a removed program is better than breaking a working one.

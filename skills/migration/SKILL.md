---
name: migration
description: Turns the user's machine-inventory into a ready-to-install plan for another release or distribution ("move me to Debian 13", "migrate to Ubuntu 26.04"). Reads the inventory, never scans the machine; keeps what fits the target, researches and proposes alternatives for what does not, and records every decision in the user's private migration repo (migration/<ID>-<VERSION_ID>/), so the new machine only has to clone it and run `mi migrate <target>`. Never installs anything and never runs git. Runs when the user types /migration.
disable-model-invocation: true
---

# /migration

The user wants to move to another release or distribution. Their inventory (`/inventory`) describes the current machine; this skill turns it into a **migration target**: a self-contained folder with a catalog, config and payload files already adapted to the target, plus `migration.yaml` with one decision per inventory item. On the new machine the user clones the migration repo and installs it as it is.

Usage: `/migration <distro> <version>` (e.g. `/migration Debian 13`, `/migration Ubuntu 26.04`), or `/migration` to continue a target that already exists.

## Language

Write every file, name and identifier in English (catalog entries, decisions, READMEs). Reply to the user in their language.

## Rules, and why

- **Never scan the machine.** No `mi export`, no `mi check`, no reading `/etc`, package databases or the home directory to learn about the system. The inventory is the only source of facts about the current machine; if it is stale, the user runs `/inventory` first. This keeps the result reproducible and reviewable.
- **The inventory is read-only.** Never edit anything in `$INVENTORY`. Everything you write goes into `$MIGRATION/<target>/` and `$MIGRATION/README.md`.
- **No installs, no system changes, no git writes.** Committing and pushing the migration repo is the user's decision. Reading remote URLs (`git remote get-url origin`) is fine.
- **Automate what a script can do.** The new machine should need the user only for what truly needs a person: signing in to an account or activating a licence, consent dialogs, checks that only make sense after a reboot, and code changes (porting). Anything else (an official command-line installer, recreating a git-ignored config file, building a workspace, registering a file with this machine's paths, marking a package manual, a kernel parameter the source machine had) becomes `post:` commands that are safe to run twice, not a `manual:` step. Test non-trivial commands on scratch copies of the files they edit.
- **Never state a fact about the target without checking it** in an official source (the distro's package search or archive, its release notes, upstream documentation, the extension or app store API). Say where you looked. "Probably available" is not a decision.
- **Nothing specific is hard-coded here.** The checklist below is by category; the answers come from research for this source, this target and these items.
- **The tool repo is public.** Never put personal data into `$TOOL`.

## Find the tool, the inventory and the migration repo

This skill is a symlink created by `mi install-skill`. Resolve it; never assume a path:

```bash
TOOL="$(cd "$(dirname "$(readlink -f ~/.claude/skills/migration/SKILL.md)")/../.." && pwd)"   # <tool>/skills/migration -> <tool>
INVENTORY="$TOOL/inventory"
MIGRATION="$TOOL/migration"
```

- `$INVENTORY` missing: if `$TOOL/data` exists, tell the user to `mv "$TOOL/data" "$TOOL/inventory"`; otherwise they must clone their inventory repo there. Stop.
- `$MIGRATION` missing: explain that targets live in a second private repo, any name, on any git host, always cloned as `migration/`. Either clone it (`git clone <your-migration-repo> "$TOOL/migration"`) or create it (`mkdir "$TOOL/migration" && git -C "$TOOL/migration" init`), then add a private remote. Ask the user which; do not run git yourself. You may create the empty folder if they ask you to.
- Read `$INVENTORY/notes/pitfalls.md` and the top entry of `$INVENTORY/reports/inventory-log.md`. If the latest scan is older than a few weeks, or the log is missing, suggest running `/inventory` first and ask whether to continue anyway.

| Path (in `$MIGRATION/<target>/`) | Written by | What |
|---|---|---|
| `migration.yaml` | `mi migrate init`, then **you** | target, source, one decision per inventory item (`keep`, `replace`, `drop`, `add`, `pending`) with `why` and `todo` |
| `target-os-release` | `mi migrate init` | the target's os-release; `catalog`, `render` and `restore --dry-run` judge items against it |
| `catalog.yaml` | `mi migrate init`, then **you** | only what goes onto the target, adapted to it |
| `config.yaml` | copy of the inventory's | adapt `dconf_paths`, `restore_exclude`, `expected_disabled` if the target needs it |
| `snapshot/` | `mi migrate init`, then **you** | payload: only the files the items reference (dotfiles, etc, systemd, dconf, apt sources and keys, repos.tsv) |
| `INVENTORY.md` | `mi render` | generated: decisions + catalog. Never edit by hand. |

## 1. Pin down the target

Turn the user's words into an os-release file for the target. Look the values up in official sources (the distro's release page or `os-release` documentation; an image's `/etc/os-release` if they have one): `ID`, `VERSION_ID`, `VERSION_CODENAME`, `PRETTY_NAME`, `ID_LIKE` when the distro sets it. Ask if the request is ambiguous (which point release, which flavour, LTS or not). Write it to a scratch file (session scratchpad or `mktemp`), in os-release format:

```
PRETTY_NAME="<name>"
ID=<distro>
VERSION_ID="<version>"
VERSION_CODENAME=<codename>
ID_LIKE=<parents>
```

If `$MIGRATION/<ID>-<VERSION_ID>/` already exists, continue that target (do not re-create it). Otherwise:

```bash
"$TOOL/mi" migrate init <os-release file>
"$TOOL/mi" migrate check <ID>-<VERSION_ID>
```

`init` keeps every item whose `compat` matches the target, decides `replace` when a compatible item `replaces` an incompatible one, and leaves the rest `pending`. `check` lists what is open: undecided items, items incompatible with the target, missing payload files, apt packages the target's archive lacks, and third-party sources to review. Its output is your work list. If `check` says the target's archive is not supported yet, check packages by hand in the distro's package search (and tell the user how to add the archive: `machine_inventory/archives.py`).

## 2. Review every item, kept ones included

A `keep` from `init` only means the catalog did not rule the item out. Go through every item in `catalog.yaml` and every `pending` decision, and ask what changes between the source (`migration.yaml` `source.system`) and the target in each category below. Research each finding; record what you found.

- **Packages:** names that changed, packages split, merged or dropped (`check` lists those missing from the archive; also look for renamed replacements). Version jumps that change behaviour.
- **Third-party repositories:** sources that name a suite or codename, PPAs (Ubuntu-only), vendor repositories that do not publish the target release. Find the upstream instructions for the target; adapt the source file in `snapshot/apt-sources/` or switch to a distro package, flatpak or snap.
- **Session and desktop:** display server (X11/Wayland) defaults and settings that force one, the desktop environment and its version (shell extensions must support the target's shell version: check the extension store's API; dconf keys renamed or removed between versions), audio stack (PulseAudio/PipeWire) and its config files.
- **Default applications and integration:** how the target sets default apps, autostart, MIME handlers, alternatives; tools that the source distro ships and the target does not (update notifiers, crash reporters, release-upgrade prompts).
- **Language packaging policies:** system Python and pip (externally managed environments), language runtimes and their versions, `pip --user` packages that need a venv or pipx on the target.
- **Kernel, drivers and firmware:** driver packages and their naming, firmware components (non-free firmware), DKMS modules, GPU drivers. These matter most; never guess.
- **Toolchains and frameworks with per-release builds:** robotics stacks, GPU compute, simulators, SDKs. Check the vendor's supported-platform matrix for the target.
- **Services and configuration:** systemd units with paths, users or binaries that may differ; `/etc` files whose format or location changed; `post:` commands that call tools the target may lack.
- **Hardware:** `requires_paths` items are judged on the new machine at restore time; keep them unless the user says the hardware changes.

## 3. Own projects and repositories

For every item with `repos:` (and services that run the user's own code), read what the project depends on from the copies the inventory points to: build files, `package.xml`/rosdep keys, `requirements*.txt`/`pyproject.toml`, `package.json`, `CMakeLists.txt`, Dockerfiles, service units. You may read those project files; that is reading the inventory's references, not scanning the system. Then check that the stack exists on the target.

If it does not (the framework has no release for the target, a required library is gone), recommend **not cloning** the project: decide `drop` with a `why`, or keep the clone with a `todo` that says what porting would need. Restoring something that cannot build only wastes the user's time. You do not port code.

## 4. Decide with the user

Present your findings grouped (fits as is / adapted / replaced / dropped / new), each with the evidence. Let the user decide item by item or in batches; never decide a `drop` or a replacement silently. Ask about anything you could not verify.

Then apply the decisions, editing only `$MIGRATION/<target>/`:
- **keep, adapted:** edit the item in `catalog.yaml` (packages, sources, `post:` commands) and the payload files in `snapshot/` (a suite in an apt source, a line in a dotfile, a path in a unit). Decision `keep`, with a `why` saying what was adapted.
- **replace:** add the alternative item to `catalog.yaml` with `replaces: [<old id>]`, remove the old item, decision `{action: replace, by: [<new id>], why: ...}`. Naming the new item in `by` is enough; it needs no decision of its own unless it is an inventory item (then `keep`).
- **drop:** remove the item from `catalog.yaml`; decision `drop` with a `why`.
- **add:** something the target needs that the source did not (a replacement for a removed default tool, a firmware package); decision `add` with a `why`.
- Anything the new machine needs by hand (see "Automate what a script can do") goes into the decision's `todo` and, when it belongs to an item, into its `manual:`.
- Remove payload files that no item references any more; copy any file a new item needs into `snapshot/` (only from the inventory's snapshot or from official sources you cite).

Every decision except a plain `keep` needs a `why`. Keep `migration.yaml` in catalog order.

## 5. Verify

```bash
"$TOOL/mi" migrate render <target>
"$TOOL/mi" migrate check <target>
"$TOOL/mi" migrate <target> --dry-run --all-defaults
```

`migrate check` must report nothing open (warnings are for review: explain each one in a `why` or a `todo`). The dry run must show no incompatible items and no "not in the snapshot" notes. Read the plan once as the new machine would: sources, packages, files written, services enabled, manual steps.

## 6. Write the migration README

Write or refresh `$MIGRATION/README.md` from what is in the repo now (never from memory). For the clone URLs read `git -C "$TOOL" remote get-url origin` and `git -C "$MIGRATION" remote get-url origin`. The tool repo is public: write an scp-style URL (`git@host:owner/repo.git`) in its `https://host/owner/repo.git` form so it clones without keys. Write the migration repo's URL as it is (if it is an SSH URL, say the user needs their SSH key on that host first). If a repo has no remote, write `<tool-repo-url>` or `<your-migration-repo>`; never ask the user for a URL just for the README.
- what this repo is (migration targets made from the user's inventory by `/migration`) and that `INVENTORY.md` in each target has the details;
- **one section per target** (every folder with a `migration.yaml`): target name, source system, created date, decision totals;
  - **Migrate to a new machine**, as separate `bash` blocks:
    `git clone <tool-repo-url> machine-inventory && cd machine-inventory`,
    `git clone <your-migration-repo> migration`, `./bootstrap.sh`, `./mi install-skill`,
    `./mi migrate <target> --dry-run`, `./mi migrate <target>`;
  - open `todo`s and the items' manual steps, in the order the user will meet them.

Nothing else: no change log and no list of dropped items (both are in each target's `INVENTORY.md`).

## 7. Summarise

Short: target, decision totals, what was adapted or replaced, what needs the user's hands on the new machine, and anything you could not verify. Remind them that committing and pushing the migration repo is theirs (`git -C "$MIGRATION" status`, `git add -A`, `git commit`, `git push`), and that the inventory itself was not changed.

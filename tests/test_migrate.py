import datetime
import io
import lzma
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

from machine_inventory import archives, catalog, cli, migrate
from machine_inventory.catalog import CatalogError, parse
from machine_inventory.config import EXAMPLES, Context, migration_dir
from tests.helpers import FIXTURES, make_data

DEBIAN = FIXTURES / "debian-13"
NOBLE = FIXTURES / "ubuntu-24.04"

# Every apt package of the example catalog except vlc (and code, which comes from its own source).
ARCHIVE = ["build-essential", "cmake", "ninja-build", "pkg-config", "git", "git-lfs", "example-tool"]


def packages_index(names, provides=()):
    text = "".join(f"Package: {n}\nVersion: 1.0\n\n" for n in names)
    text += "".join(f"Package: real-{p}\nProvides: {p} (= 1.0), other-{p}\n\n" for p in provides)
    return lzma.compress(text.encode())


def fetcher(names, calls=None):
    """Fake archive: only the first pocket's main component exists."""
    def fetch(url):
        if calls is not None:
            calls.append(url)
        if "/main/" in url and "-updates/" not in url:
            return packages_index(names)
        return None
    return fetch


def make_inventory(test):
    """The example inventory plus things the migration code must handle."""
    inv = make_data(test)
    snap = inv / "snapshot"
    extra = {
        "dotfiles/.profile": "# profile\n",
        "dotfiles/.gitconfig": "[user]\n",
        "dotfiles/.unrelated": "not referenced by any item\n",
        "systemd/system/battery-threshold.service": "[Service]\nExecStart=@HOME@/bin/limit\n",
        "packages/apt-manual.txt": "git\n",
        "repos.tsv": "~/Projects/demo\thttps://git.example.com/demo.git\tmain\t0\t0\n"
                     "~/Projects/other\thttps://git.example.com/other.git\tmain\t0\t0\n",
    }
    for rel, text in extra.items():
        (snap / rel).parent.mkdir(parents=True, exist_ok=True)
        (snap / rel).write_text(text)
    data = yaml.safe_load((inv / "catalog.yaml").read_text())
    data["groups"].append({"id": "projects", "name": "Projects", "items": [
        {"id": "demo", "name": "Demo project", "repos": ["~/Projects/demo"], "default": True},
        {"id": "old-only", "name": "Only on the old release", "apt": ["old-tool"], "compat": {"ubuntu": ["22.04"]}},
    ]})
    (inv / "catalog.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return inv


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.inv = make_inventory(self)
        self.root = Path(tempfile.mkdtemp(prefix="mi-mig-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root))

    def init(self, os_release=DEBIAN):
        return migrate.init(self.inv, os_release, self.root, today=datetime.date(2026, 1, 2), out=lambda _: None)

    def check(self, target, names=ARCHIVE, **kw):
        lines = []
        kw.setdefault("fetch", fetcher(names))
        rep = migrate.check(target, kw.pop("inventory", self.inv), out=lines.append, **kw)
        return rep, "\n".join(lines)

    def decide(self, target, **decisions):
        data = migrate.load(target)
        data["decisions"].update(decisions)
        migrate.save(target, data)


class Init(MigrationTest):
    def test_decisions(self):
        t = self.init()
        self.assertEqual(t, self.root / "debian-13")
        d = migrate.load(t)["decisions"]
        self.assertEqual(d["build-tools"], {"action": "keep"})
        self.assertEqual(d["legacy-tool"]["action"], "replace")
        self.assertEqual(d["legacy-tool"]["by"], ["example-tool-archive"])
        self.assertEqual(d["old-only"]["action"], "pending")
        self.assertIn("ubuntu 22.04", d["old-only"]["why"])
        self.assertEqual(d["battery-limit"], {"action": "keep"})      # requires_paths is ignored
        self.assertNotIn("installer-etc", d)                          # known items are not migrated

    def test_target_files(self):
        t = self.init()
        data = migrate.load(t)
        self.assertEqual(data["target"]["VERSION_CODENAME"], "trixie")
        self.assertEqual(data["source"], {"system": "Ubuntu 22.04.5 LTS", "arch": "x86_64",
                                          "inventory_commit": None, "created": "2026-01-02"})
        self.assertEqual((t / "target-os-release").read_text(), DEBIAN.read_text())
        self.assertEqual((t / "config.yaml").read_text(), (self.inv / "config.yaml").read_text())
        self.assertIn("## Migration decisions", (t / "INVENTORY.md").read_text())

    def test_target_catalog(self):
        t = self.init()
        cat = catalog.load(t)
        ids = [i.id for i in cat.all_items()]
        self.assertNotIn("legacy-tool", ids)
        self.assertNotIn("old-only", ids)
        self.assertIn("example-tool-archive", ids)
        self.assertNotIn("legacy", [g.id for g in cat.groups])        # empty groups are left out
        self.assertEqual(list(cat.apt_sources), ["vscode"])            # only the sources still used
        self.assertEqual(cat.known, [])
        self.assertEqual(cat.item("example-tool-archive").replaces, ["legacy-tool"])
        self.assertEqual(cat.item("some-licensed-app").manual, catalog.load(self.inv).item("some-licensed-app").manual)

    def test_only_referenced_payload_is_copied(self):
        t = self.init()
        files = sorted(str(p.relative_to(t / "snapshot")) for p in (t / "snapshot").rglob("*") if p.is_file())
        self.assertEqual(files, [
            "apt-sources/keyrings/usr/share/keyrings/microsoft.gpg",
            "apt-sources/vscode.sources",
            "dotfiles/.bashrc",
            "dotfiles/.config/Code/User/settings.json",
            "dotfiles/.gitconfig",
            "dotfiles/.profile",
            "etc/files/systemd/resolved.conf.d/dot.conf",
            "repos.tsv",
            "systemd/system/battery-threshold.service",
        ])
        self.assertEqual((t / "snapshot/repos.tsv").read_text(),
                         "~/Projects/demo\thttps://git.example.com/demo.git\tmain\t0\t0\n")

    def test_ubuntu_target(self):
        t = self.init(NOBLE)
        self.assertEqual(t.name, "ubuntu-24.04")
        d = migrate.load(t)["decisions"]
        self.assertEqual(d["legacy-tool"]["action"], "replace")
        self.assertEqual(d["old-only"]["action"], "pending")

    def test_refuses_existing_target_and_bad_input(self):
        self.init()
        with self.assertRaises(migrate.MigrationError):
            self.init()
        partial = self.root / "partial-os-release"
        partial.write_text("ID=debian\n")
        with self.assertRaises(migrate.MigrationError):
            self.init(partial)
        with self.assertRaises(migrate.MigrationError):
            migrate.init(self.root / "debian-13", NOBLE, self.root / "x", out=lambda _: None)

    def test_restore_uses_the_payload(self):
        t = self.init()
        ctx = Context(data_dir=t)
        self.assertEqual(ctx.os["ID"], "debian")                       # defaults to target-os-release
        from machine_inventory import plan
        cat = catalog.load(t)
        items, _ = plan.select(cat, ctx.os, ["vscode", "demo", "battery-limit"])
        text = "\n".join(c for s in plan.build(ctx, cat, items) for c in s.commands)
        self.assertIn(str(t / "snapshot/apt-sources/vscode.sources"), text)
        self.assertIn("git clone -b main https://git.example.com/demo.git", text)


class Check(MigrationTest):
    def test_fresh_target_lists_open_work(self):
        t = self.init()
        rep, out = self.check(t)
        self.assertTrue(rep.failed)
        self.assertIn("FAIL   old-only: pending", out)
        self.assertIn("FAIL   vlc: not in the debian 13 archive (amd64): vlc", out)
        self.assertIn("WARN   vscode: third-party source vscode (suite: stable)", out)
        self.assertIn("must come from that source: code", out)
        self.assertNotIn("build-tools", out)

    def test_clean_after_decisions(self):
        t = self.init()
        self.decide(t, **{"old-only": {"action": "drop", "why": "not needed any more"}})
        rep, out = self.check(t, names=ARCHIVE + ["vlc"])
        self.assertFalse(rep.failed, out)

    def test_items_added_to_the_inventory_later(self):
        t = self.init()
        data = yaml.safe_load((self.inv / "catalog.yaml").read_text())
        data["groups"][0]["items"].append({"id": "new-thing", "apt": ["htop"]})
        (self.inv / "catalog.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
        _, out = self.check(t)
        self.assertIn("FAIL   new-thing: in the inventory but has no decision", out)

    def test_inconsistent_decisions(self):
        t = self.init()
        self.decide(t, **{
            "old-only": {"action": "replace", "by": ["nope"], "why": "x"},
            "vlc": {"action": "drop", "why": "x"},                     # still in catalog.yaml
            "git": {"action": "replace", "by": ["build-tools"]},       # no why, still in catalog
        })
        _, out = self.check(t, offline=True)
        self.assertIn("old-only: replaced by 'nope', which is not in catalog.yaml", out)
        self.assertIn("vlc: decision is drop, but the item is still in catalog.yaml", out)
        self.assertIn("git: decision replace has no `why`", out)

    def test_undocumented_catalog_item(self):
        t = self.init()
        data = yaml.safe_load((t / "catalog.yaml").read_text())
        data["groups"][0]["items"].append({"id": "extra", "apt": ["git"]})
        (t / "catalog.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
        _, out = self.check(t, offline=True)
        self.assertIn("FAIL   extra: in catalog.yaml but not in migration.yaml", out)

    def test_missing_payload_and_incompatible_item(self):
        t = self.init()
        (t / "snapshot/dotfiles/.bashrc").unlink()
        data = yaml.safe_load((t / "catalog.yaml").read_text())
        data["groups"][0]["items"][0]["compat"] = {"ubuntu": ["22.04"]}
        (t / "catalog.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
        _, out = self.check(t, offline=True)
        self.assertIn("FAIL   payload: shell: no saved dotfile matches ~/.bashrc", out)
        self.assertIn("FAIL   build-tools: not compatible with debian 13", out)

    def test_unknown_distro_is_not_an_error(self):
        osr = self.root / "os-release"
        osr.write_text('ID=someos\nVERSION_ID="7"\nVERSION_CODENAME=seven\nPRETTY_NAME="Some OS 7"\n')
        t = self.init(osr)
        self.decide(t, **{i: {"action": "drop", "why": "x"} for i in ("old-only", "legacy-tool", "example-tool-archive")})
        rep, out = self.check(t, fetch=lambda url: self.fail("must not download"))
        self.assertIn("not supported yet", out)
        self.assertIn("archives.py", out)
        self.assertFalse(rep.failed, out)

    def test_download_failure_fails_unless_offline(self):
        t = self.init()

        def broken(url):
            raise archives.ArchiveError("network down")
        rep, out = self.check(t, fetch=broken)
        self.assertIn("FAIL   package check failed: network down", out)
        _, out = self.check(t, offline=True, fetch=broken)
        self.assertIn("WARN   package check skipped (--offline)", out)

    def test_works_without_the_inventory(self):
        t = self.init()
        _, out = self.check(t, inventory=None, offline=True)
        self.assertIn("inventory not found", out)
        self.assertIn("FAIL   old-only: pending", out)

    def test_cached_indexes(self):
        t = self.init()
        cache, calls = self.root / "cache", []
        self.check(t, fetch=fetcher(ARCHIVE, calls), cache=cache)
        n = len(calls)
        self.assertGreater(n, 0)
        self.check(t, fetch=fetcher(ARCHIVE, calls), cache=cache)
        self.assertEqual(len(calls), n)                                # second run: all from the cache
        self.check(t, fetch=fetcher(ARCHIVE, calls), cache=cache, refresh=True)
        self.assertEqual(len(calls), 2 * n)


class Archives(unittest.TestCase):
    def test_layouts(self):
        noble = {"ID": "ubuntu", "VERSION_ID": "24.04", "VERSION_CODENAME": "noble"}
        urls = [u for _, _, u in archives.index_urls(noble, "amd64")]
        self.assertEqual(urls[0], "http://archive.ubuntu.com/ubuntu/dists/noble/main/binary-amd64/Packages.xz")
        self.assertIn("http://archive.ubuntu.com/ubuntu/dists/noble-updates/universe/binary-amd64/Packages.xz", urls)
        self.assertTrue(archives.index_urls(noble, "arm64")[0][2].startswith("http://ports.ubuntu.com/"))
        trixie = {"ID": "debian", "VERSION_ID": "13", "VERSION_CODENAME": "trixie"}
        self.assertIn("http://deb.debian.org/debian/dists/trixie/non-free-firmware/binary-arm64/Packages.xz",
                      [u for _, _, u in archives.index_urls(trixie, "arm64")])
        with self.assertRaises(archives.Unsupported):
            archives.index_urls({"ID": "someos", "VERSION_CODENAME": "x"}, "amd64")
        with self.assertRaises(archives.ArchiveError):
            archives.index_urls({"ID": "debian", "VERSION_ID": "13"}, "amd64")

    def test_wrong_codename(self):
        osr = {"ID": "debian", "VERSION_ID": "13", "VERSION_CODENAME": "nope"}
        with self.assertRaises(archives.ArchiveError) as e:
            archives.packages(osr, "amd64", fetch=lambda url: None)
        self.assertIn("codename", str(e.exception))

    def test_index_parsing(self):
        names, read = archives.packages({"ID": "debian", "VERSION_ID": "13", "VERSION_CODENAME": "trixie"}, "amd64",
                                        fetch=lambda url: packages_index(["a"], provides=["virt"])
                                        if url.endswith("/trixie/main/binary-amd64/Packages.xz") else None)
        self.assertEqual(names, {"a", "real-virt", "virt", "other-virt"})
        self.assertEqual(read, ["trixie/main"])

    def test_names(self):
        self.assertEqual(archives.base_name("libfoo:i386"), "libfoo")
        self.assertEqual(archives.base_name("bar=1.2-3"), "bar")
        self.assertEqual(archives.base_name("baz/noble-backports"), "baz")
        self.assertEqual(archives.deb_arch("x86_64"), "amd64")
        self.assertEqual(archives.deb_arch("aarch64"), "arm64")


class Replaces(unittest.TestCase):
    def test_validation(self):
        def cat(**kw):
            return {"groups": [{"id": "g", "items": [{"id": "a"}, dict({"id": "b"}, **kw)]}]}
        self.assertEqual(parse(cat(replaces="a")).item("b").replaces, ["a"])
        for bad in (cat(replaces=["b"]), cat(replaces=["nope"]), cat(replaces={"x": 1})):
            with self.subTest(bad=bad), self.assertRaises(CatalogError):
                parse(bad)
        # A migration target's catalog names inventory items it does not contain.
        self.assertEqual(parse(cat(replaces=["gone"]), strict_refs=False).item("b").replaces, ["gone"])

    def test_dump_round_trip(self):
        cat = catalog.load(EXAMPLES)
        self.assertEqual(parse(yaml.safe_load(catalog.dump(cat, "header"))), cat)
        self.assertTrue(catalog.dump(cat, "header").startswith("# header\n"))


class Cli(MigrationTest):
    def run_mi(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(list(args))
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else (err.write(str(e.code)) and 1)
        return rc, out.getvalue() + err.getvalue()

    def test_migration_names(self):
        self.assertEqual(migration_dir("debian-13").parts[-2:], ("migration", "debian-13"))
        self.assertEqual(migration_dir("/tmp/x/debian-13"), Path("/tmp/x/debian-13"))

    def test_init_and_check_commands(self):
        rc, out = self.run_mi("--data", str(self.inv), "migrate", "init", str(DEBIAN), "--out", str(self.root))
        self.assertEqual(rc, 0, out)
        self.assertIn("decisions: keep 13, replace 1, pending 1", out)
        rc, out = self.run_mi("--data", str(self.inv), "migrate", "check", str(self.root / "debian-13"), "--offline")
        self.assertEqual(rc, 1)
        self.assertIn("old-only: pending", out)
        rc, out = self.run_mi("--data", str(self.inv), "migrate", "init", str(DEBIAN), "--out", str(self.root))
        self.assertEqual(rc, 2)
        self.assertIn("already exists", out)

    def test_export_and_check_are_refused(self):
        t = self.init()
        for cmd in ("export", "check"):
            rc, out = self.run_mi("--data", str(t), cmd)
            self.assertNotEqual(rc, 0)
            self.assertIn("migration target", out)
            self.assertIn("mi migrate check debian-13", out)
            self.assertIn("mi migrate debian-13 --dry-run", out)

    def test_migrate_target_commands(self):
        t = self.init()
        rc, out = self.run_mi("migrate", "catalog", str(t))
        self.assertEqual(rc, 0)
        self.assertIn("Target: Debian GNU/Linux 13 (trixie)", out)
        rc, out = self.run_mi("migrate", str(t), "--dry-run", "--all-defaults")      # `mi migrate <target>`
        self.assertEqual(rc, 0)
        self.assertIn("Target system: Debian GNU/Linux 13 (trixie)", out)
        self.assertIn("dry run", out)
        self.assertNotIn("Incompatible", out)
        rc, out = self.run_mi("migrate", str(self.inv), "--dry-run")                 # not a migration target
        self.assertNotEqual(rc, 0)
        self.assertIn("not a migration target", out)
        rc, out = self.run_mi("restore", "--migration", str(t), "--dry-run")         # the old option is gone
        self.assertEqual(rc, 2)

    def test_route_migrate(self):
        self.assertEqual(cli._route_migrate(["migrate", "debian-13", "-n"]), ["migrate", "restore", "debian-13", "-n"])
        self.assertEqual(cli._route_migrate(["--data", "x", "migrate", "check", "debian-13"]),
                         ["--data", "x", "migrate", "check", "debian-13"])
        self.assertEqual(cli._route_migrate(["migrate", "-h"]), ["migrate", "-h"])
        self.assertEqual(cli._route_migrate(["restore", "-n"]), ["restore", "-n"])

    def test_real_restore_needs_the_target_release(self):
        osr = self.root / "os-release"
        osr.write_text('ID=someos\nVERSION_ID="7"\nVERSION_CODENAME=seven\nPRETTY_NAME="Some OS 7"\n')
        t = self.init(osr)
        rc, out = self.run_mi("migrate", str(t), "--select", "build-tools", "--yes")
        self.assertEqual(rc, 2)
        self.assertIn("this migration target is for someos 7", out)

    def test_render_adds_decisions(self):
        t = self.init()
        self.decide(t, **{"old-only": {"action": "drop", "why": "gone", "todo": "install by hand if needed"}})
        rc, _ = self.run_mi("migrate", "render", str(t))
        self.assertEqual(rc, 0)
        text = (t / "INVENTORY.md").read_text()
        self.assertIn("# Migration target: Debian GNU/Linux 13 (trixie)", text)
        self.assertIn("| `old-only` | drop | - | gone | install by hand if needed |", text)
        self.assertIn("| `legacy-tool` | replace | `example-tool-archive` |", text)
        self.assertIn("- `old-only`: install by hand if needed", text)


if __name__ == "__main__":
    unittest.main()

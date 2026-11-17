import io
import unittest
from contextlib import redirect_stdout

from machine_inventory import catalog, cli, plan, ui
from machine_inventory.modules import apt
from machine_inventory.modules.base import PHASE_APT, PHASE_MANUAL, PHASE_POST, PHASE_SOURCES
from tests.helpers import FIXTURES, ctx_for, make_data


def ids(items):
    return [i.id for i in items]


class Selection(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx_for(self)
        self.cat = catalog.load(self.ctx.data)

    def test_group_selects_its_defaults(self):
        items, warnings = plan.select(self.cat, self.ctx.os, ["dev"])
        self.assertEqual(ids(items), ["build-tools", "git", "vscode"])
        self.assertEqual(warnings, [])

    def test_item_and_group_keep_catalog_order(self):
        items, _ = plan.select(self.cat, self.ctx.os, ["discord", "build-tools"])
        self.assertEqual(ids(items), ["build-tools", "discord"])

    def test_all_defaults(self):
        items, _ = plan.select(self.cat, self.ctx.os, all_defaults=True)
        self.assertEqual(ids(items), ["build-tools", "git", "vscode", "vlc", "shell"])

    def test_unknown_and_known_ids_are_errors(self):
        for tok in ("nope", "installer-etc"):
            with self.assertRaises(plan.SelectionError):
                plan.select(self.cat, self.ctx.os, [tok])

    def test_incompatible_item_is_skipped_on_noble(self):
        ctx = ctx_for(self, "ubuntu-24.04")
        items, warnings = plan.select(self.cat, ctx.os, ["legacy-tool"])
        self.assertEqual(items, [])
        self.assertIn("legacy-tool", warnings[0])
        items, _ = plan.select(self.cat, ctx.os, ["legacy-tool"], include_incompatible=True)
        self.assertEqual(ids(items), ["legacy-tool"])

    def test_offered_hides_incompatible(self):
        ctx = ctx_for(self, "ubuntu-24.04")
        self.assertNotIn("legacy", [g.id for g, _ in plan.offered(self.cat, ctx.os)])
        self.assertIn("legacy", [g.id for g, _ in plan.offered(self.cat, ctx.os, True)])
        self.assertIn("legacy", [g.id for g, _ in plan.offered(self.cat, self.ctx.os)])


class Steps(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx_for(self)
        self.cat = catalog.load(self.ctx.data)

    def build(self, *tokens, **kw):
        items, _ = plan.select(self.cat, self.ctx.os, list(tokens), **kw)
        return plan.build(self.ctx, self.cat, items)

    def test_phases_are_ordered(self):
        steps = self.build("vscode", "git", "some-licensed-app")
        phases = [s.phase for s in steps]
        self.assertEqual(phases, sorted(phases))
        self.assertEqual(phases[0], PHASE_SOURCES)
        self.assertIn(PHASE_POST, phases)
        self.assertEqual(phases[-1], PHASE_MANUAL)

    def test_only_selected_things(self):
        steps = self.build("build-tools")
        text = "\n".join(c for s in steps for c in s.commands)
        self.assertIn("sudo apt install -y build-essential cmake ninja-build pkg-config", text)
        self.assertNotIn("code", text)
        self.assertNotIn("sources.list.d", text)

    def test_sources_copy_keyring_and_file(self):
        steps = self.build("vscode")
        src = [s for s in steps if s.phase == PHASE_SOURCES][0]
        self.assertTrue(any("keyrings/usr/share/keyrings/microsoft.gpg /usr/share/keyrings/microsoft.gpg" in c
                            for c in src.commands))
        self.assertTrue(any(c.endswith("/etc/apt/sources.list.d/vscode.sources") for c in src.commands))
        apt_step = [s for s in steps if s.phase == PHASE_APT][0]
        self.assertEqual(apt_step.commands[0], "sudo apt update")

    def test_key_url_source_gets_signed_by(self):
        steps = self.build("legacy-tool")
        src = [s for s in steps if s.phase == PHASE_SOURCES][0]
        self.assertIn("curl -fsSL https://repo.example.com/key.gpg", src.commands[0])
        self.assertIn("[signed-by=/usr/share/keyrings/example-archive-keyring.gpg]", src.commands[1])

    def test_restore_exclude(self):
        self.ctx.cfg["restore_exclude"] = ["cmake"]
        text = "\n".join(c for s in self.build("build-tools") for c in s.commands)
        self.assertNotIn("cmake", text)

    def test_dotfiles_etc_repos(self):
        text = "\n".join(c for s in self.build("shell", "dns-over-tls", "vscode") for c in s.commands)
        self.assertIn("/.bashrc", text)
        self.assertIn(".bak-", text)
        self.assertIn("sudo install -D -m 644", text)
        self.assertIn("/etc/systemd/resolved.conf.d/dot.conf", text)
        self.assertIn("settings.json", text)

    def test_missing_snapshot_files_are_skipped(self):
        cmds = "\n".join(c for s in self.build("shell") for c in s.commands)
        self.assertIn(".bashrc", cmds)
        self.assertNotIn(".gitconfig", cmds)       # not in the fake snapshot

    def test_rewrite_signed_by(self):
        self.assertEqual(apt._rewrite_signed_by("deb [arch=amd64] http://x jammy main", "/k.gpg"),
                         ["deb [arch=amd64 signed-by=/k.gpg] http://x jammy main"])
        self.assertEqual(apt._rewrite_signed_by("# comment\ndeb [signed-by=/a] http://x y z", "/k"),
                         ["# comment", "deb [signed-by=/a] http://x y z"])


class TextWizard(unittest.TestCase):
    def test_groups_then_items(self):
        ctx = ctx_for(self)
        cat = catalog.load(ctx.data)
        answers = iter(["",          # groups: accept defaults (dev, desktop, config)
                        "2", "",     # dev: untick git, accept
                        "",          # desktop: accept (vlc)
                        "1", ""])    # config: untick shell, accept
        out = []
        items = ui.wizard(cat, ctx.os, use_whiptail=False, ask=lambda _: next(answers), out=out.append)
        self.assertEqual(ids(items), ["build-tools", "vscode", "vlc"])

    def test_cancel(self):
        ctx = ctx_for(self)
        cat = catalog.load(ctx.data)
        self.assertIsNone(ui.wizard(cat, ctx.os, use_whiptail=False, ask=lambda _: "q", out=lambda _: None))


class Cli(unittest.TestCase):
    def run_mi(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(list(args))
        return rc, buf.getvalue()

    def test_dry_run(self):
        data = make_data(self)
        rc, out = self.run_mi("--data", str(data), "restore", "--dry-run", "--select", "dev",
                              "--os-release", str(FIXTURES / "ubuntu-24.04"))
        self.assertEqual(rc, 0)
        self.assertIn("Target system: Ubuntu 24.04.1 LTS", out)
        self.assertIn("dry run", out)

    def test_render(self):
        data = make_data(self)
        rc, _ = self.run_mi("--data", str(data), "render")
        self.assertEqual(rc, 0)
        text = (data / "INVENTORY.md").read_text()
        self.assertIn("## Development tools", text)
        self.assertIn("## Known, not restored", text)


if __name__ == "__main__":
    unittest.main()

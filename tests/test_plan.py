import io
import unittest
from contextlib import redirect_stdout

from machine_inventory import catalog, cli, plan, ui
from machine_inventory.modules import apt, systemd
from machine_inventory.modules.base import PARTIAL, Step
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
        self.assertIn("apt-install build-essential cmake ninja-build pkg-config", text)
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


class AptInstall(unittest.TestCase):
    def run_install(self, pkgs, known, broken=()):
        calls = []

        def run(args):
            calls.append(args[4:])
            return 1 if any(p in broken for p in args[4:]) else 0
        skipped, failed = apt.install_packages(pkgs, run=run, out=lambda _: None,
                                               locate=lambda ps: None if known is None else set(known) & set(ps))
        return skipped, failed, calls

    def test_unknown_packages_are_skipped_not_fatal(self):
        skipped, failed, calls = self.run_install(["a", "neofetch", "b"], known=["a", "b"])
        self.assertEqual((skipped, failed), (["neofetch"], []))
        self.assertEqual(calls, [["a", "b"]])

    def test_batch_failure_falls_back_to_one_by_one(self):
        skipped, failed, calls = self.run_install(["a", "bad", "b"], known=["a", "bad", "b"], broken=["bad"])
        self.assertEqual(failed, ["bad"])
        self.assertEqual(calls, [["a", "bad", "b"], ["a"], ["bad"], ["b"]])

    def test_unreadable_lists_still_install(self):
        skipped, failed, calls = self.run_install(["a"], known=None)
        self.assertEqual((skipped, failed, calls), ([], [], [["a"]]))


class Execute(unittest.TestCase):
    def test_soft_failures_and_partial_do_not_stop_the_run(self):
        steps = [Step(20, "apt packages", ["apt-ok"], soft=True),
                 Step(30, "snaps", ["snap-bad", "snap-ok"], soft=True),
                 Step(40, "partial", ["half"])]
        codes = {"apt-ok": 0, "snap-bad": 1, "snap-ok": 0, "half": PARTIAL}
        ran = []
        finished, problems = plan.execute(steps, ask=lambda _: self.fail("must not ask"),
                                          out=lambda _: None, run=lambda c: ran.append(c) or codes[c])
        self.assertTrue(finished)
        self.assertEqual(ran, ["apt-ok", "snap-bad", "snap-ok", "half"])
        self.assertEqual(len(problems), 2)

    def test_hard_failure_asks_and_can_abort(self):
        steps = [Step(10, "sources", ["bad", "never"])]
        ran = []
        finished, _ = plan.execute(steps, ask=lambda _: "a", out=lambda _: None,
                                   run=lambda c: ran.append(c) or 1)
        self.assertFalse(finished)
        self.assertEqual(ran, ["bad"])

    def test_keep_going_never_asks(self):
        steps = [Step(10, "sources", ["bad", "next"])]
        finished, problems = plan.execute(steps, ask=lambda _: self.fail("must not ask"), out=lambda _: None,
                                          keep_going=True, run=lambda c: 1 if c == "bad" else 0)
        self.assertTrue(finished)
        self.assertEqual(len(problems), 1)


class UnitTemplates(unittest.TestCase):
    UNIT = ("[Service]\nUser=alice\nGroup=staff\nWorkingDirectory=/srv/users/alice/app\n"
            "ExecStart=/srv/users/alice/app/run.sh --data '/srv/users/alice'\nEnvironment=X=/srv/users/alice2/keep\n")

    def test_round_trip_for_another_user(self):
        t = systemd.to_template(self.UNIT, "/srv/users/alice", "alice", "staff")
        self.assertNotIn("/srv/users/alice/", t)
        self.assertIn("User=@USER@", t)
        self.assertIn("ExecStart=@HOME@/app/run.sh --data '@HOME@'", t)
        self.assertIn("/srv/users/alice2/keep", t)            # another user's path is left alone
        out = systemd.from_template(t, "/srv/users/bob", "bob", "bob")
        self.assertIn("WorkingDirectory=/srv/users/bob/app", out)
        self.assertIn("User=bob", out)

    def test_root_user_is_not_templated(self):
        t = systemd.to_template("User=root\n", "/srv/users/alice", "alice", "alice")
        self.assertEqual(t, "User=root\n")

    def test_restore_command_fills_placeholders(self):
        import subprocess
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(d)]))
        (d / "u.service").write_text("ExecStart=@HOME@/x\nUser=@USER@\n")
        cmd = systemd._install_cmd(d / "u.service", d / "out/u.service", sudo=False)
        subprocess.run(["bash", "-c", cmd], check=True)
        text = (d / "out/u.service").read_text()
        self.assertIn(f"ExecStart={Path.home()}/x", text)
        self.assertNotIn("@", text)


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

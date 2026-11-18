import unittest

from machine_inventory.modules import gnome


class FakeSystem:
    """Answers the commands install_extensions runs; records the ones that change something."""

    def __init__(self, shell="GNOME Shell 50.1", installed=(), enabled="['system-dock@example.org']", fail=()):
        self.shell, self.installed, self.enabled, self.fail = shell, list(installed), enabled, set(fail)
        self.calls = []

    def run(self, args, check=True):
        self.calls.append(args)
        if args[:2] == ["gnome-shell", "--version"]:
            return self.shell
        if args[:2] == ["gnome-extensions", "list"]:
            return "\n".join(self.installed)
        if args[:2] == ["gnome-extensions", "install"]:
            return None if any(u in args[-1] for u in self.fail) else ""
        if args[:3] == ["gsettings", "get", "org.gnome.shell"]:
            return self.enabled
        if args[:3] == ["gsettings", "set", "org.gnome.shell"]:
            self.enabled = args[4]
            return ""
        raise AssertionError(args)


INFO = {
    "good@example.org": {"shell_version_map": {"49": {"version": 7}, "50": {"version": 8}}},
    "old@example.org": {"shell_version_map": {"43": {"version": 44}}},
}


def fetch(url):
    uuid = url.split("uuid=")[1]
    if uuid not in INFO:
        raise OSError("HTTP Error 404")
    return INFO[uuid]


class InstallExtensionsTest(unittest.TestCase):
    def install(self, sysm, uuids):
        downloads, lines = [], []
        missing = gnome.install_extensions(uuids, run=sysm.run, fetch_json=fetch,
                                           download=lambda url, dest: downloads.append(url), out=lines.append)
        return missing, downloads, "\n".join(lines)

    def test_installs_supported_and_skips_the_rest(self):
        sysm = FakeSystem()
        missing, downloads, out = self.install(sysm, ["good@example.org", "old@example.org", "unknown@example.org"])
        self.assertEqual(missing, ["old@example.org", "unknown@example.org"])
        self.assertEqual(downloads, [f"{gnome.EGO}/download-extension/{'good@example.org'}.shell-extension.zip?shell_version=50"])
        self.assertIn("no version for GNOME Shell 50", out)
        self.assertIn("lookup failed", out)
        self.assertEqual(sysm.enabled, "['system-dock@example.org', 'good@example.org']")

    def test_already_installed_is_only_enabled(self):
        sysm = FakeSystem(installed=["good@example.org"], enabled="@as []")
        missing, downloads, _ = self.install(sysm, ["good@example.org"])
        self.assertEqual((missing, downloads), ([], []))
        self.assertEqual(sysm.enabled, "['good@example.org']")

    def test_no_change_when_already_enabled(self):
        sysm = FakeSystem(installed=["good@example.org"], enabled="['good@example.org']")
        self.install(sysm, ["good@example.org"])
        self.assertFalse(any(c[:2] == ["gsettings", "set"] for c in sysm.calls))

    def test_install_failure_is_reported(self):
        sysm = FakeSystem(fail=["good@example.org"])
        missing, _, out = self.install(sysm, ["good@example.org"])
        self.assertEqual(missing, ["good@example.org"])
        self.assertIn("gnome-extensions install failed", out)

    def test_without_gnome_shell(self):
        sysm = FakeSystem(shell=None)
        missing, _, out = self.install(sysm, ["good@example.org"])
        self.assertEqual(missing, ["good@example.org"])
        self.assertIn("gnome-shell not found", out)

    def test_helpers(self):
        self.assertEqual(gnome.shell_major("GNOME Shell 50.1"), "50")
        self.assertIsNone(gnome.shell_major(""))
        self.assertEqual(gnome.parse_strv("@as []"), [])
        self.assertEqual(gnome.parse_strv("['a', 'b']"), ["a", "b"])
        self.assertEqual(gnome.format_strv(["a@b", "x'y"]), "['a@b', 'x\\'y']")


if __name__ == "__main__":
    unittest.main()

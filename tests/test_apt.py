import unittest

from machine_inventory.modules import apt

HISTORY = """
Start-Date: 2026-04-20  10:00:00
Commandline: apt-get --yes -oDebug::pkgDepCache::AutoInstall=yes install ubuntu-desktop grub-efi-amd64
Install: ubuntu-desktop:amd64 (1.0), grub-efi-amd64:amd64 (2.0)
End-Date: 2026-04-20  10:05:00

Start-Date: 2026-11-18  11:50:00
Commandline: apt-get install -y --no-install-recommends terminator git ./local.deb pandoc=3.1
Requested-By: user (1000)
Install: terminator:amd64 (2.1), python3-cairo:amd64 (1.2, automatic), local:amd64 (1.0)
End-Date: 2026-11-18  11:51:00

Start-Date: 2026-11-18  12:00:00
Commandline: apt install curl
Requested-By: user (1000)
End-Date: 2026-11-18  12:00:01
""".lstrip("\n").splitlines(keepends=True)


class RequestedPackages(unittest.TestCase):
    def test_only_requested_by_entries_and_named_packages(self):
        names = apt.requested_packages(HISTORY)
        # named on the command line (curl was already installed: no Install: line), or installed on request
        self.assertTrue({"terminator", "git", "pandoc", "curl", "local"} <= names)
        self.assertNotIn("python3-cairo", names)       # a dependency
        self.assertNotIn("ubuntu-desktop", names)      # image build / installer: no Requested-By
        self.assertNotIn("grub-efi-amd64", names)
        self.assertFalse(any(n.startswith("-") or "/" in n for n in names))

    def test_user_added(self):
        manual = ["curl", "git", "grub-efi-amd64", "terminator", "wine:i386"]
        self.assertEqual(apt.user_added(manual, initial={"curl", "grub-efi-amd64"}), ["git", "terminator", "wine:i386"])
        self.assertEqual(apt.user_added(manual, requested={"curl", "git", "terminator", "wine"}),
                         ["curl", "git", "terminator", "wine:i386"])


if __name__ == "__main__":
    unittest.main()

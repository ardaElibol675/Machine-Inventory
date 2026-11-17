import unittest

from machine_inventory import catalog
from machine_inventory.catalog import CatalogError, compat_matches, parse
from machine_inventory.config import EXAMPLES, load_yaml, read_os_release
from tests.helpers import FIXTURES


def item(**kw):
    return {"groups": [{"id": "g", "items": [dict({"id": "a"}, **kw)]}]}


class CatalogParsing(unittest.TestCase):
    def test_example_catalog_parses(self):
        cat = catalog.load(EXAMPLES)
        self.assertEqual([g.id for g in cat.groups], ["dev", "desktop", "legacy", "config"])
        vs = cat.item("vscode")
        self.assertEqual(vs.get("apt"), ["code"])
        self.assertEqual(vs.get("apt_sources"), ["vscode"])
        self.assertTrue(vs.default)
        self.assertEqual(cat.item("installer-etc").group, "known")

    def test_scalar_becomes_list(self):
        cat = parse(item(apt="vim"))
        self.assertEqual(cat.item("a").get("apt"), ["vim"])

    def test_claimed_includes_known(self):
        cat = catalog.load(EXAMPLES)
        self.assertIn("default/keyboard", cat.claimed("etc"))
        self.assertNotIn("default/keyboard", cat.claimed("etc", include_known=False))

    def test_errors(self):
        bad = [
            item(unknown_key=1),
            item(apt_sources=["nope"]),
            {"groups": [{"id": "g", "items": [{"id": "a"}, {"id": "a"}]}]},
            {"groups": [{"id": "a", "items": [{"id": "a"}]}]},
            {"apt_sources": {"s": {"keyring": "/k"}}},
            {"apt_sources": {"s": {"file": "x.list", "key_url": "https://k"}}},
            item(compat=["22.04"]),
            {"groups": [{"items": []}]},
        ]
        for data in bad:
            with self.subTest(data=data):
                with self.assertRaises(CatalogError):
                    parse(data)


class Compat(unittest.TestCase):
    def setUp(self):
        self.jammy = read_os_release(FIXTURES / "ubuntu-22.04")
        self.noble = read_os_release(FIXTURES / "ubuntu-24.04")
        self.mint = read_os_release(FIXTURES / "linuxmint-21.3")

    def test_empty_matches_everything(self):
        self.assertTrue(compat_matches({}, self.noble))

    def test_version_list(self):
        c = {"ubuntu": ["22.04"]}
        self.assertTrue(compat_matches(c, self.jammy))
        self.assertFalse(compat_matches(c, self.noble))

    def test_any_version(self):
        self.assertTrue(compat_matches({"ubuntu": []}, self.noble))

    def test_derivative_needs_any_version(self):
        # Mint is ID_LIKE=ubuntu: it matches "any Ubuntu" but not a specific Ubuntu version.
        self.assertTrue(compat_matches({"ubuntu": []}, self.mint))
        self.assertFalse(compat_matches({"ubuntu": ["22.04"]}, self.mint))
        self.assertTrue(compat_matches({"linuxmint": ["21.3"]}, self.mint))

    def test_other_distro(self):
        self.assertFalse(compat_matches({"debian": ["12"]}, self.jammy))

    def test_version_ranges(self):
        resolute = read_os_release(FIXTURES / "ubuntu-26.04")
        self.assertTrue(compat_matches({"ubuntu": [">=24.04"]}, self.noble))
        self.assertTrue(compat_matches({"ubuntu": [">=24.04"]}, resolute))
        self.assertFalse(compat_matches({"ubuntu": [">=24.04"]}, self.jammy))
        self.assertTrue(compat_matches({"ubuntu": ["<=24.04"]}, self.jammy))
        self.assertFalse(compat_matches({"ubuntu": ["<=24.04"]}, resolute))
        self.assertTrue(compat_matches({"ubuntu": ["<26.04", "26.10"]}, self.noble))
        self.assertFalse(compat_matches({"ubuntu": [">22.04"]}, self.jammy))


class HardwareConditions(unittest.TestCase):
    def test_requires_paths(self):
        import tempfile
        here = tempfile.gettempdir()
        cat = parse({"groups": [{"id": "g", "items": [
            {"id": "ok", "requires_paths": [here]},
            {"id": "glob", "requires_paths": [here[:-1] + "*"]},
            {"id": "missing", "requires_paths": ["/nonexistent/asus-nb-wmi"]}]}]})
        jammy = read_os_release(FIXTURES / "ubuntu-22.04")
        self.assertTrue(cat.item("ok").compatible(jammy))
        self.assertTrue(cat.item("glob").compatible(jammy))
        self.assertFalse(cat.item("missing").compatible(jammy))
        self.assertIn("/nonexistent/asus-nb-wmi", cat.item("missing").why_incompatible(jammy))


class ExampleFiles(unittest.TestCase):
    def test_example_config_keys_are_known(self):
        from machine_inventory.config import DEFAULTS
        self.assertLessEqual(set(load_yaml(EXAMPLES / "config.yaml")), set(DEFAULTS))


if __name__ == "__main__":
    unittest.main()

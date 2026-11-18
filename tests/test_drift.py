import unittest

import yaml

from machine_inventory import catalog
from machine_inventory.modules import base, dotfiles, etc, gnome, repos, systemd
from tests.helpers import ctx_for


class MissingItems(unittest.TestCase):
    def test_promised_but_not_on_this_machine(self):
        ctx = ctx_for(self)
        data = yaml.safe_load((ctx.data / "catalog.yaml").read_text())
        data["groups"].append({"id": "extra", "name": "Extra", "items": [
            {"id": "present", "name": "On the machine", "repos": ["~/Projects/demo"],
             "dotfiles": ["~/.bashrc"], "etc": ["systemd/resolved.conf.d/dot.conf"]},
            {"id": "absent", "name": "Not on the machine", "repos": ["~/Projects/gone"],
             "systemd": ["system/gone.service"], "dotfiles": ["~/.gone"], "etc": ["gone.conf"],
             "gnome_extensions": ["gone@example.com"], "dconf": ["/org/gone/"]},
            {"id": "other-hardware", "name": "Hidden here", "repos": ["~/Projects/hw"],
             "requires_paths": ["/nonexistent/device"]},
        ]})
        (ctx.data / "catalog.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
        cat = catalog.load(ctx.data)
        lines = []
        rep = base.Report(out=lines.append)
        for mod in (repos, systemd, dotfiles, etc, gnome):
            mod.drift(ctx, cat, rep)
        text = "\n".join(lines)
        for entry in ("~/Projects/gone", "system/gone.service", "~/.gone", "gone.conf", "gone@example.com", "/org/gone/"):
            self.assertIn(f"catalog item 'absent' is missing: {entry}", text)
        self.assertNotIn("'present'", text)
        self.assertNotIn("other-hardware", text)


if __name__ == "__main__":
    unittest.main()

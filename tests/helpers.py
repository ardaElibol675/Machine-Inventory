"""Builds a throw-away data directory from examples/inventory plus a tiny fake snapshot."""
import shutil
import tempfile
from pathlib import Path

from machine_inventory.config import EXAMPLES, Context

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def make_data(test):
    tmp = Path(tempfile.mkdtemp(prefix="mi-test-"))
    test.addCleanup(shutil.rmtree, tmp)
    for name in ("config.yaml", "catalog.yaml"):
        shutil.copyfile(EXAMPLES / name, tmp / name)
    snap = tmp / "snapshot"
    files = {
        "apt-sources/vscode.sources": "Types: deb\nURIs: https://packages.microsoft.com/repos/code\n"
                                      "Suites: stable\nComponents: main\n"
                                      "Signed-By: /usr/share/keyrings/microsoft.gpg\n",
        "apt-sources/keyrings/usr/share/keyrings/microsoft.gpg": "key",
        "packages/apt-user-added.txt": "build-essential\ncmake\ncode\ngit\nhtop\n",
        "dotfiles/.bashrc": "# bashrc\n",
        "dotfiles/.config/Code/User/settings.json": "{}\n",
        "etc/files/systemd/resolved.conf.d/dot.conf": "[Resolve]\n",
        "repos.tsv": "~/Projects/demo\thttps://git.example.com/demo.git\tmain\t0\t0\n",
        "system.txt": 'PRETTY_NAME="Ubuntu 22.04.5 LTS"\nID="ubuntu"\nVERSION_ID="22.04"\n'
                      'KERNEL="6.8.0-1-generic"\nARCH="x86_64"\n',
    }
    for rel, text in files.items():
        p = snap / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp


def ctx_for(test, os_name="ubuntu-22.04"):
    return Context(data_dir=make_data(test), os_release=str(FIXTURES / os_name))

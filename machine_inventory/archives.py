"""Package archives of other distributions, for `mi migrate check`.

A small registry: distro ID -> where its official archive is and how it is laid out.
`packages(os_info, arch)` downloads the binary Packages indexes of the target release
(cached in ~/.cache/machine-inventory/) and returns every package name they provide.

To support another apt-based distribution, add an entry to ARCHIVES:
    mirror        base URL of the archive (the directory that holds dists/)
    ports_mirror  optional: base URL for architectures the main mirror does not carry
    main_arches   optional: architectures on `mirror`; the rest use `ports_mirror`
    pockets       suites to read, "{codename}" is replaced by the target's VERSION_CODENAME;
                  the first one must exist, later ones (updates) are skipped if missing
    components    archive components to read
"""
import lzma
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ARCHIVES = {
    "ubuntu": {
        "mirror": "http://archive.ubuntu.com/ubuntu",
        "ports_mirror": "http://ports.ubuntu.com/ubuntu-ports",
        "main_arches": ["amd64", "i386"],
        "pockets": ["{codename}", "{codename}-updates"],
        "components": ["main", "restricted", "universe", "multiverse"],
    },
    "debian": {
        "mirror": "http://deb.debian.org/debian",
        "pockets": ["{codename}", "{codename}-updates"],
        "components": ["main", "contrib", "non-free", "non-free-firmware"],
    },
}

# `uname -m` (snapshot/system.txt ARCH) -> Debian architecture name.
DEB_ARCH = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "armhf", "i686": "i386", "i386": "i386",
            "ppc64le": "ppc64el", "s390x": "s390x", "riscv64": "riscv64"}

CACHE_MAX_AGE = 24 * 3600


class ArchiveError(RuntimeError):
    pass


class Unsupported(ArchiveError):
    pass


def deb_arch(machine):
    return DEB_ARCH.get(machine, machine)


def how_to_add():
    return ("Add an entry for it to ARCHIVES in machine_inventory/archives.py "
            "(mirror, pockets, components); see the comment at the top of that file.")


def layout(os_info):
    """The ARCHIVES entry for this release, or raise Unsupported."""
    distro = os_info.get("ID", "")
    if distro not in ARCHIVES:
        raise Unsupported(f"the package archive of '{distro or 'unknown'}' is not supported yet. {how_to_add()}")
    if not os_info.get("VERSION_CODENAME"):
        raise ArchiveError(f"the target os-release has no VERSION_CODENAME; the archive suite is unknown")
    return ARCHIVES[distro]


def index_urls(os_info, arch):
    """[(pocket, component, url)] of every Packages.xz to read, in order."""
    a = layout(os_info)
    mirror = a["mirror"]
    if a.get("ports_mirror") and arch not in a.get("main_arches", [arch]):
        mirror = a["ports_mirror"]
    out = []
    for pocket in a["pockets"]:
        suite = pocket.format(codename=os_info["VERSION_CODENAME"])
        for comp in a["components"]:
            out.append((suite, comp, f"{mirror}/dists/{suite}/{comp}/binary-{arch}/Packages.xz"))
    return out


def http_fetch(url, timeout=60):
    """bytes of url, or None if it does not exist (404). Other errors raise ArchiveError."""
    req = urllib.request.Request(url, headers={"User-Agent": "machine-inventory"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise ArchiveError(f"{url}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise ArchiveError(f"{url}: {getattr(e, 'reason', e)}") from e


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "machine-inventory"


def _cached(url, fetch, cache, refresh):
    if cache is None:
        return fetch(url)
    path = Path(cache) / url.split("://", 1)[-1]
    missing = path.with_name(path.name + ".missing")
    for p in (path, missing):
        if not refresh and p.exists() and time.time() - p.stat().st_mtime < CACHE_MAX_AGE:
            return p.read_bytes() if p == path else None
    data = fetch(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    if data is None:
        missing.write_bytes(b"")
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(data)
        missing.unlink(missing_ok=True)
    return data


def parse_packages(text):
    """Package names and the virtual names they Provide, from a Packages index."""
    names = set()
    for line in text.splitlines():
        if line.startswith("Package:"):
            names.add(line.split(":", 1)[1].strip())
        elif line.startswith("Provides:"):
            for part in line.split(":", 1)[1].split(","):
                name = part.strip().split(" ", 1)[0].split("(", 1)[0]
                if name:
                    names.add(name)
    return names


def packages(os_info, arch, fetch=None, cache=None, refresh=False, log=None):
    """Every package name the target release's archive provides for `arch`.

    Returns (names, sources): sources lists the suites/components that were read.
    fetch(url) -> bytes | None (404); defaults to an HTTP download.
    cache: directory for downloaded indexes (None = no cache).
    """
    fetch = fetch or http_fetch
    names, sources = set(), []
    first_suite = None
    for suite, comp, url in index_urls(os_info, arch):
        first_suite = first_suite or suite
        data = _cached(url, fetch, cache, refresh)
        if data is None:
            if suite == first_suite and comp == layout(os_info)["components"][0]:
                raise ArchiveError(f"{url} does not exist: is '{os_info['VERSION_CODENAME']}' the right "
                                   f"codename for {os_info.get('ID')} {os_info.get('VERSION_ID', '')}?")
            if log:
                log(f"{suite}/{comp}: no index for {arch}, skipped")
            continue
        try:
            text = lzma.decompress(data).decode("utf-8", errors="replace")
        except lzma.LZMAError as e:
            raise ArchiveError(f"{url}: not a valid .xz file ({e})") from e
        names |= parse_packages(text)
        sources.append(f"{suite}/{comp}")
    return names, sources


def base_name(pkg):
    """apt entry -> package name: drops ':arch', '=version' and '/release' suffixes."""
    for sep in ("=", "/", ":"):
        pkg = pkg.split(sep, 1)[0]
    return pkg.strip()

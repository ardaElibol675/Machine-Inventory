#!/usr/bin/env bash
# Prepare a fresh Debian/Ubuntu system for `./mi`: installs python3, PyYAML, git, whiptail and curl
# if they are missing. Safe to run more than once.
set -euo pipefail

need=()
command -v python3  >/dev/null || need+=(python3)
python3 -c 'import yaml' 2>/dev/null || need+=(python3-yaml)
command -v git      >/dev/null || need+=(git)
command -v whiptail >/dev/null || need+=(whiptail)
command -v curl     >/dev/null || need+=(curl)      # some apt sources download their key

if [ ${#need[@]} -eq 0 ]; then
  echo "Everything mi needs is installed."
else
  echo "Installing: ${need[*]}"
  sudo apt-get update
  sudo apt-get install -y "${need[@]}"
fi

cd "$(dirname "$(readlink -f "$0")")"
if [ ! -d data ]; then
  echo
  echo "No data/ directory yet. Either clone your private data repo:"
  echo "    git clone <your-private-data-repo> data"
  echo "or start a new one:"
  echo "    ./mi init"
else
  echo
  echo "Next: ./mi restore --dry-run    (then ./mi restore)"
fi

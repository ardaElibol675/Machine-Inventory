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
echo
if [ -d data ] && [ ! -d inventory ]; then
  echo "data/ was renamed to inventory/. Rename it (its git repository is not affected):"
  echo "    mv data inventory"
elif [ -d migration ]; then
  echo "Migration targets found:"
  for d in migration/*/; do [ -f "$d/migration.yaml" ] && echo "    $(basename "$d")"; done
  echo "Next: ./mi --migration <target> restore --dry-run    (then the same without --dry-run)"
elif [ -d inventory ]; then
  echo "Next: ./mi restore --dry-run    (then ./mi restore)"
else
  echo "No inventory/ directory yet. Either clone your private inventory repo:"
  echo "    git clone <your-inventory-repo> inventory"
  echo "or start a new one:"
  echo "    ./mi init"
  echo "Moving to another release? Clone your migration repo instead:"
  echo "    git clone <your-migration-repo> migration"
fi

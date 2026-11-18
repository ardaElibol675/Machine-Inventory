#!/usr/bin/env bash
# Fails if personal data leaked into the public tool repo (everything outside your private
# inventory/ and migration/ repos):
#   - home directory paths  (/home/<name>/...)
#   - your username, git name and git email
#   - email addresses (example.com/org and noreply addresses are allowed)
#   - every word in inventory/config.yaml `private_terms`
#     (except inside this repo's own public clone URL, the https form of `git remote get-url origin`)
# Usage: scripts/check-public.sh        exit 0 = clean, 1 = something found
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

mapfile -d '' files < <(find . \( -path ./inventory -o -path ./migration -o -path ./data \) -prune -o -path ./.git -prune -o -name __pycache__ -prune \
  -o -type f -print0)

terms=("$(id -un)")
for k in user.name user.email; do v="$(git config --get "$k" 2>/dev/null)" && [ -n "$v" ] && terms+=("$v"); done
if [ -f inventory/config.yaml ]; then
  mapfile -t extra < <(python3 -c 'import sys, yaml
c = yaml.safe_load(open("inventory/config.yaml")) or {}
print("\n".join(str(t) for t in c.get("private_terms") or []))')
  terms+=("${extra[@]}")
fi

# The tool repo is public, so its own clone URL may appear (e.g. in README.md) even if it names the owner.
own="$(git remote get-url origin 2>/dev/null | sed -E 's#^git@([^:]+):#https://\1/#; s#^ssh://git@([^/]+)/#https://\1/#')"
own="${own%.git}"
without_own() {
  if [ -z "$own" ]; then cat; return; fi
  while IFS= read -r l; do
    s="${l//"$own.git"/}"; s="${s//"$own"/}"
    grep -qiwF -- "$1" <<<"$s" && printf '%s\n' "$l"
  done
}

found=0
report() { echo "LEAK  $1"; echo "$2" | sed 's/^/      /'; found=1; }

hits="$(grep -nHE '/home/[A-Za-z0-9_][A-Za-z0-9_.-]*' "${files[@]}" 2>/dev/null)"
[ -n "$hits" ] && report "home directory path" "$hits"

hits="$(grep -nHoE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' "${files[@]}" 2>/dev/null \
  | grep -vE '@(example\.(com|org)|users\.noreply\.github\.com)$|noreply@' )"
[ -n "$hits" ] && report "email address" "$hits"

for t in "${terms[@]}"; do
  [ -z "$t" ] && continue
  hits="$(grep -nHiwF -- "$t" "${files[@]}" 2>/dev/null | without_own "$t")"
  [ -n "$hits" ] && report "private term '$t'" "$hits"
done

if [ "$found" = 0 ]; then
  echo "OK    no personal data found in ${#files[@]} files outside inventory/ and migration/"
fi
exit "$found"

#!/usr/bin/env bash
#
# Cut and publish a release:  ./scripts/release.sh 0.5.0
#
# Cut from main or dev, tagged on the branch it is cut from. Never tag by hand:
# pip reads the version from pyproject.toml, not from the tag.

set -euo pipefail
cd "$(dirname "$0")/.."

version=${1:-}
die() {
    echo "$*" >&2
    exit 1
}

[ -n "$version" ] || die "Usage: ./scripts/release.sh 0.5.0"
[ -z "$(git status --porcelain --untracked-files=no)" ] || die "Working tree is not clean"
branch=$(git rev-parse --abbrev-ref HEAD)
[ "$branch" = main ] || [ "$branch" = dev ] || die "Releases are cut from main or dev, not $branch"
! git rev-parse -q --verify "refs/tags/v$version" >/dev/null || die "v$version already exists"

sed -i.bak -E "s/^version = \".*\"/version = \"$version\"/" pyproject.toml && rm -f pyproject.toml.bak
grep -q "^version = \"$version\"$" pyproject.toml || die "Could not set the version"
git diff --quiet -- pyproject.toml || git commit -qm "Release v$version" pyproject.toml

git tag -a "v$version" -m "v$version"
git push --atomic origin "$branch" "v$version"

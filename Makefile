# Development tasks. The server is run with ./fwtrack.sh, which needs no make.

SHELL := /bin/bash

.PHONY: up update down logs token ps restart test lint release

# Kept as aliases: a deployment updating from a version whose fwtrack.sh still
# called make runs `make up` from the freshly pulled Makefile.
up update down logs token ps restart:
	@./fwtrack.sh $@

test:
	python -m pytest -q

lint:
	ruff check . && ruff format --check .

## Cut and publish a release: make release VERSION=0.1.1
## Tags live on main, so that a tag never points at work in progress.
## Never tag by hand: pip reads the version from pyproject.toml, not the tag.
release:
	@test -n "$(VERSION)" || { echo "Usage: make release VERSION=0.1.1"; exit 1; }
	@test -z "$$(git status --porcelain --untracked-files=no)" || { echo "Working tree is not clean"; exit 1; }
	@test "$$(git rev-parse --abbrev-ref HEAD)" = "main" || \
		{ echo "Releases are tagged on main: git checkout main && git merge dev"; exit 1; }
	@! git rev-parse -q --verify "refs/tags/v$(VERSION)" >/dev/null || { echo "v$(VERSION) already exists"; exit 1; }
	@sed -i.bak -E 's/^version = ".*"/version = "$(VERSION)"/' pyproject.toml && rm -f pyproject.toml.bak
	@grep -q '^version = "$(VERSION)"$$' pyproject.toml || { echo "Could not set the version"; exit 1; }
	@git diff --quiet -- pyproject.toml || git commit -qm "Release v$(VERSION)" pyproject.toml
	git tag -a v$(VERSION) -m "v$(VERSION)"
	git push --atomic origin main v$(VERSION)

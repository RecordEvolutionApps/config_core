# config_core dev tasks. Run `just` (or `just --list`) to see them.

venv := ".venv"

# List available recipes.
default:
    @just --list

# Create a local virtualenv with the package (editable) and test deps.
setup:
    python3 -m venv {{venv}}
    {{venv}}/bin/python -m pip install -U pip
    {{venv}}/bin/python -m pip install -e ".[dev]"

# Run the tests; uses .venv if present, else system python (e.g. `just test -k create`).
test *args:
    #!/usr/bin/env bash
    set -euo pipefail
    py=python3
    [ -x "{{venv}}/bin/python" ] && py="{{venv}}/bin/python"
    "$py" -m pytest {{args}}

# Cut a release: bump version, test, commit, tag vX.Y.Z and push. Usage: `just release 1.1.0`
release version:
    #!/usr/bin/env bash
    set -euo pipefail
    ver="{{version}}"
    [[ "$ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "error: version must be X.Y.Z (got '$ver')"; exit 1; }
    tag="v$ver"
    branch="$(git rev-parse --abbrev-ref HEAD)"
    [[ "$branch" == "main" ]] || { echo "error: cut releases from main (currently on '$branch')"; exit 1; }
    [[ -z "$(git status --porcelain)" ]] || { echo "error: commit or stash changes first (working tree not clean)"; exit 1; }
    if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then echo "error: tag $tag already exists"; exit 1; fi
    perl -i -pe 's/^version = ".*"/version = "'"$ver"'"/' pyproject.toml
    perl -i -pe 's/^__version__ = ".*"/__version__ = "'"$ver"'"/' config_core/__init__.py
    just test
    if ! git diff --quiet pyproject.toml config_core/__init__.py; then git add pyproject.toml config_core/__init__.py && git commit -m "Release $tag"; fi
    git tag "$tag"
    git push origin main --tags
    echo "Released $tag. Next: re-pin the consuming apps to @$tag (see README)."

# Bump the patch version (X.Y.Z -> X.Y.(Z+1)) and release it. Usage: `just release-patch`
release-patch:
    #!/usr/bin/env bash
    set -euo pipefail
    cur="$(perl -ne 'print $1 if /^version = "(.*)"/' pyproject.toml)"
    [[ "$cur" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || { echo "error: cannot parse version '$cur' from pyproject.toml"; exit 1; }
    next="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.$((BASH_REMATCH[3] + 1))"
    echo "Patch release: $cur -> $next"
    just release "$next"

#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../bootstrap.sh
source "$ROOT/bootstrap.sh"

[[ "$(parse_bootstrap_arguments)" == $'false\tfalse' ]]
[[ "$(parse_bootstrap_arguments --verbose --upgrade)" == $'true\ttrue' ]]
if parse_bootstrap_arguments --verbose --verbose >/dev/null 2>&1; then
  printf 'duplicate bootstrap verbose option was accepted\n' >&2
  exit 1
fi
if parse_bootstrap_arguments --uninstall >/dev/null 2>&1; then
  printf 'unsupported bootstrap option was accepted\n' >&2
  exit 1
fi

for url in \
    'https://astral.sh/uv/install.sh' \
    'https://claude.ai/install.sh' \
    'https://chatgpt.com/codex/install.sh'; do
  rg -Fq "$url" "$ROOT/bootstrap.sh"
done
rg -Fq "ORICHUM_REPOSITORY='https://github.com/orichum/orichum.git'" \
  "$ROOT/bootstrap.sh"
rg -Fq 'git clone --depth 1 --branch "$release_tag"' "$ROOT/bootstrap.sh"
rg -Fq 'ORICHUM_RELEASE_REPOSITORY='"'"'orichum/orichum'"'"'' \
  "$ROOT/bootstrap.sh"
rg -Fq 'gh api "repos/$ORICHUM_RELEASE_REPOSITORY/releases" --paginate' \
  "$ROOT/bootstrap.sh"
rg -Fq 'git clone --depth 1 --branch "$release_tag"' "$ROOT/bootstrap.sh"
rg -Fq 'refs/tags/$release_tag:refs/tags/$release_tag' "$ROOT/bootstrap.sh"
if rg -Fq 'pull --ff-only origin main' "$ROOT/bootstrap.sh"; then
  printf 'bootstrap updates from main instead of a release tag\n' >&2
  exit 1
fi
rg -Fq 'bootstrap_install_user_command codex' "$ROOT/bootstrap.sh"
if rg -Fq 'sudo npm' "$ROOT/bootstrap.sh"; then
  printf 'bootstrap uses sudo npm\n' >&2
  exit 1
fi

printf 'PASS: bootstrap installer contract\n'

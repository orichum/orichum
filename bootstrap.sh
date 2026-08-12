#!/usr/bin/env bash
set -euo pipefail

ORICHUM_REPOSITORY='https://github.com/orichum/orichum.git'
ORICHUM_SOURCE_DIR="${ORICHUM_SOURCE_DIR:-$HOME/.local/share/orichum}"

bootstrap_usage() {
  printf 'Usage: bootstrap.sh [--verbose] [--upgrade]\n' >&2
}

bootstrap_die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

parse_bootstrap_arguments() {
  local verbose=false
  local upgrade=false
  local argument
  for argument in "$@"; do
    case "$argument" in
      --verbose)
        [[ "$verbose" == false ]] || return 2
        verbose=true
        ;;
      --upgrade)
        [[ "$upgrade" == false ]] || return 2
        upgrade=true
        ;;
      *) return 2 ;;
    esac
  done
  printf '%s\t%s\n' "$verbose" "$upgrade"
}

bootstrap_command_exists() {
  command -v "$1" >/dev/null 2>&1
}

bootstrap_is_ubuntu() {
  [[ -r /etc/os-release ]] || return 1
  local os_id
  os_id="$(
    . /etc/os-release
    printf '%s' "${ID:-}"
  )"
  [[ "$os_id" == ubuntu ]]
}

bootstrap_brew() {
  local candidate
  for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [[ -x "$candidate" ]] && {
      printf '%s\n' "$candidate"
      return 0
    }
  done
  return 1
}

bootstrap_install_system_packages() {
  case "$(uname -s)" in
    Darwin)
      local brew
      brew="$(bootstrap_brew)" || bootstrap_die \
        'Homebrew is required on macOS; install it, then rerun this command'
      "$brew" install git gh jq python ripgrep uv
      ;;
    Linux)
      bootstrap_is_ubuntu || bootstrap_die \
        'Automatic prerequisite installation currently supports Ubuntu; install the commands listed in docs/installation.md, then run ./install.sh from a checkout'
      bootstrap_command_exists sudo || bootstrap_die \
        'sudo is required to install Ubuntu prerequisites'
      sudo env DEBIAN_FRONTEND=noninteractive apt-get update
      sudo env DEBIAN_FRONTEND=noninteractive apt-get install --yes \
        ca-certificates git gh jq python3 ripgrep tar iproute2
      ;;
    *) bootstrap_die 'supported platforms are macOS, Linux, and WSL2' ;;
  esac
}

bootstrap_add_user_bin_to_path() {
  export PATH="$HOME/.local/bin:$PATH"
}

bootstrap_install_user_command() {
  local command_name="$1"
  local installer_url="$2"
  local interpreter="$3"
  bootstrap_command_exists "$command_name" && return
  curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
    "$installer_url" | "$interpreter"
  bootstrap_add_user_bin_to_path
}

bootstrap_install_user_commands() {
  bootstrap_install_user_command uv https://astral.sh/uv/install.sh sh
  bootstrap_install_user_command claude https://claude.ai/install.sh bash
  bootstrap_install_user_command codex https://chatgpt.com/codex/install.sh sh
}

bootstrap_verify_prerequisites() {
  local command_name
  local -a commands=(
    bash curl gh git install jq python3 rg tar uv claude codex
  )
  if [[ "$(uname -s)" == Linux ]]; then
    commands+=(ss)
  fi
  for command_name in "${commands[@]}"; do
    bootstrap_command_exists "$command_name" || \
      bootstrap_die "missing required command after bootstrap: $command_name"
  done
  python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || \
    bootstrap_die 'Python 3.10 or newer is required'
}

bootstrap_checkout_or_update() {
  local source_parent
  source_parent="$(dirname "$ORICHUM_SOURCE_DIR")"
  install -d -m 0700 "$source_parent"
  if [[ -e "$ORICHUM_SOURCE_DIR/.git" ]]; then
    git -C "$ORICHUM_SOURCE_DIR" pull --ff-only origin main
  elif [[ -e "$ORICHUM_SOURCE_DIR" ]]; then
    bootstrap_die "source directory exists but is not an Orichum checkout: $ORICHUM_SOURCE_DIR"
  else
    git clone "$ORICHUM_REPOSITORY" "$ORICHUM_SOURCE_DIR"
  fi
  [[ -x "$ORICHUM_SOURCE_DIR/install.sh" ]] || \
    bootstrap_die "Orichum installer is missing: $ORICHUM_SOURCE_DIR/install.sh"
}

bootstrap_script_dir() {
  local script_source="${BASH_SOURCE[0]}"
  cd "$(dirname "$script_source")" && pwd -P
}

main() {
  local parsed verbose upgrade
  parsed="$(parse_bootstrap_arguments "$@")" || {
    bootstrap_usage
    exit 2
  }
  IFS=$'\t' read -r verbose upgrade <<<"$parsed"
  export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
  bootstrap_command_exists curl || \
    bootstrap_die 'curl is required to run the bootstrap; install curl and retry'
  if [[ "$(bootstrap_script_dir)" != "$ORICHUM_SOURCE_DIR" ]]; then
    bootstrap_install_system_packages
    bootstrap_checkout_or_update
    exec "$ORICHUM_SOURCE_DIR/bootstrap.sh" "$@"
  fi
  bootstrap_install_user_commands
  bootstrap_verify_prerequisites
  local -a installer_arguments=()
  [[ "$upgrade" == true ]] && installer_arguments+=(--upgrade)
  [[ "$verbose" == true ]] && installer_arguments+=(--verbose)
  exec "$ORICHUM_SOURCE_DIR/install.sh" "${installer_arguments[@]}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

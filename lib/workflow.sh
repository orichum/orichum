#!/usr/bin/env bash

workflow_die() {
  printf 'ERROR: %s\n' "$*" >&2
  return 1
}

parse_install_mode() {
  case "$#" in
    0) printf 'fast\n' ;;
    1)
      case "$1" in
        --upgrade) printf 'upgrade\n' ;;
        --uninstall) printf 'uninstall\n' ;;
        *) return 2 ;;
      esac
      ;;
    2)
      [[ "$1" == --uninstall && "$2" == --purge ]] || return 2
      printf 'purge\n'
      ;;
    *) return 2 ;;
  esac
}

parse_install_arguments() {
  local verbose=false
  local -a mode_arguments=()
  local argument
  for argument in "$@"; do
    if [[ "$argument" == --verbose ]]; then
      [[ "$verbose" == false ]] || return 2
      verbose=true
    else
      mode_arguments+=("$argument")
    fi
  done
  local mode
  if ((${#mode_arguments[@]})); then
    mode="$(parse_install_mode "${mode_arguments[@]}")" || return 2
  else
    mode="$(parse_install_mode)" || return 2
  fi
  printf '%s\t%s\n' "$mode" "$verbose"
}

create_install_diagnostic_log() {
  (($# == 1)) || return 2
  local data_root="$1"
  local log_dir log_path temporary_log
  [[ "$data_root" == /* && ! -L "$data_root" ]] || return 1
  install -d -m 0700 "$data_root" || return 1
  [[ -d "$data_root" && ! -L "$data_root" && \
     "$(path_uid "$data_root")" == "$(id -u)" && \
     "$(path_mode "$data_root")" == 700 ]] || return 1
  log_dir="$data_root/logs"
  [[ ! -e "$log_dir" || ( -d "$log_dir" && ! -L "$log_dir" ) ]] || \
    return 1
  install -d -m 0700 "$log_dir" || return 1
  [[ "$(path_uid "$log_dir")" == "$(id -u)" && \
     "$(path_mode "$log_dir")" == 700 ]] || return 1
  umask 077
  temporary_log="$(mktemp "$log_dir/install.XXXXXX")" || return 1
  log_path="$temporary_log.log"
  if ! mv "$temporary_log" "$log_path"; then
    rm -f "$temporary_log"
    return 1
  fi
  chmod 0600 "$log_path" || return 1
  printf '%s\n' "$log_path"
}

print_install_outcome() {
  (($# == 3)) || return 2
  local provider_pending="$1"
  local optional_shell="$2"
  local log_path="$3"
  case "$optional_shell" in
    ''|bash|fish|zsh) ;;
    *) return 2 ;;
  esac
  case "$provider_pending" in
    true)
      printf 'Orichum is installed.\nNext: orichum setup\n'
      ;;
    false)
      printf 'Orichum is ready.\n'
      ;;
    *) return 2 ;;
  esac
  printf 'Diagnostics: %s\n' "$log_path"
}

print_install_progress() {
  (($# == 2)) || return 2
  case "$1" in
    false) printf '%s\n' "$2" ;;
    true) ;;
    *) return 2 ;;
  esac
}

print_install_failure() {
  (($# == 1)) || return 2
  printf '\nInstallation stopped.\n\n'
  printf 'Run:\n  ./install.sh\n\n'
  printf 'Diagnostics:\n  %s\n\n' "$1"
  printf 'Details:\n  ./install.sh --verbose\n'
}

print_install_component_results() {
  (($# == 8)) || return 2
  local optional_shell="${8}"
  local index
  local -a labels=(
    Python CLIProxyAPI Claudex LeanCTX Routing 'Controller plugin' Completion
  )
  local -a actions=("${@:1:7}")
  for index in "${!actions[@]}"; do
    case "${actions[$index]}" in
      reused|repaired|upgraded) ;;
      *) return 2 ;;
    esac
  done
  case "$optional_shell" in
    ''|bash|fish|zsh) ;;
    *) return 2 ;;
  esac

  printf '\nComponents\n'
  for index in "${!labels[@]}"; do
    if [[ "$index" -eq 6 && -n "$optional_shell" ]]; then
      printf '  ⚠ %s completion not activated; existing profile left unchanged\n' \
        "$optional_shell"
    else
      printf '  ✓ %s %s\n' "${labels[$index]}" "${actions[$index]}"
    fi
  done
}

component_state_matches() {
  local manifest="$1"
  local name="$2"
  local version="$3"
  local source_identity="$4"
  local artifact_sha="$5"
  local input_sha="$6"
  local probe_sha="$7"
  jq -e \
    --arg name "$name" \
    --arg version "$version" \
    --arg source_identity "$source_identity" \
    --arg artifact_sha "$artifact_sha" \
    --arg input_sha "$input_sha" \
    --arg probe_sha "$probe_sha" \
    '.components[$name] == {
      version: $version,
      sourceIdentity: $source_identity,
      artifactSha256: $artifact_sha,
      inputSha256: $input_sha,
      probeSha256: $probe_sha
    }' "$manifest" >/dev/null
}

decide_install_component() {
  local manifest="$1"
  local name="$2"
  shift 2
  if [[ "${INSTALL_MODE:-fast}" == upgrade ]]; then
    printf 'upgraded\n'
  elif component_state_matches "$manifest" "$name" "$@"; then
    printf 'reused\n'
  elif jq -e --arg name "$name" \
      '.components[$name] != null' "$manifest" >/dev/null 2>&1; then
    printf 'repaired\n'
  else
    printf 'upgraded\n'
  fi
}

install_state_component_field() {
  local manifest="$1"
  local name="$2"
  local field="$3"
  jq -er --arg name "$name" --arg field "$field" \
    '.components[$name][$field]' "$manifest"
}

print_component_status_table() {
  (($# == 7)) || return 2
  local value
  for value in "$@"; do
    case "$value" in
      reused|repaired|upgraded) ;;
      *) return 2 ;;
    esac
  done
  printf '\n%-20s  %-9s\n' COMPONENT STATUS
  printf '%-20s  %-9s\n' '--------------------' '---------'
  printf '%-20s  %-9s\n' \
    Python "$1" \
    CLIProxyAPI "$2" \
    Claudex "$3" \
    LeanCTX "$4" \
    Routing "$5" \
    'Controller plugin' "$6" \
    Completion "$7"
}

linux_environment_kind() {
  local osrelease_path="${1:-/proc/sys/kernel/osrelease}"
  if rg -qi microsoft "$osrelease_path" 2>/dev/null; then
    if rg -qi 'wsl2|microsoft-standard' "$osrelease_path" 2>/dev/null; then
      printf 'wsl2\n'
    else
      printf 'wsl1\n'
    fi
  else
    printf 'linux\n'
  fi
}

physical_pwd() {
  pwd -P
}

orichum_home_dir() {
  local home_root="${ORICHUM_HOME:-$HOME/.orichum}"
  case "$home_root" in
    /*) printf '%s' "${home_root%/}" ;;
    *) workflow_die "ORICHUM_HOME must be an absolute path" ;;
  esac
}

workflow_data_dir() {
  local data_root="${ORICHUM_DATA_HOME:-$(orichum_home_dir)}"
  case "$data_root" in
    /*) printf '%s' "$data_root" ;;
    *) workflow_die "ORICHUM_DATA_HOME must be an absolute path" ;;
  esac
}

workflow_config_dir() {
  local config_root="${ORICHUM_CONFIG_HOME:-$(orichum_home_dir)/config}"
  case "$config_root" in
    /*) printf '%s' "$config_root" ;;
    *) workflow_die "ORICHUM_CONFIG_HOME must be an absolute path" ;;
  esac
}

workflow_cache_dir() {
  local cache_root="${ORICHUM_CACHE_HOME:-$(orichum_home_dir)/cache}"
  case "$cache_root" in
    /*) printf '%s' "$cache_root" ;;
    *) workflow_die "ORICHUM_CACHE_HOME must be an absolute path" ;;
  esac
}

orichum_python_root() {
  local data_root="$1"
  [[ "$data_root" == /* && "$data_root" != / ]] || {
    workflow_die "Orichum data root must be an absolute private path"
    return 1
  }
  printf '%s/python' "${data_root%/}"
}

orichum_python_entrypoint() {
  local data_root="$1"
  [[ "$data_root" == /* && "$data_root" != / ]] || {
    workflow_die "Orichum data root must be an absolute private path"
    return 1
  }
  printf '%s/bin/orichum-python' "${data_root%/}"
}

workflow_physical_path() {
  local candidate="$1"
  local link_target directory
  [[ "$candidate" == /* ]] || return 1
  while [[ -L "$candidate" ]]; do
    link_target="$(readlink "$candidate")" || return 1
    case "$link_target" in
      /*) candidate="$link_target" ;;
      *) candidate="$(dirname "$candidate")/$link_target" ;;
    esac
  done
  directory="$(cd -P -- "$(dirname "$candidate")" && pwd)" || return 1
  printf '%s/%s' "$directory" "$(basename "$candidate")"
}

path_uid() {
  case "$(uname -s)" in
    Darwin) stat -f '%u' "$1" ;;
    Linux) stat -c '%u' "$1" ;;
    *) return 1 ;;
  esac
}

managed_executable_is_safe() {
  local executable="$1"
  local parent
  [[ "$executable" == /* && -f "$executable" && ! -L "$executable" && \
     -x "$executable" ]] || return 1
  parent="$(dirname "$executable")"
  [[ -d "$parent" && ! -L "$parent" ]] || return 1
  [[ "$(path_uid "$executable")" == "$(id -u)" && \
     "$(path_uid "$parent")" == "$(id -u)" && \
     "$(path_mode "$executable")" == 755 && \
     "$(path_mode "$parent")" == 700 ]]
}

validate_orichum_python() {
  local data_root="$1"
  local interpreter="$2"
  local private_root private_root_real interpreter_real identity implementation version
  local root_mode interpreter_mode current_uid current_dir current_mode
  local entrypoint_dir="$data_root/bin"
  private_root="$(orichum_python_root "$data_root")" || return 1
  [[ -d "$private_root" && ! -L "$private_root" ]] || {
    workflow_die "private Python root is missing or unsafe: $private_root"
    return 1
  }
  private_root_real="$(workflow_physical_path "$private_root")" || return 1
  interpreter_real="$(workflow_physical_path "$interpreter")" || {
    workflow_die "private Python interpreter could not be resolved"
    return 1
  }
  case "$interpreter_real" in
    "$private_root_real"/*) ;;
    *)
      workflow_die "Python interpreter is outside private Python root"
      return 1
      ;;
  esac
  [[ -f "$interpreter_real" && ! -L "$interpreter_real" && \
     -x "$interpreter_real" ]] || {
    workflow_die "private Python interpreter is not a regular executable"
    return 1
  }
  current_uid="$(id -u)"
  [[ -d "$entrypoint_dir" && ! -L "$entrypoint_dir" && \
     "$(path_uid "$entrypoint_dir")" == "$current_uid" && \
     "$(path_uid "$private_root")" == "$current_uid" && \
     "$(path_uid "$interpreter_real")" == "$current_uid" ]] || {
    workflow_die "private Python runtime is not owned by the current user"
    return 1
  }
  root_mode="$(path_mode "$private_root")" || return 1
  interpreter_mode="$(path_mode "$interpreter_real")" || return 1
  (( (8#$root_mode & 0022) == 0 && \
     (8#$interpreter_mode & 0022) == 0 )) || {
    workflow_die "private Python runtime is writable by group or others"
    return 1
  }
  current_mode="$(path_mode "$entrypoint_dir")" || return 1
  (( (8#$current_mode & 0022) == 0 )) || {
    workflow_die "private Python runtime is writable by group or others"
    return 1
  }
  current_dir="$(dirname "$interpreter_real")"
  while [[ "$current_dir" != "$private_root_real" ]]; do
    [[ -d "$current_dir" && ! -L "$current_dir" && \
       "$(path_uid "$current_dir")" == "$current_uid" ]] || {
      workflow_die "private Python runtime is not owned by the current user"
      return 1
    }
    current_mode="$(path_mode "$current_dir")" || return 1
    (( (8#$current_mode & 0022) == 0 )) || {
      workflow_die "private Python runtime is writable by group or others"
      return 1
    }
    current_dir="$(dirname "$current_dir")"
  done
  identity="$(
    "$interpreter_real" -I -B -c \
      'import platform,sys; print(f"{platform.python_implementation()}\t{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")'
  )" || {
    workflow_die "private Python interpreter identity probe failed"
    return 1
  }
  IFS=$'\t' read -r implementation version <<<"$identity"
  [[ "$implementation" == CPython && "$version" == 3.14.* ]] || {
    workflow_die "Orichum requires CPython 3.14.x, found ${implementation:-unknown} ${version:-unknown}"
    return 1
  }
  printf '%s\t%s\n' "$version" "$interpreter_real"
}

resolve_orichum_python() {
  local data_root="$1"
  local entrypoint
  entrypoint="$(orichum_python_entrypoint "$data_root")" || return 1
  validate_orichum_python "$data_root" "$entrypoint" >/dev/null || return 1
  printf '%s' "$entrypoint"
}

workflow_python() {
  local data_root interpreter
  if [[ "${ORICHUM_INSTALL_BOOTSTRAP:-false}" == true && \
        -z "${ORICHUM_PYTHON:-}" ]]; then
    python3 "$@"
    return
  fi
  data_root="$(workflow_data_dir)" || return 1
  interpreter="${ORICHUM_PYTHON:-$(orichum_python_entrypoint "$data_root")}"
  if [[ "${ORICHUM_PYTHON_VALIDATED:-}" != "$interpreter" ]]; then
    validate_orichum_python "$data_root" "$interpreter" >/dev/null || return 1
    ORICHUM_PYTHON_VALIDATED="$interpreter"
    export ORICHUM_PYTHON_VALIDATED
  fi
  "$interpreter" "$@"
}

python_version_is_at_least() {
  local current="$1"
  local available="$2"
  jq -en --arg current "$current" --arg available "$available" '
    def version_parts:
      split(".")
      | if length == 3 and all(.[]; test("^[0-9]+$"))
        then map(tonumber)
        else error("invalid Python version")
        end;
    ($current | version_parts) >= ($available | version_parts)
  ' >/dev/null
}

install_or_reuse_orichum_python() (
  local data_root="$1"
  local resolve_upstream="${2:-true}"
  local recorded_version="${3:-}"
  local expected_artifact_sha="${4:-}"
  local private_root entrypoint prior_identity prior_version=
  local available_json latest_version stage_root stage_root_real candidate
  local candidate_real
  local relative generation_name generation destination identity version action
  local force_repair=false
  private_root="$(orichum_python_root "$data_root")" || return 1
  entrypoint="$(orichum_python_entrypoint "$data_root")" || return 1
  install -d -m 0700 \
    "$data_root" "$data_root/bin" "$data_root/state" "$private_root" || return 1
  if prior_identity="$(validate_orichum_python "$data_root" "$entrypoint" 2>/dev/null)"; then
    IFS=$'\t' read -r prior_version candidate_real <<<"$prior_identity"
  fi
  if [[ "$resolve_upstream" == false && \
        "$recorded_version" =~ ^3\.14\.[0-9]+$ ]]; then
    if [[ "$prior_version" == "$recorded_version" ]] && \
       {
         [[ -z "$expected_artifact_sha" ]] || \
           [[ "$(sha256_file "$candidate_real")" == "$expected_artifact_sha" ]]
       }; then
      printf 'reused\t%s\t%s\t\n' "$prior_version" "$candidate_real"
      return 0
    fi
    [[ "$expected_artifact_sha" =~ ^[a-f0-9]{64}$ ]] || {
      workflow_die "recorded private CPython artifact hash is invalid"
      return 1
    }
    force_repair=true
  fi
  if [[ "$resolve_upstream" == false && \
        "$recorded_version" =~ ^3\.14\.[0-9]+$ ]]; then
    latest_version="$recorded_version"
  else
    if ! available_json="$(
        uv python list --only-downloads --output-format json \
          --no-config 3.14
      )"; then
      if [[ "$resolve_upstream" == true && \
            -n "$prior_version" && \
            "${INSTALL_MODE:-fast}" != upgrade ]]; then
        printf 'reused\t%s\t%s\t\n' "$prior_version" "$candidate_real"
        return 0
      fi
      workflow_die "uv could not resolve the latest private CPython 3.14"
      return 1
    fi
    latest_version="$(
      jq -er '
        [
          .[]
          | select(
              .version_parts.major == 3 and
              .version_parts.minor == 14
            )
        ]
        | sort_by([
            .version_parts.major,
            .version_parts.minor,
            .version_parts.patch
          ])
        | last
        | .version
      ' <<<"$available_json"
    )" || {
      workflow_die "uv returned no downloadable CPython 3.14 runtime"
      return 1
    }
  fi
  if [[ "$force_repair" != true && -n "$prior_version" ]] && \
     python_version_is_at_least "$prior_version" "$latest_version"; then
    printf 'reused\t%s\t%s\t\n' "$prior_version" "$candidate_real"
    return 0
  fi
  stage_root="$(mktemp -d \
    "$data_root/state/python-stage.XXXXXX")" || return 1
  chmod 0700 "$stage_root" || return 1
  stage_root_real="$(workflow_physical_path "$stage_root")" || return 1
  trap 'rm -rf -- "$stage_root"' EXIT
  if ! uv python install --install-dir "$stage_root" \
      --no-bin --no-config "$latest_version" 1>&2; then
    if [[ "$resolve_upstream" == true && \
          -n "$prior_version" && \
          "${INSTALL_MODE:-fast}" != upgrade ]]; then
      printf 'reused\t%s\t%s\t\n' "$prior_version" "$candidate_real"
      return 0
    fi
    workflow_die "uv could not install private CPython $latest_version"
    return 1
  fi
  candidate="$(
    UV_PYTHON_INSTALL_DIR="$stage_root" \
      uv python find --managed-python --no-project --no-python-downloads \
        --resolve-links --no-config "$latest_version"
  )" || {
    workflow_die "uv could not resolve staged CPython $latest_version"
    return 1
  }
  candidate_real="$(workflow_physical_path "$candidate")" || return 1
  case "$candidate_real" in
    "$stage_root_real"/*) ;;
    *)
      workflow_die "uv staged Python outside the private staging root"
      return 1
      ;;
  esac
  relative="${candidate_real#"$stage_root_real"/}"
  generation_name="${relative%%/*}"
  [[ "$generation_name" == cpython-* && \
     "$relative" != "$generation_name" ]] || {
    workflow_die "uv staged Python has an unexpected layout"
    return 1
  }
  generation="$stage_root_real/$generation_name"
  destination="$private_root/$generation_name"
  if [[ "$force_repair" == true ]]; then
    destination="$private_root/${generation_name}.repair.$$.$RANDOM"
  fi
  if [[ -e "$destination" || -L "$destination" ]]; then
    candidate="$destination/${relative#*/}"
    if identity="$(validate_orichum_python \
        "$data_root" "$candidate" 2>/dev/null)"; then
      generation=
    else
      remove_orichum_python_generation \
        "$data_root" "$destination" || return 1
      mv -- "$generation" "$destination" || return 1
      generation="$destination"
      candidate="$destination/${relative#*/}"
      if ! identity="$(validate_orichum_python "$data_root" "$candidate")"; then
        remove_orichum_python_generation \
          "$data_root" "$destination" || true
        return 1
      fi
    fi
  else
    mv -- "$generation" "$destination" || return 1
    generation="$destination"
    candidate="$destination/${relative#*/}"
    if ! identity="$(validate_orichum_python "$data_root" "$candidate")"; then
      rm -rf -- "$destination"
      return 1
    fi
  fi
  IFS=$'\t' read -r version candidate_real <<<"$identity"
  if [[ "$force_repair" == true ]]; then
    if [[ "$(sha256_file "$candidate_real")" != "$expected_artifact_sha" ]]; then
      remove_orichum_python_generation "$data_root" "$generation" || true
      workflow_die "repaired private CPython artifact did not match"
      return 1
    fi
    action=repaired
  elif [[ -z "$prior_version" ]]; then
    action=installed
  elif [[ "$prior_version" == "$version" ]]; then
    action=reused
  else
    action=upgraded
  fi
  printf '%s\t%s\t%s\t%s\n' \
    "$action" "$version" "$candidate_real" "$generation"
)

remove_orichum_python_generation() {
  local data_root="$1"
  local generation="$2"
  local private_root private_root_real generation_parent generation_real
  [[ -n "$generation" ]] || return 0
  private_root="$(orichum_python_root "$data_root")" || return 1
  private_root_real="$(workflow_physical_path "$private_root")" || return 1
  [[ -d "$generation" && ! -L "$generation" && \
     "$(path_uid "$generation")" == "$(id -u)" ]] || return 1
  generation_parent="$(workflow_physical_path "$(dirname "$generation")")" || \
    return 1
  generation_real="$(workflow_physical_path "$generation")" || return 1
  [[ "$generation_parent" == "$private_root_real" && \
     "$generation_real" == "$private_root_real"/cpython-* && \
     "$(path_uid "$generation_parent")" == "$(id -u)" ]] || {
    workflow_die "refusing unsafe private Python generation removal"
    return 1
  }
  rm -rf -- "$generation_real"
}

activate_orichum_python() {
  local data_root="$1"
  local interpreter="$2"
  local entrypoint identity interpreter_real temporary
  entrypoint="$(orichum_python_entrypoint "$data_root")" || return 1
  identity="$(validate_orichum_python "$data_root" "$interpreter")" || return 1
  IFS=$'\t' read -r _ interpreter_real <<<"$identity"
  [[ -d "$data_root/bin" && ! -L "$data_root/bin" && \
     "$(path_uid "$data_root/bin")" == "$(id -u)" ]] || {
    workflow_die "private Python entry-point directory is unsafe"
    return 1
  }
  temporary="$data_root/bin/.orichum-python.$$.$RANDOM"
  rm -f -- "$temporary"
  ln -s "$interpreter_real" "$temporary" || return 1
  if ! mv -f -- "$temporary" "$entrypoint"; then
    rm -f -- "$temporary"
    return 1
  fi
  resolve_orichum_python "$data_root" >/dev/null
}

preflight_orichum_python_runtime() (
  local interpreter="$1"
  local workflow_root="$2"
  local data_root="$3"
  [[ "$workflow_root" == /* && -d "$workflow_root" ]] || return 1
  install -d -m 0700 "$data_root/state" || return 1
  "$interpreter" -I -B - "$workflow_root" "$data_root" <<'PY'
from pathlib import Path
import sys

workflow_root = Path(sys.argv[1])
data_root = Path(sys.argv[2])
sys.path.insert(0, str(workflow_root))

from integrations.common.route_proxy import ProxyConfig, RouteProxyServer

server = RouteProxyServer(
    ("127.0.0.1", 0),
    ProxyConfig(
        upstream_port=65535,
        state_home=data_root / "state",
        data_home=data_root,
    ),
)
try:
    host, port = server.server_address
    assert host == "127.0.0.1"
    assert 1024 <= port <= 65535
finally:
    server.server_close()
PY
)

service_ports_file() {
  printf '%s/service-ports.json' "$1"
}

valid_service_port() {
  local port="$1"
  [[ "$port" =~ ^[0-9]+$ ]] || return 1
  [[ "$port" == "$((10#$port))" ]] || return 1
  ((10#$port >= 1024 && 10#$port <= 65535))
}

read_service_ports() {
  local data_root="$1"
  local ports_file
  ports_file="$(service_ports_file "$data_root")"
  if [[ ! -e "$ports_file" ]]; then
    printf '8317\t13456\t13457\t13458\n'
    return 0
  fi
  [[ -f "$ports_file" && ! -L "$ports_file" ]] || return 1
  jq -er '
    select(type == "object") |
    if keys == [
        "claudexProxyPort",
        "cliproxyPort",
        "routeProxyPort"
      ] then
      . as $old |
      first(
        range(13458; 65536) as $candidate |
        select(([
          $old.cliproxyPort,
          $old.claudexProxyPort,
          $old.routeProxyPort
        ] | index($candidate)) == null) |
        $old + {leanctxProxyPort: $candidate}
      )
    elif keys == [
        "claudexProxyPort",
        "cliproxyPort",
        "leanctxProxyPort",
        "routeProxyPort"
      ] then .
    else empty
    end |
    select(.cliproxyPort | type == "number" and floor == . and
      . >= 1024 and . <= 65535) |
    select(.claudexProxyPort | type == "number" and floor == . and
      . >= 1024 and . <= 65535) |
    select(.routeProxyPort | type == "number" and floor == . and
      . >= 1024 and . <= 65535) |
    select(.leanctxProxyPort | type == "number" and floor == . and
      . >= 1024 and . <= 65535) |
    select(([
      .cliproxyPort,
      .claudexProxyPort,
      .routeProxyPort,
      .leanctxProxyPort
    ] | unique | length) == 4) |
    [
      .cliproxyPort,
      .claudexProxyPort,
      .routeProxyPort,
      .leanctxProxyPort
    ] | @tsv
  ' "$ports_file"
}

write_service_ports() {
  local data_root="$1"
  local cliproxy_port="$2"
  local claudex_proxy_port="$3"
  local route_proxy_port="$4"
  local leanctx_proxy_port="$5"
  local ports_file temporary
  valid_service_port "$cliproxy_port" || return 1
  valid_service_port "$claudex_proxy_port" || return 1
  valid_service_port "$route_proxy_port" || return 1
  valid_service_port "$leanctx_proxy_port" || return 1
  [[ "$(printf '%s\n' \
      "$cliproxy_port" "$claudex_proxy_port" \
      "$route_proxy_port" "$leanctx_proxy_port" | sort -u | wc -l | tr -d ' ')" \
      == 4 ]] || return 1
  install -d -m 0700 "$data_root" || return 1
  ports_file="$(service_ports_file "$data_root")"
  [[ ! -L "$ports_file" ]] || return 1
  temporary="$(mktemp "$data_root/.service-ports.XXXXXX")" || return 1
  if ! jq -n --argjson cliproxy "$cliproxy_port" \
      --argjson claudex_proxy "$claudex_proxy_port" \
      --argjson route_proxy "$route_proxy_port" \
      --argjson leanctx_proxy "$leanctx_proxy_port" \
      '{
        claudexProxyPort: $claudex_proxy,
        cliproxyPort: $cliproxy,
        leanctxProxyPort: $leanctx_proxy,
        routeProxyPort: $route_proxy
      }' >"$temporary" || \
     ! chmod 0600 "$temporary" || ! mv -f "$temporary" "$ports_file"; then
    rm -f -- "$temporary"
    return 1
  fi
}

port_is_available() {
  local port="$1"
  valid_service_port "$port" || return 1
  workflow_python - "$port" <<'PY'
import socket
import sys

port = int(sys.argv[1])
listener = socket.socket()
try:
    listener.bind(("127.0.0.1", port))
except OSError:
    raise SystemExit(1)
finally:
    listener.close()
PY
}

loopback_port_is_listening() {
  local port="$1"
  valid_service_port "$port" || return 1
  workflow_python - "$port" <<'PY'
import socket
import sys

listener = socket.socket()
listener.settimeout(0.2)
try:
    status = listener.connect_ex(("127.0.0.1", int(sys.argv[1])))
finally:
    listener.close()
raise SystemExit(0 if status == 0 else 1)
PY
}

next_available_port() {
  local occupied_port="$1"
  local reserved_port
  shift
  valid_service_port "$occupied_port" || return 1
  for reserved_port in "$@"; do
    valid_service_port "$reserved_port" || return 1
  done
  workflow_python - "$occupied_port" "$@" <<'PY'
import itertools
import socket
import sys

start = int(sys.argv[1])
reserved = set(map(int, sys.argv[2:]))
ports = itertools.chain(range(start + 1, 65536), range(1024, start))
for port in ports:
    if port in reserved:
        continue
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", port))
    except OSError:
        continue
    finally:
        listener.close()
    print(port)
    raise SystemExit(0)
raise SystemExit(1)
PY
}

select_service_port() {
  local service_name="$1"
  local override_name="$2"
  local desired_port="$3"
  local owned_listener="$4"
  local interactive="$5"
  local suggested_port selected_port reserved_port collision_reason
  local desired_is_reserved=false
  shift 5

  valid_service_port "$desired_port" || return 1
  for reserved_port do
    valid_service_port "$reserved_port" || return 1
    if [[ "$desired_port" == "$reserved_port" ]]; then
      desired_is_reserved=true
    fi
  done
  if [[ "$desired_is_reserved" == false ]] && \
     { port_is_available "$desired_port" || [[ "$owned_listener" == true ]]; }; then
    printf '%s\n' "$desired_port"
    return 0
  fi
  if [[ -n "${!override_name:-}" ]]; then
    workflow_die "$service_name port $desired_port from $override_name is unavailable"
    return 1
  fi
  suggested_port="$(next_available_port "$desired_port" "$@")" || {
    workflow_die "$service_name port $desired_port is occupied and no alternate port is available"
    return 1
  }
  collision_reason=occupied
  if [[ "$desired_is_reserved" == true ]]; then
    collision_reason='reserved by another Orichum service'
  fi
  if [[ "$interactive" != true ]]; then
    printf 'NOTICE: %s port %s is %s; using %s.\n' \
      "$service_name" "$desired_port" "$collision_reason" \
      "$suggested_port" >&2
    printf '%s\n' "$suggested_port"
    return 0
  fi

  while true; do
    printf '%s port %s is occupied. Port to use [%s]: ' \
      "$service_name" "$desired_port" "$suggested_port" >&2
    IFS= read -r selected_port || return 1
    selected_port="${selected_port:-$suggested_port}"
    if ! valid_service_port "$selected_port"; then
      printf 'Port must be an integer from 1024 through 65535.\n' >&2
      continue
    fi
    for reserved_port do
      if [[ "$selected_port" == "$reserved_port" ]]; then
        printf 'Port %s is reserved by another Orichum service.\n' \
          "$selected_port" >&2
        selected_port=
        break
      fi
    done
    [[ -n "$selected_port" ]] || continue
    if ! port_is_available "$selected_port"; then
      printf 'Port %s is also occupied.\n' "$selected_port" >&2
      continue
    fi
    printf '%s\n' "$selected_port"
    return 0
  done
}

print_install_summary() {
  local workflow_root="$1"
  local data_root="$2"
  local user_bin_dir="$3"
  local claudex_binary="$4"
  local cliproxy_binary="$5"
  local cliproxy_service_file="$6"
  local cliproxy_port="$7"
  local cliproxy_action="$8"
  local claudex_proxy_service_file="$9"
  local claudex_proxy_port="${10}"
  local route_proxy_port="${11}"
  local claudex_proxy_action="${12}"
  local python_entrypoint="${13}"
  local python_version="${14}"
  local python_realpath="${15}"
  local python_action="${16}"
  local leanctx_binary="${17}"
  local source_root="${18}"
  local home_root="${19}"
  local leanctx_proxy_service_file="${20}"
  local leanctx_proxy_port="${21}"
  local leanctx_proxy_action="${22}"

  printf '%s\n' \
    '' \
    'Installation locations' \
    "  Orichum home:       $home_root" \
    "  Runtime release:    $workflow_root" \
    "  Installer source:   $source_root" \
    "  Launcher:          $user_bin_dir/orichum -> $workflow_root/bin/orichum" \
    "  Claudex runtime:   $claudex_binary" \
    "  CLIProxyAPI:       $cliproxy_binary" \
    "  LeanCTX:           $leanctx_binary" \
    "  Atlassian MCP:     $data_root/tools/bin/mcp-atlassian" \
    '' \
    'Python runtime' \
    '  Python request: 3.14.x' \
    "  Python version: $python_version" \
    "  Python runtime: $python_entrypoint -> $python_realpath" \
    "  Python action:  $python_action" \
    '' \
    'Services' \
    "  CLIProxyAPI: $cliproxy_action at 127.0.0.1:$cliproxy_port" \
    "    $cliproxy_service_file" \
    "  LeanCTX:     $leanctx_proxy_action at 127.0.0.1:$leanctx_proxy_port" \
    "    $leanctx_proxy_service_file" \
    "  Claudex:     per-session from 127.0.0.1:$claudex_proxy_port" \
    "  Route proxy: $claudex_proxy_action at 127.0.0.1:$route_proxy_port" \
    "    $claudex_proxy_service_file"
  if [[ "$data_root" != "$home_root" ]]; then
    printf '  Advanced data override: %s\n' "$data_root"
  fi
}

service_definition_is_owned() {
  local service_file="$1"
  local data_root="$2"
  local service_kind="$3"
  local ownership_mode="${4:-either}"
  local workflow_root="${5:-}"
  local allow_previous_leanctx_environment="${6:-false}"
  [[ "$allow_previous_leanctx_environment" == true || \
     "$allow_previous_leanctx_environment" == false ]] || return 1
  [[ -f "$service_file" && ! -L "$service_file" ]] || return 1
  workflow_python - "$service_file" "$data_root" "$service_kind" "$ownership_mode" \
    "$workflow_root" "$allow_previous_leanctx_environment" <<'PY'
import os
import plistlib
import shlex
import sys
from pathlib import Path

path = Path(sys.argv[1])
data_root = sys.argv[2]
kind = sys.argv[3]
mode = sys.argv[4]
workflow_root = sys.argv[5]
allow_previous_leanctx_environment = sys.argv[6] == "true"
route_runner = (
    'import os,sys; '
    'sys.path.insert(0, os.environ["ORICHUM_WORKFLOW_ROOT"]); '
    'from integrations.common.route_proxy import main; '
    'raise SystemExit(main())'
)


def valid_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        return False
    return str(port) == str(value) and 1024 <= port <= 65535


def leanctx_environment_owned(environment, expected):
    if environment == expected:
        return True
    if not allow_previous_leanctx_environment:
        return False
    previous = dict(expected)
    del previous["LEAN_CTX_RULES_INJECTION"]
    return environment == previous


def claudex_proxy_arguments_owned(arguments):
    if isinstance(arguments, list) and len(arguments) == 9:
        if mode not in ("legacy", "either"):
            return False
        port = arguments[2]
        upstream = arguments[4]
        return (
            valid_port(port)
            and valid_port(upstream)
            and port != upstream
            and arguments == [
                f"{data_root}/bin/orichum-route-proxy",
                "--port",
                port,
                "--upstream-port",
                upstream,
                "--state-home",
                f"{data_root}/state",
                "--data-home",
                data_root,
            ]
        )
    if not isinstance(arguments, list) or len(arguments) not in (13, 15):
        return False
    port = arguments[6]
    upstream = arguments[8]
    catalog = arguments[10] if len(arguments) == 15 else upstream
    tail = (
        [
            "--catalog-port",
            catalog,
            "--state-home",
            f"{data_root}/state",
            "--data-home",
            data_root,
        ]
        if len(arguments) == 15
        else [
            "--state-home",
            f"{data_root}/state",
            "--data-home",
            data_root,
        ]
    )
    return (
        valid_port(port)
        and valid_port(upstream)
        and valid_port(catalog)
        and (
            len({port, upstream, catalog}) == 3
            if len(arguments) == 15
            else port != upstream
        )
        and arguments == [
            f"{data_root}/bin/orichum-python",
            "-I",
            "-B",
            "-c",
            route_runner,
            "--port",
            port,
            "--upstream-port",
            upstream,
            *tail,
        ]
    )


raw = path.read_bytes()
if b"<plist" in raw[:500]:
    document = plistlib.loads(raw)
    arguments = document.get("ProgramArguments")
    if kind == "cliproxy":
        owned = (
            document.get("Label") == "io.orichum.cliproxy"
            and arguments == [
                f"{data_root}/bin/cli-proxy-api",
                "--config",
                f"{data_root}/cliproxy.yaml",
            ]
        )
    elif kind == "leanctx-proxy":
        environment = document.get("EnvironmentVariables")
        port_argument = (
            arguments[3]
            if isinstance(arguments, list) and len(arguments) == 4
            else ""
        )
        owned = (
            document.get("Label") == "io.orichum.leanctx-proxy"
            and arguments == [
                f"{data_root}/bin/lean-ctx",
                "proxy",
                "start",
                port_argument,
            ]
            and port_argument.startswith("--port=")
            and valid_port(port_argument[len("--port="):])
            and leanctx_environment_owned(
                environment,
                {
                    "HOME": os.environ.get("HOME"),
                    "LEAN_CTX_CACHE_DIR": f"{data_root}/leanctx/proxy/cache",
                    "LEAN_CTX_CONFIG_DIR": f"{data_root}/leanctx/proxy/config",
                    "LEAN_CTX_DATA_DIR": f"{data_root}/leanctx/lean-ctx",
                    "LEAN_CTX_HEADLESS": "1",
                    "LEAN_CTX_MINIMAL": "1",
                    "LEAN_CTX_RULES_INJECTION": "off",
                    "LEAN_CTX_STATE_DIR": f"{data_root}/leanctx/proxy/state",
                    "XDG_DATA_HOME": f"{data_root}/leanctx",
                },
            )
        )
    elif kind == "claudex-proxy":
        environment = document.get("EnvironmentVariables")
        legacy = isinstance(arguments, list) and len(arguments) == 9
        owned = (
            document.get("Label") == "io.orichum.route-proxy"
            and claudex_proxy_arguments_owned(arguments)
            and isinstance(environment, dict)
            and environment.get("HOME") == os.environ.get("HOME")
            and (
                (
                    legacy
                    and environment.get("ORICHUM_DATA_HOME")
                    in (None, data_root)
                )
                or (
                    not legacy
                    and environment.get("ORICHUM_WORKFLOW_ROOT") == workflow_root
                    and environment.get("ORICHUM_PYTHON")
                    == f"{data_root}/bin/orichum-python"
                    and environment.get("ORICHUM_DATA_HOME")
                    in (None, data_root)
                )
            )
        )
    else:
        owned = False
    raise SystemExit(0 if owned else 1)

lines = [
    line.strip() for line in raw.decode("utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith(("#", ";"))
]


def decoded_words(value):
    return shlex.split(value.replace("%%", "%").replace("$$", "$"))


def decoded_environment_words(value):
    return shlex.split(value.replace("%%", "%"))


def systemd_quote(value, escape_dollar=True):
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if escape_dollar:
        value = value.replace("$", "$$")
    return f'"{value}"'


exec_lines = [line[len("ExecStart="):] for line in lines if line.startswith("ExecStart=")]
if len(exec_lines) != 1:
    raise SystemExit(1)
try:
    arguments = decoded_words(exec_lines[0])
except ValueError:
    raise SystemExit(1)

if kind == "cliproxy":
    expected = [
        f"{data_root}/bin/cli-proxy-api",
        "--config",
        f"{data_root}/cliproxy.yaml",
    ]
    raise SystemExit(0 if arguments == expected else 1)

descriptions = [line for line in lines if line.startswith("Description=")]
environment = {}
for line in lines:
    if not line.startswith("Environment="):
        continue
    try:
        values = decoded_environment_words(line[len("Environment="):])
    except ValueError:
        raise SystemExit(1)
    for value in values:
        if "=" in value:
            key, item = value.split("=", 1)
            environment[key] = item

if kind == "leanctx-proxy":
    port_argument = arguments[3] if len(arguments) == 4 else ""
    owned = (
        descriptions == ["Description=Orichum LeanCTX proxy"]
        and arguments == [
            f"{data_root}/bin/lean-ctx",
            "proxy",
            "start",
            port_argument,
        ]
        and port_argument.startswith("--port=")
        and valid_port(port_argument[len("--port="):])
        and leanctx_environment_owned(
            environment,
            {
                "HOME": os.environ.get("HOME", ""),
                "LEAN_CTX_CACHE_DIR": f"{data_root}/leanctx/proxy/cache",
                "LEAN_CTX_CONFIG_DIR": f"{data_root}/leanctx/proxy/config",
                "LEAN_CTX_DATA_DIR": f"{data_root}/leanctx/lean-ctx",
                "LEAN_CTX_HEADLESS": "1",
                "LEAN_CTX_MINIMAL": "1",
                "LEAN_CTX_RULES_INJECTION": "off",
                "LEAN_CTX_STATE_DIR": f"{data_root}/leanctx/proxy/state",
                "XDG_DATA_HOME": f"{data_root}/leanctx",
            },
        )
    )
    raise SystemExit(0 if owned else 1)

if kind == "claudex-proxy":
    legacy = len(arguments) == 9
    port = (
        arguments[2]
        if legacy
        else arguments[6]
        if len(arguments) in (13, 15)
        else ""
    )
    upstream = (
        arguments[4]
        if legacy
        else arguments[8]
        if len(arguments) in (13, 15)
        else ""
    )
    if legacy:
        expected_exec = " ".join([
            systemd_quote(f"{data_root}/bin/orichum-route-proxy"),
            "--port",
            port,
            "--upstream-port",
            upstream,
            "--state-home",
            systemd_quote(f"{data_root}/state"),
            "--data-home",
            systemd_quote(data_root),
        ])
    elif len(arguments) == 13:
        expected_exec = " ".join([
            systemd_quote(f"{data_root}/bin/orichum-python"),
            "-I",
            "-B",
            "-c",
            systemd_quote(route_runner),
            "--port",
            port,
            "--upstream-port",
            upstream,
            "--state-home",
            systemd_quote(f"{data_root}/state"),
            "--data-home",
            systemd_quote(data_root),
        ])
    else:
        catalog = arguments[10] if len(arguments) == 15 else ""
        expected_exec = " ".join([
            systemd_quote(f"{data_root}/bin/orichum-python"),
            "-I",
            "-B",
            "-c",
            systemd_quote(route_runner),
            "--port",
            port,
            "--upstream-port",
            upstream,
            "--catalog-port",
            catalog,
            "--state-home",
            systemd_quote(f"{data_root}/state"),
            "--data-home",
            systemd_quote(data_root),
        ])
    environment_lines = [line for line in lines if line.startswith("Environment=")]
    expected_environment = "Environment=" + systemd_quote(
        f"HOME={os.environ.get('HOME', '')}", escape_dollar=False
    )
    expected_data_environment = "Environment=" + systemd_quote(
        f"ORICHUM_DATA_HOME={data_root}", escape_dollar=False
    )
    expected_workflow_environment = "Environment=" + systemd_quote(
        f"ORICHUM_WORKFLOW_ROOT={workflow_root}", escape_dollar=False
    )
    expected_python_environment = "Environment=" + systemd_quote(
        f"ORICHUM_PYTHON={data_root}/bin/orichum-python",
        escape_dollar=False,
    )
    owned = (
        descriptions == ["Description=Orichum same-family recovery proxy"]
        and claudex_proxy_arguments_owned(arguments)
        and exec_lines[0] == expected_exec
        and (
            (
                legacy
                and environment_lines in (
                    [expected_environment],
                    [expected_environment, expected_data_environment],
                )
            )
            or (
                not legacy
                and environment_lines in (
                    [
                        expected_environment,
                        expected_workflow_environment,
                        expected_python_environment,
                    ],
                    [
                        expected_environment,
                        expected_workflow_environment,
                        expected_python_environment,
                        expected_data_environment,
                    ],
                )
            )
        )
        and environment.get("HOME") == os.environ.get("HOME")
        and (
            (
                legacy
                and environment.get("ORICHUM_DATA_HOME")
                in (None, data_root)
            )
            or (
                not legacy
                and environment.get("ORICHUM_WORKFLOW_ROOT") == workflow_root
                and environment.get("ORICHUM_PYTHON")
                == f"{data_root}/bin/orichum-python"
                and environment.get("ORICHUM_DATA_HOME")
                in (None, data_root)
            )
        )
    )
    raise SystemExit(0 if owned else 1)

raise SystemExit(1)
PY
}

cliproxy_service_is_owned() {
  service_definition_is_owned "$1" "$2" cliproxy
}

leanctx_proxy_service_is_owned() {
  service_definition_is_owned \
    "$1" "$2" leanctx-proxy either "" "${3:-false}"
}

claudex_proxy_service_is_owned() {
  service_definition_is_owned "$1" "$2" claudex-proxy either "${3:-}"
}

leanctx_proxy_service_identity() {
  local platform="$1"
  case "$platform" in
    darwin)
      printf '%s\t%s\t%s\n' \
        "$HOME/Library/LaunchAgents/io.orichum.leanctx-proxy.plist" \
        'io.orichum.leanctx-proxy' '-'
      ;;
    systemd)
      printf '%s\t%s\t%s\n' \
        "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/orichum-leanctx-proxy.service" \
        '-' 'orichum-leanctx-proxy.service'
      ;;
    *) return 1 ;;
  esac
}

claudex_proxy_service_identity() {
  local platform="$1"
  case "$platform" in
    darwin)
      printf '%s\t%s\t%s\n' \
        "$HOME/Library/LaunchAgents/io.orichum.route-proxy.plist" \
        'io.orichum.route-proxy' '-'
      ;;
    systemd)
      printf '%s\t%s\t%s\n' \
        "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/orichum-route-proxy.service" \
        '-' 'orichum-route-proxy.service'
      ;;
    *) return 1 ;;
  esac
}

managed_service_target_state() {
  local platform="$1"
  local label="$2"
  local unit="$3"
  local load_state output status
  case "$platform" in
    darwin)
      if launchctl print "gui/$(id -u)/$label" \
          >/dev/null 2>&1; then
        printf 'loaded\n'
        return 0
      else
        status=$?
      fi
      [[ "$status" -eq 113 ]] || return 1
      printf 'absent\n'
      ;;
    systemd)
      load_state="$(systemctl --user show --property LoadState --value \
        "$unit" 2>/dev/null)" || return 1
      case "$load_state" in
        loaded) printf 'loaded\n' ;;
        not-found) printf 'absent\n' ;;
        *) return 1 ;;
      esac
      ;;
    *) return 1 ;;
  esac
}

managed_service_target_is_loaded() {
  [[ "$(managed_service_target_state "$@")" == loaded ]]
}

managed_service_definition_path() {
  local platform="$1"
  local label="$2"
  local unit="$3"
  local output definition_path
  case "$platform" in
    darwin)
      output="$(launchctl print "gui/$(id -u)/$label" 2>/dev/null)" || \
        return 1
      definition_path="$(printf '%s\n' "$output" | awk '
        sub(/^[[:space:]]*path = /, "") {print; exit}
      ')"
      ;;
    systemd)
      definition_path="$(systemctl --user show \
        --property FragmentPath --value "$unit" 2>/dev/null)" || return 1
      ;;
    *) return 1 ;;
  esac
  [[ "$definition_path" == /* ]] || return 1
  printf '%s\n' "$definition_path"
}

managed_service_main_pid_value() {
  local platform="$1"
  local label="$2"
  local unit="$3"
  local output service_pid
  case "$platform" in
    darwin)
      output="$(launchctl print "gui/$(id -u)/$label" 2>/dev/null)" || return 1
      service_pid="$(printf '%s\n' "$output" | \
        awk '$1 == "pid" && $2 == "=" && $3 ~ /^[1-9][0-9]*$/ {print $3; exit}')"
      service_pid="${service_pid:-0}"
      ;;
    systemd)
      output="$(systemctl --user show --property MainPID --value \
        "$unit" 2>/dev/null)" || return 1
      service_pid="$(printf '%s\n' "$output" | \
        awk '$0 ~ /^(0|[1-9][0-9]*)$/ {print; exit}')"
      ;;
    *) return 1 ;;
  esac
  [[ "$service_pid" =~ ^(0|[1-9][0-9]*)$ ]] || return 1
  printf '%s\n' "$service_pid"
}

managed_service_main_pid() {
  local service_pid
  service_pid="$(managed_service_main_pid_value "$@")" || return 1
  [[ "$service_pid" =~ ^[1-9][0-9]*$ ]] || return 1
  printf '%s\n' "$service_pid"
}

pid_owns_loopback_listener() {
  local service_pid="$1"
  local port="$2"
  local platform output
  [[ "$service_pid" =~ ^[1-9][0-9]*$ ]] || return 1
  valid_service_port "$port" || return 1
  platform="$(uname -s)" || return 1
  case "$platform" in
    Darwin)
      command -v lsof >/dev/null 2>&1 || return 1
      output="$(lsof -nP -a -p "$service_pid" \
        -iTCP@127.0.0.1:"$port" -sTCP:LISTEN -t 2>/dev/null)" || return 1
      printf '%s\n' "$output" | awk -v expected="$service_pid" '
        $0 == expected {found = 1}
        END {exit(found ? 0 : 1)}
      '
      ;;
    Linux)
      command -v ss >/dev/null 2>&1 || return 1
      output="$(ss -H -ltnp "sport = :$port" 2>/dev/null)" || return 1
      workflow_python - "$service_pid" "$port" "$output" <<'PY'
import re
import sys

pid, port, output = sys.argv[1:]
expected_address = f"127.0.0.1:{port}"
pid_pattern = re.compile(rf"(?:^|[,()])pid={re.escape(pid)}(?:,|[)])")
for line in output.splitlines():
    fields = line.split()
    if (
        len(fields) >= 5
        and fields[0] == "LISTEN"
        and fields[3] == expected_address
        and pid_pattern.search(line)
    ):
        raise SystemExit(0)
raise SystemExit(1)
PY
      ;;
    *) return 1 ;;
  esac
}

pid_owns_loopback_connection() {
  local service_pid="$1"
  local service_port="$2"
  local client_port="$3"
  local platform output
  [[ "$service_pid" =~ ^[1-9][0-9]*$ ]] || return 1
  valid_service_port "$service_port" || return 1
  valid_service_port "$client_port" || return 1
  platform="$(uname -s)" || return 1
  case "$platform" in
    Darwin)
      command -v lsof >/dev/null 2>&1 || return 1
      output="$(lsof -nP -a -p "$service_pid" -iTCP \
        -sTCP:ESTABLISHED -Fn 2>/dev/null)" || return 1
      printf '%s\n' "$output" | awk \
        -v expected="n127.0.0.1:${service_port}->127.0.0.1:${client_port}" '
          $0 == expected {found = 1}
          END {exit(found ? 0 : 1)}
        '
      ;;
    Linux)
      command -v ss >/dev/null 2>&1 || return 1
      output="$(ss -H -tnp \
        "( sport = :$service_port and dport = :$client_port )" \
        2>/dev/null)" || return 1
      workflow_python - "$service_pid" "$service_port" "$client_port" "$output" <<'PY'
import re
import sys

pid, service_port, client_port, output = sys.argv[1:]
expected_local = f"127.0.0.1:{service_port}"
expected_peer = f"127.0.0.1:{client_port}"
pid_pattern = re.compile(rf"(?:^|[,()])pid={re.escape(pid)}(?:,|[)])")
for line in output.splitlines():
    fields = line.split()
    if (
        len(fields) >= 5
        and fields[0] == "ESTAB"
        and fields[3] == expected_local
        and fields[4] == expected_peer
        and pid_pattern.search(line)
    ):
        raise SystemExit(0)
raise SystemExit(1)
PY
      ;;
    *) return 1 ;;
  esac
}

claudex_proxy_models_response_is_ready() {
  local response_file="$1"
  local expected_model="$2"
  jq -e --arg model "$expected_model" '
    .object == "list" and (.data | type == "array") and
    any(.data[]?; .id == $model)
  ' "$response_file" >/dev/null 2>&1
}

claudex_config_default_model() {
  local config_file="$1"
  [[ -f "$config_file" && ! -L "$config_file" ]] || return 1
  awk '
    /^[[:space:]]*default_model[[:space:]]*=/ {
      value = $0
      sub(/^[^=]*=[[:space:]]*"/, "", value)
      sub(/"[[:space:]]*$/, "", value)
      if (value == "" || found) exit 2
      found = 1
      model = value
    }
    END {
      if (found == 1) print model
      else exit 1
    }
  ' "$config_file"
}

validated_workflow_data_dir() {
  local checkout_root="$1"
  local data_root="${ORICHUM_DATA_HOME:-$(orichum_home_dir)}"
  workflow_python - "$data_root" "$HOME" "$checkout_root" <<'PY'
import os
import stat
import sys
from pathlib import Path

raw, home_raw, checkout_raw = sys.argv[1:]
if not os.path.isabs(raw):
    raise SystemExit("ORICHUM_DATA_HOME must be an absolute path")

normalized = Path(os.path.normpath(raw))
cursor = Path(normalized.anchor)
parts = normalized.parts[1:]
for index, component in enumerate(parts):
    cursor /= component
    try:
        value = os.lstat(cursor)
    except FileNotFoundError:
        break
    except OSError as error:
        raise SystemExit("ORICHUM_DATA_HOME existing ancestor is inaccessible") from error
    if stat.S_ISLNK(value.st_mode):
        raise SystemExit("ORICHUM_DATA_HOME existing ancestors must not be symlinks")
    if index < len(parts) - 1 and not stat.S_ISDIR(value.st_mode):
        raise SystemExit("ORICHUM_DATA_HOME existing ancestor is not a directory")

candidate = normalized.resolve(strict=False)
home = Path(home_raw).resolve(strict=False)
checkout = Path(checkout_raw).resolve(strict=True)
root = Path(candidate.anchor)
try:
    candidate.relative_to(checkout)
except ValueError:
    inside_checkout = False
else:
    inside_checkout = True
if candidate in (root, home) or inside_checkout:
    raise SystemExit("refusing unsafe ORICHUM_DATA_HOME")
print(candidate, end="")
PY
}

validated_orichum_home_dir() {
  local checkout_root="$1"
  local home_root
  home_root="$(orichum_home_dir)" || return 1
  workflow_python - "$home_root" "$HOME" "$checkout_root" <<'PY'
import os
import stat
import sys
from pathlib import Path

raw, user_home_raw, checkout_raw = sys.argv[1:]
if not os.path.isabs(raw):
    raise SystemExit("ORICHUM_HOME must be an absolute path")

normalized = Path(os.path.normpath(raw))
cursor = Path(normalized.anchor)
for index, component in enumerate(normalized.parts[1:]):
    cursor /= component
    try:
        value = os.lstat(cursor)
    except FileNotFoundError:
        break
    except OSError as error:
        raise SystemExit("ORICHUM_HOME existing ancestor is inaccessible") from error
    if stat.S_ISLNK(value.st_mode):
        raise SystemExit("ORICHUM_HOME existing ancestors must not be symlinks")
    if index < len(normalized.parts[1:]) - 1 and not stat.S_ISDIR(value.st_mode):
        raise SystemExit("ORICHUM_HOME existing ancestor is not a directory")

candidate = normalized.resolve(strict=False)
user_home = Path(user_home_raw).resolve(strict=True)
checkout = Path(checkout_raw).resolve(strict=True)
root = Path(candidate.anchor)
try:
    candidate.relative_to(checkout)
except ValueError:
    inside_checkout = False
else:
    inside_checkout = True
if candidate in (root, user_home) or inside_checkout:
    raise SystemExit("refusing unsafe ORICHUM_HOME")
print(candidate, end="")
PY
}

workflow_cleanup_init() {
  WORKFLOW_CLEANUP_PATHS=()
  WORKFLOW_LOCK_DIR=
  WORKFLOW_LOCK_IDENTITY=
  WORKFLOW_LOCK_FD=
  WORKFLOW_LOCK_GUARD_DIR=
  WORKFLOW_LOCK_GUARD_IDENTITY=
  WORKFLOW_LOCK_QUARANTINE_ACTIVE=false
  WORKFLOW_LOCK_QUARANTINE_RESTORE_REQUIRED=false
  WORKFLOW_LOCK_QUARANTINE_DIR=
  WORKFLOW_LOCK_QUARANTINE_CANONICAL=
  WORKFLOW_TRANSACTION_ACTIVE=false
  WORKFLOW_ROLLBACK_HANDLER=
}

orichum_lifecycle_lock_path() {
  local lifecycle_root="$HOME/.local/state/orichum"
  [[ "$HOME" == /* && -d "$HOME" && ! -L "$HOME" ]] || return 1
  for path in \
      "$HOME/.local" "$HOME/.local/state" "$lifecycle_root"; do
    [[ ! -L "$path" ]] || return 1
  done
  install -d -m 0700 "$lifecycle_root" || return 1
  chmod 0700 "$lifecycle_root" || return 1
  printf '%s\n' "$lifecycle_root/install.lock"
}

register_cleanup_path() {
  local cleanup_path="$1"
  case "$cleanup_path" in
    ''|/) workflow_die "refusing unsafe cleanup path" ;;
    *) WORKFLOW_CLEANUP_PATHS+=("$cleanup_path") ;;
  esac
}

workflow_cleanup() {
  local status="${1:-0}"
  local cleanup_path rollback_status=0 quarantine_status=0
  trap - EXIT INT TERM HUP

  if [[ "${WORKFLOW_LOCK_QUARANTINE_ACTIVE:-false}" == true ]]; then
    resolve_workflow_lock_quarantine || quarantine_status=$?
  fi

  if [[ "${WORKFLOW_TRANSACTION_ACTIVE:-false}" == true ]] && \
     [[ -n "${WORKFLOW_ROLLBACK_HANDLER:-}" ]]; then
    "$WORKFLOW_ROLLBACK_HANDLER" || rollback_status=$?
  fi

  for cleanup_path in "${WORKFLOW_CLEANUP_PATHS[@]:-}"; do
    [[ -n "$cleanup_path" ]] && rm -rf -- "$cleanup_path"
  done
  if ((quarantine_status == 0)); then
    release_workflow_lock "${WORKFLOW_LOCK_DIR:-}" || true
    release_workflow_lock_guard "${WORKFLOW_LOCK_GUARD_DIR:-}" || true
    rmdir "$HOME/.local/state/orichum" 2>/dev/null || true
  else
    printf 'ERROR: installer lock quarantine recovery failed; acquisition guard retained (fail-closed)\n' >&2
  fi

  if ((status == 0 && rollback_status != 0)); then
    status="$rollback_status"
  fi
  if ((status == 0 && quarantine_status != 0)); then
    status="$quarantine_status"
  fi
  exit "$status"
}

clear_workflow_lock_quarantine() {
  WORKFLOW_LOCK_QUARANTINE_ACTIVE=false
  WORKFLOW_LOCK_QUARANTINE_RESTORE_REQUIRED=false
  WORKFLOW_LOCK_QUARANTINE_DIR=
  WORKFLOW_LOCK_QUARANTINE_CANONICAL=
}

resolve_workflow_lock_quarantine() {
  local quarantine_dir="${WORKFLOW_LOCK_QUARANTINE_DIR:-}"
  local canonical_dir="${WORKFLOW_LOCK_QUARANTINE_CANONICAL:-}"
  local guard_dir="$canonical_dir.guard"
  local quarantined_pid quarantined_identity
  local restored_pid restored_identity

  [[ "${WORKFLOW_LOCK_QUARANTINE_ACTIVE:-false}" == true ]] || return 0
  if [[ "${WORKFLOW_LOCK_GUARD_DIR:-}" != "$guard_dir" ]] || \
     [[ ! -d "$guard_dir" ]] || \
     [[ "$(sed -n '1p' "$guard_dir/pid" 2>/dev/null || true)" != "$$" ]] || \
     [[ "$(sed -n '1p' "$guard_dir/identity" 2>/dev/null || true)" != \
        "${WORKFLOW_LOCK_GUARD_IDENTITY:-}" ]]; then
    workflow_die "cannot resolve installer lock quarantine without its owned acquisition guard"
    return 1
  fi
  if [[ "${WORKFLOW_LOCK_QUARANTINE_RESTORE_REQUIRED:-false}" != true ]]; then
    if [[ -n "$quarantine_dir" ]]; then
      rm -rf -- "$quarantine_dir" || {
        workflow_die "could not remove verified stale lock quarantine $quarantine_dir"
        return 1
      }
    fi
    clear_workflow_lock_quarantine
    return 0
  fi

  if [[ ! -d "$quarantine_dir" ]]; then
    workflow_die "quarantined installer lock is missing; acquisition guard retained"
    return 1
  fi
  if [[ -e "$canonical_dir" || -L "$canonical_dir" ]]; then
    workflow_die "canonical installer lock is occupied; quarantined owner and acquisition guard retained"
    return 1
  fi

  quarantined_pid="$(sed -n '1p' "$quarantine_dir/pid" 2>/dev/null || true)"
  quarantined_identity="$(sed -n '1p' "$quarantine_dir/identity" 2>/dev/null || true)"
  quarantined_identity="${quarantined_identity:-legacy-pid-only}"
  if ! mv "$quarantine_dir" "$canonical_dir" 2>/dev/null; then
    workflow_die "could not restore quarantined installer lock; acquisition guard retained"
    return 1
  fi

  restored_pid="$(sed -n '1p' "$canonical_dir/pid" 2>/dev/null || true)"
  restored_identity="$(sed -n '1p' "$canonical_dir/identity" 2>/dev/null || true)"
  restored_identity="${restored_identity:-legacy-pid-only}"
  if [[ "$restored_pid" != "$quarantined_pid" ]] || \
     [[ "$restored_identity" != "$quarantined_identity" ]]; then
    workflow_die "restored installer lock identity could not be verified; acquisition guard retained"
    return 1
  fi

  clear_workflow_lock_quarantine
}

acquire_workflow_lock_guard() {
  local guard_dir="$1"
  local owner_pid guard_identity

  if [[ "${WORKFLOW_LOCK_GUARD_DIR:-}" == "$guard_dir" ]] && \
     [[ -d "$guard_dir" ]] && \
     [[ "$(sed -n '1p' "$guard_dir/pid" 2>/dev/null || true)" == "$$" ]] && \
     [[ "$(sed -n '1p' "$guard_dir/identity" 2>/dev/null || true)" == \
        "${WORKFLOW_LOCK_GUARD_IDENTITY:-}" ]]; then
    return 0
  fi

  if ! mkdir "$guard_dir" 2>/dev/null; then
    owner_pid="$(sed -n '1p' "$guard_dir/pid" 2>/dev/null || true)"
    if [[ "$owner_pid" =~ ^[0-9]+$ ]] && kill -0 "$owner_pid" 2>/dev/null; then
      workflow_die \
        "installer lock acquisition guard is busy (pid $owner_pid); retry"
    else
      workflow_die \
        "stale installer lock acquisition guard found at $guard_dir; remove it manually and retry"
    fi
    return 1
  fi

  guard_identity="$$:$RANDOM:$RANDOM"
  printf '%s\n' "$guard_identity" >"$guard_dir/identity"
  printf '%s\n' "$$" >"$guard_dir/pid"
  WORKFLOW_LOCK_GUARD_DIR="$guard_dir"
  WORKFLOW_LOCK_GUARD_IDENTITY="$guard_identity"
}

release_workflow_lock_guard() {
  local guard_dir="${1:-}"
  local owner_pid owner_identity
  [[ -n "$guard_dir" && -d "$guard_dir" ]] || return 0
  owner_pid="$(sed -n '1p' "$guard_dir/pid" 2>/dev/null || true)"
  owner_identity="$(sed -n '1p' "$guard_dir/identity" 2>/dev/null || true)"
  [[ "$owner_pid" == "$$" ]] || return 0
  [[ -n "${WORKFLOW_LOCK_GUARD_IDENTITY:-}" ]] || return 0
  [[ "$owner_identity" == "$WORKFLOW_LOCK_GUARD_IDENTITY" ]] || return 0
  rm -rf -- "$guard_dir"
  if [[ "${WORKFLOW_LOCK_GUARD_DIR:-}" == "$guard_dir" ]]; then
    WORKFLOW_LOCK_GUARD_DIR=
    WORKFLOW_LOCK_GUARD_IDENTITY=
  fi
}

hold_workflow_lock_descriptor() {
  local lock_dir="$1"
  if ! exec 9<"$lock_dir"; then
    workflow_die "installer lock descriptor could not be retained"
    return 1
  fi
  WORKFLOW_LOCK_FD=9
}

acquire_workflow_lock() {
  local lock_dir="$1"
  local guard_dir="$lock_dir.guard"
  local owner_pid owner_identity stale_lock
  local quarantined_pid quarantined_identity lock_identity reclamation_error
  local failed_move_pid failed_move_identity

  acquire_workflow_lock_guard "$guard_dir" || return 1
  if [[ "${WORKFLOW_LOCK_QUARANTINE_ACTIVE:-false}" == true ]]; then
    if ! resolve_workflow_lock_quarantine; then
      workflow_die "installer lock remains fail-closed; acquisition guard retained"
      return 1
    fi
  fi

  if mkdir "$lock_dir" 2>/dev/null; then
    if ! chmod 0700 "$lock_dir"; then
      rm -rf -- "$lock_dir"
      release_workflow_lock_guard "$guard_dir" || true
      workflow_die "installer lock permissions could not be secured"
      return 1
    fi
    lock_identity="$$:$RANDOM:$RANDOM"
    printf '%s\n' "$lock_identity" >"$lock_dir/identity"
    printf '%s\n' "$$" >"$lock_dir/pid"
    if ! hold_workflow_lock_descriptor "$lock_dir"; then
      rm -rf -- "$lock_dir"
      release_workflow_lock_guard "$guard_dir" || true
      return 1
    fi
    WORKFLOW_LOCK_DIR="$lock_dir"
    WORKFLOW_LOCK_IDENTITY="$lock_identity"
    release_workflow_lock_guard "$guard_dir"
    return 0
  fi

  owner_pid="$(sed -n '1p' "$lock_dir/pid" 2>/dev/null || true)"
  owner_identity="$(sed -n '1p' "$lock_dir/identity" 2>/dev/null || true)"
  owner_identity="${owner_identity:-legacy-pid-only}"
  if [[ ! "$owner_pid" =~ ^[0-9]+$ ]]; then
    release_workflow_lock_guard "$guard_dir" || true
    workflow_die "installer lock has no valid owner: $lock_dir"
    return 1
  fi
  if kill -0 "$owner_pid" 2>/dev/null; then
    release_workflow_lock_guard "$guard_dir" || true
    workflow_die "another installer owns $lock_dir (pid $owner_pid)"
    return 1
  fi

  stale_lock="$lock_dir.stale.$$.$RANDOM"
  while [[ -e "$stale_lock" || -L "$stale_lock" ]]; do
    stale_lock="$lock_dir.stale.$$.$RANDOM"
  done
  WORKFLOW_LOCK_QUARANTINE_ACTIVE=true
  WORKFLOW_LOCK_QUARANTINE_RESTORE_REQUIRED=true
  WORKFLOW_LOCK_QUARANTINE_DIR="$stale_lock"
  WORKFLOW_LOCK_QUARANTINE_CANONICAL="$lock_dir"
  if ! mv "$lock_dir" "$stale_lock" 2>/dev/null; then
    if [[ -d "$stale_lock" ]]; then
      if resolve_workflow_lock_quarantine; then
        release_workflow_lock_guard "$guard_dir" || true
        workflow_die "installer lock rename reported failure after quarantine; owner restored"
      else
        workflow_die "installer lock rename/restoration ambiguous; acquisition guard retained (fail-closed)"
      fi
      return 1
    fi

    if [[ -d "$lock_dir" ]] && [[ ! -e "$stale_lock" && ! -L "$stale_lock" ]]; then
      failed_move_pid="$(sed -n '1p' "$lock_dir/pid" 2>/dev/null || true)"
      failed_move_identity="$(sed -n '1p' "$lock_dir/identity" 2>/dev/null || true)"
      failed_move_identity="${failed_move_identity:-legacy-pid-only}"
      if { [[ "$failed_move_pid" == "$owner_pid" ]] && \
           [[ "$failed_move_identity" == "$owner_identity" ]]; } || \
         { [[ "$failed_move_pid" =~ ^[0-9]+$ ]] && \
           kill -0 "$failed_move_pid" 2>/dev/null; }; then
        clear_workflow_lock_quarantine
        release_workflow_lock_guard "$guard_dir" || true
        workflow_die "installer lock rename failed without moving the observed owner"
        return 1
      fi
    fi

    workflow_die "installer lock rename result is ambiguous; acquisition guard retained (fail-closed)"
    return 1
  fi
  quarantined_pid="$(sed -n '1p' "$stale_lock/pid" 2>/dev/null || true)"
  quarantined_identity="$(sed -n '1p' "$stale_lock/identity" 2>/dev/null || true)"
  quarantined_identity="${quarantined_identity:-legacy-pid-only}"
  if [[ "$quarantined_pid" != "$owner_pid" ]] || \
     [[ "$quarantined_identity" != "$owner_identity" ]] || \
     kill -0 "$quarantined_pid" 2>/dev/null; then
    if resolve_workflow_lock_quarantine; then
      reclamation_error="installer lock owner changed during stale reclamation"
      release_workflow_lock_guard "$guard_dir" || true
    else
      reclamation_error="installer lock restoration failed; acquisition guard retained (fail-closed)"
    fi
    workflow_die "$reclamation_error"
    return 1
  fi
  if ! mkdir "$lock_dir" 2>/dev/null; then
    if resolve_workflow_lock_quarantine; then
      release_workflow_lock_guard "$guard_dir" || true
      workflow_die "could not claim canonical installer lock; quarantined owner restored"
    else
      workflow_die "could not claim or restore installer lock; acquisition guard retained (fail-closed)"
    fi
    return 1
  fi
  if ! chmod 0700 "$lock_dir"; then
    rm -rf -- "$lock_dir"
    resolve_workflow_lock_quarantine || true
    release_workflow_lock_guard "$guard_dir" || true
    workflow_die "installer lock permissions could not be secured"
    return 1
  fi
  lock_identity="$$:$RANDOM:$RANDOM"
  printf '%s\n' "$lock_identity" >"$lock_dir/identity"
  printf '%s\n' "$$" >"$lock_dir/pid"
  if ! hold_workflow_lock_descriptor "$lock_dir"; then
    rm -rf -- "$lock_dir"
    resolve_workflow_lock_quarantine || true
    release_workflow_lock_guard "$guard_dir" || true
    return 1
  fi
  WORKFLOW_LOCK_DIR="$lock_dir"
  WORKFLOW_LOCK_IDENTITY="$lock_identity"
  WORKFLOW_LOCK_QUARANTINE_RESTORE_REQUIRED=false
  rm -rf -- "$stale_lock"
  clear_workflow_lock_quarantine
  release_workflow_lock_guard "$guard_dir"
}

release_workflow_lock() {
  local lock_dir="${1:-}"
  local guard_dir
  local owner_pid owner_identity
  [[ -n "$lock_dir" ]] || return 0
  guard_dir="$lock_dir.guard"
  acquire_workflow_lock_guard "$guard_dir" || return 1
  if [[ "${WORKFLOW_LOCK_QUARANTINE_ACTIVE:-false}" == true ]] && \
     ! resolve_workflow_lock_quarantine; then
    workflow_die "installer lock remains fail-closed; acquisition guard retained"
    return 1
  fi
  if [[ ! -d "$lock_dir" ]]; then
    release_workflow_lock_guard "$guard_dir"
    return 0
  fi
  owner_pid="$(sed -n '1p' "$lock_dir/pid" 2>/dev/null || true)"
  owner_identity="$(sed -n '1p' "$lock_dir/identity" 2>/dev/null || true)"
  if [[ "$owner_pid" == "$$" ]] && \
     [[ -n "${WORKFLOW_LOCK_IDENTITY:-}" ]] && \
     [[ "$owner_identity" == "$WORKFLOW_LOCK_IDENTITY" ]]; then
    rm -rf -- "$lock_dir"
    if [[ "${WORKFLOW_LOCK_FD:-}" == 9 ]]; then
      exec 9<&-
      WORKFLOW_LOCK_FD=
    fi
    if [[ "${WORKFLOW_LOCK_DIR:-}" == "$lock_dir" ]]; then
      WORKFLOW_LOCK_DIR=
      WORKFLOW_LOCK_IDENTITY=
    fi
  fi
  release_workflow_lock_guard "$guard_dir"
}

file_change_state() {
  local desired_path="$1"
  local current_path="$2"
  if [[ -e "$current_path" || -L "$current_path" ]] && \
     cmp -s "$desired_path" "$current_path"; then
    printf '%s' unchanged
  else
    printf '%s' changed
  fi
}

run_rollback_if_active() {
  local transaction_active="$1"
  local rollback_handler="$2"
  [[ "$transaction_active" == true ]] || return 0
  "$rollback_handler"
}

sha256_text() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{print $1}'
  else
    sha256sum | awk '{print $1}'
  fi
}

controller_plugin_fingerprint() {
  (($# == 3)) || return 2
  local module_root="$1"
  local content_root="$2"
  local python_runtime="$3"
  "$python_runtime" -I -B - "$module_root" "$content_root" <<'PY'
import os
from pathlib import Path
import stat
import sys

module_root = Path(sys.argv[1]).absolute()
content_root = Path(sys.argv[2]).absolute()
sys.path.insert(0, str(module_root))
from integrations.common.install_state import (  # noqa: E402
    InstallStateError,
    fingerprint_paths,
)

plugin_root = content_root / "controller" / "plugin"
uid = os.getuid()
paths: list[Path] = []
try:
    for directory, names, files in os.walk(
        plugin_root, topdown=True, followlinks=False
    ):
        current = Path(directory)
        observed = os.lstat(current)
        if (
            stat.S_ISLNK(observed.st_mode)
            or not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != uid
            or stat.S_IMODE(observed.st_mode) & 0o022
        ):
            raise InstallStateError(
                "controller plugin directory is unsafe"
            )
        for name in sorted(names):
            child = current / name
            child_state = os.lstat(child)
            if (
                stat.S_ISLNK(child_state.st_mode)
                or not stat.S_ISDIR(child_state.st_mode)
            ):
                raise InstallStateError(
                    "controller plugin entry is unsafe"
                )
        names[:] = sorted(
            name for name in names if name != "__pycache__"
        )
        for name in sorted(files):
            child = current / name
            child_state = os.lstat(child)
            if (
                stat.S_ISLNK(child_state.st_mode)
                or not stat.S_ISREG(child_state.st_mode)
            ):
                raise InstallStateError(
                    "controller plugin entry is unsafe"
                )
            if name.endswith((".pyc", ".pyo")):
                continue
            paths.append(child.relative_to(content_root))
    paths.append(Path("config/plugins.json"))
    print(fingerprint_paths(content_root, paths))
except (InstallStateError, OSError, RuntimeError, ValueError) as error:
    print(f"ERROR: {error}", file=sys.stderr)
    raise SystemExit(1)
PY
}

verified_route_runtime_digest() {
  (($# == 4)) || return 2
  local workflow_root="$1"
  local python_runtime="$2"
  local python_version="$3"
  local descriptor="$4"
  "$python_runtime" -I -B - \
    "$workflow_root" "$python_runtime" \
    "$python_version" "$descriptor" <<'PY'
import hashlib
import os
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
runtime = Path(sys.argv[2])
version = sys.argv[3]
descriptor = Path(sys.argv[4])
uid = os.getuid()
digest = hashlib.sha256()


def add_file(label: str, path: Path) -> None:
    observed = os.lstat(path)
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != uid
        or stat.S_IMODE(observed.st_mode) & 0o022
    ):
        raise SystemExit(1)
    digest.update(label.encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")


def add_tree(relative: str) -> None:
    base = root / relative
    for directory, names, files in os.walk(
        base, topdown=True, followlinks=False
    ):
        current = Path(directory)
        observed = os.lstat(current)
        if (
            stat.S_ISLNK(observed.st_mode)
            or not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != uid
            or stat.S_IMODE(observed.st_mode) & 0o022
        ):
            raise SystemExit(1)
        names[:] = sorted(name for name in names if name != "__pycache__")
        for name in names:
            child = current / name
            child_state = os.lstat(child)
            if (
                stat.S_ISLNK(child_state.st_mode)
                or not stat.S_ISDIR(child_state.st_mode)
            ):
                raise SystemExit(1)
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            path = current / name
            add_file(path.relative_to(root).as_posix(), path)


add_file("python", runtime)
digest.update(version.encode())
digest.update(b"\0")
for relative in (
    "VERSION",
    "lib/workflow.sh",
    "bin/orichum",
    "bin/orichum-route-proxy",
    "bin/orichum-statusline",
    "bin/orichum-verify-leanctx-proxy",
    "controller/settings.json",
    "config/plugins.json",
):
    add_file(relative, root / relative)
add_tree("integrations/common")
add_tree("controller/plugin")
value = digest.hexdigest()
descriptor.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
descriptor.write_text(value + "\n", encoding="ascii")
os.chmod(descriptor, 0o600)
print(value)
PY
}

route_service_runtime_digest() {
  (($# == 1)) || return 2
  local service_file="$1"
  workflow_python - "$service_file" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    raw = path.read_bytes()
except OSError:
    raise SystemExit(1)
matches = re.findall(
    rb"Orichum route runtime SHA-256: ([0-9a-f]{64})",
    raw,
)
if len(matches) != 1:
    raise SystemExit(1)
print(matches[0].decode("ascii"))
PY
}

verified_routing_input_fingerprint() {
  (($# >= 9)) || return 2
  local descriptor="$1"
  local cliproxy_artifact="$2"
  local claudex_artifact="$3"
  local route_runtime="$4"
  local cliproxy_port="$5"
  local claudex_port="$6"
  local route_port="$7"
  local leanctx_port="$8"
  shift 8
  python3 -I -B - \
    "$descriptor" "$cliproxy_artifact" "$claudex_artifact" \
    "$route_runtime" "$cliproxy_port" "$claudex_port" "$route_port" \
    "$leanctx_port" \
    "$@" <<'PY'
import hashlib
import os
from pathlib import Path
import stat
import sys

descriptor = Path(sys.argv[1])
values = sys.argv[2:9]
paths = [Path(value) for value in sys.argv[9:]]
uid = os.getuid()
digest = hashlib.sha256()
for label, value in zip(
    ("cliproxy", "claudex", "route-runtime", "cliproxy-port",
     "claudex-port", "route-port", "leanctx-port"),
    values,
):
    digest.update(label.encode())
    digest.update(b"=")
    digest.update(value.encode())
    digest.update(b"\0")
for index, path in enumerate(paths):
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        raise SystemExit(1)
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != uid
        or stat.S_IMODE(observed.st_mode) & 0o022
    ):
        raise SystemExit(1)
    digest.update(str(index).encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
value = digest.hexdigest()
descriptor.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
descriptor.write_text(value + "\n", encoding="ascii")
os.chmod(descriptor, 0o600)
print(value)
PY
}

verified_routing_runtime_artifact() {
  (($# == 6)) || return 2
  local data_root="$1"
  local config_root="$2"
  local cliproxy_service="$3"
  local leanctx_service="$4"
  local route_service="$5"
  local descriptor="$6"
  local active_claudex active_models active_effective
  active_claudex="$(model_config_file "$data_root" claudex.toml)" || return 1
  active_models="$(model_config_file "$data_root" models.json)" || return 1
  active_effective="$(
    model_config_file "$data_root" effective-models.json
  )" || return 1
  python3 -I -B - \
    "$descriptor" \
    "$data_root/cliproxy.yaml" \
    "$data_root/leanctx/proxy/config/config.toml" \
    "$cliproxy_service" "$leanctx_service" "$route_service" \
    "$active_claudex" "$active_models" "$active_effective" \
    "$data_root/claude-config/settings.json" \
    "$config_root/accounts.json" \
    "$config_root/jira-profiles.json" \
    "$config_root/model-stacks.json" \
    "$config_root/plugins.json" \
    "$config_root/projects.json" \
    "$config_root/providers.json" \
    "$config_root/runtime.json" \
    "$config_root/controller-policy.md" <<'PY'
import hashlib
import os
from pathlib import Path
import stat
import sys

descriptor = Path(sys.argv[1])
paths = [Path(value) for value in sys.argv[2:]]
uid = os.getuid()
digest = hashlib.sha256()
for index, path in enumerate(paths):
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        raise SystemExit(1)
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != uid
        or stat.S_IMODE(observed.st_mode) & 0o022
    ):
        raise SystemExit(1)
    digest.update(str(index).encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
value = digest.hexdigest()
descriptor.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
descriptor.write_text(value + "\n", encoding="ascii")
os.chmod(descriptor, 0o600)
print(value)
PY
}

orichum_completion_root() {
  (($# == 1)) || return 2
  printf '%s/completions' "$1"
}

orichum_fish_completion_path() {
  printf '%s/fish/completions/orichum.fish' \
    "${XDG_CONFIG_HOME:-$HOME/.config}"
}

orichum_fish_completion_record_path() {
  (($# == 1)) || return 2
  printf '%s/completions/fish-path' "$1"
}

orichum_recorded_fish_completion_path() {
  (($# == 1)) || return 2
  workflow_python -I -B - "$(orichum_fish_completion_record_path "$1")" <<'PY'
import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    observed = path.lstat()
except FileNotFoundError:
    raise SystemExit(1)
if (
    stat.S_ISLNK(observed.st_mode)
    or not stat.S_ISREG(observed.st_mode)
    or observed.st_uid != os.getuid()
    or stat.S_IMODE(observed.st_mode) != 0o600
    or observed.st_size > 4096
):
    raise SystemExit(10)
try:
    payload = path.read_text(encoding="utf-8")
except (OSError, UnicodeError):
    raise SystemExit(10)
if not payload.endswith("\n") or payload.count("\n") != 1:
    raise SystemExit(10)
value = payload[:-1]
if (
    not value.startswith("/")
    or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    or not value.endswith("/fish/completions/orichum.fish")
):
    raise SystemExit(10)
print(value)
PY
}

orichum_write_fish_completion_record() {
  (($# == 2)) || return 2
  local home_root="$1"
  local fish_path="$2"
  local record_path
  record_path="$(orichum_fish_completion_record_path "$home_root")"
  orichum_prepare_completion_directory \
    "$(dirname "$record_path")" "$home_root" || return 1
  workflow_python -I -B - "$record_path" "$fish_path" <<'PY'
import os
import stat
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
value = sys.argv[2]
if (
    not value.startswith("/")
    or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    or not value.endswith("/fish/completions/orichum.fish")
):
    raise SystemExit(1)
try:
    observed = path.lstat()
except FileNotFoundError:
    observed = None
if observed is not None and (
    stat.S_ISLNK(observed.st_mode)
    or not stat.S_ISREG(observed.st_mode)
    or observed.st_uid != os.getuid()
    or stat.S_IMODE(observed.st_mode) != 0o600
):
    raise SystemExit(1)
descriptor, temporary = tempfile.mkstemp(
    prefix=".orichum-fish-path.", dir=path.parent
)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
}

orichum_bash_login_profile_path() {
  local candidate
  for candidate in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
    if [[ -e "$candidate" || -L "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  printf '%s/.bash_profile\n' "$HOME"
}

orichum_prepare_completion_directory() {
  (($# == 2)) || return 2
  workflow_python -I -B - "$1" "$2" <<'PY'
import os
import stat
import sys
from pathlib import Path

directory = Path(sys.argv[1])
managed_root = Path(sys.argv[2])
if not directory.is_absolute() or not managed_root.is_absolute():
    raise SystemExit("completion paths must be absolute")
try:
    directory.relative_to(managed_root)
except ValueError as error:
    raise SystemExit("completion path escapes its managed root") from error

cursor = Path(directory.anchor)
managed_started = False
for component in directory.parts[1:]:
    cursor /= component
    if cursor == managed_root:
        managed_started = True
    try:
        observed = cursor.lstat()
    except FileNotFoundError:
        cursor.mkdir(mode=0o700 if cursor == managed_root else 0o755)
        observed = cursor.lstat()
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
        raise SystemExit(f"unsafe completion directory: {cursor}")
    if managed_started and observed.st_uid != os.getuid():
        raise SystemExit(f"foreign completion directory: {cursor}")
PY
}

render_orichum_completion_definition() {
  (($# == 3 || $# == 4)) || return 2
  local workflow_root="$1"
  local shell="$2"
  local destination="$3"
  local destination_dir managed_root temporary raw digest
  if [[ -e "$destination" || -L "$destination" ]]; then
    orichum_completion_file_is_owned "$destination" || {
      printf 'ERROR: refusing unknown Orichum completion path: %s\n' \
        "$destination" >&2
      return 1
    }
  fi
  destination_dir="$(dirname "$destination")"
  if (($# == 4)); then
    managed_root="$4"
  elif [[ "$shell" == fish ]]; then
    managed_root="${XDG_CONFIG_HOME:-$HOME/.config}"
  else
    managed_root="${ORICHUM_HOME:-$HOME/.orichum}"
  fi
  orichum_prepare_completion_directory \
    "$destination_dir" "$managed_root" || return 1
  temporary="$(mktemp "$destination_dir/.orichum-completion.XXXXXX")" || \
    return 1
  raw="$temporary.raw"
  if ! (
    cd "$workflow_root"
    PYTHONDONTWRITEBYTECODE=1 workflow_python -I -B - \
      "$workflow_root" "$shell" >"$raw" <<'PY'
import sys

root = sys.argv[1]
sys.path.insert(0, root)
from integrations.common.orichum_cli import build_parser
from integrations.common.orichum_completion import render_completion

sys.stdout.write(render_completion(build_parser(), sys.argv[2]))
PY
  ); then
    rm -f -- "$temporary" "$raw"
    return 1
  fi
  [[ -s "$raw" ]] || {
    rm -f -- "$temporary" "$raw"
    return 1
  }
  case "$shell" in
    bash)
      if command -v bash >/dev/null 2>&1; then
        bash -n "$raw" || {
          rm -f -- "$temporary" "$raw"
          return 1
        }
      fi
      ;;
    zsh)
      if command -v zsh >/dev/null 2>&1; then
        zsh -n "$raw" || {
          rm -f -- "$temporary" "$raw"
          return 1
        }
      fi
      ;;
    fish)
      if command -v fish >/dev/null 2>&1; then
        fish -n "$raw" || {
          rm -f -- "$temporary" "$raw"
          return 1
        }
      fi
      ;;
    *)
      rm -f -- "$temporary" "$raw"
      return 2
      ;;
  esac
  digest="$(sha256_file "$raw")" || {
    rm -f -- "$temporary" "$raw"
    return 1
  }
  if [[ "$shell" == zsh ]]; then
    {
      IFS= read -r first_line <"$raw"
      printf '%s\n' "$first_line"
      printf '# Orichum completion body-sha256: %s\n' "$digest"
      sed '1d' "$raw"
    } >"$temporary"
  else
    {
      printf '# Orichum completion body-sha256: %s\n' "$digest"
      cat "$raw"
    } >"$temporary"
  fi
  chmod 0644 "$temporary"
  mv -f "$temporary" "$destination"
  rm -f -- "$raw"
}

orichum_completion_file_is_owned() {
  (($# == 1)) || return 2
  workflow_python -I -B - "$1" <<'PY'
import hashlib
import os
import re
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    observed = path.lstat()
except FileNotFoundError:
    raise SystemExit(1)
if (
    stat.S_ISLNK(observed.st_mode)
    or not stat.S_ISREG(observed.st_mode)
    or observed.st_uid != os.getuid()
    or stat.S_IMODE(observed.st_mode) & 0o022
):
    raise SystemExit(1)
lines = path.read_bytes().splitlines(keepends=True)
header = re.compile(
    rb"^# Orichum completion body-sha256: ([a-f0-9]{64})\n$"
)
matches = [
    (index, header.fullmatch(line))
    for index, line in enumerate(lines[:2])
    if header.fullmatch(line)
]
if len(matches) != 1:
    raise SystemExit(1)
index, match = matches[0]
body = b"".join((*lines[:index], *lines[index + 1 :]))
if hashlib.sha256(body).hexdigest().encode("ascii") != match.group(1):
    raise SystemExit(1)
PY
}

orichum_profile_block() {
  (($# == 3)) || return 2
  local shell="$1"
  local completion_path="$2"
  local destination="$3"
  local quoted
  printf -v quoted '%q' "$completion_path"
  case "$shell" in
    zsh)
      cat >"$destination" <<EOF
# >>> Orichum completion >>>
fpath=($quoted \$fpath)
if (( \$+functions[compdef] )); then
  autoload -Uz _orichum
  compdef _orichum orichum
fi
# <<< Orichum completion <<<
EOF
      ;;
    bash)
      cat >"$destination" <<EOF
# >>> Orichum completion >>>
if [ -n "\${BASH_VERSION:-}" ] && [ -r $quoted ]; then
  . $quoted
fi
# <<< Orichum completion <<<
EOF
      ;;
    *) return 2 ;;
  esac
}

orichum_profile_block_matches() {
  (($# == 2)) || return 2
  workflow_python -I -B - "$1" "$2" <<'PY'
import os
import stat
import sys
from pathlib import Path

profile = Path(sys.argv[1])
expected = Path(sys.argv[2]).read_bytes()
try:
    observed = profile.lstat()
except FileNotFoundError:
    raise SystemExit(1)
if (
    stat.S_ISLNK(observed.st_mode)
    or not stat.S_ISREG(observed.st_mode)
    or observed.st_uid != os.getuid()
):
    raise SystemExit(1)
payload = profile.read_bytes()
if payload.count(b"# >>> Orichum completion >>>") != 1:
    raise SystemExit(1)
if payload.count(b"# <<< Orichum completion <<<") != 1:
    raise SystemExit(1)
start = payload.index(b"# >>> Orichum completion >>>")
end = payload.index(b"# <<< Orichum completion <<<", start)
end = payload.find(b"\n", end)
end = len(payload) if end < 0 else end + 1
if payload[start:end] != expected:
    raise SystemExit(1)
PY
}

reconcile_orichum_profile_block() {
  (($# == 4)) || return 2
  local profile="$1"
  local block="$2"
  local shell="$3"
  local manual="$4"
  local status=0
  workflow_python -I -B - "$profile" "$block" <<'PY' || status=$?
import os
import stat
import sys
import tempfile
from pathlib import Path

profile = Path(sys.argv[1])
block = Path(sys.argv[2]).read_bytes()
begin = b"# >>> Orichum completion >>>"
end = b"# <<< Orichum completion <<<"

def fingerprint(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_size,
        value.st_mtime_ns,
    )

def read_path(path):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError:
        raise SystemExit(10)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            raise SystemExit(10)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read()
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            fingerprint(before) != fingerprint(after)
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise SystemExit(10)
        return current, payload
    finally:
        os.close(descriptor)

try:
    observed, payload = read_path(profile)
except FileNotFoundError:
    observed = None
    payload = b""
mode = 0o600 if observed is None else stat.S_IMODE(observed.st_mode)
begin_count = payload.count(begin)
end_count = payload.count(end)
if begin_count == end_count == 0:
    separator = b"" if not payload or payload.endswith(b"\n") else b"\n"
    separator += b"" if not payload else b"\n"
    updated = payload + separator + block
elif begin_count == end_count == 1:
    start = payload.index(begin)
    finish = payload.index(end, start)
    finish = payload.find(b"\n", finish)
    finish = len(payload) if finish < 0 else finish + 1
    if payload[start:finish] != block:
        raise SystemExit(10)
    updated = payload
else:
    raise SystemExit(10)
if updated == payload:
    raise SystemExit(0)
profile.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
descriptor, temporary = tempfile.mkstemp(
    prefix=".orichum-profile.", dir=profile.parent
)
claim = Path(temporary + ".original")

def retain_or_restore_claim():
    try:
        os.link(claim, profile, follow_symlinks=False)
    except FileExistsError:
        print(
            f"WARNING: original profile retained at conflict path: {claim}",
            file=sys.stderr,
        )
    except OSError:
        print(
            f"WARNING: original profile retained at conflict path: {claim}",
            file=sys.stderr,
        )
    else:
        os.unlink(claim)

try:
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    # Claim the path atomically before replacement.
    if observed is None:
        try:
            os.link(temporary, profile, follow_symlinks=False)
        except (FileExistsError, OSError):
            raise SystemExit(10)
    else:
        try:
            os.rename(profile, claim)
        except (FileNotFoundError, OSError):
            raise SystemExit(10)
        try:
            current, current_payload = read_path(claim)
        except (OSError, SystemExit):
            retain_or_restore_claim()
            raise
        if (
            fingerprint(current) != fingerprint(observed)
            or current_payload != payload
        ):
            retain_or_restore_claim()
            raise SystemExit(10)
        # Install without replacing a concurrent writer.
        try:
            os.link(temporary, profile, follow_symlinks=False)
        except (FileExistsError, OSError):
            retain_or_restore_claim()
            raise SystemExit(10)
        os.unlink(claim)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
  case "$status" in
    0) return 0 ;;
    10)
      # shellcheck disable=SC2034 # Consumed by install.sh after this call.
      ORICHUM_COMPLETION_OPTIONAL_SHELL="$shell"
      printf 'WARNING: retained unsafe or drifted Orichum completion profile: %s\n' \
        "$profile" >&2
      printf 'Manual %s activation: %s\n' "$shell" "$manual" >&2
      return 0
      ;;
    *) return "$status" ;;
  esac
}

reconcile_orichum_completions() {
  (($# == 4 || $# == 5)) || return 2
  local workflow_root="$1"
  local home_root="$2"
  local config_root="$3"
  local data_root="$4"
  local completion_root zsh_path bash_path fish_path fish_record prior_fish_path
  local bash_login_profile temporary record_status
  local zsh_manual bash_manual quoted
  completion_root="$(orichum_completion_root "$home_root")"
  zsh_path="$completion_root/zsh/_orichum"
  bash_path="$completion_root/bash/orichum"
  fish_path="$(orichum_fish_completion_path)"
  fish_record="$(orichum_fish_completion_record_path "$home_root")"
  bash_login_profile="${5:-$(orichum_bash_login_profile_path)}"
  prior_fish_path=
  record_status=0
  prior_fish_path="$(
    orichum_recorded_fish_completion_path "$home_root"
  )" || record_status=$?
  case "$record_status" in
    0) ;;
    1) prior_fish_path= ;;
    *)
      printf 'ERROR: refusing unsafe Orichum fish completion record: %s\n' \
        "$fish_record" >&2
      return 1
      ;;
  esac
  if [[ -n "$prior_fish_path" && "$prior_fish_path" != "$fish_path" && \
        ( -e "$prior_fish_path" || -L "$prior_fish_path" ) ]]; then
    orichum_completion_file_is_owned "$prior_fish_path" || {
      printf 'ERROR: refusing drifted prior Orichum fish completion: %s\n' \
        "$prior_fish_path" >&2
      return 1
    }
  fi
  ORICHUM_HOME="$home_root" ORICHUM_CONFIG_HOME="$config_root" \
  ORICHUM_DATA_HOME="$data_root" \
    render_orichum_completion_definition \
      "$workflow_root" zsh "$zsh_path" || return 1
  ORICHUM_HOME="$home_root" ORICHUM_CONFIG_HOME="$config_root" \
  ORICHUM_DATA_HOME="$data_root" \
    render_orichum_completion_definition \
      "$workflow_root" bash "$bash_path" || return 1
  ORICHUM_HOME="$home_root" ORICHUM_CONFIG_HOME="$config_root" \
  ORICHUM_DATA_HOME="$data_root" \
    render_orichum_completion_definition \
      "$workflow_root" fish "$fish_path" || return 1
  if [[ -n "$prior_fish_path" && "$prior_fish_path" != "$fish_path" ]]; then
    rm -f -- "$prior_fish_path"
  fi
  orichum_write_fish_completion_record "$home_root" "$fish_path" || \
    return 1
  temporary="$(mktemp -d "${TMPDIR:-/tmp}/orichum-profiles.XXXXXX")" || \
    return 1
  orichum_profile_block zsh "$(dirname "$zsh_path")" \
    "$temporary/zsh.block"
  orichum_profile_block bash "$bash_path" "$temporary/bash.block"
  printf -v quoted '%q' "$(dirname "$zsh_path")"
  zsh_manual="fpath=($quoted \$fpath); autoload -Uz compinit && compinit"
  printf -v quoted '%q' "$bash_path"
  bash_manual="source $quoted"
  reconcile_orichum_profile_block \
    "$HOME/.zshrc" "$temporary/zsh.block" zsh "$zsh_manual" || {
      rm -rf -- "$temporary"
      return 1
    }
  reconcile_orichum_profile_block \
    "$HOME/.bashrc" "$temporary/bash.block" bash "$bash_manual" || {
      rm -rf -- "$temporary"
      return 1
    }
  reconcile_orichum_profile_block \
    "$bash_login_profile" "$temporary/bash.block" bash "$bash_manual" || {
      rm -rf -- "$temporary"
      return 1
    }
  rm -rf -- "$temporary"
}

verify_orichum_completions() {
  (($# == 4 || $# == 5)) || return 2
  local workflow_root="$1"
  local home_root="$2"
  local config_root="$3"
  local data_root="$4"
  local completion_root zsh_path bash_path fish_path recorded_fish_path
  local bash_login_profile temporary shell target
  completion_root="$(orichum_completion_root "$home_root")"
  zsh_path="$completion_root/zsh/_orichum"
  bash_path="$completion_root/bash/orichum"
  fish_path="$(orichum_fish_completion_path)"
  recorded_fish_path="$(
    orichum_recorded_fish_completion_path "$home_root"
  )" || return 1
  [[ "$recorded_fish_path" == "$fish_path" ]] || return 1
  bash_login_profile="${5:-$(orichum_bash_login_profile_path)}"
  temporary="$(mktemp -d "${TMPDIR:-/tmp}/orichum-completion-verify.XXXXXX")" || \
    return 1
  temporary="$(cd -P "$temporary" && pwd)" || return 1
  for shell in zsh bash fish; do
    case "$shell" in
      zsh) target="$zsh_path" ;;
      bash) target="$bash_path" ;;
      fish) target="$fish_path" ;;
    esac
    orichum_completion_file_is_owned "$target" || {
      rm -rf -- "$temporary"
      return 1
    }
    ORICHUM_HOME="$home_root" ORICHUM_CONFIG_HOME="$config_root" \
    ORICHUM_DATA_HOME="$data_root" \
      render_orichum_completion_definition \
        "$workflow_root" "$shell" "$temporary/$shell" "$temporary" || {
          rm -rf -- "$temporary"
          return 1
        }
    cmp -s "$temporary/$shell" "$target" || {
      rm -rf -- "$temporary"
      return 1
    }
  done
  orichum_profile_block zsh "$(dirname "$zsh_path")" \
    "$temporary/zsh.block"
  orichum_profile_block bash "$bash_path" "$temporary/bash.block"
  local status=0
  if ! orichum_profile_block_matches \
      "$HOME/.zshrc" "$temporary/zsh.block"; then
    status=1
  elif ! orichum_profile_block_matches \
      "$HOME/.bashrc" "$temporary/bash.block"; then
    status=1
  elif ! orichum_profile_block_matches \
      "$bash_login_profile" "$temporary/bash.block"; then
    status=1
  fi
  rm -rf -- "$temporary"
  return "$status"
}

verified_orichum_completion_artifact() {
  (($# == 1)) || return 2
  local completion_root zsh_path bash_path fish_path recorded_fish_path
  completion_root="$(orichum_completion_root "$1")"
  zsh_path="$completion_root/zsh/_orichum"
  bash_path="$completion_root/bash/orichum"
  fish_path="$(orichum_fish_completion_path)"
  recorded_fish_path="$(
    orichum_recorded_fish_completion_path "$1"
  )" || return 1
  [[ "$recorded_fish_path" == "$fish_path" ]] || return 1
  orichum_completion_file_is_owned "$zsh_path" && \
    orichum_completion_file_is_owned "$bash_path" && \
    orichum_completion_file_is_owned "$fish_path" || return 1
  workflow_python -I -B - "$zsh_path" "$bash_path" "$fish_path" <<'PY'
import hashlib
import sys
from pathlib import Path

digest = hashlib.sha256()
for path in map(Path, sys.argv[1:]):
    digest.update(path.name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
print(digest.hexdigest())
PY
}

snapshot_path() {
  local source_path="$1"
  local snapshot_dir="$2"
  local snapshot_name="$3"
  install -d -m 0700 "$snapshot_dir"
  rm -f -- "$snapshot_dir/$snapshot_name.data" \
    "$snapshot_dir/$snapshot_name.present" "$snapshot_dir/$snapshot_name.absent"
  if [[ -e "$source_path" || -L "$source_path" ]]; then
    cp -pPR "$source_path" "$snapshot_dir/$snapshot_name.data"
    : >"$snapshot_dir/$snapshot_name.present"
  else
    : >"$snapshot_dir/$snapshot_name.absent"
  fi
}

restore_snapshot() {
  local destination="$1"
  local snapshot_dir="$2"
  local snapshot_name="$3"
  rm -f -- "$destination"
  if [[ -f "$snapshot_dir/$snapshot_name.present" ]]; then
    cp -pPR "$snapshot_dir/$snapshot_name.data" "$destination"
  elif [[ ! -f "$snapshot_dir/$snapshot_name.absent" ]]; then
    workflow_die "missing snapshot state for $destination"
    return 1
  fi
}

snapshot_path_matches() {
  local destination="$1"
  local snapshot_dir="$2"
  local snapshot_name="$3"
  if [[ -f "$snapshot_dir/$snapshot_name.present" ]]; then
    [[ -e "$destination" || -L "$destination" ]] && \
      cmp -s "$snapshot_dir/$snapshot_name.data" "$destination" && \
      [[ "$(path_mode "$snapshot_dir/$snapshot_name.data")" == \
         "$(path_mode "$destination")" ]]
  elif [[ -f "$snapshot_dir/$snapshot_name.absent" ]]; then
    [[ ! -e "$destination" && ! -L "$destination" ]]
  else
    workflow_die "missing snapshot state for $destination"
  fi
}

path_mode() {
  case "$(uname -s)" in
    Darwin) stat -f '%Lp' "$1" ;;
    Linux) stat -c '%a' "$1" ;;
    *) return 1 ;;
  esac
}

model_config_root() {
  printf '%s/model-config' "$1"
}

endpoint_config_lock_path() {
  printf '%s/endpoint.lock' "$(model_config_root "$1")"
}

acquire_endpoint_config_lock() {
  local data_root="$1"
  local lock_token="$2"
  local lock_path
  lock_path="$(endpoint_config_lock_path "$data_root")"
  if ! workflow_python - "$lock_token" "$lock_path" 2>/dev/null <<'PY'
import os
import sys
os.symlink(sys.argv[1], sys.argv[2])
PY
  then
    workflow_die "endpoint model publication is already locked (busy or stale)"
    return 1
  fi
}

release_endpoint_config_lock() {
  local data_root="$1"
  local lock_token="$2"
  local lock_path
  lock_path="$(endpoint_config_lock_path "$data_root")"
  if [[ ! -L "$lock_path" ]] || \
     [[ "$(readlink "$lock_path" 2>/dev/null || true)" != "$lock_token" ]]; then
    workflow_die "endpoint model publication lock ownership changed (fail-closed)"
    return 1
  fi
  rm -f -- "$lock_path"
}

acquire_model_publication_lock() {
  local data_root="$1"
  local lock_token="$2"
  local lock_dir
  lock_dir="$(model_config_root "$data_root")/publication.lock"
  if ! workflow_python - "$lock_token" "$lock_dir" 2>/dev/null <<'PY'
import os
import sys
os.symlink(sys.argv[1], sys.argv[2])
PY
  then
    workflow_die "model config publication is already locked (busy or stale)"
    return 1
  fi
  MODEL_PUBLICATION_LOCK_DIR="$lock_dir"
  MODEL_PUBLICATION_LOCK_IDENTITY="$lock_token"
}

release_model_publication_lock() {
  local lock_dir="${1:-${MODEL_PUBLICATION_LOCK_DIR:-}}"
  local lock_token="${2:-${MODEL_PUBLICATION_LOCK_IDENTITY:-}}"
  [[ -n "$lock_dir" ]] || return 0
  if [[ ! -L "$lock_dir" ]] || \
     [[ "$(readlink "$lock_dir" 2>/dev/null || true)" != "$lock_token" ]]; then
    workflow_die "model config publication lock ownership changed (fail-closed)"
    return 1
  fi
  rm -f -- "$lock_dir" || return 1
  MODEL_PUBLICATION_LOCK_DIR=
  MODEL_PUBLICATION_LOCK_IDENTITY=
}

atomic_replace_path() {
  workflow_python - "$1" "$2" <<'PY'
import os
import sys
os.replace(sys.argv[1], sys.argv[2])
PY
}

resolve_model_config_generation() {
  local data_root="$1"
  local config_root current_target generation
  config_root="$(model_config_root "$data_root")"
  [[ -L "$config_root/current" ]] || return 1
  current_target="$(readlink "$config_root/current")" || return 1
  case "$current_target" in
    ''|/*|*'/'*) return 1 ;;
  esac
  generation="$config_root/$current_target"
  [[ -d "$generation" && -f "$generation/models.json" && \
     -f "$generation/claudex.toml" ]] || return 1
  printf '%s' "$generation"
}

model_config_file() {
  local data_root="$1"
  local config_name="$2"
  local generation
  case "$config_name" in
    models.json|claudex.toml|effective-models.json) ;;
    *) workflow_die "unsupported model config file: $config_name"; return 1 ;;
  esac
  if generation="$(resolve_model_config_generation "$data_root")"; then
    printf '%s/model-config/current/%s' "$data_root" "$config_name"
  else
    printf '%s/%s' "$data_root" "$config_name"
  fi
}

ensure_model_config_compat_links() {
  local data_root="$1"
  local config_name desired_target temporary_link
  for config_name in models.json claudex.toml; do
    desired_target="model-config/current/$config_name"
    if [[ -L "$data_root/$config_name" ]] && \
       [[ "$(readlink "$data_root/$config_name")" == "$desired_target" ]]; then
      continue
    fi
    temporary_link="$data_root/.$config_name.$$.$RANDOM"
    rm -f -- "$temporary_link"
    ln -s "$desired_target" "$temporary_link" || return 1
    atomic_replace_path "$temporary_link" "$data_root/$config_name" || {
      rm -f -- "$temporary_link"
      return 1
    }
  done
}

_activate_model_config_generation() {
  local data_root="$1"
  local candidate="$2"
  local config_root candidate_name generation generation_name
  local owned_path pointer_candidate active_name stale_generation
  local prior_generation observed_generation observed_target
  config_root="$(model_config_root "$data_root")"
  [[ "$(dirname "$candidate")" == "$config_root" ]] || \
    { workflow_die "model config generation is outside its workflow root"; return 1; }
  candidate_name="$(basename "$candidate")"
  case "$candidate_name" in candidate.*) ;; *) return 1 ;; esac
  owned_path="$candidate"
  if [[ ! -f "$candidate/models.json" || ! -f "$candidate/claudex.toml" ]]; then
    rm -rf -- "$owned_path"
    return 1
  fi

  generation_name="generation.${candidate_name#candidate.}"
  generation="$config_root/$generation_name"
  if [[ -e "$generation" || -L "$generation" ]]; then
    rm -rf -- "$owned_path"
    return 1
  fi
  prior_generation=
  if observed_generation="$(resolve_model_config_generation "$data_root")"; then
    prior_generation="$observed_generation"
  fi
  if ! mv "$candidate" "$generation"; then
    if [[ ! -e "$candidate" && ! -L "$candidate" ]] && \
       [[ -e "$generation" || -L "$generation" ]]; then
      owned_path="$generation"
    fi
    rm -rf -- "$owned_path"
    return 1
  fi
  owned_path="$generation"

  pointer_candidate="$config_root/.current.$$.$RANDOM"
  if ! ln -s "$generation_name" "$pointer_candidate"; then
    rm -rf -- "$owned_path"
    return 1
  fi
  if ! atomic_replace_path "$pointer_candidate" "$config_root/current"; then
    rm -f -- "$pointer_candidate"
    observed_target="$(readlink "$config_root/current" 2>/dev/null || true)"
    if [[ "$observed_target" != "$generation_name" ]]; then
      observed_generation=
      if observed_generation="$(resolve_model_config_generation "$data_root")" && \
         [[ -n "$prior_generation" ]] && \
         [[ "$observed_generation" == "$prior_generation" ]]; then
        rm -rf -- "$owned_path"
      fi
      return 1
    fi
  fi
  [[ "$(readlink "$config_root/current")" == "$generation_name" ]] || return 1
  ensure_model_config_compat_links "$data_root" || \
    printf 'WARN: model config compatibility links could not be refreshed\n' >&2

  if [[ "${CLAUDEX_DEFER_MODEL_PRUNE:-0}" != 1 ]]; then
    active_name="$(readlink "$config_root/current")"
    for stale_generation in "$config_root"/generation.*; do
      [[ -d "$stale_generation" ]] || continue
      [[ "$(basename "$stale_generation")" == "$active_name" ]] || \
        rm -rf -- "$stale_generation"
    done
  fi
}

restore_model_config_generation() {
  local data_root="$1"
  local prior_target="$2"
  local prior_snapshot="${3:-}"
  local config_root current_target current_generation pointer_candidate config_name
  local lock_token restore_candidate
  config_root="$(model_config_root "$data_root")"
  case "$prior_target" in
    ''|generation.*) ;;
    *) return 1 ;;
  esac
  if [[ -n "$prior_snapshot" ]] && \
     [[ ! -f "$prior_snapshot/models.json" || \
        ! -f "$prior_snapshot/claudex.toml" ]]; then
    return 1
  fi
  lock_token="$$:$RANDOM:$RANDOM"
  acquire_model_publication_lock "$data_root" "$lock_token" || return 1
  if [[ -n "$prior_target" ]] && \
     [[ ! -f "$config_root/$prior_target/models.json" || \
        ! -f "$config_root/$prior_target/claudex.toml" ]]; then
    [[ -n "$prior_snapshot" ]] || {
      release_model_publication_lock \
        "$config_root/publication.lock" "$lock_token" || true
      return 1
    }
    if [[ -e "$config_root/$prior_target" || \
          -L "$config_root/$prior_target" ]]; then
      release_model_publication_lock \
        "$config_root/publication.lock" "$lock_token" || true
      return 1
    fi
    restore_candidate="$config_root/.generation.rollback.$$.$RANDOM"
    cp -pPR "$prior_snapshot" "$restore_candidate" || {
      rm -rf -- "$restore_candidate"
      release_model_publication_lock \
        "$config_root/publication.lock" "$lock_token" || true
      return 1
    }
    if ! mv "$restore_candidate" "$config_root/$prior_target"; then
      rm -rf -- "$restore_candidate"
      if [[ ! -f "$config_root/$prior_target/models.json" || \
            ! -f "$config_root/$prior_target/claudex.toml" ]]; then
        release_model_publication_lock \
          "$config_root/publication.lock" "$lock_token" || true
        return 1
      fi
    fi
  fi
  current_target="$(readlink "$config_root/current" 2>/dev/null || true)"
  if [[ "$current_target" == "$prior_target" ]] && \
     { [[ -n "$prior_target" ]] || \
       [[ ! -e "$config_root/current" && ! -L "$config_root/current" ]]; }; then
    release_model_publication_lock "$config_root/publication.lock" "$lock_token"
    return
  fi
  current_generation=
  case "$current_target" in
    generation.*) current_generation="$config_root/$current_target" ;;
  esac
  if [[ -n "$prior_target" ]]; then
    pointer_candidate="$config_root/.current.rollback.$$.$RANDOM"
    ln -s "$prior_target" "$pointer_candidate" || {
      release_model_publication_lock \
        "$config_root/publication.lock" "$lock_token" || true
      return 1
    }
    atomic_replace_path "$pointer_candidate" "$config_root/current" || {
      rm -f -- "$pointer_candidate"
      release_model_publication_lock \
        "$config_root/publication.lock" "$lock_token" || true
      return 1
    }
    ensure_model_config_compat_links "$data_root" || true
  else
    rm -f -- "$config_root/current"
    for config_name in models.json claudex.toml; do
      if [[ -L "$data_root/$config_name" ]] && \
         [[ "$(readlink "$data_root/$config_name")" == \
            "model-config/current/$config_name" ]]; then
        rm -f -- "$data_root/$config_name"
      fi
    done
  fi
  if [[ -n "$current_generation" && \
        "$(basename "$current_generation")" != "$prior_target" ]]; then
    rm -rf -- "$current_generation"
  fi
  release_model_publication_lock "$config_root/publication.lock" "$lock_token"
}

prune_model_config_generations() {
  local data_root="$1"
  local config_root active_name stale_generation lock_token
  config_root="$(model_config_root "$data_root")"
  lock_token="$$:$RANDOM:$RANDOM"
  acquire_model_publication_lock "$data_root" "$lock_token" || return 1
  active_name="$(readlink "$config_root/current" 2>/dev/null || true)"
  case "$active_name" in
    generation.*) ;;
    *)
      release_model_publication_lock \
        "$config_root/publication.lock" "$lock_token"
      return 0
      ;;
  esac
  for stale_generation in "$config_root"/generation.*; do
    [[ -d "$stale_generation" ]] || continue
    [[ "$(basename "$stale_generation")" == "$active_name" ]] || \
      rm -rf -- "$stale_generation"
  done
  release_model_publication_lock "$config_root/publication.lock" "$lock_token"
}

restore_model_publication_signal_traps() {
  trap - HUP INT TERM
  [[ -z "${publication_saved_hup:-}" ]] || eval "$publication_saved_hup"
  [[ -z "${publication_saved_int:-}" ]] || eval "$publication_saved_int"
  [[ -z "${publication_saved_term:-}" ]] || eval "$publication_saved_term"
}

redeliver_model_publication_signal() {
  local signal_name="$1"
  # In Bash 3.2, $$ remains the outer shell PID inside background and
  # parenthesized contexts. A freshly exec'd helper observes the actual Bash
  # execution context as its parent.
  "$BASH" -c 'kill -s "$1" "$PPID"' claudex-signal "$signal_name"
}

handle_model_publication_signal() {
  local signal_name="$1"
  local signal_status="$2"
  if [[ "$publication_lock_owned" == true ]] || \
     { [[ -L "$publication_lock_dir" ]] && \
       [[ "$(readlink "$publication_lock_dir" 2>/dev/null || true)" == \
          "$publication_lock_token" ]]; }; then
    release_model_publication_lock \
      "$publication_lock_dir" "$publication_lock_token" || true
  fi
  restore_model_publication_signal_traps
  if redeliver_model_publication_signal "$signal_name"; then
    return 0
  fi
  return "$signal_status"
}

_activate_model_config_generation_locked() {
  local data_root="$1"
  local candidate="$2"
  local publication_lock_dir publication_lock_token publication_trap_capture
  local status=0
  local publication_lock_owned=false
  local publication_saved_hup publication_saved_int publication_saved_term
  publication_lock_dir="$(model_config_root "$data_root")/publication.lock"
  publication_lock_token="$$:$RANDOM:$RANDOM"
  publication_trap_capture="$candidate/.publication-trap.capture"
  trap -p HUP >"$publication_trap_capture"
  publication_saved_hup=
  IFS= read -r -d '' publication_saved_hup \
    <"$publication_trap_capture" || true
  trap -p INT >"$publication_trap_capture"
  publication_saved_int=
  IFS= read -r -d '' publication_saved_int \
    <"$publication_trap_capture" || true
  trap -p TERM >"$publication_trap_capture"
  publication_saved_term=
  IFS= read -r -d '' publication_saved_term \
    <"$publication_trap_capture" || true
  rm -f -- "$publication_trap_capture"
  trap 'handle_model_publication_signal HUP 129' HUP
  trap 'handle_model_publication_signal INT 130' INT
  trap 'handle_model_publication_signal TERM 143' TERM
  if ! acquire_model_publication_lock "$data_root" "$publication_lock_token"; then
    restore_model_publication_signal_traps
    rm -rf -- "$candidate"
    return 1
  fi
  publication_lock_owned=true
  _activate_model_config_generation "$data_root" "$candidate" || status=$?
  if ! release_model_publication_lock \
    "$publication_lock_dir" "$publication_lock_token"; then
    restore_model_publication_signal_traps
    return 1
  fi
  publication_lock_owned=false
  restore_model_publication_signal_traps
  return "$status"
}

activate_model_config_generation() {
  local data_root="$1"
  local candidate="$2"
  local config_root candidate_name
  config_root="$(model_config_root "$data_root")"
  [[ "$(dirname "$candidate")" == "$config_root" ]] || {
    workflow_die "model config generation is outside its workflow root"
    return 1
  }
  candidate_name="$(basename "$candidate")"
  case "$candidate_name" in candidate.*) ;; *) return 1 ;; esac
  _activate_model_config_generation_locked "$data_root" "$candidate"
}

migrate_legacy_model_config() {
  local data_root="$1"
  local config_root candidate
  config_root="$(model_config_root "$data_root")"
  install -d -m 0700 "$config_root"
  if resolve_model_config_generation "$data_root" >/dev/null 2>&1; then
    ensure_model_config_compat_links "$data_root" || \
      printf 'WARN: model config compatibility links could not be refreshed\n' >&2
    return
  fi
  [[ ! -e "$config_root/current" && ! -L "$config_root/current" ]] || return 1
  [[ -f "$data_root/models.json" && ! -L "$data_root/models.json" && \
     -f "$data_root/claudex.toml" && ! -L "$data_root/claudex.toml" ]] || return 0

  candidate="$(mktemp -d "$config_root/candidate.XXXXXX")" || return 1
  if ! cp -p "$data_root/models.json" "$candidate/models.json" || \
     ! cp -p "$data_root/claudex.toml" "$candidate/claudex.toml"; then
    rm -rf -- "$candidate"
    return 1
  fi
  activate_model_config_generation "$data_root" "$candidate"
}

cliproxy_models_response_is_ready() {
  jq -e '.data | type == "array"' "$1" >/dev/null 2>&1
}

assert_owned_session() {
  local workflow_root="$1"
  local data_root="$2"
  local run_dir="$3"
  local context_sha256="$4"
  local effective_models_sha256="$5"
  (
    cd "$workflow_root" || exit 1
    workflow_python -m integrations.common.session_config verify \
      --workflow-root "$workflow_root" \
      --data-root "$data_root" \
      --run-dir "$run_dir" \
      --context-sha256 "$context_sha256" \
      --effective-models-sha256 "$effective_models_sha256"
  )
}

remove_managed_claude_base_url() {
  local input_file="$1"
  local output_file="$2"
  jq --indent 2 'if (.env? | type) == "object" then del(.env.ANTHROPIC_BASE_URL) else . end' \
    "$input_file" >"$output_file"
}

render_claudex_config() {
  local output_file="$1"
  local default_model="$2"
  local fast_model="$3"
  local balanced_model="$4"
  local powerful_model="$5"
  local haiku_model="$6"
  local sonnet_model="$7"
  local opus_model="$8"
  local claude_binary="${9:-}"
  local cliproxy_port="${10:-8317}"
  local claudex_proxy_port="${11:-13456}"
  local route_proxy_port="${12:-13457}"

  valid_service_port "$cliproxy_port" || return 1
  valid_service_port "$claudex_proxy_port" || return 1
  valid_service_port "$route_proxy_port" || return 1
  [[ "$cliproxy_port" != "$claudex_proxy_port" && \
     "$cliproxy_port" != "$route_proxy_port" && \
     "$claudex_proxy_port" != "$route_proxy_port" ]] || return 1

  if [[ -z "$claude_binary" ]]; then
    claude_binary="$(command -v claude)" || {
      workflow_die "claude is not installed or not on PATH"
      return 1
    }
  fi

  printf '%s\n' \
    "claude_binary = \"$claude_binary\"" \
    "proxy_port = $claudex_proxy_port" \
    'proxy_host = "127.0.0.1"' \
    'log_level = "info"' \
    'hyperlinks = "auto"' \
    '' \
    '[model_aliases]' \
    "fast = \"$fast_model\"" \
    "balanced = \"$balanced_model\"" \
    "powerful = \"$powerful_model\"" \
    '' \
    '[[profiles]]' \
    'name = "gpt"' \
    'provider_type = "DirectAnthropic"' \
    "base_url = \"http://127.0.0.1:$route_proxy_port\"" \
    'api_key = "claudex-passthrough"' \
    "default_model = \"$default_model\"" \
    'enabled = true' \
    'priority = 100' \
    '' \
    '[profiles.models]' \
    "haiku = \"$haiku_model\"" \
    "sonnet = \"$sonnet_model\"" \
    "opus = \"$opus_model\"" \
    '' \
    '[profiles.custom_headers]' \
    'X-Orichum-Session-ID = "unbound"' \
    '' \
    '[router]' \
    'enabled = false' \
    '' \
    '[context.compression]' \
    'enabled = false' \
    '' \
    '[context.sharing]' \
    'enabled = false' \
    '' \
    '[context.rag]' \
    'enabled = false' >"$output_file"
}

render_cliproxy_config() {
  local output_file="$1"
  local auth_dir="$2"
  local cliproxy_port="${3:-8317}"
  local management_key="${4:-}"

  valid_service_port "$cliproxy_port" || return 1
  if [[ "$management_key" =~ ^\$2[a-z]\$[0-9]{2}\$.{53}$ ]]; then
    :
  elif (( ${#management_key} < 32 || ${#management_key} > 256 )) || \
       [[ ! "$management_key" =~ ^[A-Za-z0-9._~-]+$ ]]; then
    return 1
  fi

  printf '%s\n' \
    'host: "127.0.0.1"' \
    "port: $cliproxy_port" \
    'tls:' \
    '  enable: false' \
    'remote-management:' \
    '  allow-remote: false' \
    "  secret-key: \"$management_key\"" \
    '  disable-control-panel: true' \
    "auth-dir: \"$auth_dir\"" \
    'api-keys: []' \
    'debug: false' \
    'pprof:' \
    '  enable: false' \
    'plugins:' \
    '  enabled: false' \
    'commercial-mode: true' \
    'logging-to-file: false' \
    'usage-statistics-enabled: false' \
    'passthrough-headers: true' \
    'request-retry: 1' \
    'max-retry-credentials: 0' \
    'max-retry-interval: 10' \
    'quota-exceeded:' \
    '  antigravity-credits: true' \
    'routing:' \
    '  strategy: "fill-first"' \
    '  session-affinity: true' >"$output_file"
}

sha256_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

select_first_available() {
  local available_models="$1"
  shift
  local candidate
  for candidate in "$@"; do
    if printf '%s\n' "$available_models" | rg -Fxq "$candidate"; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  printf '%s\n' "$available_models" | head -1
}

select_required_model() {
  local available_models="$1"
  shift
  local candidate
  for candidate in "$@"; do
    if printf '%s\n' "$available_models" | rg -Fxq "$candidate"; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  workflow_die "none of the required models are available: $*"
}

login_flag_for_provider() {
  local proxy_binary="$1"
  local provider="$2"
  local candidate help_text supported_names supported
  [[ "$provider" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] || {
    workflow_die "login provider name is unsafe"
    return 1
  }
  [[ -x "$proxy_binary" ]] || {
    workflow_die "CLIProxyAPI executable is unavailable"
    return 1
  }
  candidate="-${provider}-login"
  help_text="$("$proxy_binary" --help 2>&1)" || {
    workflow_die "CLIProxyAPI login capabilities could not be read"
    return 1
  }
  supported_names="$(printf '%s\n' "$help_text" |
    rg -o -- '-{1,2}[a-z0-9][a-z0-9-]*-login' |
    sed -e 's/^-*//' -e 's/-login$//' |
    sort -u)"
  supported="$(printf '%s\n' "$supported_names" |
    sort -u |
    awk 'BEGIN { separator="" } { printf "%s%s", separator, $0; separator=", " }')"
  printf '%s\n' "$supported_names" | rg -Fxq -- "$provider" || {
    workflow_die "installed CLIProxyAPI does not support provider '$provider'; supported OAuth providers: ${supported:-none}"
    return 1
  }
  printf '%s' "$candidate"
}

workflow_role_surface_is_exact() (
  local workflow_root="$1"
  local plugin_root="$2"
  local guard="$plugin_root/scripts/guard-orchestration.sh"
  local declared_roles role

  cd "$workflow_root" || exit 1
  declared_roles="$(workflow_python -B - "$plugin_root" <<'PY'
import sys
from pathlib import Path
from integrations.common.model_routing import ROLES

plugin = Path(sys.argv[1])
agents = plugin / "agents"
try:
    agent_entries = [
        entry for entry in agents.iterdir() if entry.suffix == ".md"
    ]
except OSError:
    raise SystemExit(1)
if any(entry.is_symlink() or not entry.is_file() for entry in agent_entries):
    raise SystemExit(1)
declared = set(ROLES)
source_roles = {entry.stem for entry in agent_entries}
if source_roles != declared:
    raise SystemExit(1)
print("\n".join(ROLES))
PY
  )" || exit 1

  [[ -f "$guard" && ! -L "$guard" && -x "$guard" ]] || exit 1

  workflow_invoke_agent_guard() {
    local agent_type="$1"
    local isolation="${2:-}"
    local guard_input
    guard_input="$(jq -cn \
      --arg agent_type "$agent_type" \
      --arg isolation "$isolation" \
      '{
        tool_name: "Agent",
        tool_input: (
          {subagent_type: $agent_type} +
          (if $isolation == "" then {} else {isolation: $isolation} end)
        )
      }'
    )" || return 1
    CLAUDE_PLUGIN_ROOT="$plugin_root" "$guard" <<<"$guard_input"
  }

  workflow_guard_permits() {
    local output
    output="$(workflow_invoke_agent_guard "$@" 2>/dev/null)" || return 1
    [[ -z "$output" ]]
  }

  workflow_guard_denies() {
    local output
    output="$(workflow_invoke_agent_guard "$@" 2>/dev/null)" || return 1
    jq -e '
      type == "object" and
      .hookSpecificOutput.hookEventName == "PreToolUse" and
      .hookSpecificOutput.permissionDecision == "deny"
    ' >/dev/null 2>&1 <<<"$output"
  }

  while IFS= read -r role; do
    if [[ "$role" == "implementation-worker" ]]; then
      workflow_guard_permits "claudex-controller:$role" worktree || exit 1
      workflow_guard_denies "claudex-controller:$role" || exit 1
    else
      workflow_guard_permits "claudex-controller:$role" || exit 1
      workflow_guard_denies "claudex-controller:$role" worktree || exit 1
    fi
  done <<<"$declared_roles"

  workflow_guard_denies Explore || exit 1
  workflow_guard_denies claudex-controller:unknown-role || exit 1
)

render_discovered_claudex_config() {
  local effective_models_json="$1"
  local output_file="$2"
  local cliproxy_port="${3:-8317}"
  local claudex_proxy_port="${4:-13456}"
  local route_proxy_port="${5:-13457}"
  local controller_model
  local fast_model balanced_model powerful_model
  local haiku_model sonnet_model opus_model

  controller_model="$(jq -er '.controller | strings | select(length > 0)' \
    "$effective_models_json")" || return 1
  fast_model="$(jq -er '.agents["repository-explorer"] |
    strings | select(length > 0)' "$effective_models_json")" || return 1
  balanced_model="$(jq -er '.agents["repository-verifier"] |
    strings | select(length > 0)' "$effective_models_json")" || return 1
  powerful_model="$controller_model"
  haiku_model="$fast_model"
  sonnet_model="$(jq -er '.agents["correctness-critic"] |
    strings | select(length > 0)' "$effective_models_json")" || return 1
  opus_model="$(jq -er '.agents["architecture-advisor"] |
    strings | select(length > 0)' "$effective_models_json")" || return 1

  render_claudex_config "$output_file" \
    "$controller_model" "$fast_model" "$balanced_model" "$powerful_model" \
    "$haiku_model" "$sonnet_model" "$opus_model" "" \
    "$cliproxy_port" "$claudex_proxy_port" "$route_proxy_port"
}

extract_semver() {
  printf '%s\n' "$1" | rg -o -m1 '[0-9]+\.[0-9]+\.[0-9]+' | head -1
}

binary_reports_semver() {
  local binary="$1"
  local expected="$2"
  local binary_dir binary_name expected_semver reported_output reported_semver
  local version_probe=--version
  expected_semver="$(extract_semver "$expected")" || return 1
  binary_dir="$(dirname "$binary")"
  binary_name="$(basename "$binary")"
  if [[ "$binary_name" == cli-proxy-api ]]; then
    version_probe=--help
  fi
  reported_output="$(
    cd "$binary_dir" || exit 1
    "./$binary_name" "$version_probe" 2>&1
  )" || return 1
  reported_semver="$(extract_semver "$reported_output")" || return 1
  [[ "$reported_semver" == "$expected_semver" ]]
}

semver_at_least() {
  local current="$1"
  local minimum="$2"
  local current_major current_minor current_patch
  local minimum_major minimum_minor minimum_patch

  [[ "$current" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  [[ "$minimum" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1

  IFS=. read -r current_major current_minor current_patch <<<"$current"
  IFS=. read -r minimum_major minimum_minor minimum_patch <<<"$minimum"

  ((10#$current_major > 10#$minimum_major)) && return 0
  ((10#$current_major < 10#$minimum_major)) && return 1
  ((10#$current_minor > 10#$minimum_minor)) && return 0
  ((10#$current_minor < 10#$minimum_minor)) && return 1
  ((10#$current_patch >= 10#$minimum_patch))
}

remove_managed_symlink() {
  local link_path="$1"
  local expected_target="$2"
  if [[ -L "$link_path" ]] && [[ "$(readlink "$link_path")" == "$expected_target" ]]; then
    unlink "$link_path"
  fi
}

fetch_latest_github_release() {
  local repository="$1"
  local output_file="$2"
  if [[ -n "${GH_TOKEN:-}" ]]; then
    gh api "repos/$repository/releases/latest" >"$output_file"
  else
    curl --fail --location --silent --show-error \
      "https://api.github.com/repos/$repository/releases/latest" \
      --output "$output_file"
  fi
}

fetch_github_release_tag() {
  local repository="$1"
  local tag="$2"
  local output_file="$3"
  if [[ -n "${GH_TOKEN:-}" ]]; then
    gh api "repos/$repository/releases/tags/$tag" >"$output_file"
  else
    curl --fail --location --silent --show-error \
      "https://api.github.com/repos/$repository/releases/tags/$tag" \
      --output "$output_file"
  fi
}

leanctx_release_suffix() {
  local platform="$1"
  local architecture="$2"
  case "$platform:$architecture" in
    darwin:aarch64|darwin:x86_64)
      printf -- '-%s-apple-darwin.tar.gz\n' "$architecture"
      ;;
    systemd:aarch64|systemd:x86_64)
      printf -- '-%s-unknown-linux-gnu.tar.gz\n' "$architecture"
      ;;
    *)
      workflow_die \
        "unsupported LeanCTX platform: $platform/$architecture"
      return 1
      ;;
  esac
}

provision_leanctx_embeddings() {
  local leanctx_binary="$1"
  local data_root="$2"
  local temporary_parent="$3"
  local managed_root config_dir state_dir
  [[ "$leanctx_binary" == /* && -x "$leanctx_binary" ]] || {
    workflow_die "LeanCTX embedding provisioning requires an executable binary"
    return 1
  }
  [[ "$data_root" == /* && "$data_root" != / ]] || {
    workflow_die "LeanCTX embedding provisioning requires an absolute data root"
    return 1
  }
  [[ "$temporary_parent" == /* && -d "$temporary_parent" ]] || {
    workflow_die "LeanCTX embedding provisioning requires a temporary parent"
    return 1
  }
  managed_root="$data_root/leanctx"
  config_dir="$(mktemp -d \
    "$temporary_parent/leanctx-embeddings-config.XXXXXX")" || return 1
  state_dir="$(mktemp -d \
    "$temporary_parent/leanctx-embeddings-state.XXXXXX")" || return 1
  chmod 0700 "$config_dir" "$state_dir"
  install -d -m 0700 \
    "$managed_root/cache" "$managed_root/lean-ctx"
  LEAN_CTX_CACHE_DIR="$managed_root/cache" \
  LEAN_CTX_CONFIG_DIR="$config_dir" \
  LEAN_CTX_DATA_DIR="$managed_root/lean-ctx" \
  LEAN_CTX_RULES_INJECTION=off \
  LEAN_CTX_STATE_DIR="$state_dir" \
  XDG_DATA_HOME="$managed_root" \
    "$leanctx_binary" embeddings provision || {
      workflow_die "LeanCTX ONNX Runtime provisioning command failed"
      return 1
    }
}

verified_leanctx_ort_dylib_path() {
  local leanctx_binary="$1"
  local data_root="$2"
  local temporary_parent="$3"
  local managed_root managed_data_root config_dir state_dir
  local status runtime_path managed_data_real runtime_real runtime_mode
  [[ "$leanctx_binary" == /* && -x "$leanctx_binary" ]] || {
    workflow_die "LeanCTX runtime verification requires an executable binary"
    return 1
  }
  [[ "$data_root" == /* && "$data_root" != / ]] || {
    workflow_die "LeanCTX runtime verification requires an absolute data root"
    return 1
  }
  [[ "$temporary_parent" == /* && -d "$temporary_parent" ]] || {
    workflow_die "LeanCTX runtime verification requires a temporary parent"
    return 1
  }
  managed_root="$data_root/leanctx"
  managed_data_root="$managed_root/lean-ctx"
  config_dir="$(mktemp -d \
    "$temporary_parent/leanctx-status-config.XXXXXX")" || return 1
  state_dir="$(mktemp -d \
    "$temporary_parent/leanctx-status-state.XXXXXX")" || return 1
  chmod 0700 "$config_dir" "$state_dir"
  install -d -m 0700 "$managed_root/cache" "$managed_data_root"
  status="$(
    LEAN_CTX_CACHE_DIR="$managed_root/cache" \
    LEAN_CTX_CONFIG_DIR="$config_dir" \
    LEAN_CTX_DATA_DIR="$managed_data_root" \
    LEAN_CTX_RULES_INJECTION=off \
    LEAN_CTX_STATE_DIR="$state_dir" \
    XDG_DATA_HOME="$managed_root" \
      "$leanctx_binary" embeddings status
  )" || {
    workflow_die "LeanCTX ONNX Runtime status command failed"
    return 1
  }
  runtime_path="$(
    sed -n \
      's/^managed ONNX Runtime [^:][^:]*: \(\/.*\)$/\1/p' \
      <<<"$status" | head -1
  )"
  [[ "$runtime_path" == /* && -f "$runtime_path" && \
     ! -L "$runtime_path" ]] || {
    workflow_die "LeanCTX managed ONNX Runtime is missing or unsafe"
    return 1
  }
  case "$runtime_path" in
    "$managed_data_root"/*) ;;
    *)
      workflow_die "LeanCTX managed ONNX Runtime escaped its data root"
      return 1
      ;;
  esac
  managed_data_real="$(workflow_physical_path "$managed_data_root")" || \
    return 1
  runtime_real="$(workflow_physical_path "$runtime_path")" || return 1
  case "$runtime_real" in
    "$managed_data_real"/*) ;;
    *)
      workflow_die "LeanCTX managed ONNX Runtime escaped its data root"
      return 1
      ;;
  esac
  runtime_mode="$(path_mode "$runtime_path")" || return 1
  [[ "$(path_uid "$runtime_path")" == "$(id -u)" ]] && \
    (( (8#$runtime_mode & 0022) == 0 )) || {
      workflow_die "LeanCTX managed ONNX Runtime is not private"
      return 1
    }
  printf '%s\n' "$runtime_path"
}

probe_leanctx_capabilities() {
  local leanctx_binary="$1"
  local python_runtime="$2"
  local workflow_root="$3"
  local temporary_parent="$4"
  local ort_dylib_path="$5"
  local shared_cache_dir="$6"
  local probe_root project_root config_dir state_dir shared_dir
  [[ "$leanctx_binary" == /* && -x "$leanctx_binary" ]] || {
    workflow_die "LeanCTX capability probe requires an executable binary"
    return 1
  }
  [[ "$python_runtime" == /* && -x "$python_runtime" ]] || {
    workflow_die "LeanCTX capability probe requires a Python runtime"
    return 1
  }
  [[ -f "$workflow_root/integrations/common/mcp_probe.py" ]] || {
    workflow_die "LeanCTX MCP probe is unavailable"
    return 1
  }
  [[ "$ort_dylib_path" == /* && -f "$ort_dylib_path" && \
     ! -L "$ort_dylib_path" ]] || {
    workflow_die "LeanCTX capability probe requires an ONNX Runtime"
    return 1
  }
  [[ "$shared_cache_dir" == /* && "$shared_cache_dir" != / ]] || {
    workflow_die "LeanCTX capability probe requires a shared cache"
    return 1
  }
  probe_root="$(mktemp -d "$temporary_parent/leanctx-capability.XXXXXX")" || \
    return 1
  chmod 0700 "$probe_root"
  project_root="$probe_root/project"
  config_dir="$probe_root/config"
  state_dir="$probe_root/state"
  shared_dir="$probe_root/shared"
  install -d -m 0700 \
    "$project_root" "$config_dir" "$state_dir" \
    "$shared_cache_dir" \
    "$shared_dir" "$shared_dir/lean-ctx"
  PYTHONDONTWRITEBYTECODE=1 "$python_runtime" -I -B - \
    "$workflow_root" "$config_dir/config.toml" "$project_root/probe.py" <<'PY'
import os
from pathlib import Path
import sys

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.leanctx_contract import config_bytes

target = Path(sys.argv[2])
descriptor = os.open(
    target,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
)
try:
    payload = config_bytes()
    offset = 0
    while offset < len(payload):
        offset += os.write(descriptor, payload[offset:])
    os.fsync(descriptor)
finally:
    os.close(descriptor)

source = Path(sys.argv[3])
descriptor = os.open(
    source,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
)
try:
    payload = (
        b"def orichum_probe_target():\n"
        b"    return 1\n\n"
        b"def orichum_probe_caller():\n"
        b"    return orichum_probe_target()\n"
    )
    offset = 0
    while offset < len(payload):
        offset += os.write(descriptor, payload[offset:])
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
  PYTHONDONTWRITEBYTECODE=1 "$python_runtime" -B \
    "$workflow_root/integrations/common/mcp_probe.py" \
    --cwd "$project_root" \
    --exact-tool ctx_read \
    --exact-tool ctx_search \
    --exact-tool ctx_tree \
    --exact-tool ctx_expand \
    --exact-tool ctx_graph \
    --exact-tool ctx_impact \
    --exact-tool ctx_callgraph \
    --exact-tool ctx_knowledge \
    --exact-tool ctx_overview \
    --exact-tool ctx_patch \
    --exact-tool ctx_shell \
    --probe-call \
      '{"name":"ctx_graph","arguments":{"action":"build"},"contains":"Project Graph: 1 files"}' \
    --probe-call \
      '{"name":"ctx_graph","arguments":{"action":"symbol","path":"probe.py::orichum_probe_target"},"contains":"probe.py::orichum_probe_target"}' \
    --probe-call \
      '{"name":"ctx_impact","arguments":{"action":"analyze","path":"probe.py"},"contains":"[ctx_impact:"}' \
    --probe-call \
      '{"name":"ctx_overview","arguments":{"path":".","task":"Verify Orichum readiness"}}' \
    --probe-call \
      '{"name":"ctx_search","arguments":{"action":"reindex","path":"."},"contains":"Reindexed"}' \
    --probe-call \
      '{"name":"ctx_search","arguments":{"action":"semantic","mode":"dense","query":"function that returns the Orichum probe value","path":"."},"contains":"orichum_probe_target","retry_contains":"BM25 index is being built in the background","attempts":6,"retry_delay":1}' \
    --probe-call \
      '{"name":"ctx_shell","arguments":{"command":"printf orichum-shell-ready","raw":true},"contains":"orichum-shell-ready"}' \
    -- env \
    LEAN_CTX_ALLOW_REROOT=false \
    LEAN_CTX_AUTONOMY=false \
    LEAN_CTX_BYPASS_HINTS=off \
    LEAN_CTX_CACHE_DIR="$shared_cache_dir" \
    LEAN_CTX_CONFIG_DIR="$config_dir" \
    LEAN_CTX_DATA_DIR="$shared_dir/lean-ctx" \
    LEAN_CTX_FULL_TOOLS=0 \
    LEAN_CTX_HEADLESS=1 \
    LEAN_CTX_MINIMAL=1 \
    LEAN_CTX_PROJECT_ROOT="$project_root" \
    LEAN_CTX_RULES_INJECTION=off \
    LEAN_CTX_STATE_DIR="$state_dir" \
    ORT_DYLIB_PATH="$ort_dylib_path" \
    XDG_DATA_HOME="$shared_dir" \
    "$leanctx_binary"
}

pinned_release_allows_recorded_version() {
  local recorded_version="$1"
  local requested_tag="$2"
  local requested_version
  [[ "$recorded_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && \
     "$requested_tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 2
  requested_version="${requested_tag#v}"
  jq -en \
    --arg recorded "$recorded_version" \
    --arg requested "$requested_version" '
      def parts: split(".") | map(tonumber);
      ($recorded | parts) <= ($requested | parts)
    ' >/dev/null
}

stage_github_binary() {
  local repository="$1"
  local prefix="$2"
  local suffix="$3"
  local archive_binary="$4"
  local destination="$5"
  local staging_dir="$6"
  local resolve_upstream="${7:-true}"
  local recorded_version="${8:-}"
  local source_identity="${9:-}"
  local expected_artifact_sha="${10:-}"
  local requested_tag="${11:-}"
  local metadata archive row url digest asset tag version actual_sha staged_binary
  local expected_source_prefix expected_tag=

  if [[ -n "$requested_tag" && \
        ! "$requested_tag" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
    workflow_die "requested GitHub release tag is unsafe"
    return 1
  fi

  if [[ "$resolve_upstream" == false ]]; then
    [[ "$recorded_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
      workflow_die "recorded GitHub binary version is invalid"
      return 1
    }
    [[ "$expected_artifact_sha" =~ ^[a-f0-9]{64}$ ]] || {
      workflow_die "recorded GitHub artifact hash is invalid"
      return 1
    }
    expected_source_prefix="github:$repository@"
    case "$source_identity" in
      "$expected_source_prefix"*)
        expected_tag="${source_identity#"$expected_source_prefix"}"
        ;;
      *)
        workflow_die "recorded GitHub source identity is invalid"
        return 1
        ;;
    esac
    [[ "$expected_tag" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || {
      workflow_die "recorded GitHub release tag is unsafe"
      return 1
    }
    if managed_executable_is_safe "$destination" && \
       binary_reports_semver "$destination" "$recorded_version" && \
       [[ "$(sha256_file "$destination")" == "$expected_artifact_sha" ]]; then
      jq -cn --arg version "$recorded_version" --arg tag "$expected_tag" \
        '{version: $version, tag: $tag, changed: false, staged_path: null}'
      return 0
    fi
  fi
  install -d -m 0700 "$staging_dir"
  metadata="$staging_dir/release.json"
  archive="$staging_dir/release.tar.gz"
  staged_binary="$staging_dir/$archive_binary"
  if [[ "$resolve_upstream" == false ]]; then
    fetch_github_release_tag "$repository" "$expected_tag" "$metadata"
  elif [[ -n "$requested_tag" ]]; then
    fetch_github_release_tag "$repository" "$requested_tag" "$metadata"
  else
    fetch_latest_github_release "$repository" "$metadata"
  fi
  row="$(jq -er --arg prefix "$prefix" --arg suffix "$suffix" '
    [.assets[] | select(.name | startswith($prefix) and endswith($suffix))] |
    if length == 1 then .[0] else error("expected exactly one release asset") end |
    [.browser_download_url, .digest, .name] | @tsv
  ' "$metadata")"
  IFS=$'\t' read -r url digest asset <<<"$row"
  tag="$(jq -er '.tag_name' "$metadata")"
  version="$(jq -er '.tag_name | sub("^v"; "")' "$metadata")"
  [[ "$tag" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || \
    workflow_die "GitHub release tag is unsafe"
  if [[ "$resolve_upstream" == true && \
        -n "$requested_tag" && "$tag" != "$requested_tag" ]]; then
    workflow_die "requested GitHub release identity did not match"
    return 1
  fi
  if [[ "$resolve_upstream" == false && \
        ( "$tag" != "$expected_tag" || "$version" != "$recorded_version" ) ]]; then
    workflow_die "recorded GitHub release identity did not match"
    return 1
  fi
  if [[ "$digest" != sha256:* ]]; then
    workflow_die "GitHub did not publish a SHA-256 digest for $asset"
    return 1
  fi

  if managed_executable_is_safe "$destination" && \
     binary_reports_semver "$destination" "$version" && \
     {
       { [[ "$resolve_upstream" == true && -z "$requested_tag" ]]; } || \
         { [[ "$resolve_upstream" == false ]] && \
           [[ "$(sha256_file "$destination")" == "$expected_artifact_sha" ]]; }
     }; then
    jq -cn --arg version "$version" --arg tag "$tag" \
      '{version: $version, tag: $tag, changed: false, staged_path: null}'
    return 0
  fi

  curl --fail --location --silent --show-error "$url" --output "$archive"
  actual_sha="$(sha256_file "$archive")"
  if [[ "$actual_sha" != "${digest#sha256:}" ]]; then
    workflow_die "checksum mismatch for $asset"
    return 1
  fi
  tar -xzf "$archive" -C "$staging_dir" "$archive_binary"
  chmod 0755 "$staged_binary"
  if ! binary_reports_semver "$staged_binary" "$version"; then
    workflow_die "staged $asset did not report version $version"
    return 1
  fi
  if [[ "$resolve_upstream" == false && \
        "$(sha256_file "$staged_binary")" != "$expected_artifact_sha" ]]; then
    workflow_die "recorded GitHub binary artifact did not match"
    return 1
  fi
  jq -cn --arg version "$version" --arg tag "$tag" \
    --arg staged_path "$staged_binary" \
    '{version: $version, tag: $tag, changed: true, staged_path: $staged_path}'
}

activate_staged_file() {
  local staged_path="$1"
  local destination="$2"
  local mode="$3"
  install -m "$mode" "$staged_path" "$destination"
}

activate_private_file_atomic() {
  local staged_path="$1"
  local destination="$2"
  local mode="$3"
  workflow_python - "$staged_path" "$destination" "$mode" <<'PY'
import os
from pathlib import Path
import secrets
import stat
import sys

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
mode = int(sys.argv[3], 8)
parent = destination.parent
parent_stat = os.lstat(parent)
if (
    stat.S_ISLNK(parent_stat.st_mode)
    or not stat.S_ISDIR(parent_stat.st_mode)
    or parent_stat.st_uid != os.getuid()
    or stat.S_IMODE(parent_stat.st_mode) != 0o700
):
    raise SystemExit("private destination directory is unsafe")
source_stat = os.lstat(source)
if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
    raise SystemExit("staged private file is unsafe")
try:
    destination_stat = os.lstat(destination)
except FileNotFoundError:
    destination_stat = None
if destination_stat is not None and not (
    stat.S_ISREG(destination_stat.st_mode)
    or stat.S_ISLNK(destination_stat.st_mode)
):
    raise SystemExit("private destination is neither regular, symlink, nor absent")

directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
no_follow = getattr(os, "O_NOFOLLOW", 0)
directory_fd = os.open(parent, directory_flags | no_follow)
source_fd = None
temporary_fd = None
temporary_name = f".{destination.name}.{secrets.token_hex(12)}"
replaced = False
try:
    directory_stat = os.fstat(directory_fd)
    if (directory_stat.st_dev, directory_stat.st_ino) != (
        parent_stat.st_dev,
        parent_stat.st_ino,
    ):
        raise OSError("private destination directory changed")
    source_fd = os.open(source, os.O_RDONLY | no_follow)
    if (os.fstat(source_fd).st_dev, os.fstat(source_fd).st_ino) != (
        source_stat.st_dev,
        source_stat.st_ino,
    ):
        raise OSError("staged private file changed")
    temporary_fd = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow,
        mode,
        dir_fd=directory_fd,
    )
    os.fchmod(temporary_fd, mode)
    while True:
        block = os.read(source_fd, 65536)
        if not block:
            break
        offset = 0
        while offset < len(block):
            offset += os.write(temporary_fd, block[offset:])
    os.fsync(temporary_fd)
    temporary_stat = os.fstat(temporary_fd)
    os.close(temporary_fd)
    temporary_fd = None
    os.replace(
        temporary_name,
        destination.name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )
    replaced = True
    os.fsync(directory_fd)
    final_stat = os.stat(
        destination.name,
        dir_fd=directory_fd,
        follow_symlinks=False,
    )
    parent_after = os.lstat(parent)
    if (
        not stat.S_ISREG(final_stat.st_mode)
        or final_stat.st_uid != os.getuid()
        or stat.S_IMODE(final_stat.st_mode) != mode
        or (final_stat.st_dev, final_stat.st_ino)
        != (temporary_stat.st_dev, temporary_stat.st_ino)
        or (parent_after.st_dev, parent_after.st_ino)
        != (directory_stat.st_dev, directory_stat.st_ino)
        or parent_after.st_uid != os.getuid()
        or stat.S_IMODE(parent_after.st_mode) != 0o700
    ):
        raise OSError("private destination validation failed")
finally:
    if source_fd is not None:
        os.close(source_fd)
    if temporary_fd is not None:
        os.close(temporary_fd)
    if not replaced:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
    os.close(directory_fd)
PY
}

backup_path() {
  local source_path="$1"
  local backup_dir="$2"
  local backup_name="$3"
  if [[ -e "$source_path" || -L "$source_path" ]]; then
    cp -pPR "$source_path" "$backup_dir/$backup_name"
  fi
}

render_launch_agent() {
  local output_file="$1"
  local data_root="$2"
  local escaped_binary escaped_config escaped_log escaped_home
  escaped_binary="$(xml_escape "$data_root/bin/cli-proxy-api")"
  escaped_config="$(xml_escape "$data_root/cliproxy.yaml")"
  escaped_log="$(xml_escape "$data_root/logs/cliproxy.log")"
  escaped_home="$(xml_escape "$HOME")"
  printf '%s\n' \
    '<?xml version="1.0" encoding="UTF-8"?>' \
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
    '<plist version="1.0">' \
    '<dict>' \
    '  <key>Label</key>' \
    '  <string>io.orichum.cliproxy</string>' \
    '  <key>ProgramArguments</key>' \
    '  <array>' \
    "    <string>$escaped_binary</string>" \
    '    <string>--config</string>' \
    "    <string>$escaped_config</string>" \
    '  </array>' \
    '  <key>RunAtLoad</key>' \
    '  <true/>' \
    '  <key>KeepAlive</key>' \
    '  <true/>' \
    '  <key>ProcessType</key>' \
    '  <string>Background</string>' \
    '  <key>Umask</key>' \
    '  <integer>63</integer>' \
    '  <key>StandardOutPath</key>' \
    "  <string>$escaped_log</string>" \
    '  <key>StandardErrorPath</key>' \
    "  <string>$escaped_log</string>" \
    '  <key>EnvironmentVariables</key>' \
    '  <dict>' \
    '    <key>HOME</key>' \
    "    <string>$escaped_home</string>" \
    '  </dict>' \
    '</dict>' \
    '</plist>' >"$output_file"
}

systemd_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//\%/%%}"
  value="${value//\$/\$\$}"
  printf '"%s"' "$value"
}

systemd_environment_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//\%/%%}"
  printf '"%s"' "$value"
}

xml_escape() {
  printf '%s' "$1" | sed \
    -e 's/&/\&amp;/g' \
    -e 's/</\&lt;/g' \
    -e 's/>/\&gt;/g' \
    -e 's/"/\&quot;/g' \
    -e "s/'/\\\&apos;/g"
}

render_systemd_user_unit() {
  local output_file="$1"
  local data_root="$2"
  local executable config
  executable="$(systemd_quote "$data_root/bin/cli-proxy-api")"
  config="$(systemd_quote "$data_root/cliproxy.yaml")"
  printf '%s\n' \
    '[Unit]' \
    'Description=Orichum CLIProxyAPI' \
    'StartLimitIntervalSec=60' \
    'StartLimitBurst=3' \
    '' \
    '[Service]' \
    'Type=exec' \
    "ExecStart=$executable --config $config" \
    'Restart=on-failure' \
    'RestartSec=5' \
    'StandardOutput=journal' \
    'StandardError=journal' \
    '' \
    '[Install]' \
    'WantedBy=default.target' >"$output_file"
}

render_leanctx_proxy_launch_agent() {
  local output_file="$1"
  local data_root="$2"
  local port="${3:-13458}"
  local escaped_binary escaped_port escaped_log escaped_home
  local escaped_config escaped_state escaped_cache escaped_data
  valid_service_port "$port" || return 1
  escaped_binary="$(xml_escape "$data_root/bin/lean-ctx")"
  escaped_port="$(xml_escape "--port=$port")"
  escaped_log="$(xml_escape "$data_root/logs/leanctx-proxy.log")"
  escaped_home="$(xml_escape "$HOME")"
  escaped_config="$(xml_escape "$data_root/leanctx/proxy/config")"
  escaped_state="$(xml_escape "$data_root/leanctx/proxy/state")"
  escaped_cache="$(xml_escape "$data_root/leanctx/proxy/cache")"
  escaped_data="$(xml_escape "$data_root/leanctx")"
  printf '%s\n' \
    '<?xml version="1.0" encoding="UTF-8"?>' \
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
    '<plist version="1.0">' \
    '<dict>' \
    '  <key>Label</key>' \
    '  <string>io.orichum.leanctx-proxy</string>' \
    '  <key>ProgramArguments</key>' \
    '  <array>' \
    "    <string>$escaped_binary</string>" \
    '    <string>proxy</string>' \
    '    <string>start</string>' \
    "    <string>$escaped_port</string>" \
    '  </array>' \
    '  <key>RunAtLoad</key>' \
    '  <true/>' \
    '  <key>KeepAlive</key>' \
    '  <true/>' \
    '  <key>ProcessType</key>' \
    '  <string>Background</string>' \
    '  <key>Umask</key>' \
    '  <integer>63</integer>' \
    '  <key>StandardOutPath</key>' \
    "  <string>$escaped_log</string>" \
    '  <key>StandardErrorPath</key>' \
    "  <string>$escaped_log</string>" \
    '  <key>EnvironmentVariables</key>' \
    '  <dict>' \
    '    <key>HOME</key>' \
    "    <string>$escaped_home</string>" \
    '    <key>LEAN_CTX_CACHE_DIR</key>' \
    "    <string>$escaped_cache</string>" \
    '    <key>LEAN_CTX_CONFIG_DIR</key>' \
    "    <string>$escaped_config</string>" \
    '    <key>LEAN_CTX_DATA_DIR</key>' \
    "    <string>$escaped_data/lean-ctx</string>" \
    '    <key>LEAN_CTX_HEADLESS</key>' \
    '    <string>1</string>' \
    '    <key>LEAN_CTX_MINIMAL</key>' \
    '    <string>1</string>' \
    '    <key>LEAN_CTX_RULES_INJECTION</key>' \
    '    <string>off</string>' \
    '    <key>LEAN_CTX_STATE_DIR</key>' \
    "    <string>$escaped_state</string>" \
    '    <key>XDG_DATA_HOME</key>' \
    "    <string>$escaped_data</string>" \
    '  </dict>' \
    '</dict>' \
    '</plist>' >"$output_file"
}

render_leanctx_proxy_systemd_user_unit() {
  local output_file="$1"
  local data_root="$2"
  local port="${3:-13458}"
  local executable
  valid_service_port "$port" || return 1
  executable="$(systemd_quote "$data_root/bin/lean-ctx")"
  printf '%s\n' \
    '[Unit]' \
    'Description=Orichum LeanCTX proxy' \
    'Wants=orichum-cliproxy.service' \
    'After=orichum-cliproxy.service' \
    '' \
    '[Service]' \
    'Type=exec' \
    "ExecStart=$executable proxy start --port=$port" \
    'Restart=always' \
    'RestartSec=3' \
    "Environment=$(systemd_environment_quote "HOME=$HOME")" \
    "Environment=$(systemd_environment_quote "LEAN_CTX_CACHE_DIR=$data_root/leanctx/proxy/cache")" \
    "Environment=$(systemd_environment_quote "LEAN_CTX_CONFIG_DIR=$data_root/leanctx/proxy/config")" \
    "Environment=$(systemd_environment_quote "LEAN_CTX_DATA_DIR=$data_root/leanctx/lean-ctx")" \
    'Environment="LEAN_CTX_HEADLESS=1"' \
    'Environment="LEAN_CTX_MINIMAL=1"' \
    'Environment="LEAN_CTX_RULES_INJECTION=off"' \
    "Environment=$(systemd_environment_quote "LEAN_CTX_STATE_DIR=$data_root/leanctx/proxy/state")" \
    "Environment=$(systemd_environment_quote "XDG_DATA_HOME=$data_root/leanctx")" \
    'StandardOutput=journal' \
    'StandardError=journal' \
    '' \
    '[Install]' \
    'WantedBy=default.target' >"$output_file"
}

render_claudex_proxy_launch_agent() {
  local output_file="$1"
  local data_root="$2"
  local workflow_root="$3"
  local port="${4:-13456}"
  local upstream_port="${5:-8317}"
  local catalog_port="${6:-8317}"
  local runtime_digest="${7:-}"
  local route_runner
  local escaped_binary escaped_runner escaped_state escaped_data
  local escaped_log escaped_home escaped_workflow
  valid_service_port "$port" || return 1
  valid_service_port "$upstream_port" || return 1
  valid_service_port "$catalog_port" || return 1
  [[ "$port" != "$upstream_port" && \
     "$port" != "$catalog_port" && \
     "$upstream_port" != "$catalog_port" ]] || return 1
  [[ "$runtime_digest" =~ ^[a-f0-9]{64}$ ]] || return 1
  [[ "$workflow_root" == /* && -d "$workflow_root" ]] || return 1
  route_runner='import os,sys; sys.path.insert(0, os.environ["ORICHUM_WORKFLOW_ROOT"]); from integrations.common.route_proxy import main; raise SystemExit(main())'
  escaped_binary="$(xml_escape "$data_root/bin/orichum-python")"
  escaped_runner="$(xml_escape "$route_runner")"
  escaped_state="$(xml_escape "$data_root/state")"
  escaped_data="$(xml_escape "$data_root")"
  escaped_log="$(xml_escape "$data_root/logs/route-proxy.log")"
  escaped_home="$(xml_escape "$HOME")"
  escaped_workflow="$(xml_escape "$workflow_root")"
  printf '%s\n' \
    '<?xml version="1.0" encoding="UTF-8"?>' \
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
    "<!-- Orichum route runtime SHA-256: $runtime_digest -->" \
    '<plist version="1.0">' \
    '<dict>' \
    '  <key>Label</key>' \
    '  <string>io.orichum.route-proxy</string>' \
    '  <key>ProgramArguments</key>' \
    '  <array>' \
    "    <string>$escaped_binary</string>" \
    '    <string>-I</string>' \
    '    <string>-B</string>' \
    '    <string>-c</string>' \
    "    <string>$escaped_runner</string>" \
    '    <string>--port</string>' \
    "    <string>$port</string>" \
    '    <string>--upstream-port</string>' \
    "    <string>$upstream_port</string>" \
    '    <string>--catalog-port</string>' \
    "    <string>$catalog_port</string>" \
    '    <string>--state-home</string>' \
    "    <string>$escaped_state</string>" \
    '    <string>--data-home</string>' \
    "    <string>$escaped_data</string>" \
    '  </array>' \
    '  <key>RunAtLoad</key>' \
    '  <true/>' \
    '  <key>KeepAlive</key>' \
    '  <true/>' \
    '  <key>ProcessType</key>' \
    '  <string>Background</string>' \
    '  <key>Umask</key>' \
    '  <integer>63</integer>' \
    '  <key>StandardOutPath</key>' \
    "  <string>$escaped_log</string>" \
    '  <key>StandardErrorPath</key>' \
    "  <string>$escaped_log</string>" \
    '  <key>EnvironmentVariables</key>' \
    '  <dict>' \
    '    <key>HOME</key>' \
    "    <string>$escaped_home</string>" \
    '    <key>ORICHUM_WORKFLOW_ROOT</key>' \
    "    <string>$escaped_workflow</string>" \
    '    <key>ORICHUM_PYTHON</key>' \
    "    <string>$escaped_binary</string>" \
    '    <key>ORICHUM_DATA_HOME</key>' \
    "    <string>$escaped_data</string>" \
    '  </dict>' \
    '</dict>' \
    '</plist>' >"$output_file"
}

render_claudex_proxy_systemd_user_unit() {
  local output_file="$1"
  local data_root="$2"
  local workflow_root="$3"
  local port="${4:-13456}"
  local upstream_port="${5:-8317}"
  local catalog_port="${6:-8317}"
  local runtime_digest="${7:-}"
  local route_runner executable runner state data home_environment
  local workflow_environment python_environment data_environment
  valid_service_port "$port" || return 1
  valid_service_port "$upstream_port" || return 1
  valid_service_port "$catalog_port" || return 1
  [[ "$port" != "$upstream_port" && \
     "$port" != "$catalog_port" && \
     "$upstream_port" != "$catalog_port" ]] || return 1
  [[ "$runtime_digest" =~ ^[a-f0-9]{64}$ ]] || return 1
  [[ "$workflow_root" == /* && -d "$workflow_root" ]] || return 1
  route_runner='import os,sys; sys.path.insert(0, os.environ["ORICHUM_WORKFLOW_ROOT"]); from integrations.common.route_proxy import main; raise SystemExit(main())'
  executable="$(systemd_quote "$data_root/bin/orichum-python")"
  runner="$(systemd_quote "$route_runner")"
  state="$(systemd_quote "$data_root/state")"
  data="$(systemd_quote "$data_root")"
  home_environment="$(systemd_environment_quote "HOME=$HOME")"
  workflow_environment="$(
    systemd_environment_quote "ORICHUM_WORKFLOW_ROOT=$workflow_root"
  )"
  python_environment="$(
    systemd_environment_quote \
      "ORICHUM_PYTHON=$data_root/bin/orichum-python"
  )"
  data_environment="$(
    systemd_environment_quote "ORICHUM_DATA_HOME=$data_root"
  )"
  printf '%s\n' \
    "# Orichum route runtime SHA-256: $runtime_digest" \
    '[Unit]' \
    'Description=Orichum same-family recovery proxy' \
    'Wants=orichum-leanctx-proxy.service' \
    'After=orichum-leanctx-proxy.service' \
    '' \
    '[Service]' \
    'Type=exec' \
    "ExecStart=$executable -I -B -c $runner --port $port --upstream-port $upstream_port --catalog-port $catalog_port --state-home $state --data-home $data" \
    'Restart=always' \
    'RestartSec=3' \
    "Environment=$home_environment" \
    "Environment=$workflow_environment" \
    "Environment=$python_environment" \
    "Environment=$data_environment" \
    'StandardOutput=journal' \
    'StandardError=journal' \
    '' \
    '[Install]' \
    'WantedBy=default.target' >"$output_file"
}

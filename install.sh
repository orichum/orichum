#!/usr/bin/env bash
set -euo pipefail

WORKFLOW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$WORKFLOW_ROOT"
# shellcheck source=lib/workflow.sh
source "$WORKFLOW_ROOT/lib/workflow.sh"
export ORICHUM_INSTALL_BOOTSTRAP=true

install_usage() {
  printf 'Usage: ./install.sh [--verbose] [--upgrade | --uninstall [--purge]]\n' >&2
}

install_arguments="$(parse_install_arguments "$@")" || {
  install_usage
  exit 2
}
IFS=$'\t' read -r INSTALL_MODE INSTALL_VERBOSE <<<"$install_arguments"
INSTALL_OUTPUT_ACTIVE=false
INSTALL_LOG_PATH=
ORICHUM_COMPLETION_OPTIONAL_SHELL=

install_cleanup() {
  local status="${1:-0}"
  if [[ "$INSTALL_OUTPUT_ACTIVE" == true && "$status" -ne 0 ]]; then
    print_install_failure "$INSTALL_LOG_PATH" >&4
  fi
  workflow_cleanup "$status"
}

workflow_cleanup_init
trap 'install_cleanup "$?"' EXIT
trap 'install_cleanup 129' HUP
trap 'install_cleanup 130' INT
trap 'install_cleanup 143' TERM
lifecycle_lock_path="$(orichum_lifecycle_lock_path)" || \
  workflow_die "refusing unsafe Orichum lifecycle lock"
acquire_workflow_lock "$lifecycle_lock_path"

case "$INSTALL_MODE" in
  fast|upgrade) ;;
  uninstall)
    # shellcheck source=lib/uninstall.sh
    source "$WORKFLOW_ROOT/lib/uninstall.sh"
    orichum_uninstall false
    exit
    ;;
  purge)
    # shellcheck source=lib/uninstall.sh
    source "$WORKFLOW_ROOT/lib/uninstall.sh"
    orichum_uninstall true
    exit
    ;;
esac

# BEGIN installed control-plane transaction
stage_installed_control_plane() {
  local python_runtime="$1"
  local workflow_root="$2"
  local installed_root="$3"
  local candidate_root="$4"
  (
    cd "$workflow_root"
    PYTHONDONTWRITEBYTECODE=1 "$python_runtime" -I -B - \
      "$workflow_root" "$installed_root" "$candidate_root" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.install_control_plane import stage

stage(root, Path(sys.argv[2]), Path(sys.argv[3]))
PY
  )
}

activate_installed_control_plane() {
  local python_runtime="$1"
  local workflow_root="$2"
  local candidate_root="$3"
  local installed_root="$4"
  local snapshot_root="$5"
  local install_lock_path="$6"
  local install_lock_fd="$7"
  (
    cd "$workflow_root"
    PYTHONDONTWRITEBYTECODE=1 "$python_runtime" -I -B - \
      "$workflow_root" "$candidate_root" "$installed_root" \
      "$snapshot_root" "$install_lock_path" "$install_lock_fd" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.install_control_plane import activate

activate(
    Path(sys.argv[2]),
    Path(sys.argv[3]),
    Path(sys.argv[4]),
    Path(sys.argv[5]),
    int(sys.argv[6]),
)
PY
  )
}

rollback_installed_control_plane() {
  local python_runtime="$1"
  local workflow_root="$2"
  local installed_root="$3"
  local snapshot_root="$4"
  local install_lock_path="$5"
  local install_lock_fd="$6"
  (
    cd "$workflow_root"
    PYTHONDONTWRITEBYTECODE=1 "$python_runtime" -I -B - \
      "$workflow_root" "$installed_root" "$snapshot_root" \
      "$install_lock_path" "$install_lock_fd" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.install_control_plane import rollback

rollback(
    Path(sys.argv[2]),
    Path(sys.argv[3]),
    Path(sys.argv[4]),
    int(sys.argv[5]),
)
PY
  )
}

recover_installed_control_plane() {
  local python_runtime="$1"
  local workflow_root="$2"
  local installed_root="$3"
  local journal_root="$4"
  local install_lock_path="$5"
  local install_lock_fd="$6"
  (
    cd "$workflow_root"
    PYTHONDONTWRITEBYTECODE=1 "$python_runtime" -I -B - \
      "$workflow_root" "$installed_root" "$journal_root" \
      "$install_lock_path" "$install_lock_fd" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.install_control_plane import recover

recover(
    Path(sys.argv[2]),
    Path(sys.argv[3]),
    Path(sys.argv[4]),
    int(sys.argv[5]),
)
PY
  )
}

finalize_installed_control_plane() {
  local python_runtime="$1"
  local workflow_root="$2"
  local journal_root="$3"
  local install_lock_path="$4"
  local install_lock_fd="$5"
  (
    cd "$workflow_root"
    PYTHONDONTWRITEBYTECODE=1 "$python_runtime" -I -B - \
      "$workflow_root" "$journal_root" "$install_lock_path" \
      "$install_lock_fd" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.install_control_plane import finalize

finalize(Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4]))
PY
  )
}

verify_committed_control_plane() {
  local installed_root="$1"
  local data_root="$2"
  ORICHUM_CONFIG_HOME="$installed_root" \
  ORICHUM_DATA_HOME="$data_root" \
    "$WORKFLOW_ROOT/bin/orichum" config validate >/dev/null
}
# END installed control-plane transaction

# BEGIN consolidated home and runtime transaction
prepare_orichum_home() {
  local source_root="$1"
  local home_root="$2"
  local journal_root="$3"
  (
    cd "$source_root"
    PYTHONDONTWRITEBYTECODE=1 python3 -I -B - \
      "$source_root" "$home_root" \
      "${XDG_DATA_HOME:-$HOME/.local/share}/orichum" \
      "${XDG_CONFIG_HOME:-$HOME/.config}/orichum" \
      "${XDG_CACHE_HOME:-$HOME/.cache}/orichum" \
      "$journal_root" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.home_layout import prepare

journal = prepare(
    Path(sys.argv[2]),
    Path(sys.argv[3]),
    Path(sys.argv[4]),
    Path(sys.argv[5]),
    Path(sys.argv[6]),
)
if journal is not None:
    print(journal, end="")
PY
  )
}

rollback_orichum_home() {
  local source_root="$1"
  local journal="$2"
  [[ -n "$journal" ]] || return 0
  (
    cd "$source_root"
    PYTHONDONTWRITEBYTECODE=1 python3 -I -B - \
      "$source_root" "$journal" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.home_layout import rollback

rollback(Path(sys.argv[2]))
PY
  )
}

commit_orichum_home() {
  local source_root="$1"
  local journal="$2"
  [[ -n "$journal" ]] || return 0
  (
    cd "$source_root"
    PYTHONDONTWRITEBYTECODE=1 python3 -I -B - \
      "$source_root" "$journal" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.home_layout import commit

commit(Path(sys.argv[2]))
PY
  )
}

stage_orichum_runtime() {
  local source_root="$1"
  local stage_root="$2"
  (
    cd "$source_root"
    PYTHONDONTWRITEBYTECODE=1 python3 -I -B - \
      "$source_root" "$stage_root" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.runtime_bundle import build

print(build(root, Path(sys.argv[2])), end="")
PY
  )
}

activate_orichum_runtime() {
  local source_root="$1"
  local staged_release="$2"
  local home_root="$3"
  (
    cd "$source_root"
    PYTHONDONTWRITEBYTECODE=1 python3 -I -B - \
      "$source_root" "$staged_release" "$home_root" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.runtime_bundle import activate

release, previous = activate(Path(sys.argv[2]), Path(sys.argv[3]))
print(f"{release}\t{previous or '-'}")
PY
  )
}

current_orichum_runtime() {
  local source_root="$1"
  local home_root="$2"
  (
    cd "$source_root"
    PYTHONDONTWRITEBYTECODE=1 python3 -I -B - \
      "$source_root" "$home_root" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.runtime_bundle import current_release

release = current_release(Path(sys.argv[2]))
print(release or "-", end="")
PY
  )
}

rollback_orichum_runtime() {
  local source_root="$1"
  local home_root="$2"
  local release="$3"
  local previous="$4"
  (
    cd "$source_root"
    PYTHONDONTWRITEBYTECODE=1 python3 -I -B - \
      "$source_root" "$home_root" "$release" "$previous" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.runtime_bundle import rollback_attempt

rollback_attempt(
    Path(sys.argv[2]),
    Path(sys.argv[3]),
    None if sys.argv[4] == "-" else Path(sys.argv[4]),
)
PY
  )
}

prune_orichum_runtime() {
  local source_root="$1"
  local home_root="$2"
  local release="$3"
  (
    cd "$source_root"
    PYTHONDONTWRITEBYTECODE=1 python3 -I -B - \
      "$source_root" "$home_root" "$release" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.runtime_bundle import prune

prune(Path(sys.argv[2]), (Path(sys.argv[3]),))
PY
  )
}
# END consolidated home and runtime transaction


USER_BIN_DIR="${USER_BIN_DIR:-$HOME/.local/bin}"
ORICHUM_HOME_ROOT="$(validated_orichum_home_dir "$SOURCE_ROOT")" || \
  workflow_die "refusing unsafe ORICHUM_HOME"
ORICHUM_HOME="$ORICHUM_HOME_ROOT"
export ORICHUM_HOME
home_migration_journal=
home_migration_active=false
home_migration_performed=false
if [[ -z "${ORICHUM_DATA_HOME:-}" && \
      -z "${ORICHUM_CONFIG_HOME:-}" && \
      -z "${ORICHUM_CACHE_HOME:-}" ]]; then
  home_migration_journal="$(
    prepare_orichum_home \
      "$SOURCE_ROOT" "$ORICHUM_HOME_ROOT" \
      "${lifecycle_lock_path%/install.lock}"
  )" || workflow_die "existing Orichum state could not be consolidated safely"
  if [[ -n "$home_migration_journal" ]]; then
    home_migration_active=true
    home_migration_performed=true
    printf 'Consolidating existing Orichum state into %s\n' \
      "$ORICHUM_HOME_ROOT"
  fi
fi
WORKFLOW_DATA_ROOT="$(validated_workflow_data_dir "$WORKFLOW_ROOT")" || \
  workflow_die "refusing unsafe ORICHUM_DATA_HOME"
ORICHUM_CONFIG_ROOT="$(workflow_config_dir)"
INSTALLED_CONFIG_ROOT="$ORICHUM_CONFIG_ROOT"
case "$ORICHUM_CONFIG_ROOT" in
  /*) ;;
  *) workflow_die "ORICHUM_CONFIG_HOME must be an absolute path" ;;
esac
INSTALL_LOG_PATH="$(
  create_install_diagnostic_log "$WORKFLOW_DATA_ROOT"
)" || workflow_die "private installer diagnostics are unavailable"
exec 3>&1 4>&2
if [[ "$INSTALL_VERBOSE" == true ]]; then
  exec > >(tee -a "$INSTALL_LOG_PATH" >&3) \
    2> >(tee -a "$INSTALL_LOG_PATH" >&4)
else
  exec >>"$INSTALL_LOG_PATH" 2>&1
fi
INSTALL_OUTPUT_ACTIVE=true
print_install_progress "$INSTALL_VERBOSE" 'Installing Orichum…' >&3
print_install_progress \
  "$INSTALL_VERBOSE" '  Checking existing installation…' >&3
SERVICE_LABEL="io.orichum.cliproxy"
runtime_transaction_active=false
runtime_release=
runtime_previous=-
atlassian_tool_transaction_active=false

rollback_consolidated_runtime_and_home() {
  local rollback_ready=true
  if [[ "${atlassian_tool_transaction_active:-false}" == true ]]; then
    rm -rf -- "$WORKFLOW_DATA_ROOT/tools" || rollback_ready=false
    if [[ -f "$snapshot_dir/atlassian-tools.present" ]]; then
      cp -pPR "$snapshot_dir/atlassian-tools.data" \
        "$WORKFLOW_DATA_ROOT/tools" || rollback_ready=false
    elif [[ ! -f "$snapshot_dir/atlassian-tools.absent" ]]; then
      rollback_ready=false
    fi
    atlassian_tool_transaction_active=false
  fi
  if [[ "${runtime_transaction_active:-false}" == true ]]; then
    rollback_orichum_runtime \
      "$SOURCE_ROOT" "$ORICHUM_HOME_ROOT" \
      "$runtime_release" "$runtime_previous" || rollback_ready=false
    runtime_transaction_active=false
  fi
  if [[ "${home_migration_active:-false}" == true ]]; then
    rollback_orichum_home \
      "$SOURCE_ROOT" "$home_migration_journal" || rollback_ready=false
    home_migration_active=false
  fi
  [[ "$rollback_ready" == true ]]
}
if [[ "$home_migration_active" == true ]]; then
  WORKFLOW_ROLLBACK_HANDLER=rollback_consolidated_runtime_and_home
  WORKFLOW_TRANSACTION_ACTIVE=true
fi

for command_name in curl gh jq tar install python3 git rg uv; do
  command -v "$command_name" >/dev/null || workflow_die "missing required command: $command_name"
done
command -v claude >/dev/null || workflow_die "Claude Code is not installed or not on PATH"
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || \
  workflow_die "Python 3.10 or newer is required"
[[ -x "$WORKFLOW_ROOT/bin/orichum" ]] || \
  workflow_die "required launcher is missing or not executable: orichum"
for helper in \
    orichum-atlassian-mcp \
    orichum-context orichum-doctor orichum-login \
    orichum-plugin orichum-route-proxy orichum-runtime-ready \
    orichum-verify-cliproxy orichum-verify-leanctx-proxy; do
  [[ -x "$WORKFLOW_ROOT/bin/$helper" ]] || \
    workflow_die "required Orichum helper is missing or not executable: $helper"
done
for managed_launcher in orichum; do
  if [[ -d "$USER_BIN_DIR/$managed_launcher" && \
        ! -L "$USER_BIN_DIR/$managed_launcher" ]]; then
    workflow_die \
      "refusing to replace real launcher directory: $USER_BIN_DIR/$managed_launcher"
  fi
done

case "$(uname -s)" in
  Darwin)
    platform=darwin
    cliproxy_os=darwin
    claudex_os=apple-darwin
    ;;
  Linux)
    platform=systemd
    cliproxy_os=linux
    claudex_os=unknown-linux-gnu
    if [[ "$(linux_environment_kind)" == wsl1 ]]; then
      workflow_die "WSL1 is unsupported; use WSL2 with systemd enabled"
    fi
    systemctl --user show-environment >/dev/null 2>&1 || \
      workflow_die "a working systemd user manager is required"
    command -v ss >/dev/null 2>&1 || \
      workflow_die "missing required command: ss (install iproute2)"
    ;;
  *) workflow_die "supported platforms are macOS, Linux, and WSL2" ;;
esac

case "$(uname -m)" in
  arm64|aarch64)
    cliproxy_arch=aarch64
    claudex_arch=aarch64
    ;;
  x86_64|amd64)
    cliproxy_arch=amd64
    claudex_arch=x86_64
    ;;
  *) workflow_die "unsupported CPU architecture: $(uname -m)" ;;
esac
leanctx_release_asset_suffix="$(
  leanctx_release_suffix "$platform" "$claudex_arch"
)"
leanctx_release_tag=v3.9.12

if [[ "$platform" == darwin ]]; then
  for command_name in launchctl plutil lsof; do
    command -v "$command_name" >/dev/null || workflow_die "missing required command: $command_name"
  done
fi

install -d -m 0700 \
  "$WORKFLOW_DATA_ROOT" "$WORKFLOW_DATA_ROOT/state" "$ORICHUM_CONFIG_ROOT"
installer_temp="$(mktemp -d "${TMPDIR:-/tmp}/orichum-install.XXXXXX")"
register_cleanup_path "$installer_temp"
snapshot_dir="$installer_temp/snapshots"
install -d -m 0700 "$snapshot_dir"
install_state_path="$WORKFLOW_DATA_ROOT/state/install-state.json"
install_state_platform="$platform:$cliproxy_arch"
prior_install_state="$installer_temp/prior-install-state.json"
prior_install_state_verified=false
install_state_read_status=0
python3 -I -B "$WORKFLOW_ROOT/integrations/common/install_state.py" \
  snapshot "$install_state_path" "$snapshot_dir" install-state || \
  workflow_die "existing installer state could not be snapshotted safely"
python3 -I -B "$WORKFLOW_ROOT/integrations/common/install_state.py" \
  read "$install_state_path" "$install_state_platform" \
  >"$prior_install_state" 2>/dev/null || install_state_read_status=$?
if [[ "$install_state_read_status" -eq 0 ]]; then
  chmod 0600 "$prior_install_state"
  prior_install_state_verified=true
else
  rm -f -- "$prior_install_state"
fi
control_plane_journal="$WORKFLOW_DATA_ROOT/state/install-control-plane"
control_plane_recovery_needed=false
if [[ -e "$control_plane_journal" || -L "$control_plane_journal" ]]; then
  control_plane_recovery_needed=true
fi
if [[ "$control_plane_recovery_needed" == true ]]; then
  recover_installed_control_plane \
    python3 "$WORKFLOW_ROOT" \
    "$INSTALLED_CONFIG_ROOT" "$control_plane_journal" \
    "$lifecycle_lock_path" "$WORKFLOW_LOCK_FD" || \
    workflow_die \
      "unfinished Orichum control-plane activation could not be recovered"
fi

staged_runtime_release="$(
  stage_orichum_runtime "$SOURCE_ROOT" "$installer_temp/runtime-stage"
)" || workflow_die "standalone Orichum runtime could not be staged"
runtime_previous="$(
  current_orichum_runtime "$SOURCE_ROOT" "$ORICHUM_HOME_ROOT"
)" || workflow_die "existing standalone Orichum runtime is invalid"
runtime_release="$ORICHUM_HOME_ROOT/runtime/releases/$(basename "$staged_runtime_release")"
runtime_transaction_active=true
WORKFLOW_ROLLBACK_HANDLER=rollback_consolidated_runtime_and_home
WORKFLOW_TRANSACTION_ACTIVE=true
activation_result="$(
  activate_orichum_runtime \
    "$SOURCE_ROOT" "$staged_runtime_release" "$ORICHUM_HOME_ROOT"
)" || workflow_die "standalone Orichum runtime could not be activated"
IFS=$'\t' read -r activated_runtime_release activated_runtime_previous \
  <<<"$activation_result"
[[ "$activated_runtime_release" == "$runtime_release" && \
   "$activated_runtime_previous" == "$runtime_previous" ]] || \
  workflow_die "standalone Orichum runtime activation identity changed"
WORKFLOW_ROOT="$runtime_release"

install_contract_fingerprint() {
  python3 -I -B "$WORKFLOW_ROOT/integrations/common/install_state.py" \
    fingerprint "$WORKFLOW_ROOT" "$@"
}
python_input_sha="$(
  install_contract_fingerprint lib/workflow.sh
)" || workflow_die "Python installer input fingerprint failed"
python_probe_sha="$(
  install_contract_fingerprint \
    lib/workflow.sh integrations/common/route_proxy.py
)" || workflow_die "Python probe fingerprint failed"
cliproxy_input_sha="$(
  install_contract_fingerprint install.sh lib/workflow.sh
)" || workflow_die "CLIProxyAPI installer input fingerprint failed"
cliproxy_probe_sha="$cliproxy_input_sha"
claudex_input_sha="$cliproxy_input_sha"
claudex_probe_sha="$(
  install_contract_fingerprint \
    install.sh lib/workflow.sh integrations/common/route_proxy.py
)" || workflow_die "Claudex probe fingerprint failed"
leanctx_input_sha="$(
  install_contract_fingerprint \
    install.sh lib/workflow.sh \
    integrations/common/leanctx_contract.py \
    integrations/common/mcp_probe.py
)" || workflow_die "LeanCTX installer input fingerprint failed"
leanctx_probe_sha="$(
  install_contract_fingerprint \
    lib/workflow.sh integrations/common/leanctx_contract.py \
    integrations/common/mcp_probe.py
)" || workflow_die "LeanCTX probe fingerprint failed"
empty_artifact_sha="$(printf '0%.0s' {1..64})"
controller_plugin_input_sha="$(
  controller_plugin_fingerprint \
    "$WORKFLOW_ROOT" "$WORKFLOW_ROOT" python3
)" || workflow_die "controller plugin input fingerprint failed"
controller_plugin_probe_sha="$(
  install_contract_fingerprint \
    bin/orichum-plugin controller/plugin/hooks/hooks.json
)" || workflow_die "controller plugin probe fingerprint failed"
routing_probe_sha="$(
  install_contract_fingerprint \
    discover-models.sh integrations/common/model_routing.py \
    integrations/common/route_proxy.py
)" || workflow_die "routing probe fingerprint failed"
completion_input_sha="$(
  install_contract_fingerprint \
    bin/orichum bin/orichum-complete lib/workflow.sh \
    integrations/common/orichum_cli.py \
    integrations/common/orichum_completion.py \
    integrations/common/project_context.py
)" || workflow_die "completion installer input fingerprint failed"
completion_probe_sha="$(
  install_contract_fingerprint \
    lib/workflow.sh integrations/common/orichum_completion.py
)" || workflow_die "completion probe fingerprint failed"
controller_plugin_decision=upgraded
if [[ "$prior_install_state_verified" == true ]]; then
  controller_plugin_decision="$(
    decide_install_component \
      "$prior_install_state" controllerPlugin \
      1 orichum:controller-plugin \
      "$controller_plugin_input_sha" \
      "$controller_plugin_input_sha" "$controller_plugin_probe_sha"
  )"
fi
completion_root="$(orichum_completion_root "$ORICHUM_HOME_ROOT")"
completion_zsh_path="$completion_root/zsh/_orichum"
completion_bash_path="$completion_root/bash/orichum"
completion_fish_path="$(orichum_fish_completion_path)"
completion_fish_record="$(
  orichum_fish_completion_record_path "$ORICHUM_HOME_ROOT"
)"
completion_prior_fish_path=
completion_prior_fish_status=0
completion_prior_fish_path="$(
  orichum_recorded_fish_completion_path "$ORICHUM_HOME_ROOT"
)" || completion_prior_fish_status=$?
[[ "$completion_prior_fish_status" -eq 0 ]] || completion_prior_fish_path=
completion_zsh_profile="$HOME/.zshrc"
completion_bash_profile="$HOME/.bashrc"
completion_bash_login_profile="$(orichum_bash_login_profile_path)"
completion_artifact="$empty_artifact_sha"
if verify_orichum_completions \
    "$WORKFLOW_ROOT" "$ORICHUM_HOME_ROOT" \
    "$ORICHUM_CONFIG_ROOT" "$WORKFLOW_DATA_ROOT" \
    "$completion_bash_login_profile" \
    >/dev/null 2>&1; then
  completion_artifact="$(
    verified_orichum_completion_artifact "$ORICHUM_HOME_ROOT"
  )" || workflow_die "completion artifact fingerprint failed"
fi
completion_decision=upgraded
if [[ "$prior_install_state_verified" == true ]]; then
  completion_decision="$(
    decide_install_component \
      "$prior_install_state" completion \
      1 orichum:completion \
      "$completion_artifact" \
      "$completion_input_sha" "$completion_probe_sha"
  )"
fi

(
  cd "$WORKFLOW_ROOT"
  PYTHONDONTWRITEBYTECODE=1 python3 -B - \
    "$WORKFLOW_ROOT/config" <<'PY'
import sys
from pathlib import Path
from integrations.common.orichum_config import (
    default_config_paths,
    load_control_plane,
)

load_control_plane(default_config_paths(Path(sys.argv[1])))
PY
) || workflow_die "source Orichum control plane is invalid"

attempt_verified_fast_install() (
  [[ "$INSTALL_MODE" == fast && \
     "$prior_install_state_verified" == true && \
     "$control_plane_recovery_needed" == false && \
     "$controller_plugin_decision" == reused ]] || return 1
  local python_entrypoint python_identity python_version python_runtime
  local python_artifact cliproxy_artifact claudex_artifact leanctx_artifact
  local cliproxy_service leanctx_service route_service
  local cliproxy_port claudex_port route_port leanctx_port
  local route_runtime routing_input routing_artifact
  local completion_artifact
  local management_key_file management_key
  local binary_identity_file
  local binary_identity_pid
  local config_verify_pid runtime_verify_pid verification_ready

  cleanup_fast_verifiers() {
    local verifier_pid
    for verifier_pid in \
        "${config_verify_pid:-}" "${runtime_verify_pid:-}" \
        "${binary_identity_pid:-}"; do
      [[ "$verifier_pid" =~ ^[1-9][0-9]*$ ]] || continue
      if kill -0 "$verifier_pid" 2>/dev/null; then
        kill "$verifier_pid" 2>/dev/null || true
      fi
      wait "$verifier_pid" 2>/dev/null || true
    done
  }
  trap cleanup_fast_verifiers EXIT

  verify_committed_control_plane \
    "$INSTALLED_CONFIG_ROOT" "$WORKFLOW_DATA_ROOT" 2>/dev/null &
  config_verify_pid=$!
  ORICHUM_CONFIG_HOME="$INSTALLED_CONFIG_ROOT" \
  ORICHUM_DATA_HOME="$WORKFLOW_DATA_ROOT" \
    "$WORKFLOW_ROOT/bin/orichum-runtime-ready" \
      "$WORKFLOW_DATA_ROOT" >/dev/null 2>&1 &
  runtime_verify_pid=$!

  python_entrypoint="$(orichum_python_entrypoint "$WORKFLOW_DATA_ROOT")"
  python_identity="$(
    validate_orichum_python \
      "$WORKFLOW_DATA_ROOT" "$python_entrypoint" 2>/dev/null
  )" || return 1
  IFS=$'\t' read -r python_version python_runtime <<<"$python_identity"
  python_artifact="$(sha256_file "$python_runtime")" || return 1
  ORICHUM_PYTHON="$python_entrypoint"
  ORICHUM_PYTHON_VALIDATED="$python_entrypoint"
  export ORICHUM_PYTHON ORICHUM_PYTHON_VALIDATED
  export ORICHUM_INSTALL_BOOTSTRAP=false

  binary_identity_file="$installer_temp/fast-binaries"
  (
    managed_executable_is_safe \
      "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api" &&
    managed_executable_is_safe \
      "$WORKFLOW_DATA_ROOT/bin/claudex" &&
    managed_executable_is_safe \
      "$WORKFLOW_DATA_ROOT/bin/lean-ctx" &&
    [[ -x "$WORKFLOW_DATA_ROOT/tools/bin/mcp-atlassian" ]] &&
    "$WORKFLOW_DATA_ROOT/tools/bin/mcp-atlassian" \
      --version >/dev/null 2>&1 &&
    printf '%s\t%s\t%s\n' \
      "$(sha256_file "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api")" \
      "$(sha256_file "$WORKFLOW_DATA_ROOT/bin/claudex")" \
      "$(sha256_file "$WORKFLOW_DATA_ROOT/bin/lean-ctx")"
  ) >"$binary_identity_file" &
  binary_identity_pid=$!
  verification_ready=true
  wait "$binary_identity_pid" || verification_ready=false
  binary_identity_pid=
  [[ "$verification_ready" == true ]] || return 1
  IFS=$'\t' read -r \
    cliproxy_artifact claudex_artifact leanctx_artifact \
    <"$binary_identity_file" || return 1

  if [[ "$platform" == darwin ]]; then
    cliproxy_service="$HOME/Library/LaunchAgents/$SERVICE_LABEL.plist"
  else
    cliproxy_service="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/orichum-cliproxy.service"
  fi
  IFS=$'\t' read -r route_service _ _ \
    < <(claudex_proxy_service_identity "$platform") || return 1
  IFS=$'\t' read -r leanctx_service _ _ \
    < <(leanctx_proxy_service_identity "$platform") || return 1
  IFS=$'\t' read -r cliproxy_port claudex_port route_port leanctx_port \
    < <(read_service_ports "$WORKFLOW_DATA_ROOT") || return 1
  valid_service_port "$cliproxy_port" || return 1
  valid_service_port "$claudex_port" || return 1
  valid_service_port "$route_port" || return 1
  valid_service_port "$leanctx_port" || return 1

  [[ -L "$USER_BIN_DIR/orichum" && \
     "$(readlink "$USER_BIN_DIR/orichum")" == \
       "$WORKFLOW_ROOT/bin/orichum" ]] || return 1
  [[ -L "$WORKFLOW_DATA_ROOT/bin/orichum-route-proxy" && \
     "$(readlink "$WORKFLOW_DATA_ROOT/bin/orichum-route-proxy")" == \
       "$WORKFLOW_ROOT/bin/orichum-route-proxy" ]] || return 1
  cmp -s "$WORKFLOW_ROOT/controller/settings.json" \
    "$WORKFLOW_DATA_ROOT/claude-config/settings.json" || return 1
  verify_orichum_completions \
    "$WORKFLOW_ROOT" "$ORICHUM_HOME_ROOT" \
    "$ORICHUM_CONFIG_ROOT" "$WORKFLOW_DATA_ROOT" \
    "$completion_bash_login_profile" \
    >/dev/null 2>&1 || return 1
  completion_artifact="$(
    verified_orichum_completion_artifact "$ORICHUM_HOME_ROOT"
  )" || return 1

  management_key_file="$WORKFLOW_DATA_ROOT/cliproxy-management.key"
  [[ -f "$management_key_file" && ! -L "$management_key_file" && \
     "$(path_uid "$management_key_file")" == "$(id -u)" && \
     "$(path_mode "$management_key_file")" == 600 ]] || return 1
  management_key="$(tr -d '\r\n' <"$management_key_file")"
  (( ${#management_key} >= 32 && ${#management_key} <= 256 )) || return 1
  [[ "$management_key" =~ ^[A-Za-z0-9._~-]+$ ]] || return 1

  route_runtime="$(
    verified_route_runtime_digest \
      "$WORKFLOW_ROOT" "$python_runtime" "$python_version" \
      "$installer_temp/fast-route-runtime"
  )" || return 1
  routing_input="$(
    verified_routing_input_fingerprint \
      "$installer_temp/fast-routing-input" \
      "$cliproxy_artifact" "$claudex_artifact" "$route_runtime" \
      "$cliproxy_port" "$claudex_port" "$route_port" "$leanctx_port" \
      "$INSTALLED_CONFIG_ROOT/accounts.json" \
      "$INSTALLED_CONFIG_ROOT/jira-profiles.json" \
      "$INSTALLED_CONFIG_ROOT/model-stacks.json" \
      "$INSTALLED_CONFIG_ROOT/plugins.json" \
      "$INSTALLED_CONFIG_ROOT/projects.json" \
      "$INSTALLED_CONFIG_ROOT/providers.json" \
      "$INSTALLED_CONFIG_ROOT/runtime.json" \
      "$INSTALLED_CONFIG_ROOT/controller-policy.md" \
      "$WORKFLOW_ROOT/config/model-stacks.json" \
      "$WORKFLOW_ROOT/config/jira-profiles.json" \
      "$WORKFLOW_ROOT/config/plugins.json" \
      "$WORKFLOW_ROOT/config/projects.json" \
      "$WORKFLOW_ROOT/config/providers.json" \
      "$WORKFLOW_ROOT/config/runtime.json" \
      "$WORKFLOW_ROOT/config/controller-policy.md" \
      "$WORKFLOW_DATA_ROOT/claude-config/settings.json" \
      "$WORKFLOW_DATA_ROOT/cliproxy.yaml" \
      "$WORKFLOW_DATA_ROOT/leanctx/proxy/config/config.toml" \
      "$cliproxy_service" "$leanctx_service" "$route_service"
  )" || return 1
  routing_artifact="$(
    verified_routing_runtime_artifact \
      "$WORKFLOW_DATA_ROOT" "$INSTALLED_CONFIG_ROOT" \
      "$cliproxy_service" "$leanctx_service" "$route_service" \
      "$installer_temp/fast-routing-artifact"
  )" || return 1

  jq -e \
    --arg python_version "$python_version" \
    --arg python_artifact "$python_artifact" \
    --arg python_input "$python_input_sha" \
    --arg python_probe "$python_probe_sha" \
    --arg cliproxy_artifact "$cliproxy_artifact" \
    --arg cliproxy_input "$cliproxy_input_sha" \
    --arg cliproxy_probe "$cliproxy_probe_sha" \
    --arg claudex_artifact "$claudex_artifact" \
    --arg claudex_input "$claudex_input_sha" \
    --arg claudex_probe "$claudex_probe_sha" \
    --arg leanctx_artifact "$leanctx_artifact" \
    --arg leanctx_input "$leanctx_input_sha" \
    --arg leanctx_probe "$leanctx_probe_sha" \
    --arg controller_input "$controller_plugin_input_sha" \
    --arg controller_probe "$controller_plugin_probe_sha" \
    --arg completion_artifact "$completion_artifact" \
    --arg completion_input "$completion_input_sha" \
    --arg completion_probe "$completion_probe_sha" \
    --arg routing_artifact "$routing_artifact" \
    --arg routing_input "$routing_input" \
    --arg routing_probe "$routing_probe_sha" '
      .components as $c |
      ($c | keys) == [
        "claudex", "cliproxy", "completion", "controllerPlugin",
        "leanctx", "python", "routing"
      ] and
      $c.python == {
        version: $python_version,
        sourceIdentity: ("python:" + $python_version),
        artifactSha256: $python_artifact,
        inputSha256: $python_input,
        probeSha256: $python_probe
      } and
      ($c.cliproxy.sourceIdentity |
        startswith("github:router-for-me/CLIProxyAPI@")) and
      $c.cliproxy.artifactSha256 == $cliproxy_artifact and
      $c.cliproxy.inputSha256 == $cliproxy_input and
      $c.cliproxy.probeSha256 == $cliproxy_probe and
      ($c.claudex.sourceIdentity |
        startswith("github:alupao/claudex@")) and
      $c.claudex.artifactSha256 == $claudex_artifact and
      $c.claudex.inputSha256 == $claudex_input and
      $c.claudex.probeSha256 == $claudex_probe and
      ($c.leanctx.sourceIdentity |
        startswith("github:yvgude/lean-ctx@")) and
      $c.leanctx.artifactSha256 == $leanctx_artifact and
      $c.leanctx.inputSha256 == $leanctx_input and
      $c.leanctx.probeSha256 == $leanctx_probe and
      $c.controllerPlugin == {
        version: "1",
        sourceIdentity: "orichum:controller-plugin",
        artifactSha256: $controller_input,
        inputSha256: $controller_input,
        probeSha256: $controller_probe
      } and
      $c.completion == {
        version: "1",
        sourceIdentity: "orichum:completion",
        artifactSha256: $completion_artifact,
        inputSha256: $completion_input,
        probeSha256: $completion_probe
      } and
      $c.routing == {
        version: "1",
        sourceIdentity: "orichum:routing",
        artifactSha256: $routing_artifact,
        inputSha256: $routing_input,
        probeSha256: $routing_probe
      }
    ' "$prior_install_state" >/dev/null || return 1

  verification_ready=true
  wait "$config_verify_pid" || verification_ready=false
  config_verify_pid=
  wait "$runtime_verify_pid" || verification_ready=false
  runtime_verify_pid=
  [[ "$verification_ready" == true ]] || return 1

  print_component_status_table \
    reused reused reused reused reused reused reused
  printf 'Verified Orichum installation is current for %s.\n' "$platform"
  print_install_summary \
    "$WORKFLOW_ROOT" "$WORKFLOW_DATA_ROOT" "$USER_BIN_DIR" \
    "$WORKFLOW_DATA_ROOT/bin/claudex" \
    "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api" \
    "$cliproxy_service" "$cliproxy_port" reused \
    "$route_service" "$claudex_port" "$route_port" reused \
    "$python_entrypoint" "$python_version" "$python_runtime" reused \
    "$WORKFLOW_DATA_ROOT/bin/lean-ctx" "$SOURCE_ROOT" "$ORICHUM_HOME_ROOT" \
    "$leanctx_service" "$leanctx_port" reused
  printf '\nFast readiness checks passed.\n'
)

if attempt_verified_fast_install; then
  runtime_transaction_active=false
  WORKFLOW_TRANSACTION_ACTIVE=false
  if [[ "$home_migration_active" == true ]]; then
    commit_orichum_home \
      "$SOURCE_ROOT" "$home_migration_journal" || \
      workflow_die "consolidated Orichum home could not be committed"
    home_migration_active=false
  fi
  prune_orichum_runtime \
    "$SOURCE_ROOT" "$ORICHUM_HOME_ROOT" "$runtime_release" || \
    printf 'WARNING: obsolete Orichum runtime releases could not be removed.\n' >&2
  print_install_component_results \
    reused reused reused reused reused reused reused \
    "$ORICHUM_COMPLETION_OPTIONAL_SHELL" >&3
  print_install_outcome false "$ORICHUM_COMPLETION_OPTIONAL_SHELL" \
    "$INSTALL_LOG_PATH" >&3
  exit 0
fi

print_install_progress \
  "$INSTALL_VERBOSE" '  Installing or updating components…' >&3

if [[ "$controller_plugin_decision" != reused ]]; then
  validation_config="$(mktemp -d "${TMPDIR:-/tmp}/orichum-plugin.XXXXXX")"
  register_cleanup_path "$validation_config"
  chmod 0700 "$validation_config"
  CLAUDE_CONFIG_DIR="$validation_config" \
    claude plugin validate --strict \
      "$WORKFLOW_ROOT/controller/plugin" >/dev/null || \
    workflow_die "controller plugin validation failed"
  rm -rf -- "$validation_config"
fi

snapshot_path "$WORKFLOW_DATA_ROOT/tools" \
  "$snapshot_dir" atlassian-tools
atlassian_tool_transaction_active=true

install -d -m 0755 "$USER_BIN_DIR"
install -d -m 0700 "$WORKFLOW_DATA_ROOT"
install -d -m 0700 \
  "$WORKFLOW_DATA_ROOT/bin" \
  "$WORKFLOW_DATA_ROOT/auth" \
  "$WORKFLOW_DATA_ROOT/claude-config" \
  "$WORKFLOW_DATA_ROOT/state" \
  "$WORKFLOW_DATA_ROOT/state/sessions" \
  "$WORKFLOW_DATA_ROOT/logs" \
  "$WORKFLOW_DATA_ROOT/leanctx" \
  "$WORKFLOW_DATA_ROOT/leanctx/lean-ctx" \
  "$WORKFLOW_DATA_ROOT/leanctx/proxy/config" \
  "$WORKFLOW_DATA_ROOT/leanctx/proxy/state" \
  "$WORKFLOW_DATA_ROOT/leanctx/proxy/cache" \
  "$WORKFLOW_DATA_ROOT/tools" \
  "$WORKFLOW_DATA_ROOT/tools/bin" \
  "$WORKFLOW_DATA_ROOT/tools/uv"
chmod 0700 "$WORKFLOW_DATA_ROOT/bin"

atlassian_tool_arguments=(tool install)
if [[ "$INSTALL_MODE" == upgrade ]]; then
  atlassian_tool_arguments+=(--upgrade)
fi
atlassian_tool_arguments+=(mcp-atlassian)
if [[ "$INSTALL_MODE" == upgrade || \
      ! -x "$WORKFLOW_DATA_ROOT/tools/bin/mcp-atlassian" ]]; then
  UV_TOOL_DIR="$WORKFLOW_DATA_ROOT/tools/uv" \
  UV_TOOL_BIN_DIR="$WORKFLOW_DATA_ROOT/tools/bin" \
    uv --quiet "${atlassian_tool_arguments[@]}" || \
    workflow_die "mcp-atlassian could not be installed"
fi
"$WORKFLOW_DATA_ROOT/tools/bin/mcp-atlassian" \
  --version >/dev/null 2>&1 || \
  workflow_die "mcp-atlassian failed its executable readiness check"

python_entrypoint="$(orichum_python_entrypoint "$WORKFLOW_DATA_ROOT")"
snapshot_path "$python_entrypoint" "$snapshot_dir" orichum-python
python_recorded_version=
python_recorded_source=
python_recorded_artifact="$empty_artifact_sha"
python_current_artifact="$empty_artifact_sha"
python_resolve_upstream=true
python_decision=upgraded
if [[ "$prior_install_state_verified" == true ]]; then
  python_recorded_version="$(
    install_state_component_field \
      "$prior_install_state" python version 2>/dev/null || true
  )"
  python_recorded_source="$(
    install_state_component_field \
      "$prior_install_state" python sourceIdentity 2>/dev/null || true
  )"
  python_recorded_artifact="$(
    install_state_component_field \
      "$prior_install_state" python artifactSha256 2>/dev/null || \
      printf '%s' "$empty_artifact_sha"
  )"
  if python_identity="$(
      validate_orichum_python \
        "$WORKFLOW_DATA_ROOT" "$python_entrypoint" 2>/dev/null
    )"; then
    IFS=$'\t' read -r _ python_current_path <<<"$python_identity"
    python_current_artifact="$(sha256_file "$python_current_path")"
  fi
  python_decision="$(
    decide_install_component \
      "$prior_install_state" python \
      "$python_recorded_version" "$python_recorded_source" \
      "$python_current_artifact" "$python_input_sha" "$python_probe_sha"
  )"
  if [[ "$INSTALL_MODE" == fast ]]; then
    python_resolve_upstream=false
  fi
fi
python_transaction_active=false
python_candidate_generation=
rollback_python_activation() {
  local rollback_ready=true
  if [[ "${python_transaction_active:-false}" == true ]]; then
    restore_snapshot "$python_entrypoint" \
      "$snapshot_dir" orichum-python || rollback_ready=false
    snapshot_path_matches "$python_entrypoint" \
      "$snapshot_dir" orichum-python || rollback_ready=false
    remove_orichum_python_generation \
      "$WORKFLOW_DATA_ROOT" "${python_candidate_generation:-}" || \
      rollback_ready=false
  fi
  [[ "$rollback_ready" == true ]]
}
rollback_python_and_consolidated() {
  local rollback_ready=true
  rollback_python_activation || rollback_ready=false
  rollback_consolidated_runtime_and_home || rollback_ready=false
  [[ "$rollback_ready" == true ]]
}
python_transaction_active=true
WORKFLOW_ROLLBACK_HANDLER=rollback_python_and_consolidated
WORKFLOW_TRANSACTION_ACTIVE=true
IFS=$'\t' read -r \
  orichum_python_action orichum_python_version orichum_python_candidate \
  python_candidate_generation < <(
    install_or_reuse_orichum_python \
      "$WORKFLOW_DATA_ROOT" "$python_resolve_upstream" \
      "$python_recorded_version" "$python_recorded_artifact"
  ) || workflow_die "private Orichum Python could not be provisioned"
(
  cd "$WORKFLOW_ROOT"
  PYTHONDONTWRITEBYTECODE=1 "$orichum_python_candidate" -I -B - \
    "$WORKFLOW_ROOT" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
import integrations.common.orichum_cli  # noqa: F401
import integrations.common.route_proxy  # noqa: F401

for source in sorted((root / "integrations" / "common").glob("*.py")):
    compile(source.read_text(encoding="utf-8"), str(source), "exec")
PY
) || workflow_die "private Orichum Python failed module validation"
if [[ "$python_decision" != reused ]]; then
  preflight_orichum_python_runtime \
    "$orichum_python_candidate" "$WORKFLOW_ROOT" "$WORKFLOW_DATA_ROOT" || \
    workflow_die "private Orichum Python failed recovery-proxy preflight"
  if [[ "$orichum_python_action" == reused ]]; then
    orichum_python_action=repaired
  fi
fi
activate_orichum_python \
  "$WORKFLOW_DATA_ROOT" "$orichum_python_candidate" || \
  workflow_die "private Orichum Python could not be activated"
ORICHUM_PYTHON="$(resolve_orichum_python "$WORKFLOW_DATA_ROOT")"
export ORICHUM_PYTHON
ORICHUM_PYTHON_VALIDATED="$ORICHUM_PYTHON"
export ORICHUM_PYTHON_VALIDATED
export ORICHUM_INSTALL_BOOTSTRAP=false

candidate_config_root="$installer_temp/control-plane"
stage_installed_control_plane \
  "$ORICHUM_PYTHON" "$WORKFLOW_ROOT" \
  "$INSTALLED_CONFIG_ROOT" "$candidate_config_root" || \
  workflow_die "installed Orichum control plane could not be staged"
candidate_config_root="$(
  cd -P -- "$candidate_config_root" && pwd
)" || workflow_die "installed Orichum control plane path is unavailable"
ORICHUM_CONFIG_ROOT="$candidate_config_root"
ORICHUM_CONFIG_HOME="$ORICHUM_CONFIG_ROOT"
export ORICHUM_CONFIG_HOME
ORICHUM_CONFIG_HOME="$ORICHUM_CONFIG_ROOT" \
ORICHUM_DATA_HOME="$WORKFLOW_DATA_ROOT" \
  "$WORKFLOW_ROOT/bin/orichum" config validate >/dev/null || \
  workflow_die "installed Orichum control plane is invalid"
(
  cd "$WORKFLOW_ROOT"
  PYTHONDONTWRITEBYTECODE=1 "$ORICHUM_PYTHON" -B - \
    "$WORKFLOW_DATA_ROOT" "$ORICHUM_CONFIG_ROOT/projects.json" <<'PY'
import json
import sys
from pathlib import Path

from integrations.common.github_identity import ensure_github_identity

data_home = Path(sys.argv[1])
projects = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
accounts = {
    context.get("githubAccount")
    for context in projects["contexts"]
    if context.get("githubAccount") is not None
}
for account in sorted(accounts):
    ensure_github_identity(data_home, account)
PY
) || workflow_die \
  "one or more project GitHub identities could not be isolated; verify gh auth"

management_key_file="$WORKFLOW_DATA_ROOT/cliproxy-management.key"
if [[ ! -e "$management_key_file" ]]; then
  umask 077
  "$ORICHUM_PYTHON" -c 'import secrets; print(secrets.token_urlsafe(48))' \
    >"$management_key_file"
  chmod 0600 "$management_key_file"
fi
[[ -f "$management_key_file" && ! -L "$management_key_file" ]] || \
  workflow_die "CLIProxyAPI management key is unsafe"
"$ORICHUM_PYTHON" - "$management_key_file" <<'PY' || \
  workflow_die "CLIProxyAPI management key is unsafe"
import os
import stat
import sys

observed = os.stat(sys.argv[1], follow_symlinks=False)
if observed.st_uid != os.getuid() or stat.S_IMODE(observed.st_mode) != 0o600:
    raise SystemExit(1)
PY
management_key="$(tr -d '\r\n' <"$management_key_file")"
if (( ${#management_key} < 32 || ${#management_key} > 256 )) || \
   [[ ! "$management_key" =~ ^[A-Za-z0-9._~-]+$ ]]; then
  workflow_die "CLIProxyAPI management key is invalid"
fi
ln -sfn "$WORKFLOW_ROOT/bin/orichum-route-proxy" \
  "$WORKFLOW_DATA_ROOT/bin/orichum-route-proxy"

migrate_legacy_model_config "$WORKFLOW_DATA_ROOT"
find "$WORKFLOW_DATA_ROOT/auth" -maxdepth 1 -type f -exec chmod 0600 {} \;
chmod 0755 "$WORKFLOW_ROOT/controller/plugin/scripts/"*.sh

export PATH="$HOME/.local/bin:$PATH"

cliproxy_recorded_version=
cliproxy_recorded_source=
cliproxy_recorded_artifact="$empty_artifact_sha"
cliproxy_current_artifact="$empty_artifact_sha"
cliproxy_resolve_upstream=true
cliproxy_decision=upgraded
claudex_recorded_version=
claudex_recorded_source=
claudex_recorded_artifact="$empty_artifact_sha"
claudex_current_artifact="$empty_artifact_sha"
claudex_resolve_upstream=true
claudex_decision=upgraded
leanctx_recorded_version=
leanctx_recorded_source=
leanctx_recorded_artifact="$empty_artifact_sha"
leanctx_current_artifact="$empty_artifact_sha"
leanctx_resolve_upstream=true
leanctx_decision=upgraded
if [[ "$prior_install_state_verified" == true ]]; then
  cliproxy_recorded_version="$(
    install_state_component_field \
      "$prior_install_state" cliproxy version 2>/dev/null || true
  )"
  cliproxy_recorded_source="$(
    install_state_component_field \
      "$prior_install_state" cliproxy sourceIdentity 2>/dev/null || true
  )"
  cliproxy_recorded_artifact="$(
    install_state_component_field \
      "$prior_install_state" cliproxy artifactSha256 2>/dev/null || \
      printf '%s' "$empty_artifact_sha"
  )"
  if managed_executable_is_safe \
      "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api"; then
    cliproxy_current_artifact="$(
      sha256_file "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api"
    )"
  fi
  cliproxy_decision="$(
    decide_install_component \
      "$prior_install_state" cliproxy \
      "$cliproxy_recorded_version" "$cliproxy_recorded_source" \
      "$cliproxy_current_artifact" \
      "$cliproxy_input_sha" "$cliproxy_probe_sha"
  )"
  if [[ "$INSTALL_MODE" == fast && \
        "$cliproxy_decision" != upgraded ]]; then
    cliproxy_resolve_upstream=false
  fi

  claudex_recorded_version="$(
    install_state_component_field \
      "$prior_install_state" claudex version 2>/dev/null || true
  )"
  claudex_recorded_source="$(
    install_state_component_field \
      "$prior_install_state" claudex sourceIdentity 2>/dev/null || true
  )"
  claudex_recorded_artifact="$(
    install_state_component_field \
      "$prior_install_state" claudex artifactSha256 2>/dev/null || \
      printf '%s' "$empty_artifact_sha"
  )"
  if managed_executable_is_safe "$WORKFLOW_DATA_ROOT/bin/claudex"; then
    claudex_current_artifact="$(
      sha256_file "$WORKFLOW_DATA_ROOT/bin/claudex"
    )"
  fi
  claudex_decision="$(
    decide_install_component \
      "$prior_install_state" claudex \
      "$claudex_recorded_version" "$claudex_recorded_source" \
      "$claudex_current_artifact" "$claudex_input_sha" "$claudex_probe_sha"
  )"
  if [[ "$INSTALL_MODE" == fast && \
        "$claudex_decision" != upgraded ]]; then
    claudex_resolve_upstream=false
  fi

  leanctx_recorded_version="$(
    install_state_component_field \
      "$prior_install_state" leanctx version 2>/dev/null || true
  )"
  leanctx_recorded_source="$(
    install_state_component_field \
      "$prior_install_state" leanctx sourceIdentity 2>/dev/null || true
  )"
  leanctx_recorded_artifact="$(
    install_state_component_field \
      "$prior_install_state" leanctx artifactSha256 2>/dev/null || \
      printf '%s' "$empty_artifact_sha"
  )"
  if managed_executable_is_safe "$WORKFLOW_DATA_ROOT/bin/lean-ctx"; then
    leanctx_current_artifact="$(
      sha256_file "$WORKFLOW_DATA_ROOT/bin/lean-ctx"
    )"
  fi
  leanctx_decision="$(
    decide_install_component \
      "$prior_install_state" leanctx \
      "$leanctx_recorded_version" "$leanctx_recorded_source" \
      "$leanctx_current_artifact" "$leanctx_input_sha" "$leanctx_probe_sha"
  )"
  if [[ "$INSTALL_MODE" == fast && \
        "$leanctx_decision" != upgraded ]]; then
    leanctx_resolve_upstream=false
  fi
fi

if [[ "$INSTALL_MODE" == upgrade && \
      "$prior_install_state_verified" == true && \
      -n "$leanctx_recorded_version" ]]; then
  if pinned_release_allows_recorded_version \
      "$leanctx_recorded_version" "$leanctx_release_tag"; then
    :
  else
    pin_status="$?"
    if [[ "$pin_status" -eq 1 ]]; then
      workflow_die \
        "refusing to downgrade LeanCTX $leanctx_recorded_version to ${leanctx_release_tag#v}; use a newer Orichum release with a compatible LeanCTX pin"
    fi
    workflow_die "LeanCTX release pin could not be compared safely"
  fi
fi

cliproxy_state="$(stage_github_binary \
  router-for-me/CLIProxyAPI 'CLIProxyAPI_' "_${cliproxy_os}_${cliproxy_arch}.tar.gz" \
  cli-proxy-api "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api" \
  "$installer_temp/cliproxy" "$cliproxy_resolve_upstream" \
  "$cliproxy_recorded_version" "$cliproxy_recorded_source" \
  "$cliproxy_recorded_artifact")"
cliproxy_version="$(jq -r '.version' <<<"$cliproxy_state")"
claudex_state="$(stage_github_binary \
  alupao/claudex 'claudex-v' "-${claudex_arch}-${claudex_os}.tar.gz" \
  claudex "$WORKFLOW_DATA_ROOT/bin/claudex" \
  "$installer_temp/claudex" "$claudex_resolve_upstream" \
  "$claudex_recorded_version" "$claudex_recorded_source" \
  "$claudex_recorded_artifact")"
claudex_version="$(jq -r '.version' <<<"$claudex_state")"
leanctx_state="$(stage_github_binary \
  yvgude/lean-ctx 'lean-ctx-' "$leanctx_release_asset_suffix" \
  lean-ctx "$WORKFLOW_DATA_ROOT/bin/lean-ctx" \
  "$installer_temp/leanctx" "$leanctx_resolve_upstream" \
  "$leanctx_recorded_version" "$leanctx_recorded_source" \
  "$leanctx_recorded_artifact" "$leanctx_release_tag")"
leanctx_version="$(jq -r '.version' <<<"$leanctx_state")"
cliproxy_binary_changed="$(jq -r '.changed' <<<"$cliproxy_state")"
claudex_binary_changed="$(jq -r '.changed' <<<"$claudex_state")"
leanctx_binary_changed="$(jq -r '.changed' <<<"$leanctx_state")"
if [[ "$leanctx_binary_changed" == true ]]; then
  leanctx_candidate="$(jq -r '.staged_path' <<<"$leanctx_state")"
else
  leanctx_candidate="$WORKFLOW_DATA_ROOT/bin/lean-ctx"
fi
provision_leanctx_embeddings \
  "$leanctx_candidate" "$WORKFLOW_DATA_ROOT" "$installer_temp" || \
  workflow_die "LeanCTX ONNX Runtime provisioning failed"
leanctx_ort_dylib_path="$(
  verified_leanctx_ort_dylib_path \
    "$leanctx_candidate" "$WORKFLOW_DATA_ROOT" "$installer_temp"
)" || workflow_die "LeanCTX managed ONNX Runtime verification failed"
if [[ "$leanctx_decision" != reused ]]; then
  probe_leanctx_capabilities \
    "$leanctx_candidate" "$ORICHUM_PYTHON" "$WORKFLOW_ROOT" \
    "$installer_temp" "$leanctx_ort_dylib_path" \
    "$WORKFLOW_DATA_ROOT/leanctx/cache" || \
    workflow_die "LeanCTX failed the bounded headless MCP capability probe"
fi
desired_cliproxy_config="$installer_temp/cliproxy.yaml"
desired_leanctx_proxy_config="$installer_temp/leanctx-proxy.toml"

if [[ "$platform" == darwin ]]; then
  service_file="$HOME/Library/LaunchAgents/$SERVICE_LABEL.plist"
  desired_service_file="$installer_temp/$SERVICE_LABEL.plist"
  service_mode=0644
  leanctx_proxy_service_mode=0644
  claudex_proxy_service_mode=0644
  cliproxy_service_label="$SERVICE_LABEL"
  cliproxy_service_unit=-
else
  service_file="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/orichum-cliproxy.service"
  desired_service_file="$installer_temp/orichum-cliproxy.service"
  service_mode=0600
  leanctx_proxy_service_mode=0600
  claudex_proxy_service_mode=0600
  cliproxy_service_label=-
  cliproxy_service_unit=orichum-cliproxy.service
fi
if ! IFS=$'\t' read -r \
    leanctx_proxy_service_file leanctx_proxy_service_label \
    leanctx_proxy_service_unit \
    < <(leanctx_proxy_service_identity "$platform"); then
  workflow_die "LeanCTX proxy service identity could not be resolved"
fi
if ! IFS=$'\t' read -r \
    claudex_proxy_service_file claudex_proxy_service_label \
    claudex_proxy_service_unit \
    < <(claudex_proxy_service_identity "$platform"); then
  workflow_die "Orichum route proxy service identity could not be resolved"
fi
if [[ "$platform" == darwin ]]; then
  leanctx_proxy_desired_service_file="$installer_temp/io.orichum.leanctx-proxy.plist"
  claudex_proxy_desired_service_file="$installer_temp/io.orichum.route-proxy.plist"
else
  leanctx_proxy_desired_service_file="$installer_temp/orichum-leanctx-proxy.service"
  claudex_proxy_desired_service_file="$installer_temp/orichum-route-proxy.service"
fi
install -d -m 0755 \
  "$(dirname "$service_file")" \
  "$(dirname "$leanctx_proxy_service_file")" \
  "$(dirname "$claudex_proxy_service_file")"

cliproxy_service_was_present=false
cliproxy_service_owned=false
if [[ -e "$service_file" || -L "$service_file" ]]; then
  cliproxy_service_was_present=true
  if cliproxy_service_is_owned "$service_file" "$WORKFLOW_DATA_ROOT"; then
    :
  elif [[ "$home_migration_active" == true ]] && \
       cliproxy_service_is_owned \
         "$service_file" "${XDG_DATA_HOME:-$HOME/.local/share}/orichum"; then
    :
  else
    workflow_die "refusing to overwrite unknown service file: $service_file"
  fi
  cliproxy_service_owned=true
fi
leanctx_proxy_service_was_present=false
leanctx_proxy_service_owned=false
if [[ -e "$leanctx_proxy_service_file" || \
      -L "$leanctx_proxy_service_file" ]]; then
  leanctx_proxy_service_was_present=true
  if ! leanctx_proxy_service_is_owned \
      "$leanctx_proxy_service_file" "$WORKFLOW_DATA_ROOT" true; then
    workflow_die \
      "refusing to overwrite unknown service file: $leanctx_proxy_service_file"
  fi
  leanctx_proxy_service_owned=true
fi
claudex_proxy_service_was_present=false
claudex_proxy_service_owned=false
prior_route_data_root="$WORKFLOW_DATA_ROOT"
prior_route_workflow_root="$WORKFLOW_ROOT"
if [[ "$runtime_previous" != "-" ]]; then
  prior_route_workflow_root="$runtime_previous"
fi
if [[ -e "$claudex_proxy_service_file" || \
      -L "$claudex_proxy_service_file" ]]; then
  claudex_proxy_service_was_present=true
  if claudex_proxy_service_is_owned \
      "$claudex_proxy_service_file" "$WORKFLOW_DATA_ROOT" \
      "$WORKFLOW_ROOT"; then
    :
  elif [[ "$runtime_previous" != "-" ]] && \
       claudex_proxy_service_is_owned \
         "$claudex_proxy_service_file" "$WORKFLOW_DATA_ROOT" \
         "$runtime_previous"; then
    prior_route_workflow_root="$runtime_previous"
  elif [[ "$home_migration_active" == true ]] && \
       claudex_proxy_service_is_owned \
         "$claudex_proxy_service_file" \
         "${XDG_DATA_HOME:-$HOME/.local/share}/orichum" \
         "$SOURCE_ROOT"; then
    prior_route_data_root="${XDG_DATA_HOME:-$HOME/.local/share}/orichum"
    prior_route_workflow_root="$SOURCE_ROOT"
  else
    workflow_die \
      "refusing to overwrite unknown service file: $claudex_proxy_service_file"
  fi
  claudex_proxy_service_owned=true
fi
leanctx_proxy_manager_target_state="$(managed_service_target_state \
  "$platform" "$leanctx_proxy_service_label" \
  "$leanctx_proxy_service_unit")" || workflow_die \
  "LeanCTX proxy manager target could not be inspected safely"
if [[ "$leanctx_proxy_manager_target_state" == loaded ]]; then
  leanctx_proxy_loaded_definition="$(managed_service_definition_path \
    "$platform" "$leanctx_proxy_service_label" \
    "$leanctx_proxy_service_unit" 2>/dev/null || true)"
  if [[ "$leanctx_proxy_service_owned" != true ]] || \
     [[ "$leanctx_proxy_loaded_definition" != \
        "$leanctx_proxy_service_file" ]]; then
    workflow_die "refusing to replace loaded unknown LeanCTX proxy target"
  fi
fi
claudex_proxy_manager_target_state="$(managed_service_target_state \
  "$platform" "$claudex_proxy_service_label" \
  "$claudex_proxy_service_unit")" || workflow_die \
  "Orichum route proxy manager target could not be inspected safely"
if [[ "$claudex_proxy_manager_target_state" == loaded ]]; then
  claudex_proxy_loaded_definition="$(managed_service_definition_path \
    "$platform" "$claudex_proxy_service_label" \
    "$claudex_proxy_service_unit" 2>/dev/null || true)"
  if [[ "$claudex_proxy_service_owned" != true ]] || \
     [[ "$claudex_proxy_loaded_definition" != \
        "$claudex_proxy_service_file" ]]; then
    workflow_die \
      "refusing to replace loaded unknown Orichum route proxy target"
  fi
fi

if ! IFS=$'\t' read -r \
    CLIPROXY_PORT PERSISTED_CLAUDEX_PROXY_PORT PERSISTED_ROUTE_PROXY_PORT \
    PERSISTED_LEANCTX_PROXY_PORT \
    < <(read_service_ports "$WORKFLOW_DATA_ROOT"); then
  workflow_die "service port configuration is invalid"
fi
PRIOR_CLIPROXY_PORT="$CLIPROXY_PORT"
PRIOR_ROUTE_PROXY_PORT="$PERSISTED_ROUTE_PROXY_PORT"
PRIOR_LEANCTX_PROXY_PORT="$PERSISTED_LEANCTX_PROXY_PORT"
CLIPROXY_PORT="${ORICHUM_CLIPROXY_PORT:-$CLIPROXY_PORT}"
CLAUDEX_PROXY_PORT="${ORICHUM_CLAUDEX_PROXY_PORT:-$PERSISTED_CLAUDEX_PROXY_PORT}"
ROUTE_PROXY_LISTEN_PORT="${ORICHUM_ROUTE_PROXY_PORT:-$PERSISTED_ROUTE_PROXY_PORT}"
LEANCTX_PROXY_PORT="${ORICHUM_LEANCTX_PROXY_PORT:-$PERSISTED_LEANCTX_PROXY_PORT}"
valid_service_port "$CLIPROXY_PORT" || workflow_die "invalid CLIProxyAPI port"
valid_service_port "$CLAUDEX_PROXY_PORT" || \
  workflow_die "invalid Claudex proxy port"
valid_service_port "$ROUTE_PROXY_LISTEN_PORT" || \
  workflow_die "invalid Orichum route proxy port"
valid_service_port "$LEANCTX_PROXY_PORT" || \
  workflow_die "invalid LeanCTX proxy port"
[[ "$(printf '%s\n' \
    "$CLIPROXY_PORT" "$CLAUDEX_PROXY_PORT" \
    "$ROUTE_PROXY_LISTEN_PORT" "$LEANCTX_PROXY_PORT" | \
    sort -u | wc -l | tr -d ' ')" == 4 ]] || \
  workflow_die "Orichum service ports must differ"

cliproxy_endpoint_ready_at() {
  curl -fsS --connect-timeout 1 --max-time 2 \
    "http://127.0.0.1:$1/v1/models" 2>/dev/null | \
    cliproxy_models_response_is_ready /dev/stdin
}

leanctx_proxy_endpoint_ready_at() {
  curl -fsS --connect-timeout 1 --max-time 2 \
    "http://127.0.0.1:$1/health" 2>/dev/null | \
    jq -e '.status == "ok" or .status == "healthy"' >/dev/null 2>&1
}

leanctx_proxy_runtime_is_owned() {
  local port="$1"
  local service_pid
  service_pid="$(managed_service_main_pid \
    "$platform" "$leanctx_proxy_service_label" \
    "$leanctx_proxy_service_unit")" || return 1
  pid_owns_loopback_listener "$service_pid" "$port" || return 1
  leanctx_proxy_endpoint_ready_at "$port"
}

wait_for_leanctx_proxy() {
  local port="${1:-$LEANCTX_PROXY_PORT}"
  for _ in {1..30}; do
    if leanctx_proxy_runtime_is_owned "$port"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

claudex_proxy_endpoint_ready_at() {
  local port="$1"
  local expected_model="$2"
  curl -fsS --connect-timeout 1 --max-time 2 \
    "http://127.0.0.1:$port/v1/models" 2>/dev/null | \
    claudex_proxy_models_response_is_ready /dev/stdin "$expected_model"
}

claudex_proxy_health_is_ready_at() {
  local port="$1"
  curl -fsS --connect-timeout 1 --max-time 2 \
    "http://127.0.0.1:$port/health" 2>/dev/null | \
    jq -e '
      .service == "orichum-route-proxy" and
      .ready == true
    ' >/dev/null 2>&1
}

claudex_proxy_runtime_is_owned() {
  local port="$1"
  local expected_model="$2"
  local service_pid
  service_pid="$(managed_service_main_pid \
    "$platform" "$claudex_proxy_service_label" \
    "$claudex_proxy_service_unit")" || return 1
  claudex_proxy_health_is_ready_at "$port" || return 1
  claudex_proxy_endpoint_ready_at "$port" "$expected_model"
}

wait_for_claudex_proxy() {
  local port="$1"
  local expected_model="$2"
  for _ in {1..30}; do
    if claudex_proxy_runtime_is_owned "$port" "$expected_model"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

claudex_proxy_loaded_target_is_expected() {
  local loaded_definition target_state
  claudex_proxy_service_is_owned \
    "$claudex_proxy_service_file" "$WORKFLOW_DATA_ROOT" \
    "$WORKFLOW_ROOT" || return 1
  target_state="$(managed_service_target_state \
    "$platform" "$claudex_proxy_service_label" \
    "$claudex_proxy_service_unit")" || return 1
  if [[ "$target_state" == loaded ]]; then
    loaded_definition="$(managed_service_definition_path \
      "$platform" "$claudex_proxy_service_label" \
      "$claudex_proxy_service_unit" 2>/dev/null)" || return 1
    [[ "$loaded_definition" == "$claudex_proxy_service_file" ]] || return 1
  fi
}

claudex_proxy_prior_runtime_safe_to_stop() {
  local current_pid target_state loaded_definition
  target_state="$(managed_service_target_state \
    "$platform" "$claudex_proxy_service_label" \
    "$claudex_proxy_service_unit")" || return 1
  claudex_proxy_service_is_owned \
    "$claudex_proxy_service_file" "$prior_route_data_root" \
    "$prior_route_workflow_root" || return 1
  [[ "$target_state" == absent ]] && return 0
  loaded_definition="$(managed_service_definition_path \
    "$platform" "$claudex_proxy_service_label" \
    "$claudex_proxy_service_unit" 2>/dev/null)" || return 1
  [[ "$loaded_definition" == "$claudex_proxy_service_file" ]] || return 1
  current_pid="$(managed_service_main_pid_value \
    "$platform" "$claudex_proxy_service_label" \
    "$claudex_proxy_service_unit" 2>/dev/null)" || return 1
  [[ "$current_pid" == 0 ]] && return 0
  pid_owns_loopback_listener \
    "$current_pid" "$PRIOR_ROUTE_PROXY_PORT"
}

managed_target_matches_definition_or_absent() {
  local service_file="$1"
  local service_label="$2"
  local service_unit="$3"
  local target_state loaded_definition
  target_state="$(managed_service_target_state \
    "$platform" "$service_label" "$service_unit")" || return 1
  [[ "$target_state" == absent ]] && return 0
  loaded_definition="$(managed_service_definition_path \
    "$platform" "$service_label" "$service_unit" 2>/dev/null)" || return 1
  [[ "$loaded_definition" == "$service_file" ]]
}

managed_listener_is_owned() {
  local service_file="$1"
  local service_label="$2"
  local service_unit="$3"
  local port="$4"
  local service_pid
  managed_service_target_is_loaded \
    "$platform" "$service_label" "$service_unit" || return 1
  [[ "$(managed_service_definition_path \
    "$platform" "$service_label" "$service_unit" 2>/dev/null)" == \
    "$service_file" ]] || return 1
  service_pid="$(managed_service_main_pid \
    "$platform" "$service_label" "$service_unit")" || return 1
  pid_owns_loopback_listener "$service_pid" "$port"
}

managed_target_matches_definition_or_absent \
  "$service_file" "$cliproxy_service_label" "$cliproxy_service_unit" || \
  workflow_die "refusing to replace loaded unknown CLIProxyAPI target"
managed_target_matches_definition_or_absent \
  "$leanctx_proxy_service_file" "$leanctx_proxy_service_label" \
  "$leanctx_proxy_service_unit" || \
  workflow_die "refusing to replace loaded unknown LeanCTX proxy target"
cliproxy_listener_owned=false
if [[ "$CLIPROXY_PORT" == "$PRIOR_CLIPROXY_PORT" ]] && \
   [[ "$cliproxy_service_owned" == true ]] && \
   managed_listener_is_owned \
     "$service_file" "$cliproxy_service_label" "$cliproxy_service_unit" \
     "$CLIPROXY_PORT"; then
  cliproxy_listener_owned=true
fi
leanctx_proxy_listener_owned=false
if [[ "$LEANCTX_PROXY_PORT" == "$PRIOR_LEANCTX_PROXY_PORT" ]] && \
   [[ "$leanctx_proxy_service_owned" == true ]] && \
   managed_listener_is_owned \
     "$leanctx_proxy_service_file" "$leanctx_proxy_service_label" \
     "$leanctx_proxy_service_unit" "$LEANCTX_PROXY_PORT" && \
   leanctx_proxy_endpoint_ready_at "$LEANCTX_PROXY_PORT"; then
  leanctx_proxy_listener_owned=true
fi
prior_claudex_config="$(model_config_file \
  "$WORKFLOW_DATA_ROOT" claudex.toml)"
prior_controller_model=
if [[ -f "$prior_claudex_config" ]]; then
  prior_controller_model="$(claudex_config_default_model \
    "$prior_claudex_config" 2>/dev/null || true)"
fi
claudex_proxy_listener_owned=false
claudex_proxy_port_owned=false
claudex_proxy_prior_manager_pid=
if [[ "$claudex_proxy_service_owned" == true ]]; then
  if [[ "$claudex_proxy_manager_target_state" == loaded ]]; then
    claudex_proxy_prior_manager_pid="$(managed_service_main_pid_value \
      "$platform" "$claudex_proxy_service_label" \
      "$claudex_proxy_service_unit" 2>/dev/null)" || workflow_die \
      "Orichum route proxy manager PID could not be inspected safely"
  fi
  if [[ "$claudex_proxy_prior_manager_pid" =~ ^[1-9][0-9]*$ ]]; then
    if pid_owns_loopback_listener \
        "$claudex_proxy_prior_manager_pid" \
        "$PRIOR_ROUTE_PROXY_PORT"; then
      if [[ "$ROUTE_PROXY_LISTEN_PORT" == \
            "$PRIOR_ROUTE_PROXY_PORT" ]]; then
        claudex_proxy_port_owned=true
      fi
      if [[ "$claudex_proxy_port_owned" == true ]] && \
         [[ -n "$prior_controller_model" ]] && \
         claudex_proxy_endpoint_ready_at \
           "$PRIOR_ROUTE_PROXY_PORT" "$prior_controller_model"; then
        claudex_proxy_listener_owned=true
      fi
    else
      workflow_die \
        "refusing to stop ownership-drifted Orichum route proxy runtime PID $claudex_proxy_prior_manager_pid"
    fi
  fi
fi
interactive_install=false
if [[ -t 0 && -t 1 ]]; then
  interactive_install=true
fi
CLIPROXY_PORT="$(select_service_port \
  CLIProxyAPI ORICHUM_CLIPROXY_PORT "$CLIPROXY_PORT" \
  "$cliproxy_listener_owned" "$interactive_install")" || exit 1
LEANCTX_PROXY_PORT="$(select_service_port \
  'LeanCTX proxy' ORICHUM_LEANCTX_PROXY_PORT "$LEANCTX_PROXY_PORT" \
  "$leanctx_proxy_listener_owned" "$interactive_install" \
  "$CLIPROXY_PORT" "$CLAUDEX_PROXY_PORT" \
  "$ROUTE_PROXY_LISTEN_PORT")" || exit 1
ROUTE_PROXY_LISTEN_PORT="$(select_service_port \
  'Orichum route proxy' ORICHUM_ROUTE_PROXY_PORT "$ROUTE_PROXY_LISTEN_PORT" \
  "$claudex_proxy_port_owned" "$interactive_install" \
  "$CLIPROXY_PORT" "$CLAUDEX_PROXY_PORT" "$LEANCTX_PROXY_PORT")" || exit 1
ports_changed=false
if [[ "$CLIPROXY_PORT" != "$PRIOR_CLIPROXY_PORT" ]] || \
   [[ "$CLAUDEX_PROXY_PORT" != "$PERSISTED_CLAUDEX_PROXY_PORT" ]] || \
   [[ "$ROUTE_PROXY_LISTEN_PORT" != "$PRIOR_ROUTE_PROXY_PORT" ]] || \
   [[ "$LEANCTX_PROXY_PORT" != "$PRIOR_LEANCTX_PROXY_PORT" ]]; then
  ports_changed=true
fi
service_ports_path="$(service_ports_file "$WORKFLOW_DATA_ROOT")"

configured_management_secret="$management_key"
if [[ -f "$WORKFLOW_DATA_ROOT/cliproxy.yaml" && \
      ! -L "$WORKFLOW_DATA_ROOT/cliproxy.yaml" ]]; then
  observed_management_secret="$(sed -n \
    's/^[[:space:]]*secret-key:[[:space:]]*"\([^"]*\)"[[:space:]]*$/\1/p' \
    "$WORKFLOW_DATA_ROOT/cliproxy.yaml" | head -1)"
  if [[ "$observed_management_secret" =~ ^\$2[a-z]\$[0-9]{2}\$.{53}$ ]]; then
    configured_management_secret="$observed_management_secret"
  fi
fi
render_cliproxy_config \
  "$desired_cliproxy_config" "$WORKFLOW_DATA_ROOT/auth" "$CLIPROXY_PORT" \
  "$configured_management_secret"
chmod 0600 "$desired_cliproxy_config"
(
  cd "$WORKFLOW_ROOT"
  workflow_python -I -B - \
    "$WORKFLOW_ROOT" "$CLIPROXY_PORT" "$LEANCTX_PROXY_PORT" \
    >"$desired_leanctx_proxy_config" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
from integrations.common.leanctx_contract import proxy_config_bytes

sys.stdout.buffer.write(
    proxy_config_bytes(int(sys.argv[2]), int(sys.argv[3]))
)
PY
) || workflow_die "LeanCTX proxy configuration could not be rendered"
chmod 0600 "$desired_leanctx_proxy_config"

probe_leanctx_proxy() (
  local probe_root="$installer_temp/leanctx-proxy-probe"
  local probe_port probe_pid= probe_ready=false
  install -d -m 0700 \
    "$probe_root/config" "$probe_root/state" \
    "$probe_root/cache" "$probe_root/data"
  install -m 0600 "$desired_leanctx_proxy_config" \
    "$probe_root/config/config.toml"
  probe_port="$(next_available_port \
    "$CLIPROXY_PORT" "$CLAUDEX_PROXY_PORT" \
    "$ROUTE_PROXY_LISTEN_PORT" "$LEANCTX_PROXY_PORT")" || return 1
  cleanup_leanctx_proxy_probe() {
    if [[ -n "$probe_pid" ]] && kill -0 "$probe_pid" 2>/dev/null; then
      kill "$probe_pid" 2>/dev/null || true
      wait "$probe_pid" 2>/dev/null || true
    fi
  }
  trap cleanup_leanctx_proxy_probe EXIT
  LEAN_CTX_CONFIG_DIR="$probe_root/config" \
  LEAN_CTX_STATE_DIR="$probe_root/state" \
  LEAN_CTX_CACHE_DIR="$probe_root/cache" \
  LEAN_CTX_DATA_DIR="$probe_root/data/lean-ctx" \
  LEAN_CTX_RULES_INJECTION=off \
  XDG_DATA_HOME="$probe_root/data" \
    "$leanctx_candidate" config validate >/dev/null || return 1
  LEAN_CTX_CONFIG_DIR="$probe_root/config" \
  LEAN_CTX_STATE_DIR="$probe_root/state" \
  LEAN_CTX_CACHE_DIR="$probe_root/cache" \
  LEAN_CTX_DATA_DIR="$probe_root/data/lean-ctx" \
  LEAN_CTX_HEADLESS=1 LEAN_CTX_MINIMAL=1 \
  LEAN_CTX_RULES_INJECTION=off \
  XDG_DATA_HOME="$probe_root/data" \
    "$leanctx_candidate" proxy start "--port=$probe_port" \
    >"$probe_root/proxy.log" 2>&1 &
  probe_pid=$!
  for _ in {1..30}; do
    kill -0 "$probe_pid" 2>/dev/null || break
    if leanctx_proxy_endpoint_ready_at "$probe_port"; then
      probe_ready=true
      break
    fi
    sleep 0.1
  done
  [[ "$probe_ready" == true ]]
)

probe_leanctx_proxy || \
  workflow_die "LeanCTX failed isolated proxy configuration and health checks"

probe_cliproxy_management() (
  local probe_root probe_port probe_pid= probe_binary
  probe_root="$installer_temp/cliproxy-management-probe"
  probe_port="$(next_available_port \
    "$CLIPROXY_PORT" "$CLAUDEX_PROXY_PORT" \
    "$ROUTE_PROXY_LISTEN_PORT")" || \
    return 1
  install -d -m 0700 "$probe_root/auth"
  printf '{"type":"codex","disabled":true}\n' \
    >"$probe_root/auth/orichum-capability-probe.json"
  chmod 0600 "$probe_root/auth/orichum-capability-probe.json"
  render_cliproxy_config \
    "$probe_root/config.yaml" "$probe_root/auth" "$probe_port" \
    "$management_key"
  chmod 0600 "$probe_root/config.yaml"
  probe_binary="$(jq -r '.staged_path' <<<"$cliproxy_state")"
  if [[ "$probe_binary" == null ]]; then
    probe_binary="$WORKFLOW_DATA_ROOT/bin/cli-proxy-api"
  fi
  cleanup_management_probe() {
    if [[ -n "$probe_pid" ]] && kill -0 "$probe_pid" 2>/dev/null; then
      kill "$probe_pid" 2>/dev/null || true
      wait "$probe_pid" 2>/dev/null || true
    fi
  }
  trap cleanup_management_probe EXIT
  "$probe_binary" --config "$probe_root/config.yaml" \
    >"$probe_root/probe.log" 2>&1 &
  probe_pid=$!
  "$ORICHUM_PYTHON" - "$probe_port" "$management_key" \
    "$probe_root/auth/orichum-capability-probe.json" <<'PY'
import http.client
import json
from pathlib import Path
import sys
import time

port = int(sys.argv[1])
key = sys.argv[2]
credential = Path(sys.argv[3])
headers = {"X-Management-Key": key}
deadline = time.monotonic() + 15
while True:
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        connection.request(
            "GET", "/v0/management/auth-files", headers=headers
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        if response.status == 200:
            break
    except OSError:
        pass
    if time.monotonic() >= deadline:
        raise SystemExit("management API did not become ready")
    time.sleep(0.1)

payload = json.dumps(
    {
        "name": credential.name,
        "prefix": "orichum-capability",
        "priority": 7,
    },
    separators=(",", ":"),
).encode()
connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
connection.request(
    "PATCH",
    "/v0/management/auth-files/fields",
    body=payload,
    headers={
        **headers,
        "Content-Type": "application/json",
        "Content-Length": str(len(payload)),
    },
)
response = connection.getresponse()
response.read()
connection.close()
if response.status != 200:
    raise SystemExit(f"management PATCH returned {response.status}")
deadline = time.monotonic() + 3
while time.monotonic() < deadline:
    document = json.loads(credential.read_text(encoding="utf-8"))
    if (
        document.get("prefix") == "orichum-capability"
        and document.get("priority") == 7
    ):
        raise SystemExit(0)
    time.sleep(0.05)
raise SystemExit("management PATCH did not persist exact fields")
PY
)
route_proxy_runtime_digest="$(
  verified_route_runtime_digest \
    "$WORKFLOW_ROOT" "$orichum_python_candidate" \
    "$orichum_python_version" "$installer_temp/route-runtime"
)" || workflow_die "Orichum route runtime could not be fingerprinted"
[[ "$route_proxy_runtime_digest" =~ ^[a-f0-9]{64}$ ]] || \
  workflow_die "Orichum route runtime fingerprint is invalid"
if [[ "$platform" == darwin ]]; then
  render_launch_agent "$desired_service_file" "$WORKFLOW_DATA_ROOT"
  plutil -lint "$desired_service_file" >/dev/null
  render_leanctx_proxy_launch_agent \
    "$leanctx_proxy_desired_service_file" "$WORKFLOW_DATA_ROOT" \
    "$LEANCTX_PROXY_PORT"
  plutil -lint "$leanctx_proxy_desired_service_file" >/dev/null
  render_claudex_proxy_launch_agent \
    "$claudex_proxy_desired_service_file" "$WORKFLOW_DATA_ROOT" \
    "$WORKFLOW_ROOT" \
    "$ROUTE_PROXY_LISTEN_PORT" "$LEANCTX_PROXY_PORT" "$CLIPROXY_PORT" \
    "$route_proxy_runtime_digest"
  plutil -lint "$claudex_proxy_desired_service_file" >/dev/null
else
  render_systemd_user_unit "$desired_service_file" "$WORKFLOW_DATA_ROOT"
  render_leanctx_proxy_systemd_user_unit \
    "$leanctx_proxy_desired_service_file" "$WORKFLOW_DATA_ROOT" \
    "$LEANCTX_PROXY_PORT"
  render_claudex_proxy_systemd_user_unit \
    "$claudex_proxy_desired_service_file" "$WORKFLOW_DATA_ROOT" \
    "$WORKFLOW_ROOT" \
    "$ROUTE_PROXY_LISTEN_PORT" "$LEANCTX_PROXY_PORT" "$CLIPROXY_PORT" \
    "$route_proxy_runtime_digest"
fi

cliproxy_config_changed="$(file_change_state \
  "$desired_cliproxy_config" "$WORKFLOW_DATA_ROOT/cliproxy.yaml")"
cliproxy_service_changed="$(file_change_state "$desired_service_file" "$service_file")"
leanctx_proxy_config_path="$WORKFLOW_DATA_ROOT/leanctx/proxy/config/config.toml"
leanctx_proxy_config_changed="$(file_change_state \
  "$desired_leanctx_proxy_config" "$leanctx_proxy_config_path")"
leanctx_proxy_service_changed="$(file_change_state \
  "$leanctx_proxy_desired_service_file" "$leanctx_proxy_service_file")"
claudex_proxy_service_changed="$(file_change_state \
  "$claudex_proxy_desired_service_file" "$claudex_proxy_service_file")"
if [[ "$cliproxy_decision" != reused || \
      "$cliproxy_binary_changed" == true || \
      "$cliproxy_config_changed" == changed || \
      "$cliproxy_service_changed" == changed ]]; then
  probe_cliproxy_management || workflow_die \
    "CLIProxyAPI failed the required management PATCH/readback capability probe"
fi
claudex_proxy_port_changed=false
if [[ "$ROUTE_PROXY_LISTEN_PORT" != "$PRIOR_ROUTE_PROXY_PORT" ]]; then
  claudex_proxy_port_changed=true
fi
leanctx_proxy_restart_required=false
if [[ "$leanctx_binary_changed" == true ]] || \
   [[ "$leanctx_proxy_config_changed" == changed ]] || \
   [[ "$leanctx_proxy_service_changed" == changed ]] || \
   [[ "$leanctx_proxy_listener_owned" != true ]]; then
  leanctx_proxy_restart_required=true
fi

cliproxy_is_ready() {
  local port="${1:-$CLIPROXY_PORT}"
  curl -fsS --connect-timeout 1 --max-time 2 \
    "http://127.0.0.1:$port/v1/models" 2>/dev/null | \
    cliproxy_models_response_is_ready /dev/stdin
}

wait_for_cliproxy() {
  local port="${1:-$CLIPROXY_PORT}"
  for _ in {1..30}; do
    if cliproxy_is_ready "$port"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

cliproxy_ready_before=true
cliproxy_is_ready || cliproxy_ready_before=false
cliproxy_restart_required=false
if [[ "$cliproxy_binary_changed" == true ]] || \
   [[ "$cliproxy_config_changed" == changed ]] || \
   [[ "$cliproxy_service_changed" == changed ]] || \
   [[ "$cliproxy_ready_before" == false ]]; then
  cliproxy_restart_required=true
fi

model_config_root_path="$(model_config_root "$WORKFLOW_DATA_ROOT")"
claude_settings_path="$WORKFLOW_DATA_ROOT/claude-config/settings.json"
prior_model_generation=
prior_model_generation_snapshot=
endpoint_lock_owned=false
endpoint_lock_token=
snapshot_path "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api" "$snapshot_dir" cliproxy-binary
snapshot_path "$WORKFLOW_DATA_ROOT/bin/claudex" "$snapshot_dir" claudex-binary
snapshot_path "$WORKFLOW_DATA_ROOT/bin/lean-ctx" "$snapshot_dir" leanctx-binary
snapshot_path "$WORKFLOW_DATA_ROOT/cliproxy.yaml" "$snapshot_dir" cliproxy-config
snapshot_path "$service_file" "$snapshot_dir" cliproxy-service
snapshot_path "$leanctx_proxy_config_path" \
  "$snapshot_dir" leanctx-proxy-config
snapshot_path "$leanctx_proxy_service_file" \
  "$snapshot_dir" leanctx-proxy-service
snapshot_path "$claudex_proxy_service_file" \
  "$snapshot_dir" claudex-proxy-service
snapshot_path "$service_ports_path" "$snapshot_dir" service-ports
snapshot_path "$USER_BIN_DIR/orichum" \
  "$snapshot_dir" orichum-launcher
snapshot_path "$claude_settings_path" "$snapshot_dir" claude-settings
snapshot_path "$completion_zsh_path" "$snapshot_dir" completion-zsh
snapshot_path "$completion_bash_path" "$snapshot_dir" completion-bash
snapshot_path "$completion_fish_path" "$snapshot_dir" completion-fish
snapshot_path "$completion_fish_record" \
  "$snapshot_dir" completion-fish-record
if [[ -n "$completion_prior_fish_path" && \
      "$completion_prior_fish_path" != "$completion_fish_path" ]]; then
  snapshot_path "$completion_prior_fish_path" \
    "$snapshot_dir" completion-fish-prior
fi
snapshot_path "$completion_zsh_profile" \
  "$snapshot_dir" completion-zshrc
snapshot_path "$completion_bash_profile" \
  "$snapshot_dir" completion-bashrc
snapshot_path "$completion_bash_login_profile" \
  "$snapshot_dir" completion-bash-login
cliproxy_transaction_active=false
claudex_proxy_transaction_active=false
claudex_proxy_runtime_mutated=false
endpoint_transaction_active=true
orichum_launcher_mutated=false
leanctx_transaction_active=false
leanctx_proxy_transaction_active=false
leanctx_proxy_runtime_mutated=false
install_state_transaction_active=false
claude_settings_transaction_active=false
completion_transaction_active=false
completion_installed_snapshotted=false
if [[ "$leanctx_binary_changed" == true ]]; then
  leanctx_transaction_active=true
fi
if [[ "$leanctx_proxy_restart_required" == true ]]; then
  leanctx_proxy_transaction_active=true
fi

restore_claudex_proxy_service() {
  local recovery_ready=true restored_model
  [[ "$claudex_proxy_transaction_active" == true ]] || return 0

  restore_snapshot "$claudex_proxy_service_file" \
    "$snapshot_dir" claudex-proxy-service || recovery_ready=false
  snapshot_path_matches "$claudex_proxy_service_file" \
    "$snapshot_dir" claudex-proxy-service || recovery_ready=false
  claudex_proxy_service_is_owned \
    "$claudex_proxy_service_file" "$prior_route_data_root" \
    "$prior_route_workflow_root" || recovery_ready=false

  if [[ "$recovery_ready" != true ]] || \
     [[ "${claudex_proxy_recovery_prerequisites_ready:-false}" != true ]]; then
    return 1
  fi

  if [[ -f "$snapshot_dir/claudex-proxy-service.present" ]]; then
    restored_model="$(claudex_config_default_model \
      "$(model_config_file "$WORKFLOW_DATA_ROOT" claudex.toml)" \
      2>/dev/null || true)"
    [[ -n "$restored_model" ]] || return 1
    if [[ "${claudex_proxy_runtime_mutated:-false}" == true ]]; then
      if [[ "$platform" == darwin ]]; then
        launchctl enable \
          "gui/$(id -u)/$claudex_proxy_service_label" \
          >/dev/null 2>&1 || recovery_ready=false
        if [[ "$recovery_ready" == true ]]; then
          launchctl bootstrap "gui/$(id -u)" \
            "$claudex_proxy_service_file" \
            >/dev/null 2>&1 || recovery_ready=false
        fi
      else
        claudex_proxy_loaded_target_is_expected || return 1
        systemctl --user daemon-reload \
          >/dev/null 2>&1 || recovery_ready=false
        if [[ "$recovery_ready" == true ]]; then
          claudex_proxy_loaded_target_is_expected || recovery_ready=false
        fi
        if [[ "$recovery_ready" == true ]]; then
          systemctl --user enable "$claudex_proxy_service_unit" \
            >/dev/null 2>&1 || recovery_ready=false
        fi
        if [[ "$recovery_ready" == true ]]; then
          systemctl --user restart "$claudex_proxy_service_unit" \
            >/dev/null 2>&1 || recovery_ready=false
        fi
      fi
    fi
    if [[ "$recovery_ready" == true ]]; then
      wait_for_claudex_proxy \
        "$PRIOR_ROUTE_PROXY_PORT" "$restored_model" || recovery_ready=false
    fi
  elif [[ "$platform" == systemd ]]; then
    systemctl --user disable "$claudex_proxy_service_unit" \
      >/dev/null 2>&1 || true
    systemctl --user daemon-reload >/dev/null 2>&1 || recovery_ready=false
  fi

  [[ "$recovery_ready" == true ]]
}

rollback_install_transaction() {
  local rollback_ready=true

  if [[ "${claudex_proxy_runtime_mutated:-false}" == true ]]; then
    if claudex_proxy_loaded_target_is_expected; then
      if [[ "$platform" == darwin ]]; then
        launchctl bootout "gui/$(id -u)" "$claudex_proxy_service_file" \
          >/dev/null 2>&1 || true
      else
        systemctl --user stop "$claudex_proxy_service_unit" \
          >/dev/null 2>&1 || true
      fi
    else
      rollback_ready=false
    fi
  fi

  if [[ "${leanctx_proxy_runtime_mutated:-false}" == true ]]; then
    if [[ "$platform" == darwin ]]; then
      launchctl bootout "gui/$(id -u)" "$leanctx_proxy_service_file" \
        >/dev/null 2>&1 || true
    else
      systemctl --user stop "$leanctx_proxy_service_unit" \
        >/dev/null 2>&1 || true
    fi
  fi

  if [[ "${config_transaction_active:-false}" == true ]]; then
    rollback_installed_control_plane \
      "$ORICHUM_PYTHON" "$WORKFLOW_ROOT" \
      "$INSTALLED_CONFIG_ROOT" "$control_plane_journal" \
      "$lifecycle_lock_path" "$WORKFLOW_LOCK_FD" || \
      rollback_ready=false
  fi

  if [[ "${claude_settings_transaction_active:-false}" == true ]]; then
    if snapshot_path_matches \
        "$claude_settings_path" "$snapshot_dir" claude-settings; then
      :
    else
      local claude_settings_ready=true
      local prior_settings=
      if [[ -f "$snapshot_dir/claude-settings.present" ]]; then
        prior_settings="$snapshot_dir/claude-settings.data"
      elif [[ ! -f "$snapshot_dir/claude-settings.absent" ]]; then
        claude_settings_ready=false
      fi
      if [[ "$claude_settings_ready" == true ]]; then
        (
          cd "$WORKFLOW_ROOT"
          workflow_python - \
            "$WORKFLOW_ROOT" "$claude_settings_path" \
            "$snapshot_dir/claude-settings-installed.data" \
            "$prior_settings" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.install_control_plane import (
    InstallControlPlaneError,
    rollback_claude_settings,
)

try:
    rollback_claude_settings(
        Path(sys.argv[2]),
        Path(sys.argv[3]),
        Path(sys.argv[4]) if sys.argv[4] else None,
    )
except InstallControlPlaneError as error:
    raise SystemExit(str(error))
PY
        ) || {
          claude_settings_ready=false
          rollback_ready=false
        }
      fi
      if [[ "$claude_settings_ready" == true ]]; then
        snapshot_path_matches \
          "$claude_settings_path" "$snapshot_dir" claude-settings || \
          rollback_ready=false
      else
        rollback_ready=false
      fi
    fi
  fi

  if [[ "${python_transaction_active:-false}" == true ]]; then
    rollback_python_activation || rollback_ready=false
  fi

  if [[ "${leanctx_transaction_active:-false}" == true ]]; then
    restore_snapshot "$WORKFLOW_DATA_ROOT/bin/lean-ctx" \
      "$snapshot_dir" leanctx-binary || rollback_ready=false
    snapshot_path_matches "$WORKFLOW_DATA_ROOT/bin/lean-ctx" \
      "$snapshot_dir" leanctx-binary || rollback_ready=false
  fi
  if [[ "${leanctx_proxy_transaction_active:-false}" == true ]]; then
    restore_snapshot "$leanctx_proxy_config_path" \
      "$snapshot_dir" leanctx-proxy-config || rollback_ready=false
    restore_snapshot "$leanctx_proxy_service_file" \
      "$snapshot_dir" leanctx-proxy-service || rollback_ready=false
  fi

  if [[ "$cliproxy_transaction_active" == true ]]; then
    if [[ "$platform" == darwin ]]; then
      launchctl bootout "gui/$(id -u)" "$service_file" >/dev/null 2>&1 || true
    else
      systemctl --user stop orichum-cliproxy.service >/dev/null 2>&1 || true
    fi

    restore_snapshot "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api" \
      "$snapshot_dir" cliproxy-binary || rollback_ready=false
    restore_snapshot "$WORKFLOW_DATA_ROOT/bin/claudex" \
      "$snapshot_dir" claudex-binary || rollback_ready=false
    restore_snapshot "$WORKFLOW_DATA_ROOT/cliproxy.yaml" \
      "$snapshot_dir" cliproxy-config || rollback_ready=false
    restore_snapshot "$service_file" \
      "$snapshot_dir" cliproxy-service || rollback_ready=false

    if [[ -f "$snapshot_dir/cliproxy-service.present" ]]; then
      if [[ "$platform" == darwin ]]; then
        launchctl bootstrap "gui/$(id -u)" "$service_file" >/dev/null 2>&1 || \
          rollback_ready=false
      else
        systemctl --user daemon-reload >/dev/null 2>&1 || rollback_ready=false
        systemctl --user enable orichum-cliproxy.service >/dev/null 2>&1 || \
          rollback_ready=false
        systemctl --user restart orichum-cliproxy.service >/dev/null 2>&1 || \
          rollback_ready=false
      fi
      wait_for_cliproxy "$PRIOR_CLIPROXY_PORT" || rollback_ready=false
    elif [[ "$platform" == systemd ]]; then
      systemctl --user disable orichum-cliproxy.service >/dev/null 2>&1 || true
      systemctl --user daemon-reload >/dev/null 2>&1 || rollback_ready=false
    fi
  fi

  if [[ "${leanctx_proxy_transaction_active:-false}" == true ]]; then
    if [[ -f "$snapshot_dir/leanctx-proxy-service.present" ]]; then
      if [[ "$platform" == darwin ]]; then
        launchctl bootstrap "gui/$(id -u)" "$leanctx_proxy_service_file" \
          >/dev/null 2>&1 || rollback_ready=false
      else
        systemctl --user daemon-reload >/dev/null 2>&1 || rollback_ready=false
        systemctl --user enable "$leanctx_proxy_service_unit" \
          >/dev/null 2>&1 || rollback_ready=false
        systemctl --user restart "$leanctx_proxy_service_unit" \
          >/dev/null 2>&1 || rollback_ready=false
      fi
      wait_for_leanctx_proxy "$PRIOR_LEANCTX_PROXY_PORT" || \
        rollback_ready=false
    elif [[ "$platform" == systemd ]]; then
      systemctl --user disable "$leanctx_proxy_service_unit" \
        >/dev/null 2>&1 || true
      systemctl --user daemon-reload >/dev/null 2>&1 || rollback_ready=false
    fi
  fi

  if [[ "${endpoint_transaction_active:-false}" == true ]]; then
    if [[ "${endpoint_lock_owned:-false}" == true ]]; then
      restore_model_config_generation \
        "$WORKFLOW_DATA_ROOT" "$prior_model_generation" \
        "$prior_model_generation_snapshot" || rollback_ready=false
    fi
    restore_snapshot "$service_ports_path" \
      "$snapshot_dir" service-ports || rollback_ready=false
    snapshot_path_matches "$service_ports_path" \
      "$snapshot_dir" service-ports || rollback_ready=false
  fi

  claudex_proxy_recovery_prerequisites_ready="$rollback_ready"
  run_rollback_if_active \
    "${claudex_proxy_transaction_active:-false}" \
    restore_claudex_proxy_service || rollback_ready=false

  if [[ "${endpoint_lock_owned:-false}" == true ]]; then
    release_endpoint_config_lock \
      "$WORKFLOW_DATA_ROOT" "$endpoint_lock_token" || rollback_ready=false
    endpoint_lock_owned=false
  fi

  if [[ "${completion_transaction_active:-false}" == true ]]; then
    local completion_index
    local -a completion_targets=(
      "$completion_zsh_path"
      "$completion_bash_path"
      "$completion_fish_path"
      "$completion_fish_record"
      "$completion_zsh_profile"
      "$completion_bash_profile"
      "$completion_bash_login_profile"
    )
    local -a completion_names=(
      completion-zsh
      completion-bash
      completion-fish
      completion-fish-record
      completion-zshrc
      completion-bashrc
      completion-bash-login
    )
    if [[ -n "$completion_prior_fish_path" && \
          "$completion_prior_fish_path" != "$completion_fish_path" ]]; then
      completion_targets+=("$completion_prior_fish_path")
      completion_names+=(completion-fish-prior)
    fi
    for completion_index in "${!completion_targets[@]}"; do
      if [[ "${completion_installed_snapshotted:-false}" == true ]] && \
         ! snapshot_path_matches \
           "${completion_targets[$completion_index]}" "$snapshot_dir" \
           "${completion_names[$completion_index]}-installed"; then
        printf 'WARNING: retained completion path changed during rollback: %s\n' \
          "${completion_targets[$completion_index]}" >&2
        rollback_ready=false
        continue
      fi
      restore_snapshot \
        "${completion_targets[$completion_index]}" "$snapshot_dir" \
        "${completion_names[$completion_index]}" || rollback_ready=false
      snapshot_path_matches \
        "${completion_targets[$completion_index]}" "$snapshot_dir" \
        "${completion_names[$completion_index]}" || rollback_ready=false
    done
  fi

  if [[ "${orichum_launcher_mutated:-false}" == true ]]; then
    restore_snapshot "$USER_BIN_DIR/orichum" \
      "$snapshot_dir" orichum-launcher || rollback_ready=false
    snapshot_path_matches "$USER_BIN_DIR/orichum" \
      "$snapshot_dir" orichum-launcher || rollback_ready=false
  fi

  if [[ "${install_state_transaction_active:-false}" == true ]]; then
    if [[ "$rollback_ready" == true ]]; then
      restore_snapshot "$install_state_path" \
        "$snapshot_dir" install-state || rollback_ready=false
      snapshot_path_matches "$install_state_path" \
        "$snapshot_dir" install-state || rollback_ready=false
    else
      if [[ -e "$install_state_path" || -L "$install_state_path" ]]; then
        if [[ -f "$install_state_path" && \
              ! -L "$install_state_path" && \
              "$(path_uid "$install_state_path")" == "$(id -u)" ]]; then
          rm -f -- "$install_state_path" || rollback_ready=false
        else
          rollback_ready=false
        fi
      fi
    fi
  fi

  rollback_consolidated_runtime_and_home || rollback_ready=false
  [[ "$rollback_ready" == true ]]
}

WORKFLOW_ROLLBACK_HANDLER=rollback_install_transaction
WORKFLOW_TRANSACTION_ACTIVE=true
config_transaction_active=false

endpoint_lock_token="$$:$RANDOM:$RANDOM"
acquire_endpoint_config_lock \
  "$WORKFLOW_DATA_ROOT" "$endpoint_lock_token" || \
  workflow_die "could not serialize endpoint model publication"
endpoint_lock_owned=true
prior_model_generation="$(readlink \
  "$model_config_root_path/current" 2>/dev/null || true)"
if [[ -n "$prior_model_generation" ]]; then
  prior_model_generation_snapshot="$snapshot_dir/prior-model-generation"
  cp -pPR "$model_config_root_path/$prior_model_generation" \
    "$prior_model_generation_snapshot" || \
    workflow_die "prior model configuration could not be snapshotted"
fi

routing_input_descriptor="$installer_temp/routing-input"
cliproxy_desired_artifact="$(
  if [[ "$cliproxy_binary_changed" == true ]]; then
    sha256_file "$(jq -r '.staged_path' <<<"$cliproxy_state")"
  else
    sha256_file "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api"
  fi
)"
claudex_desired_artifact="$(
  if [[ "$claudex_binary_changed" == true ]]; then
    sha256_file "$(jq -r '.staged_path' <<<"$claudex_state")"
  else
    sha256_file "$WORKFLOW_DATA_ROOT/bin/claudex"
  fi
)"
routing_input_sha="$(
  verified_routing_input_fingerprint \
    "$routing_input_descriptor" \
    "$cliproxy_desired_artifact" "$claudex_desired_artifact" \
    "$route_proxy_runtime_digest" \
    "$CLIPROXY_PORT" "$CLAUDEX_PROXY_PORT" "$ROUTE_PROXY_LISTEN_PORT" \
    "$LEANCTX_PROXY_PORT" \
    "$candidate_config_root/accounts.json" \
    "$candidate_config_root/jira-profiles.json" \
    "$candidate_config_root/model-stacks.json" \
    "$candidate_config_root/plugins.json" \
    "$candidate_config_root/projects.json" \
    "$candidate_config_root/providers.json" \
    "$candidate_config_root/runtime.json" \
    "$candidate_config_root/controller-policy.md" \
    "$WORKFLOW_ROOT/config/model-stacks.json" \
    "$WORKFLOW_ROOT/config/jira-profiles.json" \
    "$WORKFLOW_ROOT/config/plugins.json" \
    "$WORKFLOW_ROOT/config/projects.json" \
    "$WORKFLOW_ROOT/config/providers.json" \
    "$WORKFLOW_ROOT/config/runtime.json" \
    "$WORKFLOW_ROOT/config/controller-policy.md" \
    "$WORKFLOW_ROOT/controller/settings.json" \
    "$desired_cliproxy_config" \
    "$desired_leanctx_proxy_config" \
    "$desired_service_file" "$leanctx_proxy_desired_service_file" \
    "$claudex_proxy_desired_service_file"
)" || workflow_die "routing input fingerprint failed"

routing_runtime_artifact() {
  verified_routing_runtime_artifact \
    "$WORKFLOW_DATA_ROOT" "$INSTALLED_CONFIG_ROOT" \
    "$service_file" "$leanctx_proxy_service_file" \
    "$claudex_proxy_service_file" \
    "$installer_temp/routing-artifact"
}
routing_current_artifact="$empty_artifact_sha"
if observed_routing_artifact="$(routing_runtime_artifact 2>/dev/null)"; then
  routing_current_artifact="$observed_routing_artifact"
fi
routing_decision=upgraded
if [[ "$prior_install_state_verified" == true ]]; then
  routing_decision="$(
    decide_install_component \
      "$prior_install_state" routing \
      1 orichum:routing "$routing_current_artifact" \
      "$routing_input_sha" "$routing_probe_sha"
  )"
fi

preflight_claudex_proxy() (
  local preflight_port preflight_pid= preflight_ready=false
  local response_file="$installer_temp/claudex-proxy-preflight-models.json"
  preflight_port="$(next_available_port \
    "$ROUTE_PROXY_LISTEN_PORT" "$CLAUDEX_PROXY_PORT" \
    "$CLIPROXY_PORT" "$LEANCTX_PROXY_PORT")" || \
    return 1
  cleanup_claudex_preflight() {
    if [[ -n "$preflight_pid" ]] && \
       kill -0 "$preflight_pid" 2>/dev/null; then
      kill "$preflight_pid" 2>/dev/null || true
      wait "$preflight_pid" 2>/dev/null || true
    fi
  }
  trap cleanup_claudex_preflight EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  "$WORKFLOW_DATA_ROOT/bin/orichum-route-proxy" \
    --port "$preflight_port" \
    --upstream-port "$LEANCTX_PROXY_PORT" \
    --catalog-port "$CLIPROXY_PORT" \
    --state-home "$WORKFLOW_DATA_ROOT/state" \
    --data-home "$WORKFLOW_DATA_ROOT" \
    >"$installer_temp/route-proxy-preflight.log" 2>&1 &
  preflight_pid=$!
  for _ in {1..30}; do
    kill -0 "$preflight_pid" 2>/dev/null || break
    if curl -fsS --connect-timeout 1 --max-time 2 \
        "http://127.0.0.1:$preflight_port/v1/models" \
        >"$response_file" 2>/dev/null && \
       claudex_proxy_models_response_is_ready \
         "$response_file" "$active_controller_model"; then
      preflight_ready=true
      break
    fi
    sleep 1
  done
  if [[ "$preflight_ready" != true ]]; then
    sed -n '1,160p' "$installer_temp/route-proxy-preflight.log" \
      >&2 || true
    return 1
  fi
)

preflight_claudex_translation_proxy() (
  local config_file="$1"
  local probe_home="$installer_temp/claudex-translation-home"
  local preflight_port preflight_pid='' preflight_ready=false
  local response_file="$installer_temp/claudex-translation-models.json"
  install -d -m 0700 \
    "$probe_home" "$probe_home/cache" "$probe_home/runtime"
  preflight_port="$(next_available_port \
    "$CLAUDEX_PROXY_PORT" "$ROUTE_PROXY_LISTEN_PORT" \
    "$CLIPROXY_PORT")" || \
    return 1
  cleanup_claudex_translation_preflight() {
    if [[ -n "$preflight_pid" ]] && \
       kill -0 "$preflight_pid" 2>/dev/null; then
      kill "$preflight_pid" 2>/dev/null || true
      wait "$preflight_pid" 2>/dev/null || true
    fi
  }
  trap cleanup_claudex_translation_preflight EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  HOME="$probe_home" \
  XDG_CACHE_HOME="$probe_home/cache" \
  XDG_RUNTIME_DIR="$probe_home/runtime" \
    "$WORKFLOW_DATA_ROOT/bin/claudex" \
      --config "$config_file" proxy start --port "$preflight_port" \
      >"$installer_temp/claudex-translation-preflight.log" 2>&1 &
  preflight_pid=$!
  for _ in {1..30}; do
    kill -0 "$preflight_pid" 2>/dev/null || break
    if curl -fsS --connect-timeout 1 --max-time 2 \
        "http://127.0.0.1:$preflight_port/health" 2>/dev/null | \
        rg -Fxq ok && \
       curl -fsS --connect-timeout 1 --max-time 2 \
        "http://127.0.0.1:$preflight_port/v1/models" \
        >"$response_file" 2>/dev/null && \
       claudex_proxy_models_response_is_ready \
         "$response_file" "$active_controller_model"; then
      preflight_ready=true
      break
    fi
    sleep 1
  done
  if [[ "$preflight_ready" != true ]]; then
    sed -n '1,160p' \
      "$installer_temp/claudex-translation-preflight.log" >&2 || true
    return 1
  fi
)

require_activation_port_available() {
  local service_name="$1"
  local port="$2"
  local activation_port_ready=false
  for _ in {1..150}; do
    if port_is_available "$port"; then
      activation_port_ready=true
      break
    fi
    sleep 0.1
  done
  [[ "$activation_port_ready" == true ]] || workflow_die \
    "$service_name activation port $port remained occupied; prior state will be restored"
}

print_install_progress \
  "$INSTALL_VERBOSE" '  Configuring services…' >&3
write_service_ports "$WORKFLOW_DATA_ROOT" \
  "$CLIPROXY_PORT" "$CLAUDEX_PROXY_PORT" "$ROUTE_PROXY_LISTEN_PORT" \
  "$LEANCTX_PROXY_PORT" || \
  workflow_die "service port configuration could not be saved"

if [[ "$cliproxy_binary_changed" == true ]] || \
   [[ "$claudex_binary_changed" == true ]] || \
   [[ "$cliproxy_config_changed" == changed ]] || \
   [[ "$cliproxy_service_changed" == changed ]] || \
   [[ "$cliproxy_restart_required" == true ]]; then
  cliproxy_transaction_active=true
fi
if [[ "$cliproxy_binary_changed" == true ]]; then
  activate_staged_file "$(jq -r '.staged_path' <<<"$cliproxy_state")" \
    "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api" 0755
fi
if [[ "$claudex_binary_changed" == true ]]; then
  activate_staged_file "$(jq -r '.staged_path' <<<"$claudex_state")" \
    "$WORKFLOW_DATA_ROOT/bin/claudex" 0755
fi
if [[ "$leanctx_binary_changed" == true ]]; then
  activate_staged_file "$(jq -r '.staged_path' <<<"$leanctx_state")" \
    "$WORKFLOW_DATA_ROOT/bin/lean-ctx" 0755
fi
if [[ "$leanctx_proxy_config_changed" == changed ]]; then
  activate_staged_file "$desired_leanctx_proxy_config" \
    "$leanctx_proxy_config_path" 0600
fi
if [[ "$leanctx_proxy_service_changed" == changed ]]; then
  activate_staged_file "$leanctx_proxy_desired_service_file" \
    "$leanctx_proxy_service_file" "$leanctx_proxy_service_mode"
fi
leanctx_proxy_service_is_owned \
  "$leanctx_proxy_service_file" "$WORKFLOW_DATA_ROOT" || \
  workflow_die "installed LeanCTX proxy service definition is not owned"
if [[ "$cliproxy_config_changed" == changed ]]; then
  activate_staged_file "$desired_cliproxy_config" "$WORKFLOW_DATA_ROOT/cliproxy.yaml" 0600
fi
if [[ "$cliproxy_service_changed" == changed ]]; then
  activate_staged_file "$desired_service_file" "$service_file" "$service_mode"
fi

if [[ "$cliproxy_restart_required" == true ]]; then
  printf 'WARNING: restarting a changed or unhealthy service may interrupt active Claudex sessions.\n' >&2
  if [[ "$platform" == darwin ]]; then
    launchctl bootout "gui/$(id -u)" "$service_file" >/dev/null 2>&1 || true
  else
    systemctl --user stop orichum-cliproxy.service >/dev/null 2>&1 || true
    systemctl --user daemon-reload
    systemctl --user enable orichum-cliproxy.service
  fi
  require_activation_port_available CLIProxyAPI "$CLIPROXY_PORT"
  if [[ "$platform" == darwin ]]; then
    launchctl bootstrap "gui/$(id -u)" "$service_file"
  else
    systemctl --user start orichum-cliproxy.service
  fi
  wait_for_cliproxy || workflow_die \
    "CLIProxyAPI failed readiness checks; previous service will be restored"
fi

if [[ "$leanctx_proxy_restart_required" == true ]]; then
  printf 'WARNING: restarting the shared LeanCTX proxy may interrupt one in-flight request across active sessions.\n' >&2
  leanctx_proxy_runtime_mutated=true
  if [[ "$platform" == darwin ]]; then
    launchctl bootout "gui/$(id -u)" "$leanctx_proxy_service_file" \
      >/dev/null 2>&1 || true
  else
    systemctl --user stop "$leanctx_proxy_service_unit" \
      >/dev/null 2>&1 || true
    systemctl --user daemon-reload
    systemctl --user enable "$leanctx_proxy_service_unit"
  fi
  require_activation_port_available \
    'LeanCTX proxy' "$LEANCTX_PROXY_PORT"
  if [[ "$platform" == darwin ]]; then
    launchctl bootstrap "gui/$(id -u)" "$leanctx_proxy_service_file"
  else
    systemctl --user start "$leanctx_proxy_service_unit"
  fi
  wait_for_leanctx_proxy || workflow_die \
    "LeanCTX proxy failed readiness checks; previous service will be restored"
fi

for launcher in orichum; do
  ln -sfn "$WORKFLOW_ROOT/bin/$launcher" "$USER_BIN_DIR/$launcher"
  orichum_launcher_mutated=true
done

completion_transaction_active=true
reconcile_orichum_completions \
  "$WORKFLOW_ROOT" "$ORICHUM_HOME_ROOT" \
  "$ORICHUM_CONFIG_ROOT" "$WORKFLOW_DATA_ROOT" \
  "$completion_bash_login_profile" || \
  workflow_die "shell completion reconciliation failed"
snapshot_path "$completion_zsh_path" \
  "$snapshot_dir" completion-zsh-installed
snapshot_path "$completion_bash_path" \
  "$snapshot_dir" completion-bash-installed
snapshot_path "$completion_fish_path" \
  "$snapshot_dir" completion-fish-installed
snapshot_path "$completion_fish_record" \
  "$snapshot_dir" completion-fish-record-installed
if [[ -n "$completion_prior_fish_path" && \
      "$completion_prior_fish_path" != "$completion_fish_path" ]]; then
  snapshot_path "$completion_prior_fish_path" \
    "$snapshot_dir" completion-fish-prior-installed
fi
snapshot_path "$completion_zsh_profile" \
  "$snapshot_dir" completion-zshrc-installed
snapshot_path "$completion_bash_profile" \
  "$snapshot_dir" completion-bashrc-installed
snapshot_path "$completion_bash_login_profile" \
  "$snapshot_dir" completion-bash-login-installed
completion_installed_snapshotted=true
completion_artifact="$(
  verified_orichum_completion_artifact "$ORICHUM_HOME_ROOT"
)" || workflow_die "installed completion artifact could not be fingerprinted"

source "$WORKFLOW_ROOT/discover-models.sh"
model_discovery_succeeded=true
model_discovery_performed=false
routing_action="$routing_decision"
discovery_entrypoint=discover_models_main_core
model_discovery_status=0
if [[ "$routing_decision" != reused ]]; then
  model_discovery_performed=true
  CLAUDEX_DEFER_MODEL_PRUNE=1 \
    ORICHUM_SUPPRESS_SETUP_INSTRUCTION=1 \
    "$discovery_entrypoint" || \
    model_discovery_status=$?
fi
if [[ "$model_discovery_status" -ne 0 ]]; then
  model_discovery_succeeded=false
  if [[ -z "$prior_model_generation" ]] && \
     [[ "$model_discovery_status" -eq \
        "$MODEL_DISCOVERY_LOGIN_INCOMPLETE" ]]; then
    printf 'NOTICE: persistent Orichum route proxy is pending-provider-login.\n' >&2
  elif [[ -n "$prior_model_generation" ]] && \
       [[ "$ports_changed" == false ]] && \
       [[ "$cliproxy_binary_changed" == false ]] && \
       [[ "$cliproxy_config_changed" == unchanged ]] && \
       [[ "$cliproxy_service_changed" == unchanged ]] && \
       [[ "$cliproxy_listener_owned" == true ]] && \
       [[ "$cliproxy_ready_before" == true ]] && \
       [[ "$leanctx_proxy_restart_required" == false ]] && \
       [[ "$leanctx_proxy_listener_owned" == true ]] && \
       [[ "$claudex_binary_changed" == false ]] && \
       [[ "$claudex_proxy_service_changed" == unchanged ]] && \
       [[ "$claudex_proxy_listener_owned" == true ]]; then
    printf 'WARNING: model discovery failed; unchanged healthy proxy state was retained.\n' >&2
    routing_action=reused
  else
    workflow_die \
      "model discovery failed while persistent proxy reconciliation was required"
  fi
fi

claudex_proxy_action=pending-provider-login
claudex_proxy_readiness_drifted=false
if [[ "$model_discovery_succeeded" == true || \
      -n "$prior_model_generation" ]]; then
  active_claudex_config="$(model_config_file \
    "$WORKFLOW_DATA_ROOT" claudex.toml)"
  active_controller_model="$(claudex_config_default_model \
    "$active_claudex_config")" || \
    workflow_die "active Claudex controller model could not be resolved"
  claudex_model_config_changed=true
  if [[ -n "$prior_model_generation_snapshot" && \
        -f "$prior_model_generation_snapshot/claudex.toml" ]] && \
     cmp -s "$active_claudex_config" \
       "$prior_model_generation_snapshot/claudex.toml"; then
    claudex_model_config_changed=false
  fi
  if [[ "$claudex_decision" != reused || \
        "$claudex_binary_changed" == true || \
        "$claudex_model_config_changed" == true || \
        "$claudex_proxy_service_changed" == changed || \
        "$claudex_proxy_port_changed" == true ]]; then
    preflight_claudex_translation_proxy "$active_claudex_config" || \
      workflow_die \
        "Claudex translation proxy failed isolated bind and catalogue preflight"
  fi

  if [[ "$claudex_proxy_service_owned" != true ]] || \
     ! claudex_proxy_runtime_is_owned \
       "$ROUTE_PROXY_LISTEN_PORT" "$active_controller_model"; then
    claudex_proxy_readiness_drifted=true
  fi

  claudex_proxy_restart_required=false
  if [[ "$claudex_proxy_service_changed" == changed ]] || \
     [[ "$claudex_proxy_port_changed" == true ]] || \
     [[ "$claudex_proxy_readiness_drifted" == true ]]; then
    claudex_proxy_restart_required=true
  fi

  if [[ "$claudex_proxy_restart_required" == true ]]; then
    preflight_claudex_proxy || workflow_die \
      "Orichum recovery proxy failed isolated preflight; the existing service was left running"
    if [[ "$claudex_proxy_service_was_present" == true ]]; then
      claudex_proxy_prior_runtime_safe_to_stop || workflow_die \
        "refusing to stop ownership-drifted Orichum route proxy runtime"
    fi
    claudex_proxy_transaction_active=true
    printf 'WARNING: restarting the shared Orichum route proxy may interrupt one in-flight request across active sessions.\n' >&2
    if [[ "$claudex_proxy_service_changed" == changed ]]; then
      activate_staged_file "$claudex_proxy_desired_service_file" \
        "$claudex_proxy_service_file" "$claudex_proxy_service_mode"
    fi
    claudex_proxy_service_is_owned \
      "$claudex_proxy_service_file" "$WORKFLOW_DATA_ROOT" \
      "$WORKFLOW_ROOT" || \
      workflow_die "installed Orichum route proxy service definition is not owned"
    if [[ "$claudex_proxy_service_was_present" == true ]]; then
      claudex_proxy_runtime_mutated=true
      if [[ "$platform" == darwin ]]; then
        launchctl bootout \
          "gui/$(id -u)/$claudex_proxy_service_label" \
          >/dev/null 2>&1 || true
      else
        systemctl --user stop "$claudex_proxy_service_unit" \
          >/dev/null 2>&1 || true
      fi
    fi
    activation_port_ready=false
    for _ in {1..150}; do
      if ! loopback_port_is_listening "$ROUTE_PROXY_LISTEN_PORT"; then
        activation_port_ready=true
        break
      fi
      sleep 0.1
    done
    [[ "$activation_port_ready" == true ]] || workflow_die \
      "Orichum route proxy activation port $ROUTE_PROXY_LISTEN_PORT still has a listener; prior state will be restored"
    if [[ "$platform" == darwin ]]; then
      claudex_proxy_loaded_target_is_expected || workflow_die \
        "Orichum route proxy definition ownership drifted before start"
      claudex_proxy_runtime_mutated=true
      launchctl enable \
        "gui/$(id -u)/$claudex_proxy_service_label"
      launchctl bootstrap \
        "gui/$(id -u)" "$claudex_proxy_service_file"
    else
      systemctl --user daemon-reload
      claudex_proxy_loaded_target_is_expected || workflow_die \
        "Orichum route proxy definition ownership drifted before start"
      systemctl --user enable "$claudex_proxy_service_unit"
      claudex_proxy_runtime_mutated=true
      systemctl --user start "$claudex_proxy_service_unit"
    fi
    wait_for_claudex_proxy \
      "$ROUTE_PROXY_LISTEN_PORT" "$active_controller_model" || \
      workflow_die \
        "Orichum route proxy failed ownership or readiness checks; previous state will be restored"
    if [[ "$claudex_proxy_service_was_present" == true ]]; then
      claudex_proxy_action=reconciled
    else
      claudex_proxy_action=installed
    fi
  else
    claudex_proxy_action=reused
  fi
fi
if [[ "$routing_action" == reused ]] && \
   [[ "$claudex_proxy_action" == reconciled ]]; then
  routing_action=repaired
fi

if [[ "$endpoint_lock_owned" == true ]]; then
  release_endpoint_config_lock \
    "$WORKFLOW_DATA_ROOT" "$endpoint_lock_token" || \
    workflow_die "endpoint model publication lock could not be released"
  endpoint_lock_owned=false
fi
config_transaction_active=true
activate_installed_control_plane \
  "$ORICHUM_PYTHON" "$WORKFLOW_ROOT" \
  "$candidate_config_root" "$INSTALLED_CONFIG_ROOT" \
  "$control_plane_journal" "$lifecycle_lock_path" \
  "$WORKFLOW_LOCK_FD" || \
  workflow_die "installed Orichum control plane could not be committed"
ORICHUM_CONFIG_ROOT="$INSTALLED_CONFIG_ROOT"
ORICHUM_CONFIG_HOME="$ORICHUM_CONFIG_ROOT"
export ORICHUM_CONFIG_HOME
print_install_progress \
  "$INSTALL_VERBOSE" '  Verifying installation…' >&3
verify_committed_control_plane \
  "$ORICHUM_CONFIG_ROOT" "$WORKFLOW_DATA_ROOT" || \
  workflow_die "committed Orichum control plane is invalid"
install -m 0600 "$WORKFLOW_ROOT/controller/settings.json" \
  "$snapshot_dir/claude-settings-installed.data"
claude_settings_transaction_active=true
activate_private_file_atomic \
  "$snapshot_dir/claude-settings-installed.data" \
  "$claude_settings_path" 0600 || \
  workflow_die "isolated Claude settings could not be activated safely"
committed_route_service_file="$claudex_proxy_service_file"
if [[ "$claudex_proxy_action" == pending-provider-login ]]; then
  committed_route_service_file="$claudex_proxy_desired_service_file"
fi
routing_input_sha="$(
  verified_routing_input_fingerprint \
    "$routing_input_descriptor" \
    "$(sha256_file "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api")" \
    "$(sha256_file "$WORKFLOW_DATA_ROOT/bin/claudex")" \
    "$route_proxy_runtime_digest" \
    "$CLIPROXY_PORT" "$CLAUDEX_PROXY_PORT" "$ROUTE_PROXY_LISTEN_PORT" \
    "$LEANCTX_PROXY_PORT" \
    "$INSTALLED_CONFIG_ROOT/accounts.json" \
    "$INSTALLED_CONFIG_ROOT/jira-profiles.json" \
    "$INSTALLED_CONFIG_ROOT/model-stacks.json" \
    "$INSTALLED_CONFIG_ROOT/plugins.json" \
    "$INSTALLED_CONFIG_ROOT/projects.json" \
    "$INSTALLED_CONFIG_ROOT/providers.json" \
    "$INSTALLED_CONFIG_ROOT/runtime.json" \
    "$INSTALLED_CONFIG_ROOT/controller-policy.md" \
    "$WORKFLOW_ROOT/config/model-stacks.json" \
    "$WORKFLOW_ROOT/config/jira-profiles.json" \
    "$WORKFLOW_ROOT/config/plugins.json" \
    "$WORKFLOW_ROOT/config/projects.json" \
    "$WORKFLOW_ROOT/config/providers.json" \
    "$WORKFLOW_ROOT/config/runtime.json" \
    "$WORKFLOW_ROOT/config/controller-policy.md" \
    "$WORKFLOW_DATA_ROOT/claude-config/settings.json" \
    "$WORKFLOW_DATA_ROOT/cliproxy.yaml" \
    "$leanctx_proxy_config_path" \
    "$service_file" "$leanctx_proxy_service_file" \
    "$committed_route_service_file"
)" || workflow_die "committed routing input fingerprint failed"
if [[ "$controller_plugin_decision" != reused ]]; then
  ORICHUM_CONFIG_HOME="$ORICHUM_CONFIG_ROOT" \
  ORICHUM_DATA_HOME="$WORKFLOW_DATA_ROOT" \
    "$WORKFLOW_ROOT/bin/orichum-plugin" sync || \
    workflow_die \
      "services are healthy, but declared Claude plugins could not be synchronized; rerun the installer after correcting the plugin error"
fi
if [[ "$claudex_proxy_action" != pending-provider-login ]]; then
  ORICHUM_CONFIG_HOME="$ORICHUM_CONFIG_ROOT" \
  ORICHUM_DATA_HOME="$WORKFLOW_DATA_ROOT" \
    "$WORKFLOW_ROOT/bin/orichum-runtime-ready" \
      "$WORKFLOW_DATA_ROOT" || \
    workflow_die "focused Orichum runtime readiness failed"
fi
install_state_prior_components="$installer_temp/prior-components.json"
if [[ "$prior_install_state_verified" == true ]]; then
  jq -e '.components' "$prior_install_state" \
    >"$install_state_prior_components" || \
    workflow_die "verified installer component state could not be read"
else
  printf '{}\n' >"$install_state_prior_components"
fi
chmod 0600 "$install_state_prior_components"
install_state_components="$installer_temp/install-state-components.json"
jq -n \
  --slurpfile prior "$install_state_prior_components" \
  --arg python_version "$orichum_python_version" \
  --arg python_artifact "$(sha256_file "$ORICHUM_PYTHON")" \
  --arg python_input "$python_input_sha" \
  --arg python_probe "$python_probe_sha" \
  --arg cliproxy_version "$cliproxy_version" \
  --arg cliproxy_tag "$(jq -r '.tag' <<<"$cliproxy_state")" \
  --arg cliproxy_artifact \
    "$(sha256_file "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api")" \
  --arg cliproxy_input "$cliproxy_input_sha" \
  --arg cliproxy_probe "$cliproxy_probe_sha" \
  --arg claudex_version "$claudex_version" \
  --arg claudex_tag "$(jq -r '.tag' <<<"$claudex_state")" \
  --arg claudex_artifact \
    "$(sha256_file "$WORKFLOW_DATA_ROOT/bin/claudex")" \
  --arg claudex_input "$claudex_input_sha" \
  --arg claudex_probe "$claudex_probe_sha" \
  --arg leanctx_version "$leanctx_version" \
  --arg leanctx_tag "$(jq -r '.tag' <<<"$leanctx_state")" \
  --arg leanctx_artifact \
    "$(sha256_file "$WORKFLOW_DATA_ROOT/bin/lean-ctx")" \
  --arg leanctx_input "$leanctx_input_sha" \
  --arg leanctx_probe "$leanctx_probe_sha" \
  --arg controller_plugin_input "$controller_plugin_input_sha" \
  --arg controller_plugin_probe "$controller_plugin_probe_sha" \
  --arg completion_artifact "$completion_artifact" \
  --arg completion_input "$completion_input_sha" \
  --arg completion_probe "$completion_probe_sha" \
  '$prior[0] + {
    python: {
      version: $python_version,
      sourceIdentity: ("python:" + $python_version),
      artifactSha256: $python_artifact,
      inputSha256: $python_input,
      probeSha256: $python_probe
    },
    cliproxy: {
      version: $cliproxy_version,
      sourceIdentity: (
        "github:router-for-me/CLIProxyAPI@" + $cliproxy_tag
      ),
      artifactSha256: $cliproxy_artifact,
      inputSha256: $cliproxy_input,
      probeSha256: $cliproxy_probe
    },
    claudex: {
      version: $claudex_version,
      sourceIdentity: ("github:alupao/claudex@" + $claudex_tag),
      artifactSha256: $claudex_artifact,
      inputSha256: $claudex_input,
      probeSha256: $claudex_probe
    },
    leanctx: {
      version: $leanctx_version,
      sourceIdentity: ("github:yvgude/lean-ctx@" + $leanctx_tag),
      artifactSha256: $leanctx_artifact,
      inputSha256: $leanctx_input,
      probeSha256: $leanctx_probe
    },
    controllerPlugin: {
      version: "1",
      sourceIdentity: "orichum:controller-plugin",
      artifactSha256: $controller_plugin_input,
      inputSha256: $controller_plugin_input,
      probeSha256: $controller_plugin_probe
    },
    completion: {
      version: "1",
      sourceIdentity: "orichum:completion",
      artifactSha256: $completion_artifact,
      inputSha256: $completion_input,
      probeSha256: $completion_probe
    }
  }' >"$install_state_components" || \
  workflow_die "candidate installer state could not be built"
chmod 0600 "$install_state_components"
if [[ "$model_discovery_succeeded" == true ]]; then
  routing_verified_artifact="$(routing_runtime_artifact)" || \
    workflow_die "verified routing artifact could not be fingerprinted"
  routing_state_candidate="$installer_temp/routing-state-components.json"
  jq \
    --arg artifact "$routing_verified_artifact" \
    --arg input "$routing_input_sha" \
    --arg probe "$routing_probe_sha" \
    '.routing = {
      version: "1",
      sourceIdentity: "orichum:routing",
      artifactSha256: $artifact,
      inputSha256: $input,
      probeSha256: $probe
    }' "$install_state_components" >"$routing_state_candidate" || \
    workflow_die "candidate routing state could not be built"
  chmod 0600 "$routing_state_candidate"
  mv -f "$routing_state_candidate" "$install_state_components"
fi
if [[ "$model_discovery_succeeded" == true ]]; then
  prune_model_config_generations "$WORKFLOW_DATA_ROOT" || \
    printf 'WARNING: stale model configuration could not be pruned.\n' >&2
fi
cliproxy_action=reused
if [[ "$cliproxy_restart_required" == true ]]; then
  if [[ "$cliproxy_service_was_present" == true ]]; then
    cliproxy_action=reconciled
  else
    cliproxy_action=installed
  fi
fi
if [[ "$routing_action" == reused ]] && \
   [[ "$cliproxy_action" == reconciled ]]; then
  routing_action=repaired
fi
print_component_status_table \
  "$python_decision" "$cliproxy_decision" "$claudex_decision" \
  "$leanctx_decision" "$routing_action" "$controller_plugin_decision" \
  "$completion_decision" || \
  workflow_die "component reconciliation status is invalid"
printf 'Installed Orichum with Claudex %s, CLIProxyAPI %s, and LeanCTX %s for %s.\n' \
  "$claudex_version" "$cliproxy_version" "$leanctx_version" "$platform"
print_install_summary \
  "$WORKFLOW_ROOT" "$WORKFLOW_DATA_ROOT" "$USER_BIN_DIR" \
  "$WORKFLOW_DATA_ROOT/bin/claudex" \
  "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api" \
  "$service_file" \
  "$CLIPROXY_PORT" "$cliproxy_action" \
  "$claudex_proxy_service_file" "$CLAUDEX_PROXY_PORT" \
  "$ROUTE_PROXY_LISTEN_PORT" \
  "$claudex_proxy_action" \
  "$ORICHUM_PYTHON" "$orichum_python_version" \
  "$orichum_python_candidate" "$orichum_python_action" \
  "$WORKFLOW_DATA_ROOT/bin/lean-ctx" "$SOURCE_ROOT" "$ORICHUM_HOME_ROOT" \
  "$leanctx_proxy_service_file" "$LEANCTX_PROXY_PORT" \
  "$(
    if [[ "$leanctx_proxy_restart_required" == true ]]; then
      if [[ "$leanctx_proxy_service_was_present" == true ]]; then
        printf reconciled
      else
        printf installed
      fi
    else
      printf reused
    fi
  )"
if [[ "$claudex_proxy_action" == pending-provider-login ]]; then
  printf 'Next: orichum setup\n'
elif [[ "$INSTALL_MODE" == upgrade || \
        "$home_migration_performed" == true || \
        "$prior_install_state_verified" != true ]]; then
  printf '\nRunning Orichum doctor...\n'
  ORICHUM_CONFIG_HOME="$ORICHUM_CONFIG_ROOT" \
  ORICHUM_DATA_HOME="$WORKFLOW_DATA_ROOT" \
    "$USER_BIN_DIR/orichum" doctor
else
  printf '\nFast readiness checks passed.\n'
fi
install_state_transaction_active=true
python3 -I -B "$WORKFLOW_ROOT/integrations/common/install_state.py" \
  write "$install_state_path" "$install_state_platform" \
  "$install_state_components" || \
  workflow_die "verified installer state could not be published"
finalize_installed_control_plane \
  "$ORICHUM_PYTHON" "$WORKFLOW_ROOT" "$control_plane_journal" \
  "$lifecycle_lock_path" "$WORKFLOW_LOCK_FD" || \
  workflow_die "installed Orichum control-plane journal could not be finalized"
runtime_transaction_active=false
cliproxy_transaction_active=false
claudex_proxy_transaction_active=false
claudex_proxy_runtime_mutated=false
endpoint_transaction_active=false
leanctx_transaction_active=false
python_transaction_active=false
config_transaction_active=false
install_state_transaction_active=false
claude_settings_transaction_active=false
completion_transaction_active=false
WORKFLOW_TRANSACTION_ACTIVE=false
if [[ "$home_migration_active" == true ]]; then
  commit_orichum_home \
    "$SOURCE_ROOT" "$home_migration_journal" || \
    workflow_die "consolidated Orichum home could not be committed"
  home_migration_active=false
fi
prune_orichum_runtime \
  "$SOURCE_ROOT" "$ORICHUM_HOME_ROOT" "$runtime_release" || \
  printf 'WARNING: obsolete Orichum runtime releases could not be removed.\n' >&2
provider_pending=false
if [[ "$claudex_proxy_action" == pending-provider-login ]]; then
  provider_pending=true
fi
print_install_component_results \
  "$python_decision" "$cliproxy_decision" "$claudex_decision" \
  "$leanctx_decision" "$routing_action" "$controller_plugin_decision" \
  "$completion_decision" "$ORICHUM_COMPLETION_OPTIONAL_SHELL" >&3
print_install_outcome \
  "$provider_pending" "$ORICHUM_COMPLETION_OPTIONAL_SHELL" \
  "$INSTALL_LOG_PATH" >&3

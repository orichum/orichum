#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/workflow.sh
source "$ROOT/lib/workflow.sh"
export ORICHUM_INSTALL_BOOTSTRAP=true
fixture="$(mktemp -d "${TMPDIR:-/tmp}/orichum-transaction.XXXXXX")"
fixture="$(cd -P "$fixture" && pwd)"
trap 'rm -rf -- "$fixture"' EXIT

python3 - "$ROOT/install.sh" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
routing_decision_start = source.index("routing_decision=upgraded")
routing_decision_end = source.index(
    "\n\npreflight_claudex_proxy()", routing_decision_start
)
routing_decision = source[routing_decision_start:routing_decision_end]
early_runtime_samples = (
    "cliproxy_listener_owned",
    "cliproxy_ready_before",
    "claudex_proxy_listener_owned",
)
if any(sample in routing_decision for sample in early_runtime_samples):
    raise SystemExit(
        "routing status still treats an early runtime sample as a completed "
        "repair"
    )
route_action_start = source.index("claudex_proxy_action=pending-provider-login")
route_action_end = source.index(
    "\nif [[ \"$endpoint_lock_owned\" == true ]]", route_action_start
)
route_action = source[route_action_start:route_action_end]
if (
    '[[ "$routing_action" == reused ]]' not in route_action
    or '[[ "$claudex_proxy_action" == reconciled ]]' not in route_action
    or "routing_action=repaired" not in route_action
):
    raise SystemExit(
        "routing status does not report an actual route-proxy reconciliation"
    )
cliproxy_action_start = source.index("cliproxy_action=reused")
cliproxy_action_end = source.index(
    "\nprint_component_status_table", cliproxy_action_start
)
cliproxy_action = source[cliproxy_action_start:cliproxy_action_end]
if (
    '[[ "$routing_action" == reused ]]' not in cliproxy_action
    or '[[ "$cliproxy_action" == reconciled ]]' not in cliproxy_action
    or "routing_action=repaired" not in cliproxy_action
):
    raise SystemExit(
        "routing status does not report an actual CLIProxyAPI reconciliation"
    )

start = source.index('elif [[ -n "$prior_model_generation" ]]')
end = source.index('routing_action=reused', start)
fallback = source[start:end]
required = (
    '[[ "$cliproxy_binary_changed" == false ]]',
    '[[ "$cliproxy_config_changed" == unchanged ]]',
    '[[ "$cliproxy_service_changed" == unchanged ]]',
    '[[ "$cliproxy_listener_owned" == true ]]',
    '[[ "$cliproxy_ready_before" == true ]]',
)
missing = [condition for condition in required if condition not in fallback]
if missing:
    raise SystemExit(
        "model-discovery fallback lacks CLIProxy invariants: "
        + ", ".join(missing)
    )
PY

model_file_data="$fixture/model-file-data"
model_file_generation="$model_file_data/model-config/generation.test"
install -d -m 0700 "$model_file_generation"
printf '{}\n' >"$model_file_generation/models.json"
printf 'default_model = "test"\n' \
  >"$model_file_generation/claudex.toml"
printf '{}\n' >"$model_file_generation/effective-models.json"
ln -s generation.test "$model_file_data/model-config/current"
[[ "$(model_config_file \
  "$model_file_data" effective-models.json)" == \
  "$model_file_data/model-config/current/effective-models.json" ]]

snapshot="$fixture/snapshot"
install -d -m 0700 "$snapshot" "$fixture/bin"

launcher="$fixture/bin/orichum"
prior="$fixture/prior-orichum"
printf '#!/usr/bin/env bash\nexit 0\n' >"$prior"
chmod 0755 "$prior"
ln -s "$prior" "$launcher"

snapshot_path "$launcher" "$snapshot" launcher
rm "$launcher"
printf 'partial install\n' >"$launcher"
restore_snapshot "$launcher" "$snapshot" launcher
snapshot_path_matches "$launcher" "$snapshot" launcher
[[ -L "$launcher" && "$(readlink "$launcher")" == "$prior" ]]

absent="$fixture/bin/absent"
snapshot_path "$absent" "$snapshot" absent
printf 'partial install\n' >"$absent"
restore_snapshot "$absent" "$snapshot" absent
snapshot_path_matches "$absent" "$snapshot" absent
[[ ! -e "$absent" && ! -L "$absent" ]]

rollback_source="$(
  sed -n '/^rollback_install_transaction() {/,/^}/p' "$ROOT/install.sh"
)"
eval "$rollback_source"
rollback_consolidated_runtime_and_home() { return 0; }
settings_root="$fixture/claude-config"
settings_path="$settings_root/settings.json"
snapshot_dir="$fixture/settings-snapshot"
install -d -m 0700 "$settings_root" "$snapshot_dir"
printf '{"before":true}\n' >"$settings_path"
chmod 0644 "$settings_path"
snapshot_path "$settings_path" "$snapshot_dir" claude-settings
printf '{"managed":true}\n' >"$snapshot_dir/claude-settings-installed.data"
chmod 0600 "$snapshot_dir/claude-settings-installed.data"
cp "$snapshot_dir/claude-settings-installed.data" "$settings_path"
chmod 0600 "$settings_path"

WORKFLOW_ROOT="$ROOT"
claude_settings_path="$settings_path"
claude_settings_transaction_active=true
claudex_proxy_runtime_mutated=false
config_transaction_active=false
python_transaction_active=false
leanctx_transaction_active=false
cliproxy_transaction_active=false
endpoint_transaction_active=false
claudex_proxy_transaction_active=false
endpoint_lock_owned=false
orichum_launcher_mutated=false
install_state_transaction_active=false
rollback_install_transaction
cmp -s "$settings_path" "$snapshot_dir/claude-settings.data"
[[ "$(path_mode "$settings_path")" == 644 ]]

cp "$snapshot_dir/claude-settings-installed.data" "$settings_path"
chmod 0600 "$settings_path"
printf '{"concurrent":true}\n' >"$settings_path"
if rollback_install_transaction 2>"$fixture/settings-drift.stderr"; then
  printf 'settings rollback overwrote or accepted concurrent drift\n' >&2
  exit 1
fi
rg -Fq 'Claude settings changed during rollback' \
  "$fixture/settings-drift.stderr"
[[ "$(<"$settings_path")" == '{"concurrent":true}' ]]

completion_snapshot="$fixture/completion-snapshot"
completion_root="$fixture/completion-root"
completion_zsh="$completion_root/zsh/_orichum"
completion_bash="$completion_root/bash/orichum"
completion_fish="$fixture/fish/orichum.fish"
completion_prior_fish="$fixture/prior-fish/orichum.fish"
completion_fish_record="$completion_root/fish-path"
completion_zshrc="$fixture/home/.zshrc"
completion_zshrc_target="$fixture/home/.dotfiles/.zshrc"
completion_bashrc="$fixture/home/.bashrc"
completion_bash_login="$fixture/home/.bash_profile"
install -d -m 0700 \
  "$(dirname "$completion_zsh")" "$(dirname "$completion_bash")" \
  "$(dirname "$completion_fish")" "$(dirname "$completion_prior_fish")" \
  "$(dirname "$completion_zshrc")" \
  "$(dirname "$completion_zshrc_target")" \
  "$completion_snapshot"
ln -s .dotfiles/.zshrc "$completion_zshrc"
completion_paths=(
  "$completion_zsh"
  "$completion_bash"
  "$completion_fish"
  "$completion_prior_fish"
  "$completion_fish_record"
  "$completion_zshrc_target"
  "$completion_bashrc"
  "$completion_bash_login"
)
completion_names=(
  completion-zsh
  completion-bash
  completion-fish
  completion-fish-prior
  completion-fish-record
  completion-zshrc
  completion-bashrc
  completion-bash-login
)
for index in "${!completion_paths[@]}"; do
  printf 'before %s\n' "$index" >"${completion_paths[$index]}"
  snapshot_path \
    "${completion_paths[$index]}" "$completion_snapshot" \
    "${completion_names[$index]}"
  printf 'installed %s\n' "$index" >"${completion_paths[$index]}"
  snapshot_path \
    "${completion_paths[$index]}" "$completion_snapshot" \
    "${completion_names[$index]}-installed"
done
snapshot_dir="$completion_snapshot"
completion_transaction_active=true
completion_installed_snapshotted=true
completion_zsh_path="$completion_zsh"
completion_bash_path="$completion_bash"
completion_fish_path="$completion_fish"
completion_prior_fish_path="$completion_prior_fish"
completion_zsh_profile="$completion_zshrc"
completion_zsh_snapshot_profile="$(
  HOME="$fixture/home" orichum_profile_snapshot_path "$completion_zshrc"
)"
[[ "$completion_zsh_snapshot_profile" == "$completion_zshrc_target" ]]
completion_bash_profile="$completion_bashrc"
completion_bash_login_profile="$completion_bash_login"
claude_settings_transaction_active=false
rollback_install_transaction
[[ -L "$completion_zshrc" ]]
[[ "$(readlink "$completion_zshrc")" == .dotfiles/.zshrc ]]
for index in "${!completion_paths[@]}"; do
  [[ "$(<"${completion_paths[$index]}")" == "before $index" ]]
done
completion_transaction_active=false

install_state_dir="$fixture/install-state"
install_state_snapshot="$fixture/install-state-snapshot"
install_state_file="$install_state_dir/install-state.json"
install -d -m 0700 "$install_state_dir" "$install_state_snapshot"
printf '{"prior":true}\n' >"$install_state_file"
chmod 0600 "$install_state_file"
snapshot_path \
  "$install_state_file" "$install_state_snapshot" install-state
printf '{"partial":true}\n' >"$install_state_file"
restore_snapshot \
  "$install_state_file" "$install_state_snapshot" install-state
snapshot_path_matches \
  "$install_state_file" "$install_state_snapshot" install-state
[[ "$(<"$install_state_file")" == '{"prior":true}' ]]

python3 - "$fixture/occupied.port" <<'PY' &
import socket
import sys
import time

listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen()
with open(sys.argv[1], "w", encoding="ascii") as handle:
    handle.write(str(listener.getsockname()[1]))
while True:
    time.sleep(1)
PY
listener_pid=$!
trap 'kill "$listener_pid" 2>/dev/null || true; wait "$listener_pid" 2>/dev/null || true; rm -rf -- "$fixture"' EXIT
for _ in {1..100}; do
  [[ -s "$fixture/occupied.port" ]] && break
  sleep 0.01
done
occupied="$(cat "$fixture/occupied.port")"
selected="$(
  select_service_port 'Route proxy' TEST_PORT "$occupied" false false
)"
[[ "$selected" != "$occupied" ]]
valid_service_port "$selected"
port_is_available "$selected"

TEST_PORT="$occupied"
if select_service_port 'Route proxy' TEST_PORT "$occupied" false false \
    >"$fixture/override.stdout" 2>"$fixture/override.stderr"; then
  printf 'explicit occupied port was silently replaced\n' >&2
  exit 1
fi
rg -Fq 'from TEST_PORT is unavailable' "$fixture/override.stderr"

loopback_port_is_listening "$occupied"
kill "$listener_pid"
wait "$listener_pid" 2>/dev/null || true
listener_pid=
if loopback_port_is_listening "$occupied"; then
  printf 'stopped listener was confused with residual socket state\n' >&2
  exit 1
fi

rg -Fq 'snapshot_path "$USER_BIN_DIR/orichum"' "$ROOT/install.sh"
rg -Fq 'orichum_launcher_mutated=true' "$ROOT/install.sh"
rg -Fq 'restore_snapshot "$USER_BIN_DIR/orichum"' "$ROOT/install.sh"
rg -Fq 'remove_orichum_python_generation' "$ROOT/install.sh"
rg -Fq 'from integrations.common.install_control_plane import activate' \
  "$ROOT/install.sh"
rg -Fq 'from integrations.common.install_control_plane import rollback' \
  "$ROOT/install.sh"
rg -Fq 'rollback_installed_control_plane' "$ROOT/install.sh"
rg -Fq 'managed_listener_is_owned' "$ROOT/install.sh"
rg -Fq 'managed_target_matches_definition_or_absent' "$ROOT/install.sh"
settings_line="$(rg -n -F 'install -m 0600 "$WORKFLOW_ROOT/controller/settings.json"' \
  "$ROOT/install.sh" | cut -d: -f1)"
transaction_end_line="$(rg -n -F 'WORKFLOW_TRANSACTION_ACTIVE=false' \
  "$ROOT/install.sh" | tail -1 | cut -d: -f1)"
[[ "$settings_line" -lt "$transaction_end_line" ]]

python3 - "$ROOT/install.sh" <<'PY'
import sys

source = open(sys.argv[1], encoding="utf-8").read()
workflow = open(
    str(__import__("pathlib").Path(sys.argv[1]).parent / "lib/workflow.sh"),
    encoding="utf-8",
).read()
acquire_start = workflow.index("acquire_workflow_lock()")
acquire_end = workflow.index("release_workflow_lock()", acquire_start)
acquire = workflow[acquire_start:acquire_end]
if (
    'hold_workflow_lock_descriptor "$lock_dir"' not in acquire
    or 'exec 9<"$lock_dir"' not in workflow
):
    raise SystemExit("workflow lock acquisition does not retain lock FD 9")
for helper in (
    "recover_installed_control_plane()",
    "activate_installed_control_plane()",
    "rollback_installed_control_plane()",
    "finalize_installed_control_plane()",
):
    start = source.index(helper)
    end = source.index("\n}", start)
    helper_source = source[start:end]
    if (
        "install_lock_path" not in helper_source
        or "install_lock_fd" not in helper_source
    ):
        raise SystemExit(
            f"{helper} does not pass the held lifecycle lock identity"
        )
if source.count('"$WORKFLOW_LOCK_FD"') < 4:
    raise SystemExit("journal helper call sites omit the held installer lock FD")
if source.count('"$lifecycle_lock_path"') < 4:
    raise SystemExit(
        "journal helper call sites omit the held lifecycle lock path"
    )
start = source.index("rollback_install_transaction()")
end = source.index("WORKFLOW_ROLLBACK_HANDLER=", start)
rollback = source[start:end]

stop_route = rollback.index("claudex_proxy_runtime_mutated")
restore_installed_config = rollback.index(
    "rollback_installed_control_plane"
)
restore_python = rollback.index("rollback_python_activation")
restore_cliproxy = rollback.index(
    'restore_snapshot "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api"'
)
restore_endpoint = rollback.index("restore_model_config_generation")
restore_route = rollback.index("restore_claudex_proxy_service")
restore_install_state = rollback.index(
    'restore_snapshot "$install_state_path"'
)
restore_consolidated_home = rollback.index(
    "rollback_consolidated_runtime_and_home"
)
if not (
    stop_route
    < restore_installed_config
    < restore_python
    < restore_cliproxy
    < restore_endpoint
    < restore_route
    < restore_install_state
    < restore_consolidated_home
):
    raise SystemExit("combined service rollback dependency order is unsafe")
python_rollback_start = source.index("rollback_python_activation()")
python_rollback_end = source.index(
    "\n}\nrollback_python_and_consolidated()", python_rollback_start
)
if "rollback_consolidated_runtime_and_home" in source[
    python_rollback_start:python_rollback_end
]:
    raise SystemExit(
        "Python rollback moves the consolidated home before service recovery"
    )
prior_stop_start = source.index("claudex_proxy_prior_runtime_safe_to_stop()")
prior_stop_end = source.index("\n}", prior_stop_start)
prior_stop = source[prior_stop_start:prior_stop_end]
if (
    "prior_route_data_root" not in prior_stop
    or "prior_route_workflow_root" not in prior_stop
):
    raise SystemExit(
        "route migration stop guard ignores the verified prior service roots"
    )
service_preflight_start = source.index(
    "claudex_proxy_service_was_present=false"
)
service_preflight_end = source.index(
    "claudex_proxy_manager_target_state=", service_preflight_start
)
service_preflight = source[service_preflight_start:service_preflight_end]
if (
    '[[ "$runtime_previous" != "-" ]]' not in service_preflight
    or '"$runtime_previous"' not in service_preflight
):
    raise SystemExit(
        "route upgrade preflight ignores the previously verified runtime"
    )
install_state_rollback = rollback[
    rollback.index('if [[ "${install_state_transaction_active:-false}"'):
]
if "prior_install_state_verified" in install_state_rollback:
    raise SystemExit(
        "install-state rollback still depends on manifest verification"
    )

fast_attempt = source.index("if attempt_verified_fast_install")
source_validation = source.index("source Orichum control plane is invalid")
first_runtime_snapshot = source.index(
    'snapshot_path "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api"'
)
if not (
    source_validation
    < fast_attempt
    < first_runtime_snapshot
):
    raise SystemExit(
        "verified fast path validation or snapshot ordering is unsafe"
    )
fast_start = source.index("attempt_verified_fast_install()")
fast_end = source.index("\n)\n\nif attempt_verified_fast_install", fast_start)
fast_body = source[fast_start:fast_end]
if (
    "trap cleanup_fast_verifiers EXIT" not in fast_body
    or "wait \"$config_verify_pid\"" not in fast_body
    or "wait \"$runtime_verify_pid\"" not in fast_body
):
    raise SystemExit("verified fast path does not reap background verifiers")

restore_start = source.index("restore_claudex_proxy_service()")
restore_end = source.index("\n}\n\nrollback_install_transaction()", restore_start)
restore_service = source[restore_start:restore_end]
platform_branch = restore_service.index('if [[ "$platform" == darwin ]]')
bootstrap = restore_service.index("launchctl bootstrap", platform_branch)
runtime_branch = restore_service.index(
    'if [[ "${claudex_proxy_runtime_mutated:-false}" == true ]]'
)
if "claudex_proxy_loaded_target_is_expected" in restore_service[
    runtime_branch:platform_branch
] or "claudex_proxy_loaded_target_is_expected" in restore_service[
    platform_branch:bootstrap
]:
    raise SystemExit(
        "darwin rollback requires a loaded target after bootout"
    )
if "claudex_proxy_service_is_owned" not in restore_service:
    raise SystemExit(
        "route-proxy rollback does not validate the restored service file"
    )

stage_config = source.index(
    "stage_installed_control_plane",
    source.index("candidate_config_root="),
)
acquire_install_lock = source.index(
    'acquire_workflow_lock "$lifecycle_lock_path"'
)
stable_journal = source.index(
    'control_plane_journal="$WORKFLOW_DATA_ROOT/state/install-control-plane"'
)
recover_config = source.index(
    "recover_installed_control_plane", stable_journal
)
validate_candidate = source.index(
    '"$WORKFLOW_ROOT/bin/orichum" config validate',
    stage_config,
)
activate_config = source.index(
    "activate_installed_control_plane", validate_candidate
)
config_active = source.rindex(
    "config_transaction_active=true", validate_candidate, activate_config
)
transaction_end = source.index(
    "WORKFLOW_TRANSACTION_ACTIVE=false", activate_config
)
if not (
    acquire_install_lock
    < stable_journal
    < recover_config
    < stage_config
    < validate_candidate
    < config_active
    < activate_config
    < transaction_end
):
    raise SystemExit(
        "installed control plane activation is not rollback-active before "
        "its first mutation"
    )
if '"$candidate_config_root" "$INSTALLED_CONFIG_ROOT" \\\n  "$control_plane_journal"' not in source:
    raise SystemExit("activation does not use the stable control-plane journal")
finalize_config = source.index(
    "finalize_installed_control_plane", activate_config
)
doctor = source.index('"$USER_BIN_DIR/orichum" doctor', activate_config)
runtime_ready = source.index(
    '"$WORKFLOW_ROOT/bin/orichum-runtime-ready"',
    activate_config,
)
committed_routing_fingerprint = source.index(
    "committed routing input fingerprint failed",
    activate_config,
)
pending_route_service = source.index(
    'committed_route_service_file="$claudex_proxy_desired_service_file"',
    activate_config,
)
if not (
    activate_config
    < pending_route_service
    < committed_routing_fingerprint
):
    raise SystemExit(
        "provider-free install does not fingerprint its staged route service"
    )
publish_install_state = source.index(
    'write "$install_state_path" "$install_state_platform"',
    doctor,
)
install_state_active = source.index(
    "install_state_transaction_active=true",
    doctor,
)
config_inactive = source.index(
    "config_transaction_active=false", finalize_config
)
if not (
    activate_config
    < committed_routing_fingerprint
    < runtime_ready
    < doctor
    < install_state_active
    < publish_install_state
    < finalize_config
    < config_inactive
    < transaction_end
):
    raise SystemExit(
        "stable control-plane journal is not finalized before disarming "
        "rollback"
    )

python_transaction = source.index("python_transaction_active=true")
provision_python = source.index("install_or_reuse_orichum_python")
snapshot_install_state = source.index(
    'snapshot "$install_state_path" "$snapshot_dir" install-state'
)
read_install_state = source.index(
    'read "$install_state_path" "$install_state_platform"'
)
if not (
    snapshot_install_state
    < read_install_state
    < python_transaction
    < provision_python
):
    raise SystemExit(
        "installer state snapshot does not protect Python provisioning"
    )
PY

shared_suite_workflow="$ROOT/.github/workflows/amd64-acceptance.yml"
rg -Fq 'if ! bash "$test_script"; then' "$shared_suite_workflow"
rg -Fq 'bash -x "$test_script"' "$shared_suite_workflow"

for acceptance_workflow in \
    "$ROOT/.github/workflows/amd64-acceptance.yml" \
    "$ROOT/.github/workflows/macos-arm64-acceptance.yml"; do
  rg -Fq 'report_acceptance_failure()' "$acceptance_workflow"
  rg -Fq 'trap report_acceptance_failure ERR' "$acceptance_workflow"
  rg -Fq "printf '%s\\n' \"\$doctor_output\"" "$acceptance_workflow"
  python3 - "$acceptance_workflow" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text()
disable = source.index("trap - ERR", source.index("doctor_output=") - 300)
capture = source.index("doctor_output=", disable)
enable = source.index("trap report_acceptance_failure ERR", capture)
if not disable < capture < enable:
    raise SystemExit("expected doctor failure is not isolated from the ERR trap")
PY
  rg -Fq 'Native acceptance failure' "$acceptance_workflow"
done

rg -Fq 'report_test_failure()' "$ROOT/tests/test_installer.sh"
rg -Fq 'trap report_test_failure ERR' "$ROOT/tests/test_installer.sh"
rg -Fq 'ERROR: test_installer.sh:%s exited %s: %s' \
  "$ROOT/tests/test_installer.sh"
python3 - "$ROOT/install.sh" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text()
start = source.index("claudex_proxy_runtime_is_owned()")
end = source.index("\n}\n", start) + 3
runtime_check = source[start:end]
if "managed_service_main_pid" not in runtime_check:
    raise SystemExit("route proxy readiness does not verify an active service")
if "claudex_proxy_health_is_ready_at" not in runtime_check:
    raise SystemExit("route proxy readiness does not verify health identity")
if "pid_owns_loopback_listener" in runtime_check:
    raise SystemExit("route proxy readiness still depends on socket metadata")

restart_start = source.index(
    'if [[ "$claudex_proxy_restart_required" == true ]]'
)
restart_end = source.index(
    "claudex_proxy_transaction_active=false", restart_start
)
restart = source[restart_start:restart_end]
bootstrap = restart.index('launchctl bootstrap')
if 'port_is_available "$ROUTE_PROXY_LISTEN_PORT"' in restart[:bootstrap]:
    raise SystemExit(
        "route proxy restart rejects a bindable socket in TIME_WAIT"
    )
if 'loopback_port_is_listening "$ROUTE_PROXY_LISTEN_PORT"' not in restart[:bootstrap]:
    raise SystemExit(
        "route proxy restart does not reject a competing listener"
    )
if restart.index("wait_for_claudex_proxy", bootstrap) <= bootstrap:
    raise SystemExit("route proxy restart omits post-start ownership checks")
PY

printf 'PASS: Orichum installer rollback and port selection\n'

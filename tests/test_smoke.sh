#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/workflow.sh
source "$ROOT/lib/workflow.sh"
[[ "$(<"$ROOT/VERSION")" == 0.1.0-rc.17 ]]
rg -Fq '## 0.1.0-rc.17 - 2026-08-19' "$ROOT/CHANGELOG.md"
rg -Fq "evidence-driven delegation: continuously assess the task's evolving scope," \
  "$ROOT/config/controller-policy.md"
rg -Fq 'never controller model, provider, fixed keywords' \
  "$ROOT/config/controller-policy.md"
rg -Fq 'The controller routes adaptively from the current evidence' \
  "$ROOT/docs/subagents.md"
rg -Fq 'Do not use model family, provider, keywords, file counts' \
  "$ROOT/docs/subagents.md"
rg -Fq 'orichum --version' "$ROOT/docs/cli-reference.md"
rg -Fq '[Changelog](CHANGELOG.md)' "$ROOT/README.md"
rg -Fq 'orichum sessions cleanup' \
  "$ROOT/docs/cli-reference.md" "$ROOT/docs/sessions.md"
for completion_command in \
    'orichum completion zsh' \
    'orichum completion bash' \
    'orichum completion fish'; do
  rg -Fq "$completion_command" "$ROOT/docs/cli-reference.md"
done
rg -Fq '# >>> Orichum completion >>>' "$ROOT/docs/installation.md"
if rg -Fq 'never changes the system Python, shell profiles' \
    "$ROOT/docs/installation.md"; then
  printf 'installation guide still denies managed profile activation\n' >&2
  exit 1
fi
fixture="$(mktemp -d "${TMPDIR:-/tmp}/orichum-smoke.XXXXXX")"
trap 'rm -rf -- "$fixture"' EXIT

ports_root="$fixture/ports"
install -d -m 0700 "$ports_root"
write_service_ports "$ports_root" 8317 13456 13457 13458
IFS=$'\t' read -r \
  cliproxy_port claudex_proxy_port route_proxy_port leanctx_proxy_port \
  < <(read_service_ports "$ports_root")
[[ "$cliproxy_port" == 8317 ]]
[[ "$claudex_proxy_port" == 13456 ]]
[[ "$route_proxy_port" == 13457 ]]
[[ "$leanctx_proxy_port" == 13458 ]]

render_claudex_config \
  "$fixture/claudex.toml" \
  gpt-5.6-sol gpt-5.6-terra claude-sonnet-5 gpt-5.6-sol \
  gpt-5.6-terra claude-sonnet-5 claude-opus-5 \
  /usr/bin/true 8317 13456 13457
rg -Fxq 'base_url = "http://127.0.0.1:13457"' "$fixture/claudex.toml"

for script in "$ROOT"/bin/orichum* "$ROOT"/bootstrap.sh "$ROOT"/install.sh "$ROOT"/doctor.sh; do
  [[ -x "$script" ]]
  bash -n "$script"
done
rg -Fq 'https://raw.githubusercontent.com/orichum/orichum/main/bootstrap.sh | bash' \
  "$ROOT/README.md" \
  "$ROOT/docs/installation.md" \
  "$ROOT/docs/setup-and-configuration.md"
rg -Fq 'https://claude.ai/install.sh' "$ROOT/bootstrap.sh"
rg -Fq 'https://chatgpt.com/codex/install.sh' "$ROOT/bootstrap.sh"
rg -Fq 'https://astral.sh/uv/install.sh' "$ROOT/bootstrap.sh"
for status_health_contract in \
    'Orichum status line is installed and isolated' \
    'route telemetry endpoint is private and redacted'; do
  rg -Fq "$status_health_contract" "$ROOT/doctor.sh"
done
rg -Fq \
  'Usage: ./install.sh [--verbose] [--upgrade | --uninstall [--purge]]' \
  "$ROOT/install.sh"
rg -Fq 'https://github.com/orichum/orichum.git' \
  "$ROOT/README.md" "$ROOT/docs/installation.md"

install -d \
  "$fixture/fake-bin" \
  "$fixture/caller" \
  "$fixture/shadowed/integrations/common" \
  "$fixture/data/bin" \
  "$fixture/data/python/cpython-3.14.6/bin"
system_python="$(command -v python3)"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [[ "$*" == *platform.python_implementation* ]]; then' \
  "  printf 'CPython\\t3.14.6\\n'" \
  'elif [[ -n "${CAPTURE_ARGS:-}" ]]; then' \
  '  printf "%s\n" "$@"' \
  'elif [[ -n "${OBSERVE_CWD:-}" ]]; then' \
  '  pwd' \
  'else' \
  "  exec \"$system_python\" \"\$@\"" \
  'fi' >"$fixture/data/python/cpython-3.14.6/bin/python3.14"
chmod 0755 "$fixture/data/python/cpython-3.14.6/bin/python3.14"
ln -s "$fixture/data/python/cpython-3.14.6/bin/python3.14" \
  "$fixture/data/bin/orichum-python"
printf '#!/usr/bin/env bash\nexit 99\n' >"$fixture/fake-bin/python3"
chmod 0755 "$fixture/fake-bin/python3"
export ORICHUM_DATA_HOME="$fixture/data"
caller_dir="$(cd "$fixture/caller" && pwd -P)"
observed_cwd="$(
  cd "$caller_dir"
  OBSERVE_CWD=1 PATH="$fixture/fake-bin:$PATH" "$ROOT/bin/orichum" config
)"
[[ "$observed_cwd" == "$caller_dir" ]]

install -d \
  "$fixture/post-install-system-bin" \
  "$fixture/post-install-user-bin" \
  "$fixture/post-install-data/bin" \
  "$fixture/post-install-data/python/cpython-3.14.6/bin"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [[ "$*" == *platform.python_implementation* ]]; then' \
  "  printf 'CPython\\t3.14.6\\n'" \
  '  exit 0' \
  'fi' \
  'exit 0' \
  >"$fixture/post-install-data/python/cpython-3.14.6/bin/python3.14"
chmod 0755 "$fixture/post-install-data/python/cpython-3.14.6/bin/python3.14"
ln -s "$fixture/post-install-data/python/cpython-3.14.6/bin/python3.14" \
  "$fixture/post-install-data/bin/orichum-python"
ln -s "$ROOT/bin/orichum" "$fixture/post-install-user-bin/orichum"
post_install_tools="$(
  ORICHUM_DATA_HOME="$fixture/post-install-data" \
  PATH="$fixture/post-install-user-bin:$fixture/post-install-system-bin:/usr/bin:/bin" \
    "$fixture/post-install-user-bin/orichum" config
)"
[[ -z "$post_install_tools" ]]

forwarded="$(
  cd "$caller_dir"
  CAPTURE_ARGS=1 PATH="$fixture/fake-bin:$PATH" \
    "$ROOT/bin/orichum" -p "acceptance prompt"
)"
[[ "$(tail -n 3 <<<"$forwarded")" == $'--\n-p\nacceptance prompt' ]]
rg -Fxq -- '-I' <<<"$forwarded"
rg -Fq 'export ORICHUM_PYTHON_VALIDATED' "$ROOT/bin/orichum"
if rg -n '(^|[[:space:]])python3([[:space:]]|$)' \
    "$ROOT/bin" "$ROOT/controller/plugin/hooks/hooks.json" \
    "$ROOT/discover-models.sh"; then
  printf 'installed Orichum runtime still invokes ambient python3\n' >&2
  exit 1
fi

touch \
  "$fixture/shadowed/integrations/__init__.py" \
  "$fixture/shadowed/integrations/common/__init__.py"
printf 'raise SystemExit(97)\n' >"$fixture/shadowed/runpy.py"
(
  cd "$fixture/shadowed"
  ORICHUM_CONFIG_HOME="$ROOT/config" \
  ORICHUM_DATA_HOME="$fixture/data" \
  ORICHUM_STATE_HOME="$fixture/state" \
  ORICHUM_CACHE_HOME="$fixture/cache" \
    "$ROOT/bin/orichum" config validate
)

help="$("$ROOT/bin/orichum" --help)"
rg -Fq 'usage: orichum ' <<<"$help"
rg -Fq 'context' <<<"$help"
rg -Fq 'leanctx' <<<"$help"
rg -Fq 'sessions' <<<"$help"

leanctx_help="$("$ROOT/bin/orichum" leanctx --help)"
rg -Fq 'dashboard' <<<"$leanctx_help"
rg -Fq 'list' <<<"$leanctx_help"
rg -Fq 'stats' <<<"$leanctx_help"
rg -Fq 'watch' <<<"$leanctx_help"

ORICHUM_CONFIG_HOME="$ROOT/config" \
ORICHUM_DATA_HOME="$fixture/data" \
ORICHUM_STATE_HOME="$fixture/state" \
ORICHUM_CACHE_HOME="$fixture/cache" \
  "$ROOT/bin/orichum" config validate

models="$(
  ORICHUM_CONFIG_HOME="$ROOT/config" \
  ORICHUM_DATA_HOME="$fixture/data" \
    "$ROOT/bin/orichum" models list
)"
rg -Fq 'gpt-5.6-sol' <<<"$models"
rg -Fq 'claude-opus-5' <<<"$models"
rg -Fq 'claude-opus-4-6-thinking' <<<"$models"

stacks="$(
  ORICHUM_CONFIG_HOME="$ROOT/config" \
  ORICHUM_DATA_HOME="$fixture/data" \
    "$ROOT/bin/orichum" models stacks
)"
rg -Fq 'STACK' <<<"$stacks"
rg -Fq 'balanced' <<<"$stacks"

stack_list="$(
  ORICHUM_CONFIG_HOME="$ROOT/config" \
  ORICHUM_DATA_HOME="$fixture/data" \
    "$ROOT/bin/orichum" stack list
)"
rg -Fq 'STACK' <<<"$stack_list"
rg -Fq 'balanced' <<<"$stack_list"
stack_show="$(
  ORICHUM_CONFIG_HOME="$ROOT/config" \
  ORICHUM_DATA_HOME="$fixture/data" \
    "$ROOT/bin/orichum" stack show balanced
)"
rg -Fq 'ACCOUNT POLICY' <<<"$stack_show"
rg -Fq 'Automatic within provider' <<<"$stack_show"
if ORICHUM_CONFIG_HOME="$ROOT/config" \
    ORICHUM_DATA_HOME="$fixture/data" \
    "$ROOT/bin/orichum" stack configure \
    >"$fixture/noninteractive-stack.stdout" \
    2>"$fixture/noninteractive-stack.stderr"; then
  printf 'non-interactive stack mutation unexpectedly succeeded\n' >&2
  exit 1
fi
rg -Fq 'stack configuration requires an interactive terminal' \
  "$fixture/noninteractive-stack.stderr"

contexts="$(
  ORICHUM_CONFIG_HOME="$ROOT/config" \
  ORICHUM_DATA_HOME="$fixture/data" \
    "$ROOT/bin/orichum" context list
)"
rg -Fq 'ACCOUNT POOLS' <<<"$contexts"
rg -Fq 'mcp-atlassian' "$ROOT/README.md"
rg -Fq 'orichum fork' "$ROOT/docs/sessions.md"
rg -Fq 'orichum models stacks' "$ROOT/docs/sessions.md"
rg -Fq 'orichum stack available' "$ROOT/docs/model-stacks.md"
rg -Fq 'orichum stack configure' "$ROOT/docs/model-stacks.md"
rg -Fq 'orichum stack list' "$ROOT/docs/model-stacks.md"
rg -Fq 'orichum stack show STACK' "$ROOT/docs/model-stacks.md"
rg -Fq 'orichum provider configure' \
  "$ROOT/README.md" \
  "$ROOT/docs/providers-and-accounts.md" \
  "$ROOT/docs/cli-reference.md"
rg -Fq 'orichum setup' \
  "$ROOT/README.md" \
  "$ROOT/docs/installation.md" \
  "$ROOT/docs/providers-and-accounts.md" \
  "$ROOT/docs/cli-reference.md"
rg -Fq 'Advanced and recovery commands' \
  "$ROOT/docs/providers-and-accounts.md"
rg -Fq 'TARGET_STACK' "$ROOT/docs/sessions.md"
if rg -Fq 'claude-heavy' "$ROOT/README.md" "$ROOT/docs"/*.md || \
   rg -Fq 'google-heavy' "$ROOT/README.md" "$ROOT/docs"/*.md; then
  printf 'documentation references model stacks that are not configured\n' >&2
  exit 1
fi
rg -Fq 'https://github.com/orichum/orichum.git' \
  "$ROOT/README.md" "$ROOT/docs/installation.md"
if rg -Fq 'https://github.com/orichum/claudex-workflow' \
    "$ROOT/README.md" "$ROOT/docs/installation.md"; then
  printf 'documentation still uses the previous repository URL\n' >&2
  exit 1
fi
rg -Fq 'macOS on Apple Silicon (native acceptance)' \
  "$ROOT/docs/installation.md"
rg -Fq 'Linux on x86-64 with systemd (native acceptance)' \
  "$ROOT/docs/installation.md"
rg -Fq 'WSL2 on x86-64 with systemd (contract acceptance)' \
  "$ROOT/docs/installation.md"
if rg -Fq 'macOS on Apple Silicon or x86-64' \
    "$ROOT/docs/installation.md"; then
  printf 'installation guide overstates native platform acceptance\n' >&2
  exit 1
fi
[[ "$(rg -c -- '--max-time 4' \
  "$ROOT/controller/plugin/scripts/check-local-services.sh")" == 4 ]]
rg -Fq \
  'Claudex template separates per-session and recovery proxy ports' \
  "$ROOT/doctor.sh"
rg -Fq 'Claudex template is pending provider login' "$ROOT/doctor.sh"
rg -Fq 'provider_login_pending=false' "$ROOT/doctor.sh"
rg -Fq 'provider setup is pending; run orichum setup' "$ROOT/doctor.sh"
rg -Fq 'no provider account is registered; run orichum setup' \
  "$ROOT/integrations/common/orichum_cli.py"
rg -Fq 'Authentication complete. Next: orichum setup' \
  "$ROOT/bin/orichum-login"
rg -Fq 'Manual recovery: orichum provider account add --help' \
  "$ROOT/bin/orichum-login"
rg -Fq 'Private CPython 3.14' "$ROOT/doctor.sh"
rg -Fq 'validate_stack_bindings' "$ROOT/doctor.sh"
rg -Fq 'load_accounts(config_root / "accounts.json")' "$ROOT/doctor.sh"
jq -e '
  .schemaVersion == 1 and
  .controller == "gpt-5.6-terra" and
  (.agents | keys | length) == 5 and
  .jiraProfile == null and
  .githubAccount == "alupao"
' "$ROOT/.orichum/config.json" >/dev/null
for leanctx_install_contract in \
    'provision_leanctx_embeddings' \
    'verified_leanctx_ort_dylib_path' \
    '$WORKFLOW_DATA_ROOT/leanctx/cache'; do
  rg -Fq "$leanctx_install_contract" "$ROOT/install.sh"
done
rg -Fq 'verified_leanctx_ort_dylib_path' "$ROOT/doctor.sh"
rg -Fq '$data_root/leanctx/cache' "$ROOT/doctor.sh"
leanctx_input_fingerprint_block="$(sed -n \
  '/^leanctx_input_sha=/,/LeanCTX installer input fingerprint failed/p' \
  "$ROOT/install.sh")"
rg -Fq 'install.sh lib/workflow.sh' \
  <<<"$leanctx_input_fingerprint_block"
leanctx_probe_fingerprint_block="$(sed -n \
  '/^leanctx_probe_sha=/,/LeanCTX probe fingerprint failed/p' \
  "$ROOT/install.sh")"
rg -Fq 'lib/workflow.sh integrations/common/leanctx_contract.py' \
  <<<"$leanctx_probe_fingerprint_block"
rg -Fq 'integrations/common/mcp_probe.py' \
  <<<"$leanctx_probe_fingerprint_block"
rg -Fq \
  'Display names appear in explicit account and route inspection output.' \
  "$ROOT/docs/providers-and-accounts.md"
rg -Fq 'validate_orichum_python' \
  "$ROOT/bin/orichum-runtime-ready"
for parallel_health_contract in \
    'clip_verify_pid=$!' \
    'route_verify_pid=$!'; do
  rg -Fq "$parallel_health_contract" "$ROOT/bin/orichum-runtime-ready"
done
for python_summary in \
    'Python request: 3.14.x' \
    'Python version:' \
    'Python runtime:' \
    'Python action:'; do
  rg -Fq "$python_summary" "$ROOT/lib/workflow.sh"
done
[[ "$(jq -r '
  .hooks.SessionStart[0].hooks[0].timeout
' "$ROOT/controller/plugin/hooks/hooks.json")" == 6 ]]

audited_workflows="$ROOT/controller/plugin/audited-workflows"
orchestration_guard="$ROOT/controller/plugin/scripts/guard-orchestration.sh"
planning_agent="$ROOT/controller/plugin/agents/planning-advisor.md"
[[ -f "$audited_workflows/investigate.js" ]]
[[ -f "$audited_workflows/review.js" ]]
[[ ! -e "$ROOT/controller/plugin/workflows" ]]
[[ -f "$planning_agent" ]]

routed_plan="$(
  CLAUDE_PLUGIN_ROOT="$ROOT/controller/plugin" \
    "$orchestration_guard" <<'JSON'
{"tool_name":"Agent","tool_input":{"subagent_type":"Plan","description":"Design rollout","prompt":"Produce the safe rollout plan","model":"inherit"}}
JSON
)"
jq -e '
  .hookSpecificOutput.permissionDecision == "allow"
  and .hookSpecificOutput.updatedInput.subagent_type
    == "orichum-controller:planning-advisor"
  and .hookSpecificOutput.updatedInput.description == "Design rollout"
  and .hookSpecificOutput.updatedInput.prompt
    == "Produce the safe rollout plan"
  and .hookSpecificOutput.updatedInput.model == "inherit"
' >/dev/null <<<"$routed_plan"

denied_isolated_plan="$(
  CLAUDE_PLUGIN_ROOT="$ROOT/controller/plugin" \
    "$orchestration_guard" <<'JSON'
{"tool_name":"Agent","tool_input":{"subagent_type":"Plan","description":"Design rollout","prompt":"Produce the safe rollout plan","isolation":"worktree"}}
JSON
)"
jq -e '
  .hookSpecificOutput.permissionDecision == "deny"
' >/dev/null <<<"$denied_isolated_plan"

routed_explore="$(
  CLAUDE_PLUGIN_ROOT="$ROOT/controller/plugin" \
    "$orchestration_guard" <<'JSON'
{"tool_name":"Agent","tool_input":{"subagent_type":"Explore","description":"Inspect module","prompt":"Find the relevant module"}}
JSON
)"
jq -e '
  .hookSpecificOutput.permissionDecision == "allow"
  and .hookSpecificOutput.updatedInput.subagent_type
    == "orichum-controller:repository-explorer"
  and .hookSpecificOutput.updatedInput.description == "Inspect module"
  and .hookSpecificOutput.updatedInput.prompt == "Find the relevant module"
' >/dev/null <<<"$routed_explore"

denied_generic="$(
  CLAUDE_PLUGIN_ROOT="$ROOT/controller/plugin" \
    "$orchestration_guard" <<'JSON'
{"tool_name":"Agent","tool_input":{"subagent_type":"general-purpose","description":"Do everything","prompt":"Act without a bounded role"}}
JSON
)"
jq -e '
  .hookSpecificOutput.permissionDecision == "deny"
' >/dev/null <<<"$denied_generic"

checkpoint_writer="$ROOT/controller/plugin/scripts/save-compaction-checkpoint.sh"
checkpoint_run="$fixture/checkpoint-run"
checkpoint_repo="$fixture/checkpoint-repo"
checkpoint_transcript="$fixture/checkpoint-transcript.jsonl"
jq -e '
  any(
    .hooks.PostCompact[]?;
    .matcher == "manual|auto"
    and any(
      .hooks[]?;
      .command
        == "\"${CLAUDE_PLUGIN_ROOT}/scripts/save-compaction-checkpoint.sh\""
      and .timeout == 5
    )
  )
' "$ROOT/controller/plugin/hooks/hooks.json" >/dev/null
install -d -m 0700 "$checkpoint_run" "$checkpoint_repo"
git -C "$checkpoint_repo" init --quiet
git -C "$checkpoint_repo" config user.name "Orichum Tests"
git -C "$checkpoint_repo" config user.email "tests@orichum.invalid"
printf 'baseline\n' >"$checkpoint_repo/state.txt"
git -C "$checkpoint_repo" add state.txt
git -C "$checkpoint_repo" commit --quiet -m baseline
checkpoint_head="$(git -C "$checkpoint_repo" rev-parse HEAD)"
checkpoint_root="$(git -C "$checkpoint_repo" rev-parse --show-toplevel)"
printf 'dirty\n' >>"$checkpoint_repo/state.txt"
cat >"$checkpoint_transcript" <<'JSONL'
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Agent","id":"agent-success-1","input":{"subagent_type":"orichum-controller:repository-explorer","description":"Inspect AKS Terraform setup","prompt":"secret prompt that must not be stored"}}]}}
{"type":"user","toolUseResult":{"status":"completed"},"message":{"content":[{"type":"tool_result","tool_use_id":"agent-success-1","content":[{"type":"text","text":"large result that must not be stored"}]}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Agent","id":"agent-denied","input":{"subagent_type":"Plan","description":"Denied built-in plan","prompt":"must not survive"}}]}}
{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"agent-denied","is_error":true,"content":"Agent type is not in the Orichum controller allowlist: Plan"}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Agent","id":"agent-incomplete","input":{"subagent_type":"orichum-controller:repository-verifier","description":"Still running","prompt":"must not survive"}}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Agent","id":"agent-success-2","input":{"subagent_type":"orichum-controller:planning-advisor","description":"Design usa-dev setup plan","prompt":"another secret prompt"}}]}}
{"type":"user","toolUseResult":{"status":"completed"},"message":{"content":[{"type":"tool_result","tool_use_id":"agent-success-2","content":"completed output that must not be stored"}]}}
JSONL
chmod 0600 "$checkpoint_transcript"
CLAUDEX_RUN_DIR="$checkpoint_run" \
  "$checkpoint_writer" <<JSON
{"session_id":"checkpoint-session","trigger":"manual","cwd":"$checkpoint_repo","transcript_path":"$checkpoint_transcript","compact_summary":"Continue with the approved implementation and do not repeat completed reconnaissance."}
JSON
checkpoint_file="$checkpoint_run/compaction-checkpoint.json"
[[ -f "$checkpoint_file" && ! -L "$checkpoint_file" ]]
[[ "$(path_mode "$checkpoint_file")" == 600 ]]
jq -e \
  --arg head "$checkpoint_head" \
  --arg root "$checkpoint_root" \
  --arg cwd "$checkpoint_repo" '
  .schemaVersion == 1
  and .sessionId == "checkpoint-session"
  and .trigger == "manual"
  and .cwd == $cwd
  and .compactSummary
    == "Continue with the approved implementation and do not repeat completed reconnaissance."
  and .repository == {root: $root, head: $head, dirty: true}
  and .completedAgents == [
    {
      type: "orichum-controller:repository-explorer",
      description: "Inspect AKS Terraform setup"
    },
    {
      type: "orichum-controller:planning-advisor",
      description: "Design usa-dev setup plan"
    }
  ]
  and (tostring | contains("secret prompt") | not)
  and (tostring | contains("large result") | not)
  and (tostring | contains("Denied built-in plan") | not)
  and (tostring | contains("Still running") | not)
' "$checkpoint_file" >/dev/null

checkpoint_restore="$ROOT/controller/plugin/scripts/restore-compaction-checkpoint.sh"
jq -e '
  any(
    .hooks.SessionStart[]?;
    .matcher == "compact"
    and any(
      .hooks[]?;
      .command
        == "\"${CLAUDE_PLUGIN_ROOT}/scripts/restore-compaction-checkpoint.sh\""
      and .timeout == 5
    )
  )
' "$ROOT/controller/plugin/hooks/hooks.json" >/dev/null
restored_checkpoint="$(
  CLAUDEX_RUN_DIR="$checkpoint_run" \
    "$checkpoint_restore" <<JSON
{"session_id":"checkpoint-session","source":"compact","cwd":"$checkpoint_repo"}
JSON
)"
jq -e '
  .hookSpecificOutput.hookEventName == "SessionStart"
  and (
    .hookSpecificOutput.additionalContext |
    contains("The compact summary is authoritative")
  )
  and (
    .hookSpecificOutput.additionalContext |
    contains("Repository state matches the compaction checkpoint")
  )
  and (
    .hookSpecificOutput.additionalContext |
    contains("Do not redispatch equivalent completed investigations")
  )
  and (
    .hookSpecificOutput.additionalContext |
    contains("Inspect AKS Terraform setup")
  )
  and (
    .hookSpecificOutput.additionalContext |
    contains("Design usa-dev setup plan")
  )
  and (
    .hookSpecificOutput.additionalContext |
    contains("Continue with the approved implementation and do not repeat completed reconnaissance.") |
    not
  )
  and (.hookSpecificOutput.additionalContext | length <= 8192)
' >/dev/null <<<"$restored_checkpoint"

mismatched_checkpoint="$(
  CLAUDEX_RUN_DIR="$checkpoint_run" \
    "$checkpoint_restore" <<JSON
{"session_id":"different-session","source":"compact","cwd":"$checkpoint_repo"}
JSON
)"
[[ -z "$mismatched_checkpoint" ]]

malformed_checkpoint_run="$fixture/malformed-checkpoint-run"
install -d -m 0700 "$malformed_checkpoint_run"
printf 'not-json\n' \
  >"$malformed_checkpoint_run/compaction-checkpoint.json"
chmod 0600 "$malformed_checkpoint_run/compaction-checkpoint.json"
malformed_checkpoint="$(
  CLAUDEX_RUN_DIR="$malformed_checkpoint_run" \
    "$checkpoint_restore" <<JSON
{"session_id":"checkpoint-session","source":"compact","cwd":"$checkpoint_repo"}
JSON
)"
[[ -z "$malformed_checkpoint" ]]

oversized_checkpoint_run="$fixture/oversized-checkpoint-run"
install -d -m 0700 "$oversized_checkpoint_run"
head -c 524289 /dev/zero \
  >"$oversized_checkpoint_run/compaction-checkpoint.json"
chmod 0600 "$oversized_checkpoint_run/compaction-checkpoint.json"
oversized_checkpoint="$(
  CLAUDEX_RUN_DIR="$oversized_checkpoint_run" \
    "$checkpoint_restore" <<JSON
{"session_id":"checkpoint-session","source":"compact","cwd":"$checkpoint_repo"}
JSON
)"
[[ -z "$oversized_checkpoint" ]]

symlinked_checkpoint_run="$fixture/symlinked-checkpoint-run"
install -d -m 0700 "$symlinked_checkpoint_run"
ln -s "$checkpoint_file" \
  "$symlinked_checkpoint_run/compaction-checkpoint.json"
symlinked_checkpoint="$(
  CLAUDEX_RUN_DIR="$symlinked_checkpoint_run" \
    "$checkpoint_restore" <<JSON
{"session_id":"checkpoint-session","source":"compact","cwd":"$checkpoint_repo"}
JSON
)"
[[ -z "$symlinked_checkpoint" ]]

git -C "$checkpoint_repo" add state.txt
git -C "$checkpoint_repo" commit --quiet -m changed
changed_checkpoint="$(
  CLAUDEX_RUN_DIR="$checkpoint_run" \
    "$checkpoint_restore" <<JSON
{"session_id":"checkpoint-session","source":"compact","cwd":"$checkpoint_repo"}
JSON
)"
jq -e '
  .hookSpecificOutput.hookEventName == "SessionStart"
  and (
    .hookSpecificOutput.additionalContext |
    contains("Repository state changed since the compaction checkpoint")
  )
  and (
    .hookSpecificOutput.additionalContext |
    contains("Revalidate only the changed repository boundaries")
  )
' >/dev/null <<<"$changed_checkpoint"

allowed_workflow="$(
  CLAUDE_PLUGIN_ROOT="$ROOT/controller/plugin" \
    "$orchestration_guard" <<JSON
{"tool_name":"Workflow","tool_input":{"scriptPath":"$audited_workflows/investigate.js","args":{"question":"q","scope":"s","highRisk":false}}}
JSON
)"
[[ -z "$allowed_workflow" ]]

for denied_workflow in \
    '{"tool_name":"Workflow","tool_input":{"name":"orichum-controller:orichum-investigate","args":"q"}}' \
    "{\"tool_name\":\"Workflow\",\"tool_input\":{\"scriptPath\":\"$ROOT/controller/plugin/workflows/investigate.js\",\"args\":{\"question\":\"q\",\"scope\":\"s\",\"highRisk\":false}}}"; do
  denied_output="$(
    CLAUDE_PLUGIN_ROOT="$ROOT/controller/plugin" \
      "$orchestration_guard" <<<"$denied_workflow"
  )"
  jq -e '
    .hookSpecificOutput.permissionDecision == "deny"
  ' >/dev/null <<<"$denied_output"
done

for obsolete in \
  claudex-context claudex-doctor claudex-gpt \
  claudex-login claudex-models claudex-plugin \
  claudex-provider; do
  [[ ! -e "$ROOT/bin/$obsolete" ]]
done

amd64_workflow="$ROOT/.github/workflows/amd64-acceptance.yml"
[[ -f "$amd64_workflow" ]]
for required_contract in \
    'name: Native AMD64 acceptance' \
    'workflow_dispatch:' \
    'permissions:' \
    'contents: read' \
    'runs-on: blacksmith-4vcpu-ubuntu-2404' \
    'timeout-minutes: 30' \
    'uses: astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b # v8.1.0' \
    'sudo apt-get install --yes ripgrep' \
    'PATH="$poison_bin:$USER_BIN_DIR:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"' \
    'Run repository test suites' \
    'Fresh install without providers' \
    'Activate disposable multi-family routes' \
    'tests/test_live_stack_routes.sh' \
    'tests/test_orichum_launcher.sh' \
    'Verify LeanCTX code-intelligence contract' \
    'probe_leanctx_capabilities' \
    'verified_leanctx_ort_dylib_path' \
    '$ORICHUM_DATA_HOME/leanctx/cache' \
    'name: Linux AMD64 acceptance' \
    'ubuntu:24.04' \
    '--privileged' \
    'loginctl enable-linger orichum' \
    'Verify fast repeat and explicit upgrade' \
    'repeat_started="$(python3 -c' \
    'test "$repeat_ms" -lt 15000' \
    'orichum-fast.log' \
    "grep -Fxq 'Orichum is ready.'" \
    './install.sh --upgrade --verbose' \
    'orichum-upgrade.log' \
    '^Controller plugin[[:space:]]+upgraded' \
    'orichum setup --verbose' \
    'setup-*.log' \
    '.components.routing | not' \
    'Running Orichum doctor'; do
  rg -Fq -- "$required_contract" "$amd64_workflow"
done
if rg -q '^  push:' "$amd64_workflow"; then
  printf 'AMD64 acceptance must not repeat verified PR work after merge\n' >&2
  exit 1
fi
if rg -q '^  pull_request:' "$amd64_workflow"; then
  printf 'AMD64 acceptance must run only when explicitly dispatched\n' >&2
  exit 1
fi
set +e
rg -q 'secrets[.]|\$\{\{[[:space:]]*secrets' "$amd64_workflow"
secret_scan_rc=$?
set -e
case "$secret_scan_rc" in
  0)
    printf 'AMD64 acceptance workflow must not consume repository secrets\n' >&2
    exit 1
    ;;
  1) ;;
  *)
    printf 'AMD64 acceptance workflow secret scan failed (rc=%s)\n' \
      "$secret_scan_rc" >&2
    exit 1
    ;;
esac

macos_workflow="$ROOT/.github/workflows/macos-arm64-acceptance.yml"
[[ -f "$macos_workflow" ]]
for required_contract in \
    'name: Native macOS ARM64 acceptance' \
    'workflow_dispatch:' \
    'permissions:' \
    'contents: read' \
    'runs-on: blacksmith-6vcpu-macos-15' \
    'GH_TOKEN: ${{ github.token }}' \
    'test "$(uname -m)" = arm64' \
    'brew install ripgrep' \
    'launchctl print "gui/$(id -u)/io.orichum.cliproxy"' \
    'launchctl print "gui/$(id -u)/io.orichum.route-proxy"' \
    'Fresh install without providers' \
    'Activate disposable multi-family routes' \
    'Verify LeanCTX code-intelligence contract' \
    'probe_leanctx_capabilities' \
    'verified_leanctx_ort_dylib_path' \
    '$ORICHUM_DATA_HOME/leanctx/cache' \
    'Verify fast repeat and explicit upgrade' \
    'repeat_started="$(python3 -c' \
    'test "$repeat_ms" -lt 15000' \
    'orichum-fast.log' \
    "grep -Fxq 'Orichum is ready.'" \
    './install.sh --upgrade --verbose' \
    'orichum-upgrade.log' \
    '^Controller plugin[[:space:]]+upgraded' \
    'orichum setup --verbose' \
    'setup-*.log' \
    '.components.routing | not' \
    'Running Orichum doctor' \
    'Clean up launch agents'; do
  rg -Fq -- "$required_contract" "$macos_workflow"
done
if rg -q '^  push:' "$macos_workflow"; then
  printf 'macOS acceptance must not repeat verified PR work after merge\n' >&2
  exit 1
fi
if rg -q '^  pull_request:' "$macos_workflow"; then
  printf 'macOS acceptance must run only when explicitly dispatched\n' >&2
  exit 1
fi
if rg -Fq 'Run repository test suites' "$macos_workflow"; then
  printf 'macOS acceptance must not repeat platform-neutral repository tests\n' >&2
  exit 1
fi
if rg -Fq 'macos-15-intel' "$macos_workflow"; then
  printf 'macOS acceptance must run on Apple Silicon only\n' >&2
  exit 1
fi
set +e
rg -q 'secrets[.]|\$\{\{[[:space:]]*secrets' "$macos_workflow"
macos_secret_scan_rc=$?
set -e
case "$macos_secret_scan_rc" in
  0)
    printf 'macOS acceptance workflow must not consume repository secrets\n' >&2
    exit 1
    ;;
  1) ;;
  *)
    printf 'macOS acceptance workflow secret scan failed (rc=%s)\n' \
      "$macos_secret_scan_rc" >&2
    exit 1
    ;;
esac

for installer_document in \
    "$ROOT/docs/installation.md" \
    "$ROOT/docs/cli-reference.md"; do
  rg -Fq './install.sh --upgrade' "$installer_document"
done
if rg -Fq './install.sh' "$ROOT/README.md"; then
  printf 'README still directs bootstrap users to a relative installer path\n' >&2
  exit 1
fi
rg -Fq '~/.local/share/orichum/install.sh --uninstall' "$ROOT/README.md"
rg -Fq 'Rerun the [bootstrap command](#install)' "$ROOT/README.md"
for installation_contract in \
    'Fast reconciliation' \
    'about 10 seconds' \
    'state/install-state.json' \
    'identities and digests, not secrets'; do
  rg -Fq "$installation_contract" "$ROOT/docs/installation.md"
done

printf 'PASS: Orichum command and control-plane smoke\n'

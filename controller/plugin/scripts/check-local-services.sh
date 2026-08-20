#!/usr/bin/env bash
set -euo pipefail

WORKFLOW_ROOT="${CLAUDEX_WORKFLOW_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
# shellcheck source=../../../lib/workflow.sh
source "$WORKFLOW_ROOT/lib/workflow.sh"
WORKFLOW_DATA_ROOT="$(workflow_data_dir)"
if ! IFS=$'\t' read -r \
    CLIPROXY_PORT _ ROUTE_PROXY_PORT LEANCTX_PROXY_PORT \
    < <(read_service_ports "$WORKFLOW_DATA_ROOT"); then
  jq -cn '{systemMessage:"Orichum health warning: service port configuration is invalid."}'
  exit 0
fi
SESSION_CLAUDEX_PORT_FILE="${CLAUDEX_RUN_DIR:-}/claudex-proxy-port"
if [[ -z "${CLAUDEX_RUN_DIR:-}" ]] || \
   [[ ! -f "$SESSION_CLAUDEX_PORT_FILE" || \
      -L "$SESSION_CLAUDEX_PORT_FILE" ]] || \
   [[ "$(path_mode "$SESSION_CLAUDEX_PORT_FILE" 2>/dev/null || true)" != 600 ]] || \
   ! SESSION_CLAUDEX_PORT="$(<"$SESSION_CLAUDEX_PORT_FILE")" || \
   ! valid_service_port "$SESSION_CLAUDEX_PORT"; then
  jq -cn \
    '{systemMessage:"Orichum health warning: session Claudex proxy state is invalid."}'
  exit 0
fi

tmp_dir=""
models_response=""
models_error=""
claudex_response=""
claudex_error=""
session_response=""
session_error=""
leanctx_response=""
leanctx_error=""

# shellcheck disable=SC2329 # Invoked indirectly by the EXIT trap.
cleanup() {
  if [[ -n "$models_response" ]]; then
    rm -f -- "$models_response" || :
  fi
  if [[ -n "$models_error" ]]; then
    rm -f -- "$models_error" || :
  fi
  if [[ -n "$claudex_response" ]]; then
    rm -f -- "$claudex_response" || :
  fi
  if [[ -n "$claudex_error" ]]; then
    rm -f -- "$claudex_error" || :
  fi
  if [[ -n "$session_response" ]]; then
    rm -f -- "$session_response" || :
  fi
  if [[ -n "$session_error" ]]; then
    rm -f -- "$session_error" || :
  fi
  if [[ -n "$leanctx_response" ]]; then
    rm -f -- "$leanctx_response" || :
  fi
  if [[ -n "$leanctx_error" ]]; then
    rm -f -- "$leanctx_error" || :
  fi
  if [[ -n "$tmp_dir" ]]; then
    rmdir "$tmp_dir" 2>/dev/null || :
  fi
}
trap cleanup EXIT

emit_warning() {
  jq -cn --arg warning "$1" '{systemMessage:$warning}'
}

if ! tmp_dir="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/claudex-health.XXXXXX" 2>/dev/null)"; then
  emit_warning "Orichum health warning: local service health check could not create private temporary state."
  exit 0
fi
if ! chmod 0700 "$tmp_dir" 2>/dev/null; then
  emit_warning "Orichum health warning: local service health check could not secure private temporary state."
  exit 0
fi

models_response="$tmp_dir/models.response"
models_error="$tmp_dir/models.error"
claudex_response="$tmp_dir/claudex.response"
claudex_error="$tmp_dir/claudex.error"
session_response="$tmp_dir/session.response"
session_error="$tmp_dir/session.error"
leanctx_response="$tmp_dir/leanctx.response"
leanctx_error="$tmp_dir/leanctx.error"
if ! (umask 077
  : >"$models_response"
  : >"$models_error"
  : >"$claudex_response"
  : >"$claudex_error"
  : >"$session_response"
  : >"$session_error"
  : >"$leanctx_response"
  : >"$leanctx_error"
  chmod 0600 \
    "$models_response" "$models_error" \
    "$claudex_response" "$claudex_error" \
    "$session_response" "$session_error" \
    "$leanctx_response" "$leanctx_error"
) 2>/dev/null; then
  emit_warning "Orichum health warning: local service health check could not secure response files."
  exit 0
fi

curl --fail --silent --show-error --connect-timeout 1 --max-time 4 \
  "http://127.0.0.1:$CLIPROXY_PORT/v1/models" \
  >"$models_response" 2>"$models_error" &
models_pid=$!
curl --fail --silent --show-error --connect-timeout 1 --max-time 4 \
  "http://127.0.0.1:$ROUTE_PROXY_PORT/v1/models" \
  >"$claudex_response" 2>"$claudex_error" &
claudex_pid=$!
curl --fail --silent --show-error --connect-timeout 1 --max-time 4 \
  "http://127.0.0.1:$SESSION_CLAUDEX_PORT/health" \
  >"$session_response" 2>"$session_error" &
session_pid=$!
curl --fail --silent --show-error --connect-timeout 1 --max-time 4 \
  "http://127.0.0.1:$LEANCTX_PROXY_PORT/health" \
  >"$leanctx_response" 2>"$leanctx_error" &
leanctx_pid=$!

models_status=0
claudex_status=0
session_status=0
leanctx_status=0
if wait "$models_pid"; then
  :
else
  models_status=$?
fi
if wait "$claudex_pid"; then
  :
else
  claudex_status=$?
fi
if wait "$session_pid"; then
  :
else
  session_status=$?
fi
if wait "$leanctx_pid"; then
  :
else
  leanctx_status=$?
fi

warning=""
if [[ "$models_status" -ne 0 || "$claudex_status" -ne 0 || \
      "$session_status" -ne 0 || "$leanctx_status" -ne 0 ]]; then
  warning="Orichum health warning: a bounded local Claudex, CLIProxyAPI, LeanCTX, or Orichum proxy request failed."
else
  effective_models_file="${CLAUDEX_EFFECTIVE_MODELS_FILE:-}"
  effective_controller=""
  if [[ -z "${CLAUDEX_RUN_DIR:-}" ]] || \
     [[ "$effective_models_file" != \
        "$CLAUDEX_RUN_DIR/effective-models.json" ]] || \
     [[ ! -f "$effective_models_file" || -L "$effective_models_file" ]] || \
     [[ "$(path_mode "$effective_models_file" 2>/dev/null || true)" != 600 ]] || \
     ! effective_controller="$(jq -er '
       def model:
         type == "string" and
         test("^[A-Za-z0-9][A-Za-z0-9._:/@+\\\\-]{0,254}$");
       . as $document |
       ($document | keys) == [
         "agents",
         "configuredCandidates",
         "controller",
         "schemaVersion",
         "stack"
       ] and
       .schemaVersion == 1 and
       (.stack | type == "string" and length > 0) and
       (.controller | model) and
       (.agents | type == "object") and
       (
         (.agents | keys) == [
           "architecture-advisor",
           "correctness-critic",
           "implementation-worker",
           "planning-advisor",
           "repository-explorer",
           "repository-verifier"
         ] or
         (.agents | keys) == [
           "architecture-advisor",
           "correctness-critic",
           "implementation-worker",
           "repository-explorer",
           "repository-verifier"
         ]
       ) and
       (.agents | all(.[]; model)) |
       select(.) |
       $document.controller
     ' "$effective_models_file" 2>/dev/null)"; then
    warning="Orichum health warning: immutable session effective model mapping is missing or invalid."
  elif ! jq -e '
    (.data | type == "array" and length > 0) and
    all(
      .data[];
      (.id | type == "string") and
      (.id | test("^[A-Za-z0-9][A-Za-z0-9._:/@+\\\\-]{0,254}$"))
    )
  ' "$models_response" >/dev/null 2>&1; then
    warning="Orichum health warning: CLIProxyAPI model catalogue is invalid."
  else
    missing_models="$(jq -r \
      --slurpfile effective "$effective_models_file" '
      [.data[]?.id | select(type == "string")] as $available |
      [
        $effective[0].controller,
        ($effective[0].agents[]?)
      ] |
      unique |
      map(select(. as $model | $available | index($model) | not)) |
      join(", ")
    ' "$models_response" 2>/dev/null || true)"
    if [[ -n "$missing_models" ]]; then
      warning="Orichum health warning: required effective model missing: $missing_models."
    fi
  fi
fi

if [[ -z "$warning" ]]; then
  if [[ "$(<"$session_response")" != ok ]]; then
    warning="Orichum health warning: session Claudex proxy is not healthy."
  elif [[ -z "$effective_controller" ]] || \
     ! claudex_proxy_models_response_is_ready \
       "$claudex_response" "$effective_controller"; then
    warning="Orichum health warning: persistent Orichum proxy does not expose the configured controller model."
  fi
fi

if [[ -n "$warning" ]]; then
  emit_warning "$warning"
fi
exit 0

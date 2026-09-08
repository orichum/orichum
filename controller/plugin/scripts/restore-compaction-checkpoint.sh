#!/usr/bin/env bash
set -u

umask 077

readonly MAX_HOOK_INPUT_BYTES=65536
readonly MAX_CHECKPOINT_BYTES=524288

stat_owner() {
  case "$(uname -s)" in
    Darwin) stat -f '%u' "$1" 2>/dev/null ;;
    Linux) stat -c '%u' "$1" 2>/dev/null ;;
    *) return 1 ;;
  esac
}

stat_mode() {
  case "$(uname -s)" in
    Darwin) stat -f '%Lp' "$1" 2>/dev/null ;;
    Linux) stat -c '%a' "$1" 2>/dev/null ;;
    *) return 1 ;;
  esac
}

stat_size() {
  case "$(uname -s)" in
    Darwin) stat -f '%z' "$1" 2>/dev/null ;;
    Linux) stat -c '%s' "$1" 2>/dev/null ;;
    *) return 1 ;;
  esac
}

stat_identity() {
  case "$(uname -s)" in
    Darwin) stat -f '%d:%i:%z:%m' "$1" 2>/dev/null ;;
    Linux) stat -c '%d:%i:%s:%Y' "$1" 2>/dev/null ;;
    *) return 1 ;;
  esac
}

safe_private_directory() {
  local directory="$1"
  local mode=""
  local owner=""

  [[ -n "$directory" && "$directory" == /* ]] || return 1
  [[ -d "$directory" && ! -L "$directory" ]] || return 1
  owner="$(stat_owner "$directory")" || return 1
  [[ "$owner" == "$(id -u)" ]] || return 1
  mode="$(stat_mode "$directory")" || return 1
  [[ "$mode" =~ ^[0-7]{3,4}$ ]] || return 1
  (( (8#$mode & 077) == 0 ))
}

read_hook_input() {
  local input=""
  local size=""

  input="$(LC_ALL=C head -c $((MAX_HOOK_INPUT_BYTES + 1)))" || return 1
  size="$(LC_ALL=C printf '%s' "$input" | wc -c | tr -d '[:space:]')" || return 1
  [[ "$size" =~ ^[0-9]+$ ]] || return 1
  (( size <= MAX_HOOK_INPUT_BYTES )) || return 1
  printf '%s' "$input"
}

read_checkpoint() {
  local checkpoint="$1"
  local before=""
  local after=""
  local mode=""
  local owner=""
  local size=""
  local document=""

  [[ -f "$checkpoint" && ! -L "$checkpoint" ]] || return 1
  owner="$(stat_owner "$checkpoint")" || return 1
  [[ "$owner" == "$(id -u)" ]] || return 1
  mode="$(stat_mode "$checkpoint")" || return 1
  [[ "$mode" == 600 ]] || return 1
  size="$(stat_size "$checkpoint")" || return 1
  [[ "$size" =~ ^[0-9]+$ ]] || return 1
  (( size <= MAX_CHECKPOINT_BYTES )) || return 1
  before="$(stat_identity "$checkpoint")" || return 1
  document="$(cat -- "$checkpoint")" || return 1
  [[ -f "$checkpoint" && ! -L "$checkpoint" ]] || return 1
  after="$(stat_identity "$checkpoint")" || return 1
  [[ "$before" == "$after" ]] || return 1
  printf '%s' "$document"
}

repository_state() {
  local cwd="$1"
  local root=""
  local head=""
  local status=""
  local dirty=false

  if [[ "$cwd" != /* || ! -d "$cwd" || -L "$cwd" ]]; then
    printf 'null'
    return 0
  fi
  root="$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null)" || {
    printf 'null'
    return 0
  }
  head="$(git -C "$root" rev-parse HEAD 2>/dev/null)" || head=""
  status="$(git -C "$root" status --porcelain=v1 --untracked-files=normal 2>/dev/null | head -n 1)"
  [[ -z "$status" ]] || dirty=true
  jq -cn \
    --arg root "$root" \
    --arg head "$head" \
    --argjson dirty "$dirty" \
    '{root: $root, head: (if $head == "" then null else $head end), dirty: $dirty}'
}

agent_lines() {
  local checkpoint="$1"

  jq -r '
    .completedAgents[:20][] |
    "- [" + (.type[0:128] | gsub("[\\r\\n\\t]"; " ")) + "] " +
    (.description[0:200] | gsub("[\\r\\n\\t]"; " "))
  ' <<<"$checkpoint"
}

main() {
  local input=""
  local run_dir="${CLAUDEX_RUN_DIR:-}"
  local checkpoint_file=""
  local checkpoint=""
  local session_id=""
  local cwd=""
  local stored_session=""
  local stored_repository='null'
  local current_repository='null'
  local completed=""
  local continuity=""

  safe_private_directory "$run_dir" || return 0
  input="$(read_hook_input)" || return 0
  jq -e 'type == "object"' >/dev/null 2>&1 <<<"$input" || return 0
  session_id="$(jq -er '
    .session_id | select(type == "string" and test("^[A-Za-z0-9_-]{1,128}$"))
  ' <<<"$input" 2>/dev/null)" || return 0
  jq -e '.source == "compact"' >/dev/null 2>&1 <<<"$input" || return 0
  cwd="$(jq -er '
    .cwd | select(type == "string" and length > 0 and length <= 4096)
  ' <<<"$input" 2>/dev/null)" || return 0
  checkpoint_file="$run_dir/compaction-checkpoint-$session_id.json"
  if [[ ! -e "$checkpoint_file" && ! -L "$checkpoint_file" ]]; then
    # Old snapshots stored one checkpoint. The session identity check below
    # still prevents a background fork from restoring its parent's state.
    checkpoint_file="$run_dir/compaction-checkpoint.json"
  fi
  checkpoint="$(read_checkpoint "$checkpoint_file")" || return 0
  jq -e '
    type == "object"
    and .schemaVersion == 1
    and (.sessionId | type == "string" and length > 0 and length <= 256)
    and (.trigger == "manual" or .trigger == "auto")
    and (.cwd | type == "string" and length > 0 and length <= 4096)
    and (.compactSummary | type == "string" and length > 0 and length <= 262144)
    and (
      .repository == null
      or (
        .repository | type == "object"
        and (.root | type == "string" and length > 0 and length <= 4096)
        and (
          .head == null
          or (.head | type == "string" and test("^[0-9a-fA-F]{40,64}$"))
        )
        and (.dirty | type == "boolean")
      )
    )
    and (.completedAgents | type == "array" and length <= 64)
    and all(
      .completedAgents[];
      (.type | type == "string" and length > 0 and length <= 256)
      and (.description | type == "string" and length > 0 and length <= 512)
    )
  ' >/dev/null 2>&1 <<<"$checkpoint" || return 0
  stored_session="$(jq -r '.sessionId' <<<"$checkpoint")" || return 0
  [[ "$stored_session" == "$session_id" ]] || return 0
  stored_repository="$(jq -c '.repository' <<<"$checkpoint")" || return 0
  current_repository="$(repository_state "$cwd")" || current_repository='null'
  completed="$(agent_lines "$checkpoint")" || return 0
  if [[ -z "$completed" ]]; then
    completed="- No completed audited agent investigations were recorded."
  fi

  if [[ "$stored_repository" != null && \
        "$current_repository" == "$stored_repository" ]]; then
    continuity="The compact summary is authoritative. Continue from its pending work without reconstructing the prior conversation.
Repository state matches the compaction checkpoint. Do not redispatch equivalent completed investigations:
$completed
Revalidate only unresolved claims or external state that can change."
  else
    continuity="The compact summary is authoritative. Continue from its pending work without reconstructing the prior conversation.
Repository state changed since the compaction checkpoint. Revalidate only the changed repository boundaries before implementation; do not repeat completed investigations unrelated to those changes:
$completed"
  fi
  jq -cn \
    --arg context "$continuity" \
    '{
      hookSpecificOutput: {
        hookEventName: "SessionStart",
        additionalContext: $context
      }
    }'
}

main || :
exit 0

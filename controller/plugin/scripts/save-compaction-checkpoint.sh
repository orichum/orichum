#!/usr/bin/env bash
set -u

umask 077

readonly MAX_HOOK_INPUT_BYTES=1048576
readonly MAX_TRANSCRIPT_BYTES=67108864
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

safe_transcript() {
  local transcript="$1"
  local owner=""
  local size=""

  [[ -n "$transcript" && "$transcript" == /* ]] || return 1
  [[ -f "$transcript" && ! -L "$transcript" ]] || return 1
  owner="$(stat_owner "$transcript")" || return 1
  [[ "$owner" == "$(id -u)" ]] || return 1
  size="$(stat_size "$transcript")" || return 1
  [[ "$size" =~ ^[0-9]+$ ]] || return 1
  (( size <= MAX_TRANSCRIPT_BYTES ))
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

completed_agents() {
  local transcript="$1"

  jq -sc '
    reduce .[] as $row (
      {calls: {}, completed: []};
      reduce (
        ($row.message.content? // []) |
        if type == "array" then .[] else empty end
      ) as $item (.;
        if (
          $row.type == "assistant"
          and $item.type == "tool_use"
          and $item.name == "Agent"
          and ($item.id | type == "string" and length > 0 and length <= 256)
          and (
            $item.input.subagent_type |
            type == "string" and length > 0 and length <= 256
          )
          and (
            $item.input.description |
            type == "string" and length > 0 and length <= 512
          )
        ) then
          .calls[$item.id] = {
            type: $item.input.subagent_type,
            description: $item.input.description
          }
        elif (
          $row.type == "user"
          and $item.type == "tool_result"
          and ($item.tool_use_id | type == "string")
          and .calls[$item.tool_use_id] != null
          and ($row.toolUseResult.status? == "completed")
          and (($item.is_error // false) == false)
          and (
            (($item.content // "") | tostring) |
            contains("Agent type is not in the Orichum controller allowlist") |
            not
          )
        ) then
          ($item.tool_use_id) as $id |
          if any(.completed[]; .id == $id) then
            .
          else
            .completed += [(.calls[$id] + {id: $id})]
          end
        else
          .
        end
      )
    ) |
    [.completed[:64][] | {type, description}]
  ' "$transcript" 2>/dev/null
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

write_checkpoint() {
  local run_dir="$1"
  local document="$2"
  local checkpoint="$run_dir/compaction-checkpoint-$3.json"
  local temporary=""
  local size=""

  if [[ -e "$checkpoint" || -L "$checkpoint" ]]; then
    [[ -f "$checkpoint" && ! -L "$checkpoint" ]] || return 1
    [[ "$(stat_owner "$checkpoint")" == "$(id -u)" ]] || return 1
  fi
  temporary="$(mktemp "$run_dir/.compaction-checkpoint.XXXXXX")" || return 1
  if ! chmod 0600 "$temporary" || ! printf '%s\n' "$document" >"$temporary"; then
    rm -f -- "$temporary"
    return 1
  fi
  size="$(stat_size "$temporary")" || {
    rm -f -- "$temporary"
    return 1
  }
  if (( size > MAX_CHECKPOINT_BYTES )); then
    rm -f -- "$temporary"
    return 1
  fi
  if ! mv -f -- "$temporary" "$checkpoint"; then
    rm -f -- "$temporary"
    return 1
  fi
}

main() {
  local input=""
  local run_dir="${CLAUDEX_RUN_DIR:-}"
  local session_id=""
  local trigger=""
  local cwd=""
  local transcript=""
  local summary=""
  local agents='[]'
  local repository='null'
  local document=""

  safe_private_directory "$run_dir" || return 0
  input="$(read_hook_input)" || return 0
  jq -e 'type == "object"' >/dev/null 2>&1 <<<"$input" || return 0
  session_id="$(jq -er '
    .session_id | select(type == "string" and test("^[A-Za-z0-9_-]{1,128}$"))
  ' <<<"$input" 2>/dev/null)" || return 0
  trigger="$(jq -er '
    .trigger | select(. == "manual" or . == "auto")
  ' <<<"$input" 2>/dev/null)" || return 0
  cwd="$(jq -er '
    .cwd | select(type == "string" and length > 0 and length <= 4096)
  ' <<<"$input" 2>/dev/null)" || return 0
  transcript="$(jq -er '
    .transcript_path |
    select(type == "string" and length > 0 and length <= 4096)
  ' <<<"$input" 2>/dev/null)" || return 0
  summary="$(jq -er '
    .compact_summary |
    select(type == "string" and length > 0 and length <= 262144)
  ' <<<"$input" 2>/dev/null)" || return 0
  safe_transcript "$transcript" || return 0
  agents="$(completed_agents "$transcript")" || return 0
  repository="$(repository_state "$cwd")" || repository='null'
  document="$(jq -cn \
    --arg session_id "$session_id" \
    --arg trigger "$trigger" \
    --arg cwd "$cwd" \
    --arg compact_summary "$summary" \
    --argjson repository "$repository" \
    --argjson completed_agents "$agents" \
    '{
      schemaVersion: 1,
      sessionId: $session_id,
      trigger: $trigger,
      cwd: $cwd,
      compactSummary: $compact_summary,
      repository: $repository,
      completedAgents: $completed_agents
    }'
  )" || return 0
  write_checkpoint "$run_dir" "$document" "$session_id" || return 0
}

main || :
exit 0

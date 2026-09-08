#!/usr/bin/env bash

orichum_uninstall_validate_private_root() {
  local candidate="$1"
  local checkout_root="$2"
  local label="$3"
  workflow_python - "$candidate" "$HOME" "$checkout_root" "$label" <<'PY'
import os
import stat
import sys
from pathlib import Path

raw, home_raw, checkout_raw, label = sys.argv[1:]
if not os.path.isabs(raw):
    raise SystemExit(f"{label} must be an absolute path")

normalized = Path(os.path.normpath(raw))
cursor = Path(normalized.anchor)
for component in normalized.parts[1:]:
    cursor /= component
    try:
        value = os.lstat(cursor)
    except FileNotFoundError:
        break
    except OSError as error:
        raise SystemExit(f"{label} existing ancestor is inaccessible") from error
    if stat.S_ISLNK(value.st_mode):
        raise SystemExit(f"{label} existing ancestors must not be symlinks")
    if cursor != normalized and not stat.S_ISDIR(value.st_mode):
        raise SystemExit(f"{label} existing ancestor is not a directory")

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
if candidate in (root, home, checkout) or inside_checkout:
    raise SystemExit(f"refusing unsafe {label}")
if candidate.exists():
    value = candidate.stat()
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid():
        raise SystemExit(f"{label} is not a private current-user directory")
print(candidate, end="")
PY
}

orichum_uninstall_preflight_runtime() {
  local data_root="$1"
  workflow_python - "$data_root" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
if not root.exists():
    raise SystemExit(0)
for name, expected_directory in (
    ("bin", True),
    ("python", True),
    ("tools", True),
    ("logs", True),
    ("runtime", True),
    ("cliproxy.yaml", False),
    ("cliproxy-management.key", False),
):
    path = root / name
    try:
        value = path.lstat()
    except FileNotFoundError:
        continue
    if stat.S_ISLNK(value.st_mode) or value.st_uid != os.getuid():
        raise SystemExit(f"managed runtime path is unsafe: {path}")
    if expected_directory != stat.S_ISDIR(value.st_mode):
        raise SystemExit(f"managed runtime path has unexpected type: {path}")
PY
}

orichum_uninstall_launcher_is_owned() {
  local launcher="$1"
  local expected="$2"
  local target target_absolute
  if [[ ! -e "$launcher" && ! -L "$launcher" ]]; then
    return 0
  fi
  [[ -L "$launcher" ]] || return 1
  target="$(readlink "$launcher")" || return 1
  case "$target" in
    /*) target_absolute="$target" ;;
    *) target_absolute="$(dirname "$launcher")/$target" ;;
  esac
  target_absolute="$(workflow_python - "$target_absolute" <<'PY'
import os
import sys
print(os.path.normpath(sys.argv[1]), end="")
PY
  )" || return 1
  if [[ "$target_absolute" == "$expected" ]]; then
    return 0
  fi
  [[ -f "$target_absolute" && ! -L "$target_absolute" && \
     "$(basename "$target_absolute")" == orichum && \
     "$(basename "$(dirname "$target_absolute")")" == bin ]] || return 1
  rg -Fq 'ORICHUM_WORKFLOW_ROOT' "$target_absolute" 2>/dev/null || \
    rg -Fq 'integrations.common.orichum_cli' "$target_absolute" 2>/dev/null
}

orichum_uninstall_service_identity() {
  local platform="$1"
  local kind="$2"
  case "$platform:$kind" in
    darwin:cliproxy)
      printf '%s\t%s\t%s\n' \
        "$HOME/Library/LaunchAgents/io.orichum.cliproxy.plist" \
        io.orichum.cliproxy -
      ;;
    darwin:route)
      printf '%s\t%s\t%s\n' \
        "$HOME/Library/LaunchAgents/io.orichum.route-proxy.plist" \
        io.orichum.route-proxy -
      ;;
    darwin:leanctx)
      printf '%s\t%s\t%s\n' \
        "$HOME/Library/LaunchAgents/io.orichum.leanctx-proxy.plist" \
        io.orichum.leanctx-proxy -
      ;;
    systemd:cliproxy)
      printf '%s\t%s\t%s\n' \
        "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/orichum-cliproxy.service" \
        - orichum-cliproxy.service
      ;;
    systemd:route)
      printf '%s\t%s\t%s\n' \
        "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/orichum-route-proxy.service" \
        - orichum-route-proxy.service
      ;;
    systemd:leanctx)
      printf '%s\t%s\t%s\n' \
        "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/orichum-leanctx-proxy.service" \
        - orichum-leanctx-proxy.service
      ;;
    *) return 1 ;;
  esac
}

orichum_uninstall_preflight_service() {
  local platform="$1"
  local kind="$2"
  local service_file="$3"
  local service_label="$4"
  local service_unit="$5"
  local data_root="$6"
  local workflow_root="$7"
  local target_state loaded_definition

  if [[ -e "$service_file" || -L "$service_file" ]]; then
    case "$kind" in
      cliproxy)
        cliproxy_service_is_owned "$service_file" "$data_root"
        ;;
      route)
        claudex_proxy_service_is_owned \
          "$service_file" "$data_root" "$workflow_root"
        ;;
      leanctx)
        leanctx_proxy_service_is_owned "$service_file" "$data_root"
        ;;
      *) return 1 ;;
    esac || {
      workflow_die "refusing unknown Orichum service: $service_file"
      return 1
    }
  fi

  target_state="$(managed_service_target_state \
    "$platform" "$service_label" "$service_unit")" || {
      workflow_die "Orichum service target could not be inspected safely"
      return 1
    }
  if [[ "$target_state" == loaded ]]; then
    [[ -f "$service_file" && ! -L "$service_file" ]] || {
      workflow_die "refusing loaded Orichum target without an owned definition"
      return 1
    }
    loaded_definition="$(managed_service_definition_path \
      "$platform" "$service_label" "$service_unit" 2>/dev/null)" || {
      workflow_die "loaded Orichum service definition could not be inspected"
      return 1
    }
    [[ "$loaded_definition" == "$service_file" ]] || {
      workflow_die "refusing loaded Orichum target with a foreign definition"
      return 1
    }
  fi
  printf '%s\n' "$target_state"
}

orichum_uninstall_remove_service() {
  local platform="$1"
  local service_file="$2"
  local service_label="$3"
  local service_unit="$4"
  local target_state="$5"
  case "$platform" in
    darwin)
      if [[ "$target_state" == loaded ]]; then
        launchctl bootout "gui/$(id -u)" "$service_file" >/dev/null
      fi
      ;;
    systemd)
      if [[ "$target_state" == loaded ]]; then
        systemctl --user stop "$service_unit" >/dev/null
      fi
      if [[ -e "$service_file" || -L "$service_file" || \
            "$target_state" == loaded ]]; then
        systemctl --user disable "$service_unit" >/dev/null
      fi
      ;;
    *) return 1 ;;
  esac
  rm -f -- "$service_file"
}

orichum_uninstall_validate_lifecycle_roots() {
  (($# == 3)) || return 2
  local data_root="$1"
  local config_root="$2"
  local lock_path="$3"
  local lifecycle_root="${lock_path%/install.lock}"
  [[ "$lock_path" == "$lifecycle_root/install.lock" ]] || {
    workflow_die "Orichum lifecycle lock path is invalid"
    return 1
  }
  for root in "$data_root" "$config_root"; do
    case "$lifecycle_root" in
      "$root"|"$root"/*)
        workflow_die \
          "refusing Orichum root that contains lifecycle state: $root"
        return 1
        ;;
    esac
  done
}

orichum_uninstall_remove_completion_file() {
  (($# == 1)) || return 2
  local path="$1"
  if [[ ! -e "$path" && ! -L "$path" ]]; then
    return 0
  fi
  if orichum_completion_file_is_owned "$path"; then
    rm -f -- "$path"
    return
  fi
  printf 'WARNING: retained drifted Orichum completion: %s\n' "$path" >&2
}

orichum_uninstall_remove_profile_block() {
  (($# == 2)) || return 2
  local profile="$1"
  local expected="$2"
  local status=0
  local helper_root
  helper_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  workflow_python -I -B - "$profile" "$expected" "$helper_root" <<'PY' || status=$?
import os
import stat
import sys
import tempfile
from pathlib import Path

profile = Path(sys.argv[1])
expected = Path(sys.argv[2]).read_bytes()
sys.path.insert(0, sys.argv[3])
from integrations.common.shell_profiles import ProfilePath, UnsafeProfile
try:
    target = ProfilePath(profile)
except UnsafeProfile as error:
    print(f"Completion profile unchanged: {error}", file=sys.stderr)
    raise SystemExit(10)
profile = target.path
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
    raise SystemExit(0)
begin_count = payload.count(begin)
end_count = payload.count(end)
if begin_count == end_count == 0:
    raise SystemExit(0)
if begin_count != 1 or end_count != 1:
    raise SystemExit(10)
start = payload.index(begin)
finish = payload.index(end, start)
finish = payload.find(b"\n", finish)
finish = len(payload) if finish < 0 else finish + 1
if payload[start:finish] != expected:
    raise SystemExit(10)
updated = payload[:start] + payload[finish:]
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
    os.chmod(temporary, stat.S_IMODE(observed.st_mode))
    # Claim the path atomically before replacement.
    if not target.unchanged():
        raise SystemExit(10)
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
    if not target.unchanged():
        retain_or_restore_claim()
        raise SystemExit(10)
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
      printf 'WARNING: retained drifted Orichum completion profile: %s\n' \
        "$profile" >&2
      return 0
      ;;
    *) return "$status" ;;
  esac
}

orichum_uninstall_completions() {
  (($# == 1)) || return 2
  local home_root="$1"
  local completion_root fish_path fish_record recorded_fish_path temporary
  local record_status profile
  completion_root="$(orichum_completion_root "$home_root")"
  fish_path="$(orichum_fish_completion_path)"
  fish_record="$(orichum_fish_completion_record_path "$home_root")"
  recorded_fish_path=
  record_status=0
  recorded_fish_path="$(
    orichum_recorded_fish_completion_path "$home_root"
  )" || record_status=$?
  case "$record_status" in
    0) ;;
    1) recorded_fish_path= ;;
    *)
      printf 'WARNING: retained unsafe Orichum fish completion record: %s\n' \
        "$fish_record" >&2
      recorded_fish_path=
      ;;
  esac
  temporary="$(mktemp -d "${TMPDIR:-/tmp}/orichum-uninstall.XXXXXX")" || \
    return 1
  orichum_profile_block zsh \
    "$completion_root/zsh" "$temporary/zsh.block"
  orichum_profile_block bash \
    "$completion_root/bash/orichum" "$temporary/bash.block"
  orichum_uninstall_remove_profile_block \
    "$HOME/.zshrc" "$temporary/zsh.block"
  orichum_uninstall_remove_profile_block \
    "$HOME/.bashrc" "$temporary/bash.block"
  for profile in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
    orichum_uninstall_remove_profile_block \
      "$profile" "$temporary/bash.block"
  done
  orichum_uninstall_remove_completion_file \
    "$completion_root/zsh/_orichum"
  orichum_uninstall_remove_completion_file \
    "$completion_root/bash/orichum"
  orichum_uninstall_remove_completion_file "$fish_path"
  if [[ -n "$recorded_fish_path" && "$recorded_fish_path" != "$fish_path" ]]; then
    orichum_uninstall_remove_completion_file "$recorded_fish_path"
  fi
  if [[ "$record_status" -le 1 ]]; then
    rm -f -- "$fish_record"
  fi
  rmdir "$completion_root/zsh" "$completion_root/bash" \
    "$completion_root" \
    >/dev/null 2>&1 || true
  rm -rf -- "$temporary"
}

orichum_uninstall() {
  local purge="${1:-false}"
  local workflow_root="${WORKFLOW_ROOT:?}"
  local user_bin_dir="${USER_BIN_DIR:-$HOME/.local/bin}"
  local home_root data_root config_root cache_root platform runtime_root
  local cliproxy_file cliproxy_label cliproxy_unit cliproxy_state
  local leanctx_file leanctx_label leanctx_unit leanctx_state
  local route_file route_label route_unit route_state
  local launcher="$user_bin_dir/orichum"
  local systemd_reload=false
  local -a runtime_paths

  data_root="$(validated_workflow_data_dir "$workflow_root")" || \
    workflow_die "refusing unsafe ORICHUM_DATA_HOME"
  home_root="$(
    orichum_uninstall_validate_private_root \
      "$(orichum_home_dir)" "$workflow_root" ORICHUM_HOME
  )" || return 1
  config_root="$(
    orichum_uninstall_validate_private_root \
      "$(workflow_config_dir)" \
      "$workflow_root" ORICHUM_CONFIG_HOME
  )" || return 1
  data_root="$(
    orichum_uninstall_validate_private_root \
      "$data_root" "$workflow_root" ORICHUM_DATA_HOME
  )" || return 1
  cache_root="$(
    orichum_uninstall_validate_private_root \
      "$(workflow_cache_dir)" "$workflow_root" ORICHUM_CACHE_HOME
  )" || return 1
  orichum_uninstall_validate_lifecycle_roots \
    "$data_root" "$config_root" "${WORKFLOW_LOCK_DIR:-}" || return 1
  case "$(uname -s)" in
    Darwin) platform=darwin ;;
    Linux) platform=systemd ;;
    *)
      workflow_die "supported platforms are macOS, Linux, and WSL2"
      return 1
      ;;
  esac

  IFS=$'\t' read -r cliproxy_file cliproxy_label cliproxy_unit \
    < <(orichum_uninstall_service_identity "$platform" cliproxy)
  IFS=$'\t' read -r route_file route_label route_unit \
    < <(orichum_uninstall_service_identity "$platform" route)
  IFS=$'\t' read -r leanctx_file leanctx_label leanctx_unit \
    < <(orichum_uninstall_service_identity "$platform" leanctx)
  runtime_root="$workflow_root"
  if [[ -L "$home_root/runtime/current" ]]; then
    runtime_root="$(
      workflow_physical_path "$home_root/runtime/current"
    )" || {
      workflow_die "installed Orichum runtime pointer is invalid"
      return 1
    }
    case "$runtime_root" in
      "$home_root/runtime/releases/"*) ;;
      *)
        workflow_die "installed Orichum runtime escapes Orichum home"
        return 1
        ;;
    esac
  fi

  cliproxy_state="$(orichum_uninstall_preflight_service \
    "$platform" cliproxy "$cliproxy_file" "$cliproxy_label" \
    "$cliproxy_unit" "$data_root" "$workflow_root")" || return 1
  route_state="$(orichum_uninstall_preflight_service \
    "$platform" route "$route_file" "$route_label" \
    "$route_unit" "$data_root" "$runtime_root")" || return 1
  leanctx_state="$(orichum_uninstall_preflight_service \
    "$platform" leanctx "$leanctx_file" "$leanctx_label" \
    "$leanctx_unit" "$data_root" "$runtime_root")" || return 1
  orichum_uninstall_preflight_runtime "$data_root" || {
    workflow_die "refusing unsafe Orichum runtime layout"
    return 1
  }
  if [[ "$home_root" != "$data_root" ]]; then
    orichum_uninstall_preflight_runtime "$home_root" || {
      workflow_die "refusing unsafe Orichum home runtime layout"
      return 1
    }
  fi
  orichum_uninstall_launcher_is_owned \
    "$launcher" "$runtime_root/bin/orichum" || {
      workflow_die "refusing unknown launcher: $launcher"
      return 1
    }

  orichum_uninstall_remove_service \
    "$platform" "$route_file" "$route_label" "$route_unit" "$route_state"
  orichum_uninstall_remove_service \
    "$platform" "$leanctx_file" "$leanctx_label" \
    "$leanctx_unit" "$leanctx_state"
  orichum_uninstall_remove_service \
    "$platform" "$cliproxy_file" "$cliproxy_label" \
    "$cliproxy_unit" "$cliproxy_state"
  if [[ "$platform" == systemd ]]; then
    systemd_reload=true
  fi
  if [[ "$systemd_reload" == true ]]; then
    systemctl --user daemon-reload >/dev/null
  fi
  rm -f -- "$launcher"
  orichum_uninstall_completions "$home_root"

  if [[ "$purge" == true ]]; then
    rm -rf -- "$home_root" "$data_root" "$config_root" "$cache_root"
    printf '%s\n' \
      'Purged Orichum.' \
      "  Removed home:   $home_root" \
      "  Removed data:   $data_root" \
      "  Removed config: $config_root" \
      "  Removed cache:  $cache_root" \
      "  Preserved checkout: $workflow_root" \
      '  Standalone third-party installations were not changed.'
    return
  fi

  runtime_paths=(
    "$data_root/bin"
    "$data_root/python"
    "$data_root/tools"
    "$data_root/logs"
    "$home_root/runtime"
    "$data_root/cliproxy.yaml"
    "$data_root/cliproxy-management.key"
    "$data_root/leanctx/proxy"
  )
  rm -rf -- "${runtime_paths[@]}"
  printf '%s\n' \
    'Uninstalled Orichum runtime.' \
    "  Removed launcher: $launcher" \
    "  Preserved data:   $data_root" \
    "  Preserved config: $config_root" \
    '  Accounts, sessions, project context, and LeanCTX data were preserved.' \
    '  Standalone third-party installations were not changed.' \
    "  Reinstall with: $workflow_root/install.sh"
}

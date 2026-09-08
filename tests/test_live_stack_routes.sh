#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${ORICHUM_LIVE_ACCEPTANCE:-0}" == 1 ]]; then
  PYTHONDONTWRITEBYTECODE=1 python3 -B - "$ROOT" <<'PY'
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

root = Path(sys.argv[1])
sys.path.insert(0, str(root))

from integrations.common.account_registry import AccountError, load_accounts
from integrations.common.stack_definition import (
    StackDefinitionError,
    normalize_model_stacks,
)


def home(override: str, xdg: str, fallback: str) -> Path:
    if override in os.environ:
        return Path(os.environ[override]).expanduser().resolve(strict=False)
    if xdg in os.environ:
        return (Path(os.environ[xdg]) / "orichum").resolve(strict=False)
    return (Path.home() / fallback).resolve(strict=False)


config_root = home(
    "ORICHUM_CONFIG_HOME", "XDG_CONFIG_HOME", ".config/orichum"
)
data_root = home(
    "ORICHUM_DATA_HOME", "XDG_DATA_HOME", ".local/share/orichum"
)
try:
    accounts = load_accounts(config_root / "accounts.json")
    stacks = normalize_model_stacks(
        json.loads(
            (config_root / "model-stacks.json").read_text(encoding="utf-8")
        )
    )
    selected = stacks.stacks[stacks.default_stack]
    ports = json.loads(
        (data_root / "service-ports.json").read_text(encoding="utf-8")
    )
except (
    AccountError,
    FileNotFoundError,
    UnicodeError,
    json.JSONDecodeError,
    StackDefinitionError,
):
    print("SKIP: installed accounts, stack selection, or service state is unavailable")
    raise SystemExit(0)
port = ports.get("routeProxyPort")
if type(port) is not int or not 1024 <= port <= 65535:
    print("SKIP: Orichum route proxy service port is unavailable")
    raise SystemExit(0)

try:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/v1/models", timeout=4
    ) as response:
        advertised = {
            item["id"]
            for item in json.load(response).get("data", ())
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
except (
    OSError,
    UnicodeError,
    urllib.error.URLError,
    json.JSONDecodeError,
):
    print("SKIP: live Orichum route proxy model catalogue is unavailable")
    raise SystemExit(0)

candidates = (
    *selected.controller,
    *(candidate for role in selected.agents.values() for candidate in role),
)


def route(provider: str):
    provider_accounts = sorted(
        (
            account
            for account in accounts
            if account.state == "active" and account.provider == provider
        ),
        key=lambda account: (-account.priority, account.name, account.id),
    )
    for account in provider_accounts:
        for candidate in candidates:
            definition = stacks.models[candidate.model]
            upstream = definition.routes.get(provider)
            routed = (
                None
                if upstream is None
                else f"{account.routing_prefix}/{upstream}"
            )
            if provider in candidate.providers and routed in advertised:
                return account, routed
    return None


anthropic = route("anthropic")
antigravity = route("antigravity")
missing = [
    name
    for name, value in (
        ("an active Anthropic account with a selected advertised model", anthropic),
        (
            "an active Antigravity account with a selected advertised model",
            antigravity,
        ),
    )
    if value is None
]
if missing:
    print("SKIP: missing " + " and ".join(missing))
    raise SystemExit(0)


def send(path: str, payload: dict[str, object], headers: dict[str, str]) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read(2 * 1024 * 1024 + 1)
            if not 200 <= response.status < 300:
                raise RuntimeError(f"{path} returned HTTP {response.status}")
    except urllib.error.HTTPError as error:
        error.read(4096)
        raise RuntimeError(f"{path} returned HTTP {error.code}") from error


send(
    "/v1/messages",
    {
        "model": anthropic[1],
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "Reply with one word."}],
        "tools": [
            {
                "name": "Bash",
                "description": "Run a shell command.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            *[
                {
                    "name": f"live_compatibility_tool_{index}",
                    "description": "A compatibility test tool.",
                    "input_schema": {
                        "type": "object",
                        "properties": {},
                    },
                }
                for index in range(11)
            ],
        ],
    },
    {"anthropic-version": "2023-06-01"},
)
send(
    "/v1/chat/completions",
    {
        "model": antigravity[1],
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "Reply with one word."}],
    },
    {},
)
print(
    "PASS: one bounded Anthropic and one bounded Antigravity request "
    "through the Orichum route proxy"
)
PY
  exit
fi

fixture="$(mktemp -d "${TMPDIR:-/tmp}/orichum-stack-routes.XXXXXX")"
server_pid=
cleanup() {
  if [[ -n "$server_pid" ]]; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  rm -rf -- "$fixture"
}
trap cleanup EXIT

config_root="$fixture/config"
data_root="$fixture/data"
project_root="$fixture/project"
nested_project="$project_root/nested/repository"
install -d -m 0700 \
  "$config_root" "$data_root" "$data_root/auth" "$data_root/bin" \
  "$data_root/state" \
  "$nested_project"
install -m 0755 /usr/bin/true "$data_root/bin/lean-ctx"
git -C "$project_root" init -q
for control_file in \
    model-stacks.json providers.json plugins.json runtime.json \
    controller-policy.md; do
  install -m 0600 "$ROOT/config/$control_file" "$config_root/$control_file"
done

python3 - \
  "$config_root/accounts.json" "$config_root/projects.json" \
  "$project_root" "$data_root/auth" <<'PY'
import json
import os
from pathlib import Path
import sys

accounts_path = Path(sys.argv[1])
projects_path = Path(sys.argv[2])
project = str(Path(sys.argv[3]).resolve())
auth_dir = Path(sys.argv[4])
accounts = []
for identifier, name, provider, prefix, priority in (
    (
        "oc-a-0000000000000001",
        "Primary OpenAI",
        "openai",
        "oc-r-0000000000000001",
        100,
    ),
    (
        "oc-a-0000000000000002",
        "Reserve OpenAI",
        "openai",
        "oc-r-0000000000000002",
        50,
    ),
    (
        "oc-a-0000000000000003",
        "Primary Anthropic",
        "anthropic",
        "oc-r-0000000000000003",
        100,
    ),
    (
        "oc-a-0000000000000004",
        "Reserve Anthropic",
        "anthropic",
        "oc-r-0000000000000004",
        50,
    ),
    (
        "oc-a-0000000000000005",
        "Primary Antigravity",
        "antigravity",
        "oc-r-0000000000000005",
        100,
    ),
):
    accounts.append(
        {
            "id": identifier,
            "name": name,
            "provider": provider,
            "credentialRef": f"{provider}-{identifier[-1]}.json",
            "pool": "shared",
            "routingPrefix": prefix,
            "priority": priority,
            "state": "active",
            "originalPrefix": None,
            "originalPriority": None,
        }
    )
    credential = auth_dir / f"{provider}-{identifier[-1]}.json"
    credential.write_text(
        json.dumps(
            {
                "type": {
                    "openai": "codex",
                    "anthropic": "claude",
                    "antigravity": "antigravity",
                }[provider],
                "account_id": identifier,
                "prefix": prefix,
                "priority": priority,
                "disabled": False,
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(credential, 0o600)
accounts_path.write_text(
    json.dumps({"schemaVersion": 2, "accounts": accounts}, indent=2) + "\n",
    encoding="utf-8",
)
projects_path.write_text(
    json.dumps(
        {
            "schemaVersion": 1,
            "contexts": [
                {
                    "root": project,
                    "atlassian": None,
                    "modelStack": None,
                    "accountPools": ["shared"],
                }
            ],
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
os.chmod(accounts_path, 0o600)
os.chmod(projects_path, 0o600)
PY

port_file="$fixture/cliproxy.port"
server_log="$fixture/cliproxy.log"
python3 - "$port_file" <<'PY' 2>"$server_log" &
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys

prefixes = (
    "oc-r-0000000000000001",
    "oc-r-0000000000000002",
    "oc-r-0000000000000003",
    "oc-r-0000000000000004",
    "oc-r-0000000000000005",
)
upstreams = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "claude-sonnet-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-6-thinking",
)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/v1/models":
            self.send_error(404)
            return
        payload = json.dumps(
            {
                "object": "list",
                "data": [
                    {"id": f"{prefix}/{upstream}", "object": "model"}
                    for prefix in prefixes
                    for upstream in upstreams
                    if (
                        prefix.endswith(("1", "2"))
                        and upstream.startswith("gpt-")
                    )
                    or (
                        prefix.endswith(("3", "4"))
                        and upstream.startswith("claude-")
                    )
                    or (
                        prefix.endswith("5")
                        and upstream == "claude-opus-4-6-thinking"
                    )
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_arguments):
        return


server = HTTPServer(("127.0.0.1", 0), Handler)
Path(sys.argv[1]).write_text(
    str(server.server_address[1]), encoding="ascii"
)
server.serve_forever()
PY
server_pid=$!
for _ in {1..300}; do
  [[ -s "$port_file" ]] && break
  if ! kill -0 "$server_pid" 2>/dev/null; then
    printf 'ERROR: fake CLIProxyAPI server exited during startup\n' >&2
    sed -n '1,120p' "$server_log" >&2
    exit 1
  fi
  sleep 0.1
done
if [[ ! -s "$port_file" ]]; then
  printf 'ERROR: fake CLIProxyAPI server did not become ready within 30 seconds\n' >&2
  sed -n '1,120p' "$server_log" >&2
  exit 1
fi
cliproxy_port="$(<"$port_file")"
for _ in {1..100}; do
  if python3 - "$cliproxy_port" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

with urllib.request.urlopen(
    f"http://127.0.0.1:{sys.argv[1]}/v1/models", timeout=0.2
) as response:
    if response.status != 200:
        raise SystemExit(1)
PY
  then
    break
  fi
  sleep 0.1
done
python3 - "$cliproxy_port" <<'PY' >/dev/null
import sys
import urllib.request

with urllib.request.urlopen(
    f"http://127.0.0.1:{sys.argv[1]}/v1/models", timeout=0.2
) as response:
    if response.status != 200:
        raise SystemExit(1)
PY
printf \
  '{"claudexProxyPort":13456,"cliproxyPort":%s,"leanctxProxyPort":13458,"routeProxyPort":13457}\n' \
  "$cliproxy_port" >"$data_root/service-ports.json"
chmod 0600 "$data_root/service-ports.json"

wizard_output="$fixture/wizard.output"
if ! (
  cd "$nested_project"
  ORICHUM_CONFIG_HOME="$config_root" \
  ORICHUM_DATA_HOME="$data_root" \
  TERM=dumb \
    python3 - "$ROOT" >"$wizard_output" <<'PY'
import os
import pty
import select
import sys
import time

root = sys.argv[1]
sys.path.insert(0, root)
from integrations.common.model_routing import ROLES

child, terminal = pty.fork()
if child == 0:
    code = """
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from integrations.common import orichum_cli, stack_wizard
ports = json.loads(
    (Path(os.environ["ORICHUM_DATA_HOME"]) / "service-ports.json").read_text(
        encoding="utf-8"
    )
)
orichum_cli._verify_runtime = lambda _paths: None
stack_wizard.load_management_endpoint = lambda _data: SimpleNamespace(
    port=ports["cliproxyPort"]
)
stack_wizard.attest_owned_connection = lambda _endpoint, _client_port: None
raise SystemExit(orichum_cli.main(["stack", "configure"]))
"""
    os.execve(
        sys.executable,
        [sys.executable, "-I", "-B", "-c", code, root],
        os.environ,
    )

review_choice = len(ROLES) + 1
os.write(terminal, f"2\n\nheavy\n\n{review_choice}\n\ny\ny\n".encode("ascii"))
output = bytearray()
deadline = time.monotonic() + 20
status = None
while time.monotonic() < deadline:
    readable, _, _ = select.select([terminal], [], [], 0.05)
    if readable:
        try:
            chunk = os.read(terminal, 65536)
        except OSError:
            chunk = b""
        if chunk:
            output.extend(chunk)
    waited, observed = os.waitpid(child, os.WNOHANG)
    if waited:
        status = observed
        break
if status is None:
    os.kill(child, 9)
    os.waitpid(child, 0)
    sys.stdout.buffer.write(output)
    raise SystemExit("stack wizard pseudo-terminal timed out")
sys.stdout.buffer.write(output)
if os.waitstatus_to_exitcode(status) != 0:
    raise SystemExit("stack wizard pseudo-terminal failed")
PY
); then
  sed -n '1,240p' "$wizard_output" >&2
  if ! kill -0 "$server_pid" 2>/dev/null; then
    printf 'ERROR: fake CLIProxyAPI server exited while the wizard was running\n' >&2
  fi
  sed -n '1,120p' "$server_log" >&2
  exit 1
fi
rg -Fq 'Saved stack heavy.' "$wizard_output"
jq -e '.schemaVersion == 2 and .stacks.heavy' \
  "$config_root/model-stacks.json" >/dev/null
jq -e '.contexts[0].modelStack == "heavy"' \
  "$config_root/projects.json" >/dev/null
if [[ -e "$config_root/stack-bindings.json" ]]; then
  jq -e \
    '.schemaVersion == 1 and .candidateAccounts == {}' \
    "$config_root/stack-bindings.json" >/dev/null
fi

(
  cd "$nested_project"
  ORICHUM_CONFIG_HOME="$config_root" \
  ORICHUM_DATA_HOME="$data_root" \
    python3 - "$ROOT" "$fixture/routes.json" "$fixture/session.id" <<'PY'
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
output = Path(sys.argv[2])
session_id = Path(sys.argv[3])
sys.path.insert(0, str(root))

from integrations.common import orichum_cli


class LaunchRecorded(RuntimeError):
    pass


def rendered(binding):
    return {
        "model": binding.primary.logical_model,
        "provider": binding.primary.provider,
        "account": binding.primary.account_id,
        "upstream": binding.primary.upstream_model,
        "fallbackProvider": binding.fallbacks[0].provider,
        "fallbackAccount": binding.fallbacks[0].account_id,
        "fallbackUpstream": binding.fallbacks[0].upstream_model,
    }


def record_launch(prepared, *_args, **_kwargs):
    logical = prepared.logical
    mcp = json.loads(prepared.physical.mcp_file.read_text(encoding="utf-8"))
    servers = mcp.get("mcpServers")
    if not isinstance(servers, dict) or "leanctx" not in servers:
        raise SystemExit("session did not materialize the private LeanCTX MCP")
    output.write_text(
        json.dumps(
            {
                "session": logical.id,
                "stack": logical.stack,
                "controller": rendered(logical.controller),
                "architecture-advisor": rendered(
                    logical.agents["architecture-advisor"]
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    session_id.write_text(logical.id + "\n", encoding="ascii")
    raise LaunchRecorded


orichum_cli._verify_runtime = lambda _paths: None
orichum_cli._launch_session = record_launch
try:
    orichum_cli.main(["run"])
except LaunchRecorded:
    pass
else:
    raise SystemExit("real session preparation did not reach final launch")
PY
)

session_id="$(<"$fixture/session.id")"
ORICHUM_CONFIG_HOME="$config_root" \
ORICHUM_DATA_HOME="$data_root" \
  python3 -I -B -c \
    'import sys; sys.path.insert(0, sys.argv.pop(1)); from integrations.common.orichum_cli import main; raise SystemExit(main(sys.argv[1:]))' \
    "$ROOT" session routes "$session_id" >"$fixture/session-routes.output"
rg -Fq 'controller' "$fixture/session-routes.output"
rg -Fq 'oc-a-0000000000000001' \
  "$fixture/session-routes.output"
rg -Fq 'oc-a-0000000000000002 (openai)' \
  "$fixture/session-routes.output"
rg -Fq 'architecture-advisor' "$fixture/session-routes.output"
rg -Fq 'oc-a-0000000000000003' "$fixture/session-routes.output"
rg -Fq 'oc-a-0000000000000004 (anthropic)' \
  "$fixture/session-routes.output"

jq -e '
  .stack == "heavy" and
  .controller.model == "gpt-5.6-sol" and
  .controller.provider == "openai" and
  .controller.account == "oc-a-0000000000000001" and
  .controller.upstream ==
    "oc-r-0000000000000001/gpt-5.6-sol" and
  .controller.fallbackProvider == "openai" and
  .controller.fallbackAccount == "oc-a-0000000000000002" and
  .controller.fallbackUpstream ==
    "oc-r-0000000000000002/gpt-5.6-sol" and
  ."architecture-advisor".model == "claude-opus-5" and
  ."architecture-advisor".provider == "anthropic" and
  ."architecture-advisor".account == "oc-a-0000000000000003" and
  ."architecture-advisor".upstream ==
    "oc-r-0000000000000003/claude-opus-5" and
  ."architecture-advisor".fallbackProvider == "anthropic" and
  ."architecture-advisor".fallbackAccount ==
    "oc-a-0000000000000004" and
  ."architecture-advisor".fallbackUpstream ==
    "oc-r-0000000000000004/claude-opus-5"
' "$fixture/routes.json" >/dev/null

printf 'PASS: interactive stack configuration records exact live routes\n'

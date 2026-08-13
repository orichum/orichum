"""Pure Orichum contract for the bounded LeanCTX MCP surface."""

from pathlib import Path


AUTO_APPROVED_TOOLS = (
    "ctx_read",
    "ctx_search",
    "ctx_tree",
    "ctx_expand",
    "ctx_graph",
    "ctx_impact",
    "ctx_callgraph",
    "ctx_knowledge",
    "ctx_overview",
)
TOOLS = (*AUTO_APPROVED_TOOLS, "ctx_patch", "ctx_shell")

_CONFIG = """compression_level = "lite"
minimal_overhead = true
tools_enabled = ["ctx_read", "ctx_search", "ctx_tree", "ctx_expand", "ctx_graph", "ctx_impact", "ctx_callgraph", "ctx_knowledge", "ctx_overview", "ctx_patch", "ctx_shell"]
disabled_tools = ["ctx_call", "ctx_compose", "ctx_session", "shell"]
auto_capture = true
buddy_enabled = false
enable_wakeup_ctx = true
journal_enabled = false
max_index_threads = 2
max_ram_percent = 12
no_degrade = true
prefer_native_editor = false
proxy_enabled = false
rules_injection = "off"
shadow_mode = false
shell_activation = "off"
shell_hook_disabled = true
update_check_disabled = true

[secret_detection]
enabled = false
redact = false

[embedding]
auto_download = true
model = "minilm"
"""


def config_bytes() -> bytes:
    """Return the exact private LeanCTX configuration for one session."""
    return _CONFIG.encode("utf-8")


def proxy_config_bytes(
    upstream_port: int,
    proxy_port: int = 13458,
) -> bytes:
    """Return Orichum's cache-safe shared LeanCTX proxy configuration."""
    for value in (upstream_port, proxy_port):
        if type(value) is not int or not 1024 <= value <= 65535:
            raise ValueError("LeanCTX proxy ports must be between 1024 and 65535")
    if upstream_port == proxy_port:
        raise ValueError("LeanCTX proxy ports must be distinct")
    return (
        'minimal_overhead = true\n'
        'proxy_enabled = true\n'
        f'proxy_port = {proxy_port}\n'
        'proxy_require_token = false\n'
        'update_check_disabled = true\n'
        '\n'
        '[secret_detection]\n'
        'enabled = false\n'
        'redact = false\n'
        '\n'
        '[proxy]\n'
        f'anthropic_upstream = "http://127.0.0.1:{upstream_port}"\n'
        'cache_align_relocate = false\n'
        'cache_breakpoint = false\n'
        'counterfactual_metering = false\n'
        'effort = "off"\n'
        'history_mode = "cache-aware"\n'
        'live_compress = true\n'
    ).encode("utf-8")


def mcp_server(
    binary: Path,
    project_root: Path,
    session_dir: Path,
    shared_data_home: Path,
) -> dict[str, object]:
    """Build one headless, project-jailed LeanCTX stdio server entry."""
    for path in (binary, project_root, session_dir, shared_data_home):
        if not path.is_absolute():
            raise ValueError("LeanCTX paths must be absolute")
    config = str(session_dir / "config")
    state = str(session_dir / "state")
    cache = str(shared_data_home / "cache")
    return {
        "command": str(binary),
        "args": [],
        "env": {
            "LEAN_CTX_ALLOW_REROOT": "false",
            "LEAN_CTX_AUTONOMY": "false",
            "LEAN_CTX_BYPASS_HINTS": "off",
            "LEAN_CTX_CACHE_DIR": cache,
            "LEAN_CTX_CONFIG_DIR": config,
            "LEAN_CTX_DATA_DIR": str(shared_data_home / "lean-ctx"),
            "LEAN_CTX_FULL_TOOLS": "0",
            "LEAN_CTX_HEADLESS": "1",
            "LEAN_CTX_MINIMAL": "1",
            "LEAN_CTX_PROJECT_ROOT": str(project_root),
            "LEAN_CTX_RULES_INJECTION": "off",
            "LEAN_CTX_SHELL_ALLOWLIST_OVERRIDE": "",
            "LEAN_CTX_STATE_DIR": state,
            "XDG_DATA_HOME": str(shared_data_home),
        },
    }

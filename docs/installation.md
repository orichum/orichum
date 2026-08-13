# Installation, reconciliation, and upgrades

## Supported hosts

The release-accepted host configurations are:

- macOS on Apple Silicon (native acceptance)
- Linux on x86-64 with systemd (native acceptance)
- WSL2 on x86-64 with systemd (contract acceptance)

The installer also recognizes macOS x86-64 and Linux arm64. Those paths use
the same guarded installer logic but have not completed native release
acceptance, so they are best-effort rather than release-gated targets.

The direct installer requires `bash`, `curl`, `gh`, `git`, `jq`, `python3`
3.10 or newer, `rg`, `tar`, `uv`, and Claude Code. Linux and WSL also require
`ss`, normally supplied by `iproute2`.

For a new installation, copy and run:

```bash
curl -fsSL https://raw.githubusercontent.com/orichum/orichum/main/bootstrap.sh | bash
```

The bootstrap resolves the latest published Orichum release, checks out that
release at `~/.local/share/orichum`, installs missing prerequisites including
Claude Code, Codex CLI, uv, and jq, then runs `install.sh`. It never installs
from the `main` branch. Automatic host-package installation supports Ubuntu and
macOS with Homebrew. It does not enable systemd for WSL; enable it and restart
WSL before installing. To inspect the bootstrap before running it:

```bash
curl -fsSLO https://raw.githubusercontent.com/orichum/orichum/main/bootstrap.sh
less bootstrap.sh
bash bootstrap.sh
```

To install manually on a supported host with the required commands already
available:

```bash
git clone https://github.com/orichum/orichum.git
cd orichum
./install.sh
```

The first run performs the complete installation. It:

1. builds and validates a small, content-addressed Orichum runtime release;
2. consolidates an earlier XDG-based installation into `~/.orichum` once;
3. validates the focused configuration and controller plugin;
4. installs the newest available CPython 3.14 patch privately;
5. installs or upgrades CLIProxyAPI, Claudex, LeanCTX, and `mcp-atlassian`;
6. provisions LeanCTX's official CPU ONNX Runtime in Orichum's shared private
   data directory;
7. probes required CLIProxyAPI behavior, the exact bounded MCP surfaces,
   LeanCTX dense semantic search, and LeanCTX's isolated proxy configuration;
8. installs or reconciles the shared loopback services;
9. installs native zsh, Bash, and fish completion and bounded shell activation;
10. preserves valid configuration and authentication;
11. runs `orichum doctor` once a provider route is available and records full
   technical diagnostics privately under `~/.orichum/logs/`.

ONNX Runtime provisioning is eager and idempotent, so a verified runtime is
available before the first project session and repeat installations reuse it.
The MiniLM embedding model remains lazy: LeanCTX downloads it automatically on
the first semantic search and reuses it from Orichum's shared LeanCTX cache.

Without a logged-in provider, installation completes in
`pending-provider-login` state instead of failing the full route check. It
prints one next command:

```bash
orichum setup
```

Setup asks for provider, account name, and projects folder (default
`~/projects`). The first account is automatically Primary in the internal
`shared` group. Setup invokes the active runtime's normal idempotent installer
path, creates a compatible recommended stack without a separate wizard, maps
the projects folder, creates its editable `.orichum/config.json`, and runs the
final doctor check. Re-running it resumes from durable state without replacing
an existing project configuration or duplicating completed phases.

See [Installation, setup, and configuration](setup-and-configuration.md) for
the complete prompt-by-prompt onboarding flow and every ongoing
`orichum configure` path.

Normal installer and setup output identifies each current operation, outcomes,
and required actions. Failures include the operation that stopped and its
concise reason. Use `./install.sh --verbose` or `orichum setup --verbose` to
stream the complete technical output that is also retained in private diagnostic
logs.

## Fast reconciliation or explicit upgrade

For normal maintenance, run:

```bash
./install.sh
# Fast reconciliation; no upstream checks when verified and healthy.
```

An unchanged healthy installation targets completion in about 10 seconds.
Orichum verifies its private install-state manifest, checks owned services and
critical runtime readiness, and reuses matching components. A missing or
damaged component is repaired without upgrading unrelated tools. Fresh
installations automatically use the complete path. Repairs can take longer
than the fast-path target.

The installer preserves an existing `~/.orichum/config/model-stacks.json`
because it may contain user-created stacks. Repository default-model changes
apply automatically only to fresh installations. Existing users can review and
adopt newer defaults without losing custom stacks through:

```bash
orichum stack available
orichum stack configure
```

Install and uninstall share one per-user lifecycle lock at
`~/.local/state/orichum/install.lock`. Even when `ORICHUM_DATA_HOME` is
relocated, two processes cannot concurrently replace the same launcher or user
services. The lock directory exists only while a lifecycle operation is active.

To deliberately refresh every managed runtime, run:

```bash
./install.sh --upgrade
# Resolve permitted releases, run complete probes, and run the full doctor.
```

Components that float resolve their current upstream release. Release-pinned
components reconcile to the version declared by Orichum; RC4 pins LeanCTX
3.9.12 so an upstream release cannot change the tested installer contract.
A normal fast repair preserves a verified recorded LeanCTX version. An explicit
upgrade refuses to downgrade a newer recorded LeanCTX because Orichum preserves
its durable indexes and project knowledge and cannot yet prove downgrade-safe
data compatibility.

Verified state is stored at
`~/.orichum/state/install-state.json`. The private manifest contains
component identities and digests, not secrets. Do not edit it; the installer
discards invalid state and safely reconciles the installation.

If a preferred port belongs to an existing Orichum service, the installer
reconciles and reuses it. It does not overwrite an unknown process. Interactive
installation offers another port; non-interactive installation selects the
next available port.

## Installed locations

| Purpose | Default |
|---|---|
| Command | `~/.local/bin/orichum` |
| Orichum home | `~/.orichum/` |
| Editable configuration | `~/.orichum/config/` |
| Immutable active runtime | `~/.orichum/runtime/current` |
| Content-addressed runtime release | `~/.orichum/runtime/releases/DIGEST/` |
| Managed binaries | `~/.orichum/bin/` |
| Provider credentials | `~/.orichum/auth/` |
| Legacy project Jira credentials | `~/.orichum/config/projects.json` |
| Named Jira profiles | `~/.orichum/config/jira-profiles.json` |
| Managed Python tools | `~/.orichum/tools/bin/` |
| Managed Python versions | `~/.orichum/python/` |
| Stable private Python | `~/.orichum/bin/orichum-python` |
| Logical sessions and install state | `~/.orichum/state/` |
| LeanCTX project knowledge | `~/.orichum/leanctx/` |
| zsh and Bash completion definitions | `~/.orichum/completions/` |
| fish completion definition | `${XDG_CONFIG_HOME:-~/.config}/fish/completions/orichum.fish` |
| Logs and cache | `~/.orichum/logs/`, `~/.orichum/cache/` |

Set `ORICHUM_HOME` to one absolute private directory before installation to
relocate the entire layout. `ORICHUM_CONFIG_HOME`, `ORICHUM_DATA_HOME`, and
`ORICHUM_CACHE_HOME` remain advanced, fine-grained overrides for automation and
tests; ordinary installations should use only `ORICHUM_HOME`.

The runtime release is an allowlisted payload, not a copy of the repository.
It contains the launcher, runtime Python modules, controller plugin, built-in
configuration, and installer helpers. It excludes `.git`, tests, docs, caches,
and unrelated checkout files. Activation switches `runtime/current`
atomically only after the release validates. Services and the launcher bind to
the physical release, so later checkout edits cannot change a running install.
After a successful reconciliation, obsolete releases are removed.

Run `orichum --version` to inspect provenance. A plain version identifies an
exact clean release tag. `+g.COMMIT` identifies a Git development build,
`.dirty` marks changes within the declared runtime payload, and `+src.DIGEST`
identifies a build made without Git metadata.

The only normal paths outside `ORICHUM_HOME` are:

- `~/.local/bin/orichum`, a launcher symlink;
- the operating system's LaunchAgent or systemd user-unit files;
- the fish completion definition under the user's XDG configuration directory;
- bounded completion blocks in `~/.zshrc`, `~/.bashrc`, and the effective Bash
  login profile (`~/.bash_profile`, `~/.bash_login`, or `~/.profile`);
- the per-user lifecycle lock while install or uninstall is running.

The lifecycle lock directory is removed when the operation ends.

## Existing installation migration

When the old XDG layout is present and the fine-grained path overrides are not
set, the installer moves its data, configuration, and cache into
`ORICHUM_HOME`. The move is atomic and requires the old and new locations to be
on the same filesystem. Temporary compatibility links keep existing services
alive during reconciliation. They are removed only after the new runtime,
services, configuration, and doctor checks succeed.

If installation fails or is interrupted, the transaction restores the old
runtime pointer and original directories. Existing live sessions should still
be restarted after a successful migration because their physical session
packages were created against the earlier installation contract.

Installer reconciliation removes retired Docker MCP profile and Atlassian
account-ID fields from project contexts. It does not guess or copy credentials:
each migrated context receives `atlassian: null`. Configure Jira once with
`orichum context jira ROOT` for every project that needs it, then start or
resume a session.

## Services

The shared resident services are CLIProxyAPI, the LeanCTX wire proxy, and the
Orichum route proxy. Each active physical session owns only its Claudex
translation proxy. A project-bound `mcp-atlassian` process is also
session-scoped and exists only when that project declares Jira credentials. All
inference requests use the shared services; model
catalogue discovery bypasses LeanCTX and queries CLIProxyAPI through the route
proxy.

On Linux and WSL:

```bash
journalctl --user -u orichum-cliproxy.service
journalctl --user -u orichum-leanctx-proxy.service
journalctl --user -u orichum-route-proxy.service
```

On every platform:

```bash
orichum doctor
orichum config paths
```

The installer never changes the system Python or another project's
environment. It manages only the profile content between these exact markers:

```text
# >>> Orichum completion >>>
# <<< Orichum completion <<<
```

The zsh block adds Orichum's definition directory to `fpath` and registers the
function only when completion is already initialized; it does not run
`compinit`. The Bash block sources the generated definition from both
interactive and login shells. Missing profiles are created privately.
Symlinked, foreign-owned, malformed, concurrently changed, or edited managed
blocks are retained unchanged, and the installer prints a manual activation
command instead. Completion definitions carry a body digest so upgrades and
uninstall can distinguish owned files from edited content. Orichum also records
the active fish destination under `~/.orichum/completions/` so reinstall and
uninstall can reconcile it after `XDG_CONFIG_HOME` changes.

Upgrade staging is transactional: an unsuccessful upgrade restores the prior
managed definitions, profile blocks, binaries, and service state. Orichum
installs LeanCTX directly from its verified release asset and does not run its
machine-wide `wrap`, `setup`, `onboard`, `init`, or `proxy enable` flows. It
starts only the owned proxy process described above.

## Uninstall

Run uninstall from the Orichum checkout:

```bash
./install.sh --uninstall
```

This stops and removes only verified Orichum-owned services, removes the
`orichum` launcher, removes unchanged completion definitions and exact managed
profile blocks, and deletes replaceable managed runtime files. Edited or
ambiguous completion/profile content is retained with a warning. It preserves:

- provider credentials and named accounts;
- project-bound Atlassian credentials;
- model and project configuration;
- Claude and Orichum session state;
- LeanCTX project knowledge and graphs.

That preserved state is reused if you run `./install.sh` again.

To also permanently delete Orichum's data and configuration roots:

```bash
./install.sh --uninstall --purge
```

Purge removes saved Orichum credentials, sessions, project configuration, and
Orichum-managed LeanCTX data. It does not delete the repository checkout.

Neither mode touches standalone Claude Code, Docker MCP Toolkit, or tool
installations outside `ORICHUM_HOME`. If a service definition or launcher with
an Orichum name is not verifiably owned by this setup, uninstall stops before
changing anything.

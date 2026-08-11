# Orichum installation, setup, and configuration

This guide covers the complete user journey from a machine without Orichum to
an installed, project-aware, multi-account and mixed-model configuration.

The three lifecycle commands have separate responsibilities:

| Stage | Command | Purpose |
|---|---|---|
| Install | `./install.sh` | Install or reconcile Orichum's local runtime and services |
| First-run setup | `orichum setup` | Create the first provider account, project mapping, usable model stack, and verified route |
| Ongoing configuration | `orichum configure` | Change accounts, backups, models, roles, project settings, or local readiness for an existing project |

Normal users should start with these guided commands. Low-level provider,
stack, context, and configuration-file operations remain available for
recovery, custom placement, and automation.

## Install Orichum

### Supported hosts

Release-accepted hosts are:

- macOS on Apple Silicon;
- Linux on x86-64 with a systemd user manager; and
- WSL2 on x86-64 with systemd enabled.

The installer recognizes macOS x86-64 and Linux arm64, but those paths have
not completed native release acceptance.

Required host commands are `bash`, `curl`, `gh`, `git`, `jq`, `python3` 3.10
or newer, `rg`, `tar`, `uv`, and Claude Code. Linux and WSL also require `ss`,
normally supplied by `iproute2`.

The host Python only bootstraps installation. The installed CLI and services
use Orichum's private CPython runtime.

### Run the installer

```bash
git clone https://github.com/orichum/orichum.git
cd orichum
./install.sh
```

The installer prepares the complete local runtime. It installs the `orichum`
launcher, private Python, CLIProxyAPI, Claudex, LeanCTX, the Atlassian MCP
adapter, native shell completion, and the owned loopback services. It also
provisions LeanCTX's CPU ONNX Runtime and validates dense semantic search.

Managed runtime and mutable state live below `~/.orichum`. The Git checkout
remains source code and the installer source for later upgrades or uninstall.

If no provider has been authenticated, installation still succeeds. The route
proxy remains intentionally inactive in `pending-provider-login` state and the
installer prints one required next command:

```bash
orichum setup
```

Normal output shows progress, results, and required actions. Complete technical
output is retained privately under `~/.orichum/logs/`. Stream it during the
operation only when needed:

```bash
./install.sh --verbose
```

### Reconcile or upgrade

A later plain installer run is an idempotent reconciliation:

```bash
./install.sh
```

It reuses verified components and repairs missing or damaged owned state
without intentionally refreshing every upstream tool.

Use an explicit upgrade when you want Orichum to resolve permitted releases,
refresh managed components, run their complete probes, and perform the full
doctor check once a provider route is available:

```bash
./install.sh --upgrade
```

Orichum preserves user-managed accounts, projects, model stacks, sessions, and
LeanCTX knowledge during reconciliation and upgrade.

### Uninstall or purge

Remove installed runtimes and services while preserving accounts, sessions,
project configuration, and LeanCTX project knowledge:

```bash
./install.sh --uninstall
```

Permanently remove the preserved Orichum configuration and private data as
well:

```bash
./install.sh --uninstall --purge
```

Purge is destructive. Use it only when the saved accounts, sessions, project
mappings, model configuration, and LeanCTX data are no longer needed.

## First-run setup

Run setup after installation:

```bash
orichum setup
```

You can supply an existing project root or parent directory directly:

```bash
orichum setup ~/projects
```

When the positional path is omitted, setup asks for a projects folder and
defaults to `~/projects`. The prompted default is created when missing. An
explicit positional path must already exist and must be a directory.

The projects folder is a parent context, not a restriction to one repository.
Repositories below it inherit the configured account availability and model
stack unless a more specific project context overrides them.

### Setup phases

Setup is resumable and performs only missing phases:

| Phase | What setup does |
|---|---|
| Authentication | Uses an existing active account or asks for a provider and completes its supported login flow |
| Account | Asks for a friendly account name and registers the first account as Primary in Orichum's internal shared availability group |
| Runtime | Reconciles owned services when the installed runtime is not ready |
| Projects | Creates or reuses the projects-folder context and associates available account groups |
| Models | Reuses a usable project stack or creates a compatible recommended stack from live models |
| Services | Runs the full doctor check and verifies that the selected project has usable live routes |

Credential filenames, internal account IDs, account groups, route prefixes,
and numeric priorities are deliberately hidden during normal setup.

### Choose a provider

Setup lists the providers declared by the installed release. The standard
configuration includes:

| Provider choice | Model families |
|---|---|
| Anthropic | Claude |
| Antigravity | Claude and Google |
| Kimi | Kimi |
| OpenAI | GPT |

Choose the provider that will supply the first controller route. Additional
providers and accounts can be added later with `orichum configure`.

### Authenticate locally or over SSH

Provider authentication always prints the URL that must be opened. On a local
desktop, Orichum may also ask the operating system to open it automatically.

In an SSH session:

1. Copy the displayed URL.
2. Open it in a browser on your own machine.
3. Complete the provider sign-in.
4. Copy the final callback URL from the browser.
5. Paste that complete callback URL into the `Callback URL` prompt.

Press Enter instead when the callback reached the waiting Orichum process
automatically.

Authentication creates a private credential inside Orichum. Setup then asks
for a friendly account name, such as `openai-personal` or `work-claude`. Tokens
and credential contents are never copied into the project repository.

If a compatible private credential already exists but has not been registered
as a named account, the guided flow can reuse it instead of repeating login.

### Configure the projects folder

Setup asks:

```text
Projects folder [~/projects]:
```

Choose a stable parent directory that contains, or will contain, the projects
that should inherit this default Orichum configuration. This is commonly
`~/projects`, `~/work`, or another team-specific parent.

Setup maps the folder once. Re-running setup reports it as already configured
instead of creating duplicate contexts.

### Create the recommended stack

Setup checks the live model catalogue exposed by the owned local gateway. If
the project does not already resolve to a usable stack, Orichum creates a
compatible recommended stack and assigns it to the project context.

This avoids a second model wizard during onboarding. Use `orichum configure`
after setup when you want one model everywhere, models by work type, or custom
models for each specialist role.

### Verify readiness

The final phase runs Orichum's doctor and verifies the complete route for the
selected projects folder. Successful setup ends with:

```text
Orichum is ready.
```

Start a session from a repository below the configured parent:

```bash
cd ~/projects/my-app
orichum
```

### Resume interrupted setup

Setup records durable progress. If authentication, service startup, stack
creation, or verification is interrupted, run the same command again:

```bash
orichum setup
```

Completed phases are shown as already configured and are not repeated.

On failure, normal output states the failed action, a bounded reason, the
command to retry, and the private diagnostic-log path. Stream the technical
details on the next attempt when necessary:

```bash
orichum setup --verbose
```

If setup still cannot complete, run:

```bash
orichum doctor
```

Do not manually delete authentication or configuration merely because setup
stopped. The resumable flow is designed to reuse valid completed work.

## Ongoing guided configuration

Run configuration from the project you want to change:

```bash
cd ~/projects/my-app
orichum configure
```

Or target another configured project explicitly:

```bash
orichum configure --project ~/projects/another-app
```

The command requires an interactive terminal and a directory that resolves to
an existing Orichum project context. `--verbose` streams technical diagnostics
during reconciliation while retaining the private log:

```bash
orichum configure --verbose
```

## The configuration dashboard

Configuration opens with one compact view of the effective project state:

```text
Orichum configuration
  Project     /home/me/projects/my-app
  Profile     balanced
  Controller  gpt-5.6-sol
  Accounts    2 available
  Changes     None

What do you want to change?
1. Models             balanced · gpt-5.6-sol
2. Accounts           2 available
3. Check configuration  verify routes and repair services
4. Advanced settings  GitHub, Jira, account maintenance, custom stacks
5. Exit
```

The dashboard is shown again after each action. If a draft changes, **Changes**
becomes **Ready to review** and **Check configuration** becomes **Review and
apply changes**. Nothing is written before that review.

## Models

If the nearest project path contains `.orichum/models.json`, the dashboard
shows **Profile: Project file**. The Models action displays the authoritative
absolute path instead of offering conflicting wizard edits. Open that JSON file
in an editor to change the controller and five specialist logical-model IDs.
Accounts, repair, and readiness remain guided.

Without a project file, the **Models** menu uses progressive disclosure:

```text
Recommended setup      Orichum chooses compatible models
One model everywhere   the simplest custom setup
Models by work type     separate research, review, and implementation
Customize each role    full control over every specialist
Switch profile          use another saved model stack
Back
```

Every model comes from the gateway's live numbered list. Large lists are
searchable, the current model is marked, and users never need to type model
IDs.

### Recommended setup

Orichum chooses compatible live models for the controller and every specialist
role using the shipped recommendation policy. This is the shortest path for
users who do not want to tune individual roles.

### One model everywhere

Choose one live model once. It is assigned to the controller and every
specialist role.

### Models by work type

Choose one live model for each work category:

| Work type | Roles affected |
|---|---|
| Controller | Controller |
| Research | Repository explorer and repository verifier |
| Review | Correctness critic |
| Architecture | Architecture advisor |
| Implementation | Implementation worker |

### Customize each role

Assign models individually to the controller, repository explorer, repository
verifier, correctness critic, architecture advisor, and implementation worker.
This path preserves full mixed-provider and mixed-model control without putting
that complexity in the default flow.

### Switch profile

Choose another saved model stack that has compatible live routes for the
project. Unusable profiles are hidden. If the current profile is no longer
live, Orichum explains the state and lists the usable alternatives.

Model availability is checked again immediately before a changed draft is
saved. Orichum identifies affected roles rather than silently substituting a
model when the live catalogue changes.

## Accounts

The **Accounts** menu contains only actions that work in the guided flow:

```text
Add an account             connect another provider or credential
Add a backup account       automatic fallback for a current account
Manage existing accounts   rename, priority, enable, disable, or remove
Back
```

### Add an account

Use **Add an account** for another independent account or provider.

1. Choose a provider from the installed provider configuration.
2. Complete or reuse its SSH-safe authentication.
3. Enter a friendly account name.
4. Choose **Current project**, **All shared projects**, or advanced placement.
5. Choose whether the account is preferred, equal-choice, or a backup.

The wizard derives internal account groups and priorities from these
plain-language choices. Authentication is saved securely as soon as login
succeeds and remains reusable if the configuration draft is later discarded.

### Add a backup account

Use **Add a backup account** when a project account needs an explicit compatible
fallback.

1. Choose an active primary account already used by the project.
2. Authenticate or reuse another credential for the same provider.
3. Enter a friendly backup name.
4. Let Orichum reuse the primary availability group and derive a lower
   preference.

If a model candidate is locked to the primary account, the wizard recommends
allowing both named accounts so automatic fallback can work. A fallback remains
within the same logical model and family and is frozen into each new logical
session. Existing sessions are not rebound.

### Manage existing accounts

The guided menu provides a direct, honest handoff instead of displaying actions
that are not implemented there:

```bash
orichum provider account --help
```

The focused command supports rename, priority, enable, disable, remove, and
credential synchronization operations.

## Review, apply, and exit

When changes are pending, **Review and apply changes** shows:

- the target project;
- selected or pending primary and backup accounts;
- the concrete model for every controller and specialist role; and
- the reminder that existing sessions remain unchanged.

The final choices are:

```text
Apply changes
Keep editing
Discard and exit
```

**Apply changes** refreshes the live catalogue, validates the draft, writes the
configuration transactionally, reconciles the local runtime, and verifies the
project. **Keep editing** returns to the dashboard with the draft intact.
**Discard and exit** leaves durable configuration unchanged; completed private
authentication remains reusable.

Selecting **Exit** from the dashboard while a draft is pending cannot silently
lose work. Orichum asks whether to review and apply, discard, or keep editing.

When several new accounts are part of one confirmed draft, application is
compensating: if a later write fails, Orichum removes accounts created earlier
by that failed application attempt.

## Check and repair

With no pending changes, **Check configuration** verifies the complete project
route. A healthy project reports that no changes are needed. If owned local
services are not ready, Orichum offers:

```text
Repair local services
Back
```

Repair uses the normal idempotent reconciliation path and verifies the project
again before reporting readiness.

## Advanced settings

**Advanced settings** keeps expert capabilities one level away from the simple
flow and shows exact command entry points:

| Area | Command |
|---|---|
| Manage accounts | `orichum provider account --help` |
| Provider routes | `orichum provider --help` |
| Custom model stacks | `orichum stack --help` |
| Project and GitHub | `orichum context update --help` |
| Jira | `orichum context jira --help` |

Use these commands for automation, custom account-group placement, direct
account maintenance, ordered stack candidates, named-account locks, GitHub
identity, Jira, and other focused project-context operations.

## What configuration changes affect

Guided configuration updates durable local control-plane state and then
reconciles the owned runtime. The final review always states:

```text
Changes apply to new sessions. Existing sessions are unchanged.
```

An existing logical session keeps its frozen controller route, named account,
and at most one compatible fallback. Resume it when you want the same binding.
Start a new session to use changed account selection or model assignments. Use
an explicit fork when moving work to another stack or model family with a
bounded handoff.

Accounts, authentication, sessions, provider routing, project contexts, and
other control-plane data are machine-local private state. They must not be
committed or copied into a repository. The deliberate exception is
`.orichum/models.json`, which may be committed because it contains only logical
model assignments and no credentials or account policy. Inspect private
locations with:

```bash
orichum config paths
```

## Common workflows

### First installation and setup

```bash
git clone https://github.com/orichum/orichum.git
cd orichum
./install.sh
orichum setup
cd ~/projects/my-app
orichum
```

During setup, choose the provider, authenticate, name the account, and accept
or change the projects folder. The account placement, recommended model stack,
service reconciliation, and readiness check are automatic.

### Same-provider backup

```bash
cd ~/projects/my-app
orichum configure
```

Choose:

1. **Accounts**
2. **Add a backup account**
3. The existing primary account
4. The recommended automatic-backup policy when an account lock is present
5. **Review and apply changes**
6. **Apply changes**

Start a new session to receive the new frozen primary and compatible fallback.

### Mixed controller and specialist models

```bash
cd ~/projects/my-app
orichum configure
```

Choose:

1. **Models**
2. **Models by work type** for a compact configuration, or **Customize each
   role** for full control
3. Models from the numbered live lists
4. **Review and apply changes**
5. **Apply changes**

The reviewed role table is the exact configuration that new sessions will use.

## Inspect and validate

Use these commands after setup or configuration:

```bash
orichum --version
orichum doctor
orichum config paths
orichum config show
orichum config validate
orichum context list
orichum context validate
orichum provider accounts
orichum stack list
orichum stack show STACK
orichum models resolve
orichum models validate
```

Inspect the command surface and native completion at any level:

```bash
orichum --help
orichum setup --help
orichum configure --help
orichum provider --help
orichum provider account --help
orichum stack --help
orichum context --help
```

## Recovery and troubleshooting

### Setup stopped

Run `orichum setup` again. Setup skips completed phases and prints the private
diagnostic path when a phase fails. Add `--verbose` only when live technical
output is needed.

### Configuration was cancelled after authentication

Run `orichum configure` again. The private unregistered credential can be
reused; cancellation does not silently register an account or apply the draft.

### Project is configured but not ready

Open **Check configuration** and choose **Repair local services**, or run:

```bash
./install.sh
orichum doctor
```

### Account or model is missing from the wizard

The wizard shows only accounts and model routes that are active, visible to the
project, and currently advertised by the owned gateway. Inspect:

```bash
orichum provider accounts
orichum stack available
orichum stack show STACK
orichum models resolve STACK
orichum doctor
```

### A model changed while reviewing

Apply rechecks live availability. Choose a replacement only for the roles that
Orichum identifies as invalid, then review and apply again.

### A new session did not use the changed configuration

Confirm the launch directory resolves to the intended project context and that
you started a new logical session:

```bash
orichum context list
orichum stack show STACK
orichum sessions
```

Resumed sessions intentionally keep their original immutable route.

For deeper diagnostics, see [Troubleshooting](troubleshooting.md),
[Providers and accounts](providers-and-accounts.md),
[Multi-account routing](multi-account-usage.md), [Model stacks](model-stacks.md),
and [Project contexts](project-contexts.md).

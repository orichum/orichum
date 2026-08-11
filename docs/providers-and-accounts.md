# Providers and accounts

Providers describe how a model family reaches CLIProxyAPI. Named accounts bind
a friendly name, provider, credential reference, account pool, and priority.
Secrets remain in CLIProxyAPI's private authentication directory.

## Add an account

For initial machine and project onboarding, use the complete setup wizard:

```bash
orichum setup
```

It combines authentication, account registration, runtime reconciliation,
automatic compatible-stack creation, projects-folder mapping, and final
verification. First-run setup asks only for provider, account name, and projects
folder. It assigns the first account Primary priority in the internal `shared`
pool without exposing pool or priority choices.

To add another account or configure an explicit same-provider backup, run the
guided project configuration:

```bash
cd ~/projects/my-app
orichum configure
```

Choose **Accounts**. Orichum lists configured providers and named
accounts, runs the SSH-safe login flow, asks for a friendly name, derives the
project placement and account preference, and previews the complete change.
The backup flow fixes the provider to the selected primary account and derives
a lower preference automatically. Credential filenames, pools, numeric
priorities, route prefixes, and internal IDs are not displayed.

Authentication and account registration are separate internally. CLIProxyAPI
authentication creates a private OAuth credential; Orichum account
registration gives that credential a display name, provider adapter, pool, and
routing priority. The normal wizards perform both steps together and never ask
for a credential filename. If a previous low-level login already succeeded,
`orichum configure` offers to reuse that unregistered credential instead of
logging in again. Cancelling before the preview is applied leaves it available
for the next run without registering an account.

## Advanced and recovery commands

`orichum provider configure` and the separate low-level commands remain
available for recovery, custom account-group placement, and automation:

```bash
orichum provider login codex
orichum provider login claude
orichum provider login antigravity
orichum provider login kimi
orichum config paths
ls ~/.orichum/auth
```

Register the credential by filename, not by copying its contents:

```bash
orichum provider account add \
  "Personal GPT" openai CREDENTIAL_FILE shared --priority primary
```

`CREDENTIAL_FILE` means the filename created by CLIProxyAPI inside Orichum's
auth directory. `shared` is the account pool in which the account is available.
Normal interactive setup does not require this manual path. Login types and
provider adapters are intentionally different identifiers in some cases:
Codex authentication uses login type `codex`, while its Orichum provider
adapter is `openai`.

## Manage accounts

```bash
orichum provider accounts
orichum provider account rename ACCOUNT_ID "Work Claude"
orichum provider account priority ACCOUNT_ID secondary
orichum provider account disable ACCOUNT_ID
orichum provider account enable ACCOUNT_ID
orichum provider account remove ACCOUNT_ID
orichum provider account sync
```

Priority aliases are `primary` (100), `secondary` (50), and `reserve` (10).
Integers from 0 through 1000 are also accepted.

Automatic stack candidates select the highest-priority eligible account in the
first matching pool. Equal-priority accounts are rotated deterministically for
new sessions. A candidate locked to a named account never rolls over.

Display names appear in explicit account and route inspection output.
Credential filenames, route prefixes, tokens, and secrets are not printed.

For two accounts of the same provider and mixed Anthropic/Antigravity examples,
see [Multi-account routing](multi-account-usage.md).

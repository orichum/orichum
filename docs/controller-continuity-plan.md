# Controller continuity

## Behavior

Resuming an Orichum session adopts the controller selected by the current
project or machine configuration, including a change of model family. It keeps
the logical session ID, Claude conversation UUID, project, parent, creation
time, LeanCTX profile, and agent bindings. No fork or handoff is required.

## Implementation

1. Resolve the current controller using the existing project, stack, account,
   and live-catalogue rules, without resolving replacement agent models.
2. Validate the new controller and existing agent routes and materialize the
   physical run before changing the saved session.
3. Publish the controller binding atomically under a session lock, checking
   that another launch has not changed the binding in the meantime.
4. Launch with the existing conversation UUID and the new controller model.
   The route proxy and session inspection read the updated binding.

## Verification

Cover same-family and cross-family replacement, both session ID forms,
preservation of conversation and agent state, subsequent reloads, failed
validation/publication, concurrent updates, and unchanged-controller resumes.
Run the session, CLI, physical-session, project-model, and route-proxy suites.

Active-session `/model` changes are a separate integration point: this change
does not watch configuration files or restart a running controller.

## Result

Implemented in the session resolver and CLI resume preparation. Existing,
eligible controller primaries are retained; compatible backups are refreshed.
The saved controller is updated under a directory lock with atomic replacement
and an idempotent comparison against the expected session. Configuration
review messages and session documentation describe the new behavior.

The full Python suite, smoke checks, and bootstrap checks passed. Focused
session tests cover the final concurrency and route-refresh behavior. Live
provider transcript replay and installation are not part of these tests.

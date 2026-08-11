# Routing and failover

## Route selection

At session creation, Orichum combines:

1. the longest matching project context;
2. its selected model stack;
3. account pools visible to that project;
4. live provider and model routes;
5. account health, priority, and optional named-account locks.

The resulting logical session stores a primary route and no more than one
same-model, same-family account fallback.

```mermaid
flowchart TD
    D["Launch directory"] --> C["Project context"]
    C --> S["Model stack"]
    S --> A["Eligible account route"]
    A --> B["Immutable session binding"]
    B --> P["Primary request"]
    P -->|"success"| O["Stream response"]
    P -->|"retryable failure before output"| F["One frozen fallback"]
    F --> O
    P -->|"output or tools may have started"| E["Surface the failure"]
```

Before committing a successful response to the client, the route proxy buffers
one bounded prelude. For SSE responses it waits for the first complete `data:`
event; for bounded non-streaming responses it validates the advertised body.
A transport failure during that prelude can use the frozen fallback because no
model output or tool request has reached the client. Once any response event is
forwarded, Orichum never replays the request.

Every proxied request receives an opaque `X-Orichum-Request-ID`. The same ID is
sent upstream, returned to the client, included in private route telemetry, and
written with redacted lifecycle events to the route-proxy service log.

## Recovery limits

- Recovery never selects an account that was not frozen into the session.
- Only one retry is allowed.
- The retry must keep the same logical model and family.
- No replay occurs after response bytes or tool execution may have started.
- Authentication or quota failure can use only the preselected fallback.
- Cooldowns stop repeated pressure on a failing primary.
- Invalid configuration fails closed.

Provider changes and family changes are explicit. Use `orichum fork` with a
target stack and bounded handoff; do not expect a running Claude controller to
change protocol transparently.

Inspect a frozen route with:

```bash
orichum session routes SESSION_ID
```

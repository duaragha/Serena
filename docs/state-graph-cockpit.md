# Serena state graph and world cockpit

`core.state_graph.StateGraphStore` is the local authority for people, devices,
displays, rooms, apps, services, projects, capabilities, permissions,
locations, and normalized world items. Its default database is
`~/.local/state/serena/state-graph.sqlite3`; tests and isolated runtimes pass an
explicit path. `SERENA_STATE_GRAPH_DB_PATH` may override the default.

Schema migrations are recorded in `graph_migrations`. Entity and edge tables
are current projections. `graph_events` is the ordered immutable event stream;
an explicit `event_id` makes an update idempotent. Freshness is an observation
timestamp plus optional TTL. A record without a TTL is retained with unknown
freshness rather than silently treated as live.

`register_current_system()` registers Raghav, the current laptop, its default
browser, displays, web capability, ownership, location, and connection edges.
Desktop probes are bounded to two seconds. Missing probes create explicit
unavailable browser/display records so future devices can be registered with
the same API without lying about current hardware. Discovery entities and
their laptop relationships share bounded TTLs; freshness-aware neighbor
queries therefore stop projecting browsers and displays after they disappear.

`core.world_cockpit.WorldCockpit` consumes adapters for events, weather,
household state, and news. Providers return source, observation time,
freshness, confidence, and records. Refreshes normalize time to UTC, normalize
locations, deduplicate across sources, preserve evidence, rank relevance, and
store both provider cache state and world items in the graph. A failed provider
is `stale` when durable cached evidence exists and `unavailable` otherwise.
Relevance is evaluated against the explicit refresh clock, so fixture replay is
deterministic. Provider and record URLs are stripped of query strings,
fragments, and embedded credentials before durable storage or evidence-card
generation.

The cockpit snapshot contains evidence cards, provider states, a MapLibre style
version 8 document with a GeoJSON source, and bounded `voice_handoff` prose.
`JsonURLAdapter` uses the standard library, rejects credentials in URLs, limits
responses to one megabyte, and passes a bounded timeout. Production endpoints
must be explicitly configured free or local sources. Tests use
`FixtureAdapter` or an injected opener and make no network calls.

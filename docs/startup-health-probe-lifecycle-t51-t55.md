# Startup health-probe lifecycle (T51–T55)

This batch manages the existing non-blocking startup health probe as an
application-lifespan task. It does not change readiness semantics or the
health-check adapter contract.

## T51 — Retain the task handle

The startup health probe is stored as
`app.state.startup_health_probe_task`. Operators and tests can inspect its
lifecycle without introducing a global task registry.

## T52 — Cancel before resource shutdown

On application shutdown, an unfinished startup probe is cancelled and awaited
before the registry is closed. This prevents a background probe from continuing
to access adapters or registry resources after lifespan teardown.

## T53 — Consume task completion safely

Shutdown awaits the task with `asyncio.gather(..., return_exceptions=True)`.
Unexpected task exceptions are logged by type only; cancellation and teardown do
not leak exception details or leave an unobserved task exception.

## T54 — Preserve non-blocking startup

The probe remains scheduled with `asyncio.create_task`; startup does not wait
for slow adapters. A regression test blocks one adapter's health coroutine,
confirms the service still responds to `/health`, then confirms shutdown
cancels and awaits the probe.

## T55 — Lifecycle verification and safety

Run the focused lifecycle test, full pytest, compileall, PostgreSQL integration,
wheel packaging, Docker, and cross-platform CI. This is local application
lifespan behavior only; it does not connect real WorkBuddy Desktop or external
production services.

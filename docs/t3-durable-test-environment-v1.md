# T3 — Durable SQLite test environment

This stage creates an isolated, local Docker test stack. It does **not** deploy
to a public host, configure Cloudflare, create production secrets, or activate
the GitHub Issue relay.

## Safety properties

- `A2A_STORE=sqlite` is forced by Compose; Pull tasks are not run against memory storage.
- Hub task data and ChatGPT ingress idempotency data live in project-scoped named Docker volumes.
  Use a unique Compose project name to keep this test stack isolated from other stacks.
- The HTTP port is bound to `127.0.0.1` only (default `18080`), not exposed to
  the LAN or public Internet.
- API authentication is required. Set a unique random `A2A_API_TOKEN` in the
  untracked `deploy/test/.env.test` file.
- WorkBuddy Connector, ChatGPT ingress, and GitHub relay credentials are blank
  by default. Configure them only in a later, separately approved integration
  test; never reuse one token for another integration.
- Agent configuration is mounted read-only. Autonomy is disabled.

## Prepare and start

From the repository root:

1. Copy `deploy/test/.env.test.example` to `deploy/test/.env.test`.
2. Replace `A2A_API_TOKEN` with a unique random secret of at least 32 characters.
   Do not commit or paste it into Issues, logs, or chat.
3. Start the stack:

   `docker compose -p a2a-hub-t3-test --env-file deploy/test/.env.test -f deploy/test/compose.yaml up -d --build`

4. Check health:

   `curl http://127.0.0.1:18080/health`

5. Check an authenticated endpoint (replace the placeholder locally):

   `curl -H "Authorization: Bearer <A2A_API_TOKEN>" http://127.0.0.1:18080/agents`

The test stack uses project-scoped named volumes. Use the same project name on every Compose command.
`docker compose -p a2a-hub-t3-test down` preserves volumes; `docker compose -p a2a-hub-t3-test down -v` deletes them and must not be used when retaining test data.

## SQLite backup and verification

The maintenance script uses Python's SQLite online backup API. It is safe
for a live database and includes committed WAL transactions; it does not copy
only the main .db file while WAL data may still exist.

Create a consistent backup inside the test container:

`docker compose -p a2a-hub-t3-test --env-file deploy/test/.env.test -f deploy/test/compose.yaml exec hub-test python scripts/sqlite_maintenance.py backup --database /app/data/a2a_hub.db --output /app/backups/a2a_hub-YYYYMMDD-HHMMSS.db`

Verify it inside the container:

`docker compose -p a2a-hub-t3-test --env-file deploy/test/.env.test -f deploy/test/compose.yaml exec hub-test python scripts/sqlite_maintenance.py verify --database /app/backups/a2a_hub-YYYYMMDD-HHMMSS.db`

Export a copy to the host when needed:

`docker cp $(docker compose -p a2a-hub-t3-test --env-file deploy/test/.env.test -f deploy/test/compose.yaml ps -q hub-test):/app/backups/a2a_hub-YYYYMMDD-HHMMSS.db ./backups/`

Restore procedure (offline only):

1. Stop the Hub: `docker compose -p a2a-hub-t3-test --env-file deploy/test/.env.test -f deploy/test/compose.yaml stop hub-test`.
2. Export a separate backup of the current database before replacing it.
3. Restore with `--overwrite` only after confirming the source backup and destination. For the live task database, run the maintenance command in a one-off container while the service is stopped.
4. Start the Hub again and check `/health`, task listing, and Pull connector behavior.

`restore` writes a verified temporary database and atomically replaces the
destination. Never restore over a database while the Hub process is running.
## T3 acceptance checklist

- [ ] Compose config resolves and publishes only on loopback.
- [ ] Container starts with SQLite and required API authentication.
- [ ] `/health` reports healthy.
- [ ] Create a test Pull task, restart the container, and confirm the task remains.
- [ ] Idempotent replay after restart returns the original task, not a duplicate.
- [ ] Backup contains committed WAL data and passes `PRAGMA integrity_check`.
- [ ] Restore is tested against a disposable database with the Hub stopped.
- [ ] WorkBuddy independently reviews the exact commit and reports command outputs.
- [ ] No production endpoint, Cloudflare, real secrets, public port, or merge is used.

T3 code-level acceptance does not prove the real WorkBuddy runtime or public
GitHub-to-Hub relay is operational. That requires a later explicitly approved
integration stage.


## T4 — GitHub Issue result callback

T4 adds a result callback for tasks accepted through the signed GitHub Issue relay.
The relay includes the originating repository, Issue number, and source comment ID
inside the exact HMAC-signed ingress body. The Hub accepts this callback context only
when the relay signature is valid and the repository is present in
`A2A_GITHUB_RELAY_ALLOWED_REPOS`.

When WorkBuddy posts a terminal result, the Hub first persists the result in the task
store, then posts a bounded, escaped result comment to the original Issue. Configure
`A2A_GITHUB_RELAY_API_TOKEN` with a fine-grained GitHub token restricted to the
single repository and Issues: write permission, and set
`A2A_GITHUB_RELAY_ALLOWED_REPOS=owner/repo`. Do not reuse the ingress, relay HMAC,
or WorkBuddy connector token. The token is stored only in the Hub environment and
must never be committed.

Delivery retries use the same WorkBuddy `result_key`. The Hub searches existing
Issue comments for a deterministic hidden task marker before creating a new comment,
so a retry after an ambiguous network failure does not normally duplicate the result.
A failed callback returns HTTP 502 after the task result is durably committed; WorkBuddy
must retry the exact same result payload and key. This is not an autonomous durable
outbox: if WorkBuddy never retries after a callback failure, operator intervention
or a later retry is required. True background retry/reconciliation is a later hardening
step.

Agent output is treated as untrusted text: HTML is escaped, @-mentions are neutralized,
and output is capped at 8,000 characters. The callback is disabled unless both the
repository allowlist and a dedicated GitHub API token are configured. No public
webhook or inbound GitHub connection is opened by the Hub.


## T5 — End-to-end callback round-trip acceptance

T5 adds an integration test that exercises the application routes as one workflow,
using a disposable SQLite task database and a local in-memory GitHub API stub:

1. Submit an Issue-originated Pull task with callback context covered by the relay HMAC.
2. Verify the accepted task and the WorkBuddy claim payload do not expose the
   internal GitHub callback metadata.
3. Claim and start the task through the WorkBuddy Connector.
4. Submit a terminal result. The stub simulates GitHub accepting the comment while
   the HTTP response is lost; the Hub must return retryable HTTP 502 while keeping
   the terminal task result durable.
5. Retry the exact same result body and `result_key`. The Hub must find the
   deterministic marker, return an idempotent replay, and avoid a second POST.

Run the focused test with:

`python -m pytest -q tests/test_github_callback_roundtrip.py`

This is an application-level integration test with a local GitHub API stub. It
does not contact GitHub, use real credentials, prove the GitHub Actions webhook
or WorkBuddy desktop runtime, or replace the later container/runtime acceptance.
The existing T4 limitation remains: retries are initiated by WorkBuddy replay;
there is not yet an autonomous durable callback outbox.


## T6 — Durable GitHub callback outbox

T6 adds a separate SQLite outbox database at `A2A_GITHUB_CALLBACK_OUTBOX_DB_PATH`
(default `./data/github_callback_outbox.db`). The terminal task result is saved
first; the callback intent is inserted idempotently before outbound network I/O.
A background worker drains due entries independently of WorkBuddy replay.

- States: `pending`, `processing`, `delivered`, `dead`.
- SQLite `BEGIN IMMEDIATE` claims due entries with a lease; expired processing
  leases are reclaimable after process or worker failure.
- Exponential retry delay is capped at one hour; default attempt budget is 12.
- Task ID is the unique outbox key. Existing GitHub comment marker lookup remains
  the idempotency guard for ambiguous POST success/response loss.
- WorkBuddy may inspect `GET /integrations/workbuddy/v1/callbacks/{task_id}`
  and manually requeue a pending/dead entry with
  `POST /integrations/workbuddy/v1/callbacks/{task_id}/retry`; both require
  the separate WorkBuddy connector token.
- Startup reconciliation scans persisted terminal tasks and inserts missing
  callback intents, covering a crash after task commit but before outbox insert.

Delivery is at-least-once, not exactly-once. Keep this database on persistent
storage and include it in backups. The worker starts only when the GitHub API
token and repository allowlist are configured. No inbound webhook or public
listener is added.

For the isolated Compose stack, the outbox is stored in the persistent
`/app/data` volume. Back up both databases independently, for example:
`python scripts/sqlite_maintenance.py backup --database /app/data/github_callback_outbox.db --output /app/backups/github_callback_outbox.db`.
Do not omit the outbox database when moving or restoring a Hub instance.

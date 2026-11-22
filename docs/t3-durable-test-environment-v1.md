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
`docker compose down` preserves volumes; `docker compose down -v` deletes them and must not be used when retaining test data.

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

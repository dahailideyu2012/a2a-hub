# T3 — Durable SQLite test environment

This stage creates an isolated, local Docker test stack. It does **not** deploy
to a public host, configure Cloudflare, create production secrets, or activate
the GitHub Issue relay.

## Safety properties

- \`A2A_STORE=sqlite\` is forced by Compose; Pull tasks are not run against memory storage.
- Hub task data and ChatGPT ingress idempotency data live in the named Docker volume
  \`a2a-hub-test-data\`.
- The HTTP port is bound to \`127.0.0.1\` only (default \`18080\`), not exposed to
  the LAN or public Internet.
- API authentication is required. Set a unique random \`A2A_API_TOKEN\` in the
  untracked \`deploy/test/.env.test\` file.
- WorkBuddy Connector, ChatGPT ingress, and GitHub relay credentials are blank
  by default. Configure them only in a later, separately approved integration
  test; never reuse one token for another integration.
- Agent configuration is mounted read-only. Autonomy is disabled.

## Prepare and start

From the repository root:

1. Copy \`deploy/test/.env.test.example\` to \`deploy/test/.env.test\`.
2. Replace \`A2A_API_TOKEN\` with a unique random secret of at least 32 characters.
   Do not commit or paste it into Issues, logs, or chat.
3. Start the stack:

   \`docker compose --env-file deploy/test/.env.test -f deploy/test/compose.yaml up -d --build\`

4. Check health:

   \`curl http://127.0.0.1:18080/health\`

5. Check an authenticated endpoint (replace the placeholder locally):

   \`curl -H "Authorization: Bearer <A2A_API_TOKEN>" http://127.0.0.1:18080/agents\`

The test stack uses a named volume. \`docker compose down\` preserves the volume;
\`docker compose down -v\` deletes it and must not be used when retaining test data.

## SQLite backup and verification

The maintenance script uses Python's SQLite online backup API. This is safe
for a live database and includes committed WAL transactions; it does not copy
only the main \`.db\` file while WAL data may still exist.

Back up the Hub task database:

\`python scripts/sqlite_maintenance.py backup --database ./data/a2a_hub.db --output ./backups/a2a_hub-YYYYMMDD-HHMMSS.db\`

For the Docker volume, first create a backup from inside the container to a
mounted backup directory or use \`docker cp\` to export the resulting backup.
Do not assume \`./data\` on the host is the named volume used by this test stack.

Verify a backup:

\`python scripts/sqlite_maintenance.py verify --database ./backups/a2a_hub-YYYYMMDD-HHMMSS.db\`

Restore procedure (offline only):

1. Stop the Hub: \`docker compose --env-file deploy/test/.env.test -f deploy/test/compose.yaml stop hub-test\`.
2. Export a separate backup of the current database before replacing it.
3. Restore with \`--overwrite\` only after confirming the source backup and destination.
4. Start the Hub again and check \`/health\`, task listing, and Pull connector behavior.

\`restore\` writes a verified temporary database and atomically replaces the
destination. Never restore over a database while the Hub process is running.

## T3 acceptance checklist

- [ ] Compose config resolves and publishes only on loopback.
- [ ] Container starts with SQLite and required API authentication.
- [ ] \`/health\` reports healthy.
- [ ] Create a test Pull task, restart the container, and confirm the task remains.
- [ ] Idempotent replay after restart returns the original task, not a duplicate.
- [ ] Backup contains committed WAL data and passes \`PRAGMA integrity_check\`.
- [ ] Restore is tested against a disposable database with the Hub stopped.
- [ ] WorkBuddy independently reviews the exact commit and reports command outputs.
- [ ] No production endpoint, Cloudflare, real secrets, public port, or merge is used.

T3 code-level acceptance does not prove the real WorkBuddy runtime or public
GitHub-to-Hub relay is operational. That requires a later explicitly approved
integration stage.

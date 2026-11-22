# A2A Hub Storage Backend Evaluation V1

> Status: preliminary architecture assessment; no backend selected.
> Source baseline: 134c1bf9b78b4e473b5079911165d38fd892e164.
> Application baseline: Python FastAPI + synchronous SQLite TaskStore + separate SQLite IngressLedger + JSON SocialGraph snapshot.
> This document does not authorize cloud resource creation, account access, network exposure, DNS changes, tunnel creation, data migration, or deployment.

## 1. Executive summary

There are three realistic options:

1. **VPS + persistent block disk + current SQLite** — lowest implementation and migration risk, provided the service is deliberately single-writer/single-instance and the disk, backup and restore process are reliable.
2. **Cloudflare D1 behind a Worker data-access API** — the best Cloudflare-native relational candidate for combining task records and idempotency in one database transaction, but it requires a deliberate Python-to-Worker API boundary and a new repository adapter. D1 batch executes statements sequentially as a transaction and rolls back the batch if a statement fails. This must be verified against the exact transaction and conditional-write patterns required by the contract. [Cloudflare D1 batch docs](https://developers.cloudflare.com/d1/worker-api/d1-database/)
3. **Durable Object SQLite** — strong per-object transactional consistency and synchronous SQL, but storage is private to each object. A single global object is simple but creates a serialization/hot-object boundary; sharding introduces cross-object transaction and routing complexity. [DO SQLite storage docs](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/) [DO access guidance](https://developers.cloudflare.com/durable-objects/best-practices/access-durable-objects/)

**Do not deploy the current Python container with SQLite on its local filesystem.** Cloudflare documents that Container disk is ephemeral by default; after sleep the next start uses a fresh disk from the image. Snapshots are a distinct mechanism, not a drop-in persistent writable volume. [Containers FAQ](https://developers.cloudflare.com/containers/faq/) [Container lifecycle](https://developers.cloudflare.com/containers/concepts/architecture/)

## 2. Fit against the current application

| Dimension | VPS + SQLite | D1 + Worker API | Durable Object SQLite |
|---|---|---|---|
| Python code reuse | **High** — current sqlite3 store largely reusable | Low/medium — Python must call a defined service API; no local sqlite3 file | Low/medium — Python must call a DO API; no local sqlite3 file |
| Task + ingress atomicity | High if both tables move into the same SQLite DB and same connection/transaction; current separate DBs must be consolidated | High if task + idempotency rows live in one D1 DB and one batch transaction | High inside one DO's SQLite transaction; impossible to assume atomicity across different DOs |
| Pull lease fencing | Existing BEGIN IMMEDIATE behavior is reference; single writer | Must implement conditional updates / transactions in Worker; prove concurrent claims and stale lease rejection | Strong fit within one object; test transaction and concurrency semantics |
| Multi-instance access | SQLite file must not be shared as a multi-host writable database; use one active app instance or redesign | Designed for Worker-mediated access; validate D1 concurrency/limits and API latency | Objects scale horizontally, but each object's state is private and individually serialized |
| Social graph | Existing JSON can remain short-term on persistent disk, but snapshot concurrency is still a design issue | Relational tables can model edges/audit/approvals/quotas | Partition by graph/tenant or one object; global graph object may become hot |
| Rate limiting | Single-process limiter only unless external shared limiter added | Can share through D1 or edge-level service, with explicit atomicity/latency analysis | Could share in one DO, but adds a hot object; better use an edge-level/shared design |
| Backup / recovery | Filesystem/database backup and restore; operator responsibility | Provider backup/export/restore capabilities must be proven for the account and plan | SQLite DO supports point-in-time recovery; per-object restore and multi-object consistency need design |
| Operational burden | Medium; OS patching, disk, TLS, backups, monitoring | Medium/high; Worker API, D1 migrations, observability, platform limits | High; Worker/DO API, object routing, partitioning, hot-object monitoring |
| Lowest-risk first release | **Best fit** | Viable after adapter proof | Viable only after partition model is proved |

## 3. Important provider facts and constraints

### 3.1 D1

- D1 batch executes prepared statements sequentially and treats the batch as a transaction; failure of a statement aborts/rolls back the sequence.
- D1 is accessed through the Workers API, not as a filesystem-mounted SQLite database for this Python application.
- A viable design would place task rows, idempotency rows, Pull lease state and possibly social graph rows in one D1 database when they must share an atomic boundary.
- The Worker API must expose narrow typed operations (enqueue, claim, renew, start, progress, finish, cancel, task CRUD, ingress reserve/complete/reconcile). Do not expose arbitrary SQL.
- Before selection, prototype concurrent claim and reserve operations with conditional SQL and prove that the transaction scope covers every required row.
- Large migrations must be chunked; D1 has platform CPU, memory, query and data-size limits. See [D1 limits](https://developers.cloudflare.com/d1/platform/limits/).

### 3.2 Durable Object SQLite

- Each object has its own private transactional, strongly consistent storage; another object cannot directly access that attached database.
- SQLite-backed DO supports SQL and transactions. A single object can keep task state and idempotency records in one transactional boundary.
- Cloudflare recommends SQLite-backed DO for new namespaces. Its storage supports point-in-time recovery; the documented restore window and plan limits must be checked before production selection.
- Each DO is single-threaded. Cloudflare documents a soft limit of 1,000 requests/second per object, not a throughput guarantee; actual throughput depends on work and payload size.
- A single global DO is the simplest consistency model but can become a hotspot. Sharding requires deterministic routing and a clear rule that a task, its lease, and its ingress idempotency key land in the same shard if they need atomicity.
- The Python application needs a Worker/DO HTTP or RPC boundary; Python sqlite3 cannot open the DO database as a local file.
- References: [SQLite storage API](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/), [DO access guidance](https://developers.cloudflare.com/durable-objects/best-practices/access-durable-objects/), [DO rules and throughput](https://developers.cloudflare.com/durable-objects/best-practices/rules-of-durable-objects/), [DO limits](https://developers.cloudflare.com/durable-objects/platform/limits/).

### 3.3 VPS + SQLite

- This is the closest fit to the existing Python/FastAPI and SQLite code.
- Keep one active Hub writer and one persistent local block disk. Do not put a SQLite database on an unverified network filesystem or share it as a multi-host writable database.
- Consolidate task and ingress idempotency into one database/connection boundary if using the single-transaction solution; otherwise implement a durable outbox/reconciliation protocol.
- Backups must be consistent (SQLite backup API or a coordinated snapshot), encrypted, retained off-host, and regularly restored in an isolated environment.
- Add process supervision, TLS termination, firewall policy, OS updates, monitoring, disk-space alerts and a documented recovery procedure.
- This path still needs an explicitly approved server, domain/HTTPS plan and deployment authorization. The existence of a VPS must not be assumed.

## 4. Preliminary recommendation

For a first production release where the current Python service should remain intact, **VPS + persistent disk + single-writer SQLite is the lowest-risk implementation path**—but only if a suitable server is explicitly confirmed and the backup/restore and HTTPS requirements are accepted.

If the requirement is specifically to keep the entire service Cloudflare-native, **prototype D1 first**. Its relational model and transactional batch API align better with the current task + idempotency schema than a sharded DO design. This is a candidate, not a selection: concurrency tests must prove claim_next, lease fencing, idempotency reservation and task association.

Choose Durable Object SQLite only if its per-object ownership and routing model is intentional (for example, one object per tenant/queue partition), and tests prove no required atomic operation crosses object boundaries.

## 5. Smallest technical spikes before choosing

No cloud account or resource is needed for the first stage. ChatGPT should build local protocol fakes and contract tests first, then implement provider-specific spikes only after the user selects/authorizes a target.

### Spike A — provider-neutral contract suite

- Define typed repository protocols for tasks, ingress idempotency and social graph.
- Move current SQLite behavior behind those protocols without changing runtime behavior.
- Remove concrete SqliteTaskStore checks from rpc.py, chatgpt_ingress.py, and workbuddy_connector.py.
- Add tests for every caller and all existing route error mappings.
- Keep SQLite as the reference adapter.

### Spike B — D1 feasibility proof (only if Cloudflare-native selected)

- Implement a minimal Worker with a local/test D1-compatible database interface.
- Prove one transaction can reserve an idempotency key, create a task and bind the task ID.
- Prove two concurrent claimers cannot receive the same task.
- Prove lease fencing, cancel-vs-result race, duplicate progress/result, and restart visibility.
- Measure end-to-end latency from Python client to Worker API.
- Document migration/export and restore process.

### Spike C — VPS durability proof (only if a server is confirmed)

- Run current tests against an isolated persistent-disk instance, not production.
- Prove restart/read-after-restart for tasks, ingress records and social state.
- Demonstrate backup and restore to a separate isolated path/instance.
- Test disk-full, process crash, and database lock behavior.
- Verify single-writer process supervision and HTTPS ingress design without opening production access.

### Spike D — DO proof (only if DO selected)

- Start with one object owning one task queue partition and its idempotency records.
- Prove all critical operations are inside a single-object SQL transaction.
- Test two clients contending for the same queue, cancellation races and object eviction/restart.
- Load-test the chosen partition and document hot-object behavior before considering sharding.

## 6. Decision gates

No provider is approved until all are true:

1. User confirms the intended deployment target and authorizes the relevant account/server use.
2. Repository contract review is complete.
3. The chosen provider passes concurrent claim, fencing, idempotency, crash recovery and cross-instance tests.
4. Backup/restore and migration are demonstrated in isolation.
5. Secrets, TLS, access control, monitoring and rollback are reviewed.
6. A separate explicit production deployment approval is obtained.

## 7. Current decision

**No backend selected. No provider resources created. No production deployment authorized.**

Next work is limited to repository-port design and contract tests in the existing GitHub development branch. All implementation work remains owned by ChatGPT; WorkBuddy only performs fixed-SHA execution and independent acceptance.

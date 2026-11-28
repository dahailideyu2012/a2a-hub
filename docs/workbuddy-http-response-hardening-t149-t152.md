# WorkBuddy connector bounded HTTP response handling (T149–T160)

## Scope

This batch hardens the WorkBuddy stdio connector's HTTP response reader. It does not change task lifecycle semantics, retry policy, authentication, or the Hub API contract.

## Changes

- **T149 — bounded chunk reader:** `_read_limited(response, limit, label)` reads until EOF in chunks no larger than 16 KiB. It tolerates short reads from buffered/network streams, accepts an exactly-at-limit body, and fails immediately once the body exceeds its byte budget.
- **T150 — consistent response limits:** successful POST responses remain capped at 256 KiB; successful GET responses remain capped at 64 KiB; HTTP error bodies remain capped at 16 KiB. All three paths use the same bounded reader. No unbounded `read()` call is introduced.
- **T151 — regression coverage:** tests cover fragmented/short reads, exact-limit acceptance, limit-plus-one rejection, non-byte stream rejection, and JSON parsing through both GET and POST.
- **T153/T154 — endpoint ceiling regression tests:** exercise the real `_post` and `_get` request paths with synthetic success bodies at one byte above 256 KiB and 64 KiB respectively; assert fail-closed errors and that reading stops at the first over-limit byte.
- **T155 — invalid limit regression tests:** directly verify `_read_limited` rejects booleans, negative values, floats, strings, and `None` before reading the response.
- **T156 — acceptance evidence:** these tests pin the endpoint-specific limits and defensive argument validation; they do not imply a real Hub or WorkBuddy network test.
- **T157 — HTTP error stream lifecycle:** centralize bounded HTTP error parsing and close the `HTTPError` response stream in a `finally` block on every path.
- **T158 — lifecycle regression tests:** cover GET/POST HTTP errors, successful detail parsing, oversized error bodies, and stream closure.
- **T159 — close-failure isolation:** a stream-close exception must not mask the original Hub HTTP status or parsed error detail.
- **T160 — boundary documentation:** HTTP error bodies remain capped at 16 KiB; both success and error response streams are released on exit.
- **T152 — operational documentation:** this note records the limits and the failure behavior for future connector maintenance.

## Failure behavior

Oversized success responses raise a bounded-response error before JSON parsing. Non-byte stream chunks fail closed. Invalid UTF-8/JSON and non-object JSON continue to be rejected by the existing response validation. HTTP error handling still preserves the HTTP status prefix used by the existing transient-retry classifier; the error body is read only up to 16 KiB. The `HTTPError` stream is closed even when reading, decoding, or parsing fails; a close failure is suppressed so it cannot replace the original HTTP status.

## Validation required before acceptance

Run the focused connector regression suite, compileall, and the full pytest suite in a fresh environment. Independently verify fragmented reads and the byte ceilings. Include mutations that restore one-shot reads, remove the body ceiling, and allow non-byte chunks; each mutation must be caught by a product test.

## Explicit non-goals

- No increase to response body limits.
- No retries for claim, start, or renew.
- No change to progress/result idempotency claims.
- No real Hub, Desktop, or WorkBuddy Connector network test is implied by unit tests.


## T161–T164 — handshake receipt and cache guards

- **T161:** reject a handshake response unless `accepted is True`; a rejected receipt must not mark the process handshake-ready.
- **T162:** reject a receipt whose `connector_id` differs from the configured identity; do not cache the mismatched receipt.
- **T163:** reuse a successful handshake only while the complete cache key remains unchanged; repeated checks with identical configuration must not send another handshake.
- **T164:** changing the Hub URL or connector token changes the cache key and forces a fresh handshake. The token is represented in the cache key only by a SHA-256 fingerprint and is never copied into the handshake payload.
- These tests stub the HTTP boundary and verify local receipt/cache behavior only. They do not prove a real Hub, WorkBuddy Desktop, or Connector network session.


## T165–T168 — connection-check stage hardening

- **T165:** validate the initial presence response before sending the idle heartbeat: it must be an object for the configured connector and contain a boolean `online`. An initial `online=false` remains valid because this check is designed to send a heartbeat and then confirm the final state.
- **T166:** handshake failures return a stage-specific connection trace without copying arbitrary exception text, response bodies, headers, or credentials.
- **T167:** initial presence transport failures are captured as `presence_before` failures with only timestamps observed before the failure.
- **T168:** heartbeat-request and final-presence transport failures are captured with distinct stages; traces never fabricate timestamps for steps that did not complete.
- These are local validation and mocked-boundary guarantees; they do not prove a live Hub or WorkBuddy Desktop session.

### T165–T168 acceptance follow-up — presence boundary regression coverage

- The initial presence `online` field is a boolean observation, not a precondition that the connector is already online. A valid `online=false` response must still proceed to the idle heartbeat and final presence check; a regression test asserts the successful transition when final presence becomes online.
- The final presence response must remain an object. Non-object values (including `null` and arrays) must produce the sanitized `final_presence_shape` trace rather than leaking an attribute/type exception.
- These are local mocked-boundary regression tests. They do not establish a live Hub/Desktop/Connector session.

## T169–T172 — connection and recovery state consistency

- **T169 — presence transition validation:** connector status and connection checks use one fail-closed presence contract: the response must be an object, identify the configured connector, and carry a boolean `online`. An initial `online=false` is a valid observation; the post-heartbeat presence must be online. Malformed payloads are rejected without leaking attribute errors.
- **T170 — heartbeat continuity and interruption:** heartbeat status/task pairs are validated locally (`idle` has no task; `busy` requires a non-empty task ID). The ordinary heartbeat, connection check, and cancellation checkpoint share the same authenticated heartbeat request path; authentication/protocol rejection invalidates the cached handshake. Heartbeats are deliberately not automatically retried because the endpoint's replay/idempotency contract is not assumed.
- **T171 — recovery snapshot consistency:** read-only diagnostics must return a structured recovery section and explicitly confirm automatic recovery actions are disabled. Reading a new snapshot replaces the previous process-local snapshot; no handshake or recovery action is triggered by the read.
- **T172 — decision/diagnostic closure:** recording an operator disposition requires the exact recovery snapshot to have been read by the current connector configuration in this MCP process. The snapshot is consumed before the write attempt; after an uncertain/failed write, diagnostics must be read again before another decision attempt. The endpoint only records acknowledged/deferred/dismissed and never executes reconnect or task operations.
- These protections are local protocol and mocked-boundary guarantees. They do not prove a live Hub/Desktop/Connector session or server-side recovery execution.


## T173–T176 — task lifecycle input and lease receipt guards

- **T173 — task identity validation:** start, renew, progress, and result tools require a non-empty string task ID of at most 128 characters and reject control characters before any network request. MCP tool schemas publish the same length bounds.
- **T174 — renewal expiry validation:** renew accepts only a finite numeric Unix expiry strictly later than the current time. Booleans, numeric strings, NaN, infinities, and expired values are rejected; an invalid receipt never mutates the locally held lease.
- **T175 — progress payload validation:** sequence must be a positive integer (not a boolean), and message must be a 1–4000 character string. Invalid progress never reaches the Hub or the idempotent retry wrapper.
- **T176 — result payload validation:** state is limited to completed/failed, output is a string up to 20,000 characters, and an explicit result_key must be 8–128 characters. The default key preserves the existing task-based format when it fits; maximum-length task IDs use a deterministic SHA-256-derived key that remains within the API limit.
- Tests use the MCP `call_tool` boundary with a stubbed HTTP function to prove invalid inputs are rejected before network activity. They do not claim live Hub or Desktop connectivity.



## T177–T180 — claim filters, lease receipts, and local lease integrity

- **T177 — claim argument gate:** optional `agent_ids` must be a unique list of 1–32 valid identifiers (1–64 ASCII letters/digits/dot/underscore/hyphen, starting alphanumeric); optional `lease_seconds` must be a non-boolean integer from 15 through 900. Invalid filters are rejected before handshake or claim network I/O, and MCP schemas publish the same bounds.
- **T178 — strict claim receipt validation:** claimed task IDs use the shared non-empty/128-character/control-character contract; lease tokens must be non-empty bounded printable values; lease versions are positive bounded integers and not booleans; expiry must be a finite numeric Unix timestamp strictly in the future (booleans and numeric strings are rejected). Invalid receipts never enter the local lease registry. Malformed task history is rejected and malformed non-text parts are ignored safely.
- **T179 — local lease registry integrity:** pruning removes expired and malformed lease records without raising; token, version, and expiry types are validated before a record can remain active. A corrupted record cannot crash a later claim or leak a key/type exception through lifecycle tools.
- **T180 — shared task identity boundary:** heartbeat `current_task_id` and cancellation-check `task_id` now use the same canonical validator as start/renew/progress/result. Whitespace-only, control-character, oversized, and non-string identifiers fail locally before handshake/heartbeat. Valid surrounding whitespace is normalized consistently.
- Tests exercise MCP product entry points and the local lease registry with stubbed HTTP only. They do not establish live Hub, Desktop, or Connector connectivity.

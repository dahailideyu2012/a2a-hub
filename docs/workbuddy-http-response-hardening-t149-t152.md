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

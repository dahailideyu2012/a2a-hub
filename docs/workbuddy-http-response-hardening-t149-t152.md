# WorkBuddy connector bounded HTTP response handling (T149–T152)

## Scope

This batch hardens the WorkBuddy stdio connector's HTTP response reader. It does not change task lifecycle semantics, retry policy, authentication, or the Hub API contract.

## Changes

- **T149 — bounded chunk reader:** `_read_limited(response, limit, label)` reads until EOF in chunks no larger than 16 KiB. It tolerates short reads from buffered/network streams, accepts an exactly-at-limit body, and fails immediately once the body exceeds its byte budget.
- **T150 — consistent response limits:** successful POST responses remain capped at 256 KiB; successful GET responses remain capped at 64 KiB; HTTP error bodies remain capped at 16 KiB. All three paths use the same bounded reader. No unbounded `read()` call is introduced.
- **T151 — regression coverage:** tests cover fragmented/short reads, exact-limit acceptance, limit-plus-one rejection, non-byte stream rejection, and JSON parsing through both GET and POST.
- **T152 — operational documentation:** this note records the limits and the failure behavior for future connector maintenance.

## Failure behavior

Oversized success responses raise a bounded-response error before JSON parsing. Non-byte stream chunks fail closed. Invalid UTF-8/JSON and non-object JSON continue to be rejected by the existing response validation. HTTP error handling still preserves the HTTP status prefix used by the existing transient-retry classifier; the error body is read only up to 16 KiB.

## Validation required before acceptance

Run the focused connector regression suite, compileall, and the full pytest suite in a fresh environment. Independently verify fragmented reads and the byte ceilings. Include mutations that restore one-shot reads, remove the body ceiling, and allow non-byte chunks; each mutation must be caught by a product test.

## Explicit non-goals

- No increase to response body limits.
- No retries for claim, start, or renew.
- No change to progress/result idempotency claims.
- No real Hub, Desktop, or WorkBuddy Connector network test is implied by unit tests.

# WorkBuddy capability negotiation contract (T71–T77)

## Scope

This batch makes the existing WorkBuddy handshake capability negotiation
explicit and fail-closed. It does not add new task operations or imply that a
real WorkBuddy Desktop session is connected.

## Contract

- The server's supported capability catalog is defined once in
  `a2a_hub/workbuddy_capabilities.py`.
- Capability names must be lowercase identifiers matching
  `^[a-z][a-z0-9_]{0,63}$`.
- At most 20 capabilities may be advertised in one handshake.
- Duplicate or malformed names reject the handshake (HTTP 422).
- Supported names are returned in stable sorted order.
- Well-formed names unknown to this server are returned separately as
  `unsupported_capabilities`; they are never enabled.
- Existing clients that advertise only the six current capabilities retain
  the same negotiated capability list and behavior.

## Verification

Run:

```bash
pytest -q tests/test_workbuddy_capabilities.py tests/test_workbuddy_handshake.py
pytest -q
```

The tests cover stable negotiation, unknown-capability reporting, empty-list
compatibility, malformed/duplicate input rejection, and the count limit.

## Explicit non-goals

No background polling, automatic task execution, arbitrary tool invocation,
credential changes, production deployment, or real Desktop/Connector
acceptance is included.

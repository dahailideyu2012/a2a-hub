# T8 integration correction — connector fencing token propagation

WorkBuddy's independent T8 review found that the outbox fencing API had been
updated while the synchronous WorkBuddy result route still called the previous
method signatures.

This follow-up fixes both synchronous callback outcomes:

- Successful delivery passes the claimed `lease_token` to
  `mark_delivered(task_id, lease_token)`.
- Failed delivery passes the same `lease_token` to
  `mark_failed(task_id, attempts, lease_token, error)`.

The existing end-to-end regression tests exercise both cases:
- `test_signed_issue_to_workbuddy_to_github_roundtrip_and_ambiguous_retry`
  exercises a GitHub POST accepted with response loss, followed by durable
  failure scheduling and idempotent replay.
- `test_signed_github_task_result_is_returned_to_original_issue` exercises
  synchronous successful delivery and now asserts that the callback status
  endpoint does not expose the internal lease token.

No real GitHub write or credentials are used by tests. This patch is based on
WorkBuddy's T8 FAIL report and must receive fresh CI and independent acceptance.

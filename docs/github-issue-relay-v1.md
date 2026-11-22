# GitHub Issue → A2A Hub relay (T1/T2)

This is an opt-in bridge for accounts that cannot use a full write-capable
ChatGPT Remote MCP connection. It is not native MCP. A GitHub Issue comment is
the command envelope; GitHub Actions validates it and enqueues a Pull task into
the existing restricted ChatGPT ingress. WorkBuddy continues to claim tasks
using its separate WorkBuddy Connector token.

## Command format

Add the label `a2a-relay-enabled` to a private, controlled GitHub Issue.
Only actors listed in `A2A_GITHUB_RELAY_ALLOWED_ACTORS` may submit:

```text
/a2a submit echo-a

Please inspect the latest safe task state and return a concise report.
```

The agent must be present in `A2A_GITHUB_RELAY_ALLOWED_AGENTS` and in the
Hub-side `A2A_CHATGPT_INGRESS_ALLOWED_AGENTS`. The relay always enqueues
`mode=pull`; it never runs arbitrary shell commands or directly executes
agents. Pull requires `A2A_STORE=sqlite` and durable storage.

## GitHub repository configuration

Set Actions variables:
- `A2A_HUB_URL`: public HTTPS Hub base URL, no credentials/query/fragment.
- `A2A_GITHUB_RELAY_ALLOWED_ACTORS`: comma-separated GitHub logins.
- `A2A_GITHUB_RELAY_ALLOWED_AGENTS`: comma-separated agent IDs.

Set Actions secrets:
- `A2A_CHATGPT_INGRESS_TOKEN`: dedicated ingress token (32+ characters).
- `A2A_GITHUB_RELAY_HMAC_SECRET`: independent random HMAC secret (32+ characters).

The Hub must also have `A2A_GITHUB_RELAY_HMAC_SECRET` set to the same HMAC
secret. Never reuse the WorkBuddy Connector token. Do not place secrets in
repository variables, comments, prompts, or source control.

## T2 integrity and replay protection

The relay signs `timestamp + "." + Idempotency-Key + "." + raw JSON body`
using HMAC-SHA256. The Hub verifies the signature in constant time and rejects
timestamps outside a five-minute window. The idempotency key is derived from
the immutable GitHub repository ID, issue number, and comment ID. Existing Hub
ingress idempotency binds the key to the canonical payload for 24 hours:
same key/same payload replays the same task; same key/different payload is 409.
A retry of a transient Hub error reuses the same key and payload.

The workflow only handles newly created issue comments, rejects pull-request
comments, requires the issue label, and enforces actor and agent allowlists.
The comment is parsed as data only; no content is evaluated as shell.

## Activation boundary

GitHub's `issue_comment` workflow runs from the repository's default branch.
Therefore this workflow becomes operational only after its reviewed code is
merged to the default branch. This feature branch is not deployed or activated.
Before enabling it, configure the variables/secrets above and verify the Hub
HTTPS endpoint, SQLite durability, TLS, and firewall policy. Keep PR #6 and this
PR unmerged until independent acceptance and user approval.

## Result

On successful enqueue, the workflow posts a comment with the task ID and
explicitly says that enqueue is not execution completion. WorkBuddy must claim,
execute, and submit the result through the existing Pull Connector. ChatGPT
can then read the result from the Issue thread and Hub task record.

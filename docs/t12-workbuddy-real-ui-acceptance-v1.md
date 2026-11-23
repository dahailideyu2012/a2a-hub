# T12 — Real WorkBuddy desktop UI acceptance

## What this proves

This harness starts a disposable A2A Hub with temporary SQLite databases and a loopback-only reverse proxy. It submits one synthetic task through the Hub's ChatGPT-compatible REST ingress, then waits for the real WorkBuddy desktop UI to claim, start, progress, renew, and submit the result through the existing MCP connector. The harness reads the terminal task state back from the isolated Hub.

This validates the WorkBuddy desktop/MCP ↔ Hub Pull connector loop. It does not validate the ChatGPT product's Remote MCP connection, GitHub Actions relay, real credentials, Cloudflare, or production deployment.

## Safety properties

- Both the temporary Hub and the proxy bind only to 127.0.0.1. The proxy uses port 8080 because that is the local endpoint configured in the WorkBuddy MCP connector.
- The Hub uses disposable SQLite files and synthetic credentials. The proxy checks only that the connector token header exists; it replaces the supplied value with a synthetic token before forwarding. It never reads or reports the user's token value.
- Only WorkBuddy connector POST routes are proxied. The proxy does not expose a general HTTP tunnel.
- The harness bypasses system HTTP proxies, refuses redirects, caps HTTP responses, and redacts upstream error bodies from its report.
- It does not edit WorkBuddy configuration, stop an existing listener, call public services, or access production data. If 127.0.0.1:8080 is already occupied, it exits BLOCKED without touching that listener.
- The task is synthetic: calculate 37 × 41 and return a fixed marker. It must not access the internet, local files, or credentials.
- The loopback proxy does not authenticate individual local processes: any process able to connect to 127.0.0.1:8080 during the test window can submit the synthetic test actions. This is intentionally limited to a disposable local Hub and synthetic credentials; do not run the harness on a shared or untrusted machine.
- The proxy's fixed port is bound before the test starts, so an existing listener is never replaced. The isolated Hub selects an ephemeral port before Uvicorn binds it; a narrow port-allocation race remains possible. If another process claims that port first, startup health-check fails and the harness reports FAIL rather than using the other listener.

## Windows / WorkBuddy procedure

1. Ensure the repository dependencies are installed in the Python environment used for the project.
2. In the WorkBuddy MCP connector entry for netivora-a2a-workbuddy, temporarily set:
   - A2A_HUB_MCP_SERVER_PATH: absolute path to this checkout's workbuddy_mcp_server.py.
   - A2A_WORKBUDDY_HUB_URL: http://127.0.0.1:8080.
   - A2A_WORKBUDDY_CONNECTOR_TOKEN: a disposable placeholder of at least 32 characters, not a real Hub token.
3. Restart/reload WorkBuddy so the connector uses those temporary values and confirm its MCP tools are trusted/available.
4. From the repository root, run:
   ```powershell
   python scripts/workbuddy_real_hub_ui_acceptance.py
   ```
5. When the terminal prints READY_FOR_WORKBUDDY_UI, ask WorkBuddy to claim and execute the single isolated Hub task using the MCP tools. Do not use shell, curl, browser automation, or a second task.
6. Wait for the final JSON report. PASS requires the expected calculation/marker and the exact claim → start → progress → renew → result API sequence, all HTTP 200.
7. Restore the original WorkBuddy connector settings and restart/reload WorkBuddy after the test.

If the test is interrupted, press Ctrl+C in the harness terminal. It shuts down its temporary Hub and proxy and removes its temporary directory. A timeout or interrupted run is not a PASS.

## Test commands

```bash
python -m pytest -q tests/test_workbuddy_real_hub_ui_acceptance.py
python -m compileall -q scripts tests
python scripts/workbuddy_real_hub_ui_acceptance.py
```

The last command is an operator-driven desktop test and requires WorkBuddy to be running with the temporary loopback connector settings. Do not run it against a production connector configuration.
"""Background at-least-once delivery worker for the durable GitHub result outbox."""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from .github_result_callback import GithubResultDeliveryError, deliver_github_issue_result
from .ports import GithubCallbackOutboxRepository

log = logging.getLogger(__name__)


async def run_github_outbox_worker(
    store: GithubCallbackOutboxRepository,
    api_token: str,
    allowed_repos: str,
    stop: asyncio.Event,
    *,
    poll_seconds: float = 2.0,
) -> None:
    """Deliver pending results with durable leases and capped exponential retry.

    Delivery is at-least-once. The deterministic GitHub comment marker makes a
    retry safe if GitHub accepted a POST but the Hub crashed before marking it done.
    """
    while not stop.is_set():
        try:
            item = await asyncio.to_thread(store.claim_github_callback, 60)
            if item is None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await asyncio.to_thread(
                    deliver_github_issue_result,
                    item["callback"], item["task_id"], item["state"], item["output"],
                    api_token, allowed_repos,
                )
                await asyncio.to_thread(store.complete_github_callback, item["id"])
            except GithubResultDeliveryError:
                attempt = int(item["attempt_count"])
                delay = min(3600, 5 * (2 ** min(max(attempt - 1, 0), 10)))
                delay = max(1, int(delay * random.uniform(0.8, 1.2)))
                await asyncio.to_thread(
                    store.retry_github_callback, item["id"],
                    "GitHub callback delivery failed", delay,
                )
                log.warning("GitHub result callback delivery failed; retry scheduled in %s seconds", delay)
            except Exception:
                # Unexpected errors are also retried, but never logged with payloads.
                attempt = int(item["attempt_count"])
                delay = min(3600, 5 * (2 ** min(max(attempt - 1, 0), 10)))
                await asyncio.to_thread(
                    store.retry_github_callback, item["id"],
                    "Unexpected callback worker error", delay,
                )
                log.exception("GitHub result callback worker error; retry scheduled")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("GitHub result outbox polling failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

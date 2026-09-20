#!/usr/bin/env python
"""A2A 客户端示例 —— 不依赖 a2a_hub 包，纯 httpx 手写协议调用。

用于演示「任意第三方程序如何作为 A2A 客户端接入本 Hub」，
也可以作为写自家 agent 的参考实现。

    python examples/a2a_client.py                                  # 完整演示
    python examples/a2a_client.py --prompt "你好" --agent echo
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Iterator

import httpx


class A2AClient:
    """最小可用的 A2A 协议客户端。"""

    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base = base_url.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    # --------------------------- 发现 --------------------------- #

    def agent_card(self, agent_id: str | None = None) -> dict[str, Any]:
        path = (
            f"/agents/{agent_id}/.well-known/agent.json"
            if agent_id
            else "/.well-known/agent.json"
        )
        r = httpx.get(self.base + path, headers=self.headers, timeout=15)
        r.raise_for_status()
        return r.json()

    # --------------------------- 调用 --------------------------- #

    def send(self, text: str, agent_id: str | None = None) -> dict[str, Any]:
        """同步调用，返回完整 Task。"""
        params: dict[str, Any] = {
            "message": {"role": "user", "parts": [{"kind": "text", "text": text}]}
        }
        if agent_id:
            params["agentId"] = agent_id
        r = httpx.post(
            self.base + "/",
            headers=self.headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": params},
            timeout=600,
        )
        r.raise_for_status()
        return self._unwrap(r.json())

    def stream(self, text: str, agent_id: str | None = None) -> Iterator[dict[str, Any]]:
        """流式调用，逐条产出 A2A 事件。"""
        params: dict[str, Any] = {
            "message": {"role": "user", "parts": [{"kind": "text", "text": text}]}
        }
        if agent_id:
            params["agentId"] = agent_id

        with httpx.stream(
            "POST",
            self.base + "/",
            headers={**self.headers, "Accept": "text/event-stream"},
            json={"jsonrpc": "2.0", "id": 2, "method": "message/stream", "params": params},
            timeout=600,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    continue

    @staticmethod
    def _unwrap(envelope: dict[str, Any]) -> dict[str, Any]:
        if envelope.get("error"):
            raise RuntimeError(f"JSON-RPC 错误：{envelope['error']}")
        return envelope["result"]


def demo(base: str, token: str | None) -> None:
    client = A2AClient(base, token)

    print("=" * 68)
    print("1) 能力发现 —— 拉取 Hub 的 Agent Card")
    print("=" * 68)
    card = client.agent_card()
    print(f"名称        : {card['name']}")
    print(f"协议版本    : {card['protocolVersion']}")
    print(f"流式能力    : {card['capabilities']['streaming']}")
    print(f"对外地址    : {card['url']}")
    print(f"托管的 agent: {', '.join(card['metadata']['agents'])}")
    print(f"声明技能数  : {len(card['skills'])}")
    for s in card["skills"][:4]:
        print(f"  · {s['name']:<16} tags={','.join(s.get('tags', [])[:5])}")
    if len(card["skills"]) > 4:
        print(f"  · ... 另有 {len(card['skills']) - 4} 项")

    print()
    print("=" * 68)
    print("2) 流式任务 —— message/stream")
    print("=" * 68)
    for event in client.stream("请用一句话说明什么是 A2A 协议", agent_id="echo"):
        kind = event.get("kind")
        if kind == "status-update":
            print(f"  [state] {event['status']['state']}")
        elif kind == "artifact-update":
            art = event.get("artifact") or {}
            if art.get("name") == "output":
                parts = art.get("parts") or []
                if parts:
                    print(f"  [chunk] {parts[-1].get('text', '')!r}")

    print()
    print("=" * 68)
    print("3) 同步任务 —— message/send（自动路由）")
    print("=" * 68)
    task = client.send("帮我评审一段 Python 并发代码")
    print(f"  taskId : {task['id']}")
    print(f"  agent  : {task.get('metadata', {}).get('agentId', '(自动路由)')}")
    print(f"  state  : {task['status']['state']}")
    for art in task.get("artifacts", []):
        for part in art.get("parts", []):
            if part.get("kind") == "text":
                print(f"  产出   : {part['text'][:120]}")
                break


def main() -> int:
    p = argparse.ArgumentParser(description="A2A Hub 客户端示例")
    p.add_argument("--base", default="http://localhost:8080", help="Hub 地址")
    p.add_argument("--token", default=None, help="Bearer Token")
    p.add_argument("--prompt", default=None, help="只发一条消息（跳过演示）")
    p.add_argument("--agent", default=None, help="指定 agent")
    args = p.parse_args()

    if args.prompt:
        client = A2AClient(args.base, args.token)
        for event in client.stream(args.prompt, args.agent):
            if event.get("kind") == "artifact-update":
                art = event.get("artifact") or {}
                if art.get("name") == "output" and art.get("parts"):
                    sys.stdout.write(art["parts"][-1].get("text", ""))
                    sys.stdout.flush()
        print()
        return 0

    demo(args.base, args.token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs" / "chatgpt-actions-openapi.json"
EXPECTED_OPERATIONS = {
    ("post", "/integrations/chatgpt/v1/tasks"): "submitA2AHubTask",
    ("get", "/integrations/chatgpt/v1/tasks"): "listA2AHubTasks",
    ("get", "/integrations/chatgpt/v1/tasks/{task_id}"): "getA2AHubTask",
    ("post", "/integrations/chatgpt/v1/tasks/{task_id}/cancel"): "cancelA2AHubPullTask",
}


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_openapi_declares_only_supported_chatgpt_ingress_operations() -> None:
    schema = load_schema()
    actual = {
        (method, path): operation["operationId"]
        for path, path_item in schema["paths"].items()
        for method, operation in path_item.items()
        if method in {"get", "post"}
    }
    assert actual == EXPECTED_OPERATIONS


def test_action_uses_custom_header_api_key_authentication() -> None:
    schema = load_schema()
    assert schema["security"] == [{"ChatGPTIngressToken": []}]
    assert schema["components"]["securitySchemes"]["ChatGPTIngressToken"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-ChatGPT-Ingress-Token",
        "description": schema["components"]["securitySchemes"]["ChatGPTIngressToken"]["description"],
    }


def test_submit_requires_idempotency_and_rejects_extra_body_fields() -> None:
    schema = load_schema()
    operation = schema["paths"]["/integrations/chatgpt/v1/tasks"]["post"]
    idempotency = next(
        item for item in operation["parameters"] if item["name"] == "Idempotency-Key"
    )
    assert idempotency["in"] == "header"
    assert idempotency["required"] is True
    body = schema["components"]["schemas"]["SubmitTaskRequest"]
    assert body["additionalProperties"] is False
    assert body["required"] == ["agent_id", "prompt"]
    assert body["properties"]["mode"]["enum"] == ["hub", "pull"]


def test_action_schema_does_not_embed_credentials_or_loopback_server() -> None:
    raw = SCHEMA_PATH.read_text(encoding="utf-8")
    schema = json.loads(raw)
    assert schema["servers"][0]["url"] == "https://YOUR-HUB-DOMAIN"
    assert "127.0.0.1" not in raw
    assert "localhost" not in raw
    assert "Bearer " not in raw
    assert "sk-" not in raw
    assert "A2A_CHATGPT_INGRESS_TOKEN" not in raw


def test_schema_exposes_only_supported_non_streaming_routes() -> None:
    schema = load_schema()
    paths = set(schema["paths"])
    assert "/integrations/chatgpt/v1/tasks/{task_id}/events" not in paths
    assert "/integrations/chatgpt/v1/tasks/{task_id}/cancel" in paths

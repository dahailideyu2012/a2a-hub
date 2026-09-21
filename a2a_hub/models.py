"""A2A 数据模型 —— 严格对齐 A2A Protocol v0.3 的对象定义。

参考：https://a2a-protocol.org/latest/specification/

核心对象：
  AgentCard              —— agent 的"能力名片"，发布在 /.well-known/agent.json
  Message / Part         —— 多模态消息（text / file / data）
  Task / TaskStatus      —— 长任务生命周期管理
  Artifact               —— 任务产出物
  *UpdateEvent           —— SSE 流式事件
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> str:
    """A2A 使用 RFC3339 / ISO8601 UTC 时间戳。"""
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex
    return f"{prefix}{raw}" if prefix else raw


# --------------------------------------------------------------------------- #
# Message / Part —— 多模态内容协商
# --------------------------------------------------------------------------- #


class PartKind(str, Enum):
    TEXT = "text"
    FILE = "file"
    DATA = "data"


class FileWithBytes(BaseModel):
    """内联文件（base64）。"""

    name: Optional[str] = None
    mimeType: Optional[str] = None
    bytes: str


class FileWithUri(BaseModel):
    """外链文件。"""

    name: Optional[str] = None
    mimeType: Optional[str] = None
    uri: str


class TextPart(BaseModel):
    kind: Literal["text"] = "text"
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class FilePart(BaseModel):
    kind: Literal["file"] = "file"
    file: Union[FileWithBytes, FileWithUri]
    metadata: dict[str, Any] = Field(default_factory=dict)


class DataPart(BaseModel):
    kind: Literal["data"] = "data"
    data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


Part = Union[TextPart, FilePart, DataPart]


def parse_part(raw: Any) -> Part:
    """把 dict / 裸字符串宽松地转换成 Part。

    宽松解析是为了兼容各厂商 CLI 的裸文本输出——让人不必手写 kind 字段。
    """
    if isinstance(raw, (TextPart, FilePart, DataPart)):
        return raw
    if isinstance(raw, str):
        return TextPart(text=raw)
    if isinstance(raw, dict):
        kind = raw.get("kind")
        if kind == "text" or ("text" in raw and kind is None):
            return TextPart(**{k: v for k, v in raw.items() if k in TextPart.model_fields})
        if kind == "file":
            return FilePart(**raw)
        if kind == "data":
            return DataPart(**raw)
        # 无法判定时降级为 data part，保证不丢信息
        return DataPart(data=raw)
    return TextPart(text=str(raw))


def part_to_text(part: Part) -> str:
    if isinstance(part, TextPart):
        return part.text
    if isinstance(part, DataPart):
        import json

        return json.dumps(part.data, ensure_ascii=False)
    f = part.file
    name = getattr(f, "name", None) or "file"
    return f"[file: {name}]" if isinstance(f, FileWithUri) else f"[file: {name} (base64)]"


class Message(BaseModel):
    """A2A 消息。role 为 user 表示来自调用方，agent 表示 agent 的回复。"""

    model_config = ConfigDict(populate_by_name=True)

    role: Literal["user", "agent"]
    parts: list[Part]
    messageId: str = Field(default_factory=lambda: new_id("msg-"))
    taskId: Optional[str] = None
    contextId: Optional[str] = None
    kind: Literal["message"] = "message"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def user(cls, text: str, **kwargs: Any) -> "Message":
        return cls(role="user", parts=[TextPart(text=text)], **kwargs)

    @classmethod
    def agent(cls, text: str, **kwargs: Any) -> "Message":
        return cls(role="agent", parts=[TextPart(text=text)], **kwargs)

    def text(self) -> str:
        return "\n".join(part_to_text(p) for p in self.parts)


# --------------------------------------------------------------------------- #
# Artifact —— 任务产出物
# --------------------------------------------------------------------------- #


class Artifact(BaseModel):
    artifactId: str = Field(default_factory=lambda: new_id("art-"))
    name: Optional[str] = None
    description: Optional[str] = None
    parts: list[Part] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def text(cls, text: str, name: Optional[str] = None, **meta: Any) -> "Artifact":
        return cls(name=name, parts=[TextPart(text=text)], metadata=meta)

    def text_content(self) -> str:
        return "\n".join(part_to_text(p) for p in self.parts)


# --------------------------------------------------------------------------- #
# Task —— 长任务生命周期
# --------------------------------------------------------------------------- #


class TaskState(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    CANCELED = "canceled"
    FAILED = "failed"
    REJECTED = "rejected"
    AUTH_REQUIRED = "auth-required"
    UNKNOWN = "unknown"


TERMINAL_STATES = {
    TaskState.COMPLETED,
    TaskState.CANCELED,
    TaskState.FAILED,
    TaskState.REJECTED,
}


class TaskStatus(BaseModel):
    state: TaskState = TaskState.SUBMITTED
    message: Optional[Message] = None
    timestamp: str = Field(default_factory=utc_now)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class Task(BaseModel):
    id: str = Field(default_factory=lambda: new_id("task-"))
    contextId: str = Field(default_factory=lambda: new_id("ctx-"))
    status: TaskStatus = Field(default_factory=TaskStatus)
    artifacts: list[Artifact] = Field(default_factory=list)
    history: list[Message] = Field(default_factory=list)
    kind: Literal["task"] = "task"
    metadata: dict[str, Any] = Field(default_factory=dict)

    # --- 以下为 Hub 内部扩展字段（不参与 A2A 线上序列化语义） ---
    agentId: Optional[str] = Field(default=None, exclude=True)
    createdAt: str = Field(default_factory=utc_now, exclude=True)
    updatedAt: str = Field(default_factory=utc_now, exclude=True)

    def touch(self, state: TaskState, message: Optional[Message] = None) -> "Task":
        self.status = TaskStatus(state=state, message=message, timestamp=utc_now())
        self.updatedAt = self.status.timestamp
        return self

    def add_artifact(self, artifact: Artifact) -> Artifact:
        # 同名 artifact 视为流式增量，追加 part 而非新建
        if artifact.name:
            for existing in self.artifacts:
                if existing.name == artifact.name:
                    existing.parts.extend(artifact.parts)
                    return existing
        self.artifacts.append(artifact)
        return artifact

    def final_text(self) -> str:
        return "\n".join(a.text_content() for a in self.artifacts if a.text_content())


# --------------------------------------------------------------------------- #
# SSE 流式事件
# --------------------------------------------------------------------------- #


class TaskStatusUpdateEvent(BaseModel):
    kind: Literal["status-update"] = "status-update"
    taskId: str
    contextId: str
    status: TaskStatus
    final: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskArtifactUpdateEvent(BaseModel):
    kind: Literal["artifact-update"] = "artifact-update"
    taskId: str
    contextId: str
    artifact: Artifact
    append: bool = False
    lastChunk: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


TaskEvent = Union[Task, Message, TaskStatusUpdateEvent, TaskArtifactUpdateEvent]


# --------------------------------------------------------------------------- #
# AgentCard —— 能力名片
# --------------------------------------------------------------------------- #


class AgentSkill(BaseModel):
    id: str
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    inputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    outputModes: list[str] = Field(default_factory=lambda: ["text/plain"])


class AgentCapabilities(BaseModel):
    streaming: bool = True
    pushNotifications: bool = False
    stateTransitionHistory: bool = False


class AgentProvider(BaseModel):
    organization: str = "A2A Hub"
    url: Optional[str] = None


class AgentInterface(BaseModel):
    """A2A v0.3 的多传输端点声明。"""

    url: str
    transport: str = "JSONRPC"


class AgentCard(BaseModel):
    name: str
    description: str = ""
    url: str
    version: str = "1.0.0"
    protocolVersion: str = "0.3.0"
    preferredTransport: str = "JSONRPC"
    additionalInterfaces: list[AgentInterface] = Field(default_factory=list)
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    defaultInputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    defaultOutputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    skills: list[AgentSkill] = Field(default_factory=list)
    provider: Optional[AgentProvider] = None
    securitySchemes: dict[str, Any] = Field(default_factory=dict)
    security: list[dict[str, list[str]]] = Field(default_factory=list)
    iconUrl: Optional[str] = None
    documentationUrl: Optional[str] = None


# --------------------------------------------------------------------------- #
# JSON-RPC 2.0 信封
# --------------------------------------------------------------------------- #


class JsonRpcErrorCodes:
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    # A2A 自定义
    TASK_NOT_FOUND = -32001
    TASK_NOT_CANCELABLE = -32002
    PUSH_NOT_SUPPORTED = -32003
    UNSUPPORTED_OPERATION = -32004
    CONTENT_TYPE_NOT_SUPPORTED = -32005
    INVALID_AGENT_RESPONSE = -32006
    AUTHENTICATED_EXTENDED_CARD_NOT_CONFIGURED = -32007
    #: 社交门禁拒绝（非好友 / 缺少所需 scope）。data 里带 hint 指路。
    SOCIAL_DENIED = -32008


class JsonRpcRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: dict[str, Any] = Field(default_factory=dict)
    id: Optional[Union[str, int]] = None


class JsonRpcError(BaseModel):
    code: int
    message: str
    data: Any = None


class JsonRpcResponse(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: Optional[Union[str, int]] = None
    result: Any = None
    error: Optional[JsonRpcError] = None


class A2AError(Exception):
    """带 JSON-RPC 错误码的业务异常。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_rpc_error(self) -> JsonRpcError:
        return JsonRpcError(code=self.code, message=self.message, data=self.data)

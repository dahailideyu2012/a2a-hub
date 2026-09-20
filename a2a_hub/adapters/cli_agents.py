"""本地 CLI Agent 适配器 —— Claude Code / Codex / WorkBuddy / Gemini / 通用 CLI。

这些工具本身没有统一的远程 API，但都提供"无头模式"(headless / print mode)：
给一段 prompt，把结果打到 stdout。适配器负责：
  1. 构造 argv（prompt 作为独立 argv 元素传入，避免 shell 注入）
  2. 逐行读取 stdout，按预设解析器抽出增量文本
  3. 翻译成 A2A artifact 增量事件
  4. 支持超时与取消（杀死子进程树）

输出解析器（``parser`` 配置项）：
  text         —— 原样输出，每行一个增量（最简单，任何 CLI 都能用）
  claude       —— Claude Code `--output-format stream-json --verbose`
  codex        —— Codex `exec --json` 的 JSONL 事件流
  jsonl        —— 通用 JSONL：按 text_paths 抽字段，自动去重（累计前缀判定）
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Optional

from ..models import TaskEvent
from .base import (
    AdapterError,
    BaseAdapter,
    TaskContext,
    resolve_executable,
    run_command_stream,
    split_command,
)


# --------------------------------------------------------------------------- #
# 输出解析器
# --------------------------------------------------------------------------- #


class BaseParser:
    """把一行 stdout 翻译为零个或多个增量文本。"""

    def feed(self, line: str) -> list[str]:
        return [line] if line else []

    def flush(self) -> list[str]:
        return []


class TextParser(BaseParser):
    """每行都是一段新文本。"""


class JsonlParser(BaseParser):
    """通用 JSONL 解析：按路径抽文本，并用"累计前缀"策略去重。

    很多 CLI 每次输出的是**到目前为止的完整消息**而非增量（Claude Code
    不带 --include-partial-messages 时就是这样）。这里记录每个来源已发出
    的长度，只发新增部分，两种形态都能正确工作。
    """

    def __init__(self, text_paths: Optional[list[str]] = None) -> None:
        self.text_paths = text_paths or []
        self._emitted: dict[str, int] = {}

    def feed(self, line: str) -> list[str]:
        line = line.strip()
        if not line or not line.startswith("{"):
            return []
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return []

        out: list[str] = []
        for source, text in self._extract(obj):
            if not text:
                continue
            prev_len = self._emitted.get(source, 0)
            if len(text) > prev_len:
                out.append(text[prev_len:])
                self._emitted[source] = len(text)
        return out

    # ------------------------------------------------------------------ #

    def _extract(self, obj: dict[str, Any]) -> list[tuple[str, str]]:
        results: list[tuple[str, str]] = []
        for path in self.text_paths:
            node: Any = obj
            ok = True
            for key in path.split("."):
                if isinstance(node, dict) and key in node:
                    node = node[key]
                elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
                    node = node[int(key)]
                else:
                    ok = False
                    break
            if ok and isinstance(node, str) and node:
                results.append((path, node))
        if results:
            return results
        return _recursive_texts(obj)


_TEXT_KEYS = ("text", "content", "result", "output", "message", "answer", "delta")


def _recursive_texts(obj: Any, prefix: str = "") -> list[tuple[str, str]]:
    """兜底：递归找出疑似正文的字符串。

    只在显式路径都没命中的时候用，尽量保守——优先取白名单 key，
    并且跳过明显是元数据的短字符串。
    """
    found: list[tuple[str, str]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                child = f"{path}.{key}" if path else key
                if isinstance(val, str) and key.lower() in _TEXT_KEYS and len(val) > 0:
                    found.append((child, val))
                elif isinstance(val, (dict, list)):
                    walk(val, child)
        elif isinstance(node, list):
            for i, val in enumerate(node):
                walk(val, f"{path}.{i}")

    walk(obj, prefix)
    return found


class ClaudeParser(JsonlParser):
    """Claude Code stream-json 专用。

    事件形如：
      {"type":"assistant","message":{"id":"msg_1","content":[{"type":"text","text":"..."}]}}
      {"type":"result","subtype":"success","result":"...","session_id":"..."}
    """

    def feed(self, line: str) -> list[str]:
        line = line.strip()
        if not line or not line.startswith("{"):
            return []
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return []

        etype = obj.get("type")
        out: list[str] = []

        if etype in ("assistant", "user"):
            message = obj.get("message") or {}
            mid = message.get("id") or obj.get("uuid") or etype
            for idx, block in enumerate(message.get("content") or []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") not in (None, "text"):
                    continue
                text = block.get("text") or ""
                if not text:
                    continue
                source = f"{mid}#{idx}"
                prev = self._emitted.get(source, 0)
                if len(text) > prev:
                    out.append(text[prev:])
                    self._emitted[source] = len(text)

        elif etype == "result":
            text = obj.get("result") or ""
            # 只有在前面完全没有产出时才用 result，避免重复
            if text and not self._emitted:
                out.append(text)
                self._emitted["result"] = len(text)

        return out


class CodexParser(JsonlParser):
    """Codex `exec --json` 事件流。

    不同版本字段略有差异，这里同时兼容 ``item.text`` /
    ``msg.message`` / ``content`` 几种形态，并沿用去重机制。
    """

    def feed(self, line: str) -> list[str]:
        line = line.strip()
        if not line or not line.startswith("{"):
            return []
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return []

        out: list[str] = []
        candidates: list[tuple[str, str]] = []

        item = obj.get("item")
        if isinstance(item, dict):
            if item.get("type") in ("agent_message", "assistant_message", "message"):
                for key in ("text", "content", "message"):
                    val = item.get(key)
                    if isinstance(val, str) and val:
                        candidates.append((f"item.{item.get('id', 'x')}.{key}", val))
        msg = obj.get("msg")
        if isinstance(msg, dict):
            for key in ("message", "text", "content"):
                val = msg.get(key)
                if isinstance(val, str) and val:
                    candidates.append((f"msg.{msg.get('type', 'x')}.{key}", val))
        if not candidates:
            candidates = _recursive_texts(obj)

        for source, text in candidates:
            prev = self._emitted.get(source, 0)
            if len(text) > prev:
                out.append(text[prev:])
                self._emitted[source] = len(text)
        return out


PARSERS: dict[str, type[BaseParser]] = {
    "text": TextParser,
    "jsonl": JsonlParser,
    "claude": ClaudeParser,
    "codex": CodexParser,
}


# --------------------------------------------------------------------------- #
# 通用 CLI 适配器
# --------------------------------------------------------------------------- #


class CliAgentAdapter(BaseAdapter):
    """把任意"接收 prompt、输出文本"的 CLI 接入为 A2A agent。"""

    type = "cli"
    streaming = True

    #: 子类覆盖：默认命令模板，{prompt} 会被替换为实际的 prompt 参数
    default_command: str = ""
    #: 子类覆盖：默认解析器
    default_parser: str = "text"
    #: 子类覆盖：可执行文件环境变量名，用于定位二进制
    executable_env: str = ""

    def __init__(self, spec, hub_settings=None) -> None:
        super().__init__(spec, hub_settings)
        cfg = spec.config or {}
        self.command: str = cfg.get("command") or self.default_command
        self.parser_name: str = cfg.get("parser", self.default_parser)
        self.send_via_stdin: bool = bool(cfg.get("stdin", False))
        self.cwd: Optional[str] = cfg.get("cwd")
        self.timeout: float = float(
            cfg.get("timeout")
            or (hub_settings.task_timeout if hub_settings else 180)
        )
        self.env_extra: dict[str, str] = cfg.get("env", {}) or {}
        self.prepend_prompt: str = cfg.get("prompt_prefix", "")
        self.append_prompt: str = cfg.get("prompt_suffix", "")

        if self.executable_env:
            exe = os.environ.get(self.executable_env)
            if exe:
                self._replace_executable(exe)

    def _replace_executable(self, exe: str) -> None:
        tokens = split_command(self.command)
        if tokens:
            tokens[0] = exe
            self.command = " ".join(f'"{t}"' if " " in t else t for t in tokens)

    # ------------------------------------------------------------------ #

    def _resolved_executable(self) -> Optional[str]:
        tokens = split_command(self.command)
        if not tokens:
            return None
        return resolve_executable(tokens[0]) or tokens[0]

    def build_argv(self, prompt: str) -> tuple[list[str], Optional[str]]:
        """返回 (argv, stdin_text)。prompt 作为独立 argv 元素，杜绝注入。"""
        argv = split_command(self.command)
        if not argv:
            raise AdapterError(f"agent `{self.id}` 未配置命令模板")

        exe = resolve_executable(argv[0])
        if exe:
            argv[0] = exe

        text = f"{self.prepend_prompt}{prompt}{self.append_prompt}"
        has_placeholder = any("{prompt}" in t for t in argv)
        argv = [t.replace("{prompt}", text) for t in argv]

        if self.send_via_stdin or not has_placeholder:
            return argv, text
        return argv, None

    async def health(self) -> dict[str, Any]:
        if not self.spec.enabled:
            return {"status": "disabled", "detail": ""}
        tokens = split_command(self.command)
        if not tokens:
            return {"status": "unavailable", "detail": "未配置 command"}
        exe = resolve_executable(tokens[0])
        if exe:
            return {"status": "healthy", "detail": f"已找到 {exe}"}
        return {
            "status": "unavailable",
            "detail": f"PATH 中找不到 `{tokens[0]}`，请在 agents.yaml 的 command 里写完整路径",
        }

    # ------------------------------------------------------------------ #

    async def execute(self, ctx: TaskContext) -> AsyncIterator[TaskEvent]:
        argv, stdin_text = self.build_argv(ctx.prompt)
        parser_cls = PARSERS.get(self.parser_name, TextParser)
        parser: BaseParser = parser_cls()

        yield ctx.status("working", f"调用本地 CLI：{argv[0]}")  # type: ignore[arg-type]

        first = True
        produced = False
        buffer: list[str] = []

        async for line in run_command_stream(
            argv,
            cwd=self.cwd,
            env=self.env_extra,
            stdin_text=stdin_text,
            timeout=self.timeout,
            task_id=ctx.task.id,
            registry=self.processes,
        ):
            for delta in parser.feed(line):
                if not delta:
                    continue
                produced = True
                buffer.append(delta)
                yield ctx.artifact(delta, name="output", append=not first)
                first = False

        for delta in parser.flush():
            produced = True
            yield ctx.artifact(delta, name="output", append=not first)
            first = False

        if not produced:
            yield ctx.artifact("(CLI 未返回任何输出)", name="output")

        yield ctx.artifact("", name="output", append=True, last_chunk=True)

        meta = {
            "cli": argv[0],
            "parser": self.parser_name,
            "chars": sum(len(b) for b in buffer),
        }
        yield ctx.artifact(
            json.dumps(meta, ensure_ascii=False), name="run-meta", metadata=meta
        )
        for e in ctx.finish():
            yield e


# --------------------------------------------------------------------------- #
# 具体 CLI 适配器预设
# --------------------------------------------------------------------------- #


class ClaudeCodeAdapter(CliAgentAdapter):
    """Anthropic Claude Code（`claude`，需 npm i -g @anthropic-ai/claude-code）。"""

    type = "claude_code"
    executable_env = "CLAUDE_CLI"
    default_parser = "claude"
    default_command = (
        "claude -p {prompt} --output-format stream-json --verbose "
        "--permission-mode bypassPermissions"
    )


class CodexAdapter(CliAgentAdapter):
    """OpenAI Codex CLI（`codex`）。"""

    type = "codex"
    executable_env = "CODEX_CLI"
    default_parser = "codex"
    default_command = "codex exec --skip-git-repo-check --json {prompt}"


class WorkBuddyAdapter(CliAgentAdapter):
    """WorkBuddy CLI（`workbuddy`，无头模式）。"""

    type = "workbuddy"
    executable_env = "WORKBUDDY_CLI"
    default_parser = "jsonl"
    default_command = "workbuddy -p {prompt} --output-format stream-json"


class GeminiAdapter(CliAgentAdapter):
    """Google Gemini CLI（`gemini`）。"""

    type = "gemini"
    executable_env = "GEMINI_CLI"
    default_parser = "text"
    default_command = "gemini -p {prompt}"


class GenericCliAdapter(CliAgentAdapter):
    """通用 CLI：任何 `xxx -p {prompt}` 的工具都能挂上来。"""

    type = "generic_cli"
    default_parser = "text"
    default_command = ""


__all__ = [
    "CliAgentAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "WorkBuddyAdapter",
    "GeminiAdapter",
    "GenericCliAdapter",
    "TextParser",
    "JsonlParser",
    "ClaudeParser",
    "CodexParser",
    "PARSERS",
]

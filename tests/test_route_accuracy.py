# -*- coding: utf-8 -*-
"""锁定「该找谁」的准确性 —— config/agents.yaml 里 skills 的回归防线。

为什么需要它
------------
``registry.score()`` 完全依赖 agents.yaml 的 skills 打分（看的是 skill 的
``name`` / ``description`` / ``tags`` / ``id``），而 skills 同时还是社交简报里
展示给好友的能力清单。**改 YAML 很容易在毫无察觉的情况下把路由带偏**：
一个写得太宽的标签会变成「万有引力」，把不相干的查询全吸到某个 agent 上。

真实翻车案例：千问的通用问答技能曾带 ``是什么`` / ``为什么`` 这类虚词标签，
而中文分词会产生它们的 2-3gram，结果「这段脚本为什么会超时」被路由到了
写作 agent，而不是真正该接的 Codex。

本文件用**真实的** config/agents.yaml（不是 conftest 的 fixture）跑一批
代表性查询做断言。刻意用 ``rank()`` 而非 ``route()``：前者不做健康过滤，
因为「打分准不准」和「API Key 配没配」是两件独立的事。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_FILE = REPO_ROOT / "config" / "agents.yaml"

# (查询, 期望胜出的 agent id)
CASES = [
    # 代码评审 / 仓库理解 -> Claude Code
    ("帮我评审这段并发代码的安全问题", "claude-code"),
    ("审查这两百行 Python，指出缺陷和可维护性", "claude-code"),
    ("这个仓库的架构和调用链是怎样的", "claude-code"),
    # 算法实现 / 迁移 -> Codex
    ("实现一个 LRU 缓存算法并写单测验证", "codex"),
    ("把这套 Python 2 脚本迁移到 Python 3", "codex"),
    # 调试排错 -> Codex（这条曾被千问的虚词标签抢走过）
    ("调试这段脚本为什么会超时", "codex"),
    # 中文写作 / 办公文档 -> 千问
    ("把这份技术材料改写成给管理层的汇报稿", "qwen-office"),
    ("生成一份 10 页 PPT 大纲和会议纪要", "qwen-office"),
    ("把这段口语化的内容润色得正式一点", "qwen-office"),
    # 超长文档 -> Kimi
    ("这份一百多页的长文档讲了什么，帮我做摘要", "kimi"),
    # 数学推理 -> DeepSeek
    ("证明这个数学命题，多步推理", "deepseek"),
    # 本地文件 / 技能编排 -> WorkBuddy
    ("扫描本地工作区目录并批量重命名文件", "workbuddy"),
    ("调用已安装的技能完成这个自动化任务", "workbuddy"),
    # Bot 工作流 -> 扣子
    ("用扣子工作流查这批订单的状态", "coze"),
    # 连通性自检 -> echo
    ("ping 测试连通性", "echo"),
    # 知识问答 -> 千问（允许 glm 接手，见下方断言）
    ("解释一下什么是向量数据库", "qwen-office"),
]


@pytest.fixture(scope="module")
def real_registry():
    """用真实 config/agents.yaml 构造 registry，跑完恢复全局状态。"""
    import a2a_hub.config as cfg
    import a2a_hub.registry as reg

    prev = os.environ.get("A2A_AGENTS_FILE")
    os.environ["A2A_AGENTS_FILE"] = str(AGENTS_FILE)
    cfg._settings = None
    reg._registry = None
    try:
        yield reg.bootstrap(cfg.Settings())
    finally:
        cfg._settings = None
        reg._registry = None
        if prev is None:
            os.environ.pop("A2A_AGENTS_FILE", None)
        else:
            os.environ["A2A_AGENTS_FILE"] = prev


@pytest.mark.parametrize("query,expected", CASES, ids=[c[0][:14] for c in CASES])
def test_route_picks_expected_agent(real_registry, query, expected):
    ranked = real_registry.rank(query, top_k=5, only_enabled=True)
    assert ranked, f"{query!r} 没有任何 agent 参与打分"

    detail = "  ".join(f"{a.id}={s:.1f}" for a, s in ranked[:3])
    top_id = ranked[0][0].id
    assert top_id == expected, (
        f"{query!r} 首选是 {top_id}，期望 {expected}。"
        f"前三名得分：{detail}。"
        f"若改动 agents.yaml 的 skills 后本用例失败，多半是某个标签写得太泛，"
        f"把所有查询都吸过去了 —— 检查 tags 里的通用词（尤其是虚词）。"
    )


def test_skill_ids_are_unique_across_agents():
    """跨 agent 的 skill id 必须唯一。

    Hub 会把所有子 agent 的 skill 聚合进自己的 Agent Card，同名 id 会让人
    分不清这条能力来自谁。
    """
    data = yaml.safe_load(AGENTS_FILE.read_text(encoding="utf-8"))
    owner: dict[str, str] = {}
    dupes: list[str] = []
    for agent in data["agents"]:
        for skill in agent.get("skills", []):
            sid = skill["id"]
            if sid in owner:
                dupes.append(f"{sid}: {owner[sid]} 与 {agent['id']}")
            else:
                owner[sid] = agent["id"]
    assert not dupes, f"重复的 skill id：{'；'.join(dupes)}"


def test_every_skill_has_scoring_material():
    """每条 skill 都要有能被打分命中的素材。

    score() 只吃 name / description / tags / id。少了 tags 或 description，
    这条能力在路由上等于隐形；虚词标签则会误伤（见模块 docstring 的翻车案例）。
    """
    data = yaml.safe_load(AGENTS_FILE.read_text(encoding="utf-8"))
    problems: list[str] = []
    # 这些只能是「任何问题里都可能出现」的词，写出来会让该技能变成万能磁铁
    banned_tags = {"是什么", "为什么", "怎么样", "如何", "什么事", "怎么办"}

    for agent in data["agents"]:
        for skill in agent.get("skills", []):
            label = f"{agent['id']}.{skill['id']}"
            if not skill.get("tags"):
                problems.append(f"{label} 缺 tags")
            if not skill.get("description"):
                problems.append(f"{label} 缺 description")
            hit = banned_tags & set(skill.get("tags") or [])
            if hit:
                problems.append(f"{label} 含虚词标签 {sorted(hit)}")

    assert not problems, "\n".join(problems)

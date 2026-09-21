"""阶段 3：发现、FOF 与引荐。

阶段 1 解决「怎么成为好友」，阶段 2 解决「好友也不是什么都能干」，
这一阶段解决**「怎么找到该加谁」**——也就是「扩大交流圈层」的入口。

三件事，各有一条容易做错的约束：

1. **三档可见性**（``private`` / ``circle`` / ``public``）。
   做错的表现是「私密成员出现在别人的搜索结果里」——一次扫库就泄露全图。
2. **隐私铁律**：对外只出**共同好友数**，不出名单。
   否则加一个人就等于交出整个通讯录，再扩散一轮就拿到了全图。
3. **引荐**是 ``d2 → d1`` 的唯一自然通道，且**不授予任何权限**——
   它只提高可信度，并让 ``private`` 成员可被对方发现。
"""

from __future__ import annotations

import json

import pytest
import yaml

from a2a_hub.relations import (
    DISTANCE_UNREACHABLE,
    Member,
    Scope,
    SocialError,
    SocialGraph,
    SocialNotPermitted,
)

HUMAN = "human:seafish"
GUEST = "human:guest"
A = "agent:ocr-a"
B = "agent:codex"
C = "agent:writer"


# --------------------------------------------------------------------------- #
# 夹具：一个小型社交网络
# --------------------------------------------------------------------------- #


@pytest.fixture
def net(tmp_path) -> SocialGraph:
    """``海鱼``↔``访客``、``海鱼``↔``ocr-a``、``海鱼``↔``codex``、``海鱼``↔``writer``。

    于是 访客 到 ocr-a/codex/writer 的距离都是 2（共同好友 = 海鱼）。
    ``writer`` 是 ``private``，用来验证「私密成员的唯一对外通道是引荐」。
    """
    g = SocialGraph(
        [
            Member(id=HUMAN, name="海鱼", kind="human", discoverable="circle",
                   bio="Hub 的部署者"),
            Member(id=GUEST, name="访客", kind="human", discoverable="public"),
            Member(id=A, name="OCR 助手", kind="agent", owner=HUMAN, discoverable="public",
                   bio="擅长 OCR 与 PDF 表格提取"),
            Member(id=B, name="Codex", kind="agent", owner=HUMAN, discoverable="public",
                   bio="算法实现与代码调试"),
            Member(id=C, name="写作助手", kind="agent", owner=HUMAN, discoverable="private",
                   bio="中文写作与润色"),
        ],
        mode="strict",
        path=tmp_path / "relations.json",
    )
    for peer in (GUEST, A, B, C):
        g.request(HUMAN, peer, f"我是 {HUMAN}")
        g.accept(peer, HUMAN, [Scope.PEEK, Scope.CHAT, Scope.INVITE])
    return g


# --------------------------------------------------------------------------- #
# 距离与共同好友
# --------------------------------------------------------------------------- #


class TestDistance:
    def test_self_is_zero(self, net):
        assert net.distance(GUEST, GUEST) == 0

    def test_friend_is_one(self, net):
        assert net.distance(GUEST, HUMAN) == 1

    def test_friend_of_friend_is_two(self, net):
        assert net.distance(GUEST, A) == 2

    def test_stranger_is_unreachable(self, net):
        # 造一个完全没边的人
        net.ensure_agents([("lonely", "独行者")])
        assert net.distance(GUEST, "agent:lonely") == DISTANCE_UNREACHABLE

    def test_common_friends_counts(self, net):
        assert net.common_friends(GUEST, A) == 1
        assert net.common_friends(GUEST, B) == 1

    def test_mutual_friends_names_are_available_internally(self, net):
        # `mutual_friends` 是内部用的；对外只出数量（见 TestPrivacy）
        assert net.mutual_friends(GUEST, A) == [HUMAN]


# --------------------------------------------------------------------------- #
# 三档可见性
# --------------------------------------------------------------------------- #


class TestVisibilityTiers:
    def test_public_is_visible_to_anyone(self, net):
        net.ensure_agents([("stranger-agent", "陌生人")])
        assert net.visible_to("agent:stranger-agent", B) is True

    def test_circle_needs_distance_two(self, net):
        assert net.visible_to(GUEST, HUMAN) is True  # d1
        assert net.visible_to(GUEST, A) is True  # d2
        net.ensure_agents([("far-away", "很远的人")])
        # far-away 声明是 private（ensure_agents 的默认），造个 circle 的来试
        net._members["agent:far-away"].discoverable = "circle"
        assert net.visible_to("agent:far-away", HUMAN) is False  # d99 > 2

    def test_private_is_visible_to_nobody(self, net):
        assert net.visible_to(GUEST, C) is False
        assert net.visible_to(HUMAN, C) is False  # 连好友也不行——它选择不被「发现」
        assert net.visible_to(C, C) is True  # 自己除外

    def test_owner_control_is_not_a_discovery_channel(self, net):
        """owner 对自己的 agent 天然全权，但「全权」不等于「可发现」。"""
        assert net.can(HUMAN, C, Scope.DELEGATE) is True  # 控制照旧
        assert net.visible_to(HUMAN, C) is False  # 但 private 不进发现面

    def test_search_respects_the_tiers(self, net):
        ids = {m["id"] for m in net.search("", viewer=GUEST)}
        assert A in ids and B in ids
        assert C not in ids  # private
        assert HUMAN in ids  # d1

    def test_search_without_viewer_keeps_the_old_rule(self, net):
        """无身份的本地/测试场景退化为「只排除 private」，与阶段 1 一致。"""
        ids = {m["id"] for m in net.search("")}
        assert C not in ids
        assert HUMAN in ids and A in ids


# --------------------------------------------------------------------------- #
# 匹配打分
# --------------------------------------------------------------------------- #


class TestAffinityScoring:
    def test_breakdown_matches_the_formula(self, net):
        r = net.affinity(GUEST, A, need="OCR 表格提取")
        b = r["breakdown"]
        expected = 0.45 * b["skill"] + 0.30 * b["fof"] + 0.15 * b["kind"] - 0.10 * b["rejectPenalty"]
        assert abs(r["affinity"] - max(0.0, expected)) < 1e-3

    def test_need_match_beats_a_worse_match(self, net):
        good = net.affinity(GUEST, A, need="OCR 表格提取")["affinity"]
        bad = net.affinity(GUEST, B, need="OCR 表格提取")["affinity"]
        assert good > bad

    def test_fof_contributes(self, net):
        """共同好友是社交网络里最强的信任信号，必须有正向贡献。"""
        r = net.affinity(GUEST, A)
        assert r["breakdown"]["fof"] > 0
        assert r["commonFriends"] == 1

    def test_affinity_is_bounded(self, net):
        for cand in (A, B, C, HUMAN):
            v = net.affinity(GUEST, cand, need="随便什么")["affinity"]
            assert 0.0 <= v <= 1.0

    def test_recent_rejection_is_penalised(self, net):
        net.request(GUEST, B, "想加你")
        net.reject(B, GUEST, "暂时不需要")
        r = net.affinity(GUEST, B)
        assert r["breakdown"]["rejectPenalty"] == 1.0

    def test_duplicate_capabilities_are_discounted(self, net):
        """我已有的能力不加分，否则会加一堆同类重复的 agent。"""
        # 让 GUEST 也有 OCR 能力
        net._members[GUEST].bio = "擅长 OCR 与 PDF 表格提取"
        dup = net.affinity(GUEST, A, need="OCR 表格提取")["breakdown"]["skill"]
        net._members[GUEST].bio = ""
        fresh = net.affinity(GUEST, A, need="OCR 表格提取")["breakdown"]["skill"]
        assert dup < fresh

    def test_injected_capability_resolver_is_used(self, net):
        net.set_capability_resolver(
            lambda mid: {"skills": ["量子计算 量子纠错"]} if mid == B else {}
        )
        hit = net.affinity(GUEST, B, need="量子纠错")["breakdown"]["skill"]
        assert hit > 0


# --------------------------------------------------------------------------- #
# 发现
# --------------------------------------------------------------------------- #


class TestDiscover:
    def test_candidates_are_scored_and_sorted(self, net):
        items = net.discover(GUEST, need="OCR 表格提取")
        assert items
        assert [i["affinity"] for i in items] == sorted(
            [i["affinity"] for i in items], reverse=True
        )
        assert items[0]["id"] == A

    def test_friends_are_not_recommended(self, net):
        ids = {i["id"] for i in net.discover(GUEST)}
        assert HUMAN not in ids  # 已经是好友了

    def test_own_agents_are_not_recommended(self, net):
        ids = {i["id"] for i in net.discover(HUMAN)}
        assert C not in ids and A not in ids

    def test_pending_requests_are_skipped(self, net):
        net.request(GUEST, B, "想加你")
        assert B not in {i["id"] for i in net.discover(GUEST)}

    def test_blocked_members_are_skipped(self, net):
        net.block(GUEST, B)
        assert B not in {i["id"] for i in net.discover(GUEST)}

    def test_private_members_stay_hidden(self, net):
        assert C not in {i["id"] for i in net.discover(GUEST)}

    def test_every_candidate_carries_a_breakdown(self, net):
        for it in net.discover(GUEST, need="写作"):
            assert set(it["breakdown"]) == {"skill", "fof", "kind", "rejectPenalty"}
            assert "matchPercent" in it

    def test_limit_is_respected(self, net):
        assert len(net.discover(GUEST, limit=1)) == 1


# --------------------------------------------------------------------------- #
# 引荐
# --------------------------------------------------------------------------- #


class TestReferral:
    def test_only_a_mutual_friend_can_introduce(self, net):
        # A 与 GUEST 不是好友，所以 A 没资格引荐
        with pytest.raises(SocialNotPermitted):
            net.introduce(A, GUEST, C, "他很厉害")

    def test_cannot_introduce_someone_to_themselves(self, net):
        with pytest.raises(SocialError):
            net.introduce(HUMAN, C, C, "自荐")

    def test_introduction_makes_a_private_member_discoverable(self, net):
        assert net.visible_to(GUEST, C) is False
        net.introduce(HUMAN, GUEST, C, "写作很好，你正缺这个")
        assert net.visible_to(GUEST, C) is True
        assert C in {i["id"] for i in net.discover(GUEST)}

    def test_introduction_grants_no_scopes_at_all(self, net):
        net.introduce(HUMAN, GUEST, C, "推荐")
        # 引荐只是「提高可信度」，关系本身没有变
        assert net.is_friend(GUEST, C) is False
        for s in Scope:
            assert net.can(GUEST, C, s) is False

    def test_introduction_appears_in_the_inbox(self, net):
        net.introduce(HUMAN, GUEST, C, "写作很好")
        items = net.introductions(GUEST)
        assert len(items) == 1
        assert items[0]["peer"] == C
        assert items[0]["introducer"] == HUMAN
        assert items[0]["note"] == "写作很好"
        assert net.introductions(A) == []

    def test_repeated_introduction_keeps_only_the_latest(self, net):
        net.introduce(HUMAN, GUEST, C, "第一版")
        net.introduce(HUMAN, GUEST, C, "第二版")
        assert [i["note"] for i in net.introductions(GUEST)] == ["第二版"]

    def test_introduction_is_audited(self, net):
        net.introduce(HUMAN, GUEST, C, "推荐")
        actions = [a["action"] for a in net.trail(peer=C)]
        assert "introduce" in actions


# --------------------------------------------------------------------------- #
# 隐私
# --------------------------------------------------------------------------- #


class TestPrivacy:
    def test_profile_exposes_only_a_count(self, net):
        data = net.profile(A, GUEST)
        assert data["commonFriends"] == 1
        # 契约式断言：字段集固定，**没有任何**列出成员的字段
        assert set(data) == {
            "member", "isSelf", "isFriend", "commonFriends", "distance",
            "visible", "referred",
        }
        assert HUMAN not in json.dumps(
            {k: v for k, v in data.items() if k != "member"}, ensure_ascii=False
        )

    def test_profile_of_self_is_flagged(self, net):
        assert net.profile(GUEST, GUEST)["isSelf"] is True

    def test_discover_never_leaks_the_friend_list(self, net):
        for it in net.discover(GUEST):
            assert "friends" not in it
            assert isinstance(it["commonFriends"], int)


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #

TEST_MEMBERS = {
    "members": [
        {"id": HUMAN, "name": "海鱼", "kind": "human", "discoverable": "circle",
         "tokens": ["tok-h"]},
        {"id": GUEST, "name": "访客", "kind": "human", "discoverable": "public",
         "tokens": ["tok-g"]},
        {"id": "agent:echo-a", "name": "回显 A", "kind": "agent", "owner": HUMAN,
         "discoverable": "public"},
        {"id": "agent:echo-b", "name": "回显 B", "kind": "agent", "owner": HUMAN,
         "discoverable": "private"},
    ]
}

HUMAN_H = {"Authorization": "Bearer tok-h"}
GUEST_H = {"Authorization": "Bearer tok-g"}


@pytest.fixture
def dclient(tmp_path, agents_file, monkeypatch):
    from a2a_hub import config as cfg
    from a2a_hub import registry as reg

    mf = tmp_path / "members.yaml"
    mf.write_text(yaml.safe_dump(TEST_MEMBERS, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("A2A_AGENTS_FILE", str(agents_file))
    monkeypatch.setenv("A2A_STORE", "memory")
    monkeypatch.setenv("A2A_PUBLIC_URL", "http://testserver")
    monkeypatch.setenv("A2A_API_TOKEN", "")
    monkeypatch.delenv("A2A_REQUIRE_AUTH", raising=False)
    monkeypatch.setenv("A2A_MEMBERS_FILE", str(mf))
    monkeypatch.setenv("A2A_RELATIONS_FILE", str(tmp_path / "relations.json"))
    monkeypatch.setenv("A2A_SOCIAL_MODE", "strict")
    cfg._settings = None
    reg._registry = None

    from fastapi.testclient import TestClient

    import a2a_hub.server as srv

    srv.hub = srv.Hub()
    with TestClient(srv.app) as c:
        yield c
    cfg._settings = None
    reg._registry = None


class TestDiscoveryHttp:
    def test_discover_hides_private_members(self, dclient):
        r = dclient.get("/social/discover", params={"need": "代码评审"}, headers=GUEST_H)
        assert r.status_code == 200, r.text
        ids = {c["id"] for c in r.json()["candidates"]}
        assert "agent:echo-a" in ids
        assert "agent:echo-b" not in ids  # private，且没被引荐

    def test_discover_uses_registry_skills(self, dclient):
        """能力画像来自 registry（注入进去的），不是只有 bio。

        必须用 **GUEST** 视角：HUMAN 是本仓库里 echo-a/echo-b 的 owner，
        而 ``discover`` 会把「自己的 agent」排除掉，于是 HUMAN 的候选里
        只剩 GUEST（人类，没有 skills），skill 恒为 0，验不出解析器。
        """
        r = dclient.get("/social/discover", params={"need": "代码评审"}, headers=GUEST_H)
        assert r.status_code == 200, r.text
        top = r.json()["candidates"]
        assert top, r.text
        assert top[0]["id"] == "agent:echo-a"
        assert top[0]["breakdown"]["skill"] > 0  # 命中 registry 的 code-review 技能

    def test_profile_endpoint_has_no_friend_list(self, dclient):
        # GUEST 加 echo-a 为好友；HUMAN 是 echo-a 的 owner，
        # 但 owner 不是好友，所以这里先让 HUMAN↔GUEST 成为好友
        dclient.post("/social/requests", json={"to": HUMAN, "message": "想加你"},
                     headers=GUEST_H)
        dclient.post("/social/requests/accept", json={"peer": GUEST}, headers=HUMAN_H)
        dclient.post("/social/requests", json={"to": "agent:echo-a", "message": "想加你"},
                     headers=HUMAN_H)
        dclient.post("/social/requests/accept",
                     json={"peer": HUMAN, "as": "agent:echo-a"}, headers=HUMAN_H)

        r = dclient.get("/social/members/agent:echo-a", headers=GUEST_H)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["commonFriends"] == 1
        assert set(data) == {
            "member", "isSelf", "isFriend", "commonFriends", "distance",
            "visible", "referred",
        }

    def test_introduction_endpoint_round_trip(self, dclient):
        # 让 HUMAN↔GUEST 成为好友，HUMAN 才有资格引荐
        dclient.post("/social/requests", json={"to": HUMAN, "message": "想加你"},
                     headers=GUEST_H)
        dclient.post("/social/requests/accept", json={"peer": GUEST}, headers=HUMAN_H)
        r = dclient.post(
            "/social/introductions",
            json={"peer": "agent:echo-b", "to": GUEST, "note": "他写作很好"},
            headers=HUMAN_H,
        )
        assert r.status_code == 200, r.text
        # 被引荐后 private 成员才可见
        ids = {
            c["id"]
            for c in dclient.get("/social/discover", headers=GUEST_H).json()["candidates"]
        }
        assert "agent:echo-b" in ids
        box = dclient.get("/social/introductions", headers=GUEST_H).json()
        assert box["count"] == 1
        assert box["introductions"][0]["introducer"] == HUMAN

    def test_introduce_requires_422_without_target(self, dclient):
        r = dclient.post("/social/introductions", json={"peer": "agent:echo-a"},
                         headers=HUMAN_H)
        assert r.status_code == 422

    def test_introduce_by_a_stranger_is_403(self, dclient):
        r = dclient.post(
            "/social/introductions",
            json={"peer": "agent:echo-b", "to": HUMAN, "note": "x"},
            headers=GUEST_H,
        )
        assert r.status_code == 403

    def test_search_endpoint_is_viewer_aware(self, dclient):
        r = dclient.get("/social/members", headers=GUEST_H)
        ids = {m["id"] for m in r.json()["members"]}
        assert "agent:echo-a" in ids
        assert "agent:echo-b" not in ids

    def test_rpc_discover_is_available(self, dclient):
        r = dclient.post(
            "/",
            json={"jsonrpc": "2.0", "id": 1, "method": "social/discover",
                  "params": {"need": "代码评审"}},
            headers=GUEST_H,
        )
        body = r.json()
        assert "result" in body, body
        assert body["result"]["count"] >= 1

    def test_rpc_profile_is_available(self, dclient):
        r = dclient.post(
            "/",
            json={"jsonrpc": "2.0", "id": 1, "method": "social/profile",
                  "params": {"member": "agent:echo-a"}},
            headers=GUEST_H,
        )
        assert r.json()["result"]["member"]["id"] == "agent:echo-a"

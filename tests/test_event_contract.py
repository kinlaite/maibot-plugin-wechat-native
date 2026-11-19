"""锁定 Native 事件处理契约：游标去重、origin=live、message.committed。

这些规则参照上游 astrbot-plugin-wechat-native 的实现：
- 游标不大于已保存游标的重复事件直接忽略；
- 只有 origin=live 的入站消息才触发回复；
- 非消息事件直接推进游标。
"""

from typing import Any, Dict, List
from unittest.mock import Mock

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wechat_native import plugin as plugin_module  # noqa: E402


def _now_iso() -> str:
    """返回当前 UTC 时间，保证消息落在可回复的时间窗口内。

    Returns:
        str: ISO 8601 时间字符串。
    """

    return datetime.now(timezone.utc).isoformat()


class FakeClient:
    """按 messageId 返回固定消息的客户端替身。"""

    def __init__(self, message: Dict[str, Any]) -> None:
        self._message = message
        self.requested: List[str] = []

    async def message(self, message_id: str) -> Dict[str, Any]:
        self.requested.append(message_id)
        return self._message

    async def group(self, thread):
        return {"id": thread, "name": "测试群"}

    async def group_members(self, thread, *, limit, cursor="", search=""):
        return {"items": [{"username": "wxid_friend", "displayName": "好友甲"}]}

    async def contacts(self, *, limit, kind="", search="", query="", cursor=""):
        return {"items": [{"username": "wxid_friend", "displayName": "好友甲"}]}


class FakeStore:
    """记录游标与 accept 调用的状态库替身。"""

    def __init__(self, cursor: str = "0") -> None:
        self.cursor = cursor
        self.accepted: List[tuple] = []
        self.advanced: List[str] = []
        self.status: Dict[str, str] = {}

    def accept(self, cursor, message_id, thread, server_id, status, *, received=True):
        self.accepted.append((cursor, message_id, status, received))
        self.advanced.append(cursor)
        self.cursor = cursor
        self.status[message_id] = status
        return received

    def advance(self, cursor: str) -> None:
        self.advanced.append(cursor)
        self.cursor = cursor

    def message_status(self, message_id: str):
        return self.status.get(message_id)

    def mark_message(self, message_id: str, status: str) -> None:
        self.status[message_id] = status

    def recall(self, thread, server_id, message_id) -> None:
        pass


MESSAGE = {
    "id": "msg-abc",
    "accountId": "acct",
    "threadId": "wxid_friend",
    "senderId": "wxid_friend",
    "body": "你好",
    "type": 1,
    "direction": "incoming",
    "createdAt": _now_iso(),
    "serverId": "123456789",
    "media": [],
}


def _plugin(client: FakeClient) -> Any:
    instance = plugin_module.WeChatNativePlugin()
    ctx = Mock()
    instance._set_context(ctx)
    instance._client = client
    instance._self_id = "wxid_bot"
    instance._native = Mock()
    instance._native.account_id = "acct"
    instance._native.enable_media = False
    instance._native.enable_private_reply = True
    instance._native.allowed_users = frozenset()
    instance._native.allowed_groups = frozenset()
    instance._native.group_reply_mode = "enabled"
    instance._pending_source = {}
    return instance


def _event(cursor: str, origin: str = "live", kind: str = "message.committed", **extra: Any) -> Dict[str, Any]:
    data = {"messageId": "msg-abc", "threadId": "wxid_friend", "origin": origin, **extra}
    return {"cursor": cursor, "kind": kind, "data": data}


def test_live_incoming_message_is_routed() -> None:
    client = FakeClient(MESSAGE)
    instance = _plugin(client)
    store = FakeStore()
    routed: List[Dict[str, Any]] = []

    async def route_message(gateway_name, message, **kwargs):
        routed.append(message)
        return True

    instance.ctx.gateway = Mock()
    instance.ctx.gateway.route_message = route_message
    asyncio.run(instance._handle_event(_event("10"), store))
    assert client.requested == ["msg-abc"]
    assert len(routed) == 1
    assert store.cursor == "10"


def test_historical_origin_is_not_routed() -> None:
    """补同步的历史消息（origin 非 live）不应触发回复。"""

    client = FakeClient(MESSAGE)
    instance = _plugin(client)
    store = FakeStore()
    instance.ctx.gateway = Mock()
    instance.ctx.gateway.route_message = Mock(side_effect=AssertionError("不应路由历史消息"))
    asyncio.run(instance._handle_event(_event("11", origin="history"), store))
    assert store.cursor == "11"
    assert store.accepted and store.accepted[0][3] is False


def test_historical_message_does_not_fetch_details() -> None:
    """历史消息不应逐条请求详情：游标重置后会重放几千条，逐条请求会拖垮处理。"""

    client = FakeClient(MESSAGE)
    instance = _plugin(client)
    store = FakeStore()
    instance.ctx.gateway = Mock()
    instance.ctx.gateway.route_message = Mock(side_effect=AssertionError("不应路由历史消息"))
    asyncio.run(instance._handle_event(_event("12", origin="history"), store))
    assert client.requested == []


def test_repeated_historical_message_does_not_conflict() -> None:
    """同一条历史消息被重复投递时不能触发身份冲突。

    状态库用 ``账号:会话:原生ID`` 复合 ID，历史分支写入的 server_id 为空；
    已记录的消息再次出现时必须只推进游标，而不是判成身份复用。
    """

    client = FakeClient(MESSAGE)
    instance = _plugin(client)
    store = FakeStore()
    instance.ctx.gateway = Mock()
    instance.ctx.gateway.route_message = Mock(side_effect=AssertionError("不应路由历史消息"))
    asyncio.run(instance._handle_event(_event("13", origin="history"), store))
    accepted_after_first = len(store.accepted)
    asyncio.run(instance._handle_event(_event("14", origin="history"), store))
    # 第二次只推进游标，不再写入新身份。
    assert len(store.accepted) == accepted_after_first
    assert store.cursor == "14"


def test_duplicate_cursor_is_skipped() -> None:
    client = FakeClient(MESSAGE)
    instance = _plugin(client)
    store = FakeStore(cursor="20")
    instance.ctx.gateway = Mock()
    instance.ctx.gateway.route_message = Mock(side_effect=AssertionError("重复游标不应处理"))
    asyncio.run(instance._handle_event(_event("20"), store))
    assert client.requested == []


def test_non_message_event_advances_cursor() -> None:
    client = FakeClient(MESSAGE)
    instance = _plugin(client)
    store = FakeStore()
    for index, kind in enumerate(("sync.received", "operation.updated", "job.updated"), start=30):
        asyncio.run(instance._handle_event({"cursor": str(index), "kind": kind, "data": {}}, store))
    assert store.advanced == ["30", "31", "32"]
    assert client.requested == []


def test_outgoing_origin_live_is_ignored() -> None:
    message = {**MESSAGE, "direction": "outgoing"}
    client = FakeClient(message)
    instance = _plugin(client)
    store = FakeStore()
    instance.ctx.gateway = Mock()
    instance.ctx.gateway.route_message = Mock(side_effect=AssertionError("不应路由自发消息"))
    asyncio.run(instance._handle_event(_event("40"), store))
    assert store.cursor == "40"


def test_missing_message_id_advances_cursor() -> None:
    client = FakeClient(MESSAGE)
    instance = _plugin(client)
    store = FakeStore()
    event = {"cursor": "50", "kind": "message.committed", "data": {"origin": "live"}}
    asyncio.run(instance._handle_event(event, store))
    assert store.cursor == "50"
    assert client.requested == []

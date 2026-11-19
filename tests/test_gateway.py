"""WeChat Native 插件的网关行为测试。

用假客户端与假宿主验证过滤规则、状态标记和出站调用顺序。
"""

from typing import Any, Dict, List
from unittest.mock import Mock

import os
import sys
from datetime import datetime, timezone


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wechat_native import plugin as plugin_module  # noqa: E402
from wechat_native.config import NativeConfig  # noqa: E402


class FakeClient:
    """记录出站调用的 Native 客户端替身。"""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def send_text(self, thread, text, key, mentions=None, *, quote=None):
        self.calls.append(("text", thread, text, mentions, quote))
        return {"id": "send-1", "localMessageId": "msg_native_1", "state": "accepted"}

    async def send_image(self, thread, data, content_type, key, **kwargs):
        self.calls.append(("image", thread, content_type, len(data)))
        return {"id": "send-2", "localMessageId": "msg_native_2", "state": "accepted"}

    async def send_voice(self, thread, data, content_type, key, **kwargs):
        self.calls.append(("voice", thread, content_type, len(data)))
        return {"id": "send-3", "localMessageId": "msg_native_3", "state": "accepted"}

    async def group(self, thread):
        self.calls.append(("group", thread))
        return {"id": thread, "name": "测试群"}

    async def group_members(self, thread, *, limit, cursor="", search=""):
        self.calls.append(("members", thread, limit, search))
        return {"items": [{"username": "wxid_member", "displayName": "成员甲"}]}

    async def contacts(self, *, limit, kind="", search="", query="", cursor=""):
        self.calls.append(("contacts", limit, kind, query))
        return {"items": [{"username": "wxid_friend", "displayName": "好友甲"}]}


class FakeGateway:
    """记录入站路由的宿主网关替身。"""

    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.messages: List[Dict[str, Any]] = []

    async def route_message(self, gateway_name, message, *, external_message_id="", dedupe_key=""):
        self.messages.append(message)
        return self.accept

    async def update_state(self, gateway_name, *, ready, **kwargs):
        return True


def _config(**overrides: Any) -> NativeConfig:
    values = {
        "api_url": "http://127.0.0.1:6174/v1",
        "account_id": "acct",
        "token": "token",
        "token_file": "",
        "enable_private_reply": True,
        "enable_media": False,
        "enable_stickers": False,
        "allowed_users": frozenset(),
        "allowed_groups": frozenset(),
        "group_reply_mode": "enabled",
        "group_reply_style": "quote",
        "reconnect_delay_seconds": 3,
        "max_image_bytes": 10 * 1024 * 1024,
        "admin_token": "",
        "admin_token_file": "",
        "enable_history_tools": False,
        "enable_contact_search": False,
        "enable_wechat_actions": False,
        "enable_friend_requests": False,
        "wechat_action_admins": frozenset(),
    }
    values.update(overrides)
    return NativeConfig(**values)


def _build(store, gateway, client, native) -> Any:
    instance = plugin_module.WeChatNativePlugin()
    ctx = Mock()
    ctx.gateway = gateway
    ctx.logger = Mock()
    ctx.paths = Mock()
    instance._set_context(ctx)
    instance._store = store
    instance._client = client
    instance._self_id = "wxid_bot"
    instance._native = native
    instance._ready = True
    return instance


def _raw(**overrides: Any) -> Dict[str, Any]:
    raw = {
        "id": "m1",
        "threadId": "wxid_friend",
        "senderId": "wxid_friend",
        "body": "你好",
        "type": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    raw.update(overrides)
    return raw


def test_private_message_is_routed(tmp_path) -> None:
    store = plugin_module.StateStore(tmp_path / "state.db")
    gateway = FakeGateway()
    instance = _build(store, gateway, FakeClient(), _config())
    plugin_module.asyncio.run(instance._receive_message(_raw(), store))
    assert len(gateway.messages) == 1
    assert gateway.messages[0]["raw_message"][0]["data"] == "你好"
    assert store.message_status("acct:wxid_friend:m1") == "queued"


def test_group_message_without_mention_is_routed(tmp_path) -> None:
    """群消息即使没 @ 也要交给宿主，由宿主的回复策略决定。"""

    store = plugin_module.StateStore(tmp_path / "state.db")
    gateway = FakeGateway()
    instance = _build(store, gateway, FakeClient(), _config())
    raw = _raw(threadId="1@chatroom", senderId="wxid_member")
    plugin_module.asyncio.run(instance._receive_message(raw, store))
    assert len(gateway.messages) == 1


def test_group_message_routed_when_mentioned(tmp_path) -> None:
    store = plugin_module.StateStore(tmp_path / "state.db")
    gateway = FakeGateway()
    instance = _build(store, gateway, FakeClient(), _config())
    raw = _raw(
        threadId="1@chatroom",
        senderId="wxid_member",
        body="@麦麦\u2005 在吗",
        mentions={"state": "known", "self": True},
    )
    plugin_module.asyncio.run(instance._receive_message(raw, store))
    assert len(gateway.messages) == 1
    assert gateway.messages[0]["message_info"]["group_info"]["group_id"] == "1@chatroom"
    assert gateway.messages[0]["raw_message"][0]["data"] == "在吗"


def test_disallowed_user_is_ignored(tmp_path) -> None:
    store = plugin_module.StateStore(tmp_path / "state.db")
    gateway = FakeGateway()
    instance = _build(store, gateway, FakeClient(), _config(allowed_users=frozenset({"wxid_other"})))
    plugin_module.asyncio.run(instance._receive_message(_raw(), store))
    assert gateway.messages == []


def test_duplicate_message_is_skipped(tmp_path) -> None:
    store = plugin_module.StateStore(tmp_path / "state.db")
    gateway = FakeGateway()
    instance = _build(store, gateway, FakeClient(), _config())
    plugin_module.asyncio.run(instance._receive_message(_raw(), store))
    plugin_module.asyncio.run(instance._receive_message(_raw(), store))
    assert len(gateway.messages) == 1


def test_self_message_is_skipped(tmp_path) -> None:
    store = plugin_module.StateStore(tmp_path / "state.db")
    gateway = FakeGateway()
    instance = _build(store, gateway, FakeClient(), _config())
    plugin_module.asyncio.run(instance._receive_message(_raw(senderId="wxid_bot"), store))
    assert gateway.messages == []


def test_outbound_sends_text_then_marks_replied(tmp_path) -> None:
    store = plugin_module.StateStore(tmp_path / "state.db")
    client = FakeClient()
    instance = _build(store, FakeGateway(), client, _config())
    plugin_module.asyncio.run(instance._receive_message(_raw(), store))
    message = {
        "platform": "wechat_native",
        "account_id": "acct",
        "message_info": {
            "user_info": {"user_id": "wxid_bot", "user_nickname": "bot"},
            "additional_config": {"platform_io_target_user_id": "wxid_friend"},
        },
        "raw_message": [{"type": "text", "data": "你好呀"}],
    }
    result = plugin_module.asyncio.run(instance.send_to_platform(message))
    assert result["success"] is True
    sends = [call for call in client.calls if call[0] == "text"]
    assert sends[0][:3] == ("text", "wxid_friend", "你好呀")
    # 宿主用这个 ID 回写消息记录，必须是 Native 真实消息 ID 而非幂等键。
    assert result["external_message_id"] == "msg_native_1"
    assert store.message_status("acct:wxid_friend:m1") == "replied"


def test_outbound_refuses_when_not_ready(tmp_path) -> None:
    store = plugin_module.StateStore(tmp_path / "state.db")
    instance = _build(store, FakeGateway(), FakeClient(), _config())
    instance._ready = False
    result = plugin_module.asyncio.run(
        instance.send_to_platform(
            {"platform": "wechat_native", "account_id": "acct", "message_info": {}, "raw_message": []}
        )
    )
    assert result["success"] is False


class ComposedNameClient:
    """群资料里没有群名、成员含机器人自己的客户端替身。"""

    def __init__(self) -> None:
        self.self_id = "wxid_bot"

    async def group(self, thread):
        # Native 对未同步群名的群返回空 name，displayName 就是 chatroom ID。
        return {"id": thread, "name": "", "displayName": thread}

    async def group_members(self, thread, *, limit, cursor="", search=""):
        return {
            "items": [
                {"username": "wxid_bot", "displayName": "麦麦92", "roomDisplayName": "麦麦92"},
                {"username": "wxid_a", "displayName": "等我", "roomDisplayName": "等我"},
                {"username": "wxid_b", "displayName": "乐安", "roomDisplayName": "乐安"},
            ]
        }


def test_composed_group_name_excludes_bot_itself(tmp_path) -> None:
    """群名缺失时用成员拼名，但不能把机器人自己的群昵称算进去。"""

    store = plugin_module.StateStore(tmp_path / "state.db")
    client = ComposedNameClient()
    instance = _build(store, FakeGateway(), client, _config())
    name = plugin_module.asyncio.run(instance._group_name(client, "57753723704@chatroom"))
    assert name == "等我、乐安"


def test_member_name_cache_is_scoped_per_group(tmp_path) -> None:
    """同一个人在不同群的群昵称不同，缓存不能跨群复用。"""

    class TwoGroupClient:
        async def group_members(self, thread, *, limit, cursor="", search=""):
            display = "甲群昵称" if thread == "1@chatroom" else "乙群昵称"
            return {"items": [{"username": "wxid_member", "roomDisplayName": display}]}

        async def contacts(self, *, limit, kind="", search="", query="", cursor=""):
            return {"items": []}

    store = plugin_module.StateStore(tmp_path / "state.db")
    client = TwoGroupClient()
    instance = _build(store, FakeGateway(), client, _config())
    first = plugin_module.asyncio.run(instance._member_name(client, "1@chatroom", "wxid_member"))
    second = plugin_module.asyncio.run(instance._member_name(client, "2@chatroom", "wxid_member"))
    assert first == "甲群昵称"
    assert second == "乙群昵称"

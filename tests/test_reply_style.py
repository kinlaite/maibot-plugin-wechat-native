"""群聊回复方式（引用 / @ / 直接发送）的行为测试。"""

from typing import Any, Dict, List
from unittest.mock import Mock

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wechat_native import plugin as plugin_module  # noqa: E402
from wechat_native.config import NativeConfig  # noqa: E402

GROUP = "12345@chatroom"


class FakeClient:
    """记录出站调用的 Native 客户端替身。"""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def send_text(self, thread, text, key, mentions=None, *, quote=None):
        self.calls.append(("text", text, mentions, quote))


def _config(style: str, **overrides: Any) -> NativeConfig:
    values = {
        "api_url": "http://127.0.0.1:6174/v1",
        "account_id": "acct",
        "token": "token",
        "token_file": "",
        "admin_token": "",
        "admin_token_file": "",
        "enable_private_reply": True,
        "enable_media": False,
        "enable_stickers": False,
        "allowed_users": frozenset(),
        "allowed_groups": frozenset(),
        "group_reply_mode": "enabled",
        "group_reply_style": style,
        "reconnect_delay_seconds": 3,
        "max_image_bytes": 10 * 1024 * 1024,
        "enable_history_tools": False,
        "enable_contact_search": False,
        "enable_wechat_actions": False,
        "enable_friend_requests": False,
        "wechat_action_admins": frozenset(),
    }
    values.update(overrides)
    return NativeConfig(**values)


def _plugin(style: str, source: Dict[str, str]) -> Any:
    instance = plugin_module.WeChatNativePlugin()
    ctx = Mock()
    instance._set_context(ctx)
    instance._native = _config(style)
    instance._self_id = "wxid_bot"
    instance._pending_source = {"acct:" + GROUP + ":m1": source}
    return instance


SOURCE = {"threadId": GROUP, "senderId": "wxid_member", "messageId": "m1", "serverId": "555"}


def test_quote_style_adds_reference() -> None:
    instance = _plugin("quote", SOURCE)
    parts = [("text", "好的")]
    mentions, reply_id, quote = __import__("asyncio").run(
        instance._apply_reply_style(instance._native, FakeClient(), GROUP, parts, SOURCE, [], None)
    )
    assert reply_id == "m1"
    assert quote == {"messageId": "m1", "serverId": "555"}
    assert mentions == []


def test_mention_style_targets_sender() -> None:
    instance = _plugin("mention", SOURCE)
    parts = [("text", "好的")]
    mentions, reply_id, quote = __import__("asyncio").run(
        instance._apply_reply_style(instance._native, FakeClient(), GROUP, parts, SOURCE, [], None)
    )
    assert mentions == ["wxid_member"]
    assert quote is None


def test_plain_style_adds_nothing() -> None:
    instance = _plugin("plain", SOURCE)
    parts = [("text", "好的")]
    mentions, reply_id, quote = __import__("asyncio").run(
        instance._apply_reply_style(instance._native, FakeClient(), GROUP, parts, SOURCE, [], None)
    )
    assert mentions == []
    assert quote is None


def test_private_chat_never_adds_reference() -> None:
    instance = _plugin("quote", SOURCE)
    parts = [("text", "好的")]
    mentions, reply_id, quote = __import__("asyncio").run(
        instance._apply_reply_style(instance._native, FakeClient(), "wxid_friend", parts, SOURCE, [], None)
    )
    assert quote is None
    assert mentions == []


def test_media_only_reply_skips_style() -> None:
    instance = _plugin("quote", SOURCE)
    parts = [("image", b"data")]
    mentions, reply_id, quote = __import__("asyncio").run(
        instance._apply_reply_style(instance._native, FakeClient(), GROUP, parts, SOURCE, [], None)
    )
    assert quote is None


def test_explicit_mention_is_preserved() -> None:
    instance = _plugin("quote", SOURCE)
    parts = [("text", "@某人 好的")]
    mentions, reply_id, quote = __import__("asyncio").run(
        instance._apply_reply_style(instance._native, FakeClient(), GROUP, parts, SOURCE, ["wxid_other"], None)
    )
    assert mentions == ["wxid_other"]
    assert quote is None


def test_missing_server_id_falls_back_to_plain() -> None:
    instance = _plugin("quote", {**SOURCE, "serverId": ""})
    parts = [("text", "好的")]
    mentions, reply_id, quote = __import__("asyncio").run(
        instance._apply_reply_style(instance._native, FakeClient(), GROUP, parts, {**SOURCE, "serverId": ""}, [], None)
    )
    assert quote is None


def test_reply_source_prefers_host_reply_to_over_latest() -> None:
    """群聊里多人并发发言时，必须按宿主的 reply_to 定位触发消息。

    只按会话取「最新一条」会引用到别人刚发的消息上。
    """

    instance = plugin_module.WeChatNativePlugin()
    instance._set_context(Mock())
    instance._self_id = "wxid_bot"
    first_key = "acct:" + GROUP + ":m1"
    second_key = "acct:" + GROUP + ":m2"
    instance._pending_source = {
        first_key: {"threadId": GROUP, "senderId": "wxid_a", "messageId": "m1", "serverId": "1"},
        second_key: {"threadId": GROUP, "senderId": "wxid_b", "messageId": "m2", "serverId": "2"},
    }
    outbound = {
        "message_info": {"group_info": {"group_id": GROUP}},
        "reply_to": "m1",
    }
    assert instance._reply_source_key(outbound) == first_key


def test_reply_source_matches_composite_message_id() -> None:
    """宿主也可能把 reply_to 写成完整的复合消息 ID。"""

    instance = plugin_module.WeChatNativePlugin()
    instance._set_context(Mock())
    instance._self_id = "wxid_bot"
    key = "acct:" + GROUP + ":m1"
    instance._pending_source = {
        key: {"threadId": GROUP, "senderId": "wxid_a", "messageId": "m1", "serverId": "1"},
    }
    outbound = {
        "message_info": {"group_info": {"group_id": GROUP}},
        "reply_to": key,
    }
    assert instance._reply_source_key(outbound) == key

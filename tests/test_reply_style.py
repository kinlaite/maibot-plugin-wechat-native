"""出站 @ / 引用的翻译测试。

适配器只把宿主已经给出的 at / reply 段翻成 Native 参数，不按自己的策略
补引用或补 @：那会让微信里发出的消息和模型实际写的内容不一致。
"""

from typing import Any, Dict, List
from unittest.mock import Mock

import asyncio
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


def _config(**overrides: Any) -> NativeConfig:
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


def _plugin(source: Dict[str, str]) -> Any:
    instance = plugin_module.WeChatNativePlugin()
    ctx = Mock()
    instance._set_context(ctx)
    instance._native = _config()
    instance._self_id = "wxid_bot"
    instance._pending_source = {"acct:" + GROUP + ":m1": source}
    return instance


SOURCE = {"threadId": GROUP, "senderId": "wxid_member", "messageId": "m1", "serverId": "555"}


def test_host_reply_request_becomes_native_quote() -> None:
    """宿主给了 reply 段就引用，配对信息取自触发消息的来源。"""

    instance = _plugin(SOURCE)
    parts = [("text", "好的")]
    mentions, reply_id, quote = asyncio.run(
        instance._apply_reply_style(GROUP, SOURCE, [], "host-msg-id")
    )
    assert mentions == []
    assert reply_id == SOURCE["messageId"]
    assert quote == {"messageId": SOURCE["messageId"], "serverId": SOURCE["serverId"]}


def test_no_reply_request_means_no_quote() -> None:
    """宿主没要求引用时不能自己补一个：那是在改写模型发出的消息。"""

    instance = _plugin(SOURCE)
    parts = [("text", "好的")]
    mentions, reply_id, quote = asyncio.run(
        instance._apply_reply_style(GROUP, SOURCE, [], None)
    )
    assert mentions == []
    assert reply_id is None
    assert quote is None


def test_host_mentions_are_preserved() -> None:
    instance = _plugin(SOURCE)
    parts = [("text", "好的")]
    mentions, reply_id, quote = asyncio.run(
        instance._apply_reply_style(GROUP, SOURCE, ["wxid_other"], None)
    )
    assert mentions == ["wxid_other"]
    assert quote is None


def test_no_implicit_mention_of_trigger_sender() -> None:
    """没有 at 段时不能自动 @ 触发者。"""

    instance = _plugin(SOURCE)
    parts = [("text", "好的")]
    mentions, _, _ = asyncio.run(
        instance._apply_reply_style(GROUP, SOURCE, [], None)
    )
    assert mentions == []


def test_private_chat_never_quotes() -> None:
    instance = _plugin(SOURCE)
    parts = [("text", "好的")]
    mentions, reply_id, quote = asyncio.run(
        instance._apply_reply_style("wxid_friend", SOURCE, [], "host-msg-id")
    )
    assert quote is None
    assert mentions == []


def test_missing_server_id_keeps_reply_id_without_quote() -> None:
    """拼不出 Native 引用参数时只保留 reply_id，不能凭空构造引用。"""

    source = {**SOURCE, "serverId": ""}
    instance = _plugin(source)
    parts = [("text", "好的")]
    mentions, reply_id, quote = asyncio.run(
        instance._apply_reply_style(GROUP, source, [], "host-msg-id")
    )
    assert quote is None
    assert reply_id == "host-msg-id"


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

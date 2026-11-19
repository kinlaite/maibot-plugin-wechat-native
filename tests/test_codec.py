"""WeChat Native 适配器的本地契约测试。

用假 Native 客户端验证消息编解码、过滤与出站顺序，不访问真实微信。
"""

from typing import Any, Dict, List

import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wechat_native.codec import _split_text, inbound, outbound  # noqa: E402
from wechat_native.config import MAX_TEXT_BYTES  # noqa: E402


class FakeClient:
    """记录出站调用顺序的 Native 客户端替身。"""

    def __init__(self) -> None:
        self.calls: List[tuple[str, Any]] = []

    async def send_text(self, thread, text, key, mentions=None, *, quote=None):
        self.calls.append(("text", thread, text, mentions, quote))

    async def send_image(self, thread, data, content_type, key, **kwargs):
        self.calls.append(("image", thread, content_type, len(data)))

    async def send_voice(self, thread, data, content_type, key, **kwargs):
        self.calls.append(("voice", thread, content_type, len(data)))


def _raw(**overrides: Any) -> Dict[str, Any]:
    raw = {
        "id": "msg-1",
        "threadId": "wxid_friend",
        "senderId": "wxid_friend",
        "body": "你好",
        "type": 1,
        "createdAt": "2026-01-01T00:00:00Z",
    }
    raw.update(overrides)
    return raw


def test_private_message_shape() -> None:
    message = inbound(_raw(), "acct", text="你好", segments=[], self_id="wxid_bot")
    assert message["platform"] == "wechat_native"
    assert message["account_id"] == "acct"
    assert message["message_id"] == "acct:wxid_friend:msg-1"
    assert message["message_info"]["user_info"]["user_id"] == "wxid_friend"
    assert "group_info" not in message["message_info"]
    assert message["raw_message"] == [{"type": "text", "data": "你好"}]


def test_group_message_carries_group_info() -> None:
    raw = _raw(threadId="12345@chatroom", senderId="wxid_member")
    message = inbound(raw, "acct", text="早", segments=[], self_id="wxid_bot", group_name="测试群")
    group = message["message_info"]["group_info"]
    assert group["group_id"] == "12345@chatroom"
    assert group["group_name"] == "测试群"


def test_mention_prefix_is_stripped() -> None:
    raw = _raw(threadId="12345@chatroom", body="@麦麦\u2005 早", mentions={"state": "known", "self": True})
    message = inbound(raw, "acct", text="@麦麦\u2005 早", segments=[], self_id="wxid_bot")
    assert message["raw_message"][0]["data"] == "早"


def test_quote_is_prefixed_to_text() -> None:
    raw = _raw(type=49)
    message = inbound(
        raw,
        "acct",
        text="看这个",
        segments=[],
        self_id="wxid_bot",
        quote={"text": "原消息", "serverId": "s-1"},
    )
    assert message["raw_message"][0]["data"] == "[引用消息] 原消息\n看这个"


def test_outbound_preserves_order_and_mentions() -> None:
    image = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()
    message = {
        "platform": "wechat_native",
        "account_id": "acct",
        "message_info": {
            "group_info": {"group_id": "12345@chatroom", "group_name": "群"},
            "additional_config": {},
        },
        "raw_message": [
            {"type": "text", "data": "看图"},
            {"type": "image", "data": "[图片]", "binary_data_base64": image},
            {"type": "at", "target_user_id": "wxid_member"},
            {"type": "reply", "target_message_id": "msg-2"},
        ],
    }
    target, parts, mentions, reply_id = outbound(message, allow_group=True, max_image_bytes=1024)
    assert target == "12345@chatroom"
    assert [part[0] for part in parts] == ["text", "image"]
    assert mentions == ["wxid_member"]
    assert reply_id == "msg-2"


def test_outbound_rejects_group_when_disabled() -> None:
    message = {
        "platform": "wechat_native",
        "account_id": "acct",
        "message_info": {"group_info": {"group_id": "1@chatroom", "group_name": "群"}, "additional_config": {}},
        "raw_message": [{"type": "text", "data": "hi"}],
    }
    with pytest.raises(ValueError):
        outbound(message, allow_group=False, max_image_bytes=1024)


def test_outbound_private_uses_target_user() -> None:
    message = {
        "platform": "wechat_native",
        "account_id": "acct",
        "message_info": {
            "user_info": {"user_id": "wxid_bot", "user_nickname": "bot"},
            "additional_config": {"platform_io_target_user_id": "wxid_friend"},
        },
        "raw_message": [{"type": "text", "data": "hi"}],
    }
    target, parts, mentions, reply_id = outbound(message, allow_group=True, max_image_bytes=1024)
    assert target == "wxid_friend"
    assert mentions == []
    assert reply_id is None


def test_outbound_rejects_unsupported_segment() -> None:
    message = {
        "platform": "wechat_native",
        "account_id": "acct",
        "message_info": {"additional_config": {"platform_io_target_user_id": "wxid_friend"}},
        "raw_message": [{"type": "file", "data": "x"}],
    }
    with pytest.raises(ValueError):
        outbound(message, allow_group=True, max_image_bytes=1024)


def test_split_text_keeps_content_and_respects_limit() -> None:
    """超长文本要切成多段，且不能切断多字节字符或丢内容。"""

    text = "这是长文本分段测试。" * 1500
    chunks = _split_text(text)
    assert len(chunks) > 1
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-8")) <= MAX_TEXT_BYTES for chunk in chunks)


def test_split_text_handles_emoji_without_corruption() -> None:
    """emoji 是 4 字节字符，切分不能落在字符中间。"""

    text = "😀" * 6000
    chunks = _split_text(text)
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-8")) <= MAX_TEXT_BYTES for chunk in chunks)
    assert all("\ufffd" not in chunk for chunk in chunks)


def test_split_text_leaves_short_text_untouched() -> None:
    assert _split_text("你好") == ["你好"]


def test_outbound_splits_long_text_into_multiple_parts() -> None:
    """出站阶段就要分段，否则整条消息会被 Native 判失败。"""

    message = {
        "platform": "wechat_native",
        "account_id": "acct",
        "message_info": {"additional_config": {"platform_io_target_user_id": "wxid_friend"}},
        "raw_message": [{"type": "text", "data": "测" * 12000}],
    }
    _, parts, _, _ = outbound(message, allow_group=True, max_image_bytes=1024)
    assert len(parts) > 1
    assert all(kind == "text" for kind, _ in parts)
    assert "".join(content for _, content in parts) == "测" * 12000

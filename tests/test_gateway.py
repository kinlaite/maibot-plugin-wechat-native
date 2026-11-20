"""WeChat Native 插件的网关行为测试。

用假客户端与假宿主验证过滤规则、状态标记和出站调用顺序。
"""

from typing import Any, Dict, List
from unittest.mock import Mock

import base64
import hashlib
import os
import sys
from datetime import datetime, timezone


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wechat_native import plugin as plugin_module  # noqa: E402
from wechat_native.codec import segments_from_media as plugin_module_segments_from_media  # noqa: E402
from wechat_native.config import NativeConfig  # noqa: E402


class FakeClient:
    """记录出站调用的 Native 客户端替身。"""

    def __init__(self, account: Dict[str, Any] | None = None) -> None:
        self.calls: List[tuple] = []
        self.account_payload = account if account is not None else {
            "id": "acct",
            "ownUsername": "wxid_bot",
            "authenticated": True,
            "connection": "online",
        }
        self.account_checks = 0

    async def account(self):
        self.account_checks += 1
        return dict(self.account_payload)

    async def media_assets(self, message_id: str):
        self.calls.append(("media_assets", message_id))
        return {"items": [{"id": "asset_1", "kind": "sticker", "state": "available", "source": "message"}]}

    async def media_content(self, asset_id: str, **kwargs):
        self.calls.append(("media_content", asset_id, kwargs))
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "image/png"

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


def test_account_connection_event_refreshes_ready_state(tmp_path) -> None:
    """登录成功后 Native 只发 account.connection 事件。

    事件流稳定时主循环不会再检查账号，若不处理该事件，
    网关会一直停在“等待微信登录”，入站消息全部被宿主拒收。
    """

    store = plugin_module.StateStore(tmp_path / "state.db")
    store.advance("10")
    gateway = FakeGateway()
    client = FakeClient(
        {
            "id": "acct",
            "ownUsername": "wxid_bot",
            "authenticated": False,
            "connection": "disconnected",
        }
    )
    instance = _build(store, gateway, client, _config())
    instance._ready = False

    event = {"kind": "account.connection", "cursor": "11", "data": {"state": "auth_required"}}
    plugin_module.asyncio.run(instance._handle_event(event, store))
    assert instance._ready is False
    assert client.account_checks == 1
    assert store.cursor == "11"

    # 扫码完成后 Native 变为在线，再推一条连接事件。
    client.account_payload = {
        "id": "acct",
        "ownUsername": "wxid_bot",
        "authenticated": True,
        "connection": "online",
    }
    event = {"kind": "account.connection", "cursor": "12", "data": {"state": "online"}}
    plugin_module.asyncio.run(instance._handle_event(event, store))

    assert instance._ready is True
    assert instance._self_id == "wxid_bot"
    assert store.cursor == "12"


def test_animated_sticker_bytes_are_passed_through(tmp_path) -> None:
    """表情字节原样透传，GIF 抽帧交给宿主的 emoji_manager。

    适配器不再自行转码：重复转码既改变原始字节，也和宿主的表情处理链路冲突。
    """

    from io import BytesIO

    from PIL import Image

    frames = []
    for color in ((255, 0, 0), (0, 255, 0)):
        frames.append(Image.new("RGB", (32, 32), color))
    buffer = BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    gif_bytes = buffer.getvalue()
    assert gif_bytes[:3] == b"GIF"

    segments = plugin_module_segments_from_media([], None, stickers=[gif_bytes])
    emoji = segments[0]
    assert emoji["type"] == "emoji"

    decoded = base64.b64decode(emoji["binary_data_base64"])
    assert decoded == gif_bytes
    assert emoji["hash"] == hashlib.sha256(gif_bytes).hexdigest()


def test_static_sticker_is_sent_unchanged(tmp_path) -> None:
    """静态表情不需要重新编码，字节要保持一致。"""

    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (16, 16), (0, 0, 255)).save(buffer, format="PNG")
    png_bytes = buffer.getvalue()

    segments = plugin_module_segments_from_media([], None, stickers=[png_bytes])
    decoded = base64.b64decode(segments[0]["binary_data_base64"])
    assert decoded == png_bytes


def test_sticker_message_without_media_enabled_keeps_original_body(tmp_path) -> None:
    """type=47 的表情消息正文是 null，适配器不能自己编一句正文。

    媒体开关关闭时确实没有内容可交给宿主，但补占位等于伪造用户发言；
    正文由宿主按消息类型自行呈现。
    """

    store = plugin_module.StateStore(tmp_path / "state.db")
    gateway = FakeGateway()
    instance = _build(store, gateway, FakeClient(), _config(enable_media=False))
    raw = _raw(type=47, body=None)

    plugin_module.asyncio.run(instance._receive_message(raw, store))

    assert len(gateway.messages) == 1
    assert gateway.messages[0]["raw_message"] == []


def test_sticker_asset_marks_message_even_when_stickers_disabled(tmp_path) -> None:
    """关闭表情读取时仍要识别出表情，但不把表情当成普通图片。"""

    store = plugin_module.StateStore(tmp_path / "state.db")
    gateway = FakeGateway()
    client = FakeClient()
    instance = _build(store, gateway, client, _config(enable_media=True, enable_stickers=False))
    raw = _raw(type=47, body=None)

    plugin_module.asyncio.run(instance._receive_message(raw, store))

    assert len(gateway.messages) == 1
    message = gateway.messages[0]
    assert message["is_emoji"] is True
    assert message["raw_message"] == []
    # 关闭表情读取时，表情不应该作为图片附件发给模型。
    assert not any(call[0] == "media_content" for call in client.calls)


def test_sticker_asset_is_read_as_image_when_enabled(tmp_path) -> None:
    """开启表情读取后，表情应当以 emoji 段发给模型。

    对齐 QQ 适配器：表情用 ``type: "emoji"`` 而不是 ``"image"``。
    宿主据此生成 EmojiComponent，视觉链路才会把表情当图片读给模型。
    """

    store = plugin_module.StateStore(tmp_path / "state.db")
    gateway = FakeGateway()
    client = FakeClient()
    instance = _build(store, gateway, client, _config(enable_media=True, enable_stickers=True))
    raw = _raw(type=47, body=None)

    plugin_module.asyncio.run(instance._receive_message(raw, store))

    assert len(gateway.messages) == 1
    segments = gateway.messages[0]["raw_message"]
    # 表情消息的 body 是 null，适配器不能补一句假扮用户发言的正文。
    assert segments == [segments[0]]
    assert segments[0]["type"] == "emoji"
    assert segments[0]["data"] == ""
    assert segments[0]["binary_data_base64"]
    assert segments[0]["hash"]
    assert any(call[0] == "media_content" for call in client.calls)


def test_sticker_source_not_allowed_degrades_without_retry(tmp_path) -> None:
    """来源被 Native 白名单拒绝时不该重试，应立刻退化成占位。

    Native 的 ``validate_sticker_url`` 只接受固定 CDN 白名单，其它来源在取资产
    阶段就返回 409；等待和重试都不会改变结果，只会拖慢这条消息的处理。
    """

    class RejectedStickerClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def media_content(self, asset_id: str, **kwargs):
            self.attempts += 1
            raise plugin_module.MediaError("unsupported_format", media_kind="sticker")

    store = plugin_module.StateStore(tmp_path / "state.db")
    gateway = FakeGateway()
    client = RejectedStickerClient()
    instance = _build(store, gateway, client, _config(enable_media=True, enable_stickers=True))

    plugin_module.asyncio.run(instance._receive_message(_raw(type=47, body=None), store))

    assert client.attempts == 1
    assert len(gateway.messages) == 1
    message = gateway.messages[0]
    # 取不到表情图片时不伪造正文，消息保持原样（body 为 null 即无正文）。
    assert message["raw_message"] == []


def test_sticker_retries_while_native_download_is_pending(tmp_path) -> None:
    """表情异步下载未完成时 Native 返回 unavailable，应当重试而不是降级。"""

    class SlowStickerClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def media_content(self, asset_id: str, **kwargs):
            self.attempts += 1
            self.calls.append(("media_content", asset_id, kwargs))
            if self.attempts < 3:
                raise plugin_module.MediaError("unavailable", media_kind="sticker")
            return b"GIF89a" + b"\x00" * 8, "image/gif"

    store = plugin_module.StateStore(tmp_path / "state.db")
    gateway = FakeGateway()
    client = SlowStickerClient()
    instance = _build(store, gateway, client, _config(enable_media=True, enable_stickers=True))

    plugin_module.asyncio.run(instance._receive_message(_raw(type=47, body=None), store))

    assert len(gateway.messages) == 1
    segments = gateway.messages[0]["raw_message"]
    assert segments[0]["type"] == "emoji"
    assert client.attempts == 3


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
    # Native 没给出 @ 目标 ID 时不能凭空指定目标，正文原样保留。
    assert gateway.messages[0]["raw_message"] == [{"type": "text", "data": "@麦麦 在吗"}]


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

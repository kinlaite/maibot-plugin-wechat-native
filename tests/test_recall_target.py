"""锁定撤回目标的解析：宿主不给会话 ID、甚至拒绝授权时仍能撤回。

这次真实故障的根因是三层叠加：
- 插件 manifest 未声明 message.get_by_id，宿主拒绝授权；
- 出站时把幂等键当作 external_message_id 回给宿主，导致消息 ID 被改写；
- 会话为空时仍请求 /threads//messages，Native 返回 400。

因此这里固定两条契约：
- 拿不到会话时必须能从消息 ID 自身还原；
- 回给宿主的 external_message_id 必须是 Native 真实消息 ID。
"""

from typing import Any, Dict, List, Optional
from unittest.mock import Mock

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wechat_native import plugin as plugin_module  # noqa: E402
from wechat_native.codec import split_message_id  # noqa: E402


class DeniedMessageCapability:
    """模拟宿主未授权 message.get_by_id 的情况。"""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def get_by_id(self, message_id: str, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append((message_id, kwargs))
        raise RuntimeError("[E_CAPABILITY_DENIED] 未获授权能力: message.get_by_id")


def _plugin(capability: Optional[Any] = None) -> Any:
    """构造只带必要上下文的插件实例。

    Args:
        capability: 宿主的 message 能力替身。

    Returns:
        Any: 插件实例。
    """

    instance = plugin_module.WeChatNativePlugin()
    ctx = Mock()
    ctx.message = capability if capability is not None else DeniedMessageCapability()
    instance._set_context(ctx)
    return instance


def test_split_message_id_recovers_thread_from_composite_id() -> None:
    """合成 ID 必须能还原出会话，否则撤回会因缺会话而失败。"""

    thread, raw = split_message_id("acct:57753723704@chatroom:msg_abc")
    assert thread == "57753723704@chatroom"
    assert raw == "msg_abc"


def test_recall_target_falls_back_to_message_id_when_capability_denied() -> None:
    """宿主拒绝授权时，会话仍应从消息 ID 自身还原，而不是留空。"""

    instance = _plugin()
    thread, message_id = asyncio.run(
        instance._resolve_recall_target({"msg_id": "acct:wxid_friend:msg_abc"})
    )
    assert message_id == "acct:wxid_friend:msg_abc"
    assert thread == "wxid_friend"


def test_recall_target_prefers_host_lookup_when_available() -> None:
    """宿主授权时优先用宿主反查结果，覆盖 ID 里携带的会话。

    SDK 的 ``call_capability`` 已按 ``message`` 键解包，插件拿到的就是消息字典本身，
    不再是 ``{"success": True, "message": {...}}`` 这层信封。
    """

    capability = Mock()

    async def get_by_id(message_id: str, **kwargs: Any) -> Dict[str, Any]:
        # 与 SDK 解包后的真实返回值一致：顶层就是消息字典。
        return {
            "message_id": message_id,
            "platform": "wechat_native",
            "message_info": {
                "user_info": {"user_id": "wxid_member", "user_nickname": "成员"},
                "group_info": {"group_id": "57753723704@chatroom"},
                "additional_config": {"wechat_thread_id": "57753723704@chatroom"},
            },
        }

    capability.get_by_id = get_by_id
    instance = _plugin(capability)
    thread, _ = asyncio.run(
        instance._resolve_recall_target({"msg_id": "acct:wxid_friend:msg_abc"})
    )
    assert thread == "57753723704@chatroom"


def test_outbound_returns_native_message_id_not_idempotency_key() -> None:
    """回给宿主的 external_message_id 必须是 Native 消息 ID，不能是幂等键。"""

    class FakeClient:
        async def send_text(self, thread, text, key, mentions=None, *, quote=None):
            return {"id": "send-op", "localMessageId": "msg_native_real", "state": "accepted"}

    instance = _plugin()
    store = Mock()
    instance._store = store
    instance._client = FakeClient()
    instance._native = Mock(
        account_id="acct",
        group_reply_mode="plain",
        group_reply_style="plain",
        max_image_bytes=1024,
    )
    instance._ready = True
    instance._pending_source = {}
    instance._self_id = "wxid_bot"

    message = {
        "platform": "wechat_native",
        "account_id": "acct",
        "message_info": {
            "user_info": {"user_id": "wxid_bot", "user_nickname": "bot"},
            "additional_config": {"platform_io_target_user_id": "wxid_friend"},
        },
        "raw_message": [{"type": "text", "data": "你好"}],
    }
    result = asyncio.run(instance.send_to_platform(message))
    assert result["success"] is True
    assert result["external_message_id"] == "msg_native_real"


def test_recall_without_thread_never_requests_empty_thread() -> None:
    """没有会话也没有消息时必须直接报错，不能请求 /threads//messages。"""

    from wechat_native.tools import WechatNativeTools

    class FakeClient:
        def __init__(self) -> None:
            self.calls: List[tuple] = []

        async def messages(self, thread, *, limit, cursor="", query=""):
            self.calls.append(("messages", thread))
            return {"items": []}

    owner = Mock()
    owner._native = Mock(account_id="acct", wechat_action_admins=["wxid_admin"])
    client = FakeClient()
    owner._client = client
    owner._ready = True
    tools = WechatNativeTools(owner)

    with pytest.raises(ValueError):
        asyncio.run(tools.recall_message("", ""))
    assert client.calls == []

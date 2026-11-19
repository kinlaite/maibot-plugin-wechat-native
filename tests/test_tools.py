"""WeChat Native 工具与回复方式的权限边界测试。

这些测试保护最关键的安全约束：非管理员不能执行写操作、私聊不能调用群工具、
未开启的工具对模型不可见。
"""

from typing import Any, Dict, List, Optional
from unittest.mock import Mock

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wechat_native import plugin as plugin_module  # noqa: E402
from wechat_native.codec import is_group_thread, split_message_id  # noqa: E402
from wechat_native.config import NativeConfig  # noqa: E402
from wechat_native.tools import (  # noqa: E402
    ToolUnavailable,
    WechatNativeTools,
    _project_group,
    clamp_limit,
    clean_text,
    tool_names,
)

GROUP = "12345@chatroom"


class FakeClient:
    """记录调用并返回固定数据的 Native 客户端替身。"""

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.admin = False
        self.thread = GROUP

    async def group(self, thread: str) -> Dict[str, Any]:
        self.calls.append(("group", thread))
        return {"id": thread, "name": "测试群"}

    async def group_members(self, thread: str, *, limit: int, cursor: str = "", search: str = "") -> Dict[str, Any]:
        self.calls.append(("members", thread, limit, search))
        return {"items": [{"username": "wxid_a", "displayName": "A"}]}

    async def messages(self, thread: str, *, limit: int, cursor: str = "", query: str = "") -> Dict[str, Any]:
        self.calls.append(("messages", thread, limit, query))
        return {"items": []}

    async def search_contact(self, query: str) -> Dict[str, Any]:
        self.calls.append(("search_contact", query))
        return {"items": []}

    async def message(self, message_id: str) -> Dict[str, Any]:
        self.calls.append(("message", message_id))
        return {
            "id": message_id,
            "accountId": "acct",
            "threadId": self.thread,
            "direction": "outgoing",
            "senderId": None,
            "serverId": "987654321",
            "recall": None,
        }

    async def recall_message(self, message_id: str, server_id: str, key: str = "") -> Dict[str, Any]:
        self.calls.append(("recall", message_id, server_id))
        return {
            "id": "op-1",
            "state": "succeeded",
            "result": {"remoteOutcome": "accepted", "status": "recalled"},
        }

    async def operation(self, operation_id: str) -> Dict[str, Any]:
        self.calls.append(("operation", operation_id))
        return {
            "id": operation_id,
            "state": "succeeded",
            "result": {"remoteOutcome": "accepted", "status": "recalled"},
        }

    async def set_my_group_name(self, thread: str, name: str, key: str = "") -> Dict[str, Any]:
        self.calls.append(("set_my_group_name", thread, name))
        return {"id": "op-2"}

    async def rename_group(self, thread: str, name: str, key: str = "") -> Dict[str, Any]:
        self.calls.append(("rename_group", thread, name))
        return {"id": "op-3"}


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
        "group_reply_style": "quote",
        "reconnect_delay_seconds": 3,
        "max_image_bytes": 10 * 1024 * 1024,
        "enable_history_tools": True,
        "enable_contact_search": True,
        "enable_wechat_actions": True,
        "enable_friend_requests": False,
        "wechat_action_admins": frozenset({"wxid_admin"}),
    }
    values.update(overrides)
    return NativeConfig(**values)


def _tools(native: NativeConfig, *, admin: Optional[FakeClient] = None, client: Optional[FakeClient] = None) -> WechatNativeTools:
    owner = Mock()
    owner._native = native
    owner._client = client if client is not None else FakeClient()
    owner._ready = True
    owner._admin_client = admin if admin is not None else FakeClient()
    owner._store = Mock()
    return WechatNativeTools(owner)


def test_limit_is_clamped() -> None:
    assert clamp_limit("999", default=20, maximum=50) == 50
    assert clamp_limit("0", default=20, maximum=50) == 1
    assert clamp_limit(None, default=20, maximum=50) == 20
    assert clamp_limit("abc", default=20, maximum=50) == 20


def test_clean_text_rejects_control_chars() -> None:
    with pytest.raises(ValueError):
        clean_text("新名字\x00", label="群名", maximum=512)
    with pytest.raises(ValueError):
        clean_text("   ", label="群名", maximum=512)
    assert clean_text("  正常名字 ", label="群名", maximum=512) == "正常名字"


def test_group_thread_detection() -> None:
    assert is_group_thread(GROUP) is True
    assert is_group_thread("wxid_friend") is False


def test_admin_whitelist_blocks_outsider() -> None:
    tools = _tools(_config())
    with pytest.raises(ToolUnavailable):
        tools.check_admin("wxid_stranger")
    tools.check_admin("wxid_admin")


def test_admin_disabled_blocks_even_admin() -> None:
    tools = _tools(_config(enable_wechat_actions=False))
    with pytest.raises(ToolUnavailable):
        tools.check_admin("wxid_admin")


def test_missing_admin_token_blocks_write() -> None:
    tools = _tools(_config(), admin=None)
    tools._plugin._admin_client = None
    with pytest.raises(ToolUnavailable):
        tools._admin_client()


def test_recall_requires_own_outgoing_message() -> None:
    """撤回走日常 Token（与上游一致），不需要 admin 客户端。"""

    native = _config()
    client = FakeClient()
    tools = _tools(native, client=client)
    result = __import__("asyncio").run(tools.recall_message(GROUP, "msg-1"))
    assert result["status"] == "recalled"
    assert ("recall", "msg-1", "987654321") in client.calls


def test_split_message_id_separates_thread_and_raw_id() -> None:
    """合成的外部消息 ID 必须能拆回会话与 Native 原生 ID。"""

    thread, raw = split_message_id("acct:wxid_friend:msg_abc")
    assert thread == "wxid_friend"
    assert raw == "msg_abc"
    assert split_message_id("msg_abc") == ("", "msg_abc")
    assert split_message_id("") == ("", "")


def test_recall_accepts_composite_message_id() -> None:
    """宿主传入合成 ID 时，应只用原生 ID 请求 Native，并从 ID 还原会话。"""

    native = _config()
    client = FakeClient()
    client.thread = "wxid_friend"
    tools = _tools(native, client=client)
    result = __import__("asyncio").run(
        tools.recall_message("", "acct:wxid_friend:msg_abc")
    )
    assert result["success"] is True
    assert ("message", "msg_abc") in client.calls
    assert ("recall", "msg_abc", "987654321") in client.calls


def test_recall_rejects_private_thread() -> None:
    tools = _tools(_config())
    with pytest.raises(ValueError):
        __import__("asyncio").run(tools.recall_message("wxid_friend", "msg-1"))


def test_recall_works_in_private_chat() -> None:
    """私聊也能撤回机器人自己的消息，不应被群聊限制拦住。"""

    native = _config()
    client = FakeClient()
    client.thread = "wxid_friend"
    admin = FakeClient()
    admin.thread = "wxid_friend"
    tools = _tools(native, admin=admin, client=client)
    result = __import__("asyncio").run(tools.recall_message("wxid_friend", "msg-1"))
    assert result["success"] is True
    assert result["status"] == "recalled"


def test_recall_works_without_thread() -> None:
    """只给消息 ID 时，会话应从消息自身解析，不依赖宿主传入。"""

    native = _config()
    client = FakeClient()
    client.thread = "wxid_friend"
    admin = FakeClient()
    admin.thread = "wxid_friend"
    tools = _tools(native, admin=admin, client=client)
    result = __import__("asyncio").run(tools.recall_message("", "msg-1"))
    assert result["success"] is True
    assert result["status"] == "recalled"


def test_recall_rejects_mismatched_thread() -> None:
    """显式传入的会话与消息实际所属会话不一致时必须拒绝。"""

    native = _config()
    client = FakeClient()
    client.thread = "wxid_friend"
    admin = FakeClient()
    admin.thread = "wxid_friend"
    tools = _tools(native, admin=admin, client=client)
    with pytest.raises(ValueError):
        __import__("asyncio").run(tools.recall_message("wxid_other", "msg-1"))


def test_rename_group_writes_through_admin_client() -> None:
    admin = FakeClient()
    tools = _tools(_config(), admin=admin)
    result = __import__("asyncio").run(tools.rename_group(GROUP, "新群名"))
    assert result["name"] == "新群名"
    assert ("rename_group", GROUP, "新群名") in admin.calls


def test_search_contact_requires_flag() -> None:
    tools = _tools(_config(enable_contact_search=False))
    with pytest.raises(ToolUnavailable):
        __import__("asyncio").run(tools.search_contact("小明"))


def test_tool_names_follow_config() -> None:
    assert tool_names(None) == []
    closed = tool_names(_config(enable_history_tools=False, enable_contact_search=False, wechat_action_admins=frozenset()))
    assert closed == []
    opened = tool_names(_config())
    assert "wechat_search_history" in opened
    assert "wechat_rename_group" in opened


def test_tool_visibility_sync_hides_disabled_tools(tmp_path) -> None:
    instance = plugin_module.WeChatNativePlugin()
    ctx = Mock()
    ctx.paths = Mock()
    ctx.paths.data_dir = tmp_path
    enabled_calls: List[str] = []
    disabled_calls: List[str] = []

    async def enable(name, kind):
        enabled_calls.append(name)

    async def disable(name, kind):
        disabled_calls.append(name)

    ctx.component = Mock()
    ctx.component.enable_component = enable
    ctx.component.disable_component = disable
    instance._set_context(ctx)
    instance.set_plugin_config(
        {
            "plugin": {"enabled": False, "config_version": "1.0.0"},
            "native": {"server_url": "http://127.0.0.1:6174", "account_id": "acct"},
            "chat": {},
            "media": {},
            "tools": {"enable_history_tools": True},
            "actions": {"enable_wechat_actions": True, "wechat_action_admins": []},
        }
    )
    __import__("asyncio").run(instance._sync_tool_visibility())
    assert "wechat_search_history" in enabled_calls
    # 写操作没有管理员白名单时必须保持关闭。
    assert "wechat_rename_group" in disabled_calls
    assert "wechat_rename_group" not in enabled_calls


def test_friend_request_tools_need_admin_whitelist() -> None:
    """只开好友申请开关、没有管理员白名单时，工具必须保持关闭。

    否则工具会出现在模型列表里，但每次调用都被 check_admin 拒绝。
    """

    from wechat_native.tools import tool_names

    instance = plugin_module.WeChatNativePlugin()
    enabled_calls: List[str] = []
    disabled_calls: List[str] = []

    ctx = Mock()

    async def enable(name, kind):
        enabled_calls.append(name)

    async def disable(name, kind):
        disabled_calls.append(name)

    ctx.component = Mock()
    ctx.component.enable_component = enable
    ctx.component.disable_component = disable
    instance._set_context(ctx)
    instance.set_plugin_config(
        {
            "plugin": {"enabled": False, "config_version": "1.1.0"},
            "native": {"server_url": "http://127.0.0.1:6174", "account_id": "acct"},
            "chat": {},
            "media": {},
            "tools": {},
            "actions": {"enable_friend_requests": True, "wechat_action_admins": []},
        }
    )
    __import__("asyncio").run(instance._sync_tool_visibility())
    assert "wechat_list_friend_requests" not in enabled_calls
    assert "wechat_list_friend_requests" in disabled_calls

    # 补上白名单后两个好友申请工具都应出现。
    instance.set_plugin_config(
        {
            "plugin": {"enabled": False, "config_version": "1.1.0"},
            "native": {"server_url": "http://127.0.0.1:6174", "account_id": "acct"},
            "chat": {},
            "media": {},
            "tools": {},
            "actions": {"enable_friend_requests": True, "wechat_action_admins": ["wxid_admin"]},
        }
    )
    config = plugin_module.NativeConfig.from_settings(
        instance.config.native,
        instance.config.chat,
        instance.config.media,
        instance.config.tools,
        instance.config.actions,
    )
    assert "wechat_accept_friend_request" in tool_names(config)


def test_group_projection_never_uses_chatroom_id_as_name() -> None:
    """群名未同步时不能拿 chatroom ID 冒充群名。

    Native 的 ``displayName`` 在群名缺失时就是 ``xxx@chatroom``，
    直接返回会让模型以为这个群叫一串数字。
    """

    empty = _project_group(
        {"id": "57753723704@chatroom", "name": "", "displayName": "57753723704@chatroom"}
    )
    assert empty["name"] == ""

    named = _project_group(
        {"id": "51947325202@chatroom", "name": "吃鸡小分队", "displayName": "吃鸡小分队"}
    )
    assert named["name"] == "吃鸡小分队"

    # name 字段本身是 chatroom ID 时同样不能当群名。
    polluted = _project_group({"id": "x@chatroom", "name": "x@chatroom", "displayName": "x@chatroom"})
    assert polluted["name"] == ""

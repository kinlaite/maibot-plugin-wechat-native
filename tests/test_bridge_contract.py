"""锁定排查修掉的对接 bug。

这些规则都来自真实接口实测，而不是推测：
- 群聊引用必须用两个不同的值（messageId + serverId），同一值会被 Native 拒绝；
- 群名、昵称要按需查询，因为 Native 消息不带 groupName，历史消息也不带 senderName；
- 群管理写操作必须带幂等键，且要等到 remoteOutcome 为 accepted；
- 工具返回给模型的内容要精简，不能夹带 coverage 等内部元数据；
- 事件游标读不到事件时要回到窗口起点，否则适配器会静默空转；
- 群名未知时必须留空，不能拿 chatroom ID 冒充，否则会污染宿主聊天流名称；
- 入站消息要带 platform_io_target_* 字段，否则宿主无法主动发起会话；
- 积压的旧消息不应触发回复，@全体成员与多段发送失败都要如实反馈。
"""

from typing import Any, Dict, List
from unittest.mock import Mock

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wechat_native import plugin as plugin_module  # noqa: E402
from wechat_native.codec import inbound, outbound  # noqa: E402
from wechat_native.tools import (  # noqa: E402
    _project_group,
    _project_members,
    _project_messages,
    _summary_contacts,
    _summary_contact_requests,
    _summary_group,
    _summary_members,
    _summary_messages,
)

GROUP = "57753723704@chatroom"
SOURCE = {"threadId": GROUP, "senderId": "wxid_member", "messageId": "m1", "serverId": "555"}


def _plugin() -> Any:
    """构造带最小配置的插件实例。

    Returns:
        Any: 插件实例。
    """

    instance = plugin_module.WeChatNativePlugin()
    ctx = Mock()
    ctx.message = Mock()
    instance._set_context(ctx)
    native = Mock(
        group_reply_mode="enabled",
        account_id="acct",
    )
    instance._native = native
    instance._self_id = "wxid_bot"
    return instance


class FakeClient:
    """记录调用的 Native 客户端替身。"""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def send_text(self, thread, text, key, mentions=None, *, quote=None):
        self.calls.append(("text", thread, text, mentions, quote))
        return {"id": "op", "localMessageId": "msg_native", "state": "accepted"}

    async def send_image(self, thread, data, content_type, key, **kwargs):
        self.calls.append(("image", thread, content_type))
        return {"id": "job", "state": "completed", "result": {"messageIds": ["msg_media"]}}

    async def send_voice(self, thread, data, content_type, key, **kwargs):
        self.calls.append(("voice", thread, content_type))
        return {"id": "job", "state": "completed", "result": {"messageIds": ["msg_voice"]}}

    async def send_by_key(self, key):
        self.calls.append(("by_key", key))
        return {"id": "op", "localMessageId": "msg_looked_up", "state": "accepted"}

    async def group(self, thread):
        return {"id": thread, "name": "真实群名"}

    async def group_members(self, thread, *, limit, cursor="", search=""):
        return {"items": [{"username": "wxid_member", "displayName": "成员甲"}]}


def test_quote_uses_two_distinct_values() -> None:
    """群引用必须同时给出 messageId 与 serverId，不能复用同一个值。"""

    instance = _plugin()
    _, _, quote = asyncio.run(
        instance._apply_reply_style(
            GROUP, SOURCE, [], "host-msg-id"
        )
    )
    assert quote == {"messageId": "m1", "serverId": "555"}


def test_quote_absent_when_server_id_missing() -> None:
    """缺少 serverId 时不能拼出非法引用参数，应直接放弃引用。"""

    instance = _plugin()
    source = {"threadId": GROUP, "senderId": "wxid_member", "messageId": "m1", "serverId": ""}
    _, reply_id, quote = asyncio.run(
        instance._apply_reply_style(
            GROUP, source, [], "host-msg-id"
        )
    )
    assert quote is None
    assert reply_id == "host-msg-id"


def test_outbound_returns_real_id_for_text_and_media() -> None:
    """文本与媒体都要回传真实消息 ID，媒体 ID 藏在 result.messageIds 里。"""

    assert plugin_module._receipt_message_id({"localMessageId": "msg_a"}) == "msg_a"
    assert (
        plugin_module._receipt_message_id({"result": {"messageIds": ["msg_b"]}}) == "msg_b"
    )
    assert plugin_module._receipt_message_id({"serverId": "123"}) == ""


def test_group_projection_drops_internal_metadata() -> None:
    """群资料投影不能把 coverage 之类内部元数据带给模型。"""

    projected = _project_group(
        {
            "id": GROUP,
            "name": "",
            "displayName": GROUP,
            "remark": "别名群",
            "ownerId": "wxid_owner",
            "announcement": "公告",
            "myDisplayName": "麦麦",
            "administratorIds": ["a", "b"],
            "coverage": {"databases": [{"relativePath": "contact/contact.db"}]},
        }
    )
    assert projected["name"] == "别名群"
    assert projected["administrator_count"] == 2
    assert "coverage" not in projected


def test_member_and_message_projection_shapes() -> None:
    """成员与历史消息投影只保留模型需要的字段。"""

    members = _project_members(
        {
            "items": [
                {"username": "wxid_a", "displayName": "甲", "roomDisplayName": "甲甲"},
                {"id": "wxid_b", "displayName": "乙"},
            ],
            "nextCursor": "c1",
            "coverage": {"databases": []},
        }
    )
    # 群内昵称优先，联系人昵称不同时额外给出，空字段不再输出。
    assert members["members"][0] == {
        "user_id": "wxid_a",
        "display_name": "甲甲",
        "contact_name": "甲",
    }
    assert members["members"][1] == {"user_id": "wxid_b", "display_name": "乙"}
    assert members["next_cursor"] == "c1"

    messages = _project_messages(
        {
            "items": [
                {
                    "id": "msg_1",
                    "senderId": "wxid_a",
                    "direction": "incoming",
                    "createdAt": "2026-01-01T00:00:00+00:00",
                    "type": 1,
                    "body": "你好",
                    "version": "hash",
                },
                {"id": "msg_2", "deleted": True},
            ],
            "coverage": {"databases": []},
        }
    )
    assert len(messages["messages"]) == 1
    assert messages["messages"][0]["text"] == "你好"
    # 微信内部类型码要翻译成可读名称，不能把 1 直接丢给模型。
    assert messages["messages"][0]["type"] == "text"
    assert "version" not in messages["messages"][0]


def test_message_projection_strips_mentions_and_maps_types() -> None:
    """历史消息要把类型码翻译成可读名称，正文保持原样。"""

    messages = _project_messages(
        {
            "items": [
                {"id": "m1", "senderId": "wxid_a", "type": 1, "body": "那挂个直播吧@ffz\u2005"},
                {"id": "m2", "senderId": "wxid_b", "type": 49, "body": "能说下片名吗"},
                {"id": "m3", "senderId": "wxid_c", "type": 3, "body": ""},
            ]
        }
    )
    texts = [m["text"] for m in messages["messages"]]
    # @ 标记属于消息内容，去掉会让模型读错是谁在跟谁说话。
    assert texts[0] == "那挂个直播吧@ffz\u2005"
    types = [m["type"] for m in messages["messages"]]
    assert types == ["text", "app", "image"]


def test_inbound_uses_sender_name_and_quote_author() -> None:
    """入站消息要带上真实昵称，引用要作为 reply 段而不是拼进正文。"""

    message = inbound(
        {
            "id": "msg_1",
            "accountId": "acct",
            "threadId": GROUP,
            "senderId": "wxid_a",
            "senderName": "张三",
            "type": 1,
        },
        "acct",
        text="在的",
        segments=[],
        self_id="wxid_bot",
        group_name="测试群",
        quote={"embeddedText": "原话", "senderName": "李四", "senderId": "wxid_b"},
    )
    info = message["message_info"]
    assert info["user_info"]["user_nickname"] == "张三"
    assert info["group_info"]["group_name"] == "测试群"
    reply = message["raw_message"][0]
    assert reply["type"] == "reply"
    assert reply["data"]["target_message_content"] == "原话"
    assert reply["data"]["target_message_sender_nickname"] == "李四"
    # 正文保持用户原话，不被引用内容污染。
    assert message["raw_message"][1] == {"type": "text", "data": "在的"}


def test_cursor_probe_detects_empty_stream() -> None:
    """游标读不到事件时要判定为越界并回到窗口起点。"""

    class CursorClient:
        def __init__(self, reachable: set) -> None:
            self.reachable = reachable

        async def cursor_has_events(self, after: str) -> bool:
            return after in self.reachable

        async def window_end_cursor(self) -> None:
            # 读不到窗口上界时只能退回起点。
            return None

    store = Mock()
    store.cursor = "9999"
    resets: List[str] = []
    store.advance_reset = resets.append

    instance = _plugin()
    asyncio.run(instance._ensure_cursor_in_window(CursorClient({"0"}), store))
    assert resets == ["0"]


def test_cursor_reset_jumps_to_window_end_when_readable() -> None:
    """能确定窗口末尾且该处有事件时，直接跳过去而不是重放整个窗口。"""

    class CursorClient:
        async def cursor_has_events(self, after: str) -> bool:
            # 起点和窗口末尾都能读到事件；旧游标 9999 读不到。
            return after in {"0", "5000"}

        async def window_end_cursor(self) -> int:
            return 5000

    store = Mock()
    store.cursor = "9999"
    resets: List[str] = []
    store.advance_reset = resets.append

    instance = _plugin()
    asyncio.run(instance._ensure_cursor_in_window(CursorClient(), store))
    assert resets == ["5000"]


def test_cursor_reset_falls_back_when_window_end_is_stale() -> None:
    """窗口末尾仍读不到事件时，必须退回起点，否则会反复重置。"""

    class CursorClient:
        async def cursor_has_events(self, after: str) -> bool:
            return after == "0"

        async def window_end_cursor(self) -> int:
            # 落后于真实上界：这个位置同样读不到事件。
            return 5000

    store = Mock()
    store.cursor = "9999"
    resets: List[str] = []
    store.advance_reset = resets.append

    instance = _plugin()
    asyncio.run(instance._ensure_cursor_in_window(CursorClient(), store))
    assert resets == ["0"]


def test_cursor_probe_keeps_valid_cursor() -> None:
    """游标仍能读到事件时不得重置，否则会重复处理。"""

    class CursorClient:
        async def cursor_has_events(self, after: str) -> bool:
            return True

    store = Mock()
    store.cursor = "9999"
    resets: List[str] = []
    store.advance_reset = resets.append

    instance = _plugin()
    asyncio.run(instance._ensure_cursor_in_window(CursorClient(), store))
    assert resets == []


def test_group_name_never_falls_back_to_thread_id() -> None:
    """群名未知时必须留空，不能拿 chatroom ID 冒充。"""

    message = inbound(
        {"id": "m1", "threadId": GROUP, "senderId": "wxid_a", "type": 1},
        "acct",
        text="你好",
        segments=[],
        self_id="wxid_bot",
        group_name="",
    )
    assert message["message_info"]["group_info"]["group_name"] == ""


def test_inbound_carries_platform_target_fields() -> None:
    """入站消息要带宿主的路由目标字段，否则无法主动发起会话。"""

    group_message = inbound(
        {"id": "m1", "threadId": GROUP, "senderId": "wxid_a", "type": 1},
        "acct",
        text="你好",
        segments=[],
        self_id="wxid_bot",
        group_name="测试群",
    )
    config = group_message["message_info"]["additional_config"]
    assert config["platform_io_target_group_id"] == GROUP

    private_message = inbound(
        {"id": "m2", "threadId": "wxid_friend", "senderId": "wxid_friend", "type": 1},
        "acct",
        text="你好",
        segments=[],
        self_id="wxid_bot",
    )
    config = private_message["message_info"]["additional_config"]
    assert config["platform_io_target_user_id"] == "wxid_friend"


def test_quoted_image_is_visible_to_model() -> None:
    """被引用的图片要显示为 [图片]，而不是内容不可见。"""

    message = inbound(
        {
            "id": "m3",
            "accountId": "acct",
            "threadId": GROUP,
            "senderId": "wxid_a",
            "type": 49,
            "appMessageType": 57,
        },
        "acct",
        text="这是啥",
        segments=[],
        self_id="wxid_bot",
        group_name="测试群",
        quote={"type": 3, "embeddedImage": True, "senderName": "等我", "serverId": "1"},
    )
    reply = message["raw_message"][0]
    assert reply["type"] == "reply"
    assert reply["data"]["target_message_content"] == "[图片]"


def test_at_all_is_parsed_and_exclusive() -> None:
    """@全体成员要能解析，且不能与 @个人 同时出现。"""

    message = {
        "message_info": {
            "user_info": {"user_id": "wxid_bot"},
            "group_info": {"group_id": GROUP},
            "additional_config": {},
        },
        "raw_message": [
            {"type": "text", "data": "开会"},
            {"type": "at", "target_user_id": "all"},
        ],
    }
    _, _, mentions, _ = outbound(message, allow_group=True, max_image_bytes=1024)
    assert mentions == ["all"]
    assert plugin_module._mentions_payload(mentions) == {"all": True}

    message["raw_message"].append({"type": "at", "target_user_id": "wxid_a"})
    try:
        outbound(message, allow_group=True, max_image_bytes=1024)
    except ValueError:
        return
    raise AssertionError("同时 @全体 与 @个人 应被拒绝")


def test_stale_message_is_rejected() -> None:
    """补同步的旧消息不应触发回复，但晚到的正常消息必须放行。"""

    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)

    def at(minutes: float) -> Dict[str, str]:
        return {"createdAt": (now + timedelta(minutes=minutes)).isoformat()}

    # 网络与服务端延迟会让正常消息晚到几十秒到十几分钟，必须放行。
    assert plugin_module._timestamp_acceptable(at(0))
    assert plugin_module._timestamp_acceptable(at(-5))
    assert plugin_module._timestamp_acceptable(at(-14))
    # 补同步的历史消息要拦掉。
    assert not plugin_module._timestamp_acceptable(at(-16))
    assert not plugin_module._timestamp_acceptable(at(-60))
    # 未来消息只容忍少量时钟偏差。
    assert plugin_module._timestamp_acceptable(at(4))
    assert not plugin_module._timestamp_acceptable(at(6))
    assert not plugin_module._timestamp_acceptable({})


def test_recall_rejects_thread_outside_allowlist() -> None:
    """撤回白名单要能拦住不在列表内的会话。"""

    from wechat_native.config import NativeConfig
    from wechat_native.tools import WechatNativeTools

    config = NativeConfig(
        api_url="http://127.0.0.1:6174/v1",
        account_id="acct",
        token="t",
        token_file="",
        admin_token="a",
        admin_token_file="",
        enable_private_reply=True,
        allowed_users=frozenset(),
        allowed_groups=frozenset(),
        group_reply_mode="enabled",
        enable_media=False,
        enable_stickers=False,
        reconnect_delay_seconds=1,
        max_image_bytes=1024,
        enable_history_tools=True,
        enable_contact_search=False,
        enable_wechat_actions=True,
        enable_friend_requests=False,
        wechat_action_admins=frozenset({"wxid_a"}),
        recall_allowed_threads=frozenset({"other@chatroom"}),
    )

    class Client:
        async def message(self, message_id):
            return {
                "id": message_id,
                "accountId": "acct",
                "threadId": GROUP,
                "direction": "outgoing",
                "serverId": "123",
                "recall": None,
            }

    owner = Mock()
    owner._native = config
    owner._client = Client()
    owner._admin_client = Client()
    tools = WechatNativeTools(owner)
    try:
        asyncio.run(tools.recall_message(GROUP, "msg_native"))
    except ValueError as error:
        assert "白名单" in str(error)
        return
    raise AssertionError("白名单外的会话应拒绝撤回")


def test_group_name_falls_back_to_members() -> None:
    """Native 未同步群名时，用成员昵称拼出可读名称而不是空串。"""

    class Client:
        async def group(self, thread):
            return {"id": thread, "name": "", "displayName": thread}

        async def group_members(self, thread, *, limit, cursor="", search=""):
            return {
                "items": [
                    {"username": "wxid_a", "displayName": "甲"},
                    {"username": "wxid_b", "displayName": "乙"},
                ]
            }

    instance = _plugin()
    instance._self_id = "wxid_bot"
    name = asyncio.run(instance._group_name(Client(), GROUP))
    assert name == "甲、乙"


def test_group_name_prefers_native_name() -> None:
    """Native 有群名时直接使用，不要拼成员。"""

    class Client:
        async def group(self, thread):
            return {"id": thread, "name": "真实群名"}

        async def group_members(self, thread, *, limit, cursor="", search=""):
            raise AssertionError("有群名时不应读取成员")

    instance = _plugin()
    name = asyncio.run(instance._group_name(Client(), GROUP))
    assert name == "真实群名"


def test_private_nickname_comes_from_contacts() -> None:
    """私聊消息不带 senderName 时，昵称要来自联系人资料。"""

    class Client:
        async def contacts(self, *, limit, kind="", search="", query="", cursor=""):
            return {"items": [{"username": "wxid_friend", "displayName": "好友甲"}]}

    instance = _plugin()
    name = asyncio.run(instance._member_name(Client(), "", "wxid_friend"))
    assert name == "好友甲"


def test_group_nickname_prefers_room_display_name() -> None:
    """群聊优先用群昵称，而不是联系人的全局昵称。"""

    class Client:
        async def group_members(self, thread, *, limit, cursor="", search=""):
            return {
                "items": [
                    {"username": "wxid_a", "displayName": "全局名", "roomDisplayName": "群内名"}
                ]
            }

        async def contacts(self, *, limit, kind="", search="", query="", cursor=""):
            raise AssertionError("群昵称存在时不应回退到联系人")

    instance = _plugin()
    name = asyncio.run(instance._member_name(Client(), GROUP, "wxid_a"))
    assert name == "群内名"


def test_group_mention_sets_is_at_flags() -> None:
    """群 @ 必须设置 is_at/is_mentioned，否则宿主认为没叫它。"""

    base = {
        "id": "m1",
        "threadId": GROUP,
        "senderId": "wxid_a",
        "type": 1,
    }
    mentioned = inbound(
        {
            **base,
            "mentions": {"all": False, "self": True, "state": "known", "userIds": ["wxid_bot"]},
        },
        "acct",
        text="@麦麦 你好",
        segments=[],
        self_id="wxid_bot",
        group_name="测试群",
    )
    assert mentioned["is_at"] is True
    assert mentioned["is_mentioned"] is True

    plain = inbound(
        {**base, "mentions": {"all": False, "self": False, "state": "known", "userIds": []}},
        "acct",
        text="你好",
        segments=[],
        self_id="wxid_bot",
        group_name="测试群",
    )
    assert plain["is_at"] is False
    assert plain["is_mentioned"] is False


def test_group_mention_of_others_keeps_target() -> None:
    """@ 别人要变成 at 段，正文原样保留，模型才看得出不是在叫自己。"""

    base = {
        "id": "m1",
        "threadId": GROUP,
        "senderId": "wxid_a",
        "type": 1,
    }
    message = inbound(
        {
            **base,
            "mentions": {"all": False, "self": False, "state": "known", "userIds": ["wxid_other"]},
        },
        "acct",
        text="@乐安\u2005晚上上号吗",
        segments=[],
        self_id="wxid_bot",
        group_name="测试群",
    )
    assert message["is_at"] is False
    assert message["is_mentioned"] is False
    assert message["raw_message"][0] == {
        "type": "at",
        "data": {"target_user_id": "wxid_other", "target_user_nickname": "乐安", "target_user_cardname": None},
    }
    assert message["raw_message"][1]["data"] == "晚上上号吗"


def test_group_mention_of_others_without_ids_keeps_nickname() -> None:
    """Native 没给出 @ 目标时保留昵称，不能删掉 @ 变成无主语正文。"""

    message = inbound(
        {
            "id": "m1",
            "threadId": GROUP,
            "senderId": "wxid_a",
            "type": 1,
            "mentions": {"all": False, "self": False, "state": "known", "userIds": []},
        },
        "acct",
        text="@乐安\u2005晚上上号吗",
        segments=[],
        self_id="wxid_bot",
        group_name="测试群",
    )
    assert message["is_at"] is False
    assert message["is_mentioned"] is False
    assert message["raw_message"] == [{"type": "text", "data": "@乐安 晚上上号吗"}]


def test_group_mention_self_becomes_at_segment() -> None:
    """@ 机器人时同样是 at 段，正文不被剥掉。"""

    message = inbound(
        {
            "id": "m1",
            "threadId": GROUP,
            "senderId": "wxid_a",
            "type": 1,
            "mentions": {"all": False, "self": True, "state": "known", "userIds": ["wxid_bot"]},
        },
        "acct",
        text="让你吃泡面@大肥鱼\u2005",
        segments=[],
        self_id="wxid_bot",
        group_name="测试群",
    )
    assert message["is_at"] is True
    assert message["raw_message"] == [
        {"type": "text", "data": "让你吃泡面"},
        {
            "type": "at",
            "data": {"target_user_id": "wxid_bot", "target_user_nickname": "大肥鱼", "target_user_cardname": None},
        },
    ]


def test_group_mention_self_only_is_not_empty() -> None:
    """只 @ 机器人、没有正文时要留下 at 段，不能产出空消息。"""

    message = inbound(
        {
            "id": "m1",
            "threadId": GROUP,
            "senderId": "wxid_a",
            "type": 1,
            "mentions": {"all": False, "self": True, "state": "known", "userIds": ["wxid_bot"]},
        },
        "acct",
        text="@麦麦92\u2005",
        segments=[],
        self_id="wxid_bot",
        group_name="测试群",
    )
    assert message["is_at"] is True
    assert message["raw_message"] == [
        {
            "type": "at",
            "data": {"target_user_id": "wxid_bot", "target_user_nickname": "麦麦92", "target_user_cardname": None},
        }
    ]


def test_group_mention_others_only_is_not_empty() -> None:
    """只 @ 别人、没有正文时至少要留下 at 段，不能是空消息。"""

    message = inbound(
        {
            "id": "m1",
            "threadId": GROUP,
            "senderId": "wxid_a",
            "type": 1,
            "mentions": {"all": False, "self": False, "state": "known", "userIds": ["wxid_other"]},
        },
        "acct",
        text="@乐安\u2005",
        segments=[],
        self_id="wxid_bot",
        group_name="测试群",
    )
    assert message["is_at"] is False
    assert message["raw_message"] == [
        {
            "type": "at",
            "data": {"target_user_id": "wxid_other", "target_user_nickname": "乐安", "target_user_cardname": None},
        }
    ]


def test_tool_summaries_are_human_readable() -> None:
    """工具返回的 ``content`` 要能直接读给模型，而不是一坨 JSON。

    宿主只把 ``content`` / ``message`` 作为模型可见文本，
    没有这两个字段时模型看到的是结构化数据的 JSON 序列化结果。
    """

    group = _summary_group(
        {
            "group_id": "51947325202@chatroom",
            "name": "吃鸡小分队",
            "owner_id": "wxid_owner",
            "announcement": "密室",
            "my_display_name": "麦麦92",
            "administrator_count": 3,
        }
    )
    assert "吃鸡小分队" in group
    assert "wxid_owner" in group
    assert "密室" in group
    assert "{" not in group

    # 群名未同步时要说清楚，不能把 chatroom ID 当成群名。
    unnamed = _summary_group({"group_id": "1@chatroom", "name": "", "administrator_count": 0})
    assert "未同步" in unnamed
    assert "1@chatroom" not in unnamed
    # 没有管理员时不该输出「管理员数量：0」这种噪音。
    assert "管理员数量" not in unnamed

    members = _summary_members(
        {
            "members": [
                {"user_id": "wxid_a", "display_name": "等我"},
                {"user_id": "wxid_c", "display_name": "安安", "contact_name": "乐安"},
            ]
        }
    )
    assert "等我" in members and "安安" in members and "乐安" in members

    messages = _summary_messages(
        {
            "messages": [
                {
                    "sender_id": "wxid_a",
                    "sender_name": "等我",
                    "direction": "incoming",
                    "timestamp": "2026-11-19T00:00:00Z",
                    "type": "text",
                    "text": "你好",
                },
                {
                    "sender_id": "wxid_bot",
                    "sender_name": "",
                    "direction": "outgoing",
                    "timestamp": "2026-11-19T00:00:01Z",
                    "type": "image",
                    "text": "",
                },
            ]
        }
    )
    # 自己发的消息要写成「我」，媒体消息用类型占位而不是空正文。
    assert "等我：你好" in messages
    assert "我：[image]" in messages

    contacts = _summary_contacts({"contacts": [{"user_id": "wxid_a", "display_name": "等我", "remark": "同事"}]})
    assert "等我" in contacts and "同事" in contacts

    requests = _summary_contact_requests({"items": [{"id": "req_1", "displayName": "张三", "message": "你好"}]})
    assert "张三" in requests and "req_1" in requests


def test_tool_summaries_handle_empty_results() -> None:
    """空结果要给出明确说明，不能返回空字符串让模型猜。"""

    assert _summary_members({"members": []}).strip()
    assert _summary_messages({"messages": []}).strip()
    assert _summary_contacts({"contacts": []}).strip()
    assert _summary_contact_requests({"items": []}).strip()


def test_group_summary_keeps_announcement_verbatim() -> None:
    """群公告原样给出，删占位符或去重都会改动群主实际发布的内容。"""

    text = _summary_group(
        {
            "group_id": "1@chatroom",
            "name": "测试群",
            "announcement": "密室\n[图片]\n[图片]\n密室",
        }
    )
    assert "[图片]" in text
    assert text.count("密室") == 2

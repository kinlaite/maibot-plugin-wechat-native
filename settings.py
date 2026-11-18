"""WeChat Native 适配器配置模型。"""

from typing import List, Literal

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="启用微信连接；启用后等待 Native 账号在线")
    config_version: str = Field(default="1.1.0", description="配置版本")


class NativeSection(PluginConfigBase):
    """Native 服务连接配置。"""

    __ui_label__ = "Native 服务"
    __ui_icon__ = "server"
    __ui_order__ = 1

    server_url: str = Field(
        default="",
        description="Native 服务地址",
        json_schema_extra={
            "hint": "例如 http://native-host:6174，可带 /v1；必须从麦麦所在机器可达",
            "placeholder": "http://127.0.0.1:6174",
        },
    )
    account_id: str = Field(
        default="",
        description="Native Account ID",
        json_schema_extra={"hint": "GET /v1/accounts 返回的 id，不是微信号"},
    )
    token: str = Field(
        default="",
        description="Native Token",
        json_schema_extra={"hint": "留空时读取 token_file；填写后会写入插件配置", "hidden": True},
    )
    token_file: str = Field(
        default="",
        description="Token 只读文件路径",
        json_schema_extra={"hint": "优先于 token；须为麦麦可读的文件路径"},
    )
    reconnect_delay_seconds: int = Field(
        default=3,
        ge=1,
        le=60,
        description="断线重连初始间隔（秒）",
        json_schema_extra={"hint": "连续失败后按倍数退避，最大 30 秒"},
    )


class ChatSection(PluginConfigBase):
    """私聊与群聊接入配置。"""

    __ui_label__ = "聊天范围"
    __ui_icon__ = "messages-square"
    __ui_order__ = 2

    enable_private_reply: bool = Field(default=True, description="启用私聊回复")
    allowed_users: List[str] = Field(
        default_factory=list,
        description="私聊用户白名单",
        json_schema_extra={"hint": "使用 senderId；留空允许所有私聊用户"},
    )
    allowed_groups: List[str] = Field(
        default_factory=list,
        description="群白名单",
        json_schema_extra={"hint": "使用 threadId（通常以 @chatroom 结尾）；留空允许所有群"},
    )
    group_reply_mode: Literal["enabled", "disabled"] = Field(
        default="enabled",
        description="是否接入群聊消息",
        json_schema_extra={
            "hint": (
                "enabled：所有群消息都接入，由麦麦按 @、昵称提及和回复概率决定是否回应；"
                "disabled：不接入群聊。适配器不做 @ 过滤，否则麦麦完全看不到群聊"
            )
        },
    )
    group_reply_style: Literal["quote", "mention", "plain"] = Field(
        default="quote",
        description="群聊回复方式",
        json_schema_extra={
            "hint": "quote：引用触发消息；mention：@触发者；plain：直接发送。仅对首条文字回复生效，私聊不处理"
        },
    )


class ToolSection(PluginConfigBase):
    """只读工具开关。"""

    __ui_label__ = "只读工具"
    __ui_icon__ = "wrench"
    __ui_order__ = 4

    enable_history_tools: bool = Field(
        default=False,
        description="启用历史消息、群资料与群成员查询",
        json_schema_extra={"hint": "只读取当前聊天，不跨会话；需要 Token 具备 read 权限"},
    )
    enable_contact_search: bool = Field(
        default=False,
        description="启用联系人搜索",
        json_schema_extra={"hint": "只搜索并返回匹配结果，不会发送好友申请"},
    )


class ActionSection(PluginConfigBase):
    """写操作工具开关，均需要 admin Token。"""

    __ui_label__ = "管理工具"
    __ui_icon__ = "shield"
    __ui_order__ = 5

    admin_token: str = Field(
        default="",
        description="Native admin Token",
        json_schema_extra={"hint": "留空时读取 admin_token_file；不配置则所有写操作工具保持关闭", "hidden": True},
    )
    admin_token_file: str = Field(
        default="",
        description="Native admin Token 文件路径",
        json_schema_extra={"hint": "优先于 admin_token；须具备目标账号的 admin scope"},
    )
    enable_wechat_actions: bool = Field(
        default=False,
        description="启用撤回与群名修改工具",
        json_schema_extra={"hint": "仅对 admin 白名单内的发送者生效"},
    )
    wechat_action_admins: List[str] = Field(
        default_factory=list,
        description="可执行写操作的 senderId 白名单",
        json_schema_extra={"hint": "填写真实 senderId（例如 wxid_xxx），不是昵称；留空时任何人都不能使用写操作工具"},
    )
    recall_allowed_threads: List[str] = Field(
        default_factory=list,
        description="允许撤回消息的会话白名单",
        json_schema_extra={
            "hint": "填写群 ID 或 wxid；留空表示不限制。用于防止误撤回其他会话里的消息"
        },
    )
    enable_friend_requests: bool = Field(
        default=False,
        description="启用好友申请处理",
        json_schema_extra={"hint": "查看并处理好友申请需要 admin Token"},
    )


class MediaSection(PluginConfigBase):
    """图片、语音与 PDF 配置。"""

    __ui_label__ = "媒体"
    __ui_icon__ = "image"
    __ui_order__ = 3

    enable_media: bool = Field(default=True, description="启用图片、PDF 与语音识别")
    enabled_stickers: bool = Field(default=False, description="启用表情读取与发送")
    max_image_mb: int = Field(default=10, ge=1, le=20, description="单张图片大小上限（MB）")


class Settings(PluginConfigBase):
    """WeChat Native 适配器完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    native: NativeSection = Field(default_factory=NativeSection)
    chat: ChatSection = Field(default_factory=ChatSection)
    media: MediaSection = Field(default_factory=MediaSection)
    tools: ToolSection = Field(default_factory=ToolSection)
    actions: ActionSection = Field(default_factory=ActionSection)

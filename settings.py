"""WeChat Native 适配器配置模型。"""

from typing import List, Literal

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="启用微信连接；启用后等待 Native 账号在线")
    config_version: str = Field(default="1.0.0", description="配置版本")


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
    group_reply_mode: Literal["mention", "all", "disabled"] = Field(
        default="mention",
        description="群聊触发模式",
        json_schema_extra={
            "hint": "mention：仅明确 @机器人时接入；all：所有群消息都接入并交给麦麦主动回复策略；disabled：关闭群聊"
        },
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

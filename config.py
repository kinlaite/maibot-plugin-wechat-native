"""WeChat Native 传输层常量与配置校验。"""

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_VOICE_BYTES = 25 * 1024 * 1024 - 17
MAX_VOICE_SECONDS = 300
VOICE_SEND_TIMEOUT = 25 * 60
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_PDF_TEXT_CHARS = 32000
MEDIA_DOWNLOAD_TIMEOUT = 60
DEFAULT_IMAGE_PROMPT = "请描述这张图片的内容。"
DEFAULT_PDF_PROMPT = "请阅读并总结这份 PDF 文件的主要内容。"


@dataclass(frozen=True)
class NativeConfig:
    """校验后的 Native 连接配置。"""

    api_url: str
    account_id: str
    token: str
    token_file: str
    admin_token: str
    admin_token_file: str
    enable_private_reply: bool
    enable_media: bool
    enable_stickers: bool
    allowed_users: frozenset[str]
    allowed_groups: frozenset[str]
    group_reply_mode: Literal["enabled", "disabled"]
    group_reply_style: Literal["quote", "mention", "plain"]
    reconnect_delay_seconds: int
    max_image_bytes: int
    enable_history_tools: bool
    enable_contact_search: bool
    enable_wechat_actions: bool
    enable_friend_requests: bool
    wechat_action_admins: frozenset[str]
    recall_allowed_threads: frozenset[str] = frozenset()

    @classmethod
    def from_settings(
        cls, settings, network_settings, media_settings, tool_settings, action_settings
    ) -> "NativeConfig":
        """从插件配置节构造并校验连接参数。

        Args:
            settings: ``native`` 配置节。
            network_settings: ``chat`` 配置节。
            media_settings: ``media`` 配置节。
            tool_settings: ``tools`` 配置节。
            action_settings: ``actions`` 配置节。

        Returns:
            NativeConfig: 完成校验的连接配置。

        Raises:
            ValueError: 服务地址、账号或模式配置不合法。
        """

        url = str(settings.server_url or "").strip().rstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("server_url 必须是无认证信息、查询参数或片段的 HTTP(S) 地址")
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("server_url 端口无效") from None
        del port
        account = str(settings.account_id or "").strip()
        if not account:
            raise ValueError("account_id 必填，取 GET /v1/accounts 返回的 id")
        mode = str(network_settings.group_reply_mode or "enabled").strip()
        if mode not in {"enabled", "disabled"}:
            raise ValueError("group_reply_mode 必须为 enabled/disabled")
        style = str(network_settings.group_reply_style or "quote").strip()
        if style not in {"quote", "mention", "plain"}:
            raise ValueError("group_reply_style 必须为 quote/mention/plain")
        return cls(
            api_url=url if parsed.path.endswith("/v1") else url + "/v1",
            account_id=account,
            token=str(settings.token or ""),
            token_file=str(settings.token_file or ""),
            admin_token=str(action_settings.admin_token or ""),
            admin_token_file=str(action_settings.admin_token_file or ""),
            enable_private_reply=bool(network_settings.enable_private_reply),
            enable_media=bool(media_settings.enable_media),
            enable_stickers=bool(media_settings.enabled_stickers),
            allowed_users=frozenset(
                str(item).strip() for item in (network_settings.allowed_users or []) if str(item).strip()
            ),
            allowed_groups=frozenset(
                str(item).strip() for item in (network_settings.allowed_groups or []) if str(item).strip()
            ),
            group_reply_mode=mode,
            group_reply_style=style,
            reconnect_delay_seconds=int(settings.reconnect_delay_seconds or 3),
            max_image_bytes=int(media_settings.max_image_mb) * 1024 * 1024,
            enable_history_tools=bool(tool_settings.enable_history_tools),
            enable_contact_search=bool(tool_settings.enable_contact_search),
            enable_wechat_actions=bool(action_settings.enable_wechat_actions),
            enable_friend_requests=bool(action_settings.enable_friend_requests),
            wechat_action_admins=frozenset(
                str(item).strip() for item in (action_settings.wechat_action_admins or []) if str(item).strip()
            ),
            recall_allowed_threads=frozenset(
                str(item).strip() for item in (action_settings.recall_allowed_threads or []) if str(item).strip()
            ),
        )

    def load_admin_token(self) -> str:
        """读取 admin Token，未配置时返回空字符串。

        Returns:
            str: 可用于管理接口的 Token；未配置时为空。
        """

        if not self.admin_token and not self.admin_token_file:
            return ""
        token = self.admin_token
        if self.admin_token_file:
            try:
                with open(self.admin_token_file, encoding="utf-8") as token_handle:
                    token = token_handle.read(8193).rstrip("\r\n")
            except (OSError, TypeError, ValueError):
                raise ValueError("admin_token_file 不可读，请检查路径与权限") from None
        if not token or len(token) > 8192 or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ValueError("admin_token 必须不含空格或控制字符")
        return token

    def load_token(self) -> str:
        """读取 Token，``token_file`` 优先于内联配置。

        Returns:
            str: 可用于 ``Authorization`` 的 Token。

        Raises:
            ValueError: Token 缺失或包含非法字符。
        """

        token = self.token
        if self.token_file:
            try:
                with open(self.token_file, encoding="utf-8") as token_handle:
                    token = token_handle.read(8193).rstrip("\r\n")
            except (OSError, TypeError, ValueError):
                raise ValueError("token_file 不可读，请检查路径与权限") from None
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 8192
            or any(ord(char) < 33 or ord(char) > 126 for char in token)
        ):
            raise ValueError("必须配置有效的 token 或 token_file（不含空格或控制字符）")
        return token

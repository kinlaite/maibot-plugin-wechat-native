"""微信 Native 适配器：通过 agent-wechat-native /v1 API 接入个人微信。"""

from contextlib import suppress
from typing import Any, ClassVar, Dict, List, Optional, Type

from maibot_sdk import (
    ConfigWidget,
    ConfigWidgetState,
    MaiBotPlugin,
    MessageGateway,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import ToolParameterInfo, ToolParamType

import asyncio
import secrets

from .codec import (
    DEFAULT_IMAGE_PROMPT,
    DEFAULT_PDF_PROMPT,
    PLATFORM,
    inbound,
    is_group_thread,
    message_id,
    outbound,
    segments_from_media,
)
from .config import MAX_PDF_BYTES, MAX_VOICE_BYTES, NativeConfig
from .log import logger
from .native_client import (
    MediaError,
    NativeAPIError,
    NativeClient,
    SendError,
    extract_pdf_text,
    is_pdf_filename,
)
from .settings import Settings
from .state import StateIdentityError, StateStore, cursor_number
from .tools import ToolUnavailable, WechatNativeTools, describe_error, tool_names

GATEWAY_NAME = "wechat_native_gateway"
MEDIA_RETRY_SECONDS = 3
ALL_TOOL_NAMES = (
    "wechat_search_history",
    "wechat_group_info",
    "wechat_group_members",
    "wechat_search_contact",
    "wechat_recall_message",
    "wechat_set_my_group_nickname",
    "wechat_rename_group",
    "wechat_list_friend_requests",
    "wechat_accept_friend_request",
)


class WeChatNativePlugin(MaiBotPlugin):
    """把 agent-wechat-native 的 SSE 事件流接入麦麦消息网关。"""

    config_model: ClassVar[Optional[Type[PluginConfigBase]]] = Settings

    config_widgets: ClassVar[List[ConfigWidget]] = [
        ConfigWidget(id="connection", kind="status", label="微信连接状态"),
        ConfigWidget(id="account", kind="status", label="微信账号"),
        ConfigWidget(id="reconnect", kind="action", label="重新连接"),
    ]

    def __init__(self) -> None:
        """初始化适配器运行时状态。"""

        super().__init__()
        self._worker: Optional[asyncio.Task[None]] = None
        self._client: Optional[NativeClient] = None
        self._store: Optional[StateStore] = None
        self._native: Optional[NativeConfig] = None
        self._lifecycle_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._self_id = ""
        self._ready = False
        self._status = "尚未连接"
        self._stopping = False
        self._pending_source: Dict[str, Dict[str, str]] = {}
        self._admin_client: Optional[NativeClient] = None
        self._tools = WechatNativeTools(self)
    async def on_load(self) -> None:
        """初始化持久化状态并按配置启动接收任务。"""

        self._store = StateStore(self.ctx.paths.data_dir / "wechat-native.sqlite3")
        if self.config.plugin.enabled:
            self._start()
        await self._sync_tool_visibility()

    async def _sync_tool_visibility(self) -> None:
        """按配置启用或禁用面向模型的工具。

        工具开关只影响是否出现在模型可用列表里，实际执行时仍会再校验一次权限。
        """

        native: Optional[NativeConfig] = None
        try:
            native = NativeConfig.from_settings(
                self.config.native, self.config.chat, self.config.media, self.config.tools, self.config.actions
            )
        except ValueError:
            logger.info("WeChat Native 配置不完整，工具保持关闭")
        enabled = set(tool_names(native))
        for name in ALL_TOOL_NAMES:
            try:
                if name in enabled:
                    await self.ctx.component.enable_component(name, "tool")
                else:
                    await self.ctx.component.disable_component(name, "tool")
            except Exception as error:
                logger.warning("调整微信工具可见性失败: tool=%s error=%s", name, error)

    async def on_unload(self) -> None:
        """停止后台任务并释放连接。"""

        async with self._lifecycle_lock:
            await self._stop()

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置保存后按新参数重启接收任务。

        Args:
            scope: 配置变更范围，``self`` 表示当前插件。
            config_data: 新配置数据。
            version: 配置版本。
        """

        del version
        if scope != "self":
            return
        async with self._lifecycle_lock:
            await self._stop()
            self.set_plugin_config(config_data)
            if self.config.plugin.enabled:
                self._start()
            await self._sync_tool_visibility()

    async def get_config_widget_state(self) -> Dict[str, ConfigWidgetState]:
        """返回插件管理页展示的连接状态。

        Returns:
            Dict[str, ConfigWidgetState]: 各配置控件的状态。
        """

        configured = bool(str(self.config.native.server_url or "").strip()) and bool(
            str(self.config.native.account_id or "").strip()
        )
        text = (
            f"已连接：{self._status}"
            if self._ready
            else self._status
            if configured
            else "请先填写 Native 服务地址与 Account ID"
        )
        return {
            "connection": ConfigWidgetState(text=text, tone="success" if self._ready else "neutral"),
            "account": ConfigWidgetState(text=self._self_id or "尚未识别微信账号"),
            "reconnect": ConfigWidgetState(disabled=not configured),
        }

    async def on_config_widget_action(self, action_id: str) -> None:
        """处理配置页操作按钮。

        Args:
            action_id: 被触发的控件 ID。

        Raises:
            ValueError: 收到未知控件 ID。
        """

        if action_id != "reconnect":
            raise ValueError(f"未知配置页操作: {action_id}")
        async with self._lifecycle_lock:
            await self._stop()
            if self.config.plugin.enabled:
                self._start()

    def _require_store(self) -> StateStore:
        """返回已初始化的状态存储。

        Returns:
            StateStore: 状态存储实例。

        Raises:
            RuntimeError: 插件尚未加载完成。
        """

        if self._store is None:
            raise RuntimeError("WeChat Native 状态存储尚未初始化")
        return self._store

    def _start(self) -> None:
        """校验配置并启动接收任务。"""

        try:
            native = NativeConfig.from_settings(
                self.config.native, self.config.chat, self.config.media, self.config.tools, self.config.actions
            )
            native.load_token()
            admin_token = native.load_admin_token()
        except ValueError as error:
            self._status = f"配置无效：{error}"
            logger.error("WeChat Native 配置无效: %s", error)
            return
        self._native = native
        if admin_token:
            self._admin_client = NativeClient(native.api_url, native.account_id, admin_token)
        self._stopping = False
        self._status = "等待连接 Native"
        self._worker = asyncio.create_task(self._run(), name="wechat-native-adapter")

    async def _stop(self) -> None:
        """取消后台任务并关闭客户端。"""

        self._stopping = True
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        client = self._client
        self._client = None
        if client is not None:
            with suppress(Exception):
                await client.close()
        admin = self._admin_client
        self._admin_client = None
        if admin is not None:
            if not admin.closed:
                with suppress(Exception):
                    await admin.close()
        self._ready = False

    async def _set_ready(self, ready: bool, status: str = "") -> None:
        """向宿主上报网关就绪状态。

        Args:
            ready: 当前链路是否可用。
            status: 展示给用户的连接描述。
        """

        if status:
            self._status = status
        native = self._native
        if native is None:
            return
        accepted = await self.ctx.gateway.update_state(
            GATEWAY_NAME,
            ready=ready,
            platform=PLATFORM,
            account_id=native.account_id,
            metadata={"connection": "wechat_native"},
        )
        self._ready = ready and accepted
        if ready and not accepted:
            raise RuntimeError("麦麦未接受 WeChat Native 网关路由")

    async def _run(self) -> None:
        """保持事件流连接，断线后按退避间隔重连。"""

        native = self._native
        if native is None:
            return
        store = self._require_store()
        delay = native.reconnect_delay_seconds
        try:
            client = NativeClient(native.api_url, native.account_id, native.load_token())
            self._client = client
            await client.open()
            if self._admin_client is not None:
                await self._admin_client.open()
            while not self._stopping:
                try:
                    account = await client.account()
                    if account.get("id") != native.account_id:
                        raise ValueError("account_identity_mismatch")
                    self._self_id = str(account.get("ownUsername") or native.account_id)
                    if not account.get("authenticated") or account.get("connection") != "online":
                        await self._set_ready(False, "Native 账号尚未在线，等待微信登录")
                    else:
                        await self._set_ready(True, f"{self._self_id}（微信已在线）")
                    async for event in client.events(store.cursor):
                        await self._process_event(event, store)
                        delay = native.reconnect_delay_seconds
                    if not self._stopping:
                        logger.warning("WeChat Native 事件流已断开，使用已保存游标重连")
                except NativeAPIError as error:
                    if error.status in {401, 403, 410}:
                        await self._set_ready(False, f"Native 拒绝连接（HTTP {error.status}）")
                        logger.error("WeChat Native 停止接收: HTTP %s (%s)", error.status, error.code)
                        raise
                    await self._set_ready(False, f"Native 暂时不可达（HTTP {error.status}），等待重连")
                    logger.warning("WeChat Native 暂时不可达，将重连（HTTP %s）", error.status)
                except ValueError as error:
                    await self._set_ready(False, "消息身份校验失败，保留游标等待重试")
                    logger.error("WeChat Native 收到无效消息身份: %s", error)
                if not self._stopping:
                    await asyncio.sleep(delay)
                    delay = min(30, delay * 2)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._set_ready(False, f"连接失败：{error}")
            logger.error("WeChat Native 接收任务停止: %s", error, exc_info=True)
        finally:
            self._ready = False
            self._client = None
            with suppress(Exception):
                await client.close()
            admin = self._admin_client
            if admin is not None and not admin.closed:
                with suppress(Exception):
                    await admin.close()

    async def _process_event(self, event: Dict[str, Any], store: StateStore) -> None:
        """处理单条 Native 事件并推进游标。

        Args:
            event: Native 推送的事件对象。
            store: 状态存储。
        """

        cursor = event.get("cursor", "")
        try:
            await self._handle_event(event, store)
        except (NativeAPIError, SendError) as error:
            # 连接级错误不推进游标，重连后由 Native 重新投递。
            if isinstance(error, NativeAPIError) and error.status in {401, 403, 410}:
                raise
            logger.warning("WeChat Native 事件处理中断，保留游标: %s", error)
            raise
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "WeChat Native 单条事件处理失败，已跳过: cursor=%s error=%s", cursor, error, exc_info=True
            )
        if cursor:
            cursor_number(cursor)
            store.advance(str(cursor))

    async def _handle_event(self, event: Dict[str, Any], store: StateStore) -> None:
        """按事件类型分发消息、撤回与媒体更新。

        Args:
            event: Native 推送的事件对象。
            store: 状态存储。
        """

        kind = event.get("kind")
        if kind == "message.recalled":
            data = event.get("data") or {}
            store.recall(
                str(data.get("threadId") or ""),
                str(data.get("serverId") or "") or None,
                str(data.get("messageId") or "") or None,
            )
            return
        if kind not in {"message", "message.created", "message.received"}:
            return
        raw = event.get("data")
        if not isinstance(raw, dict) or not raw.get("threadId") or not raw.get("senderId"):
            return
        await self._receive_message(raw, store)

    async def _receive_message(self, raw: Dict[str, Any], store: StateStore) -> None:
        """把一条 Native 消息转换为麦麦消息并交给宿主路由。

        Args:
            raw: Native 消息对象。
            store: 状态存储。
        """

        native = self._native
        client = self._client
        if native is None or client is None:
            raise RuntimeError("WeChat Native 客户端未就绪")
        thread = str(raw["threadId"])
        sender = str(raw["senderId"])
        group = is_group_thread(thread)
        external_id = message_id(native.account_id, thread, str(raw["id"]))
        if store.message_status(external_id) is not None:
            return
        if group:
            if native.group_reply_mode == "disabled":
                return
            if native.allowed_groups and thread not in native.allowed_groups:
                return
            mentions = raw.get("mentions")
            mentioned = (
                isinstance(mentions, dict)
                and mentions.get("state") == "known"
                and mentions.get("self") is True
            )
            if native.group_reply_mode == "mention" and not mentioned:
                return
        elif not native.enable_private_reply:
            return
        elif native.allowed_users and sender not in native.allowed_users:
            return
        if sender == self._self_id:
            return
        body = raw["body"] if isinstance(raw.get("body"), str) else ""
        images: List[bytes] = []
        voice: Optional[bytes] = None
        pdf_name = ""
        pdf_text = ""
        if native.enable_media:
            images, voice, pdf_name, pdf_text = await self._load_media(native, raw, client)
        segments = segments_from_media(images, voice, pdf_name, pdf_text)
        text = body
        if not text.strip():
            if images:
                text = DEFAULT_IMAGE_PROMPT
            elif voice is not None:
                text = "[语音]"
            elif pdf_name:
                text = DEFAULT_PDF_PROMPT
        quote = raw.get("quote") if isinstance(raw.get("quote"), dict) else None
        message = inbound(
            raw,
            native.account_id,
            text=text,
            segments=segments,
            self_id=self._self_id,
            group_name=str(raw.get("groupName") or ""),
            quote=quote,
        )
        try:
            queue_message(self._store, native.account_id, raw, external_id)
        except StateIdentityError as error:
            # 同一条消息 ID 被复用于不同会话时，保留证据并要求人工检查，而不覆盖旧记录。
            logger.error("微信消息身份冲突，已保留原记录: %s (%s)", external_id, error)
            return
        self._pending_source[external_id] = {
            "threadId": thread,
            "senderId": sender,
            "messageId": str(raw["id"]),
            "serverId": str(raw.get("serverId") or ""),
        }
        accepted = await self.ctx.gateway.route_message(
            GATEWAY_NAME,
            message,
            external_message_id=external_id,
            dedupe_key=external_id,
        )
        store.mark_message(external_id, "queued" if accepted else "no_reply")
        if not accepted:
            logger.info("麦麦过滤或去重了微信消息: %s", external_id)

    async def _load_media(
        self, native: NativeConfig, raw: Dict[str, Any], client: NativeClient
    ) -> tuple[List[bytes], Optional[bytes], str, str]:
        """读取消息附带的图片、语音与 PDF。

        Args:
            native: 当前连接配置，用于媒体大小上限与表情开关。
            raw: Native 消息对象。
            client: Native 客户端。

        Returns:
            tuple[List[bytes], Optional[bytes], str, str]: 图片列表、语音数据、
            PDF 文件名与提取正文。
        """

        images: List[bytes] = []
        voice: Optional[bytes] = None
        pdf_name = ""
        pdf_text = ""
        if raw.get("type") not in {3, 34, 49} and not raw.get("media"):
            return images, voice, pdf_name, pdf_text
        try:
            body = await client.media_assets(str(raw["id"]))
        except (NativeAPIError, MediaError) as error:
            logger.info("微信消息无可用媒体: message=%s error=%s", raw.get("id"), error)
            return images, voice, pdf_name, pdf_text
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            return images, voice, pdf_name, pdf_text
        for item in items:
            if not isinstance(item, dict) or item.get("source") not in {None, "message"}:
                continue
            asset_id = str(item.get("id") or "")
            if not asset_id or item.get("state") == "recalled":
                continue
            kind = str(item.get("kind") or "")
            filename = str(item.get("fileName") or "")
            try:
                if kind == "image":
                    data, _ = await client.media_content(asset_id, max_bytes=native.max_image_bytes)
                    images.append(data)
                elif kind == "sticker":
                    if not native.enable_stickers:
                        continue
                    data, _ = await client.media_content(asset_id, preview=True)
                    images.append(data)
                elif kind == "voice":
                    data, _ = await client.media_content(asset_id, max_bytes=MAX_VOICE_BYTES)
                    voice = data
                elif kind == "file" and is_pdf_filename(filename):
                    data, _ = await client.media_content(asset_id, max_bytes=MAX_PDF_BYTES)
                    pdf_text = await asyncio.to_thread(extract_pdf_text, data)
                    pdf_name = filename
            except (NativeAPIError, MediaError) as error:
                logger.info("微信媒体读取失败: message=%s kind=%s error=%s", raw.get("id"), kind, error)
        return images, voice, pdf_name, pdf_text

    @Tool(
        "wechat_search_history",
        platforms=PLATFORM,
        visibility="visible",
        description=(
            "仅用于微信：搜索当前微信会话的历史消息。只能读取当前会话，不能跨聊天或跨群查询。"
            "留空 query 返回最近的若干条消息。返回内容可能不完整，较早的消息可能已被清理。"
        ),
        parameters=[
            ToolParameterInfo(name="query", param_type=ToolParamType.STRING, description="搜索关键词，留空表示最近消息"),
            ToolParameterInfo(name="limit", param_type=ToolParamType.INTEGER, description="返回条数，1-50，默认 20"),
        ],
    )
    async def tool_search_history(self, **kwargs: Any) -> Dict[str, Any]:
        """在工具调用中搜索当前微信会话的历史消息。"""

        try:
            stream = await self._tool_stream(kwargs)
            return await self._tools.search_history(stream["thread"], kwargs.get("query", ""), kwargs.get("limit", 20))
        except Exception as error:
            return describe_error(error)

    @Tool(
        "wechat_group_info",
        platforms=PLATFORM,
        visibility="visible",
        description=(
            "仅用于微信：读取当前微信群的资料，包括群名等公开信息。仅群聊可用，读取当前群而非任意群。"
        ),
    )
    async def tool_group_info(self, **kwargs: Any) -> Dict[str, Any]:
        """在工具调用中读取当前微信群资料。"""

        try:
            stream = await self._tool_stream(kwargs)
            return await self._tools.get_group_info(stream["thread"])
        except Exception as error:
            return describe_error(error)

    @Tool(
        "wechat_group_members",
        platforms=PLATFORM,
        visibility="visible",
        description=(
            "仅用于微信：列出或按昵称搜索当前微信群的成员。仅群聊可用，"
            "只能查询当前群，不能查询其他群或好友列表。"
        ),
        parameters=[
            ToolParameterInfo(name="search", param_type=ToolParamType.STRING, description="成员昵称关键词，留空表示列出成员"),
            ToolParameterInfo(name="limit", param_type=ToolParamType.INTEGER, description="返回条数，1-200，默认 50"),
        ],
    )
    async def tool_group_members(self, **kwargs: Any) -> Dict[str, Any]:
        """在工具调用中列出或搜索当前群成员。"""

        try:
            stream = await self._tool_stream(kwargs)
            return await self._tools.list_group_members(
                stream["thread"], kwargs.get("limit", 50), kwargs.get("search", "")
            )
        except Exception as error:
            return describe_error(error)

    @Tool(
        "wechat_search_contact",
        platforms=PLATFORM,
        visibility="visible",
        description=(
            "仅用于微信：按昵称或备注搜索微信联系人。只返回匹配结果，不会添加好友；"
            "添加好友需要对方先发起好友申请。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query", param_type=ToolParamType.STRING, description="昵称或备注关键词", required=True
            )
        ],
    )
    async def tool_search_contact(self, **kwargs: Any) -> Dict[str, Any]:
        """在工具调用中搜索微信联系人。"""

        try:
            return await self._tools.search_contact(kwargs.get("query", ""))
        except Exception as error:
            return describe_error(error)

    @Tool(
        "wechat_recall_message",
        platforms=PLATFORM,
        visibility="visible",
        description=(
            "仅用于微信：撤回机器人自己发送的一条消息。只能撤回机器人自己的消息，"
            "不能撤回他人消息。不传 msg_id 时撤回本会话中机器人最近发送的一条。"
            "注意微信没有禁言或踢人能力。"
        ),
        parameters=[
            ToolParameterInfo(name="msg_id", param_type=ToolParamType.STRING, description="目标消息 ID，留空表示最近一条"),
        ],
    )
    async def tool_recall_message(self, **kwargs: Any) -> Dict[str, Any]:
        """在工具调用中撤回机器人自己发送的消息。"""

        try:
            stream = await self._tool_stream(kwargs, require_group=True, admin=True)
            return await self._tools.recall_message(stream["thread"], kwargs.get("msg_id", ""))
        except Exception as error:
            return describe_error(error)

    @Tool(
        "wechat_set_my_group_nickname",
        platforms=PLATFORM,
        visibility="visible",
        description=(
            "仅用于微信：修改机器人在当前群里的群昵称。只影响机器人自己的群昵称，"
            "不能修改他人昵称，也不能用于私聊。"
        ),
        parameters=[
            ToolParameterInfo(
                name="nickname", param_type=ToolParamType.STRING, description="新的群昵称", required=True
            )
        ],
    )
    async def tool_set_my_group_nickname(self, **kwargs: Any) -> Dict[str, Any]:
        """在工具调用中修改机器人的群昵称。"""

        try:
            stream = await self._tool_stream(kwargs, require_group=True, admin=True)
            return await self._tools.set_my_group_nickname(stream["thread"], kwargs.get("nickname", ""))
        except Exception as error:
            return describe_error(error)

    @Tool(
        "wechat_rename_group",
        platforms=PLATFORM,
        visibility="visible",
        description=(
            "仅用于微信：修改当前微信群的群名称。会影响群内所有成员，属于高影响操作，"
            "仅在用户明确要求改群名时调用。不能用于私聊或其他群。"
        ),
        parameters=[
            ToolParameterInfo(name="name", param_type=ToolParamType.STRING, description="新的群名称", required=True)
        ],
    )
    async def tool_rename_group(self, **kwargs: Any) -> Dict[str, Any]:
        """在工具调用中修改当前群名称。"""

        try:
            stream = await self._tool_stream(kwargs, require_group=True, admin=True)
            return await self._tools.rename_group(stream["thread"], kwargs.get("name", ""))
        except Exception as error:
            return describe_error(error)

    @Tool(
        "wechat_list_friend_requests",
        platforms=PLATFORM,
        visibility="visible",
        description=(
            "仅用于微信：列出他人发给机器人的好友申请。只读取申请列表，不会自动通过；"
            "是否接受需要用单独的工具并经过用户确认。"
        ),
        parameters=[
            ToolParameterInfo(name="search", param_type=ToolParamType.STRING, description="申请人关键词，可选"),
        ],
    )
    async def tool_list_friend_requests(self, **kwargs: Any) -> Dict[str, Any]:
        """在工具调用中列出好友申请。"""

        try:
            await self._tool_stream(kwargs, admin=True)
            return await self._tools.list_contact_requests(kwargs.get("search", ""))
        except Exception as error:
            return describe_error(error)

    @Tool(
        "wechat_accept_friend_request",
        platforms=PLATFORM,
        visibility="visible",
        description=(
            "仅用于微信：接受一条好友申请。微信没有拒绝申请的接口，不接受时不要调用本工具。"
            "该操作会建立好友关系，仅在用户明确同意后调用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="request_id", param_type=ToolParamType.STRING, description="好友申请 ID", required=True
            )
        ],
    )
    async def tool_accept_friend_request(self, **kwargs: Any) -> Dict[str, Any]:
        """在工具调用中接受一条好友申请。"""

        try:
            await self._tool_stream(kwargs, admin=True)
            return await self._tools.accept_contact_request(kwargs.get("request_id", ""))
        except Exception as error:
            return describe_error(error)

    async def _tool_stream(
        self, kwargs: Dict[str, Any], *, require_group: bool = False, admin: bool = False
    ) -> Dict[str, str]:
        """解析当前工具调用所属的微信会话并校验权限。

        Args:
            kwargs: 宿主传入的工具参数，含当前聊天上下文。
            require_group: 是否要求当前会话是群聊。
            admin: 是否要求发起者在管理员白名单内。

        Returns:
            Dict[str, str]: 会话信息，至少包含 ``thread`` 与 ``sender``。

        Raises:
            ValueError: 当前会话不属于微信或不是群聊。
        """

        native = self._native
        if native is None:
            raise ToolUnavailable("微信适配器尚未完成初始化")
        if kwargs.get("platform") and kwargs["platform"] != PLATFORM:
            raise ValueError("该工具仅适用于微信会话")
        thread = str(kwargs.get("group_id") or kwargs.get("target_id") or "").strip()
        sender = str(kwargs.get("user_id") or "").strip()
        if not thread:
            thread = str(
                (kwargs.get("message_info") or {}).get("additional_config", {}).get("wechat_thread_id") or ""
            ).strip()
        if require_group and thread and not is_group_thread(thread):
            raise ValueError("该工具仅支持微信群聊")
        if require_group and not thread:
            raise ValueError("无法从当前会话确定微信群 ID")
        if admin:
            self._tools.check_admin(sender)
        return {"thread": thread, "sender": sender}

    @MessageGateway(
        name=GATEWAY_NAME,
        route_type="duplex",
        platform=PLATFORM,
        protocol="agent_wechat_native_v1",
        description="微信 Native 私聊与群聊的文本、图片、语音、@ 与引用双向消息网关",
    )
    async def send_to_platform(
        self, message: Dict[str, Any], route: Optional[Dict[str, Any]] = None, **kwargs: Any
    ) -> Dict[str, Any]:
        """把麦麦出站消息发送到微信。

        Args:
            message: 麦麦标准出站消息字典。
            route: 宿主提供的路由信息。
            **kwargs: 其余网关参数。

        Returns:
            Dict[str, Any]: 发送结果。
        """

        del route, kwargs
        async with self._send_lock:
            native = self._native
            client = self._client
            if not self._ready or native is None or client is None:
                return {"success": False, "error": "微信网关尚未就绪，请等待 Native 账号上线"}
            try:
                if message.get("platform") != PLATFORM:
                    raise ValueError("出站消息的平台与微信网关不一致")
                if message.get("account_id") and message["account_id"] != native.account_id:
                    raise ValueError("出站消息不属于当前微信账号")
                target, parts, mentions, reply_id = outbound(
                    message,
                    allow_group=native.group_reply_mode != "disabled",
                    max_image_bytes=native.max_image_bytes,
                )
                source = self._pending_source.get(self._reply_source_key(message))
                mentions, reply_id, quote = await self._apply_reply_style(
                    native, client, target, parts, source, mentions, reply_id
                )
                sent: List[str] = []
                for kind, content in parts:
                    key = secrets.token_hex(16)
                    if kind == "text":
                        await client.send_text(
                            target,
                            content,
                            key,
                            {"userIds": mentions} if mentions else None,
                            quote=quote,
                        )
                    elif kind == "image":
                        await client.send_image(
                            target,
                            content,
                            _image_content_type(content),
                            key,
                            record_job=lambda _job: None,
                            before_send=_noop_async,
                        )
                    else:
                        await client.send_voice(
                            target,
                            content,
                            _voice_content_type(content),
                            key,
                            record_job=lambda _job: None,
                            before_send=_noop_async,
                            record_chunks=lambda _chunks: None,
                        )
                    sent.append(key)
                last = sent[-1] if sent else ""
                self._mark_replied(message)
                return {"success": True, "external_message_id": last, "metadata": {"send_keys": sent}}
            except (NativeAPIError, SendError, MediaError, ValueError) as error:
                logger.warning("微信出站失败: %s", error)
                return {"success": False, "error": str(error)}

    def _reply_source_key(self, message: Dict[str, Any]) -> str:
        """找出触发本次回复的入站消息 ID。

        Args:
            message: 麦麦出站消息字典。

        Returns:
            str: 匹配 ``self._pending_source`` 的键；找不到时返回空字符串。
        """

        info = message.get("message_info") or {}
        group_info = info.get("group_info") or {}
        thread = str(group_info.get("group_id") or "").strip()
        if not thread:
            user_info = info.get("user_info") or {}
            thread = str(
                (info.get("additional_config") or {}).get("platform_io_target_user_id")
                or user_info.get("user_id")
                or ""
            ).strip()
        if not thread:
            return ""
        for key in reversed(list(self._pending_source)):
            if self._pending_source[key]["threadId"] == thread:
                return key
        return ""

    async def _apply_reply_style(
        self,
        native: NativeConfig,
        client: NativeClient,
        target: str,
        parts: List[Any],
        source: Optional[Dict[str, str]],
        mentions: List[str],
        reply_id: Optional[str],
    ) -> tuple[List[str], Optional[str], Optional[Dict[str, str]]]:
        """按配置为群聊首条文字回复补充引用或 @。

        Args:
            native: 当前连接配置。
            client: Native 客户端。
            target: 目标会话 ID。
            parts: 待发送的消息段。
            source: 触发本次回复的入站消息。
            mentions: 取消息段中的 @ 列表。
            reply_id: 取消息段中的引用目标。

        Returns:
            tuple[List[str], Optional[str], Optional[Dict[str, str]]]: @ 列表、
            引用消息 ID 与 Native 引用参数。
        """

        del client
        quote = {"messageId": reply_id, "serverId": reply_id} if reply_id else None
        if not is_group_thread(target) or not source or native.group_reply_style == "plain":
            return mentions, reply_id, quote
        if mentions or reply_id:
            return mentions, reply_id, quote
        has_text = any(part[0] == "text" for part in parts)
        if not has_text:
            return mentions, reply_id, quote
        if native.group_reply_style == "quote":
            message_id = source.get("messageId")
            server_id = source.get("serverId")
            if message_id and server_id:
                reply_id = message_id
                quote = {"messageId": message_id, "serverId": server_id}
            return mentions, reply_id, quote
        sender = source.get("senderId")
        if sender and sender != self._self_id:
            mentions = [sender]
        return mentions, reply_id, quote

    def _mark_replied(self, message: Dict[str, Any]) -> None:
        """把触发本次回复的入站消息标记为已回复。

        Args:
            message: 出站消息字典。
        """

        store = self._store
        native = self._native
        if store is None or native is None:
            return
        info = message.get("message_info") or {}
        group_info = info.get("group_info") or {}
        thread = str(group_info.get("group_id") or "").strip()
        if not thread:
            user_info = info.get("user_info") or {}
            thread = str(
                (info.get("additional_config") or {}).get("platform_io_target_user_id")
                or user_info.get("user_id")
                or ""
            ).strip()
        if not thread:
            return
        pending = store.latest_pending_message(native.account_id, thread)
        if pending:
            store.mark_message(pending, "replied")


async def _noop_async() -> None:
    """Native 媒体发送前的占位回调。"""


def queue_message(store: StateStore, account: str, raw: Dict[str, Any], external_id: str) -> None:
    """把入站消息写入状态库，供去重与回复追踪使用。

    Args:
        store: 状态存储。
        account: Native 账号 ID。
        raw: Native 消息对象。
        external_id: 适配器内部消息 ID。

    Raises:
        StateIdentityError: 同一条消息 ID 被重复用于不同身份。
    """

    thread = str(raw["threadId"])
    server_id = str(raw.get("serverId") or raw.get("id") or "")
    store.accept(store.cursor, external_id, thread, server_id, "queued")
def _image_content_type(data: bytes) -> str:
    """根据文件头判断图片 MIME。

    Args:
        data: 图片二进制。

    Returns:
        str: 图片 MIME 类型。

    Raises:
        ValueError: 不是受支持的图片格式。
    """

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("不支持的图片格式，请转换为 PNG/JPEG/GIF/WebP")


def _voice_content_type(data: bytes) -> str:
    """根据文件头判断语音 MIME。

    Args:
        data: 语音二进制。

    Returns:
        str: 语音 MIME 类型。

    Raises:
        ValueError: 不是受支持的音频格式。
    """

    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"#!AMR"):
        return "audio/amr"
    if data.startswith(b"\x02#!SILK") or data.startswith(b"#!SILK"):
        return "audio/silk"
    if data.startswith(b"ID3") or data[:2] in {b"\xff\xfb", b"\xff\xf3"}:
        return "audio/mpeg"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    raise ValueError("不支持的语音格式，请转换为 WAV/MP3/OGG/SILK")


def create_plugin() -> WeChatNativePlugin:
    """创建插件实例。

    Returns:
        WeChatNativePlugin: 适配器插件实例。
    """

    return WeChatNativePlugin()

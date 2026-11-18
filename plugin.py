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
import time

from .codec import (
    DEFAULT_IMAGE_PROMPT,
    DEFAULT_PDF_PROMPT,
    PLATFORM,
    inbound,
    is_group_thread,
    message_id,
    outbound,
    parse_timestamp,
    segments_from_media,
    split_message_id,
)
from .config import MAX_PDF_BYTES, MAX_VOICE_BYTES, NativeConfig
from .login import LoginSession, poll_login, start_login
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
GROUP_NAME_TTL_SECONDS = 600.0
PENDING_SOURCE_LIMIT = 512
CURSOR_PROBE_SECONDS = 4.0
TIMESTAMP_TOLERANCE_BEHIND = 60.0
TIMESTAMP_TOLERANCE_AHEAD = 300.0
TIME_WARNING_INTERVAL = 60.0
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
        ConfigWidget(id="qrcode", kind="qrcode", label="微信扫码登录"),
        ConfigWidget(id="start_login", kind="action", label="生成登录二维码"),
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
        self._group_names: Dict[str, tuple[str, float]] = {}
        self._last_time_warning = 0.0
        self._member_names: Dict[str, tuple[str, float]] = {}
        self._admin_client: Optional[NativeClient] = None
        self._tools = WechatNativeTools(self)
        self._login = LoginSession()
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

        login_task = self._login.task
        if login_task is not None and not login_task.done():
            login_task.cancel()
            with suppress(asyncio.CancelledError):
                await login_task
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
        """返回插件管理页展示的连接状态与扫码二维码。

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
        login = self._login
        qr_state = ConfigWidgetState(
            text=login.message,
            tone=login.tone,
            image=login.qr_image or None,
            expires_at=login.expires_at or None,
        )
        return {
            "connection": ConfigWidgetState(text=text, tone="success" if self._ready else "neutral"),
            "account": ConfigWidgetState(text=self._self_id or "尚未识别微信账号"),
            "qrcode": qr_state,
            "start_login": ConfigWidgetState(
                text="" if login.running() else "",
                disabled=login.running() or not self._login_client_ready(),
            ),
            "reconnect": ConfigWidgetState(disabled=not configured),
        }

    def _login_client_ready(self) -> bool:
        """判断当前是否可以发起扫码登录。

        Returns:
            bool: 已填写服务地址与 Account ID 时返回 ``True``。
        """

        return bool(str(self.config.native.server_url or "").strip()) and bool(
            str(self.config.native.account_id or "").strip()
        )

    async def _handle_start_login(self) -> None:
        """发起扫码登录并开始轮询状态。"""

        if not self._login_client_ready():
            raise ValueError("请先填写 Native 服务地址与 Account ID，再生成二维码")
        try:
            native = NativeConfig.from_settings(
                self.config.native, self.config.chat, self.config.media, self.config.tools, self.config.actions
            )
            # 扫码登录需要 login scope；日常 Token 通常具备该权限，admin Token 仅用于群管理等写操作。
            token = native.load_token()
        except ValueError as error:
            raise ValueError(f"登录配置无效：{error}") from error
        client = NativeClient(native.api_url, native.account_id, token)
        await client.open()
        try:
            session = await start_login(client)
        except NativeAPIError as error:
            await client.close()
            if error.status == 409 and error.code == "operation_in_progress":
                raise ValueError(
                    "该微信账号已有正在进行的登录操作；若账号已在线则无需扫码，"
                    "如需重新登录请先在 Native 侧断开该账号"
                ) from error
            raise ValueError(f"Native 拒绝发起登录（HTTP {error.status}，{error.code or 'unknown'}）") from error
        except Exception:
            await client.close()
            raise
        old = self._login
        if old.task is not None and not old.task.done():
            old.task.cancel()
        self._login = session

        async def run() -> None:
            try:
                await poll_login(client, session)
            finally:
                await client.close()

        session.task = asyncio.create_task(run(), name="wechat-native-login")

    async def on_config_widget_action(self, action_id: str) -> None:
        """处理配置页操作按钮。

        Args:
            action_id: 被触发的控件 ID。

        Raises:
            ValueError: 收到未知控件 ID。
        """

        if action_id == "start_login":
            await self._handle_start_login()
            return
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
            self._status = f"未启动：{error}"
            logger.error("WeChat Native 配置无效，适配器未启动: %s", error)
            return
        self._native = native
        if admin_token:
            self._admin_client = NativeClient(native.api_url, native.account_id, admin_token)
        self._stopping = False
        self._status = "等待连接 Native"
        logger.info(
            "WeChat Native 适配器启动: account=%s admin=%s",
            native.account_id,
            "yes" if admin_token else "no",
        )
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
            logger.error("WeChat Native 接收任务缺少配置，无法启动")
            return
        store = self._require_store()
        delay = native.reconnect_delay_seconds
        logger.info("WeChat Native 开始接收事件: cursor=%s", store.cursor)
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
                    await self._guard_cursor(client, store)
                    await self._ensure_cursor_in_window(client, store)
                    async for event in client.events(store.cursor):
                        await self._process_event(event, store)
                        delay = native.reconnect_delay_seconds
                    if not self._stopping:
                        logger.warning("WeChat Native 事件流已断开，使用已保存游标重连")
                except NativeAPIError as error:
                    if error.status in {401, 403}:
                        await self._set_ready(False, f"Native 拒绝连接（HTTP {error.status}）")
                        logger.error("WeChat Native 停止接收: HTTP %s (%s)", error.status, error.code)
                        raise
                    if error.status == 410:
                        # 事件日志被服务端重置后旧游标永久失效，必须回到最新位置，
                        # 否则会一直卡在同一个 410 上再也收不到消息。
                        await self._reset_cursor(client, store)
                        await self._set_ready(False, "事件游标已过期，已重置到最新位置")
                        logger.warning("WeChat Native 事件游标过期，已重置到最新位置")
                    else:
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

    async def _guard_cursor(self, client: NativeClient, store: StateStore) -> None:
        """确认保存的事件游标仍可订阅。

        Native 的事件流是滚动窗口，游标失效时会返回 410。
        这里只做一次探测，避免带着失效游标进入订阅后静默空转。

        Args:
            client: 已连接的 Native 客户端。
            store: 状态存储。

        Raises:
            NativeAPIError: 游标已失效（410）时向上抛出，由外层重置游标。
        """

        await client.events_probe(store.cursor)

    async def _ensure_cursor_in_window(self, client: NativeClient, store: StateStore) -> None:
        """确认保存的游标仍能读到事件。

        Native 的事件流是滚动窗口：游标远大于窗口上界时不会返回 410，
        而是直接给出一条都读不到的空流，导致适配器静默收不到任何消息。

        注意不能用“窗口最大游标”判断越界：``after=0`` 只会按顺序推送，
        短时间读取到的最大游标远小于真实上界，据此判断会误判。

        Args:
            client: 已连接的 Native 客户端。
            store: 状态存储。
        """

        if await self._cursor_has_events(client, store.cursor):
            return
        if not await self._cursor_has_events(client, "0"):
            # 服务端本身没有事件可读，不是游标问题。
            return
        current = store.cursor
        logger.warning("微信事件游标 %s 已读不到事件，回到事件窗口起点", current)
        store.advance_reset("0")

    async def _cursor_has_events(self, client: NativeClient, cursor: str) -> bool:
        """判断从指定游标能否读到至少一条事件。

        Args:
            client: 已连接的 Native 客户端。
            cursor: 起始游标。

        Returns:
            bool: 读到事件返回 ``True``。
        """

        stream = client.events(cursor)
        try:
            async with asyncio.timeout(CURSOR_PROBE_SECONDS):
                async for _ in stream:
                    return True
        except (TimeoutError, NativeAPIError, asyncio.CancelledError):
            return False
        finally:
            await stream.aclose()
        return False

    async def _reset_cursor(self, client: NativeClient, store: StateStore) -> None:
        """把失效的游标重置到服务端当前最新位置。

        Native 的事件日志被重置后旧游标永久失效，继续使用只会反复得到 410。
        这里直接读取事件流当前可见的最大游标并写入状态库。

        Args:
            client: 已连接的 Native 客户端。
            store: 状态存储。
        """

        latest = await client.latest_cursor()
        if latest is None:
            logger.warning("微信事件游标重置失败：无法读取服务端最新游标")
            return
        store.advance_reset(str(latest))
        logger.warning("微信事件游标已重置到 %s", latest)

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

    async def _handle_event(self, event: Dict[str, Any], store: StateStore) -> None:
        """按事件类型分发消息、撤回与媒体更新。

        参照上游实现：非消息事件直接推进游标；消息事件要求 origin 为 live
        才算真正新收到，并用 accept 一次性完成去重与游标推进。

        Args:
            event: Native 推送的事件对象。
            store: 状态存储。
        """

        kind = event.get("kind")
        cursor = str(event.get("cursor") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if not cursor or cursor_number(cursor) <= cursor_number(store.cursor):
            return
        if kind == "message.recalled":
            store.recall(
                str(data.get("threadId") or ""),
                str(data.get("serverId") or "") or None,
                str(data.get("messageId") or "") or None,
            )
            store.advance(cursor)
            return
        if kind != "message.committed":
            store.advance(cursor)
            return
        message_id = str(data.get("messageId") or "").strip()
        thread = str(data.get("threadId") or "").strip()
        if not message_id or not thread:
            store.advance(cursor)
            return
        client = self._client
        if client is None:
            raise RuntimeError("WeChat Native 客户端未就绪")
        raw = await client.message(message_id)
        if (
            raw.get("id") != message_id
            or raw.get("accountId") != self._native.account_id
            or raw.get("threadId") != thread
        ):
            raise ValueError("微信消息身份不一致")
        # 只有 live 才是刚收到的消息；补同步的历史消息不应触发回复。
        received = (
            data.get("origin") == "live"
            and raw.get("direction") == "incoming"
            and raw.get("senderId") != self._self_id
        )
        if not received:
            store.accept(cursor, message_id, thread, raw.get("serverId"), "ignored", received=False)
            return
        accepted = await self._receive_message(raw, store, cursor=cursor)
        if not accepted:
            store.accept(cursor, message_id, thread, raw.get("serverId"), "ignored", received=False)

    async def _receive_message(
        self, raw: Dict[str, Any], store: StateStore, *, cursor: str = ""
    ) -> bool:
        """把一条 Native 消息转换为麦麦消息并交给宿主路由。

        Args:
            raw: Native 消息对象。
            store: 状态存储。
            cursor: 事件游标，传入时由本方法完成去重与推进。

        Returns:
            bool: 消息是否被交给宿主路由。
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
            return False
        # 事件流积压或游标重置后补同步的旧消息不应触发回复；
        # 这类消息很密集，日志按间隔限流，避免刷屏掩盖真正的问题。
        if not _timestamp_acceptable(raw):
            now = time.monotonic()
            if now - self._last_time_warning >= TIME_WARNING_INTERVAL:
                self._last_time_warning = now
                logger.warning(
                    "微信实时消息因时间异常被忽略: message=%s createdAt=%s（补同步的历史消息属正常）",
                    raw.get("id"),
                    raw.get("createdAt"),
                )
            return False
        if group:
            if native.group_reply_mode == "disabled":
                return False
            if native.allowed_groups and thread not in native.allowed_groups:
                return False
            mentions = raw.get("mentions")
            mentioned = (
                isinstance(mentions, dict)
                and mentions.get("state") == "known"
                and mentions.get("self") is True
            )
            # 群聊里别人随手发的图片、文件与语音不是对机器人的请求，
            # 只有被 @ 时才处理，否则会被无关内容触发回复。
            raw_type = raw.get("type")
            quote = raw.get("quote") if isinstance(raw.get("quote"), dict) else {}
            direct_media = raw_type in {3, 34} or (
                raw_type == 49 and raw.get("appMessageType") == 6
            )
            quoted_media = raw_type == 49 and raw.get("appMessageType") == 57 and bool(quote)
            if (direct_media or quoted_media) and not mentioned:
                return False
            if native.group_reply_mode == "mention" and not mentioned:
                return False
        elif not native.enable_private_reply:
            return False
        elif native.allowed_users and sender not in native.allowed_users:
            return False
        if sender == self._self_id:
            return False
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
        group_name = str(raw.get("groupName") or "")
        if group and not group_name:
            group_name = await self._group_name(client, thread)
        if not str(raw.get("senderName") or "").strip():
            # 部分历史消息不带 senderName，用群成员资料补昵称，避免把 wxid 当名字。
            nickname = await self._member_name(client, thread, sender)
            if nickname:
                raw = {**raw, "senderName": nickname}
        message = inbound(
            raw,
            native.account_id,
            text=text,
            segments=segments,
            self_id=self._self_id,
            group_name=group_name,
            quote=quote,
        )
        try:
            if cursor:
                accepted = store.accept(
                    cursor, external_id, thread, raw.get("serverId"), "queued"
                )
                if not accepted:
                    return False
            else:
                queue_message(self._store, native.account_id, raw, external_id)
        except StateIdentityError as error:
            # 同一条消息 ID 被复用于不同会话时，保留证据并要求人工检查，而不覆盖旧记录。
            logger.error("微信消息身份冲突，已保留原记录: %s (%s)", external_id, error)
            return False
        self._pending_source[external_id] = {
            "threadId": thread,
            "senderId": sender,
            "messageId": str(raw["id"]),
            "serverId": str(raw.get("serverId") or ""),
        }
        # 只保留最近的一批触发消息：宿主可能长时间不回复，
        # 不设上限会让这张表随运行时间无限增长。
        while len(self._pending_source) > PENDING_SOURCE_LIMIT:
            self._pending_source.pop(next(iter(self._pending_source)))
        accepted = await self.ctx.gateway.route_message(
            GATEWAY_NAME,
            message,
            external_message_id=external_id,
            dedupe_key=external_id,
        )
        if accepted:
            store.mark_message(external_id, "queued")
        else:
            store.mark_message(external_id, "no_reply")
            logger.info("麦麦过滤或去重了微信消息: %s", external_id)
        return True

    async def _load_quoted_media(
        self,
        native: NativeConfig,
        raw: Dict[str, Any],
        quoted: Dict[str, Any],
        client: NativeClient,
    ) -> tuple[List[bytes], Optional[bytes], str, str]:
        """下载被引用消息里的媒体。

        引用预览只给标记不给正文，必须按 ``serverId`` 反查同会话的原消息，
        校验身份后再读取媒体，避免把别的会话内容混进来。

        Args:
            native: 当前连接配置。
            raw: 引用方消息。
            quoted: 引用预览对象。
            client: Native 客户端。

        Returns:
            tuple[List[bytes], Optional[bytes], str, str]: 图片、语音、PDF 文件名与正文。
        """

        empty: tuple[List[bytes], Optional[bytes], str, str] = ([], None, "", "")
        thread = str(raw.get("threadId") or "")
        server_id = str(quoted.get("serverId") or "")
        if not thread or not server_id:
            return empty
        store = self._store
        if store is None:
            return empty
        original_id = store.find_message_id(thread, server_id)
        if original_id is None:
            logger.info("引用消息的原消息不在记录范围内，无法读取媒体: server=%s", server_id)
            return empty
        try:
            original = await client.message(original_id)
        except (NativeAPIError, MediaError) as error:
            logger.info("读取引用原消息失败: message=%s error=%s", original_id, error)
            return empty
        if (
            original.get("id") != original_id
            or original.get("threadId") != thread
            or original.get("senderId") != quoted.get("senderId")
            or original.get("serverId") != server_id
            or original.get("recall") is not None
        ):
            logger.info("引用原消息身份不一致或已撤回，跳过媒体: message=%s", original_id)
            return empty
        return await self._load_media(native, original, client)

    async def _group_name(self, client: NativeClient, thread: str) -> str:
        """读取并缓存微信群名称。

        Native 的消息对象不带群名，只能查群资料；每条消息都查会明显变慢，
        因此按会话缓存一段时间。

        Args:
            client: Native 客户端。
            thread: 群会话 ID。

        Returns:
            str: 群名称；读取失败时返回空字符串。
        """

        cached = self._group_names.get(thread)
        now = asyncio.get_running_loop().time()
        if cached is not None and now - cached[1] < GROUP_NAME_TTL_SECONDS:
            return cached[0]
        try:
            info = await client.group(thread)
        except (NativeAPIError, MediaError) as error:
            logger.info("读取微信群资料失败: thread=%s error=%s", thread, error)
            return cached[0] if cached is not None else ""
        name = str(info.get("name") or info.get("displayName") or "").strip()
        if name and not is_group_thread(name):
            self._group_names[thread] = (name, now)
            return name
        # 群名未同步时不要用 chatroom ID 冒充名称，让宿主自己回退。
        self._group_names[thread] = ("", now)
        return ""

    async def _member_name(self, client: NativeClient, thread: str, user_id: str) -> str:
        """按用户 ID 查群昵称或昵称。

        Args:
            client: Native 客户端。
            thread: 群会话 ID；私聊时传空字符串。
            user_id: 微信用户 ID。

        Returns:
            str: 昵称；查不到时返回空字符串。
        """

        if not user_id:
            return ""
        cached = self._member_names.get(user_id)
        now = asyncio.get_running_loop().time()
        if cached is not None and now - cached[1] < GROUP_NAME_TTL_SECONDS:
            return cached[0]
        name = ""
        if thread and is_group_thread(thread):
            try:
                page = await client.group_members(thread, limit=200)
            except (NativeAPIError, MediaError) as error:
                logger.info("读取微信群成员失败: thread=%s error=%s", thread, error)
                page = {}
            for item in page.get("items") or []:
                if not isinstance(item, dict):
                    continue
                if str(item.get("username") or item.get("id") or "") != user_id:
                    continue
                name = str(
                    item.get("roomDisplayName") or item.get("displayName") or ""
                ).strip()
                break
        self._member_names[user_id] = (name, now)
        return name

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
        quoted = raw.get("quote")
        if (
            raw.get("type") == 49
            and raw.get("appMessageType") == 57
            and isinstance(quoted, dict)
            and quoted.get("type") in {3, 34, 49}
        ):
            # 被引用的是一条媒体消息：引用预览里没有内容，必须回到原消息下载。
            quoted_images, quoted_voice, quoted_name, quoted_text = await self._load_quoted_media(
                native, raw, quoted, client
            )
            images.extend(quoted_images)
            if quoted_voice is not None:
                voice = quoted_voice
            if quoted_name:
                pdf_name = quoted_name
                pdf_text = quoted_text
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
                    pdf_name = filename
                    try:
                        pdf_text = await asyncio.to_thread(extract_pdf_text, data)
                    except (ValueError, MediaError) as error:
                        # 提取失败也要让模型知道收到的是 PDF，而不是完全没有反应。
                        logger.info("PDF 正文提取失败: message=%s error=%s", raw.get("id"), error)
                        pdf_text = ""
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
            "仅用于微信：撤回机器人自己发送的一条消息。私聊和群聊都可以用，"
            "但只能撤回机器人自己的消息，不能撤回他人消息。"
            "微信只允许撤回发送后 2 分钟内的消息，超时会失败。"
            "msg_id 使用聊天记录中的消息 ID；不传时撤回当前会话里机器人最近发送的一条。"
            "注意微信没有禁言或踢人能力。"
        ),
        parameters=[
            ToolParameterInfo(name="msg_id", param_type=ToolParamType.STRING, description="目标消息 ID，留空表示最近一条"),
        ],
    )
    async def tool_recall_message(self, **kwargs: Any) -> Dict[str, Any]:
        """在工具调用中撤回机器人自己发送的消息。"""

        try:
            self._tools.check_admin(str(kwargs.get("user_id") or ""))
            thread, message_id = await self._resolve_recall_target(kwargs)
            return await self._tools.recall_message(thread, message_id)
        except Exception as error:
            return describe_error(error)

    async def _resolve_recall_target(self, kwargs: Dict[str, Any]) -> tuple[str, str]:
        """解析撤回目标的消息与会话。

        参照 QQ 适配器：优先用宿主消息记录反查消息所属会话，
        只有在拿不到消息时才退回当前聊天上下文。

        Args:
            kwargs: 工具调用参数。

        Returns:
            tuple[str, str]: ``(会话 ID, 消息 ID)``，均可能为空。
        """

        message_id = str(kwargs.get("msg_id") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "")
        thread = str(kwargs.get("group_id") or kwargs.get("target_id") or "").strip()
        if message_id:
            try:
                record = await self.ctx.message.get_by_id(
                    message_id, stream_id=stream_id, include_binary_data=False
                )
            except Exception as error:  # noqa: BLE001 - 查不到时改用消息自身解析
                logger.info("按消息 ID 反查微信会话失败，改用消息自身解析: %s", error)
            else:
                info = record.get("message_info") if isinstance(record, dict) else None
                if isinstance(info, dict):
                    group_info = info.get("group_info")
                    if isinstance(group_info, dict) and group_info.get("group_id"):
                        thread = str(group_info["group_id"])
                    else:
                        extra = info.get("additional_config") or {}
                        thread = str(
                            extra.get("wechat_thread_id")
                            or extra.get("platform_io_target_user_id")
                            or thread
                        ).strip()
        if not thread:
            thread = str(kwargs.get("wechat_thread_id") or "").strip()
        if not thread and message_id:
            # 会话可以由消息 ID 自身还原，交给工具层处理。
            thread = split_message_id(message_id)[0]
        return thread, message_id

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

        宿主只在群聊时传 ``group_id``，私聊只有 ``stream_id``（麦麦会话 ID），
        因此拿不到微信会话时必须按会话反查，否则私聊工具会全部失效。

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
        if not thread:
            thread = await self._thread_from_stream(kwargs, sender)
        if require_group and thread and not is_group_thread(thread):
            raise ValueError("该工具仅支持微信群聊")
        if require_group and not thread:
            raise ValueError("无法从当前会话确定微信群 ID")
        if admin:
            self._tools.check_admin(sender)
        return {"thread": thread, "sender": sender}

    async def _thread_from_stream(self, kwargs: Dict[str, Any], sender: str) -> str:
        """按麦麦会话 ID 反查真实的微信会话 ID。

        宿主的工具上下文只有 ``stream_id``，它对应麦麦的聊天流而不是微信 threadId。
        这里用 chat 能力把聊天流还原成微信群 ID 或好友 wxid。

        Args:
            kwargs: 工具调用参数。
            sender: 发起工具的微信用户 ID。

        Returns:
            str: 微信会话 ID；无法确定时返回空字符串。
        """

        stream_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "").strip()
        if not stream_id:
            return ""
        chat = getattr(self.ctx, "chat", None)
        if chat is None:
            return ""
        try:
            streams = await chat.get_all_streams(platform=PLATFORM)
        except Exception as error:  # noqa: BLE001 - 反查失败由调用方给出提示
            logger.info("按会话反查微信聊天流失败: %s", error)
            return ""
        for item in _iter_streams(streams):
            session_id = str(item.get("session_id") or item.get("stream_id") or "")
            if session_id != stream_id:
                continue
            group_id = str(item.get("group_id") or "").strip()
            if group_id:
                return group_id
            user_id = str(item.get("user_id") or "").strip()
            if user_id:
                return user_id
        if sender:
            # 私聊流的目标就是发起者本人，用它作为微信会话 ID。
            return sender
        return ""

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
            sent: List[str] = []
            external_ids: List[str] = []
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
                if mentions == ["all"] and not await self._can_mention_all(client, target):
                    raise ValueError(
                        "当前微信群不支持 @全体成员：机器人不是群主或管理员，或群设置已关闭该权限"
                    )
                for kind, content in parts:
                    key = secrets.token_hex(16)
                    if kind == "text":
                        receipt = await client.send_text(
                            target,
                            content,
                            key,
                            _mentions_payload(mentions),
                            quote=quote,
                        )
                    elif kind == "image":
                        receipt = await client.send_image(
                            target,
                            content,
                            _image_content_type(content),
                            key,
                            record_job=lambda _job: None,
                            before_send=_noop_async,
                        )
                    else:
                        receipt = await client.send_voice(
                            target,
                            content,
                            _voice_content_type(content),
                            key,
                            record_job=lambda _job: None,
                            before_send=_noop_async,
                            record_chunks=lambda _chunks: None,
                        )
                    sent.append(key)
                    # 宿主用这个 ID 回写自己的消息记录，撤回工具依赖它定位消息，
                    # 因此必须是 Native 的真实消息 ID，不能是幂等键。
                    # 群 @ 消息的受理回执不带 localMessageId，需要按幂等键回查。
                    message_id = _receipt_message_id(receipt)
                    if not message_id:
                        message_id = await self._lookup_message_id(client, key)
                    external_ids.append(message_id)
                last = next((value for value in reversed(external_ids) if value), "")
                self._mark_replied(message)
                return {"success": True, "external_message_id": last, "metadata": {"send_keys": sent}}
            except (NativeAPIError, SendError, MediaError, ValueError) as error:
                # 多段消息是逐条发送的：中途失败时前面的段落已经在微信里了，
                # 必须如实说明，否则用户会以为整条消息没发出去。
                logger.warning("微信出站失败: %s", error)
                if sent:
                    return {
                        "success": False,
                        "error": f"已发送 {len(sent)} 段后失败: {error}",
                        "external_message_id": next(
                            (value for value in reversed(external_ids) if value), ""
                        ),
                        "metadata": {"send_keys": sent},
                    }
                return {"success": False, "error": str(error)}

    async def _can_mention_all(self, client: NativeClient, thread: str) -> bool:
        """判断当前群是否允许 @全体成员。

        微信只允许群主或管理员 @全体成员，Native 在无权时返回 403。
        提前查群资料可以给出明确原因，而不是让用户看到裸的 HTTP 错误。

        Args:
            client: Native 客户端。
            thread: 群会话 ID。

        Returns:
            bool: 允许时返回 ``True``。
        """

        try:
            info = await client.group(thread)
        except (NativeAPIError, MediaError) as error:
            logger.info("读取群资料以判断 @全体成员 权限失败: thread=%s error=%s", thread, error)
            return False
        return info.get("canMentionAll") is True

    async def _lookup_message_id(self, client: NativeClient, key: str) -> str:
        """按幂等键回查已发送消息的真实 ID。

        部分发送（例如群聊 @）的受理回执不带 ``localMessageId``，
        只能按幂等键反查发送记录补回消息 ID。

        Args:
            client: Native 客户端。
            key: 发送时使用的幂等键。

        Returns:
            str: 消息 ID；查不到时返回空字符串。
        """

        # 消息落库有时间差，回查不到时短暂重试几次。
        for attempt in range(3):
            try:
                record = await client.send_by_key(key)
            except (NativeAPIError, SendError) as error:
                logger.info("按幂等键回查微信发送记录失败: %s", error)
                return ""
            if isinstance(record, dict):
                message_id = _receipt_message_id(record)
                if message_id:
                    return message_id
            if attempt < 2:
                await asyncio.sleep(0.3)
        return ""

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
        # Native 引用要求 messageId 与 serverId 是两个不同的值；
        # 只拿到 reply_id 时无法拼出合法的引用参数，直接放弃引用。
        quote = None
        if not is_group_thread(target) or not source or native.group_reply_style == "plain":
            return mentions, reply_id, quote
        if mentions or reply_id:
            return mentions, reply_id, quote
        has_text = any(part[0] == "text" for part in parts)
        if not has_text:
            return mentions, reply_id, quote
        if native.group_reply_style == "quote":
            message_id = str(source.get("messageId") or "")
            server_id = str(source.get("serverId") or "")
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
        # 触发消息已经用掉，清掉对应记录，避免后续回复误用陈旧引用。
        source_key = self._reply_source_key(message)
        if source_key:
            self._pending_source.pop(source_key, None)


def _mentions_payload(mentions: List[str]) -> Optional[Dict[str, Any]]:
    """把出站的 @ 列表翻译成 Native 的 mentions 结构。

    Args:
        mentions: 用户 ID 列表；``["all"]`` 表示 @全体成员。

    Returns:
        Optional[Dict[str, Any]]: Native mentions；没有 @ 时返回 ``None``。
    """

    if not mentions:
        return None
    if mentions == ["all"]:
        return {"all": True}
    return {"userIds": mentions}


def _timestamp_acceptable(raw: Dict[str, Any]) -> bool:
    """判断消息时间戳是否落在可回复窗口内。

    参照原版 AstrBot 插件：允许 -60 秒到 +300 秒的偏差，
    超出范围说明事件流积压或本机时钟异常。

    Args:
        raw: Native 消息对象。

    Returns:
        bool: 时间戳可接受时返回 ``True``。
    """

    stamp = parse_timestamp(raw)
    if stamp is None:
        return False
    delta = time.time() - stamp
    return -TIMESTAMP_TOLERANCE_BEHIND <= delta <= TIMESTAMP_TOLERANCE_AHEAD


def _receipt_message_id(receipt: Any) -> str:
    """从 Native 发送回执里取出真实消息 ID。

    文本发送把消息 ID 放在顶层，媒体发送则放在 ``result.messageIds`` 里。

    Args:
        receipt: ``send_text`` / ``send_image`` / ``send_voice`` 的返回值。

    Returns:
        str: 消息 ID；取不到时返回空字符串。
    """

    if not isinstance(receipt, dict):
        return ""
    for key in ("localMessageId", "messageId"):
        value = str(receipt.get(key) or "").strip()
        if value:
            return value
    result = receipt.get("result")
    if isinstance(result, dict):
        message_ids = result.get("messageIds")
        if isinstance(message_ids, list):
            for item in message_ids:
                value = str(item or "").strip()
                if value:
                    return value
        value = str(result.get("messageId") or "").strip()
        if value:
            return value
    return ""


async def _noop_async() -> None:
    """Native 媒体发送前的占位回调。"""


def _iter_streams(value: Any) -> List[Dict[str, Any]]:
    """把宿主返回的聊天流结果整理成字典列表。

    宿主成功时直接返回聊天流列表，失败时返回带 ``success=False`` 的信封。

    Args:
        value: ``ctx.chat.get_all_streams`` 的返回值。

    Returns:
        List[Dict[str, Any]]: 聊天流字典列表。

    Raises:
        RuntimeError: 宿主明确返回查询失败。
    """

    if isinstance(value, dict):
        if value.get("success") is False:
            raise RuntimeError(f"查询微信聊天流失败: {value.get('error')}")
        for key in ("streams", "items", "data"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


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

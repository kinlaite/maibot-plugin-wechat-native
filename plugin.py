"""微信 Native 适配器：通过 agent-wechat-native /v1 API 接入个人微信。"""

from contextlib import suppress
from typing import Any, ClassVar, Dict, List, Optional, Type

from maibot_sdk import ConfigWidget, ConfigWidgetState, MaiBotPlugin, MessageGateway, PluginConfigBase

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

GATEWAY_NAME = "wechat_native_gateway"
MEDIA_RETRY_SECONDS = 3


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

    async def on_load(self) -> None:
        """初始化持久化状态并按配置启动接收任务。"""

        self._store = StateStore(self.ctx.paths.data_dir / "wechat-native.sqlite3")
        if self.config.plugin.enabled:
            self._start()

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
            native = NativeConfig.from_settings(self.config.native, self.config.chat, self.config.media)
            native.load_token()
        except ValueError as error:
            self._status = f"配置无效：{error}"
            logger.error("WeChat Native 配置无效: %s", error)
            return
        self._native = native
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
                quote = None
                if reply_id:
                    quote = {"messageId": reply_id, "serverId": reply_id}
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

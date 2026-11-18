"""WeChat Native 面向模型的工具实现。

所有工具都只在微信会话内生效，并严格限制在当前聊天：不跨会话读取、不写入
非目标群。写操作还需要 admin Token 与管理员白名单双重满足。
"""

from typing import Any, Dict, List, Optional

import asyncio
import secrets
import unicodedata

from .codec import is_group_thread, split_message_id
from .config import NativeConfig
from .log import logger
from .native_client import NativeAPIError, NativeClient

CONTACT_SEARCH_LIMIT = 100
RECALL_ALLOWED_STATUSES = {"success", "already_recalled", "not_recallable", "needs_review"}


class ToolUnavailable(RuntimeError):
    """工具在当前配置或身份下不可用。"""


def require_thread(thread: str) -> str:
    """校验群会话 ID 形态。

    Args:
        thread: 待校验的会话 ID。

    Returns:
        str: 校验通过的会话 ID。

    Raises:
        ValueError: 不是群会话 ID。
    """

    normalized = str(thread or "").strip()
    if not normalized or not is_group_thread(normalized):
        raise ValueError("该操作仅支持微信群聊，当前会话不是群聊")
    return normalized


def clamp_limit(value: Any, *, default: int, maximum: int) -> int:
    """把条数参数限制在合理范围内。

    Args:
        value: 模型传入的原始值。
        default: 缺省值。
        maximum: 上限。

    Returns:
        int: 归一化后的条数。
    """

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def clean_text(value: Any, *, label: str, maximum: int) -> str:
    """清理并校验用户提供的文本参数。

    Args:
        value: 原始值。
        label: 报错时使用的字段名。
        maximum: 字节上限。

    Returns:
        str: 去掉首尾空白后的文本。

    Raises:
        ValueError: 为空、超长或含控制字符。
    """

    if not isinstance(value, str):
        raise ValueError(f"{label}必须是字符串")
    text = value.strip()
    if not text:
        raise ValueError(f"{label}不能为空")
    if len(text.encode("utf-8")) > maximum:
        raise ValueError(f"{label}过长，最多 {maximum} 字节")
    if any(unicodedata.category(char) == "Cc" for char in text):
        raise ValueError(f"{label}不能包含控制字符")
    return text


class WechatNativeTools:
    """按当前会话与权限执行微信侧工具动作。"""

    def __init__(self, plugin: Any) -> None:
        """初始化工具集。

        Args:
            plugin: 拥有该工具集的适配器插件实例。
        """

        self._plugin = plugin

    @property
    def _config(self) -> NativeConfig:
        """当前连接配置。

        Returns:
            NativeConfig: 生效的连接配置。

        Raises:
            ToolUnavailable: 插件尚未完成初始化。
        """

        native = self._plugin._native
        if native is None:
            raise ToolUnavailable("微信适配器尚未完成初始化")
        return native

    def _client(self) -> NativeClient:
        """返回日常收发使用的客户端。

        Returns:
            NativeClient: 已连接的客户端。

        Raises:
            ToolUnavailable: 网关未就绪。
        """

        client = self._plugin._client
        if client is None or not self._plugin._ready:
            raise ToolUnavailable("微信网关尚未就绪，请等待 Native 账号上线")
        return client

    def _admin_client(self) -> NativeClient:
        """返回具备 admin scope 的客户端。

        Returns:
            NativeClient: 管理客户端。

        Raises:
            ToolUnavailable: 未配置 admin Token。
        """

        client = self._plugin._admin_client
        if client is None:
            raise ToolUnavailable("未配置 Native admin Token，写操作工具已关闭")
        return client

    def check_admin(self, sender_id: str) -> None:
        """确认发起者在管理员白名单内。

        Args:
            sender_id: 当前消息发送者的 platform 用户 ID。

        Raises:
            ToolUnavailable: 未开启写操作或发起者不在白名单。
        """

        native = self._config
        if not native.enable_wechat_actions:
            raise ToolUnavailable("写操作工具未开启，请先在插件配置中启用")
        if sender_id not in native.wechat_action_admins:
            raise ToolUnavailable("当前用户不在微信写操作管理员白名单内")

    async def recall_message(self, thread: str = "", message_id: str = "") -> Dict[str, Any]:
        """撤回机器人自己发送的消息。

        参照 QQ 适配器的做法：会话优先从目标消息自身解析，
        只有未指定消息时才退回按会话查找最近一条。

        Args:
            thread: 当前会话 ID，可为空。
            message_id: 目标消息 ID；留空表示最近一条自己发的消息。

        Returns:
            Dict[str, Any]: 操作结果。
        """

        native = self._config
        client = self._client()
        thread = str(thread or "").strip()
        target = str(message_id or "").strip()
        if target:
            # 宿主既可能传本适配器合成的 ``账号:会话:原生ID``，也可能传原生 ID，
            # 甚至可能传它自己的短 ID，三种都要能落到 Native 的消息上。
            embedded_thread, raw_id = split_message_id(target)
            thread = thread or embedded_thread
            record = await self._find_message(client, target, raw_id, thread)
            target = str(record.get("id") or "")
            resolved = str(record.get("threadId") or "").strip() or thread
            if not resolved:
                raise ValueError("目标消息缺少会话信息，无法撤回")
            if thread and resolved != thread:
                raise ValueError("目标消息不属于当前微信会话")
            thread = resolved
        else:
            if not thread:
                raise ValueError("请指定要撤回的消息，或从具体聊天中发起撤回")
            record = await self._latest_own_message(client, native, thread)
            target = str(record.get("id") or "")
        if record.get("accountId") != native.account_id:
            raise ValueError("目标消息不属于当前微信账号")
        if record.get("direction") != "outgoing":
            raise ValueError("只能撤回机器人自己发送的消息")
        allowed = native.recall_allowed_threads
        if allowed and thread not in allowed:
            raise ValueError(f"会话 {thread} 不在撤回白名单内，已拒绝撤回")
        if record.get("recall") is not None:
            return {"success": True, "status": "already_recalled", "message_id": target}
        server_id = str(record.get("serverId") or "")
        if not server_id.isdigit():
            raise ValueError("目标消息缺少可撤回的服务端 ID，可能已过期")
        # 撤回走日常 Token：Native 只要求 send/login 权限，admin Token 用于群名等写操作。
        operation = await client.recall_message(target, server_id, secrets.token_hex(16))
        status, detail = await self._await_recall(client, operation)
        if status == "recalled":
            self._plugin._store.recall(thread, server_id, target)
        return {
            "success": status == "recalled",
            "status": status,
            "message_id": target,
            "detail": detail,
        }

    async def _await_operation(
        self, client: NativeClient, operation: Dict[str, Any], *, label: str
    ) -> tuple[str, Dict[str, Any]]:
        """轮询 Native 写操作直到得出确定结论。

        Native 受理写操作只代表排队成功，必须等到 ``state == succeeded``
        且 ``remoteOutcome`` 为 accepted/confirmed 才算真正生效。

        Args:
            client: Native 客户端。
            operation: 写操作接口返回的操作对象。
            label: 操作名称，用于错误提示。

        Returns:
            tuple[str, Dict[str, Any]]: 状态与最后一次操作详情。

        Raises:
            ValueError: 操作被拒绝或结果无法确认。
        """

        operation_id = str(operation.get("id") or "")
        current = operation
        for _ in range(20):
            state = str(current.get("state") or "")
            payload = current.get("result") if isinstance(current.get("result"), dict) else {}
            remote = str(payload.get("remoteOutcome") or "")
            if state == "succeeded" and remote in {"accepted", "confirmed"}:
                return "succeeded", current
            if state == "failed":
                code = str(payload.get("code") or remote or "unknown")
                service_code = payload.get("serviceCode")
                status = str(payload.get("status") or "")
                if status == "not_eligible" or service_code == -430:
                    raise ValueError(
                        f"微信拒绝了{label}：只能撤回发送后 2 分钟内的消息，"
                        "超时后微信不再允许撤回"
                    )
                if remote in {"not_attempted", "rejected"}:
                    raise ValueError(f"微信未执行{label}（{code}）")
                raise ValueError(f"{label}失败（{code}）")
            if not operation_id:
                break
            await asyncio.sleep(0.5)
            try:
                current = await client.operation(operation_id)
            except NativeAPIError:
                break
        payload = current.get("result") if isinstance(current.get("result"), dict) else {}
        remote = str(payload.get("remoteOutcome") or "unknown")
        raise ValueError(f"{label}结果未确认（remoteOutcome={remote}），请稍后在微信中确认")

    async def _await_recall(
        self, client: NativeClient, operation: Dict[str, Any]
    ) -> tuple[str, Dict[str, Any]]:
        """轮询撤回操作直到得出确定结论。

        Native 受理撤回只代表排队成功，必须等到 remoteOutcome 为
        accepted/confirmed 才算真正撤回，否则可能只是本地记录变更。

        Args:
            client: Native 客户端。
            operation: 撤回接口返回的操作对象。

        Returns:
            tuple[str, Dict[str, Any]]: 状态与最后一次操作详情。
        """

        status, current = await self._await_operation(client, operation, label="撤回")
        payload = current.get("result") if isinstance(current.get("result"), dict) else {}
        if status == "succeeded" and payload.get("status") == "already_recalled":
            return "already_recalled", current
        return "recalled", current

    async def _find_message(
        self, client: NativeClient, given: str, raw_id: str, thread: str
    ) -> Dict[str, Any]:
        """把宿主给的消息 ID 落到一条真实的 Native 消息上。

        宿主可能把外部消息 ID 换成自己的短 ID，此时无法直接按 ID 查询，
        只能在当前会话里按 ID 后缀匹配。

        Args:
            client: Native 客户端。
            given: 调用方传入的完整消息 ID。
            raw_id: 从外部 ID 拆出的原生 ID；拆不出时等于 ``given``。
            thread: 已知的会话 ID，可能为空。

        Returns:
            Dict[str, Any]: Native 消息记录。

        Raises:
            ValueError: 找不到对应消息。
        """

        candidates = [value for value in (raw_id, given) if value]
        for candidate in candidates:
            try:
                record = await client.message(candidate)
            except NativeAPIError:
                continue
            if isinstance(record, dict) and record.get("id"):
                return record
        if thread:
            page = await client.messages(thread, limit=50)
            items = page.get("items") if isinstance(page, dict) else None
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                if given in str(item.get("id") or ""):
                    return item
        raise ValueError("找不到要撤回的微信消息，它可能已超出本地记录范围")

    async def _latest_own_message(
        self, client: NativeClient, native: NativeConfig, thread: str
    ) -> Dict[str, Any]:
        """查找当前会话中最近一条机器人自己发送的消息。

        Args:
            client: Native 客户端。
            native: 当前连接配置。
            thread: 会话 ID。

        Returns:
            Dict[str, Any]: 目标消息记录。

        Raises:
            ValueError: 没有可撤回的自发消息。
        """

        page = await client.messages(str(thread or "").strip(), limit=20)
        items = page.get("items") if isinstance(page, dict) else None
        for item in items or []:
            if not isinstance(item, dict) or item.get("deleted") is True:
                continue
            if item.get("threadId") != thread or item.get("direction") != "outgoing":
                continue
            return item
        raise ValueError("当前会话里没有机器人自己发送的可撤回消息")

    async def set_my_group_nickname(self, thread: str, nickname: str) -> Dict[str, Any]:
        """修改机器人在群里的昵称。

        Args:
            thread: 群会话 ID。
            nickname: 新昵称。

        Returns:
            Dict[str, Any]: 操作结果。
        """

        thread = require_thread(thread)
        name = clean_text(nickname, label="群昵称", maximum=512)
        client = self._admin_client()
        operation = await client.set_my_group_name(thread, name, secrets.token_hex(16))
        # 受理不等于生效，必须等到 Native 明确返回成功。
        await self._await_operation(client, operation, label="修改群昵称")
        return {"success": True, "group_id": thread, "nickname": name}

    async def rename_group(self, thread: str, name: str) -> Dict[str, Any]:
        """修改群名称。

        Args:
            thread: 群会话 ID。
            name: 新群名。

        Returns:
            Dict[str, Any]: 操作结果。
        """

        thread = require_thread(thread)
        group_name = clean_text(name, label="群名", maximum=512)
        client = self._admin_client()
        operation = await client.rename_group(thread, group_name, secrets.token_hex(16))
        await self._await_operation(client, operation, label="修改群名")
        return {"success": True, "group_id": thread, "name": group_name}

    async def get_group_info(self, thread: str) -> Dict[str, Any]:
        """读取群资料。

        Args:
            thread: 群会话 ID。

        Returns:
            Dict[str, Any]: 群资料。
        """

        thread = require_thread(thread)
        raw = await self._client().group(thread)
        return _project_group(raw)

    async def list_group_members(
        self, thread: str, limit: Any = 50, search: str = ""
    ) -> Dict[str, Any]:
        """列出或搜索群成员。

        Args:
            thread: 群会话 ID。
            limit: 返回条数上限。
            search: 成员昵称关键词。

        Returns:
            Dict[str, Any]: 成员列表与分页游标。
        """

        thread = require_thread(thread)
        raw = await self._client().group_members(
            thread, limit=clamp_limit(limit, default=50, maximum=200), search=str(search or "").strip()
        )
        return _project_members(raw)

    async def search_history(
        self, thread: str, query: str = "", limit: Any = 20
    ) -> Dict[str, Any]:
        """在当前会话的历史消息中搜索。

        Args:
            thread: 会话 ID，私聊或群聊。
            query: 搜索关键词；留空返回最近消息。
            limit: 返回条数上限。

        Returns:
            Dict[str, Any]: 消息列表与游标。
        """

        normalized = str(thread or "").strip()
        if not normalized:
            raise ValueError("当前会话缺少微信会话 ID，无法读取历史消息")
        raw = await self._client().messages(
            normalized, limit=clamp_limit(limit, default=20, maximum=50), query=str(query or "").strip()
        )
        return _project_messages(raw)

    async def search_contact(self, query: str) -> Dict[str, Any]:
        """按关键词搜索微信联系人。

        Args:
            query: 昵称或备注关键词。

        Returns:
            Dict[str, Any]: 匹配的联系人。
        """

        native = self._config
        if not native.enable_contact_search:
            raise ToolUnavailable("联系人搜索未开启")
        keyword = clean_text(query, label="搜索关键词", maximum=128)
        # Native 没有独立的搜索接口，用联系人列表的 query 参数过滤。
        raw = await self._client().contacts(limit=CONTACT_SEARCH_LIMIT, query=keyword)
        return _project_contacts(raw)

    async def list_contact_requests(self, search: str = "") -> Dict[str, Any]:
        """列出来自他人的好友申请。

        Args:
            search: 申请人关键词。

        Returns:
            Dict[str, Any]: 好友申请列表。
        """

        native = self._config
        if not native.enable_friend_requests:
            raise ToolUnavailable("好友申请处理未开启")
        return await self._admin_client().contact_requests(search=str(search or "").strip())

    async def accept_contact_request(self, request_id: str) -> Dict[str, Any]:
        """接受一条好友申请。

        Native 只提供接受接口，没有拒绝接口；不接受时不要调用本工具。

        Args:
            request_id: 好友申请 ID。

        Returns:
            Dict[str, Any]: 操作结果。
        """

        native = self._config
        if not native.enable_friend_requests:
            raise ToolUnavailable("好友申请处理未开启")
        identifier = clean_text(request_id, label="好友申请 ID", maximum=512)
        admin = self._admin_client()
        detail = await admin.contact_request(identifier)
        version = str(detail.get("version") or "")
        if not version:
            raise ValueError("该好友申请缺少版本信息，无法接受")
        operation = await admin.accept_contact_request(identifier, version, secrets.token_hex(16))
        await self._await_operation(admin, operation, label="接受好友申请")
        return {"success": True, "status": "accepted", "request_id": identifier}


def _project_contacts(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把联系人列表精简成模型需要的字段。

    Args:
        raw: Native 联系人响应。

    Returns:
        Dict[str, Any]: 精简后的联系人列表。
    """

    contacts: List[Dict[str, str]] = []
    for item in raw.get("items") or []:
        if not isinstance(item, dict):
            continue
        contacts.append(
            {
                "user_id": str(item.get("username") or item.get("id") or ""),
                "display_name": str(item.get("displayName") or ""),
                "remark": str(item.get("remark") or ""),
                "alias": str(item.get("alias") or ""),
            }
        )
    return {"contacts": contacts}


def _project_group(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把群资料精简成模型需要的字段。

    Native 的返回夹带数据库覆盖信息等内部元数据，直接交给模型既浪费上下文
    也容易泄露本机路径。

    Args:
        raw: Native 群资料响应。

    Returns:
        Dict[str, Any]: 精简后的群资料。
    """

    name = str(raw.get("name") or "").strip() or str(raw.get("remark") or "").strip()
    display = str(raw.get("displayName") or "").strip()
    return {
        "group_id": str(raw.get("id") or ""),
        "name": name or display,
        "owner_id": str(raw.get("ownerId") or ""),
        "announcement": str(raw.get("announcement") or ""),
        "my_display_name": str(raw.get("myDisplayName") or ""),
        "administrator_count": len(raw.get("administratorIds") or []),
    }


def _project_members(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把群成员列表精简成模型需要的字段。

    Args:
        raw: Native 群成员响应。

    Returns:
        Dict[str, Any]: 精简后的成员列表与分页游标。
    """

    members: List[Dict[str, str]] = []
    for item in raw.get("items") or []:
        if not isinstance(item, dict):
            continue
        members.append(
            {
                "user_id": str(item.get("username") or item.get("id") or ""),
                "display_name": str(item.get("displayName") or ""),
                "group_nickname": str(item.get("roomDisplayName") or ""),
            }
        )
    result: Dict[str, Any] = {"members": members}
    next_cursor = str(raw.get("nextCursor") or "")
    if next_cursor:
        result["next_cursor"] = next_cursor
    return result


def _project_messages(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把历史消息精简成模型需要的字段。

    Args:
        raw: Native 历史消息响应。

    Returns:
        Dict[str, Any]: 精简后的消息列表与分页游标。
    """

    messages: List[Dict[str, Any]] = []
    for item in raw.get("items") or []:
        if not isinstance(item, dict) or item.get("deleted") is True:
            continue
        messages.append(
            {
                "message_id": str(item.get("id") or ""),
                "sender_id": str(item.get("senderId") or ""),
                "direction": str(item.get("direction") or ""),
                "timestamp": str(item.get("createdAt") or ""),
                "type": item.get("type"),
                "text": str(item.get("body") or ""),
            }
        )
    result: Dict[str, Any] = {"messages": messages}
    next_cursor = str(raw.get("nextCursor") or "")
    if next_cursor:
        result["next_cursor"] = next_cursor
    return result


def describe_error(error: Exception) -> Dict[str, Any]:
    """把工具异常转换为模型可读的失败结果。

    Args:
        error: 捕获到的异常。

    Returns:
        Dict[str, Any]: 统一的失败结构。
    """

    if isinstance(error, NativeAPIError):
        logger.warning("微信工具调用失败: HTTP %s (%s)", error.status, error.code)
        return {"success": False, "error": f"微信接口返回错误（HTTP {error.status}，{error.code or 'unknown'}）"}
    if isinstance(error, (ToolUnavailable, ValueError)):
        return {"success": False, "error": str(error)}
    logger.error("微信工具调用异常: %s", error, exc_info=True)
    return {"success": False, "error": "微信工具调用失败，请查看日志"}


def tool_names(config: Optional[NativeConfig]) -> List[str]:
    """根据配置返回当前应暴露给模型的工具名。

    Args:
        config: 当前连接配置；未初始化时视为全部关闭。

    Returns:
        List[str]: 可见工具名列表。
    """

    if config is None:
        return []
    names: List[str] = []
    if config.enable_history_tools:
        names.extend(["wechat_search_history", "wechat_group_info", "wechat_group_members"])
    if config.enable_contact_search:
        names.append("wechat_search_contact")
    if config.enable_wechat_actions and config.wechat_action_admins:
        names.extend(
            [
                "wechat_recall_message",
                "wechat_set_my_group_nickname",
                "wechat_rename_group",
            ]
        )
    if config.enable_friend_requests:
        names.extend(["wechat_list_friend_requests", "wechat_accept_friend_request"])
    return names

"""微信扫码登录：通过 agent-wechat-native 的 login-attempts 接口完成扫码。

Native 的登录接口挂在具体账号下，所以扫码前必须已经存在账号。
二维码以 PNG 字节返回，由配置控件渲染。
"""

from typing import Any, Dict, Optional

import asyncio
import base64
import secrets
import time

from .log import logger
from .native_client import NativeAPIError, NativeClient

QR_TTL_SECONDS = 120
POLL_INTERVAL_SECONDS = 2
LOGIN_TIMEOUT_SECONDS = 600


class LoginSession:
    """一次扫码登录尝试的本地状态。"""

    def __init__(self) -> None:
        """初始化空会话。"""

        self.operation_id: str = ""
        self.state: str = ""
        self.qr_image: str = ""
        self.generation: int = 0
        self.expires_at: float = 0.0
        self.message: str = "尚未开始扫码"
        self.tone: str = "neutral"
        self.task: Optional[asyncio.Task[None]] = None
        self.idempotency_key: str = ""

    def reset(self) -> None:
        """清除当前尝试，保留可取消的任务句柄。"""

        self.operation_id = ""
        self.state = ""
        self.qr_image = ""
        self.generation = 0
        self.expires_at = 0.0
        self.message = "尚未开始扫码"
        self.tone = "neutral"

    def running(self) -> bool:
        """是否有正在进行的登录尝试。

        Returns:
            bool: 任务仍在运行且未完成时返回 ``True``。
        """

        return self.task is not None and not self.task.done()


async def start_login(client: NativeClient, session: Optional[LoginSession] = None) -> LoginSession:
    """发起一次扫码登录并取回首张二维码。

    Args:
        client: 已连接且具备登录权限的 Native 客户端。
        session: 调用方预先创建并已暴露给配置页的会话；省略时新建一个。

    Returns:
        LoginSession: 已填入二维码的会话。

    Raises:
        NativeAPIError: Native 拒绝发起登录。
        ValueError: 返回的二维码格式不符合预期。
    """

    if session is None:
        session = LoginSession()
    session.idempotency_key = secrets.token_hex(16)
    operation = await client.start_login(session.idempotency_key, force_qr=True)
    session.operation_id = str(operation.get("id") or "")
    session.state = str(operation.get("state") or "")
    if not session.operation_id:
        raise ValueError("Native 未返回登录操作 ID")
    await _apply_operation(session, operation, client)
    return session


async def _apply_operation(session: LoginSession, operation: Dict[str, Any], client: NativeClient) -> None:
    """把一次登录操作的结果同步到会话并抓取二维码。

    Args:
        session: 当前登录会话。
        operation: ``login_attempts`` 返回的操作对象。
        client: Native 客户端。
    """

    result = operation.get("result") if isinstance(operation.get("result"), dict) else {}
    session.state = str(operation.get("state") or "")
    if session.state == "awaiting_scan" and result.get("qrAvailable") is True:
        generation = int(result.get("qrGeneration") or 1)
        if generation != session.generation or not session.qr_image:
            image = await client.login_qr(session.operation_id, generation)
            session.generation = generation
            session.qr_image = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
            # 过期时间只跟二维码本身走：每次轮询都重设会让前端永远认为它有效，
            # 而微信侧其实已经换了码。
            session.expires_at = time.time() + QR_TTL_SECONDS
        session.message = "请用微信扫描二维码，并在手机上确认登录"
        session.tone = "neutral"
        return
    if session.state in {"succeeded", "approved", "confirmed"}:
        session.message = "登录成功，可以保存配置并启用适配器"
        session.tone = "success"
        session.qr_image = ""
        return
    if session.state in {"expired", "failed", "cancelled", "rejected"}:
        session.message = f"登录未完成（{session.state}），请重新生成二维码"
        session.tone = "warning"
        session.qr_image = ""
        return
    session.message = f"等待微信确认（{session.state}）"
    session.tone = "neutral"


async def poll_login(client: NativeClient, session: LoginSession) -> None:
    """轮询登录状态直到结束或超时。

    Args:
        client: Native 客户端。
        session: 当前登录会话。
    """

    deadline = time.time() + LOGIN_TIMEOUT_SECONDS
    try:
        while session.operation_id and time.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            try:
                operation = await client.login_status(session.operation_id)
            except NativeAPIError as error:
                session.message = f"查询登录状态失败（HTTP {error.status}），稍后重试"
                session.tone = "warning"
                continue
            await _apply_operation(session, operation, client)
            if session.state in {"succeeded", "approved", "confirmed", "expired", "failed", "cancelled", "rejected"}:
                return
        session.message = "扫码登录超时，请重新生成二维码"
        session.tone = "warning"
        session.qr_image = ""
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - 登录失败不应影响插件其它功能
        logger.error("微信扫码登录轮询失败: %s", error, exc_info=True)
        session.message = "扫码登录过程出错，请查看日志"
        session.tone = "error"

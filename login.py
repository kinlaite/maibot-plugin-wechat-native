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
FIRST_QR_TIMEOUT_SECONDS = 30
FIRST_QR_POLL_INTERVAL_SECONDS = 1
START_LOGIN_RETRY_DELAYS = (1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 20.0, 30.0)
"""发起登录被 ``operation_in_progress`` 拒绝时的退避梯度（秒）。

实测 Native 取消登录操作后约 2 秒释放账号；历史尝试堆积时会明显更久，
所以重试窗口要给到一分钟以上，而不是一次 409 就判失败。
"""

_TERMINAL_STATES = {"succeeded", "approved", "confirmed", "expired", "failed", "cancelled", "rejected"}


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
    await _release_stale_login(client, session)
    session.idempotency_key = secrets.token_hex(16)
    operation = await _start_with_retry(client, session)
    session.operation_id = str(operation.get("id") or "")
    session.state = str(operation.get("state") or "")
    if not session.operation_id:
        raise ValueError("Native 未返回登录操作 ID")
    await _apply_operation(session, operation, client)
    if not session.qr_image and session.state not in _TERMINAL_STATES:
        # Native 收到请求时通常是 starting，二维码要等它转到 awaiting_scan，
        # 这里等首张码就绪，避免配置页先显示“已发起”却没有图。
        await _await_first_qr(client, session)
    return session


async def _await_first_qr(client: NativeClient, session: LoginSession) -> None:
    """等待首张二维码就绪，供配置页立即显示。

    Args:
        client: Native 客户端。
        session: 已发起但还没有二维码的会话。
    """

    deadline = time.time() + FIRST_QR_TIMEOUT_SECONDS
    while session.operation_id and time.time() < deadline and not session.qr_image:
        await asyncio.sleep(FIRST_QR_POLL_INTERVAL_SECONDS)
        try:
            operation = await client.login_status(session.operation_id)
        except NativeAPIError as error:
            logger.debug("查询首张二维码失败（HTTP %s，%s）", error.status, error.code)
            return
        await _apply_operation(session, operation, client)
        if session.state in _TERMINAL_STATES:
            return


async def _start_with_retry(client: NativeClient, session: LoginSession) -> Dict[str, Any]:
    """发起登录；账号被残留操作占用时退避重试。

    实测 Native 取消登录操作后约 2 秒才释放账号，历史尝试堆积时更久，
    所以遇到 ``operation_in_progress`` 要等它收敛，而不是直接判失败。

    Args:
        client: Native 客户端。
        session: 当前登录会话，使用其中的 ``idempotency_key``。

    Returns:
        Dict[str, Any]: Native 返回的登录操作。

    Raises:
        NativeAPIError: 重试窗口内仍被拒绝，或遇到其它错误。
    """

    for index, delay in enumerate(START_LOGIN_RETRY_DELAYS, start=1):
        try:
            return await client.start_login(session.idempotency_key, force_qr=True)
        except NativeAPIError as error:
            if error.status != 409 or error.code != "operation_in_progress":
                raise
            session.message = (
                f"账号仍被上一次登录操作占用，等待释放后重试"
                f"（{index}/{len(START_LOGIN_RETRY_DELAYS)}）…"
            )
            session.tone = "neutral"
            logger.debug("账号仍被登录操作占用，%.1f 秒后重试", delay)
            await asyncio.sleep(delay)
    return await client.start_login(session.idempotency_key, force_qr=True)


async def _release_stale_login(client: NativeClient, session: LoginSession) -> None:
    """取消上一次尝试留下的登录操作，为新的尝试腾出位置。

    Arguments:
        client: Native 客户端。
        session: 当前会话；其中的 ``operation_id`` 是上一次尝试。
    """

    operation_id = session.operation_id
    if not operation_id:
        return
    try:
        await client.cancel_login(operation_id)
    except NativeAPIError as error:
        # 操作可能已经自己结束；交给后面的退避重试判断，不再额外补偿。
        logger.debug("取消上一次登录操作失败（HTTP %s，%s）", error.status, error.code)
    session.operation_id = ""


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
            if session.state in _TERMINAL_STATES:
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

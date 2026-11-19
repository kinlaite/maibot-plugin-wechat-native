"""微信扫码登录的二维码生命周期测试。

二维码过期时间必须跟着二维码本身走：轮询只刷新状态，不能每两秒
把过期时间往后推，否则前端会一直显示一张微信侧早已失效的图。
"""

from typing import Any, Dict

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wechat_native.login import (  # noqa: E402
    QR_TTL_SECONDS,
    LoginSession,
    _apply_operation,
    start_login,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class FakeClient:
    """按 generation 返回固定 PNG 的 Native 客户端替身。"""

    def __init__(self) -> None:
        self.requested: list[int] = []

    async def login_qr(self, operation_id: str, generation: int) -> bytes:
        self.requested.append(generation)
        return PNG


def _operation(generation: int) -> Dict[str, Any]:
    return {
        "state": "awaiting_scan",
        "result": {"qrAvailable": True, "qrGeneration": generation},
    }


def test_expiry_not_extended_when_generation_is_unchanged() -> None:
    """同一张二维码反复轮询时，过期时间不能被不断推后。"""

    session = LoginSession()
    session.operation_id = "op"
    client = FakeClient()

    asyncio.run(_apply_operation(session, _operation(1), client))
    first_expiry = session.expires_at
    assert first_expiry > 0
    assert session.qr_image.startswith("data:image/png;base64,")

    # 模拟两秒后的轮询：状态没变、码没换。
    asyncio.run(_apply_operation(session, _operation(1), client))
    assert session.expires_at == first_expiry
    # 同一 generation 不重复下载二维码。
    assert client.requested == [1]


def test_expiry_refreshed_when_generation_changes() -> None:
    """微信侧换码后要重新取图并重置过期时间。"""

    session = LoginSession()
    session.operation_id = "op"
    client = FakeClient()

    asyncio.run(_apply_operation(session, _operation(1), client))
    first_expiry = session.expires_at

    session.expires_at = first_expiry - QR_TTL_SECONDS / 2
    asyncio.run(_apply_operation(session, _operation(2), client))

    assert client.requested == [1, 2]
    assert session.generation == 2
    assert session.expires_at > first_expiry - QR_TTL_SECONDS / 2


def test_start_login_reuses_given_session() -> None:
    """调用方预建的会话必须被复用，配置页才能在后台任务里看到进度。

    配置页操作有 8 秒限制，申请二维码放进后台任务后立即返回；
    如果 ``start_login`` 自己新建会话，页面就拿不到任何状态。
    """

    class LoginClient:
        async def start_login(self, key: str, *, force_qr: bool = False) -> Dict[str, Any]:
            del key, force_qr
            return {"id": "op_1", "state": "awaiting_scan", "result": {"qrAvailable": True, "qrGeneration": 1}}

        async def login_qr(self, operation_id: str, generation: int) -> bytes:
            del operation_id, generation
            return PNG

    session = LoginSession()
    session.message = "正在向 Native 申请二维码…"

    returned = asyncio.run(start_login(LoginClient(), session))

    assert returned is session
    assert session.operation_id == "op_1"
    assert session.qr_image.startswith("data:image/png;base64,")
    assert session.expires_at > 0

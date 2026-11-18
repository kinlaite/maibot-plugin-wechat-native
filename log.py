"""插件日志入口；Runner 会接管标准 logging 输出。"""

import logging

logger = logging.getLogger("plugin.wechat_native")

__all__ = ["logger"]

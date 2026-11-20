"""Native 事件与麦麦消息字典之间的编解码。"""

from typing import Any, Dict, List, Optional, Tuple

import base64
import hashlib
import re
import time

import msgpack

from .config import MAX_TEXT_BYTES, MAX_VOICE_BYTES

PLATFORM = "wechat_native"
MAX_GATEWAY_MESSAGE_BYTES = 16 * 1024 * 1024 - 64 * 1024

MENTION_SUFFIX = "\u2005"


def is_group_thread(thread: str) -> bool:
    """判断 threadId 是否代表微信群聊。

    Args:
        thread: Native 事件中的 ``threadId``。

    Returns:
        bool: 群聊返回 ``True``。
    """

    return thread.endswith("@chatroom")


def message_id(account: str, thread: str, raw_id: str) -> str:
    """构造跨账号唯一的外部消息 ID。

    Args:
        account: Native 账号 ID。
        thread: 会话 ID。
        raw_id: Native 提供的消息 ID。

    Returns:
        str: 适配器内部使用的消息 ID。
    """

    return f"{account}:{thread}:{raw_id}"


def split_message_id(value: str) -> Tuple[str, str]:
    """把外部消息 ID 拆回 ``(thread, raw_id)``。

    宿主可能把消息 ID 换成自己生成的短 ID，因此拆不出会话时返回空 thread，
    由调用方改用消息自身反查。

    Args:
        value: 消息 ID，可能是本适配器合成的外部 ID，也可能是 Native 原始 ID。

    Returns:
        Tuple[str, str]: 会话 ID 与 Native 原始消息 ID，无法拆分时 thread 为空。
    """

    text = str(value or "").strip()
    if not text:
        return "", ""
    head, sep, rest = text.partition(":")
    if not sep:
        return "", text
    thread, sep, raw = rest.partition(":")
    if not sep or not thread or not raw:
        return "", text
    return thread, raw


def event_timestamp(raw: Dict[str, Any]) -> str:
    """把 Native 的时间戳转换为秒级字符串。

    Args:
        raw: Native 消息对象。

    Returns:
        str: 秒级浮点时间戳字符串。
    """

    stamp = parse_timestamp(raw)
    return str(stamp if stamp is not None else time.time())


def parse_timestamp(raw: Dict[str, Any]) -> Optional[float]:
    """解析 Native 消息的时间戳。

    Args:
        raw: Native 消息对象。

    Returns:
        Optional[float]: 秒级时间戳；无法解析时返回 ``None``。
    """

    value = raw.get("createdAt")
    if not isinstance(value, str):
        return None
    try:
        from datetime import datetime

        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    if stamp.tzinfo is None:
        return None
    return float(stamp.timestamp())


def split_mentions(text: str, user_ids: List[str]) -> List[Dict[str, Any]]:
    """按正文中 @ 标记的位置拆出 at 段与文本段。

    微信把 ``@昵称\\u2005`` 写在正文里，Native 的 ``mentions.userIds`` 又不保证
    能解析出目标。这里只做结构拆分，不改写正文：能配到 ID 的 @ 变成 at 段，
    配不到的保留为可读的 ``@昵称`` 文本，交给宿主按自己的规则渲染。

    Args:
        text: 原始消息正文。
        user_ids: Native 给出的被 @ 用户 ID，按出现顺序与 @ 标记配对。

    Returns:
        List[Dict[str, Any]]: 依次排列的 at 段与文本段；没有 @ 时只含一个文本段。
    """

    matches = list(re.finditer(rf"@([^{MENTION_SUFFIX}\r\n]+){MENTION_SUFFIX}", text))
    if not matches:
        return [{"type": "text", "data": text}] if text else []
    # 只有数量一致时才能逐项配对；否则连结构都不改，原样交给宿主，
    # 只把微信插入的不可见分隔符连同其后的空格换成一个空格。
    if len(user_ids) != len(matches):
        normalized = re.sub(rf"{MENTION_SUFFIX}[ \t]*", " ", text)
        return [{"type": "text", "data": normalized}] if normalized else []
    segments: List[Dict[str, Any]] = []

    def _append_text(value: str) -> None:
        if not value:
            return
        if segments and segments[-1]["type"] == "text":
            segments[-1]["data"] += value
            return
        segments.append({"type": "text", "data": value})

    cursor = 0
    for index, match in enumerate(matches):
        _append_text(text[cursor : match.start()])
        name = match.group(1).strip()
        segments.append(
            {
                "type": "at",
                "data": {
                    "target_user_id": user_ids[index],
                    "target_user_nickname": name or None,
                    "target_user_cardname": None,
                },
            }
        )
        cursor = match.end()
        # 微信在 @ 标记后紧跟的空格不属于正文，去掉避免出现双空格。
        while cursor < len(text) and text[cursor] in " \t":
            cursor += 1
    _append_text(text[cursor:])
    return segments


def _reply_segment(quote: Dict[str, Any], target_id: str = "") -> Optional[Dict[str, Any]]:
    """把 Native 的引用预览翻成宿主的 reply 段。

    Native 把被引用的内容放在 ``embeddedText`` / ``embeddedImage`` 里，
    不是 ``text``；读错字段会让引用内容整条丢失。

    Args:
        quote: Native 消息的 ``quote`` 对象。
        target_id: 已知的被引用消息 ID，未知时留空。

    Returns:
        Optional[Dict[str, Any]]: reply 段；引用内容完全不可读时返回 ``None``。
    """

    content = str(quote.get("embeddedText") or "").strip()
    if not content and quote.get("embeddedImage") is True:
        content = "[图片]"
    if not content and not target_id:
        return None
    return {
        "type": "reply",
        "data": {
            "target_message_id": target_id,
            "target_message_content": content or None,
            "target_message_sender_id": str(quote.get("senderId") or "") or None,
            "target_message_sender_nickname": str(quote.get("senderName") or "") or None,
            "target_message_sender_cardname": None,
        },
    }


def inbound(
    raw: Dict[str, Any],
    account: str,
    *,
    text: str,
    segments: List[Dict[str, Any]],
    self_id: str,
    group_name: str = "",
    quote: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把一条 Native 消息转换为麦麦标准消息字典。

    Args:
        raw: Native 消息对象。
        account: Native 账号 ID。
        text: 已处理的表情/媒体占位正文。
        segments: 媒体消息段列表。
        self_id: 机器人自身的微信 ID，用于识别 @。
        group_name: 群名称，私聊留空。
        quote: 引用消息描述，可选。

    Returns:
        Dict[str, Any]: 麦麦消息网关接受的标准消息字典。
    """

    thread = raw["threadId"]
    sender = raw["senderId"]
    group = is_group_thread(thread)
    mentions = raw.get("mentions")
    mention_ids: List[str] = []
    if isinstance(mentions, dict):
        raw_ids = mentions.get("userIds")
        if isinstance(raw_ids, list):
            mention_ids = [str(item).strip() for item in raw_ids if str(item or "").strip()]
    mentioned = group and (
        (isinstance(mentions, dict) and mentions.get("state") == "known" and mentions.get("self") is True)
        or bool(self_id and self_id in mention_ids)
    )
    raw_segments: List[Dict[str, Any]] = []
    # 引用是消息结构的一部分，必须作为 reply 段交给宿主；
    # 拼进正文会让模型把别人的话当成发言者自己说的内容。
    if quote is not None:
        reply = _reply_segment(quote, _quote_target_id(raw, quote))
        if reply is not None:
            raw_segments.append(reply)
    # @ 只在群聊里是结构信息：私聊正文里的 @ 就是用户自己打的字，不做解析。
    if group and MENTION_SUFFIX in text:
        raw_segments.extend(split_mentions(text, mention_ids))
    elif text:
        raw_segments.append({"type": "text", "data": text})
    raw_segments.extend(segments)
    nickname = str(raw.get("senderName") or "").strip() or str(sender)
    additional: Dict[str, Any] = {
        "wechat_thread_id": thread,
        "wechat_mentioned": bool(mentioned),
    }
    # 宿主私聊出站依赖 platform_io_target_user_id 决定接收者，群聊依赖
    # platform_io_target_group_id；缺了这两个字段，机器人无法主动发起会话。
    if group:
        additional["platform_io_target_group_id"] = thread
    else:
        additional["platform_io_target_user_id"] = thread
    info: Dict[str, Any] = {
        "user_info": {"user_id": str(sender), "user_nickname": nickname},
        "additional_config": additional,
    }
    if group:
        # 群名为空是明确语义：适配器不知道群名，宿主会保留自己记录的名称。
        # 不能拿 chatroom ID 冒充群名，否则会把 ID 写进宿主的聊天流名称。
        info["group_info"] = {"group_id": thread, "group_name": group_name or ""}
    message = {
        "platform": PLATFORM,
        "account_id": account,
        "message_id": message_id(account, thread, str(raw["id"])),
        "timestamp": event_timestamp(raw),
        "message_info": info,
        "raw_message": raw_segments,
        # 宿主靠这些顶层标志位判断群消息是否在 @ 机器人；
        # 缺失时 is_at 恒为 False，群聊消息会被当成「没叫我」而丢弃。
        "is_at": bool(mentioned),
        "is_mentioned": bool(mentioned),
        "is_picture": any(segment["type"] == "image" for segment in segments),
        "is_emoji": bool(raw.get("isEmoji"))
        or raw.get("type") == 47
        or any(segment["type"] == "emoji" for segment in segments),
        "is_command": text.startswith("/"),
    }
    if len(msgpack.packb(message, use_bin_type=True)) > MAX_GATEWAY_MESSAGE_BYTES:
        raise ValueError("消息超过麦麦网关的 16 MB 帧限制")
    return message


def _quote_target_id(raw: Dict[str, Any], quote: Dict[str, Any]) -> str:
    """取出被引用消息在本适配器内的消息 ID。

    宿主的 reply 段需要一个可回查的目标 ID。Native 的引用预览只给
    ``serverId``，因此按 ``账号:会话:serverId`` 还原成本适配器的消息 ID 形式，
    与 ``store.find_message_id`` 的匹配方式保持一致。

    Args:
        raw: 引用方消息。
        quote: 该消息的引用预览对象。

    Returns:
        str: 被引用消息 ID；缺少必要字段时返回空字符串。
    """

    server_id = str(quote.get("serverId") or "").strip()
    thread = str(raw.get("threadId") or "").strip()
    account = str(raw.get("accountId") or "").strip()
    if not server_id or not thread or not account:
        return ""
    return f"{account}:{thread}:{server_id}"


def _segment_binary(segment: Dict[str, Any], max_bytes: int, label: str) -> bytes:
    """解出消息段中的 base64 二进制。

    Args:
        segment: 麦麦消息段。
        max_bytes: 单段大小上限。
        label: 用于报错的类型名。

    Returns:
        bytes: 解码后的二进制。

    Raises:
        ValueError: 缺少数据、格式非法或超过上限。
    """

    value = segment.get("binary_data_base64") or ""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}缺少二进制数据")
    if len(value) > (max_bytes + 2) // 3 * 4:
        raise ValueError(f"{label}超过大小限制")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise ValueError(f"{label}的 base64 数据无效") from None
    if not raw or len(raw) > max_bytes:
        raise ValueError(f"{label}为空或超过大小限制")
    return raw


def outbound(
    message: Dict[str, Any], *, allow_group: bool, max_image_bytes: int
) -> Tuple[str, List[Tuple[str, Any]], List[str], Optional[str]]:
    """把麦麦出站消息转换为 Native 发送参数。

    文本与媒体按原顺序保留；@ 与本条消息的引用关系单独返回。

    Args:
        message: 麦麦出站消息字典。
        allow_group: 是否允许发往群聊。
        max_image_bytes: 单张图片大小上限。

    Returns:
        Tuple[str, List[Tuple[str, Any]], List[str], Optional[str]]: 目标会话、
        按顺序排列的发送段、@ 的用户 ID 列表与引用消息 ID。

    Raises:
        ValueError: 目标缺失、群聊被禁用或包含不支持的消息段。
    """

    info = message["message_info"]
    extra = info.get("additional_config") or {}
    group_info = info.get("group_info")
    if group_info:
        if not allow_group:
            raise ValueError("当前配置不允许发送群消息")
        target = str(group_info.get("group_id") or "").strip()
    else:
        # 私聊出站的 user_info 是机器人自身，真正的接收者在宿主的平台字段里。
        target = str(
            extra.get("platform_io_target_user_id") or extra.get("platform_io_target_group_id") or ""
        ).strip()
    if not target:
        raise ValueError("缺少出站目标会话 ID")
    parts: List[Tuple[str, Any]] = []
    mentions: List[str] = []
    all_members = False
    reply_id: Optional[str] = None
    for segment in message["raw_message"]:
        kind = segment.get("type")
        if kind == "text":
            text = segment.get("data")
            if not isinstance(text, str):
                raise ValueError("文本消息段格式无效")
            if text:
                # 微信单条文本上限 16 KiB，超出会整条发送失败；
                # 这里按字节切成多段，避免长回复直接丢失。
                parts.extend(("text", chunk) for chunk in _split_text(text))
        elif kind in {"image", "emoji"}:
            parts.append(("image", _segment_binary(segment, max_image_bytes, "图片")))
        elif kind == "voice":
            parts.append(("voice", _segment_binary(segment, MAX_VOICE_BYTES, "语音")))
        elif kind in {"at", "at_all"}:
            # 宿主的 at 段把目标放在 data 里，早期适配器写法是直接放在段上。
            at_data = segment.get("data")
            if isinstance(at_data, dict):
                target_user = str(at_data.get("target_user_id") or "").strip()
            else:
                target_user = str(segment.get("target_user_id") or "").strip()
            if kind == "at_all" or target_user in {"all", "全体", "everyone"}:
                all_members = True
            elif target_user and target_user not in mentions:
                mentions.append(target_user)
        elif kind == "reply":
            # 宿主的 reply 段把目标放在 data 里，早期适配器写法是直接放在段上。
            reply_data = segment.get("data")
            if isinstance(reply_data, dict):
                reply_target = str(reply_data.get("target_message_id") or "").strip()
            else:
                reply_target = str(reply_data or segment.get("target_message_id") or "").strip()
            reply_id = reply_target or reply_id
        else:
            raise ValueError(f"WeChat Native 暂不支持发送 {kind} 消息")
    if not parts:
        raise ValueError("没有可发送的消息内容")
    if (mentions or all_members) and not is_group_thread(target):
        mentions = []
        all_members = False
    if all_members and mentions:
        raise ValueError("不能同时 @全体成员 和 @个人")
    if all_members:
        # 用保留值表示 @全体成员，由发送层翻译成 Native 的 {"all": True}。
        mentions = ["all"]
    if parts and not any(part[0] == "text" for part in parts) and (mentions or reply_id):
        raise ValueError("Native 媒体接口不支持引用或 @，请在消息中添加文本承载这些信息")
    return target, parts, mentions, reply_id


def _split_text(text: str, max_bytes: int = MAX_TEXT_BYTES) -> List[str]:
    """把超长文本切成多条消息。

    微信单条文本上限是 16 KiB，超过后 Native 直接判失败。
    这里按 UTF-8 字节数切分，并保证不切断多字节字符；
    优先在换行处断开，让分段读起来更自然。

    Args:
        text: 原始文本。
        max_bytes: 单段字节上限。

    Returns:
        List[str]: 切分后的文本段；未超限时返回只含原文的列表。
    """

    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    chunks: List[str] = []
    remaining = text
    while remaining:
        if len(remaining.encode("utf-8")) <= max_bytes:
            chunks.append(remaining)
            break
        # 按字节上限折算出最多能放多少字符，再从那里往回找换行。
        cut = max_bytes
        while cut > 0 and len(remaining[:cut].encode("utf-8")) > max_bytes:
            cut -= 1
        if cut <= 0:
            cut = 1
        window = remaining[:cut]
        newline = window.rfind("\n")
        if newline > cut // 2:
            window = window[:newline]
        chunks.append(window)
        remaining = remaining[len(window):]
    return [chunk for chunk in chunks if chunk]


def segments_from_media(
    images: List[bytes],
    voice: Optional[bytes],
    pdf_name: str = "",
    pdf_text: str = "",
    stickers: Optional[List[bytes]] = None,
) -> List[Dict[str, Any]]:
    """构造图片、表情、语音与 PDF 的麦麦媒体消息段。

    Args:
        images: 图片二进制列表。
        voice: 语音二进制，可选。
        pdf_name: PDF 文件名。
        pdf_text: PDF 提取正文。
        stickers: 表情二进制列表，按图片发送但标记为表情。

    Returns:
        List[Dict[str, Any]]: 麦麦媒体消息段列表。
    """

    segments: List[Dict[str, Any]] = []
    for binary in images:
        # data 必须留空：宿主只把「空 content」当作待识别，非空会直接当成
        # 已识别的正文跳过视觉模型，等于适配器替模型写好了图片描述。
        segments.append(
            {"type": "image", "data": "", "binary_data_base64": base64.b64encode(binary).decode("ascii")}
        )
    for binary in stickers or []:
        # 与 QQ 适配器一致：表情用 emoji 段而不是 image 段。
        # 宿主据此生成 EmojiComponent，视觉链路会额外标注“[消息类型]表情包”，
        # 并把表情当作可读图片送进模型；GIF 抽帧也由宿主的 emoji_manager 负责。
        segments.append(
            {
                "type": "emoji",
                "data": "",
                "binary_data_base64": base64.b64encode(binary).decode("ascii"),
                "hash": hashlib.sha256(binary).hexdigest(),
            }
        )
    if voice is not None:
        # 同图片：data 留空才会走宿主的语音转写，写死占位等于跳过转写。
        segments.append(
            {"type": "voice", "data": "", "binary_data_base64": base64.b64encode(voice).decode("ascii")}
        )
    if pdf_name and pdf_text:
        # PDF 正文提取是适配器唯一必须做的解析：宿主没有 PDF 处理链路，
        # 不转成文本模型就完全看不到附件内容。这里只输出提取结果本身，
        # 不加任何「以下是资料」之类的指令性包装，那是替模型做判断。
        segments.append({"type": "text", "data": pdf_text})
    elif pdf_name:
        # 提取不到正文时给出可辨识的文件段，而不是伪造一句用户发言。
        segments.append({"type": "file", "data": {"name": pdf_name}})
    return segments

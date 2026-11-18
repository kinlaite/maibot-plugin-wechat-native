# 微信 Native 适配器

通过 [agent-wechat-native](https://github.com/thisnick/agent-wechat) 的 `/v1` API 把个人微信接入麦麦，无需在麦麦进程里运行微信客户端，也无需 AstrBot。

本插件是把 AstrBot 插件 [astrbot-plugin-wechat-native](https://github.com/neozhu/astrbot-plugin-wechat-native)（作者 neozhu，MIT）移植到麦麦插件 SDK 的适配器版本。

## 工作原理

```
微信客户端 ←→ agent-wechat-native（/v1 + SSE）←→ 本插件 ←→ 麦麦消息网关
```

`native_client.py` 是原项目的原生传输层，不依赖 AstrBot；`codec.py`、`plugin.py` 是按麦麦消息网关协议重写的接入层。

## 前置条件

1. 运行并登录 `agent-wechat-native`，确认 `/v1/accounts` 能返回账号。
2. 准备一个对该账号有权限的 Token。

## 配置

在插件管理页打开「微信 Native 适配器」，填写：

| 配置 | 说明 |
| --- | --- |
| Native 服务地址 | 例如 `http://127.0.0.1:6174`，可带 `/v1`；必须从麦麦所在机器可达 |
| Native Account ID | `GET /v1/accounts` 返回的 `id`，不是微信号 |
| Native Token / Token 文件 | 二选一；`token_file` 优先，适合用只读文件挂载密钥 |
| 启用插件 | 打开后才会建立连接 |

保存后插件会自行连接并维护 SSE 长连接，断线按 3→30 秒退避重连，重启后从已保存的游标续传。

## 聊天范围

| 配置 | 说明 |
| --- | --- |
| 私聊用户白名单 | 使用 `senderId`；留空允许所有私聊用户 |
| 群白名单 | 使用 `threadId`（通常以 `@chatroom` 结尾）；留空允许所有群 |
| 群聊触发模式 | `mention` 仅明确 @机器人；`all` 所有群消息交给麦麦主动回复策略；`disabled` 关闭群聊 |
| 群聊回复方式 | `quote` 引用触发消息；`mention` @触发者；`plain` 直接发送 |

群聊的「是否真的回复」仍由麦麦自己的适配器策略与主动回复概率决定，本插件只负责把消息送进网关。

群聊回复方式只影响**群聊里机器人的首条文字回复**：`quote` 会引用触发消息，`mention` 会在正文前 @ 发送者，`plain` 什么都不加。私聊不处理；纯图片或语音回复也不会自动补引用。选 `mention` 时如果取不到群成员资料，会省略 @ 但仍然发送正文。

## 面向模型的工具

工具只在微信会话内可用，且只能操作**当前聊天**，不会跨会话读取或写入其他群。

### 只读工具（需要 `read` 权限）

| 工具 | 用途 | 开关 |
| --- | --- | --- |
| `wechat_search_history` | 搜索当前会话的历史消息 | 启用历史工具 |
| `wechat_group_info` | 读取当前群资料 | 启用历史工具 |
| `wechat_group_members` | 列出或搜索当前群成员 | 启用历史工具 |
| `wechat_search_contact` | 搜索微信联系人 | 启用联系人搜索 |

### 管理工具（需要 admin Token + 管理员白名单）

| 工具 | 用途 | 限制 |
| --- | --- | --- |
| `wechat_recall_message` | 撤回机器人自己发的消息 | 不能撤回他人消息 |
| `wechat_set_my_group_nickname` | 改机器人的群昵称 | 仅群聊，只改自己 |
| `wechat_rename_group` | 改群名称 | 仅群聊，影响全群 |
| `wechat_list_friend_requests` | 列出好友申请 | 只读 |
| `wechat_accept_friend_request` | 接受好友申请 | 微信没有拒绝接口 |

两个条件必须同时满足才会执行：配置里有可用的 **admin Token**，且发起者 `senderId` 在**管理员白名单**里。任一条件不满足，这些工具不仅不会执行，还不会出现在模型的可用工具列表中。

### 微信没有的能力

微信个人号接口不支持**禁言**和**移出群成员**，所以这里没有对应工具。模型被告知了这一限制，不会假装能执行。

## 权限与 Token

| 功能 | 所需 scope |
| --- | --- |
| 收发消息、读取历史、群资料、群成员、搜索联系人 | `read`、`send`、`media` |
| 撤回、改群昵称、改群名、好友申请 | `admin` |

日常连接用普通 Token（`Native Token`），管理工具另外配置 `admin Token`。不配置 admin Token 时插件仍可正常收发消息，只是管理工具保持关闭。

## 支持的消息

- 私聊与群聊文本，群内 @机器人可被识别并去掉 @ 前缀。
- 图片、语音、PDF（PDF 以提取文本的形式作为参考资料注入）。
- 引用消息：入站会以 `[引用消息]` 前缀带出原文；出站可用引用回复。
- 表情：需在配置中显式开启「表情读取与发送」。

暂不支持文件收发、朋友圈、群管理（禁言、踢人）与语音转写（语音依赖上游 Native 提供内容）。

## 与内置「微信 ClawBot 适配器」的区别

MaiBot 内置的 `weixin_clawbot_adapter` 走微信官方 iLink / ClawBot 通道，只需扫码，但仅支持与机器人的私聊会话。

本插件走 `agent-wechat-native`，可以接入真实的个人微信私聊与群聊，代价是需要自行部署并运行 Native 服务。

## 排错

- **一直显示「Native 账号尚未在线」**：Native 侧微信没登录或掉线，先检查 Native 服务本身。
- **HTTP 401/403/410**：Token 无效或事件游标过期；前者检查 Token，后者查看 Native 日志。
- **收不到群消息**：确认群聊触发模式为 `mention` 时消息确实 @ 了机器人，且群在允许列表内。
- **发送失败提示「发送结果不确定」**：Native 已提交但未拿到回执，为避免重复不会自动重试，请先在微信确认是否收到。

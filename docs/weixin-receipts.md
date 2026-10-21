# 微信 iLink 发送回执

本片只收紧 `sendmessage` 已出现状态字段的类型校验。轮询、上传地址和 CDN 的原有处理保持不变，不新增语音发送或编码能力。

## 成功与未知

腾讯官方 iLink 2.4.6 的发送响应将 `ret` 定义为可选数字，且官方测试明确接受 HTTP 200 加空对象 `{}`。MuLi 因此保留字段省略的兼容行为，不要求补造远端消息 ID。已出现的 `ret`、`errcode` 必须是有限数字，布尔值不算数字；`0` 与 `0.0` 均表示零。整数直接检查，不转换成浮点数。

官方发送响应只声明 `ret`；`errcode` 在官方轮询响应中另有定义。MuLi 保留历史上对可选 `errcode` 的错误兼容：两个字段类型均合法时，沿原有 `ret` 优先、`ret` 为零时检查 `errcode` 的规则处理非零拒绝；`-14` 继续延后一小时重试。不能把缺少 `errcode` 当失败，也不能把它当成功收据或远端消息 ID。

任一已出现状态字段为 `null`、字符串、布尔值、数组、对象或非有限数字时，不能据此确认成功或安全重发。适配器抛出固定的“iLink 发送回执格式错误”，并明确标记结果不确定。即使另一个状态字段为零或非零，也不修补畸形响应。错误和投递状态不包含上游 `errmsg`、完整响应或上下文凭据。

真实发送后的不确定结果进入现有 outbox `unknown`，保留原分段位置和此前收据，不自动重发。重复提交同一投递键仍返回原记录；人工重试须显式接受可能重复的风险，沿用原 `client_id`，已成功分段不会重发。HTTP 连接未建立、传输超时、发送取消及重启遗留状态仍沿用现有渠道处理。

成功状态仅表示平台 API 按该协议接受了请求。`muli-…` 形式的 `client_id` 是 MuLi 本地生成的请求标识，不证明对方已收到、已读或已播放，也不提供跨平台绝对去重保证。

## 固定来源

- 腾讯 [`openclaw-weixin v2.4.6`](https://github.com/Tencent/openclaw-weixin/tree/cef0bfc390393f716903e16d50408118047f87e0)，固定提交 `cef0bfc390393f716903e16d50408118047f87e0`：[`SendMessageResp`](https://github.com/Tencent/openclaw-weixin/blob/cef0bfc390393f716903e16d50408118047f87e0/src/api/types.ts#L194-L215) 的可选字段、[`sendMessage`](https://github.com/Tencent/openclaw-weixin/blob/cef0bfc390393f716903e16d50408118047f87e0/src/api/api.ts#L471-L489) 的非零判断，以及 [`HTTP 200 + {}` 成功测试](https://github.com/Tencent/openclaw-weixin/blob/cef0bfc390393f716903e16d50408118047f87e0/src/api/api.test.ts#L162-L168)。
- 独立客户端 [`weixin-ilink-client v0.1.3`](https://github.com/69gg/weixin-ilink-client/tree/e23374ca8d6c2c9b22f712ec0511f86dd471811a)，固定提交 `e23374ca8d6c2c9b22f712ec0511f86dd471811a`：发送错误判断会把缺失或非整数 `ret` 默认为零。这是客户端的宽松实现，不能据此认定畸形字段是合法成功回执。MuLi 不依赖或安装该客户端。

## 验证边界

旧实现的真实 `ChannelManager` / SQLite outbox 反例已执行：HTTP 200 返回 `ret:false` 时实际变为 `sent`，新断言期望 `unknown`，稳定失败。加入仅发送响应的类型门后，该例通过。

专用 42 项回归通过（0.30 秒、峰值 63.9 MiB），覆盖畸形字段、官方空对象和数字零、非零与 `-14`、超大整数、真实工作器不自动重发未知项、同键复用、人工重试的原 `client_id`、分段进度及其他 iLink 方法兼容。既有 `test_channels.py` 与 `test_channel_voice.py` 的 53 项通过（0.43 秒、峰值 79.9 MiB）；两文件 Ruff / 格式和修改源码的严格 mypy 通过，静态检查最高 127.1 MiB。所有检查经过共享资源守卫，单次 30 秒、1 GiB、swap 0。

测试使用有界临时 SQLite 与 `httpx.MockTransport`，未访问真实账号、网络 API、模型或媒体服务。真实微信账号的发送验收尚未进行。

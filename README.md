# 最终上下文记录器 v1.0.0

记录真正发出前已组装完成的模型请求，方便排查人设、历史上下文、ContextWeave 等插件注入内容和工具调用。日志保存在本地，按配置的总容量滚动删除最旧文件。

## 安装与查看

1. 在 AstrBot 插件管理中从本地 ZIP 安装本插件；也可把整个 `astrbot_plugin_context_recorder` 文件夹放入 `data/plugins/` 后重启 AstrBot。
2. 在插件配置中设置“日志文件总容量上限”，默认 **100 MiB**；单文件目标大小默认 **10 MiB**。修改后重载插件（或重启 AstrBot）生效。
3. 正常对话后，用管理员命令 `/ctxlog` 查看日志目录、开关和占用大小。
4. 日志位于 AstrBot 数据目录下的 `data/plugin_data/astrbot_plugin_context_recorder/logs/`（实际绝对路径以 `/ctxlog` 和启动日志为准）。
5. 用浏览器打开插件目录下的 **viewer.html**，选择一个或多个 `context-*.jsonl` 文件。支持全文搜索、逐条查看角色消息、展开完整 JSON。日志变化后重新选择文件刷新。

也可直接用文本编辑器查看 JSONL；每一行是一条完整请求，UTF-8 编码，时间为 UTC，查看器按浏览器本地时区显示。日志最新文件名的数字最大。插件没有对外 Web 服务，也不将上下文发送到聊天中。

## 捕获位置与范围

插件包装 `httpx.AsyncClient.send` 和 `httpx.Client.send`，在 SDK 已经生成 HTTP JSON 正文、调用原发送方法之前保存正文副本，随后原样继续发送。不会修改请求或读取响应。

识别以下 POST 路由及正文结构：

| API | 路由 | 上下文字段 |
| --- | --- | --- |
| OpenAI 兼容 Chat Completions | `*/chat/completions` | `messages` |
| OpenAI Responses | `*/responses` | `input` |
| Anthropic Messages | `*/messages` | `messages`、`system` |
| Gemini | `*:generateContent`、`*:streamGenerateContent` | `contents`、`systemInstruction` |

适用于使用 httpx 的上述请求，包括流式、非流式、工具调用后续轮次和 SDK 重试。每次经过 send 的尝试单独记录，不表示服务端一定接收成功。同一进程内其他插件发出的匹配请求也会记录，因此无需依赖容易被后续修改覆盖的 `on_llm_request` 钩子。可用 `allowed_hosts` 限定主机（如 `api.openai.com`，不填协议和路径）。

**范围限制：**不捕获 aiohttp、requests、WebSocket、本地推理、第三方 Agent 服务器内部的隐藏上下文，也不捕获非标准 API 路由。httpx 的认证器或自定义 transport 若在 send 内再次改变正文，此处不会反映这种额外变更。当前不会关联 QQ 消息 ID/群号；可搜索请求正文定位会话。conversation_id 仅在请求存在 `x-astrbot-conversation-id` 时记录。

记录包含正文中的系统提示、用户输入、历史消息、工具定义及结果、模型采样参数等。不会保存 HTTP 请求头或 URL 查询参数；正文中的常见凭证字段会遮蔽。正文自然语言里的敏感信息仍会保留。

默认对 data URL 和 Gemini inlineData 的二进制内容保存省略标记、长度和 SHA-256；**文本上下文不做常规截断**。启用 `include_inline_media` 后保存这些原始媒体数据。外部媒体 URL 原样保留。

## 容量与可靠性

- 上限计算本插件 `logs` 目录中 `context-数字.jsonl` 文件的实际字节总数（不包含文件系统分配单位、源代码和查看器）。只清理这些文件。
- 每次写入前删除最旧文件腾出容量。文件按时间编号排序，单条请求不跨文件拆分。单条记录可超过“单文件目标大小”，但不会超过总容量上限。
- **如果一条请求序列化后大于总容量上限，不保存该条正文，改存 `payload_omitted` 标记、原始字节数及原因。**若要保留这种请求，请调大总上限。
- 启动时也按当前上限清理，调低上限后重载即可收缩。最小配置为 1 MiB。
- 线程锁串行写入，异步调用通过工作线程落盘；每次写入关闭文件，随时可以读取。仅支持单个 AstrBot 进程管理同一日志目录。
- 写盘或序列化失败不会阻止模型请求，错误按最多每分钟一次写入 AstrBot 日志。磁盘故障时无法保证保留本次记录。
- 禁用/卸载时停止观察并恢复由本插件安装的包装；保留已有日志供之后查看。

## 配置

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `enabled` | `true` | 是否记录 |
| `max_total_mb` | `100` | 日志总上限，单位 MiB |
| `segment_mb` | `10` | 单文件目标大小，单位 MiB |
| `include_inline_media` | `false` | 是否完整保存内嵌媒体 |
| `allowed_hosts` | `[]` | 模型 API 主机白名单，空列表表示不限制 |

仅使用 Python 标准库和 AstrBot 自带的 httpx，没有额外依赖。已验证本机 AstrBot 4.28.2 的真实插件 API、初始化、管理员状态命令与卸载生命周期。其他版本需提供 `StarTools.get_data_dir` 与标准插件配置 API。

测试脚本位于 ZIP 外的 `context_recorder_tests/`，使用 httpx MockTransport，不连接真实模型。运行 `python -m unittest discover -s context_recorder_tests -v`；真实 API 验证脚本为 `context_recorder_tests/smoke_astrbot.py`，需在带有 AstrBot 依赖的 Python 环境运行，可用 `ASTRBOT_SOURCE` 指定源码目录。

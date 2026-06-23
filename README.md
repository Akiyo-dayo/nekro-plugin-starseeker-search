# 星巡搜索

星巡搜索是一个面向 Nekro Agent 的联网搜索插件。它把外部搜索结果整理成 Agent 可引用的标题、摘要和 URL，用于回答新闻、版本、活动、价格、汇率、资料查证等需要外部信息的问题。

插件只使用 Python 标准库实现，不依赖 `requests`、`httpx`、`openai` 或 `bs4`，适合最小化运行容器和无法动态安装依赖的环境。

## 功能

- 为 Agent 提供 `web_search` 沙箱方法。
- 支持 Tavily、Brave Search、自建 SearXNG。
- 在未配置正式搜索 API 时，提供有限的无 key fallback。
- 对搜索结果进行去重、关键词相关性评分和低价值域名降权。
- 当结果可信度不足时返回明确失败原因，避免 Agent 用弱相关链接编造答案。

## 工具接口

```python
web_search(query: str, max_results: int = 5) -> str
```

参数：

- `query`: 要搜索的关键词、问题、新闻主题或网页主题。
- `max_results`: 返回结果数量，范围 `1-10`。不传时使用插件配置里的 `MAX_RESULTS`。

返回内容包含：

- 搜索主题
- 使用的结果来源
- 多条搜索结果的标题、摘要和 URL
- 搜索失败或低可信时的原因说明

## 推荐配置

生产环境建议配置正式搜索 API。推荐顺序：

1. `Tavily`: 适合 Agent 工作流，结果摘要对问答场景友好。
2. `Brave`: 通用网页搜索 API，适合需要传统搜索结果列表的场景。
3. `SearXNG`: 适合自建搜索网关；不建议依赖公共实例。
4. `fallback`: 无 key 临时兜底，不应作为稳定搜索能力。

## 配置项

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `PROVIDER` | `auto` | 搜索提供方。支持 `auto`、`tavily`、`brave`、`searxng`、`duckduckgo`、`bing`、`fallback`。 |
| `TAVILY_API_KEY` | 空 | Tavily API key。 |
| `BRAVE_API_KEY` | 空 | Brave Search API key。 |
| `SEARXNG_BASE_URL` | 空 | 自建 SearXNG 地址，例如 `https://search.example.com`。实例需要开放 JSON 搜索。 |
| `MAX_RESULTS` | `5` | 默认返回结果数量，范围 `1-10`。 |
| `TIMEOUT_SECONDS` | `12` | 单次请求超时时间，范围 `3-60` 秒。 |
| `ALLOW_BING_FALLBACK` | `true` | 允许无 key fallback。名称沿用旧配置，实际 fallback 会组合多个公开网页搜索入口。 |

`PROVIDER=auto` 的行为：

- 有 `BRAVE_API_KEY` 时尝试 Brave。
- 有 `TAVILY_API_KEY` 时尝试 Tavily。
- 有 `SEARXNG_BASE_URL` 时尝试 SearXNG。
- 如果允许 fallback，再尝试无 key fallback。

## 无 Key Fallback

无 key fallback 是临时兜底，不是稳定搜索服务。它会优先使用相对可靠的通用搜索结果，再用低置信度来源补充：

- 中文查询：Bing HTML、Bing RSS、DuckDuckGo Lite、360 Search。
- 非中文查询：DuckDuckGo Lite、Bing HTML、Bing RSS。

注意事项：

- Bing 和 DuckDuckGo 的公开页面结构可能变化，解析结果不能等同正式 API。
- 360 Search 结果置信度较低，仅作为中文查询的补充来源。
- 公共 SearXNG 实例常见 `403`、`429` 或反机器人页，不适合作为默认后端。
- 对非中文查询，如果 fallback 只得到 Bing 的低可信结果，插件会返回失败提示，避免 Agent 据此编造答案。

## 使用建议

Agent 调用示例：

```python
web_search(query="OpenAI latest model news", max_results=5)
```

建议：

- 当用户问“最新”“今天”“是否发布”“有没有联动”“价格”“版本发布时间”等问题时，先调用 `web_search`。
- 回答时引用结果中的 URL，不要只凭摘要下结论。
- 如果返回“未找到可靠结果”或“低可信”，应直接告诉用户搜索源不足。
- 对金融、医疗、法律、重大新闻等高风险问题，即使搜索成功也应提示来源和时效限制。

## 故障排查

### 插件没有加载

检查插件详情里是否满足：

- `name`: `星巡搜索`
- `moduleName`: `nekro_plugin_starseeker_search`
- `enabled`: `true`
- `loadFailed`: `false`
- `methods` 中存在 `web_search`

### 返回结果答非所问

优先检查：

1. 是否配置了 `TAVILY_API_KEY`、`BRAVE_API_KEY` 或 `SEARXNG_BASE_URL`。
2. 当前 `PROVIDER` 是否被固定为 `bing` 或 `fallback`。
3. 查询词是否太宽泛。建议包含关键实体和限定词。
4. 是否依赖无 key fallback。无 key fallback 不保证稳定结果质量。

### DuckDuckGo 报 TLS 错误

这是容器网络到 DuckDuckGo Lite 的握手问题。插件会继续尝试其他 fallback 源，但结果质量可能下降。生产环境应配置 Tavily、Brave 或自建 SearXNG。

## 维护说明

- 插件目录：`plugins/packages/nekro_plugin_starseeker_search`
- 配置目录：`plugin_data/Akiyo_Codex.nekro_plugin_starseeker_search`
- 插件作者字段必须只包含字母、数字和下划线，所以使用 `Akiyo_Codex`。
- 修改代码后需要重启 Nekro Agent 才能让聊天侧加载新版本。

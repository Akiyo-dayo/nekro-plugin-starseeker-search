"""Starseeker Search plugin for Nekro Agent.

The plugin intentionally uses only Python's standard library so it can run in
minimal Nekro Agent containers without installing dynamic dependencies.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from nekro_agent.api.plugin import ConfigBase, NekroPlugin, SandboxMethodType
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.core import logger
from pydantic import Field


plugin = NekroPlugin(
    name="星巡搜索",
    module_name="nekro_plugin_starseeker_search",
    description="像巡航星图一样为 Agent 探索互联网，支持 Brave、Tavily、SearXNG 与零配置 fallback。",
    version="0.1.0",
    author="Akiyo_Codex",
    url="",
)


@plugin.mount_config()
class StarseekerSearchConfig(ConfigBase):
    """星巡搜索配置"""

    PROVIDER: str = Field(
        default="auto",
        title="搜索服务",
        description="auto / brave / tavily / searxng / duckduckgo / bing / fallback。auto 会按可用配置依次尝试。",
    )
    BRAVE_API_KEY: str = Field(
        default="",
        title="Brave Search API Key",
        description="Brave Search API 的订阅密钥，配置后优先使用 Brave。",
    )
    TAVILY_API_KEY: str = Field(
        default="",
        title="Tavily API Key",
        description="Tavily API 密钥，适合 Agent 搜索摘要。",
    )
    SEARXNG_BASE_URL: str = Field(
        default="",
        title="SearXNG 地址",
        description="自建 SearXNG 地址，例如 https://search.example.com。",
    )
    MAX_RESULTS: int = Field(default=5, title="默认结果数", ge=1, le=10)
    TIMEOUT_SECONDS: int = Field(default=12, title="请求超时秒数", ge=3, le=60)
    ALLOW_BING_FALLBACK: bool = Field(
        default=True,
        title="允许 Bing 兜底",
        description="没有正式 API 配置或正式 API 失败时，允许使用无 Key 搜索源兜底。",
    )


config: StarseekerSearchConfig = plugin.get_config(StarseekerSearchConfig)


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str


class SearchError(RuntimeError):
    pass


LOW_VALUE_DOMAINS = (
    "support.google.com",
    "accounts.google.com",
    "youtube.com",
    "google.com/search",
    "baike.baidu.com",
)

QUERY_STOPWORDS = {
    "联网",
    "搜索",
    "查询",
    "当前",
    "一下",
    "关于",
    "相关",
    "信息",
    "消息",
    "官方",
    "有没有",
    "是否",
    "什么时候",
}


def _clean_text(value: Any, limit: int = 500) -> str:
    text = "" if value is None else str(value)
    text = html.unescape(text)
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "..."
    return text


def _http_text(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 12,
) -> str:
    data = None
    request_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    }
    if headers:
        request_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise SearchError(f"HTTP {exc.code}: {body}") from exc
    except Exception as exc:
        raise SearchError(f"{type(exc).__name__}: {exc}") from exc


def _http_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
    text = _http_text(*args, **kwargs)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SearchError(f"Invalid JSON response: {text[:200]}") from exc


def _decode_redirect_url(url: str) -> str:
    url = html.unescape(url)
    if url.startswith("//"):
        url = "https:" + url
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "uddg" in query and query["uddg"]:
        return query["uddg"][0]
    if "u" in query and query["u"]:
        return query["u"][0]
    return url


def _important_terms(query: str) -> list[str]:
    raw_parts = re.split(r'[\s,.;:!?"\'`~@#$%^&*+=|/\\<>()\[\]{}-]+', query.lower())
    terms = []
    for raw_part in raw_parts:
        part = raw_part.strip()
        if len(part) < 2 or part in QUERY_STOPWORDS:
            continue
        terms.append(part)
        sub_parts = re.findall(r"[a-z]+[a-z0-9._+-]*|\d+[a-z0-9._+-]*|[\u4e00-\u9fff]+", part)
        if len(sub_parts) > 1:
            terms.extend(sub for sub in sub_parts if len(sub) >= 2 and sub not in QUERY_STOPWORDS)
        if re.fullmatch(r"[\u4e00-\u9fff]{5,}", part):
            for size in (2, 3, 4):
                for index in range(0, len(part) - size + 1):
                    gram = part[index : index + size]
                    if gram not in QUERY_STOPWORDS:
                        terms.append(gram)
    if terms:
        return list(dict.fromkeys(terms))[:12]

    segments = re.findall(r"[a-z]+[a-z0-9._+-]*|\d+[a-z0-9._+-]*|[\u4e00-\u9fff]+", query.lower())
    for segment in segments:
        if segment in QUERY_STOPWORDS:
            continue
        if len(segment) <= 4:
            terms.append(segment)
        elif re.fullmatch(r"[\u4e00-\u9fff]+", segment):
            for size in (2, 3, 4):
                for index in range(0, len(segment) - size + 1):
                    gram = segment[index : index + size]
                    if gram not in QUERY_STOPWORDS:
                        terms.append(gram)
        else:
            terms.append(segment)
    return list(dict.fromkeys(terms))[:12]

def _query_variants(query: str) -> list[str]:
    query = re.sub(r"\s+", " ", query).strip()
    terms = _important_terms(query)
    variants = []
    if len(terms) >= 2:
        variants.append(" ".join(f'"{term}"' for term in terms[:4]))
    variants.append(query)
    compact = re.sub(r"\s+", "", query)
    if compact and compact != query:
        variants.append(compact)
    if len(terms) >= 2:
        variants.append(" ".join(terms[:4]) + " 新闻")
        variants.append(" ".join(terms[:4]) + " 讨论")
    return list(dict.fromkeys(variant for variant in variants if variant))[:5]


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _is_low_confidence_no_key_result(query: str, results: list[SearchResult]) -> bool:
    return bool(results) and not _contains_cjk(query) and all(item.source in {"Bing HTML", "Bing RSS"} for item in results)


def _score_result(query: str, item: SearchResult) -> float:
    terms = _important_terms(query)
    title = item.title.lower()
    snippet = item.snippet.lower()
    url = item.url.lower()
    haystack = f"{title} {snippet} {url}"
    score = 0.0
    for term in terms:
        if term in title:
            score += 4.0
        elif term in snippet:
            score += 2.5
        elif term in url:
            score += 1.0
    if terms:
        matched = sum(1 for term in terms if term in haystack)
        score += 5.0 * matched / len(terms)
        if matched == len(terms):
            score += 4.0
    if any(domain in url for domain in LOW_VALUE_DOMAINS):
        score -= 5.0
    if not item.snippet:
        score -= 1.0
    return score


def _rank_and_filter_results(query: str, candidates: list[SearchResult], limit: int) -> list[SearchResult]:
    seen = set()
    unique = []
    for item in candidates:
        clean_url = item.url.split("#", 1)[0]
        if not clean_url.startswith("http") or clean_url in seen:
            continue
        seen.add(clean_url)
        unique.append(SearchResult(item.title, clean_url, item.snippet, item.source))

    scored = sorted(
        ((item, _score_result(query, item)) for item in unique),
        key=lambda pair: pair[1],
        reverse=True,
    )
    if not scored:
        return []

    terms = _important_terms(query)
    threshold = 5.0 if len(terms) >= 2 else 2.0
    min_matches = 2 if len(terms) >= 3 else 1
    filtered = [
        item
        for item, score in scored
        if score >= threshold and sum(1 for term in terms if term in f"{item.title} {item.snippet} {item.url}".lower()) >= min_matches
    ]
    if not filtered and scored[0][1] >= threshold - 1.5:
        filtered = [scored[0][0]]
    return filtered[:limit]


def _search_brave(query: str, limit: int, timeout: int) -> list[SearchResult]:
    if not config.BRAVE_API_KEY.strip():
        raise SearchError("Brave API key is not configured")
    params = urllib.parse.urlencode(
        {
            "q": query,
            "count": min(limit, 10),
            "search_lang": "zh-hans",
            "country": "CN",
            "safesearch": "moderate",
            "text_decorations": "false",
        }
    )
    data = _http_json(
        f"https://api.search.brave.com/res/v1/web/search?{params}",
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": config.BRAVE_API_KEY.strip(),
        },
        timeout=timeout,
    )
    results = []
    for item in data.get("web", {}).get("results", [])[:limit]:
        results.append(
            SearchResult(
                title=_clean_text(item.get("title"), 160),
                url=str(item.get("url") or ""),
                snippet=_clean_text(item.get("description"), 360),
                source="Brave",
            )
        )
    return [item for item in results if item.url]


def _search_tavily(query: str, limit: int, timeout: int) -> list[SearchResult]:
    if not config.TAVILY_API_KEY.strip():
        raise SearchError("Tavily API key is not configured")
    payload = {
        "query": query,
        "max_results": min(limit, 10),
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
    }
    headers = {"Authorization": f"Bearer {config.TAVILY_API_KEY.strip()}"}
    try:
        data = _http_json(
            "https://api.tavily.com/search",
            method="POST",
            headers=headers,
            payload=payload,
            timeout=timeout,
        )
    except SearchError:
        payload_with_key = dict(payload)
        payload_with_key["api_key"] = config.TAVILY_API_KEY.strip()
        data = _http_json(
            "https://api.tavily.com/search",
            method="POST",
            payload=payload_with_key,
            timeout=timeout,
        )
    results = []
    for item in data.get("results", [])[:limit]:
        results.append(
            SearchResult(
                title=_clean_text(item.get("title"), 160),
                url=str(item.get("url") or ""),
                snippet=_clean_text(item.get("content"), 360),
                source="Tavily",
            )
        )
    return [item for item in results if item.url]


def _search_searxng(query: str, limit: int, timeout: int) -> list[SearchResult]:
    base_url = config.SEARXNG_BASE_URL.strip().rstrip("/")
    if not base_url:
        raise SearchError("SearXNG base URL is not configured")
    params = urllib.parse.urlencode(
        {
            "q": query,
            "format": "json",
            "language": "all",
            "categories": "general",
        }
    )
    data = _http_json(f"{base_url}/search?{params}", timeout=timeout)
    results = []
    for item in data.get("results", [])[:limit]:
        results.append(
            SearchResult(
                title=_clean_text(item.get("title"), 160),
                url=str(item.get("url") or ""),
                snippet=_clean_text(item.get("content"), 360),
                source="SearXNG",
            )
        )
    return [item for item in results if item.url]


def _search_duckduckgo_lite(query: str, limit: int, timeout: int) -> list[SearchResult]:
    params = urllib.parse.urlencode({"q": query})
    page = _http_text(f"https://lite.duckduckgo.com/lite/?{params}", timeout=timeout)
    anchors = list(
        re.finditer(
            r"<a\b(?=[^>]*class=['\"]result-link['\"])(?=[^>]*href=['\"]([^'\"]+)['\"])[^>]*>(.*?)</a>",
            page,
            flags=re.I | re.S,
        )
    )
    results = []
    for index, anchor in enumerate(anchors):
        href = _decode_redirect_url(anchor.group(1))
        block_end = anchors[index + 1].start() if index + 1 < len(anchors) else len(page)
        block = page[anchor.end() : block_end]
        snippet_match = re.search(
            r"<td\b[^>]*class=['\"]result-snippet['\"][^>]*>(.*?)</td>",
            block,
            flags=re.I | re.S,
        )
        title = _clean_text(anchor.group(2), 180)
        snippet = _clean_text(snippet_match.group(1) if snippet_match else "", 420)
        if title and href.startswith("http"):
            results.append(SearchResult(title=title, url=href, snippet=snippet, source="DuckDuckGo Lite"))
        if len(results) >= limit:
            break
    return results


def _search_360(query: str, limit: int, timeout: int) -> list[SearchResult]:
    params = urllib.parse.urlencode({"q": query})
    page = _http_text(
        f"https://www.so.com/s?{params}",
        headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"},
        timeout=timeout,
    )
    blocks = re.findall(
        r"<li\b[^>]*class=['\"][^'\"]*\bres-list\b[^'\"]*['\"][^>]*>(.*?)</li>",
        page,
        flags=re.I | re.S,
    )
    results = []
    for block in blocks:
        title_match = re.search(r"<h3\b[^>]*class=['\"][^'\"]*\bres-title\b[^'\"]*['\"][^>]*>(.*?)</h3>", block, flags=re.I | re.S)
        if not title_match:
            continue
        title_html = title_match.group(1)
        url_match = re.search(r"\bdata-mdurl=['\"]([^'\"]+)['\"]", title_html, flags=re.I)
        if not url_match:
            url_match = re.search(r"<a\b[^>]*href=['\"]([^'\"]+)['\"]", title_html, flags=re.I | re.S)
        url = _decode_redirect_url(url_match.group(1)) if url_match else ""
        desc_match = re.search(r"<p\b[^>]*class=['\"][^'\"]*\bres-desc\b[^'\"]*['\"][^>]*>(.*?)</p>", block, flags=re.I | re.S)
        if not desc_match:
            desc_match = re.search(r"<span\b[^>]*class=['\"][^'\"]*\bres-list-summary\b[^'\"]*['\"][^>]*>(.*?)</span>", block, flags=re.I | re.S)
        title = _clean_text(title_html, 180)
        snippet = _clean_text(desc_match.group(1) if desc_match else "", 420)
        if title and url.startswith("http"):
            results.append(SearchResult(title=title, url=url, snippet=snippet, source="360 Search"))
        if len(results) >= limit:
            break
    return results


def _search_bing_rss(query: str, limit: int, timeout: int) -> list[SearchResult]:
    params = urllib.parse.urlencode({"q": query, "format": "rss"})
    text = _http_text(f"https://www.bing.com/search?{params}", timeout=timeout)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SearchError(f"Invalid Bing RSS: {exc}") from exc
    results = []
    for item in root.findall("./channel/item")[:limit]:
        title = _clean_text(item.findtext("title"), 180)
        url = html.unescape(item.findtext("link") or "")
        snippet = _clean_text(item.findtext("description"), 420)
        if title and url.startswith("http"):
            results.append(SearchResult(title=title, url=url, snippet=snippet, source="Bing RSS"))
    return results


def _extract_bing_blocks(page: str) -> list[str]:
    blocks = re.findall(
        r'<li[^>]+class="[^"]*\bb_algo\b[^"]*"[^>]*>(.*?)</li>',
        page,
        flags=re.I | re.S,
    )
    if blocks:
        return blocks
    return re.findall(r"<h2[^>]*>.*?</h2>.*?(?:<p[^>]*>.*?</p>)?", page, flags=re.I | re.S)


def _search_bing_fallback(query: str, limit: int, timeout: int) -> list[SearchResult]:
    if not config.ALLOW_BING_FALLBACK:
        raise SearchError("Bing fallback is disabled")
    params = urllib.parse.urlencode({"q": query, "count": min(limit, 10)})
    page = _http_text(f"https://cn.bing.com/search?{params}", timeout=timeout)
    results = []
    for block in _extract_bing_blocks(page):
        link_match = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, flags=re.I | re.S)
        if not link_match:
            continue
        url = html.unescape(link_match.group(1))
        if not url.startswith("http"):
            continue
        title = _clean_text(link_match.group(2), 160)
        snippet_match = re.search(r"<p[^>]*>(.*?)</p>", block, flags=re.I | re.S)
        snippet = _clean_text(snippet_match.group(1) if snippet_match else "", 360)
        if title and url:
            results.append(SearchResult(title=title, url=url, snippet=snippet, source="Bing HTML"))
        if len(results) >= limit:
            break
    return results


def _search_fallback(query: str, limit: int, timeout: int) -> list[SearchResult]:
    errors = []
    candidates = []
    if _contains_cjk(query):
        engine_plan = [
            ("bing_html", _search_bing_fallback, 4, min(timeout, 8)),
            ("bing_rss", _search_bing_rss, 2, min(timeout, 8)),
            ("duckduckgo", _search_duckduckgo_lite, 1, min(timeout, 6)),
            ("360", _search_360, 1, min(timeout, 8)),
        ]
    else:
        engine_plan = [
            ("duckduckgo", _search_duckduckgo_lite, 2, min(timeout, 6)),
            ("bing_html", _search_bing_fallback, 4, min(timeout, 8)),
            ("bing_rss", _search_bing_rss, 1, min(timeout, 8)),
        ]
    for engine_name, engine, variant_count, engine_timeout in engine_plan:
        for variant in _query_variants(query)[:variant_count]:
            try:
                candidates.extend(engine(variant, max(limit * 2, 8), engine_timeout))
                ranked = _rank_and_filter_results(query, candidates, limit)
                if len(ranked) >= min(limit, 3):
                    if _is_low_confidence_no_key_result(query, ranked):
                        raise SearchError(
                            "no-key fallback returned only low-confidence Bing results for a non-Chinese query; "
                            "configure Tavily, Brave, or SearXNG for reliable search"
                        )
                    return ranked
            except Exception as exc:
                errors.append(f"{engine_name}({variant}): {exc}")
                logger.warning(f"星巡搜索 fallback {engine_name} failed: {exc}")
    ranked = _rank_and_filter_results(query, candidates, limit)
    if ranked:
        if _is_low_confidence_no_key_result(query, ranked):
            raise SearchError(
                "no-key fallback returned only low-confidence Bing results for a non-Chinese query; "
                "configure Tavily, Brave, or SearXNG for reliable search"
            )
        return ranked
    raise SearchError("; ".join(errors[-4:]) or "fallback found no relevant results")


def _provider_order() -> list[str]:
    provider = config.PROVIDER.strip().lower()
    if provider and provider != "auto":
        return [provider]
    order = []
    if config.BRAVE_API_KEY.strip():
        order.append("brave")
    if config.TAVILY_API_KEY.strip():
        order.append("tavily")
    if config.SEARXNG_BASE_URL.strip():
        order.append("searxng")
    if config.ALLOW_BING_FALLBACK:
        order.append("fallback")
    return order or ["fallback"]


def _run_search(query: str, limit: int) -> tuple[list[SearchResult], list[str]]:
    timeout = max(3, min(int(config.TIMEOUT_SECONDS), 60))
    errors = []
    for provider in _provider_order():
        try:
            if provider == "brave":
                results = _search_brave(query, limit, timeout)
            elif provider == "tavily":
                results = _search_tavily(query, limit, timeout)
            elif provider == "searxng":
                results = _search_searxng(query, limit, timeout)
            elif provider == "duckduckgo":
                results = _rank_and_filter_results(query, _search_duckduckgo_lite(query, max(limit * 2, 8), timeout), limit)
            elif provider == "bing":
                bing_candidates = _search_bing_fallback(query, max(limit * 2, 8), timeout) + _search_bing_rss(query, max(limit * 2, 8), timeout)
                results = _rank_and_filter_results(query, bing_candidates, limit)
            elif provider == "fallback":
                results = _search_fallback(query, limit, timeout)
            else:
                raise SearchError(f"Unknown provider: {provider}")
            ranked_results = _rank_and_filter_results(query, results, limit)
            if ranked_results:
                return ranked_results, errors
            errors.append(f"{provider}: no relevant results")
        except Exception as exc:
            errors.append(f"{provider}: {exc}")
            logger.warning(f"星巡搜索 provider {provider} failed: {exc}")
    return [], errors


def _format_results(query: str, results: list[SearchResult], errors: list[str]) -> str:
    if not results:
        details = "\n".join(f"- {error}" for error in errors[-5:])
        return f"星巡搜索没有找到 `{query}` 的可用结果。\n{details}".strip()
    source = results[0].source
    lines = [f"星巡搜索结果：{query}", f"来源：{source}", ""]
    for index, item in enumerate(results, start=1):
        lines.append(f"{index}. {item.title}")
        if item.snippet:
            lines.append(f"   摘要：{item.snippet}")
        lines.append(f"   URL：{item.url}")
    return "\n".join(lines).strip()


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="web_search",
    description="联网搜索关键词、问题或网页主题，返回标题、摘要和来源链接。",
)
async def web_search(_ctx: AgentCtx, query: str, max_results: int | None = None) -> str:
    """联网搜索关键词、问题或网页主题，并返回可供回答引用的结果。

    Args:
        query: 要搜索的关键词、问题、新闻主题或网页主题。
        max_results: 返回结果数量，默认使用插件配置，范围 1-10。

    Returns:
        包含标题、摘要和 URL 的搜索结果。
    """
    query = (query or "").strip()
    if not query:
        return "星巡搜索需要一个非空查询。"
    limit = max_results if max_results is not None else config.MAX_RESULTS
    limit = max(1, min(int(limit), 10))
    loop = asyncio.get_running_loop()
    results, errors = await loop.run_in_executor(None, _run_search, query, limit)
    return _format_results(query, results, errors)


@plugin.mount_cleanup_method()
async def clean_up() -> None:
    logger.info("星巡搜索插件已清理完毕")

# 网页阅读

通用网页、RSS。

## 通用网页 (Jina Reader)

```bash
# 读取任意网页内容
curl -s "https://r.jina.ai/URL"

# 示例
curl -s "https://r.jina.ai/https://example.com/article"
```

**适用场景**: 大多数网页可以直接用 Jina Reader 读取。

## Web Reader (MCP)

```bash
# 读取网页内容 (Markdown 格式)
mcporter call web-reader.webReader url="https://example.com"

# 保留图片
mcporter call web-reader.webReader url="https://example.com" retain_images=true

# 纯文本格式
mcporter call web-reader.webReader url="https://example.com" return_format="text"
```

**适用场景**: 需要更精确控制输出格式时使用。

## RSS (feedparser)

```python
python3 -c "
import feedparser
for e in feedparser.parse('FEED_URL').entries[:5]:
    print(f'{e.title} — {e.link}')
"
```

**适用场景**: 订阅博客、新闻源、播客等 RSS feed。

## 选择指南

| 场景 | 推荐工具 |
|-----|---------|
| 通用网页 | Jina Reader (`curl r.jina.ai`) |
| 需要图片/格式控制 | web-reader MCP |
| RSS 订阅 | feedparser |

## Jina Reader: two things that are not obvious

**Percent-encode the whole target URL.** A query string passed raw is parsed
as Jina's own parameters and the request fails with `ParamValidationError`,
which says nothing about the real cause:

```bash
# wrong — ?action=... is read as Jina's parameters
curl -s "https://r.jina.ai/https://example.org/api.php?action=parse&page=X"

# right
python3 -c "from urllib.parse import quote;print(quote('https://example.org/api.php?action=parse&page=X',safe=''))"
curl -s "https://r.jina.ai/https%3A%2F%2Fexample.org%2Fapi.php%3Faction%3Dparse%26page%3DX"
```

**For a MediaWiki site behind a bot challenge, ask the API for wikitext.**
A direct `curl` gets a JavaScript challenge page (a few KB of script and no
content), but Jina renders it. Better still, skip the rendered HTML entirely:

```bash
# search the wiki
.../api.php?action=query&list=search&srsearch=<term>&format=json
# fetch one page as raw wikitext
.../api.php?action=parse&page=<Title>&prop=wikitext&format=json
```

Raw wikitext carries the conditions, prerequisites and effects in their source
form — tables and templates that the rendered page flattens or drops. When
accuracy matters more than readability, prefer it.

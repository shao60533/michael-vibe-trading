---
name: pdf-loader
description: PDF 加载与文本抽取工具。用于读取本地或 URL PDF、提取页文本和元数据、把券商研报、年报、公告、SEC filing PDF、招股书、会议纪要等长文档转成可供投研分析的结构化文本。适用于用户要求加载 PDF、解析 PDF、读取 PDF 内容、总结 PDF、从 PDF 里提取表述/风险/财务信息/估值假设/页码引用等场景。
---

# PDF Loader

## When to Activate

- 用户提供 PDF 本地路径或 URL，并要求读取、解析、加载、总结或问答。
- 用户要分析券商研报、公司公告、年报、季报、招股书、SEC filing PDF、政策文件或会议纪要。
- 用户要从 PDF 里提取投资观点、盈利预测、估值假设、风险提示、财务指标、章节标题或页码引用。
- 用户要把 PDF 内容作为后续 swarm/preset 分析的输入证据。

关键词：PDF、加载PDF、解析PDF、读取PDF、研报PDF、公告PDF、年报PDF、招股书、prospectus、filing pdf、extract text、pdf loader。

## Installed Dependency

容器内已安装：

```bash
pip install pypdf==6.12.1
```

`pypdf` 适合纯文本抽取、元数据读取、页码定位和轻量 PDF 处理。它不能对扫描版图片 PDF 做 OCR；如果页面抽出的文本很少，应明确说明这可能是扫描件，需要 OCR。

## Quick Use

```python
from pypdf import PdfReader

reader = PdfReader("/path/to/report.pdf")
page_count = len(reader.pages)
text = "\n\n".join(
    page.extract_text(extraction_mode="layout") or ""
    for page in reader.pages
)
```

## Robust Loader

Use this helper when the user asks to load a local PDF or a URL PDF. It returns page-bounded text with metadata and keeps output sizes controlled.

```python
from __future__ import annotations

import hashlib
import os
import pathlib
import tempfile
import urllib.parse
import urllib.request
from typing import Any

from pypdf import PdfReader


def _safe_name_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    name = pathlib.Path(parsed.path).name or "download.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
    return f"{digest}_{name}"


def download_pdf(url: str, target_dir: str | os.PathLike[str] | None = None) -> str:
    if target_dir is None:
        target_dir = pathlib.Path(tempfile.gettempdir()) / "pdf-loader"
    target = pathlib.Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = target / _safe_name_from_url(url)

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; michael-vibe-trading-pdf-loader/1.0)"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        content_type = response.headers.get("content-type", "").lower()
        data = response.read()

    if b"%PDF" not in data[:2048] and "pdf" not in content_type:
        raise ValueError(f"URL did not look like a PDF: content-type={content_type!r}")

    path.write_bytes(data)
    return str(path)


def load_pdf_text(
    source: str,
    *,
    password: str | None = None,
    max_pages: int | None = None,
    max_chars: int = 200_000,
    layout: bool = True,
) -> dict[str, Any]:
    path = download_pdf(source) if source.startswith(("http://", "https://")) else source
    reader = PdfReader(path)

    if reader.is_encrypted:
        if not password:
            raise ValueError("PDF is encrypted; password is required")
        reader.decrypt(password)

    pages = []
    total_chars = 0
    page_limit = min(len(reader.pages), max_pages or len(reader.pages))
    extraction_kwargs = {"extraction_mode": "layout"} if layout else {}

    for idx in range(page_limit):
        page = reader.pages[idx]
        raw = page.extract_text(**extraction_kwargs) or ""
        text = raw.strip()
        if not text:
            pages.append({"page": idx + 1, "text": "", "chars": 0})
            continue

        remaining = max_chars - total_chars
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining] + "\n[TRUNCATED]"

        pages.append({"page": idx + 1, "text": text, "chars": len(text)})
        total_chars += len(text)

    metadata = {
        str(k).lstrip("/"): str(v)
        for k, v in (reader.metadata or {}).items()
        if v is not None
    }

    return {
        "source": source,
        "local_path": str(path),
        "page_count": len(reader.pages),
        "loaded_pages": len(pages),
        "chars": total_chars,
        "metadata": metadata,
        "pages": pages,
    }
```

## Analysis Workflow

1. 先加载 PDF，检查 `page_count`、`loaded_pages`、`chars` 和 `metadata`。
2. 如果大多数页面 `text` 为空，告诉用户该文件可能是扫描件或图片型 PDF，需要 OCR。
3. 做投研分析时保留页码引用，例如 `第 12 页`、`第 35 页`，避免只给无来源总结。
4. 长 PDF 先按章节、目录、风险提示、财务摘要、估值假设拆块，再汇总。
5. 如果 PDF 来自外部 URL，记录 `source` 和下载后的 `local_path`，方便复查。

## Output Shape

给用户汇报 PDF 结果时，优先包含：

- 文件标题或来源。
- 页数和已加载页数。
- 关键结论，按页码标注。
- 无法抽取的页或扫描件风险。
- 后续可以进入哪个投研 preset，例如 `earnings_research_desk`、`risk_committee`、`investment_committee`。

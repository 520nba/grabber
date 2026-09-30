"""按链接自动分发到对应平台解析器。"""
from __future__ import annotations

import re
from urllib.parse import unquote

from . import cookie_guard, douyin, xhs

URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff\"'<>]+")


def extract_url(text: str) -> str:
    # 快捷指令/浏览器把链接塞进 URL path 时，分享文案可能被整体 URL 编码过
    #（例如 https%3A%2F%2F...），先 unquote 再抽链接，避免误判成无链接。
    raw = (text or "").strip()
    decoded = raw
    try:
        decoded = unquote(raw)
    except Exception:
        pass
    m = URL_RE.search(decoded)
    return m.group(0).rstrip("，。）)】],") if m else raw


def detect(url: str) -> str:
    u = url.lower()
    if "xiaohongshu" in u or "xhslink" in u or "xhs" in u:
        return "xhs"
    if "douyin" in u or "iesdouyin" in u or "snssdk" in u:
        return "douyin"
    raise ValueError("只支持小红书和抖音链接")


async def parse(text: str) -> dict:
    url = extract_url(text)
    platform = detect(url)
    mod = xhs if platform == "xhs" else douyin
    result = await mod.parse(url)
    result["source_url"] = url
    return result

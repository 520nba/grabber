"""按链接自动分发到对应平台解析器。"""
from __future__ import annotations

import contextvars
import re
from urllib.parse import unquote

from . import cookie_guard, douyin, xhs
from .base import PLATFORM_HOSTS, assert_host_allowed, host_allowed, host_of

URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff\"'<>]+")

# 当前请求的出网降级记录。base.fetch 往里追加，parse 结束后取出来附到响应上。
# 用 ContextVar 隔离并发请求（模块全局会让两个请求的诊断串台）。
DIAG: contextvars.ContextVar[list] = contextvars.ContextVar("fetch_diag", default=None)
_DIAG = DIAG


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
    """判定平台。**只看主机名，不看 URL 里有没有关键字。**

    旧实现是 `"xhs" in url` 这种子串匹配，两个后果：
      1. 只要在查询串里加 `?xhs=1`，任意地址都会被当成小红书链接送给解析器；
      2. 解析器在提取不出 ID 时会把原始 URL 直接出网（短链展开），于是
         `http://127.0.0.1:8765/...?xhs=1`、`http://169.254.169.254/...?xhs=1`
         会被真的请求出去 —— 即 SSRF。
    现在按主机名判定，且不在白名单内直接拒绝。
    """
    host = host_of(url)
    for platform, hosts in PLATFORM_HOSTS.items():
        if host_allowed(url, hosts):
            return platform
    raise ValueError(
        "只支持小红书和抖音链接"
        + (f"（当前主机：{host}）" if host else "（无法从输入中解析出有效链接）")
    )


async def parse(text: str) -> dict:
    url = extract_url(text)
    platform = detect(url)
    # 分发后再确认一次，避免后续改动绕过 detect 的判定
    assert_host_allowed(url, PLATFORM_HOSTS[platform], what="链接域名")
    mod = xhs if platform == "xhs" else douyin

    # 取数期间记录出网路径与降级原因。用 ContextVar 而不是模块全局：
    # FastAPI 每个请求跑在独立 task 上下文，避免并发请求的诊断结果互相串台。
    token = _DIAG.set([])
    try:
        result = await mod.parse(url)
    finally:
        diag = _DIAG.get()
        _DIAG.reset(token)

    if diag:
        # 只在**发生过降级**时才附上，正常路径不往响应里塞噪音
        result["fetch_diagnostics"] = diag
    result["source_url"] = url
    return result

"""抖音作品解析：无水印视频 / 图集原图。yt-dlp 为主，页面解析为备。"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import urlparse

from .base import (
    UA_ANDROID,
    UA_IOS,
    absorb_cookies,
    clean_text,
    douyin_image_pick,
    douyin_video_candidates,
    extract_json_block,
    fetch,
    get_douyin_cookie,
)
from .web_api import web_detail

AWEME_ID_RE = re.compile(r"/(?:video|note|slides)/(\d{15,25})")
MODAL_ID_RE = re.compile(r"(?:modal_id|aweme_id|item_ids)=(\d{15,25})")
ANY_ID_RE = re.compile(r"(\d{18,22})")


def _extract_id(text_or_url: str) -> str | None:
    for rx in (AWEME_ID_RE, MODAL_ID_RE):
        m = rx.search(text_or_url)
        if m:
            return m.group(1)
    return None


async def _resolve(url: str) -> tuple[str, str | None]:
    """短链 → (规范链接, aweme_id)。"""
    aweme_id = _extract_id(url)
    if aweme_id:
        return url, aweme_id
    resp = await fetch(url, headers={"User-Agent": UA_IOS}, impersonate="safari17_0_ios")
    aweme_id = _extract_id(resp.url) or _extract_id(resp.text)
    if aweme_id:
        return f"https://www.douyin.com/video/{aweme_id}", aweme_id
    m = ANY_ID_RE.search(resp.text)
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}", m.group(1)
    return resp.url, None


def _ydl_info(url: str) -> dict[str, Any] | None:
    try:
        import yt_dlp
    except Exception:
        return None
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
    }
    ck = get_douyin_cookie()
    if ck:
        opts["http_headers"] = {
            "Cookie": ck,
            "User-Agent": UA_ANDROID,
            "Referer": "https://www.douyin.com/",
        }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception:
        return None


def _from_ydl(info: dict[str, Any]) -> dict[str, Any] | None:
    formats = info.get("formats") or []
    cands: list[dict[str, Any]] = []
    for f in formats:
        u = f.get("url") or ""
        if not u:
            continue
        if "playwm" in u or "watermark" in (f.get("format_id") or "").lower():
            continue
        cands.append(f)
    if not cands and info.get("url"):
        cands = [info]
    if not cands:
        return None
    best = max(cands, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
    return {
        "kind": "video",
        "url": best.get("url"),
        "cover": info.get("thumbnail") or "",
        "width": best.get("width"),
        "height": best.get("height"),
        "duration": info.get("duration"),
    }


def _from_page(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """从 _ROUTER_DATA 里挖作品数据。返回 (items, meta)。"""
    item: dict[str, Any] | None = None

    def walk(node: Any):
        nonlocal item
        if item is not None:
            return
        if isinstance(node, dict):
            if "item_list" in node and isinstance(node["item_list"], list) and node["item_list"]:
                item = node["item_list"][0]
                return
            if "aweme_detail" in node and isinstance(node["aweme_detail"], dict):
                item = node["aweme_detail"]
                return
            if "aweme_id" in node and ("video" in node or "images" in node):
                item = node
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)

    meta: dict[str, Any] = {}
    if not item:
        return [], meta

    meta["title"] = clean_text(item.get("desc") or item.get("description") or "")
    meta["author"] = ((item.get("author") or {}).get("nickname")) or ""

    images = item.get("images")
    if isinstance(images, list) and images:
        items = []
        for img in images:
            best, alt = douyin_image_pick(img)
            if not best:
                continue
            items.append(
                {
                    "kind": "image",
                    "url": best,
                    "fallback": alt,
                    "preview": best,
                    "tier": "original",
                    "badge": "高清原图",
                    "width": img.get("width"),
                    "height": img.get("height"),
                }
            )
        if items:
            return items, meta

    video = item.get("video") or {}
    cands = douyin_video_candidates(video)
    if not cands:
        return [], meta
    top = cands[0]
    cover_obj = video.get("cover") or video.get("origin_cover") or {}
    cover_urls = cover_obj.get("url_list") or []
    duration = (video.get("duration") or 0) / 1000 or None
    return (
        [
            {
                "kind": "video",
                "url": top["url"],
                "backup": [c["url"] for c in cands[1:7]],
                "cover": cover_urls[0] if cover_urls else "",
                "width": top.get("width"),
                "height": top.get("height"),
                "bit_rate": top.get("bit_rate"),
                "duration": duration,
            }
        ],
        meta,
    )


# 出站节流：同一出口 IP 的密集请求会直接触发抖音验证码。这里保证两次外呼
# 至少间隔 1 秒，把「连点解析 / 批量解析」造成的密集请求摊开。
_LAST_OUTBOUND: float = 0.0
_MIN_GAP: float = 1.0


async def _throttle() -> None:
    global _LAST_OUTBOUND
    loop = asyncio.get_event_loop()
    wait = _MIN_GAP - (loop.time() - _LAST_OUTBOUND)
    if wait > 0:
        await asyncio.sleep(wait)
    _LAST_OUTBOUND = asyncio.get_event_loop().time()


async def _from_share(aweme_id: str, headers: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """免登录分享页取数。

    分享页（`iesdouyin.com/share/...`）把作品数据嵌在 `_ROUTER_DATA` 里，图文与视频
    都能拿到，且**不需要 a_bogus 签名**。它的行为最接近「人点开分享链接」，
    是免登录场景下最不容易触发滑块验证的一条路——所以放在首选，而不是当兜底。
    """
    for tmpl in (
        "https://www.iesdouyin.com/share/video/{id}",
        "https://www.iesdouyin.com/share/slides/{id}",
    ):
        await _throttle()
        try:
            resp = await fetch(tmpl.format(id=aweme_id), headers=headers, impersonate="safari17_0_ios")
        except Exception:
            continue
        # 接住抖音滚动下发的 cookie（__ac_nonce 等短效风控 token），减少验证墙。
        absorb_cookies(resp.url or "https://www.douyin.com/", resp.cookies)
        payload = extract_json_block(resp.text, "_ROUTER_DATA")
        if not payload:
            continue
        items, meta = _from_page(payload)
        if items:
            return items, meta
    return [], {}


async def parse(url: str) -> dict[str, Any]:
    warnings: list[str] = []
    canonical, aweme_id = await _resolve(url)
    if not aweme_id:
        raise ValueError("无法从抖音链接中识别作品 ID，请复制完整的分享链接")

    headers = {"User-Agent": UA_IOS, "Referer": "https://www.douyin.com/"}
    ck = get_douyin_cookie()
    if ck:
        headers["Cookie"] = ck

    items: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    via = ""

    # 取证结论（2026-09）：抖音匿名取数已被 Argus 风控收紧——无 Cookie 的接口直连
    # 直接返 403「Uifid Not Found」，分享页 SSR 也不再内嵌作品数据。
    # 所以：有 Cookie 时优先走接口（登录态自带 Uifid / msToken / s_v_web_id，最能压住风控），
    # 无 Cookie 时优先分享页（最接近真人行为，相对不易触发滑块）。
    order = ["api", "share", "ydlp", "page"] if ck else ["share", "api", "ydlp", "page"]

    for step in order:
        if items:
            break
        if step == "api":
            try:
                wd = await web_detail(aweme_id)
            except Exception:
                wd = None
            if wd:
                items, meta, via = wd["items"], wd["meta"], "api"
        elif step == "share":
            items, meta = await _from_share(aweme_id, headers)
            if items:
                via = "share"
        elif step == "ydlp":
            # yt-dlp（带 Cookie 时更接近客户端行为）
            info = await asyncio.to_thread(_ydl_info, canonical)
            if info:
                v = _from_ydl(info)
                if v:
                    items = [v]
                    via = "ytdlp"
                    meta = {
                        "title": clean_text(info.get("title") or info.get("description") or ""),
                        "author": info.get("uploader") or "",
                    }
        else:  # page：douyin.com 作品页兜底
            await _throttle()
            try:
                resp = await fetch(
                    f"https://www.douyin.com/video/{aweme_id}",
                    headers=headers,
                    impersonate="chrome",
                )
                absorb_cookies(resp.url or "https://www.douyin.com/", resp.cookies)
                payload = extract_json_block(resp.text, "_ROUTER_DATA")
                if payload:
                    items, meta = _from_page(payload)
                    if items:
                        via = "page"
            except Exception:
                pass

    if not items:
        raise ValueError(
            "抖音未返回作品数据。抖音对匿名访问收得很紧：同一出口 IP 请求过密、"
            "或本机/机房 IP 被标记，都会直接上滑块或返 403（Uifid Not Found）。"
            "建议按顺序试：① 在「登录态 Cookie」里填入已登录的抖音 Cookie"
            "（登录态自带 Uifid / msToken，最有效）；② 等几分钟再试；③ 给服务配一个干净的出口 IP（代理）。"
        )

    if via == "share":
        warnings.append("分享页直连成功。图片取高清原图、视频取最高码率档。")
    elif via == "api":
        warnings.append(
            "接口直连成功（a_bogus + ttwid"
            + ("，并带上了已填的 Cookie" if ck else "，无需 Cookie")
            + "）。"
        )
    elif via == "ytdlp":
        warnings.append("经 yt-dlp 取数成功。")
    elif via == "page":
        warnings.append("经作品页兜底取数成功。")

    return {
        "platform": "douyin",
        "note_id": aweme_id,
        "type": items[0]["kind"],
        "title": meta.get("title", ""),
        "author": meta.get("author", ""),
        "cover": items[0].get("cover") or items[0].get("preview") or "",
        "items": items,
        "warnings": warnings,
    }

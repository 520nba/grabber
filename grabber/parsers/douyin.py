"""抖音作品解析：无水印视频 / 图集原图。yt-dlp 为主，页面解析为备。"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import urlparse

from .base import (
    DOUYIN_HOSTS,
    UA_ANDROID,
    _dy_is_audio_stream,
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
    # 同样在这里显式传白名单：本函数会把**用户粘贴的 URL** 拿去出网（短链展开）。
    resp = await fetch(
        url,
        headers={"User-Agent": UA_IOS},
        impersonate="ios",
        allow_hosts=DOUYIN_HOSTS,
    )
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
    """把 yt-dlp 的结果收敛成一个视频 item；拿不到真视频时返回 None。

    图文作品（aweme_type=68）也会被 yt-dlp 解析出一个「视频」，但那其实是
    该作品的**背景音乐**（实测 content-type: audio/mp4）。判据用 ``duration``：
    图文作品的占位 video 时长为 0，而真视频一定有正时长。
    """
    try:
        dur = float(info.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    if dur <= 0:
        return None

    formats = info.get("formats") or []
    cands: list[dict[str, Any]] = []
    for f in formats:
        u = f.get("url") or ""
        if not u:
            continue
        if "playwm" in u or "watermark" in (f.get("format_id") or "").lower():
            continue
        # 音视频分离时 yt-dlp 会单列音频轨，别把它当成视频档
        if (f.get("vcodec") or "none") == "none":
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


def _share_play_candidates(video: dict[str, Any]) -> list[dict[str, Any]]:
    """从**分享页**的 video 结构里取直链候选（这是免签名路径唯一能拿到的源）。

    为什么需要单独一条：``douyin_video_candidates`` 会跳过所有含 ``playwm`` 的地址
    （那是带水印的分享档，它的设计前提是「detail 接口里还有无水印地址」）。但分享页
    （``m.douyin.com/share/video/<id>``）**只给 playwm**，于是那条路会被整体过滤成
    0 候选 —— 表现就是「视频一律无法解析」。

    实测：把 ``/aweme/v1/playwm/`` 换成 ``/aweme/v1/play/``（并把 ratio 提到 1080p）
    返回的是**真实 MP4 字节**且不需要签名（1.4MB → 879KB，无 403）。
    所以这里保留该变换后仍可用的地址。

    注意：这些地址**没有**多档 ``bit_rate``，拿不到 detail 接口那种按码率分档的信息，
    分辨率以服务端按 ratio 选流为准（调用方会用实际字节校验，不谎报尺寸）。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in ("play_addr", "download_addr", "play_addr_h264"):
        obj = video.get(key)
        if not isinstance(obj, dict):
            continue
        for u in obj.get("url_list") or []:
            if not u or _dy_is_audio_stream(u):
                continue
            # playwm → play：去掉分享水印
            clean = re.sub(r"/aweme/v1/playwm/", "/aweme/v1/play/", u)
            # ratio 提到 1080p：已有则替换，没有则补上（缺省档通常更低）
            if re.search(r"ratio=\d+p", clean):
                clean = re.sub(r"ratio=\d+p", "ratio=1080p", clean)
            else:
                clean = clean + ("&" if "?" in clean else "?") + "ratio=1080p"
            if clean in seen:
                continue
            seen.add(clean)
            out.append({
                "url": clean,
                "width": video.get("width") or 0,
                "height": video.get("height") or 0,
                # 分享页不提供码率分档，置 0 表示「未知」而不是谎报
                "bit_rate": 0,
                "is_h265": None,
                "from_share": True,
            })
    return out


def _from_page(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """从 _ROUTER_DATA 里挖作品数据。返回 (items, meta)。"""
    item: dict[str, Any] | None = None

    def walk(node: Any):
        nonlocal item
        if item is not None:
            return
        if isinstance(node, dict):
            # 视频分享页把数据多包了一层：loaderData["video_(id)/page"]["videoInfoRes"]
            # 里才是 {status_code, item_list, filter_list, extra}。旧实现只在**顶层**
            # 找 "item_list"，于是这个节点本身没有 item_list、被整棵跳过 ——
            # 结果是「图文能解析、视频一律失败」。这里显式顺序下钻。
            for key in ("videoInfoRes", "aweme_detail", "awemeDetail"):
                sub = node.get(key)
                if isinstance(sub, dict):
                    if isinstance(sub.get("item_list"), list) and sub["item_list"]:
                        item = sub["item_list"][0]
                        return
                    if sub.get("aweme_id") and ("video" in sub or "images" in sub):
                        item = sub
                        return
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
    from_share = False
    if not cands:
        # 分享页只给带水印的 playwm 档，会被 douyin_video_candidates 过滤干净。
        # 退到「playwm → play」变换后的地址（实测返回真实 MP4，无需签名）。
        cands = _share_play_candidates(video)
        from_share = bool(cands)
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
                # 分享页拿不到码率分档，尺寸也只是元数据（实测会谎报），
                # 标出来让前端/日志知道这不是「最高码率档」的精确信息。
                "from_share": from_share,
            }
        ],
        meta,
    )


# 出站节流已下沉到 base.fetch()（见那里的 _throttle_host）：
#   · 旧实现是这里的模块级单变量，一个请求会挡住所有平台的请求；
#   · 而且只被 _from_share / 作品页两条路调用，短链展开与小红书完全没有节流。
# 现在由 fetch 统一按主机名计时并排队，这两处不再需要手动调用。


async def _from_share(aweme_id: str, headers: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """免登录分享页取数。

    分享页把作品数据嵌在 `_ROUTER_DATA` 里，**不需要 a_bogus 签名**。它的行为最接近
    「人点开分享链接」，是免登录场景下最不容易触发滑块验证的一条路。

    ⚠️ 主机名很关键：`www.iesdouyin.com` 与 `m.douyin.com` **内容不同**。
    `www.iesdouyin.com/share/video/<id>` 现在是纯客户端渲染的壳（`video_layout: null`、
    无 `item_list`）；而 `m.douyin.com/share/video/<id>` 仍会 SSR 出完整作品数据
    （实测 37KB，含 `videoInfoRes.item_list`、多档 `bit_rate`）。两者都试，取先成功者。
    """
    for tmpl in (
        "https://m.douyin.com/share/video/{id}",
        "https://www.iesdouyin.com/share/video/{id}",
        "https://m.douyin.com/share/slides/{id}",
        "https://www.iesdouyin.com/share/slides/{id}",
    ):
        try:
            resp = await fetch(tmpl.format(id=aweme_id), headers=headers, impersonate="ios")
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

    # 图文作品没有视频可下：抖音为它生成的那条是背景音，不是视频。
    # 明确说出来，避免用户以为是解析坏了。
    if meta.get("no_video_note"):
        warnings.append(meta["no_video_note"])

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

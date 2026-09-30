"""小红书笔记解析：图文原图 + 视频无水印地址。"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from .base import (
    UA_DESKTOP,
    UA_IOS,
    absorb_cookies,
    clean_text,
    extract_json_block,
    fetch,
    get_xhs_cookie,
    has_login_ticket,
    strip_cdn_params,
    xhs_clean_original,
)

NOTE_ID_RE = re.compile(r"(?:explore|discovery/item|item)/([0-9a-fA-F]{24})")
SHORT_LINK_RE = re.compile(r"https?://xhslink\.(?:com|cn)/[A-Za-z0-9/?=&_.-]+")
XHS_URL_RE = re.compile(r"https?://(?:www\.)?xiaohongshu\.com/[^\s]+")


def _pick_note_id(url: str) -> str | None:
    m = NOTE_ID_RE.search(url)
    return m.group(1) if m else None


async def _expand(url: str) -> tuple[str, str | None]:
    """短链 → 规范链接 + xsec_token。返回 (canonical_url, token)。"""
    token = None
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "xsec_token" in qs:
        token = qs["xsec_token"][0]

    note_id = _pick_note_id(url)
    if note_id:
        return url, token

    # 短链需要跟随跳转
    resp = await fetch(url, headers={"User-Agent": UA_IOS}, impersonate="safari17_0_ios")
    candidates = SHORT_LINK_RE.findall(resp.text) + XHS_URL_RE.findall(resp.text)
    for cand in candidates:
        nid = _pick_note_id(cand)
        if nid:
            q = parse_qs(urlparse(cand).query)
            token = token or q.get("xsec_token", [None])[0]
            return f"https://www.xiaohongshu.com/explore/{nid}", token

    nid = _pick_note_id(resp.url)
    if nid:
        q = parse_qs(urlparse(resp.url).query)
        token = token or q.get("xsec_token", [None])[0]
        return f"https://www.xiaohongshu.com/explore/{nid}", token
    return url, token


def _canonical(note_id: str, token: str | None) -> str:
    base = f"https://www.xiaohongshu.com/explore/{note_id}"
    if token:
        base += f"?xsec_token={token}&xsec_source=pc_share"
    return base


def _tier_of(url: str) -> str:
    u = (url or "").lower()
    if "nd_ori" in u or "wb_ori" in u:
        return "original"
    if "nd_dft" in u or "wb_dft" in u:
        return "default"
    return "preview"


def _image_entries(note: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """返回 (图片列表, 是否拿到真原图)。

    无论登录与否，都通过「干净对象键 + 图片CDN + imageView2 动态缩放」重建出
    完整原图（实测：默认档 1080 宽 → 重建后 3024×4032 真原图，无需 Cookie）。
    重建失败才回退到默认档地址。
    """
    out: list[dict[str, Any]] = []
    got_ori = False
    for img in note.get("imageList") or []:
        url_pre = img.get("urlPre") or img.get("url_pre")
        url_default = img.get("urlDefault") or img.get("url_default")
        url_ori = img.get("url")
        info_list = img.get("infoList") or []
        scene: dict[str, str] = {}
        for info in info_list:
            s = info.get("imageScene")
            if s:
                scene[s] = info.get("url") or ""
        # 任意一档都可作为重建来源
        any_url = (
            url_ori
            or scene.get("WB_ORI")
            or scene.get("WB_DFT")
            or url_default
            or scene.get("WB_PRV")
            or url_pre
        )
        if not any_url:
            continue
        # 默认档地址（带 !nd_ 后缀，永远可访问）作为失败兜底
        fallback = (
            scene.get("WB_DFT")
            or url_default
            or scene.get("WB_PRV")
            or url_pre
            or any_url
        )
        orig = xhs_clean_original(any_url)
        if orig:
            got_ori = True
            out.append(
                {
                    "kind": "image",
                    "url": orig,
                    "preview": orig,
                    "fallback": fallback,
                    "width": img.get("width"),
                    "height": img.get("height"),
                    "tier": "original",
                }
            )
        else:
            out.append(
                {
                    "kind": "image",
                    "url": any_url,
                    "preview": any_url,
                    "width": img.get("width"),
                    "height": img.get("height"),
                    "tier": _tier_of(any_url),
                }
            )
    return out, got_ori


def _media_v2(video: dict[str, Any]) -> dict[str, Any]:
    """mediaV2 可能是 JSON 字符串，也可能是 dict；统一取成 dict。"""
    mv2 = video.get("mediaV2")
    if isinstance(mv2, dict):
        return mv2
    if isinstance(mv2, str) and mv2.strip():
        try:
            got = json.loads(mv2)
            return got if isinstance(got, dict) else {}
        except Exception:
            return {}
    return {}


def _collect_video_streams(video: dict[str, Any]) -> list[dict[str, Any]]:
    """把 note.video 里所有码流档位收拢成一个列表，按「分辨率 → 码率」降序。

    两个来源都要看，且字段命名不同：
    - ``media.stream``（驼峰：masterUrl / videoBitrate / qualityType）
    - ``mediaV2``（JSON 字符串，下划线：master_url / video_bitrate / stream_type）
    旧实现只取 ``h264[0]`` 且一遇到 h264 就 break，会漏掉更高分辨率的 h265 档。
    """
    out: list[dict[str, Any]] = []

    def add(obj: Any, codec: str) -> None:
        if not isinstance(obj, dict):
            return
        url = obj.get("masterUrl") or obj.get("master_url") or ""
        backups = list(obj.get("backupUrls") or obj.get("backup_urls") or [])
        if not url and backups:
            url = backups.pop(0)
        if not url:
            return
        out.append(
            {
                "url": url,
                "backups": [b for b in backups if b],
                "width": obj.get("width") or 0,
                "height": obj.get("height") or 0,
                "bitrate": (
                    obj.get("videoBitrate")
                    or obj.get("video_bitrate")
                    or obj.get("avgBitrate")
                    or obj.get("avg_bitrate")
                    or 0
                ),
                "codec": codec or "",
                "quality": obj.get("qualityType") or obj.get("quality_type")
                or obj.get("streamDesc") or obj.get("stream_desc") or "",
                "size": obj.get("size") or 0,
            }
        )

    media = video.get("media") or {}
    stream = media.get("stream") if isinstance(media, dict) else {}
    for codec, arr in (stream.items() if isinstance(stream, dict) else []):
        for e in arr or []:
            add(e, codec)

    for codec, arr in ((_media_v2(video).get("stream") or {}).items()):
        for e in arr or []:
            add(e, codec)

    def score(c: dict[str, Any]) -> tuple:
        return (
            int(c["width"] or 0) * int(c["height"] or 0),
            int(c["bitrate"] or 0),
            1 if c["codec"] == "h264" else 0,  # 同分优先 h264，兼容性最好
        )

    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for c in sorted(out, key=score, reverse=True):
        key = c["url"].split("?", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


def _declared_original(video: dict[str, Any]) -> tuple[int, int]:
    """笔记声明的原始分辨率（mediaV2.video 或 media.video 上的 width/height）。"""
    for src in (
        (_media_v2(video).get("video") or {}),
        ((video.get("media") or {}).get("video") or {}),
    ):
        if isinstance(src, dict):
            w, h = src.get("width"), src.get("height")
            if w and h:
                try:
                    return int(w), int(h)
                except Exception:
                    pass
    return 0, 0


def _video_entry(note: dict[str, Any]) -> dict[str, Any] | None:
    video = note.get("video") or {}
    cands = _collect_video_streams(video)
    if not cands:
        return None
    top = cands[0]
    media = video.get("media") or {}
    cover_obj = video.get("cover") or {}
    cover = (
        cover_obj.get("urlPre")
        or cover_obj.get("urlDefault")
        or cover_obj.get("url_pre")
        or cover_obj.get("url_default")
        or ""
    )
    backups: list[str] = []
    for u in list(top.get("backups") or []) + [c["url"] for c in cands[1:7]]:
        if u and u not in backups:
            backups.append(u)
    return {
        "kind": "video",
        "url": top["url"],
        "backup": backups[:6],
        "cover": cover,
        "width": top.get("width") or None,
        "height": top.get("height") or None,
        "duration": (media.get("duration") if isinstance(media, dict) else None) or None,
    }


async def parse(url: str) -> dict[str, Any]:
    warnings: list[str] = []
    canonical, token = await _expand(url)
    note_id = _pick_note_id(canonical)
    if not note_id:
        raise ValueError("无法从小红书链接中识别笔记 ID，请复制完整的分享链接")

    page = _canonical(note_id, token)

    headers = {
        "User-Agent": UA_DESKTOP,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    if get_xhs_cookie():
        headers["Cookie"] = get_xhs_cookie()
    resp = await fetch(page, headers=headers, impersonate="chrome")
    # 接住平台在这次响应里滚动的 cookie（acw_tc 30 分钟一换，登录票据也会刷新），
    # 让会话尽量自己续命，减少人工回填。
    absorb_cookies(resp.url or page, resp.cookies)
    degraded = False  # 是否已因登录态失效而退回匿名请求
    if resp.status_code >= 400:
        if resp.status_code >= 500 and has_login_ticket("xhs"):
            # 实测：带无效登录态时小红书直接 500（而非干净的未登录页）。
            # 不处理的话，一个过期 Cookie 会让「本来免登录能用」的解析整个失败，
            # 比不填还糟。所以这里退回匿名请求重试，至少还能拿到 720p。
            warnings.append(
                f"页面返回 {resp.status_code}，当前登录 Cookie 很可能已失效"
                "（无效登录态会让小红书在服务端报错）。"
            )
            anon = {k: v for k, v in headers.items() if k.lower() != "cookie"}
            try:
                resp2 = await fetch(page, headers=anon, impersonate="chrome")
            except Exception:
                resp2 = None
            if resp2 is not None and resp2.status_code < 400:
                resp = resp2
                degraded = True
                warnings.append(
                    "已自动改用匿名请求重试成功：本次按未登录处理，"
                    "视频画质受网页端上限约束（最高 1080p / 单档 ≤4.5MB）。"
                    "请重新获取 Cookie 后填入以恢复登录态。"
                )
        else:
            warnings.append(f"页面返回 {resp.status_code}，可能被风控拦截")

    state = extract_json_block(resp.text, "__INITIAL_STATE__")
    # 填了 Cookie 但平台已不认（被登出 / 风控）：当场说清楚，别让人以为
    # 自己已经登录、却纳闷为什么视频还是 720p。
    if get_xhs_cookie() and not degraded and isinstance(state, dict):
        u = state.get("user") or {}
        if isinstance(u, dict) and u.get("loggedIn") is False and has_login_ticket("xhs"):
            warnings.append(
                "小红书已不认当前的登录 Cookie（可能被登出或触发风控），"
                "视频画质受网页端上限约束（最高 1080p / 单档 ≤4.5MB）。"
                "请重新获取 Cookie 后填入。"
            )
    note: dict[str, Any] | None = None
    if state:
        detail = (state.get("note") or {}).get("noteDetailMap") or {}
        node = detail.get(note_id) or {}
        note = node.get("note")
        if not note:
            for v in detail.values():
                if isinstance(v, dict) and v.get("note"):
                    note = v["note"]
                    break
    if not note:
        # 兜底：直接从 HTML 里抠图片 CDN 地址
        urls = re.findall(r"https?://sns-webpic-qc\.xhscdn\.com/[^\s\"'\\]+", resp.text)
        if urls:
            seen: list[str] = []
            items: list[dict[str, Any]] = []
            for u in urls:
                base = strip_cdn_params(u)
                if base in seen:
                    continue
                seen.append(base)
                orig = xhs_clean_original(u) or base
                items.append(
                    {
                        "kind": "image",
                        "url": orig,
                        "preview": orig,
                        "fallback": base,
                        "width": None,
                        "height": None,
                        "tier": "original" if orig != base else "default",
                    }
                )
            return {
                "platform": "xhs",
                "note_id": note_id,
                "type": "image",
                "title": "",
                "author": "",
                "cover": items[0]["preview"],
                "items": items[:18],
                "warnings": warnings + ["走的是 HTML 兜底抓取"],
            }
        warnings.append("页面未返回笔记数据，通常需要登录态 Cookie")
        raise ValueError(
            "小红书未返回笔记内容（大概率触发登录墙）。请在 config.json 填入已登录的 xhs_cookie 后重试。"
        )

    # 部分图文笔记 title/displayTitle 为空，正文写在 desc 里；取首行当标题/文件名前缀。
    # 抖音解析器本就走 desc 兜底，这里对齐，避免「保存图片时拿不到作品标题」退化成 01.jpg。
    title = clean_text(note.get("title") or note.get("displayTitle") or "")
    if not title:
        desc = clean_text(note.get("desc") or note.get("description") or "")
        if desc:
            title = desc.replace("\r", " ").replace("\n", " ").strip()
    author = ((note.get("user") or {}).get("nickname")) or ""
    items: list[dict[str, Any]] = []
    v = _video_entry(note)
    if v:
        items.append(v)
        kind = "video"
    else:
        items, got_ori = _image_entries(note)
        kind = "image"
    if not items:
        raise ValueError("笔记里没有找到可下载的图片或视频")

    # 原图通过「干净对象键 + 图片CDN + 动态缩放」重建，无需登录即可拿到真原图；
    # 仅当全部重建失败时回退默认档，并提示。
    if kind == "image" and not got_ori:
        warnings.append(
            "原图地址重建失败，已回退到平台默认档（非原图）。可重试，或填入登录 Cookie 后重试。"
        )
    elif kind == "video":
        # 实测结论（2026-09）：小红书网页端无论是否登录，都只下发三档转码流
        # streamType 301/259/309，最高 1080p、单档 ≤4.5MB。42MB 级别的「原画」是
        # 未二次编码的源文件，仅存在于小红书 App 的私签接口（edith mobile 端点，
        # 需 App 签名），网页端点（含签名调用的 web/v1/feed）一律拿不到。
        # 因此这里只如实说明当前取到的画质，不再承诺「登录后自动更高档」。
        vw, vh = _declared_original(note.get("video") or {})
        gw = items[0].get("width") or 0
        gh = items[0].get("height") or 0
        if vw and vh and vw * vh > int(gw) * int(gh):
            warnings.append(
                f"该笔记原始画质为 {vw}×{vh}，本次取到 {gw}×{gh}"
                "（小红书网页端无论是否登录都只下发 1080p 以内的转码流，单档 ≤4.5MB；"
                "更高码率的「原画」仅小红书 App 私签接口提供，网页解析器无法获取）。"
            )

    return {
        "platform": "xhs",
        "note_id": note_id,
        "type": kind,
        "title": title,
        "author": author,
        "cover": items[0].get("preview") or items[0]["url"],
        "items": items,
        "warnings": warnings,
    }

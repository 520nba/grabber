"""抖音 web API 直连（a_bogus 签名，尽力免登录）。

走官方 /aweme/v1/web/aweme/detail/ 接口，自己算 a_bogus 签名，
不依赖 yt-dlp，也不需要登录 Cookie。抖音对匿名访问会越收越紧，
所以这条路是「尽力而为」：成功就省去填 Cookie，失败再退回带 Cookie 的路径。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import string
import time
from typing import Any
from urllib.parse import quote, urlencode

from .abogus import ABogus
from .base import (
    _dy_variant_name,
    douyin_image_candidates,
    douyin_image_pick,
    douyin_video_candidates,
    fetch,
    get_douyin_cookie,
    measure_image_size,
    verify_best_image,
)

# 该算法内置的 UA 指纹对应这条 Chrome 桌面 UA；实测与签名解耦，配对容错较好。
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_BOGUS = ABogus()

_DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

# 抖音 detail 接口签名所需的常规参数（顺序与官方前端保持一致）
_BASE_PARAMS = [
    ("device_platform", "webapp"),
    ("aid", "6383"),
    ("channel", "channel_pc_web"),
    ("pc_client_type", "1"),
    ("version_code", "190500"),
    ("version_name", "19.5.0"),
    ("cookie_enabled", "true"),
    ("browser_language", "zh-CN"),
    ("browser_platform", "Win32"),
    ("browser_name", "Chrome"),
    ("browser_online", "true"),
    ("engine_name", "Blink"),
    ("os_name", "Windows"),
    ("os_version", "10"),
    ("platform", "PC"),
    ("screen_width", "1920"),
    ("screen_height", "1080"),
    ("cpu_core_num", "12"),
]


def _gen_mstoken(length: int = 148) -> str:
    """msToken 是抖音用来做风控的令牌，匿名场景下用随机串顶替。"""
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


# ── ttwid 缓存 ────────────────────────────────────────────────────
# ttwid 是字节的设备指纹 Cookie，走官方注册接口下发，有效期约一年。
# 关键：注册一次后**缓存复用**。旧实现每个请求都重新注册，短时间反复打注册接口
# 本身就是很强的机器特征，会明显抬高触发滑块验证的概率。
_TTWID: str = ""
_TTWID_AT: float = 0.0
_TTWID_TTL: float = 6 * 3600.0
# 上一次注册失败的原因（供 /api/diagnostics 显示）。原来是静默 pass，
# 于是「ttwid 拿不到」这件事在线上完全不可见。
_TTWID_LAST_ERROR: str = ""


def ttwid_last_error() -> str:
    return _TTWID_LAST_ERROR


def _register_ttwid() -> str:
    """向官方注册接口要一个 ttwid（失败返回空串，并记下原因）。"""
    global _TTWID_LAST_ERROR
    try:
        import httpx

        from .base import TIMEOUT

        r = httpx.post(
            "https://ttwid.bytedance.com/ttwid/union/register/",
            json={
                "region": "cn",
                "aid": 1768,
                "needFid": False,
                "service": "www.ixigua.com",
                "migrate_info": {"ticket": "", "source": "node"},
                "cbUrlProtocol": "https",
                "union": True,
            },
            headers={"User-Agent": UA},
            timeout=TIMEOUT,
            verify=False,
        )
        for c in r.headers.get_list("set-cookie"):
            if "ttwid=" in c:
                _TTWID_LAST_ERROR = ""
                return c.split("ttwid=", 1)[1].split(";", 1)[0]
        _TTWID_LAST_ERROR = f"HTTP {r.status_code}，但响应里没有 ttwid"
    except Exception as e:
        _TTWID_LAST_ERROR = f"{type(e).__name__}: {e}"
    return ""


def _get_ttwid(refresh: bool = False) -> str:
    """取缓存的 ttwid；缺失、过期或强制刷新时才重新注册。"""
    global _TTWID, _TTWID_AT
    now = time.time()
    if refresh or not _TTWID or (now - _TTWID_AT) > _TTWID_TTL:
        got = _register_ttwid()
        if got:
            _TTWID, _TTWID_AT = got, now
    return _TTWID


def sign_detail(aweme_id: str) -> str:
    """返回带 a_bogus 签名的 detail 请求 URL。"""
    params = list(_BASE_PARAMS)
    params.append(("msToken", _gen_mstoken()))
    params.append(("aweme_id", aweme_id))
    # urlencode 保持列表顺序（Python 3.7+ dict/列表保序）
    query = urlencode(params)
    a_bogus = _BOGUS.get_value(query, "GET")
    return _DETAIL_URL + "?" + query + "&a_bogus=" + quote(a_bogus, safe="")


async def _pick_image_verified(
    img: dict[str, Any], idx: int = 0
) -> tuple[str | None, str, str | None]:
    """选一张图，返回 (URL, 说明, 备用URL)。

    先用同步规则给出**有序候选**，再在「候选里存在多个不同变换段」时**实测**分辨
    率，挑第一个达到声明尺寸的。这样即使抖音换了缩略档的命名（原来只认
    ``~tplv-dy-shrink:``），也不会静默选到低清——判据从「猜名字」变成「量像素」。

    只有当候选里确实有多个不同处理段时才探测：单一处理段（常见情况）没有可比较
    的对象，探测只会白白多一次请求。
    """
    quick_best, quick_alt = douyin_image_pick(img)
    cands = douyin_image_candidates(img)
    if not cands:
        return quick_best, "", quick_alt

    # 用**变体名**而不是整个变换段去重：同一档的 webp/jpeg 编码不算不同画质，
    # 否则正常路径也会误触发探测（白白多几次网络往返）。
    transforms = {_dy_variant_name(u) for u in cands}
    declared = (img.get("width") or 0, img.get("height") or 0)
    if len(transforms) <= 1 or not declared[0]:
        # 没有可比较的多样性，或没有声明尺寸可对照：保持原有同步行为
        return quick_best or cands[0], "", quick_alt

    # 同档只探一次：把候选压缩成「每档取一个代表」
    seen_tr: set[str] = set()
    probes: list[str] = []
    for u in cands:
        key = _dy_variant_name(u)
        if key in seen_tr:
            continue
        seen_tr.add(key)
        probes.append(u)
        if len(probes) >= 3:
            break

    measured: dict[str, tuple[int, int] | None] = {}
    for u in probes:
        measured[u] = await measure_image_size(u)

    picked, note = verify_best_image(probes, declared, measured)
    if not picked:
        return quick_best or cands[0], note, quick_alt
    # 备用档：优先同档位、不同主机（选了哪个档就跟哪个档）
    picked_key = _dy_variant_name(picked)
    alt = next((u for u in cands if u != picked and _dy_variant_name(u) == picked_key), None) or quick_alt
    return picked, note, alt


async def extract_items(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """从 detail 接口的 JSON 里抠出视频/图文列表和元信息（图片走实测校验）。"""
    detail = payload.get("aweme_detail") or payload.get("data", {}).get("aweme_detail")
    if not detail:
        return [], {}

    meta: dict[str, Any] = {
        "title": (detail.get("desc") or "").strip(),
        "author": ((detail.get("author") or {}).get("nickname")) or "",
    }

    # 图文（收集起来，但先不 return —— 下面要判断是否还带真视频）
    images = detail.get("images")
    image_items: list[dict[str, Any]] = []
    if isinstance(images, list) and images:
        for idx, img in enumerate(images):
            picked, note, alt = await _pick_image_verified(img, idx)
            if not picked:
                continue
            item = {
                "kind": "image",
                "url": picked,
                "fallback": alt,
                "preview": picked,
                "tier": "original",
                "badge": "高清原图",
                "width": img.get("width"),
                "height": img.get("height"),
            }
            if note:
                item["pick_note"] = note
            image_items.append(item)

    # 视频：按「分辨率 → 码率」取最高清档（play_addr / play_addr_h264 /
    # play_addr_265 / bit_rate[].play_addr 统一收拢排序）
    video = detail.get("video") or {}
    # 图文作品（aweme_type=68）的 video 字段是占位：duration=0、play_addr 是
    # 720×720 方图、实际指向 content-type: audio/mp4 的背景音。
    # douyin_video_candidates() 已能撇掉这类占位；这里在「图文且没有真视频」时
    # 记一笔说明，让上层能给用户一句人话，而不是让人以为链接坏了。
    is_image_post = detail.get("aweme_type") == 68
    candidates = douyin_video_candidates(video)
    if not candidates and is_image_post and image_items:
        meta["no_video_note"] = (
            "该作品是图文（不是视频），只能取图；"
            "抖音为图文生成的那条 background audio 不是视频，已忽略。"
        )
    if not candidates and image_items:
        return image_items, meta
    if candidates:
        top = candidates[0]
        cover_obj = video.get("cover") or video.get("origin_cover") or {}
        cover = (cover_obj.get("url_list") or [None])[0] or ""
        duration = (video.get("duration") or 0) / 1000 or None
        return (
            [
                {
                    "kind": "video",
                    "url": top["url"],
                    "backup": [c["url"] for c in candidates[1:7]],
                    "cover": cover,
                    "width": top.get("width"),
                    "height": top.get("height"),
                    "bit_rate": top.get("bit_rate"),
                    "duration": duration,
                }
            ],
            meta,
        )
    return [], meta


def _cookie_header(ttwid: str) -> str:
    """拼出请求用的 Cookie。

    关键：**有用户填的 douyin cookie 就用它**。抖音详情接口现在会被 Argus 风控
    拦成 403「Uifid Not Found」——Uifid / msToken / s_v_web_id 这些都是登录态里才带的
    设备标记，光靠注册来的 ttwid 顶不住。旧实现只发 ttwid、完全无视用户填的 Cookie，
    等于「填了也白填」。这里把两者合并：用户的整行 Cookie 打底，缺 ttwid 时补上。
    """
    ck = (get_douyin_cookie() or "").strip().rstrip(";")
    if ttwid and "ttwid=" not in ck:
        ck = (ck + "; " if ck else "") + f"ttwid={ttwid}"
    if not ck:
        ck = f"ttwid={ttwid}; sid_tt="
    return ck


async def _attempt(aweme_id: str, ttwid: str) -> dict[str, Any] | None:
    """发一次 detail 请求。返回数据或 None（含返回挑战页的情况）。"""
    url = sign_detail(aweme_id)
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.douyin.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://www.douyin.com",
        # 补齐浏览器真实会带的取数请求头，降低「非浏览器」特征
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "Cookie": _cookie_header(ttwid),
    }

    try:
        resp = await fetch(url, headers=headers, impersonate="chrome")
    except Exception:
        return None

    text = resp.text or ""
    try:
        payload = json.loads(text)
    except Exception:
        # 返回的是 HTML（验证墙 / 挑战页）而非 JSON
        return None

    # 接口显式报错
    if payload.get("status_code") not in (0, None):
        return None

    items, meta = await extract_items(payload)
    if not items:
        return None

    return {
        "items": items,
        "meta": meta,
        "ttwid_used": bool(ttwid),
    }


async def web_detail(aweme_id: str) -> dict[str, Any] | None:
    """免登录直连 detail 接口。解析失败或返回空数据返回 None。

    用缓存的 ttwid 发一次；若拿到挑战页（多为 ttwid 失效或被风控标记），
    换一个新的 ttwid 再试一次，仍失败就交回上层走分享页 / yt-dlp。
    """
    # ttwid 的注册走 httpx.post（阻塞），丢进线程避免卡住事件循环；缓存命中时不发请求。
    res = await _attempt(aweme_id, await asyncio.to_thread(_get_ttwid))
    if res:
        return res
    return await _attempt(aweme_id, await asyncio.to_thread(_get_ttwid, True))

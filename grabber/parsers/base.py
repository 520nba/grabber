"""公共工具：请求会话、UA、短链展开、Cookie 配置。"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

# curl 可执行文件：Windows 是 curl.exe，Linux/容器里是 curl。找不到就跳过这条兜底。
_CURL_BIN = shutil.which("curl.exe") or shutil.which("curl")

ROOT = Path(__file__).resolve().parent.parent
# 容器里用 GRABBER_CONFIG 指向挂载出来的持久化位置（例如 /app/data/config.json），
# 这样重建镜像、拉新版代码都不会把 Cookie 弄丢。默认仍是项目根目录下的 config.json。
CONFIG_PATH = Path(os.environ.get("GRABBER_CONFIG") or (ROOT / "config.json"))

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
UA_IOS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
UA_ANDROID = (
    "Mozilla/5.0 (Linux; Android 13; SM-S918B Build/TP1A.220624.014; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/126.0.0.0 "
    "Mobile Safari/537.36"
)


def load_config() -> dict[str, Any]:
    """读取 config.json（不存在则返回空配置）。"""
    try:
        if CONFIG_PATH.is_dir():
            # Docker 常见坑：宿主上文件不存在时，bind mount 会建出一个同名**目录**。
            return {}
        if not CONFIG_PATH.exists():
            return {}
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


_config = load_config()

XHS_COOKIE: str = _config.get("xhs_cookie") or ""
DOUYIN_COOKIE: str = _config.get("douyin_cookie") or ""
PROXY: str | None = _config.get("proxy") or None
TIMEOUT: float = float(_config.get("timeout", 20))
# 下载落盘目录（下载选中媒体到 NAS 本地用）。
# 优先级：环境变量 GRABBER_DOWNLOAD_DIR > config.json 的 download_dir > 默认。
# 在 Docker/Portainer 里运行时，记得把宿主的素材目录挂进容器，并让该路径指向挂载点。
DOWNLOAD_DIR: str = (
    os.environ.get("GRABBER_DOWNLOAD_DIR")
    or _config.get("download_dir")
    or "/volume1/disk10/comfyui素材"
)
# 运行时覆盖：/api/parse 可临时带入 cookie，优先于 config.json。
# 用 ContextVar 而非模块全局——FastAPI 每个请求跑在自己独立的 asyncio task 上下文里，
# 设置后只在该请求内可见，请求结束后自动随 task 上下文丢弃，不会泄漏给后续请求，
# 也不会在并发请求间竞态。之前用全局变量，会导致「某人带的 cookie 被所有人沿用」的 bug。
_OVERRIDE_XHS: contextvars.ContextVar[str | None] = contextvars.ContextVar("xhs_override", default=None)
_OVERRIDE_DOUYIN: contextvars.ContextVar[str | None] = contextvars.ContextVar("douyin_override", default=None)


def reload_config() -> None:
    """重新读取 config.json，使 Cookie 修改即时生效（无需重启服务）。"""
    global _config, XHS_COOKIE, DOUYIN_COOKIE, PROXY, TIMEOUT, DOWNLOAD_DIR
    _config = load_config()
    XHS_COOKIE = _config.get("xhs_cookie") or ""
    DOUYIN_COOKIE = _config.get("douyin_cookie") or ""
    PROXY = _config.get("proxy") or None
    TIMEOUT = float(_config.get("timeout", 20))
    DOWNLOAD_DIR = (
        os.environ.get("GRABBER_DOWNLOAD_DIR")
        or _config.get("download_dir")
        or "/volume1/disk10/comfyui素材"
    )


def save_config(patch: dict[str, str]) -> None:
    """把 Cookie 等字段写回 config.json（文件不存在则新建）。"""
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    for k, v in patch.items():
        if k in ("xhs_cookie", "douyin_cookie", "proxy", "timeout", "download_dir"):
            cfg[k] = v
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)  # 原子替换，避免容器中途被重启写坏配置
    reload_config()


def set_override(*, xhs: str | None = None, douyin: str | None = None) -> None:
    # 只在当前请求的 task 上下文里设置，请求结束即失效（见上方说明）。
    if xhs is not None:
        _OVERRIDE_XHS.set(xhs or None)
    if douyin is not None:
        _OVERRIDE_DOUYIN.set(douyin or None)


def get_xhs_cookie() -> str:
    ov = _OVERRIDE_XHS.get()
    if ov:
        return ov
    return _merge_live("xhs", XHS_COOKIE)


def get_douyin_cookie() -> str:
    ov = _OVERRIDE_DOUYIN.get()
    if ov:
        return ov
    return _merge_live("douyin", DOUYIN_COOKIE)


# ── Cookie 自动续期 ────────────────────────────────────────────────
# 平台会在每次响应里滚动下发一批 cookie：登录票据（a1 / web_session）会随
# 会话活动刷新，风控 token（acw_tc 只有 30 分钟）更是每次都换。浏览器之所以
# 能一直保持登录，就是因为它把这些新值默默收下了。这里做同样的事：
# 把响应里的 Set-Cookie 接住 → 叠加到 config.json 的值上 → 落盘。
# 这样只要平台还认这个会话，我们就不需要人工干预。

# 票据类：一旦变化立刻落盘（丢了会影响能不能取到高清档）
_SESSION_KEYS = frozenset({
    "a1", "web_session", "webId", "gid", "xsecappid", "webBuild",
    "xsecplatform", "customerClientId", "customerClientIdV2",
    "abRequestId", "ttwid", "sessionid", "sessionid_ss", "sid_tt",
    "odin_tt", "msToken", "passport_csrf_token", "s_v_web_id",
})
# 高频轮换的风控 token：只更新内存，落盘做节流，避免每次请求都写磁盘
_VOLATILE_KEYS = frozenset({"acw_tc", "__ac_nonce", "__ac_signature", "tt_scid"})

_COOKIE_HOSTS = (
    "xiaohongshu.com", "xhscdn.com",
    "douyin.com", "iesdouyin.com", "snssdk.com", "bytedance.com", "byteimg.com",
)

# 运行时接住的新值：叠加在 config.json 之上（config 为底，live 覆盖）
_LIVE: dict[str, dict[str, str]] = {"xhs": {}, "douyin": {}}
# 落盘节流：风控 token 变化太频繁，最短间隔内只更新内存
_PERSIST_MIN_GAP = 300.0
_ABSORB: dict[str, Any] = {
    "last_persist": 0.0,
    "last_keys": [],
    "last_at": 0.0,
    "total": 0,
    "count": 0,
}


def parse_cookie(s: str) -> dict[str, str]:
    """Cookie 整行 → 字典（忽略无 = 的碎片）。"""
    out: dict[str, str] = {}
    for part in (s or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            out[k] = v
    return out


def dumps_cookie(d: dict[str, str]) -> str:
    """字典 → Cookie 整行。"""
    return "; ".join(f"{k}={v}" for k, v in d.items() if k)


def _merge_live(which: str, base: str) -> str:
    """config 里的 Cookie 串为底，叠加运行时接住的新值。"""
    merged = parse_cookie(base)
    merged.update(_LIVE.get(which) or {})
    return dumps_cookie(merged) if merged else ""


def _which_platform(url: str) -> str | None:
    host = (urlparse(url or "").netloc or "").lower()
    if not host:
        return None
    if "douyin" in host or "iesdouyin" in host or "snssdk" in host:
        return "douyin"
    if "xiaohongshu" in host or "xhscdn" in host or "xhslink" in host:
        return "xhs"
    return None


def absorb_cookies(
    url: str, cookies: dict[str, str] | None, *, persist: bool = True
) -> list[str]:
    """接住一次响应里滚动下发的 Cookie，返回真正发生变化的 key。

    票据类变化会立刻落盘；风控类只更新内存，落盘按 ``_PERSIST_MIN_GAP`` 节流。
    非目标平台的响应一律忽略，避免把 CDN 的 cookie 混进来。
    """
    if not cookies:
        return []
    which = _which_platform(url)
    if which is None:
        return []
    store = _LIVE[which]
    changed: list[str] = []
    for k, v in cookies.items():
        if not k or v is None or v == "":
            continue
        if store.get(k) != v:
            store[k] = v
            changed.append(k)
    if not changed:
        return []
    now = time.time()
    _ABSORB["last_at"] = now
    _ABSORB["last_keys"] = changed
    _ABSORB["count"] += 1
    ticket_changed = any(k in _SESSION_KEYS for k in changed)
    # 只有真填了登录票据才值得落盘：否则匿名访问接住的一堆风控 token 会把
    # config.json 写脏，还会让「用户是否已登录」的判断失真。
    if (
        persist
        and has_login_ticket(which)
        and (ticket_changed or now - _ABSORB["last_persist"] >= _PERSIST_MIN_GAP)
    ):
        _ABSORB["last_persist"] = now
        _ABSORB["total"] += len(changed)
        base = XHS_COOKIE if which == "xhs" else DOUYIN_COOKIE
        # 落盘时丢掉短效风控 token：它们跟出口 IP / CDN 边缘绑定，
        # 重启后送出去反而是脏数据（下次请求会拿到新的）。
        final = {
            k: v
            for k, v in parse_cookie(_merge_live(which, base)).items()
            if k not in _VOLATILE_KEYS
        }
        try:
            save_config({f"{which}_cookie": dumps_cookie(final)})
        except Exception:
            pass
    return changed


def has_login_ticket(which: str = "xhs") -> bool:
    """判断用户是否真的填了「登录态」Cookie。

    必须看 config.json 里用户填的值，而不是合并后的串：匿名访问本来就会下发
    ``acw_tc`` / ``abRequestId`` 这类风控 token，如果不加区分，运行期接住它们
    之后系统就会误以为「用户已登录」，进而误报登录态失效、把 config 写脏。
    """
    base = XHS_COOKIE if which == "xhs" else DOUYIN_COOKIE
    keys = set(parse_cookie(base).keys())
    if which == "xhs":
        return bool(keys & {"a1", "web_session"})
    return bool(keys & {"sessionid", "sessionid_ss", "sid_tt", "odin_tt"})


def absorb_report() -> dict[str, Any]:
    """给界面看的续期摘要（不泄漏任何 cookie 值）。"""
    return {
        "count": _ABSORB["count"],
        "total": _ABSORB["total"],
        "last_at": _ABSORB["last_at"],
        "last_keys": list(_ABSORB["last_keys"]),
        "live_keys": {k: sorted(v.keys()) for k, v in _LIVE.items()},
    }



class HttpResponse:
    __slots__ = ("status_code", "text", "url", "headers", "cookies")

    def __init__(
        self,
        status_code: int,
        text: str,
        url: str,
        headers: dict[str, str],
        cookies: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = headers
        # 平台在响应里滚动下发的 Set-Cookie（已解析成 name→value）。
        # 注意不能从 headers 里抠：dict 化会把多条 Set-Cookie 逗号拼接，
        # 而 Expires 属性本身含逗号，拆不回来。必须用客户端解析好的 cookie 对象。
        self.cookies = cookies or {}


def _client_kwargs() -> dict[str, Any]:
    kw: dict[str, Any] = {
        "timeout": TIMEOUT,
        "follow_redirects": True,
        "verify": False,  # 部分 CDN 证书链不完整，只在本地服务放宽
    }
    if PROXY:
        kw["proxy"] = PROXY
    return kw


def _curl_fallback(url: str, hdrs: dict[str, str]) -> HttpResponse | None:
    """用系统 curl 抓取（TLS 指纹比 httpx 更接近常规客户端，风控友好）。

    跨平台：Windows 走 curl.exe，Linux/容器走 curl。两者都没有时返回 None，
    由调用方继续降级到 httpx。
    """
    import os
    import subprocess
    import tempfile

    if not _CURL_BIN:
        return None

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".html")
    tmp.close()
    try:
        args = [
            _CURL_BIN,
            "-sSL",
            "--compressed",
            "--max-time",
            str(int(TIMEOUT)),
            "-o",
            tmp.name,
            "-w",
            "%{url_effective}",
            url,
        ]
        for k, v in hdrs.items():
            args += ["-H", f"{k}: {v}"]
        if PROXY:
            args += ["-x", PROXY]
        p = subprocess.run(args, capture_output=True, timeout=TIMEOUT + 10)
        final = (p.stdout.decode("utf-8", "ignore").strip() or url)
        raw = Path(tmp.name).read_bytes()
        if not raw:
            return None
        return HttpResponse(200, raw.decode("utf-8", "ignore"), final, {})
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


async def fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    impersonate: str | None = "chrome",
) -> HttpResponse:
    """拉取网页文本。优先 curl_cffi（TLS 指纹更接近真实浏览器），失败降级 httpx。"""
    hdrs = {
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        **(headers or {}),
    }
    if impersonate:
        try:
            from curl_cffi import requests as creq  # type: ignore

            def _run():
                r = creq.get(
                    url,
                    headers=hdrs,
                    impersonate=impersonate,
                    timeout=TIMEOUT,
                    proxy=PROXY,
                )
                try:
                    jar = dict(r.cookies.get_dict())
                except Exception:
                    jar = {}
                return HttpResponse(
                    r.status_code, r.text, str(r.url), dict(r.headers), jar
                )

            return await asyncio.to_thread(_run)
        except Exception:
            pass

    # 第二选择：系统 curl（真实 TLS 指纹，风控友好；Windows=curl.exe，Linux=curl）。
    # 必须丢进线程执行：subprocess.run 是阻塞调用，直接在协程里跑会卡住整个事件循环
    # （容器里没装上 curl_cffi 时每次 fetch 都走这条，等于把并发全串行化）。
    c = await asyncio.to_thread(_curl_fallback, url, hdrs)
    if c is not None:
        return c

    async with httpx.AsyncClient(**_client_kwargs()) as client:
        r = await client.get(url, headers=hdrs)
        return HttpResponse(
            r.status_code, r.text, str(r.url), dict(r.headers), dict(r.cookies)
        )


def referer_for(url: str) -> str:
    host = urlparse(url).netloc
    if "xhscdn" in host or "xiaohongshu" in host:
        return "https://www.xiaohongshu.com/"
    if "tiktok" in host:
        return "https://www.tiktok.com/"
    return "https://www.douyin.com/"


def extract_json_block(text: str, anchor: str) -> Any | None:
    """从 HTML 里抠出 `anchor = {...}</script>` 形式的 JSON。"""
    idx = text.find(anchor)
    if idx == -1:
        return None
    start = text.find("{", idx)
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                raw = text[start : i + 1]
                return _loose_json(raw)
    return None


def _loose_json(raw: str) -> Any | None:
    """JS 字面量 → JSON：undefined/null 兜底。"""
    fixed = re.sub(r"\bundefined\b", "null", raw)
    try:
        return json.loads(fixed)
    except Exception:
        return None


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"#[^\s#\[]+\[话题\]#?", "", s)
    return s.strip()


def strip_cdn_params(url: str) -> str:
    """去掉图像处理参数，尽量拿原图。"""
    if not url:
        return url
    base = url.split("?")[0]
    return base


def _img_quality_rank(u: str) -> tuple[int, int]:
    """图片地址质量打分：优先 jpeg，其次非 webp，webp 垫底。

    实测（2160×3240 同一张图）：q75 webp 仅 163KB、边缘细节最少；
    q75 jpeg 有 451KB、边缘 std 高约 14%，且 iOS 存图更友好。
    """
    is_jpeg = 1 if (".jpeg" in u or ".jpg" in u) else 0
    is_not_webp = 0 if ".webp" in u else 1
    return (is_jpeg, is_not_webp)


def _dy_transform(u: str) -> str:
    """取出字节图像 CDN 的处理段（``~tplv-...``），用于判断是否同一档画质。"""
    m = re.search(r"(~tplv-[^?]*)", u or "")
    return m.group(1) if m else ""


def _dy_is_downscaled(u: str) -> bool:
    """是否为平台预缩放的缩略档。

    分享页 / 网页接口会把 ``~tplv-dy-shrink:<w>:<h>`` 这个变体混进 ``url_list``，
    实测它只有原始尺寸的 1/1.5（1440×2160 vs 原始 2160×3240），画质明显更低。
    """
    return "~tplv-dy-shrink:" in (u or "")


def douyin_image_pick(img: dict[str, Any]) -> tuple[str | None, str | None]:
    """抖音图片选源，返回 (最佳URL, 备用URL)。

    优先 ``url_list``（干净无水印）；同档优先 jpeg。
    **关键：排除 ``~tplv-dy-shrink:`` 缩略档**——平台会把它和原始档混在同一个
    ``url_list`` 里，而 JSON 里的 ``width``/``height`` 报的是缩略尺寸（实测为原始
    的 1/1.5），所以只看元数据会误以为「已经是最清晰」，其实得靠剔除缩略档才能
    保证拿到原始分辨率。只有全是缩略档时才退而使用。
    备用档优先「同款处理段、不同 CDN 主机」的等价原尺寸地址，保证主档失败降级时
    不降画质（旧实现会把缩略 webp 当备用档，主档一失败就悄悄掉到 1440 宽）。
    ``download_url_list`` 带「抖音号：xxxx」水印，仅在 url_list 为空时兜底。
    """
    clean = [u for u in (img.get("url_list") or []) if u]
    full = [u for u in clean if not _dy_is_downscaled(u)]
    pool = full or clean
    if pool:
        best = max(pool, key=_img_quality_rank)
        best_tr = _dy_transform(best)
        # 1) 优先同款处理段、不同主机（等于同一张原尺寸图，只是换了 CDN 节点）
        alt = next((u for u in pool if u != best and _dy_transform(u) == best_tr), None)
        # 2) 退而求其次：不同格式的原尺寸档
        if not alt:
            alt = next((u for u in pool if u != best and (".webp" in u) != (".webp" in best)), None)
        if not alt:
            alt = next((u for u in pool if u != best), None)
        # 3) 最后兜底：宁可退到缩略档，也别让主档失败时彻底没图（尺寸会变小，前端会如实标出）
        if not alt:
            alt = next((u for u in clean if u != best), None)
        return best, alt
    dl = [u for u in (img.get("download_url_list") or []) if u]
    if dl:
        full = [u for u in dl if not _dy_is_downscaled(u)]
        return max(full or dl, key=_img_quality_rank), None
    return None, None


def douyin_video_candidates(video: dict[str, Any]) -> list[dict[str, Any]]:
    """抖音视频候选直链，按「分辨率 → 码率」降序，最高清在前（已剔除水印档）。

    抖音 detail 接口里同一路流会出现在多处（play_addr / play_addr_h264 /
    play_addr_265 / bit_rate[].play_addr），这里统一收拢、按质量排序并去重。
    h265 档实测为 hvc1 封装（iOS / 相册可播），故与 h264 一视同仁按码率排序。
    """
    scored: list[dict[str, Any]] = []

    def push(obj: Any, br_val: int = 0, h265: Any = None) -> None:
        if not isinstance(obj, dict):
            return
        w = obj.get("width") or 0
        h = obj.get("height") or 0
        for u in obj.get("url_list") or []:
            if not u or "playwm" in u:
                continue
            scored.append(
                {"url": u, "width": w, "height": h, "bit_rate": br_val, "is_h265": h265}
            )

    push(video.get("play_addr_h264"))
    push(video.get("play_addr_265"), 0, True)
    push(video.get("play_addr"))
    for br in video.get("bit_rate") or []:
        push(br.get("play_addr"), br.get("bit_rate") or 0, br.get("is_h265"))

    scored.sort(key=lambda c: (c["width"] * c["height"], c["bit_rate"]), reverse=True)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for c in scored:
        root = _stream_key(c["url"])
        if root in seen:
            continue
        seen.add(root)
        out.append(c)
    return out


def _stream_key(url: str) -> str:
    """把同一路流在不同 CDN 主机上的副本归一为同一个 key。"""
    try:
        p = urlparse(url)
    except Exception:
        return url.split("?", 1)[0]
    if p.path.startswith("/aweme/v1/play"):
        from urllib.parse import parse_qs

        vid = (parse_qs(p.query).get("video_id") or [""])[0]
        return f"{p.path}?video_id={vid}"
    return p.path


def xhs_clean_original(url: str) -> str | None:
    """把小红书任意档位 CDN 地址重建为「真原图」地址，无需登录。

    原理（来自实测）：XHS 图片原始对象键是 ``notes_pre_post/<key>``，
    网页下发的 ``sns-webpic-qc`` 地址带时间戳目录 + 鉴权 hash + ``!nd_xxx`` 后缀
    （如 ``.../202609130929/<hash>/notes_pre_post/<key>!nd_dft_wlteh_jpg_3``），
    其中 ``!nd_`` 后缀是「默认档」签名，删除它会 403。
    但只要把干净的对象键 ``notes_pre_post/<key>`` 拼到「图片 CDN」主机
    ``sns-img-hw.xhscdn.com`` 上，并加动态缩放指令 ``?imageView2/2/w/5000/format/jpg``
    （注意：带 /h/ 会 404，必须只用 /w/），即可拿回完整原图（如 3024×4032）。
    """
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    try:
        p = urlparse(url)
        segs = [s for s in p.path.split("/") if s]
        # 去掉时间戳目录 + 鉴权 hash（前两段，且首段为 10 位以上数字）
        if len(segs) > 2 and re.match(r"^\d{10,}$", segs[0]):
            segs = segs[2:]
        if not segs:
            return None
        clean = "/".join(segs).split("!")[0]
        if not clean:
            return None
        return f"https://sns-img-hw.xhscdn.com/{clean}?imageView2/2/w/5000/format/jpg"
    except Exception:
        return None

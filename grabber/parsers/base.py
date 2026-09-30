"""公共工具：请求会话、UA、短链展开、Cookie 配置。"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import random
import re
import shutil
import sys
import threading
import time
from collections import deque
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

# ── 出网域名白名单（全服务唯一出处）──────────────────────────────────────
# 为什么要有它：/api/parse 接收的是**用户粘贴的任意文本**，而 xhs/douyin 解析器在
# 「无法从链接里直接提取出 ID」时会把原始 URL 直接拿去出网（短链展开），
# 同时 follow_redirects=True。没有主机名校验时，`http://127.0.0.1:8765/?xhs=1`
# 这类地址会被真的请求出去 —— 即 SSRF（可探测内网、云元数据 169.254.169.254）。
#
# 判据必须是**主机名**，不能用「URL 里是否含 xhs/douyin 字样」：后者只要在查询串
# 里加 ?xhs=1 就能绕过，而且 hostname 为 None 的畸形 URL 会让判定直接抛异常。
#
# 匹配规则：精确等于，或以「.<后缀>」结尾。这样 evilxhscdn.com / xdouyin.com
# 这类「伪装成官方域名」的地址不会过检，而子域正常放行。
#
# ⚠️ 维护提醒：这些平台普遍同时使用 **.com 与 .cn** 两个后缀，而且短链与主域不同名。
# 曾经只写了 `xhslink.com`，于是用户分享的 `xhslink.cn/o/xxxx` 被判成「不支持的链接」——
# 这是从「子串匹配」改成「精确主机匹配」时最容易漏的一类。新增平台/后缀时请一起补齐，
# 并跑 tests/test_host_whitelist.py（它会对各平台的真实分享域名逐个断言）。
XHS_HOSTS = (
    "xiaohongshu.com",
    "xhslink.com",
    "xhslink.cn",     # 短链 .cn 变体（实测分享文案里很常见）
    "xhscdn.com",
    "xhscdn.cn",      # 图片 CDN 的 .cn 变体
)
DOUYIN_HOSTS = (
    "douyin.com",       # 含 v.douyin.com / www.douyin.com 等短链与主域
    "iesdouyin.com",
    "snssdk.com",
    "amemv.com",
    "ixigua.com",
)
# 资源 CDN：图片/视频代理与落盘下载放行的范围（两个平台共用）
MEDIA_HOSTS = (
    "xhscdn.com",
    "xhscdn.cn",
    "xiaohongshu.com",
    "douyinvod.com",
    "douyin.com",
    "iesdouyin.com",
    "snssdk.com",
    "amemv.com",
    "byteimg.com",
    "bytecdn.cn",
    "byteimg.cn",
    "tiktokcdn.com",
    "tiktok.com",
    "ixigua.com",
    "huoshan.com",
    "pstatp.com",
    "douyinpic.com",
    "tiktokimg.com",
    "muscdn.com",
    "tospush.com",
)

PLATFORM_HOSTS: dict[str, tuple[str, ...]] = {
    "xhs": XHS_HOSTS,
    "douyin": DOUYIN_HOSTS,
}


def host_of(url: str) -> str:
    """取 URL 的主机名（小写、已剥端口与用户信息）；无法解析时返回空串。"""
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        # 端口非法（如 http://h:99999/）时 urlparse().hostname 会抛 ValueError
        return ""


def host_allowed(url: str, suffixes) -> bool:
    """主机名是否在允许范围内：精确等于后缀，或以「.后缀」结尾。"""
    host = host_of(url)
    if not host:
        return False
    return any(host == s or host.endswith("." + s) for s in suffixes)


def assert_host_allowed(url: str, suffixes, *, what: str = "地址") -> None:
    """不在白名单内就抛 ValueError（由 server 转成 4xx/5xx 返回给前端）。"""
    if not host_allowed(url, suffixes):
        host = host_of(url) or "(无法解析)"
        raise ValueError(f"该{what}不在允许范围内：{host}")



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
    with _STATE_LOCK:
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
    """把 Cookie 等字段写回 config.json（文件不存在则新建）。

    保存后要**清掉对应平台的运行时续期值（_LIVE）**：_merge_live 是「live 覆盖
    config」，如果旧 live 不清，用户刚粘进来的新 Cookie 会被上一轮的旧值盖回去，
    表现为「保存了却好像没生效」；作废的会话票据也可能因此复活。
    """
    with _STATE_LOCK:
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
        for which in ("xhs", "douyin"):
            if f"{which}_cookie" in patch:
                _LIVE[which].clear()
        reload_config()  # RLock：同线程可重入


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
# 保护 _LIVE / _ABSORB / 几个模块级配置全局。
#
# 为什么需要：这些是模块级可变状态，而 absorb_cookies 会在**请求之间交错**执行——
# 多个 asyncio 任务在 await 点互相切换，`_ABSORB["count"] += 1` 这类「读-改-写」
# 会丢更新；_merge_live 读到半个更新状态时，拼出的 Cookie 串可能自相矛盾。
# 另外 fetch 的 curl 兜底与 yt-dlp 走 asyncio.to_thread，是真的多线程。
#
# 用 RLock 而非 Lock：save_config → reload_config 会重绑模块全局，而
# absorb_cookies 持锁期间就可能调用 save_config（同一线程重入），Lock 会死锁。
_STATE_LOCK = threading.RLock()


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
    with _STATE_LOCK:
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
    changed: list[str] = []
    now = time.time()
    # 整段「比对 + 写入 + 计数」必须在同一把锁里：否则并发请求在 await 点交错时，
    # `_ABSORB["count"] += 1` 会丢更新，且可能读到半更新的 _LIVE 拼出矛盾 Cookie 串。
    with _STATE_LOCK:
        store = _LIVE[which]
        for k, v in cookies.items():
            if not k or v is None or v == "":
                continue
            if store.get(k) != v:
                store[k] = v
                changed.append(k)
        if not changed:
            return []
        _ABSORB["last_at"] = now
        _ABSORB["last_keys"] = changed
        _ABSORB["count"] += 1
        ticket_changed = any(k in _SESSION_KEYS for k in changed)
        # 只有真填了登录票据才值得落盘：否则匿名访问接住的一堆风控 token 会把
        # config.json 写脏，还会让「用户是否已登录」的判断失真。
        should_persist = (
            persist
            and has_login_ticket(which)
            and (ticket_changed or now - _ABSORB["last_persist"] >= _PERSIST_MIN_GAP)
        )
        if should_persist:
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
    with _STATE_LOCK:
        return {
            "count": _ABSORB["count"],
            "total": _ABSORB["total"],
            "last_at": _ABSORB["last_at"],
            "last_keys": list(_ABSORB["last_keys"]),
            "live_keys": {k: sorted(v.keys()) for k, v in _LIVE.items()},
        }



class HttpResponse:
    __slots__ = ("status_code", "text", "url", "headers", "cookies", "via", "degrades")

    def __init__(
        self,
        status_code: int,
        text: str,
        url: str,
        headers: dict[str, str],
        cookies: dict[str, str] | None = None,
        # 真正取到数据的通道：curl_cffi / curl / httpx。
        # 降级链原来是全静默的，线上只看到最终 403，完全不知道中间发生了什么。
        via: str = "",
        # 本次请求过程中**失败过**的通道及原因，例如
        # ["curl_cffi: ImportError ...", "curl: 退出码 28"]。
        degrades: list[str] | None = None,
    ):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = headers
        # 平台在响应里滚动下发的 Set-Cookie（已解析成 name→value）。
        # 注意不能从 headers 里抠：dict 化会把多条 Set-Cookie 逗号拼接，
        # 而 Expires 属性本身含逗号，拆不回来。必须用客户端解析好的 cookie 对象。
        self.cookies = cookies or {}
        self.via = via
        self.degrades = degrades or []


def _client_kwargs() -> dict[str, Any]:
    kw: dict[str, Any] = {
        "timeout": TIMEOUT,
        "follow_redirects": True,
        "verify": False,  # 部分 CDN 证书链不完整，只在本地服务放宽
    }
    if PROXY:
        kw["proxy"] = PROXY
    return kw


def _curl_fallback(
    url: str, hdrs: dict[str, str], degrades: list[str] | None = None
) -> HttpResponse | None:
    """用系统 curl 抓取（TLS 指纹比 httpx 更接近常规客户端，风控友好）。

    跨平台：Windows 走 curl.exe，Linux/容器走 curl。两者都没有时返回 None，
    由调用方继续降级到 httpx。失败原因写入 degrades（原来是静默 return None）。
    """
    import os
    import subprocess
    import tempfile

    def _note(msg: str) -> None:
        if degrades is not None:
            degrades.append(f"curl: {msg}")

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
            err = (p.stderr or b"").decode("utf-8", "ignore").strip()[:160]
            _note(f"返回空响应（退出码 {p.returncode}{'，' + err if err else ''}）")
            return None
        if p.returncode != 0:
            err = (p.stderr or b"").decode("utf-8", "ignore").strip()[:160]
            _note(f"退出码 {p.returncode}{'，' + err if err else ''}（仍取了 {len(raw)} 字节）")
        return HttpResponse(200, raw.decode("utf-8", "ignore"), final, {})
    except subprocess.TimeoutExpired:
        _note(f"超时（{TIMEOUT + 10:.0f}s）")
        return None
    except Exception as e:
        _note(f"{type(e).__name__}: {e}")
        return None
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


# ── 出网诊断环形缓冲 ──────────────────────────────────────────────────
# 为什么需要：降级链（curl_cffi → curl → httpx）原来全静默，线上只看到最终 403，
# 完全看不出中间挂了哪一步。这里保留最近 N 次出网的通道与降级原因，
# 供 /api/diagnostics 查看。有界且只留摘要，不会随运行时间增长，也不落盘。
_RECENT_FETCHES: "deque[dict[str, Any]]" = deque(maxlen=50)
_VERBOSE_DEGRADE = os.environ.get("GRABBER_VERBOSE_DEGRADE", "0") == "1"


def _record_fetch(url: str, via: str, status: int, degrades: list[str]) -> None:
    # 每请求诊断：写进 parsers.DIAG（ContextVar），由 parse() 取出附到响应上。
    # 惰性取包对象是为了避开 base ←→ __init__ 的循环导入。
    if degrades:
        try:
            pkg = sys.modules.get("parsers")
            diag = getattr(pkg, "DIAG", None) if pkg is not None else None
            buf = diag.get() if diag is not None else None
            if isinstance(buf, list):
                buf.append({"host": (urlparse(url or "").hostname or ""),
                            "via": via, "status": status, "degrades": list(degrades)})
        except Exception:
            pass
    if not degrades and not _VERBOSE_DEGRADE:
        return  # 正常路径不入缓冲，避免把表刷满
    _RECENT_FETCHES.append({
        "at": time.time(),
        "host": (urlparse(url or "").hostname or ""),
        "via": via,
        "status": status,
        "degrades": list(degrades),
    })


def fetch_diagnostics() -> dict[str, Any]:
    """出网诊断摘要：最近的降级记录 + 各通道可用性。"""
    curl_cffi_ok = True
    curl_cffi_err = ""
    try:
        import curl_cffi  # noqa: F401
    except Exception as e:
        curl_cffi_ok = False
        curl_cffi_err = f"{type(e).__name__}: {e}"
    return {
        "curl_cffi_available": curl_cffi_ok,
        "curl_cffi_error": curl_cffi_err,
        "curl_binary": _CURL_BIN or "",
        "proxy": bool(PROXY),
        "timeout": TIMEOUT,
        "host_gaps": dict(_HOST_GAP),
        "default_gap": _DEFAULT_GAP,
        "recent_degrades": list(_RECENT_FETCHES),
    }


# ── 请求指纹（impersonate）解析 ────────────────────────────────────────
# 为什么不能直接写死一个字符串：curl_cffi 会**改名**。实测 0.16.3 里
# `safari17_0_ios` 已不存在（改名成 `safari17_2_ios` / `safari18_0_ios` 这类
# 带下划线的形式），而代码里原本写的就是这个名字 —— 于是每一项请求都抛
# ImpersonateError，静默降级到系统 curl，白白丢掉了 curl_cffi 的 TLS 指纹优势。
#
# 修法不是「换成某个新名字」（下次还会过期），而是**探测一次 + 缓存 + 回退**：
#   1. 按偏好顺序列出候选（既有新命名也有历史命名）；
#   2. 用一次 HEAD 实测哪个能用（ImpersonateError 明确表示不支持）；
#   3. 缓存结果；全都不行时返回 None，由调用方降级到系统 curl；
#   4. 非 iOS 语境默认用 `chrome`，同样会先探测。
# 这样 curl_cffi 任意版本改名、甚至卸载，行为都是「自动选到能用的那个」。
_IOS_IMPERSONATE_CANDIDATES = (
    "safari18_0_ios",   # 0.16.x 命名
    "safari17_2_ios",
    "safari184_ios",
    "safari260_ios",
    "safari17_0_ios",   # 旧命名，老版本仍可能只有它
    "safari15_5_ios",
)
_CHROME_IMPERSONATE_CANDIDATES = (
    "chrome",           # 指向该版本默认的较新 Chrome
    "chrome136",
    "chrome131",
    "chrome124",
)
_IMPERSONATE_PREFIX = {
    # 语义化标记：调用方说「要 iOS 的指纹」，具体用哪个版本交给解析器决定，
    # 这样 curl_cffi 再改名也不用动调用点。
    "ios": _IOS_IMPERSONATE_CANDIDATES,
    "chrome": _CHROME_IMPERSONATE_CANDIDATES,
    # 兼容仍写成历史名字的调用点
    "safari17_0_ios": _IOS_IMPERSONATE_CANDIDATES,
    "safari15_5": _CHROME_IMPERSONATE_CANDIDATES,   # 桌面 safari，走通用桌面候选
}
_IMPERSONATE_CACHE: dict[str, str | None] = {}
_IMPERSONATE_TRIED: dict[str, list[str]] = {}
# 探测用的「靶子」：一个必然连不上的本地代理。
# 这样探测**完全不依赖外网**——指纹名不受支持时 curl_cffi 在构造阶段就抛
# ImpersonateError（实测 0.001s），支持时才会去连代理、立刻失败（瞬时）。
# 不用真实站点是为了避免把「选指纹」这件事系在外网可达性上：机房 IP 被墙、
# 断网、或目标站限流都会让所有候选都被误判成不可用，从而整体退化到系统 curl。
_PROBE_PROXY = "http://127.0.0.1:1"


def _resolve_impersonate(want: str) -> str | None:
    """把「想要的指纹」解析成 curl_cffi 当前版本真正支持的那个；都不行返回 None。

    只探测一次并缓存：同名请求后续直接命中缓存，不额外发网络请求。
    """
    if not want:
        return None
    if want in _IMPERSONATE_CACHE:
        return _IMPERSONATE_CACHE[want]
    try:
        from curl_cffi import requests as creq  # type: ignore
        from curl_cffi.requests.exceptions import ImpersonateError  # type: ignore
    except Exception:
        _IMPERSONATE_CACHE[want] = None
        _IMPERSONATE_TRIED[want] = ["curl_cffi 不可用"]
        return None

    tried: list[str] = []
    # 先试候选表；若 want 本身不在表里（调用方给了别的名字）也一并试它。
    # 注意排除纯语义标记（"ios"/"chrome"）——它们不是真实指纹名，试了必然失败。
    cands = list(_IMPERSONATE_PREFIX.get(want, ())) or []
    if want not in cands and want not in _IMPERSONATE_PREFIX:
        cands.insert(0, want)
    for t in cands:
        tried.append(t)
        try:
            creq.get("http://127.0.0.1:1/", impersonate=t,
                     timeout=1, proxy=_PROBE_PROXY)
            # 居然连通了（几乎不可能）；能走到这也说明指纹名是有效的
            ok = True
        except ImpersonateError:
            ok = False           # 名字不被这个版本支持
        except Exception:
            ok = True            # 名字有效，只是连不上靶子
        if ok:
            _IMPERSONATE_CACHE[want] = t
            _IMPERSONATE_TRIED[want] = tried
            if t != want:
                _warn_once(f"fingerprint 已从 {want} 自动切到 {t}（curl_cffi 改名）")
            return t
    _IMPERSONATE_CACHE[want] = None
    _IMPERSONATE_TRIED[want] = tried
    _warn_once(f"没有可用的 impersonate 目标（试过 {', '.join(tried)}），将降级到系统 curl")
    return None


_IMPERSONATE_WARNED: set[str] = set()


def _warn_once(msg: str) -> None:
    """同一个问题只提示一次，避免每请求刷屏。"""
    if msg in _IMPERSONATE_WARNED:
        return
    _IMPERSONATE_WARNED.add(msg)
    print(f"[grabber] {msg}", flush=True)


def impersonate_diagnostics() -> dict[str, Any]:
    """供 /api/diagnostics 展示当前指纹选择情况。"""
    return {
        "resolved": {k: v for k, v in _IMPERSONATE_CACHE.items()},
        "candidates": {k: list(v) for k, v in _IMPERSONATE_TRIED.items()},
        "ios_candidates": list(_IOS_IMPERSONATE_CANDIDATES),
    }


def _is_ios_target(name: str) -> bool:
    return "ios" in (name or "").lower()


async def fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    impersonate: str | None = "chrome",
    allow_hosts: tuple[str, ...] | None = None,
    throttle: bool = True,
) -> HttpResponse:
    """拉取网页文本。优先 curl_cffi（TLS 指纹更接近真实浏览器），失败降级 httpx。

    allow_hosts 给定时，**请求前**与**跟随重定向后**都校验主机名：
    重定向是绕过域名白名单的经典路径（合法短链 302 到内网地址），
    只校验初始 URL 挡不住，所以最终地址必须再验一次。

    throttle=True（默认）时先过**按主机**的节流（见 _throttle_host）：
    节流放在这里而不是各平台模块里，是为了让所有出网路径统一受约束——
    之前 douyin.py 里的 _throttle 只覆盖分享页/作品页两条路，短链展开和小红书
    完全没有节流，而且它是全进程单通道，会把不相关的请求互相挡住。

    降级链的每一步失败都会记进返回值的 `degrades`，`via` 标明最终用哪条通道拿到数据。
    """
    degrades: list[str] = []
    if throttle:
        await _throttle_host(url)

    hdrs = {
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        **(headers or {}),
    }
    if allow_hosts is not None:
        assert_host_allowed(url, allow_hosts)

    def _verify_final(final_url: str) -> None:
        if allow_hosts is not None and final_url:
            assert_host_allowed(final_url, allow_hosts, what="跳转地址")

    if impersonate:
        # 先把「想要的指纹」解析成当前 curl_cffi 真正支持的那个（见 _resolve_impersonate）。
        # 解析失败(全部不可用)时走下面的系统 curl，而不是让每次请求都抛异常。
        resolved = await asyncio.to_thread(_resolve_impersonate, impersonate)
        if resolved is None:
            degrades.append(
                f"curl_cffi: 无可用 impersonate 目标（想要 {impersonate}）"
            )
        else:
            try:
                from curl_cffi import requests as creq  # type: ignore
            except Exception as e:
                degrades.append(f"curl_cffi: 导入失败 {type(e).__name__}: {e}")
            else:
                # 指纹是 iOS 时 UA 也要一致：curl_cffi 会自带一套 UA，但我们显式传了
                # headers 就会覆盖它。原来固定用 UA_IOS，若最终回退到 chrome 指纹，
                # 就会出现「Chrome 的 TLS 指纹 + iPhone 的 UA」这种自相矛盾的特征。
                if _is_ios_target(resolved) and "User-Agent" not in (headers or {}):
                    hdrs["User-Agent"] = UA_IOS
                elif not _is_ios_target(resolved) and "User-Agent" not in (headers or {}):
                    hdrs["User-Agent"] = UA_DESKTOP

                def _run():
                    r = creq.get(
                        url,
                        headers=hdrs,
                        impersonate=resolved,
                        timeout=TIMEOUT,
                        proxy=PROXY,
                    )
                    try:
                        jar = dict(r.cookies.get_dict())
                    except Exception:
                        jar = {}
                    _verify_final(str(r.url))
                    return HttpResponse(
                        r.status_code, r.text, str(r.url), dict(r.headers), jar,
                        via=f"curl_cffi({resolved})", degrades=list(degrades),
                    )

                try:
                    r = await asyncio.to_thread(_run)
                    _record_fetch(url, f"curl_cffi({resolved})", r.status_code, degrades)
                    return r
                except ValueError:
                    raise  # 白名单校验失败必须往上抛，不能被下面的降级吞掉
                except Exception as e:
                    # 这里**不能沉默**：这是最常见的静默降级点，线上排障就卡在这
                    degrades.append(f"curl_cffi({resolved}): {type(e).__name__}: {e}")

    # 第二选择：系统 curl（真实 TLS 指纹，风控友好；Windows=curl.exe，Linux=curl）。
    # 必须丢进线程执行：subprocess.run 是阻塞调用，直接在协程里跑会卡住整个事件循环
    # （容器里没装上 curl_cffi 时每次 fetch 都走这条，等于把并发全串行化）。
    c = await asyncio.to_thread(_curl_fallback, url, hdrs, degrades)
    if c is not None:
        _verify_final(c.url)
        c.via = "curl"
        c.degrades = list(degrades)
        _record_fetch(url, "curl", c.status_code, degrades)
        return c
    if not _CURL_BIN:
        degrades.append("curl: 系统中找不到 curl 可执行文件")

    async with httpx.AsyncClient(**_client_kwargs()) as client:
        r = await client.get(url, headers=hdrs)
        _verify_final(str(r.url))
        _record_fetch(url, "httpx", r.status_code, degrades)
        return HttpResponse(
            r.status_code, r.text, str(r.url), dict(r.headers), dict(r.cookies),
            via="httpx", degrades=list(degrades),
        )


# ── 按主机节流 ────────────────────────────────────────────────────────
# 目的：把「连点解析 / 批量解析」造成的密集请求摊开，降低风控概率。
# 与旧实现的差别：
#   1. 按**主机名**分别计时（旧的是模块级单变量，一个请求会挡住所有平台）；
#   2. 对所有出网路径生效（旧的在 douyin.py 里，只被两条路调用）；
#   3. 对同一主机**串行排队**而不是各等各的——async 下若只 sleep 不排队，
#      并发的 N 个请求会在同一时刻集体发起，节流形同虚设。
# 默认间隔（秒）。可用环境变量覆盖，例如 GRABBER_GAP_DOUYIN=1 降低力度、
# GRABBER_GAP_DEFAULT=0 基本关闭。调大更不容易触发风控，但连续解析多个链接会变慢。
_HOST_GAP = {
    "www.douyin.com": float(os.environ.get("GRABBER_GAP_DOUYIN", "2.0")),
    "www.iesdouyin.com": float(os.environ.get("GRABBER_GAP_DOUYIN", "2.0")),
    "www.xiaohongshu.com": float(os.environ.get("GRABBER_GAP_XHS", "1.5")),
}
_DEFAULT_GAP = float(os.environ.get("GRABBER_GAP_DEFAULT", "0.8"))
# 抖动上限：避免每次都卡在整秒上发起，看起来更像人工
_JITTER = float(os.environ.get("GRABBER_GAP_JITTER", "0.4"))
_last_host_at: dict[str, float] = {}
_host_lock: asyncio.Lock | None = None


def _loop_lock() -> asyncio.Lock:
    """惰性创建锁：asyncio.Lock 必须在运行中的 loop 里创建，
    模块级创建会在 import 时就绑定 loop，遇到多 loop 场景即失效。"""
    global _host_lock
    if _host_lock is None:
        _host_lock = asyncio.Lock()
    return _host_lock


def _throttle_gap_for(host: str) -> float:
    for k, v in _HOST_GAP.items():
        if host == k or host.endswith("." + k) or host.endswith(k):
            return v
    return _DEFAULT_GAP


async def _throttle_host(url: str) -> None:
    """同一主机两次出网之间至少间隔 gap 秒（超出部分加一点抖动）。"""
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return
    gap = _throttle_gap_for(host)
    lock = _loop_lock()
    async with lock:
        loop = asyncio.get_running_loop()
        wait = gap - (loop.time() - _last_host_at.get(host, 0.0))
        # 先占位再 sleep：否则并发请求会读到同一个「上次时间」而一起冲出去
        _last_host_at[host] = loop.time() + max(0.0, wait)
        # 防止长期运行的进程把这张表撑大（每个新主机都会留一条）
        if len(_last_host_at) > 256:
            cutoff = loop.time() - 3600
            for h in [h for h, t in _last_host_at.items() if t < cutoff]:
                _last_host_at.pop(h, None)
    if wait > 0:
        # 抖动：避免每次都在整秒上发起，看起来更像人工
        await asyncio.sleep(wait + random.uniform(0.0, _JITTER))


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
    """是否为平台预缩放的缩略档**（按其命名猜测）**。

    分享页 / 网页接口会把 ``~tplv-dy-shrink:<w>:<h>`` 这个变体混进 ``url_list``，
    实测它只有原始尺寸的 1/1.5（1440×2160 vs 原始 2160×3240），画质明显更低。

    ⚠️ 这是**从命名反推**的判据，只认这一个字符串。抖音换一个变体名
    （``~tplv-resize:`` / ``~tplv-image-cover:`` / 或全新名字）它就失效，
    而且失效方式是**静默选到低清**——不报错、界面还标着「高清原图」。
    所以它只作为**快速初筛**保留；真正的判据是下面按实测像素做的校验
    （见 measure_image_size / douyin_image_candidates），命名变了也不会误判。
    """
    return "~tplv-dy-shrink:" in (u or "")


# ── 实测图片真实像素 ──────────────────────────────────────────────────
# 为什么需要：仅靠变换段命名猜「哪个是原图」是脆的（见 _dy_is_downscaled 的说明）。
# 这里改成**量出来**：只取前若干字节读图片头，拿到真实宽高，
# 再和接口声明的原始尺寸比。命名怎么变都不影响这个判据。
#
# 注意不要用 Content-Length 代替：同尺寸的 WebP 可能比缩过尺寸的 JPEG 还小，
# 体积不能可靠反映分辨率（jpeg/webp 压缩率差异太大）。
PROBE_BYTES = int(os.environ.get("GRABBER_PROBE_BYTES", str(64 * 1024)))
_PROBE_IMAGE = os.environ.get("GRABBER_PROBE_IMAGE_SIZE", "1") != "0"
# 每个 URL 的实测结果缓存（同一张图在一次解析里可能被问多次）
_size_cache: dict[str, tuple[int, int] | None] = {}


def image_dimensions_from_bytes(raw: bytes) -> tuple[int, int] | None:
    """从图片头部字节解析真实宽高。支持 JPEG / PNG / WebP / GIF。

    只读元数据段，不解码像素——所以取几十 KB 就够（JPEG 的 SOF 段可能排在
    较大的 EXIF 之后，PROBE_BYTES 给了余量）。
    """
    if not raw or len(raw) < 16:
        return None

    # PNG: 固定 8 字节签名 + IHDR
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
        try:
            w = int.from_bytes(raw[16:20], "big")
            h = int.from_bytes(raw[20:24], "big")
            return (w, h) if w and h else None
        except Exception:
            return None

    # GIF: 固定偏移
    if raw[:6] in (b"GIF87a", b"GIF89a") and len(raw) >= 10:
        w = int.from_bytes(raw[6:8], "little")
        h = int.from_bytes(raw[8:10], "little")
        return (w, h) if w and h else None

    # WebP: RIFF....WEBP + VP8/VP8L/VP8X
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        fmt = raw[12:16]
        try:
            if fmt == b"VP8 " and len(raw) >= 30:
                # 帧头： 00 00 00 9d 01 2a <w:2 LE> <h:2 LE>（14 位有效）
                w = int.from_bytes(raw[26:28], "little") & 0x3FFF
                h = int.from_bytes(raw[28:30], "little") & 0x3FFF
                return (w, h) if w and h else None
            if fmt == b"VP8L" and len(raw) >= 25:
                b = int.from_bytes(raw[21:25], "little")
                w = (b & 0x3FFF) + 1
                h = ((b >> 14) & 0x3FFF) + 1
                return (w, h) if w and h else None
            if fmt == b"VP8X" and len(raw) >= 30:
                w = int.from_bytes(raw[24:27], "little") + 1
                h = int.from_bytes(raw[27:30], "little") + 1
                return (w, h) if w and h else None
        except Exception:
            return None
        return None

    # JPEG: 扫段找 SOFn（跳过其它标记段）
    if raw[:2] == b"\xff\xd8":
        i = 2
        n = len(raw)
        while i + 9 < n:
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            # 填充字节
            if marker == 0xFF:
                i += 1
                continue
            # 无长度字段的标记
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = int.from_bytes(raw[i + 2 : i + 4], "big")
            # SOF0..SOF15（排除 DHT=C4, JPG=C8, DAC=CC）
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if i + 9 <= n:
                    h = int.from_bytes(raw[i + 5 : i + 7], "big")
                    w = int.from_bytes(raw[i + 7 : i + 9], "big")
                    return (w, h) if w and h else None
                return None
            if seg_len < 2:
                return None
            i += 2 + seg_len
        return None
    return None


async def measure_image_size(url: str, *, client=None) -> tuple[int, int] | None:
    """用一次 Range 请求量出图片真实宽高；量不出返回 None（调用方应视为「未知」）。

    刻意**不抛异常**：探测失败（CDN 拒绝 Range、超时、签名过期）不应让解析失败，
    只是失去这层增强，调用方回退到原有行为。
    """
    if not _PROBE_IMAGE or not url:
        return None
    if url in _size_cache:
        return _size_cache[url]
    result: tuple[int, int] | None = None
    try:
        kw = _client_kwargs()
        if client is not None:
            r = await client.get(
                url, headers={"Range": f"bytes=0-{PROBE_BYTES - 1}",
                              "User-Agent": UA_DESKTOP,
                              "Referer": referer_for(url)},
            )
        else:
            async with httpx.AsyncClient(**kw) as c:
                r = await c.get(
                    url, headers={"Range": f"bytes=0-{PROBE_BYTES - 1}",
                                  "User-Agent": UA_DESKTOP,
                                  "Referer": referer_for(url)},
                )
        if r.status_code < 400:
            result = image_dimensions_from_bytes(r.content)
    except Exception:
        result = None
    # 只缓存成功结果：失败往往是签名过期之类的临时情况，不该被记住
    if result:
        _size_cache[url] = result
    return result


def _probe_disabled_reason() -> str:
    if not _PROBE_IMAGE:
        return "GRABBER_PROBE_IMAGE_SIZE=0 已关闭"
    return ""


def _dy_transform_has_size(tr: str) -> bool:
    """变换段里是否**写明了目标尺寸**（如 ``shrink:1440:2559:q75``）。

    写明了尺寸的档位天然可疑（等于平台替我们决定要多大）；
    没写尺寸的才可能是原尺寸档。
    """
    return bool(re.search(r":\d{2,5}:\d{2,5}", tr or ""))


def _dy_variant_name(u: str) -> str:
    """取变换段的**变体名**（丢掉质量/格式后缀），用于判断「是不是同一档」。

    为什么不能直接比较整个变换段：``~tplv-dy-aweme-images:q75.webp`` 与
    ``...:q75.jpeg`` 是**同一档位的两种编码**（同一尺寸、同一质量，只是格式不同）。
    若把它们当成两种变换段，就会误以为「候选里有多种画质」而去发探测请求——
    正常路径上白白多几次网络往返，把解析拖慢。
    真正的画质档位差异体现在变体名与尺寸参数上：
      ``dy-shrink:1440:2559:q75`` vs ``dy-aweme-images:q75`` → 变体名不同 → 不同档 ✓
      ``dy-aweme-images:q75`` webp vs jpeg     → 变体名相同 → 同一档 ✓
    """
    m = re.search(r"~tplv-([^:?]+)", u or "")
    if not m:
        return u or ""
    name = m.group(1)
    tr = _dy_transform(u)
    # 变体名里**写死了尺寸**时也算作不同的档（如 dy-shrink:1440:2559 的标准写法
    # 变体名就叫 dy-shrink，上面已区分；这里兜住「尺寸写在名字里」的形态）
    size_m = re.search(r":(\d{2,5}):(\d{2,5})", tr or "")
    return f"{name}:{size_m.group(1)}x{size_m.group(2)}" if size_m else name


def douyin_image_candidates(img: dict[str, Any]) -> list[str]:
    """按「画质优先」排序的候选 URL 列表（首个最可能是原图）。

    与 douyin_image_pick 的区别：不再试图一次猜对，而是给出**有序候选**，
    让上层用实测像素去验证（见 verify_best_image）。

    排除缩略档分两层：
      1. 命名层（快）：``~tplv-dy-shrink:``，以及变换段里**写死了尺寸**的
         （形如 ``:1440:2559:``）——写死尺寸本身就是替我们决定要多大；
      2. 实测层（准）：由 verify_best_image 真正量出像素后判定。
    只有全都被排除时才退而使用它们，避免「一个都不剩」导致无图可用。
    """
    def rank(u: str) -> tuple[int, int, int]:
        tr = _dy_transform(u)
        named_shrink = 1 if _dy_is_downscaled(u) else 0
        sized = 1 if _dy_transform_has_size(tr) else 0
        return (named_shrink, sized, 0)

    clean = [u for u in (img.get("url_list") or []) if u]
    if not clean:
        clean = [u for u in (img.get("download_url_list") or []) if u]
    if not clean:
        return []

    # 主排序：不是明显缩略档的优先；同档内按格式质量（jpeg > 非 webp > webp）
    def sort_key(u: str) -> tuple:
        named_shrink, sized, _ = rank(u)
        jpeg, not_webp = _img_quality_rank(u)
        # 越大越优先 → 对「小即好」的字段取负
        return (-named_shrink, -sized, jpeg, not_webp)

    ordered = sorted(clean, key=sort_key, reverse=True)
    return ordered


def verify_best_image(
    candidates: list[str],
    declared: tuple[int, int] | None,
    measured: dict[str, tuple[int, int] | None],
) -> tuple[str | None, str]:
    """在候选里挑第一个「实测尺寸 ≥ 声明尺寸」的；挑不出就用第一个。

    返回 (选中URL, 说明)。measured 是调用方已经量好的 {url: (w,h) | None}。

    为什么用「实测 vs 声明」而不是「实测 vs 其它候选」：抖音 JSON 里的
    width/height 实测会谎报成原始尺寸的 1/1.5，所以**声明值只能当期望上限**，
    不能当精确目标。拿它做门槛，缩略档（更小）会被挡掉，原图（更大或相等）会通过。
    """
    if not candidates:
        return None, "无候选"
    dw, dh = (declared or (0, 0))
    # 声明值不可靠时不强行判定，直接取首选，避免把好图误杀
    if not dw or not dh:
        return candidates[0], "无声明尺寸可对照，取首选"
    for u in candidates:
        got = measured.get(u)
        if not got:
            continue
        if got[0] >= dw and got[1] >= dh:
            return u, f"实测 {got[0]}×{got[1]} ≥ 声明 {dw}×{dh}"
    # 全都没量出来 → 不改行为
    if not any(measured.get(u) for u in candidates):
        return candidates[0], _probe_disabled_reason() or "全部探测失败，回退到首选"
    # 量出来了但都小于声明：取其中最大的那个（总比盲选强）
    best = max((u for u in candidates if measured.get(u)),
               key=lambda u: measured[u][0] * measured[u][1])
    got = measured[best]
    return best, f"候选实测均小于声明 {dw}×{dh}，取最大的 {got[0]}×{got[1]}"


def douyin_image_pick(img: dict[str, Any]) -> tuple[str | None, str | None]:
    """抖音图片选源，返回 (最佳URL, 备用URL)。**同步快速路径**。

    优先 ``url_list``（干净无水印）；同档优先 jpeg。
    排除 ``~tplv-dy-shrink:`` 缩略档——见 _dy_is_downscaled 的说明（该判据是
    按命名猜的，会随平台改名失效；需要精确判定时走 douyin_image_candidates +
    verify_best_image 那条实测路径）。
    备用档优先「同款处理段、不同 CDN 主机」的等价原尺寸地址，保证主档失败降级时
    不降画质（旧实现会把缩略 webp 当备用档，主档一失败就悄悄掉到 1440 宽）。
    ``download_url_list`` 带「抖音号：xxxx」水印，仅在 url_list 为空时兜底。
    """
    clean = [u for u in (img.get("url_list") or []) if u]
    full = [u for u in clean if not _dy_is_downscaled(u)]
    pool = full or clean
    if pool:
        # 同样排除「变换段里写死了尺寸」的档，再挑格式最好的
        tight = [u for u in pool if not _dy_transform_has_size(_dy_transform(u))]
        best = max(tight or pool, key=_img_quality_rank)
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


_DY_AUDIO_HINTS = ("douyinstatic.com", "tplv-dy-audio", "/audio/", "music")


def _dy_is_audio_stream(u: str) -> bool:
    """该直链是否为纯音频资源（图文作品会自动生成一条背景音）。

    实测判据：图文作品的背景音托管在 ``*.douyinstatic.com`` 的
    ``/obj/tos-cn-ve-*`` 路径下，请求回来是 ``content-type: audio/mp4``。
    真正的视频流走 ``douyinvod.com`` 或 ``/aweme/v1/play``。
    """
    low = (u or "").lower()
    return any(h in low for h in _DY_AUDIO_HINTS)


def douyin_video_candidates(video: dict[str, Any]) -> list[dict[str, Any]]:
    """抖音视频候选直链，按「分辨率 → 码率」降序，最高清在前（已剔除水印档）。

    抖音 detail 接口里同一路流会出现在多处（play_addr / play_addr_h264 /
    play_addr_265 / bit_rate[].play_addr），这里统一收拢、按质量排序并去重。
    h265 档实测为 hvc1 封装（iOS / 相册可播），故与 h264 一视同仁按码率排序。

    ⚠️ 图文作品（aweme_type=68）的 video 字段是**占位**，不是视频。实测一条图文作品：
        video.width/height = 2160×3838      （这是封面尺寸）
        play_addr.width/height = 720×720    （占位方图）
        duration = 0                        （0 秒）
        bit_rate / play_addr_h264 / play_addr_265 全为 None
        该 play_addr 的两个 URL 请求回来是 **content-type: audio/mp4**（1.8MB 背景音）
    旧实现只判断 url_list 非空就收下，于是把这 1.8MB 音频当成「视频」返回，
    还给它安上 720×720 的假尺寸 —— 前端拿 kind:"video" 去播 audio/mp4，必然失败。
    所以这里把占位/音频档排除掉。
    """
    duration_ms = video.get("duration") or 0
    try:
        duration_ms = int(duration_ms)
    except (TypeError, ValueError):
        duration_ms = 0

    scored: list[dict[str, Any]] = []

    def push(obj: Any, br_val: int = 0, h265: Any = None) -> None:
        if not isinstance(obj, dict):
            return
        w = obj.get("width") or 0
        h = obj.get("height") or 0
        for u in obj.get("url_list") or []:
            if not u or "playwm" in u:
                continue
            if _dy_is_audio_stream(u):
                # 明确的音频资源（图文作品的背景音）不是视频候选
                continue
            # duration=0 且宽高是正方形：图文作品的占位 video，不是真视频。
            # 只在「零时长」这一前提下才用方图判定，避免误伤真实的 1:1 视频。
            if duration_ms <= 0 and w and w == h:
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

"""素材抓取服务：粘贴小红书/抖音链接 → 解析高清原图/视频 → 手机端保存。

所有外链资源都经本服务代理，保证在 https 页面下不会出现混合内容拦截。
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import re
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from starlette.background import BackgroundTask
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from parsers import parse as parse_any
from parsers import browser_login, cookie_guard
from parsers.base import UA_IOS, referer_for
from parsers.base import DOWNLOAD_DIR, get_douyin_cookie, get_xhs_cookie
from parsers.base import MEDIA_HOSTS, reload_config, save_config, set_override

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(ROOT, "static")
PORT = int(os.environ.get("PORT", "8765"))

# 只允许代理这些域名的资源，避免服务被当成任意代理（SSRF）。
# 清单的唯一定义在 parsers/base.py 的 MEDIA_HOSTS —— 以前这里是第二份副本，
# 两份各写各的迟早会漂移（改了一处忘了另一处）。
ALLOWED_SUFFIXES = MEDIA_HOSTS

@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动后台 Cookie 保活任务（定时校验登录态 + 接住滚动的续期 Cookie）。"""
    task = asyncio.create_task(cookie_guard.keepalive_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        # 关掉共享出网客户端，避免连接池泄漏到进程退出
        await close_http_client()


# ── 共享出网客户端 ────────────────────────────────────────────────────
# 为什么不再每请求新建：原来 /api/img、/api/video、/api/download 各自
# `async with httpx.AsyncClient(...)`，等于每个图片/视频请求都要重做一次
# TCP + TLS 握手（到 CDN 通常 100~300ms）。一次图文解析要拉 N 张图，这部分
# 开销被放大了 N 倍。共享一个客户端即可复用连接（HTTP keep-alive）。
#
# 用懒加载而不是在 import 时建：模块级建 client 会绑定到当时的 event loop，
# 在 uvicorn reload / 测试环境下容易出「attached to a different loop」。
_http_client: httpx.AsyncClient | None = None
# 视频流单独一个客户端：它必须 timeout=None（大文件按 Range 分段读，整体时长不可预估），
# 而图片/落盘下载需要正常的读超时——两种诉求塞进一个客户端会互相拖累。
_stream_client: httpx.AsyncClient | None = None
_VERIFY_TLS = os.environ.get("GRABBER_VERIFY_TLS", "0") == "1"
# 记录客户端所属的 event loop。共享客户端一旦绑定到某个 loop 就**不能跨 loop 复用**
# （httpx 底层连接池挂在该 loop 上，跨 loop 用会报 "attached to a different loop"）。
# uvicorn --reload、以及任何「一个进程里跑多次 asyncio.run」的场景都会踩到，
# 所以这里主动检测并在 loop 变化时重建，而不是等它在请求里炸。
_client_loop: object | None = None
_stream_loop: object | None = None


def _current_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def get_http_client() -> httpx.AsyncClient:
    global _http_client, _client_loop
    loop = _current_loop()
    if _http_client is None or _http_client.is_closed or (loop is not None and _client_loop is not loop):
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10),
            follow_redirects=True,
            verify=_VERIFY_TLS,   # 部分 CDN 证书链不完整，默认放宽；设 GRABBER_VERIFY_TLS=1 可恢复校验
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
        _client_loop = loop
    return _http_client


def get_stream_client() -> httpx.AsyncClient:
    global _stream_client, _stream_loop
    loop = _current_loop()
    if _stream_client is None or _stream_client.is_closed or (loop is not None and _stream_loop is not loop):
        _stream_client = httpx.AsyncClient(
            timeout=None,
            follow_redirects=True,
            verify=_VERIFY_TLS,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )
        _stream_loop = loop
    return _stream_client


async def close_http_client() -> None:
    global _http_client, _stream_client, _client_loop, _stream_loop
    for c in (_http_client, _stream_client):
        if c is not None and not c.is_closed:
            try:
                await c.aclose()
            except Exception:
                pass
    _http_client = None
    _stream_client = None
    _client_loop = None
    _stream_loop = None


# 图片代理的响应体上限。图片代理要把内容读进内存再返回（见下方说明），
# 所以必须有上限：白名单里的图床「合法」响应也可能几十上百 MB，
# 并发几张就能把 NAS 上的容器打爆。
MAX_IMAGE_BYTES = int(os.environ.get("GRABBER_MAX_IMAGE_BYTES", str(30 * 1024 * 1024)))


app = FastAPI(title="素材抓取", version="0.1.0", lifespan=lifespan)

# 这里**刻意不加 CORSMiddleware**。原来是 allow_origins=["*"]：
#   · 前端页面由本服务自己提供，所有请求都是相对路径的同源请求，本来不需要 CORS；
#   · 而它和「挂公网时加 Cloudflare Access」是冲突的 —— 一旦有了 CF_Authorization
#     Cookie，allow_origins=["*"] 会让任意网站带着该凭据读取 /api/config（该接口对
#     同源浏览器请求会回传 Cookie 明文），等于绕过后面的 SSO。
# 若你确实要把前端放到别的源上，请在这里加**明确的来源白名单**，不要用 "*"。


def guard(url: str) -> str:
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("非法的资源地址")
    host = (p.hostname or "").lower()
    # 必须精确等于白名单后缀，或以「.<后缀>」结尾（即该域的子域），
    # 否则 evilxhscdn.com / xdouyin.com 这类「伪装成官方域名」的地址会过检。
    if not any(host == s or host.endswith("." + s) for s in ALLOWED_SUFFIXES):
        raise ValueError("该域名不在允许范围内")
    return url


def guess_type(url: str, fallback: str) -> str:
    ctype, _ = mimetypes.guess_type(url.split("?")[0])
    return ctype or fallback


@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/config")
async def get_config(request: Request):
    """返回 Cookie 状态。

    默认只回传掩码（set/len/preview）。但对**同源浏览器请求**额外回传明文，
    供前端把「当前已保存的值」预填进两个输入框——否则用户只改其中一个再保存时，
    另一个空框会把对应平台的 Cookie 一并清空（这正是老的「更新一个、另一个变空」的根因）。
    用 Sec-Fetch-Site 做轻量门槛：脚本 / 第三方直接 GET 不带这个头，拿不到明文，
    避免会话 Cookie 被随意拉走。真要挡住在公网的裸露，仍需 Cloudflare Access 之类的前置鉴权。
    """
    from parsers.base import XHS_COOKIE, DOUYIN_COOKIE

    def mask(c: str) -> dict:
        c = (c or "").strip()
        return {"set": bool(c), "len": len(c), "preview": (c[:6] + "…") if c else ""}

    out = {
        "xhs": mask(XHS_COOKIE),
        "douyin": mask(DOUYIN_COOKIE),
        "download_dir": DOWNLOAD_DIR,
    }
    sfs = (request.headers.get("sec-fetch-site") or "").lower()
    if sfs in ("same-origin", "same-site", "none"):
        out["xhs_cookie"] = (XHS_COOKIE or "").strip()
        out["douyin_cookie"] = (DOUYIN_COOKIE or "").strip()
    return out


@app.get("/api/cookie/status")
async def cookie_status():
    """Cookie 健康状态：是否仍被平台认、上次校验与上次自动续期的时间。

    只回传布尔与时间戳，绝不回传 Cookie 内容。
    """
    return cookie_guard.status()


@app.get("/api/diagnostics")
async def diagnostics():
    """出网诊断：各通道可用性、按主机节流参数、最近的降级记录。

    存在的理由：解析失败时（例如抖音一直 403），原来只能看到一个最终状态码，
    完全不知道中间降级到了哪一步、ttwid 有没有拿到。这里把那些信息暴露出来，
    不用再去抓包或翻容器日志。不含任何 Cookie / 请求头内容。
    """
    from parsers.base import fetch_diagnostics, impersonate_diagnostics
    from parsers.web_api import ttwid_last_error

    out = fetch_diagnostics()
    out["ttwid_last_error"] = ttwid_last_error()
    out["download_dir"] = DOWNLOAD_DIR
    # 当前实际选中的请求指纹（curl_cffi 改名时这里能直接看出来）
    out["impersonate"] = impersonate_diagnostics()
    return out


@app.get("/api/debug/douyin")
async def debug_douyin(aweme_id: str = "", link: str = ""):
    """抓一次抖音 detail 接口，返回**原始响应摘要**，用于排障。

    为什么需要它：/api/parse 失败时会统一返回一句人话提示，但「图文能解析、视频
    不能」这类情况，只看那句提示分不清是风控、Cookie、还是解析分支的问题。
    这里把真正拿到的 HTTP 状态、`status_code`、字段结构摊开。
    **不返回 Cookie 内容**，只返回长度。
    """
    import json

    from parsers.base import fetch
    from parsers.douyin import _resolve as _resolve_short
    from parsers.web_api import UA, _cookie_header, _get_ttwid, sign_detail

    if link and not aweme_id:
        try:
            _, aweme_id = await _resolve_short(link)
        except Exception as e:
            return {"error": f"短链解析失败: {type(e).__name__}: {e}"}
    if not aweme_id:
        return {"error": "需要给 aweme_id 或 link"}

    url = sign_detail(aweme_id)
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.douyin.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://www.douyin.com",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "Cookie": _cookie_header(_get_ttwid()),
    }
    out: dict[str, Any] = {
        "aweme_id": aweme_id,
        "cookie_len": len(get_douyin_cookie()),
        "ttwid_set": bool(_get_ttwid()),
    }
    # 只列 Cookie 的**键名**（绝不返回值）：判断登录态是否真的在里面
    # （Uifid / msToken / s_v_web_id 这几个是 Argus 风控要看的）。
    try:
        keys = sorted(
            k.strip().split("=", 1)[0]
            for k in get_douyin_cookie().split(";")
            if k.strip() and "=" in k
        )
        out["cookie_keys"] = keys
        for probe in ("Uifid", "ttwid", "msToken", "s_v_web_id", "sessionid", "sso_uid", "uid_tt"):
            out[f"has_{probe}"] = any(k.lower() == probe.lower() for k in keys)
    except Exception as e:
        out["cookie_keys_error"] = str(e)
    try:
        r = await fetch(url, headers=headers, impersonate="chrome")
    except Exception as e:
        out["error"] = f"请求异常: {type(e).__name__}: {e}"
        return out
    out["http_status"] = r.status_code
    out["via"] = r.via
    out["body_len"] = len(r.text or "")
    out["degrades"] = r.degrades
    text = (r.text or "").strip()
    if not text.startswith("{"):
        out["body_head"] = text[:300]
        return out
    try:
        payload = json.loads(text)
    except Exception as e:
        out["error"] = f"JSON 解析失败: {e}"
        out["body_head"] = text[:300]
        return out
    out["status_code"] = payload.get("status_code")
    out["top_keys"] = list(payload.keys())[:15]
    detail = payload.get("aweme_detail") or (payload.get("data") or {}).get("aweme_detail")
    if not detail:
        out["no_aweme_detail"] = True
        out["payload_head"] = json.dumps(payload, ensure_ascii=False)[:600]
        return out
    out["aweme_type"] = detail.get("aweme_type")
    out["desc"] = (detail.get("desc") or "")[:60]
    video = detail.get("video") or {}
    out["duration"] = video.get("duration")
    out["has_images"] = bool(detail.get("images"))
    out["video_url_list_len"] = len((video.get("play_addr") or {}).get("url_list") or [])
    out["bit_rate_len"] = len(video.get("bit_rate") or [])
    return out


@app.post("/api/cookie/check")
async def cookie_check():
    """立刻带 Cookie 探测一次（顺便接住平台滚动下发的新 Cookie）。"""
    await cookie_guard.check_all()
    return cookie_guard.status()


# ── 扫码登录（可选能力，需安装 Playwright）────────────────────────
# 在服务所在机器上跑一个真实浏览器完成登录，Cookie 直接落在本机，
# 全程不经过你的电脑。未安装 Playwright 时接口返回可用性提示。
@app.post("/api/login/start")
async def login_start():
    return await asyncio.to_thread(browser_login.start)


@app.get("/api/login/status")
async def login_status():
    return browser_login.status()


@app.post("/api/login/cancel")
async def login_cancel():
    return await asyncio.to_thread(browser_login.cancel)


@app.post("/api/login/reset")
async def login_reset():
    browser_login.reset()
    return {"ok": True}


@app.post("/api/config")
async def post_config(request: Request):
    """保存 Cookie / 代理等到 config.json，并即时热加载（无需重启服务）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求格式错误"}, status_code=400)
    patch = {}
    # Cookie 字段：只在「确实填了内容」时更新。空串视为「不修改该项」——
    # 这样即使前端某个框没预填成功而留空，也不会把另一个平台的 Cookie 清掉。
    for k in ("xhs_cookie", "douyin_cookie"):
        if k in body and body[k] is not None and str(body[k]).strip():
            patch[k] = str(body[k]).strip()
    # 刻意不再接受 proxy / timeout / download_dir：
    #   · download_dir 是服务端**任意路径落盘**的目标，由请求体决定等于把落盘位置
    #     交给任何能访问本服务的人（前端从不发送它，见 static/index.html 只发
    #     xhs_cookie / douyin_cookie / clear）；
    #   · proxy 能让服务去连任意地址，同样削弱 SSRF 防护；
    #   · timeout 无实际前端用途。
    # 这三项请改用环境变量 / config.json 直接配置：
    #   GRABBER_DOWNLOAD_DIR（容器场景优先）、config.json 的 download_dir / proxy / timeout。
    # 显式清空：前端「清空」按钮传 clear=["xhs","douyin"]
    clr = body.get("clear") or []
    if isinstance(clr, str):
        clr = [clr]
    for p in clr:
        if p in ("xhs", "xhs_cookie"):
            patch["xhs_cookie"] = ""
        elif p in ("douyin", "douyin_cookie"):
            patch["douyin_cookie"] = ""
    if not patch:
        return JSONResponse({"error": "没有可保存的字段"}, status_code=400)
    try:
        save_config(patch)
    except Exception as e:
        return JSONResponse({"error": f"保存失败：{e}"}, status_code=500)
    # 只有保存小红书 Cookie 时才复核登录态：cookie_guard 唯一「无需签名就能判定登录」
    # 的探针是小红书（读 explore 页的 loggedIn），抖音没有对等手段。
    # 旧逻辑只要碰到任一 Cookie 字段就跑一遍 XHS 探测，于是「只存抖音 Cookie」时也会
    # 拿小红书的状态来回话（例如报「未填小红书 Cookie」），把保存结果说拧了。
    # 未填小红书票据时 check_xhs 直接返回、不发网络请求，所以不会拖慢保存。
    checked = False
    if "xhs_cookie" in patch:
        try:
            await cookie_guard.check_all()
            checked = True
        except Exception:
            pass
    return {
        "ok": True,
        "saved": list(patch.keys()),
        "cookie_status": cookie_guard.status() if checked else None,
    }


@app.post("/api/parse")
async def api_parse(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求格式错误"}, status_code=400)
    text = (body or {}).get("url", "").strip()
    if not text:
        return JSONResponse({"error": "请先粘贴链接"}, status_code=400)
    # 允许本次请求临时携带 Cookie（前端「设置」里填的可走这里，省去重启）
    set_override(
        xhs=body.get("xhs_cookie"),
        douyin=body.get("douyin_cookie"),
    )
    try:
        data = await parse_any(text)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except Exception as e:
        return JSONResponse({"error": f"解析失败：{type(e).__name__}: {e}"}, status_code=500)
    return data


@app.get("/api/img")
async def api_img(u: str):
    """图片代理：绕防盗链 + 避免 https 页面加载 http 图片被拦。

    这里**流式**读并设体积上限：旧实现用 `r.content` 全量缓冲，图床返回超大响应时
    会直接把内存吃掉（并发几张就能打爆容器）。边读边攒、超限即断，副作用是
    仍然要拼出完整 bytes 才返回（需要在拿到真实大小后写 Content-Length），
    但至少不会为一个恶意/异常的大响应无限申请内存。
    """
    try:
        url = guard(u)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    headers = {
        "User-Agent": UA_IOS,
        "Referer": referer_for(url),
        # 不要请求 avif/webp：会让 CDN 把原 JPEG 转码，iOS 渲染时常偏色。
        # 这里只接受源站本身已有的格式（jpeg/png/webp 原样），不做格式协商。
        "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8",
    }
    client = get_http_client()
    try:
        req = client.build_request("GET", url, headers=headers)
        r = await client.send(req, stream=True)
    except Exception as e:
        return JSONResponse({"error": f"取图失败：{e}"}, status_code=502)
    try:
        if r.status_code >= 400:
            return JSONResponse({"error": f"图源返回 {r.status_code}"}, status_code=502)
        declared = r.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
            return JSONResponse(
                {"error": f"图片过大（{int(declared) // 1024 // 1024}MB，"
                          f"上限 {MAX_IMAGE_BYTES // 1024 // 1024}MB）"},
                status_code=502,
            )
        buf = bytearray()
        async for chunk in r.aiter_bytes(65536):
            buf.extend(chunk)
            if len(buf) > MAX_IMAGE_BYTES:
                return JSONResponse(
                    {"error": f"图片超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB 上限，已中止"},
                    status_code=502,
                )
    except Exception as e:
        return JSONResponse({"error": f"取图失败：{e}"}, status_code=502)
    finally:
        await r.aclose()
    ctype = r.headers.get("content-type") or guess_type(url, "image/jpeg")
    return Response(
        content=bytes(buf),
        media_type=ctype.split(";")[0],
        headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Length": str(len(buf)),
        },
    )


@app.get("/api/video")
async def api_video(u: str, request: Request, name: str = "media.mp4", dl: int = 0):
    """视频代理：支持 Range（iOS Safari 播放必需），dl=1 时触发下载到『文件』App。"""
    try:
        url = guard(u)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    headers = {
        "User-Agent": UA_IOS,
        "Referer": referer_for(url),
        "Accept": "*/*",
    }
    rng = request.headers.get("range")
    if rng:
        headers["Range"] = rng

    client = get_stream_client()
    try:
        req = client.build_request("GET", url, headers=headers)
        r = await client.send(req, stream=True)
    except Exception as e:
        return JSONResponse({"error": f"取流失败：{e}"}, status_code=502)

    # 上游 CDN 直接拒绝（多为地址过期/防盗链）时给出明确提示，别把坏流丢给客户端
    if r.status_code >= 400:
        await r.aclose()
        return JSONResponse(
            {"error": f"视频源返回 {r.status_code}，链接可能已过期，请重新解析一次"},
            status_code=502,
        )

    out_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=86400",
    }
    for k in ("Content-Range", "Content-Length", "ETag", "Last-Modified"):
        if k in r.headers:
            out_headers[k] = r.headers[k]
    if dl:
        # 文件名必须先清理 + 百分号编码：HTTP 头只能 latin-1，直接塞中文会
        # 让 Starlette 抛 UnicodeEncodeError，表现为 dl=1 直接 500（视频"无法下载"）。
        clean = name.replace("\r", "").replace("\n", "").replace('"', "'").strip() or "media.mp4"
        out_headers["Content-Disposition"] = (
            "attachment; filename=\"media.mp4\"; "
            f"filename*=UTF-8''{quote(clean)}"
        )

    return StreamingResponse(
        r.aiter_bytes(chunk_size=65536),
        status_code=r.status_code,
        headers=out_headers,
        media_type=r.headers.get("content-type", "video/mp4").split(";")[0],
        # 只释放本次响应流，**不要**关掉共享客户端（关了后续请求就没得用了；
        # 连接池由 lifespan 的 close_http_client() 在服务退出时统一关）。
        background=BackgroundTask(r.aclose),
    )


# 已知媒体扩展名白名单：只认这些，避免把裸域名的 .com / .cn 误当成扩展名
# （例如某地址没有路径段时，旧逻辑会落出 xxx.com 这种文件名）。
_KNOWN_EXT = {
    "jpg", "jpeg", "png", "webp", "gif", "bmp", "heic", "tiff",
    "mp4", "mov", "avi", "m4v", "mkv", "webm",
}


def _ext_for(url: str, kind: str) -> str:
    """按 URL 尾部的已知媒体扩展名推断落盘扩展名；否则按资源类型给默认档。"""
    path = url.split("?")[0].split("#")[0]
    m = re.search(r"\.([a-zA-Z0-9]{2,5})$", path)
    if m and m.group(1).lower() in _KNOWN_EXT:
        e = m.group(1).lower()
        if kind == "video" and e in ("jpg", "jpeg", "png", "webp", "gif", "bmp", "heic", "tiff"):
            return ".mp4"
        return "." + e
    return ".mp4" if kind == "video" else ".jpg"


def _unique_dest(target_dir: str, fname: str) -> tuple[str, str]:
    """返回一个**不会覆盖已有文件**的目标路径。

    为什么需要：文件名是「标题前缀 + 递增序号」，而前端默认把前缀填成作品标题，
    所以同一篇作品再下一次会得到完全相同的文件名，`open(dest,"wb")` 直接覆盖，
    之前存的那几张就没了——对「NAS 素材库」是真实的数据丢失，而且没有任何提示。
    这里在重名时插入 a/b/c…（第 27 个之后退化为 27、28…，避免字母耗尽）。
    """
    if not os.path.exists(os.path.join(target_dir, fname)):
        return fname, os.path.join(target_dir, fname)
    stem, ext = os.path.splitext(fname)
    for i in range(26):
        cand = f"{stem}{chr(ord('a') + i)}{ext}"
        full = os.path.join(target_dir, cand)
        if not os.path.exists(full):
            return cand, full
    n = 27
    while True:
        cand = f"{stem}{n}{ext}"
        full = os.path.join(target_dir, cand)
        if not os.path.exists(full):
            return cand, full
        n += 1


@app.post("/api/download")
async def api_download(request: Request):
    """把选中的图片/视频直接下载到 NAS 本地目录（服务端落盘，不经过手机）。

    请求体：{ urls:[...], kinds:[...], subdir:"可选标题前缀" }
    - urls/kinds 一一对应；kinds 缺省按 image 处理。
    - 不新建子目录：所有文件直接落进 DOWNLOAD_DIR（扁平存放，方便 ComfyUI 直接批量读）。
    - subdir 复用为「文件名标题前缀」，与序号拼接成「标题+序号.扩展名」；留空则只用序号。
    - **同名不覆盖**：已存在时插入 a/b/c 后缀（见 _unique_dest），避免重下同一作品把旧文件冲掉。
    每个链接都过 guard() 防 SSRF；文件名只取「标题+序号+扩展名」，杜绝目录穿越。
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求格式错误"}, status_code=400)
    urls = body.get("urls")
    if not isinstance(urls, list) or not urls:
        return JSONResponse({"error": "没有可下载的链接"}, status_code=400)
    if len(urls) > 50:
        return JSONResponse({"error": "一次最多下载 50 个"}, status_code=400)
    kinds = body.get("kinds") or []
    # subdir 字段在此复用为「文件名标题前缀」：用户要求不新建子目录，
    # 所有图片/视频直接落进 DOWNLOAD_DIR，文件名 = 标题 + 序号。
    title = (str(body.get("subdir") or "")).strip()
    # 文件名前缀消毒：去掉路径分隔与 Windows 非法字符，避免写出怪异/穿越文件名。
    title = re.sub(r'[\/\\:*?"<>|]', "_", title).strip("._ ").replace("..", "_")[:40]
    target_dir = DOWNLOAD_DIR
    try:
        os.makedirs(target_dir, exist_ok=True)
    except Exception as e:
        return JSONResponse({"error": f"无法创建目录 {target_dir}：{e}"}, status_code=500)

    results: list[dict] = []
    saved = 0
    for i, url in enumerate(urls):
        url = str(url or "").strip()
        kind = (kinds[i] if i < len(kinds) else "image") or "image"
        try:
            guard(url)
        except ValueError as e:
            results.append({"url": url, "ok": False, "error": str(e)})
            continue
        ext = _ext_for(url, kind)
        base_name = f"{title}{i + 1:02d}{ext}" if title else f"{i + 1:02d}{ext}"
        # 重名不覆盖：见 _unique_dest 的说明
        fname, dest = _unique_dest(target_dir, base_name)
        # 按域名选对应平台的 Cookie：小红书 CDN 用 xhs cookie，其余（抖音系）用 douyin cookie，
        # 避免把一方的登录会话 cookie 误发给另一方的 CDN（既无用也可能泄漏）。
        if "xhscdn" in url or "xiaohongshu" in url:
            cookie = get_xhs_cookie()
        else:
            cookie = get_douyin_cookie()
        headers = {
            "User-Agent": UA_IOS,
            "Referer": referer_for(url),
            "Accept": "*/*",
        }
        if cookie:
            headers["Cookie"] = cookie
        try:
            client = get_http_client()
            r = await client.get(url, headers=headers)
            if r.status_code >= 400:
                results.append({"url": url, "ok": False, "error": f"源返回 {r.status_code}"})
                continue
            # 落盘写入丢到线程里：f.write 是阻塞调用，直接在协程里写大文件会
            # 卡住整个事件循环（包括同时进行的 /api/img 图片响应）。
            with open(dest, "wb") as f:
                async for chunk in r.aiter_bytes(65536):
                    await asyncio.to_thread(f.write, chunk)
            saved += 1
            results.append({"url": url, "ok": True, "file": fname})
        except Exception as e:
            results.append({"url": url, "ok": False, "error": f"{type(e).__name__}: {e}"})

    return {
        "ok": True,
        "dir": target_dir,
        "saved": saved,
        "total": len(urls),
        "results": results,
    }


# SPA 兜底：任意非 /api 路径都返回 index.html，使 iOS 快捷指令把链接/整段文案
# 塞进 URL path（而非 ?url=）的跳转也能加载前端并自动解析。带扩展名的真实静态
# 文件仍直接返回，避免覆盖资源。必须在 StaticFiles mount 之前注册才会优先生效。
@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    candidate = os.path.join(STATIC_DIR, full_path)
    if "." in full_path and os.path.isfile(candidate):
        return FileResponse(candidate)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), media_type="text/html")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    print(f"服务已启动： http://0.0.0.0:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")

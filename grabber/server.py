"""素材抓取服务：粘贴小红书/抖音链接 → 解析高清原图/视频 → 手机端保存。

所有外链资源都经本服务代理，保证在 https 页面下不会出现混合内容拦截。
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import re
from contextlib import asynccontextmanager
from urllib.parse import quote, urlparse

import httpx
from starlette.background import BackgroundTask
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from parsers import parse as parse_any
from parsers import browser_login, cookie_guard
from parsers.base import UA_IOS, referer_for
from parsers.base import DOWNLOAD_DIR, get_douyin_cookie, get_xhs_cookie
from parsers.base import reload_config, save_config, set_override

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(ROOT, "static")
PORT = int(os.environ.get("PORT", "8765"))

# 只允许代理这些域名的资源，避免服务被当成任意代理（SSRF）
ALLOWED_SUFFIXES = (
    "xhscdn.com",
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


app = FastAPI(title="素材抓取", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    # 其余字段沿用原语义（允许清空）
    for k in ("proxy", "timeout", "download_dir"):
        if k in body and body[k] is not None:
            patch[k] = str(body[k])
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
    """图片代理：绕防盗链 + 避免 https 页面加载 http 图片被拦。"""
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
    try:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, verify=False
        ) as c:
            r = await c.get(url, headers=headers)
    except Exception as e:
        return JSONResponse({"error": f"取图失败：{e}"}, status_code=502)
    if r.status_code >= 400:
        return JSONResponse({"error": f"图源返回 {r.status_code}"}, status_code=502)
    ctype = r.headers.get("content-type") or guess_type(url, "image/jpeg")
    return Response(
        content=r.content,
        media_type=ctype.split(";")[0],
        headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Length": str(len(r.content)),
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

    client = httpx.AsyncClient(timeout=None, follow_redirects=True, verify=False)
    try:
        req = client.build_request("GET", url, headers=headers)
        r = await client.send(req, stream=True)
    except Exception as e:
        await client.aclose()
        return JSONResponse({"error": f"取流失败：{e}"}, status_code=502)

    # 上游 CDN 直接拒绝（多为地址过期/防盗链）时给出明确提示，别把坏流丢给客户端
    if r.status_code >= 400:
        await r.aclose()
        await client.aclose()
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
        background=BackgroundTask(client.aclose),
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


@app.post("/api/download")
async def api_download(request: Request):
    """把选中的图片/视频直接下载到 NAS 本地目录（服务端落盘，不经过手机）。

    请求体：{ urls:[...], kinds:[...], subdir:"可选标题前缀" }
    - urls/kinds 一一对应；kinds 缺省按 image 处理。
    - 不新建子目录：所有文件直接落进 DOWNLOAD_DIR（扁平存放，方便 ComfyUI 直接批量读）。
    - subdir 复用为「文件名标题前缀」，与序号拼接成「标题+序号.扩展名」；留空则只用序号。
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
        fname = f"{title}{i + 1:02d}{ext}" if title else f"{i + 1:02d}{ext}"
        dest = os.path.join(target_dir, fname)
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
            async with httpx.AsyncClient(
                timeout=120, follow_redirects=True, verify=False
            ) as c:
                r = await c.get(url, headers=headers)
                if r.status_code >= 400:
                    results.append({"url": url, "ok": False, "error": f"源返回 {r.status_code}"})
                    continue
                with open(dest, "wb") as f:
                    async for chunk in r.aiter_bytes(65536):
                        f.write(chunk)
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

"""在服务所在的机器上，用真实浏览器完成扫码登录，把 Cookie 落到本机配置。

为什么需要它
------------
实测（2026-09）：

- 小红书**匿名访问只下发** ``acw_tc`` / ``abRequestId``，**不再下发 ``a1``**；
  逐个试探过首页、explore、selfinfo、login/activate、homefeed，没有一个会下发 ``a1``。
- 所有 ``/api/sns/web/v1/*`` 接口都要 ``x-s`` 签名，无签名一律 ``406`` / ``500``。
  ``login/qrcode/create`` 同样被网关拒掉（``create invoker failed``）。

结论：**纯 HTTP 既拿不到设备指纹，也调不了登录接口。**
所以「在 NAS 上原生获取 Cookie」只有一条路——在本机跑一个真实浏览器，
由它去完成签名、二维码与风控，再把结果 Cookie 交给我们。

设计取舍
--------
浏览器**只在登录这一刻存在**：登录成功 / 超时 / 取消后立即关闭。
日常解析仍然走轻量 HTTP（curl_cffi），不常驻、不占 NAS 内存。
Playwright 未安装时本模块整体降级为「不可用」，网页会提示改用粘贴方式。
"""
from __future__ import annotations

import base64
import threading
import time
from typing import Any

from .base import extract_json_block, save_config

# 注意：**不能靠 cookie 名判断登录成功。**
# 实测（2026-09）：用真实浏览器打开一次登录页，小红书立刻下发
# a1 / webId / websectiga / sec_poison_id / **web_session** 一整套——
# 其中 web_session 只是**访客会话**，拿去探测仍是 loggedIn:false。
# 所以唯一可靠的判据是**平台自己下发的登录标记**，见 _verify_logged_in()。
LOGIN_URL = "https://www.xiaohongshu.com/login"
VERIFY_URL = "https://www.xiaohongshu.com/explore"
# 主动校验间隔（秒）：无凭据变化时也定期确认一次，避免漏掉"扫码后 cookie 没变"的情况
VERIFY_EVERY = 20
LOGIN_TIMEOUT = 300  # 秒
XHS_DOMAINS = ("xiaohongshu.com", "xhscdn.com", "xhslink.com")

# 二维码元素的候选选择器（页面改版时逐个回退，最后兜底整页截图）
QR_SELECTORS = (
    "img.qrcode-img",
    "canvas.qrcode-img",
    "div.qrcode img",
    "div.qrcode canvas",
    '[class*="qrcode"] img',
    '[class*="qrcode"] canvas',
    '[class*="qr-code"] img',
    'img[src^="data:image"]',
)

_lock = threading.Lock()
_stop = threading.Event()
STATE: dict[str, Any] = {
    "status": "idle",   # idle | starting | waiting | ok | timeout | cancelled | error
    "message": "",
    "qr": "",           # data:image/png;base64,...
    "started": 0.0,
    "finished": 0.0,
    "user_hint": "",
    "debug": "",        # 页面地址 / 选择器命中情况，便于远程排查
}


def available() -> tuple[bool, str]:
    """返回 (能否使用, 原因)。浏览器登录是可选能力，缺依赖时只提示、不报错。"""
    try:
        import playwright  # noqa: F401
    except Exception:
        return False, (
            "未安装 Playwright，无法在本机扫码登录。"
            "可用「复制 Cookie 粘贴」方式，或按 README 启用浏览器登录。"
        )
    return True, ""


def _new_context(pw):
    """优先用系统已装的 Chrome（本机开发省一次下载），否则用 Playwright 自带 Chromium。"""
    launch_args = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    try:
        browser = pw.chromium.launch(channel="chrome", **launch_args)
    except Exception:
        browser = pw.chromium.launch(**launch_args)
    return browser, browser.new_context(
        viewport={"width": 480, "height": 720},
        locale="zh-CN",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
    )


def _grab_qr(page, *, allow_fallback: bool = True) -> tuple[str, str]:
    """截二维码元素，返回 (data_url, 命中的选择器)。

    页面改版找不到元素时（allow_fallback）回退整页截图，照样能扫；
    都失败返回空串，由调用方把页面地址 / 标题写进 debug，方便在 NAS 上远程排查。
    """
    for sel in QR_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            box = loc.bounding_box()
            if not box or box.get("width", 0) < 60:
                continue
            png = loc.screenshot()
            return "data:image/png;base64," + base64.b64encode(png).decode(), sel
        except Exception:
            continue
    if not allow_fallback:
        return "", ""
    try:
        png = page.screenshot(full_page=False)
        return "data:image/png;base64," + base64.b64encode(png).decode(), "(整页兜底)"
    except Exception:
        return "", ""


def _collect_cookie(context) -> str:
    """导出小红书域下的 cookie，拼成可直接使用的整行。"""
    out: list[str] = []
    try:
        for ck in context.cookies():
            domain = (ck.get("domain") or "").lstrip(".")
            if any(domain.endswith(d) for d in XHS_DOMAINS):
                name, val = ck.get("name"), ck.get("value")
                if name and val:
                    out.append(f"{name}={val}")
    except Exception:
        return ""
    return "; ".join(out)


_LOGIN_JS = """
() => {
  try {
    const s = window.__INITIAL_STATE__;
    if (!s || !s.user) return null;
    return (typeof s.user.loggedIn === 'boolean') ? s.user.loggedIn : null;
  } catch (e) { return null; }
}
"""

STATE_ANCHOR = "__INITIAL_STATE__"


def _verify_logged_in(context) -> bool:
    """读平台自己下发的登录标记 —— 这是唯一可靠的判据。

    为什么不能看 cookie：访客打开一次登录页就会拿到
    a1 / webId / websectiga / sec_poison_id / **web_session** 一整套，
    带这套 cookie 去请求，平台仍然回答 loggedIn:false。

    两条路径，先轻后重：
      1) 用浏览器上下文的 HTTP 客户端直接拉 explore 页（共享同一份 cookie），
         解析 SSR 里的 __INITIAL_STATE__。轻量、不打断正在展示的二维码。
      2) 万一只拿到 JS 空壳（SSR 偶发不下发），再用真实页面渲染一次兜底。
    """
    try:
        r = context.request.get(VERIFY_URL, timeout=30000)
        if r.ok:
            state = extract_json_block(r.text(), STATE_ANCHOR)
            if isinstance(state, dict):
                user = state.get("user") or {}
                if isinstance(user, dict) and isinstance(user.get("loggedIn"), bool):
                    return user["loggedIn"] is True
    except Exception:
        pass

    page = None
    try:
        page = context.new_page()
        page.goto(VERIFY_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1000)
        return page.evaluate(_LOGIN_JS) is True
    except Exception:
        return False
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass


def _worker() -> None:
    """后台线程：拉浏览器 → 截图二维码 → 轮询登录 → 存 Cookie → 关浏览器。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        _set(status="error", message=f"Playwright 不可用：{e}")
        return

    browser = None
    try:
        with sync_playwright() as pw:
            browser, context = _new_context(pw)
            page = context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            # 等二维码渲染出来
            for _ in range(30):
                if _stop.is_set():
                    break
                qr, sel = _grab_qr(page, allow_fallback=False)
                if qr and sel:
                    _set(
                        status="waiting", qr=qr, message="请用小红书 App 扫码并确认",
                        debug=f"{page.url} | 选择器 {sel}",
                    )
                    break
                page.wait_for_timeout(500)
            else:
                qr, sel = _grab_qr(page)
                detail = f"{page.url} | {page.title()[:40]} | 命中 {sel or '无'}"
                if qr:
                    _set(status="waiting", qr=qr, message="请用小红书 App 扫码并确认",
                         debug=f"整页截图兜底 | {detail}")
                else:
                    _set(status="error", message="没能截到二维码，可能页面改版或被风控拦截",
                         debug=detail)

            # 定期间隔直接问平台「现在这个会话是谁」。
            # 不用「cookie 变了就检查」那种写法：acw_tc / ets / websectiga 之类
            # 每次请求都会换，会一直误触发，反而刷屏。
            deadline = time.time() + LOGIN_TIMEOUT
            next_verify = time.time() + VERIFY_EVERY
            while time.time() < deadline:
                if _stop.is_set():
                    _set(status="cancelled", message="已取消登录")
                    return
                if time.time() >= next_verify:
                    next_verify = time.time() + VERIFY_EVERY
                    if _verify_logged_in(context):
                        cookie = _collect_cookie(context)
                        if cookie:
                            _persist(cookie)
                            return
                page.wait_for_timeout(1500)
            _set(status="timeout", message="二维码已超时，请重新发起登录")
    except Exception as e:
        _set(status="error", message=f"浏览器登录失败：{type(e).__name__}: {e}")
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass


def _persist(cookie: str) -> None:
    """把登录得到的 Cookie 写进配置，并立刻校验一次。"""
    try:
        save_config({"xhs_cookie": cookie})
    except Exception as e:
        _set(status="error", message=f"登录成功但写入配置失败：{e}")
        return
    names = [p.split("=", 1)[0] for p in cookie.split(";") if "=" in p]
    _set(
        status="ok",
        qr="",
        message="登录成功，Cookie 已保存到本机配置。",
        user_hint=f"共 {len(names)} 项：{', '.join(sorted(set(names))[:10])}",
    )
    # 顺手校验（让界面立刻显示登录态是否被平台认）。
    # 本函数跑在普通线程里，没有事件循环，用 asyncio.run 自己起一个。
    try:
        import asyncio

        from . import cookie_guard

        asyncio.run(cookie_guard.check_xhs())
    except Exception:
        pass


def _set(**kw: Any) -> None:
    with _lock:
        STATE.update(kw)
        if kw.get("status") in ("ok", "timeout", "cancelled", "error"):
            STATE["finished"] = time.time()


def start() -> dict[str, Any]:
    """开始一次扫码登录（同一时刻只允许一个）。"""
    ok, why = available()
    if not ok:
        return {"ok": False, "error": why, "status": "unavailable"}
    with _lock:
        if STATE["status"] in ("starting", "waiting"):
            return {"ok": False, "error": "已有一个登录流程在进行中，请先扫码或取消"}

    _stop.clear()
    with _lock:
        STATE.update(
            status="starting", message="正在启动浏览器…", qr="",
            started=time.time(), finished=0.0, user_hint="",
        )
    threading.Thread(target=_worker, name="xhs-login", daemon=True).start()
    return {"ok": True, "status": "starting"}


def cancel() -> dict[str, Any]:
    """取消当前登录流程。"""
    _stop.set()
    with _lock:
        if STATE["status"] in ("starting", "waiting"):
            STATE["status"] = "cancelled"
            STATE["message"] = "已取消登录"
    return status()


def status() -> dict[str, Any]:
    """给界面看的状态。"""
    with _lock:
        snap = dict(STATE)
    ok, why = available()
    snap["available"] = ok
    if not ok:
        snap["unavailable_reason"] = why
    if snap.get("started"):
        snap["elapsed"] = int(time.time() - snap["started"])
    if snap["status"] in ("starting", "waiting"):
        snap["remaining"] = max(0, int(LOGIN_TIMEOUT - (time.time() - snap["started"])))
    return snap


def reset() -> None:
    """把状态复位为 idle（界面关掉后调用）。"""
    _stop.clear()
    with _lock:
        STATE.update(
            status="idle", message="", qr="", started=0.0, finished=0.0,
            user_hint="", debug="",
        )

"""Cookie 保活与登录态探测：让登录态尽量自己活着，真失效时第一时间知道。

为什么需要这个
--------------
小红书 / 抖音的登录票据（``a1`` / ``web_session``）官方给的 Max-Age 很长，
真正让它「失效」的通常不是时间，而是这三件事：

1. **风控 token 过期**：``acw_tc`` 只有 30 分钟，``__ac_nonce`` 同样。
   带着过期 token 反复请求，容易被判定成异常客户端，进而牵连登录态。
   → 每次响应都用 ``base.absorb_cookies()`` 接住平台新下发的值。
2. **会话长期不活动**：长时间零请求的会话更容易被回收。
   → 定时保活，用真实 cookie 打一次轻量页面。
3. **被登出 / 改密 / 异地登录**：这个任何服务端技巧都救不回来，只能重新登录。
   → 探测到就立刻在界面标红，不用等你某天自己发现解析坏了。

探测判据（实测得出，无需 x-s 签名）
-----------------------------------
小红书 ``/explore`` 页面里的 ``__INITIAL_STATE__.user.loggedIn``：
未登录为 ``false`` 且 ``userInfo.userId`` 为 ``undefined``；已登录为 ``true``。
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from .base import (
    UA_DESKTOP,
    absorb_cookies,
    absorb_report,
    extract_json_block,
    fetch,
    get_xhs_cookie,
    has_login_ticket,
)

XHS_PROBE_URL = "https://www.xiaohongshu.com/explore"
DOUYIN_PROBE_URL = "https://www.douyin.com/"

# 保活间隔（小时）。设 0 可关闭，见 COOKIE_KEEPALIVE_HOURS。
DEFAULT_KEEPALIVE_HOURS = 6.0

STATE: dict[str, Any] = {
    "checked_at": 0.0,      # 上次探测时间戳
    "ok": None,            # True 有效 / False 失效 / None 未知
    "logged_in": None,
    "user_id": "",
    "message": "尚未校验",
    "keepalive_hours": DEFAULT_KEEPALIVE_HOURS,
    "runs": 0,
    "xhs_has_cookie": False,
    "douyin_has_cookie": False,
}


def _mark(
    *,
    ok: bool | None,
    message: str,
    logged_in: bool | None = None,
    user_id: str = "",
) -> dict[str, Any]:
    STATE.update(
        checked_at=time.time(),
        ok=ok,
        message=message,
        logged_in=logged_in,
        user_id=user_id,
    )
    return dict(STATE)


async def check_xhs(*, persist: bool = True) -> dict[str, Any]:
    """带 Cookie 访问一次小红书，验证登录态并顺带接住滚动下发的 Cookie。"""
    # 判断「有没有登录态」要看用户填的票据，不能看合并后的串：
    # 匿名访问也会下发 acw_tc / abRequestId，光看串非空会误判成已登录。
    if not has_login_ticket("xhs"):
        return _mark(
            ok=None,
            message="未填小红书 Cookie（图片免登录可用；视频受网页端上限约束，最高 1080p）",
        )

    cookie = get_xhs_cookie()
    headers = {
        "User-Agent": UA_DESKTOP,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Cookie": cookie,
    }
    try:
        resp = await fetch(XHS_PROBE_URL, headers=headers, impersonate="chrome")
    except Exception as e:
        return _mark(ok=None, message=f"探测请求失败：{type(e).__name__}: {e}")

    refreshed = absorb_cookies(resp.url or XHS_PROBE_URL, resp.cookies, persist=persist)
    tail = f"，已自动续期 {len(refreshed)} 项" if refreshed else ""

    if resp.status_code >= 400:
        # 实测：带上一个无效的登录态，小红书不是返回干净的「未登录」页面，
        # 而是直接 500 —— 无效会话会让它在服务端报错。所以 5xx 基本等同于
        # 「这个 Cookie 已经不被接受了」，但仍留一句「也可能只是平台抖动」。
        if resp.status_code >= 500:
            msg = (
                f"小红书返回 {resp.status_code}，当前 Cookie 很可能已失效"
                "（无效登录态会让它在服务端报错），也可能只是平台抖动。"
                "建议重新获取 Cookie 后填入。"
            )
        else:
            msg = f"小红书返回 {resp.status_code}，登录态已不被接受，请重新获取 Cookie。"
        return _mark(ok=False, logged_in=False, message=msg + tail)

    state = extract_json_block(resp.text, "__INITIAL_STATE__")
    if not isinstance(state, dict):
        # 页面结构变了 / 拿到的是 JS 空壳，无法判断，别误报失效
        return _mark(
            ok=None,
            message=f"页面未返回可判读的状态，暂时无法确认登录态{tail}",
        )

    user = state.get("user") or {}
    logged = bool(user.get("loggedIn")) if isinstance(user, dict) else False
    uid = ((user.get("userInfo") or {}).get("userId")) or ""
    if logged:
        return _mark(
            ok=True,
            logged_in=True,
            user_id=str(uid),
            message=f"登录态有效{tail}",
        )
    return _mark(
        ok=False,
        logged_in=False,
        message=(
            "这个 Cookie 不是已登录状态（页面返回的 loggedIn=false，只拿到了访客会话）。"
            "请确认复制 Cookie 时浏览器里已经登录小红书，或改用「手机扫码登录」。" + tail
        ),
    )


async def check_all(*, persist: bool = True) -> dict[str, Any]:
    """校验全部平台（目前只有小红书能无签名判定登录态）。"""
    result = await check_xhs(persist=persist)
    STATE["xhs_has_cookie"] = has_login_ticket("xhs")
    STATE["douyin_has_cookie"] = has_login_ticket("douyin")
    return result


def status() -> dict[str, Any]:
    """给界面看的状态：只暴露布尔与时间，绝不回传 Cookie 内容。"""
    now = time.time()

    def ago(ts: float) -> str:
        if not ts:
            return ""
        d = int(now - ts)
        if d < 60:
            return "刚刚"
        if d < 3600:
            return f"{d // 60} 分钟前"
        if d < 86400:
            return f"{d // 3600} 小时前"
        return f"{d // 86400} 天前"

    rep = absorb_report()
    return {
        "ok": STATE["ok"],
        "logged_in": STATE["logged_in"],
        "user_id": STATE["user_id"],
        "message": STATE["message"],
        "checked_at": STATE["checked_at"],
        "checked_ago": ago(STATE["checked_at"]),
        "xhs_has_cookie": has_login_ticket("xhs"),
        "douyin_has_cookie": has_login_ticket("douyin"),
        "keepalive_hours": STATE["keepalive_hours"],
        "keepalive_runs": STATE["runs"],
        "renew": {
            "times": rep["count"],
            "keys": rep["last_keys"],
            "last_at": rep["last_at"],
            "last_ago": ago(rep["last_at"]),
        },
    }


def _keepalive_hours() -> float:
    raw = os.environ.get("COOKIE_KEEPALIVE_HOURS")
    if raw is None or raw == "":
        return DEFAULT_KEEPALIVE_HOURS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_KEEPALIVE_HOURS


async def keepalive_loop() -> None:
    """后台保活：定时校验 + 续期。任务被取消即退出。"""
    hours = _keepalive_hours()
    STATE["keepalive_hours"] = hours
    if hours <= 0:
        STATE["message"] = "保活已关闭（COOKIE_KEEPALIVE_HOURS=0）"
        return

    # 启动后先等一下，别和首次解析抢资源
    await asyncio.sleep(15)
    while True:
        try:
            if has_login_ticket("xhs"):
                STATE["runs"] += 1
                await check_xhs()
            else:
                STATE["xhs_has_cookie"] = False
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(hours * 3600)

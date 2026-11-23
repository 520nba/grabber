# -*- coding: utf-8 -*-
"""抖音登录态判据（cookie_guard.check_douyin / status 字段）的离线测试。

全部用假的 fetch 注入，不发真实网络请求。覆盖：
  · 没填 Cookie / 只有风控 token（缺登录票据）→ 不探测、状态为「未知」
  · status_code=0 + user.uid → 判定已登录，并能带出昵称/ID
  · status_code=8「用户未登录」→ 判定已失效
  · 200 但返回 HTML（验证墙）→ 判为「暂时无法确认」，不误报失效
  · 网络异常 / 5xx → 分别落到「未知」/「失效」
  · 两个平台互不串味：单独探测抖音不会改掉小红书的结论
  · check_all 同时探测两个平台，status() 输出两组字段

运行：
    python tests/test_douyin_cookie_status.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "grabber"))

# 先把配置指到临时文件，再 import parsers（base 在 import 时读 config）
_TMP = Path(tempfile.mkdtemp(prefix="dy-status-test-"))
_CFG = _TMP / "config.json"
_CFG.write_text(json.dumps({
    "xhs_cookie": "a1=1; web_session=x",
    "douyin_cookie": "sessionid=1; sid_tt=1; ttwid=1",
}), encoding="utf-8")
import os  # noqa: E402

os.environ["GRABBER_CONFIG"] = str(_CFG)

from parsers import base, cookie_guard  # noqa: E402

_ok = _bad = 0


def check(label, cond, extra=""):
    global _ok, _bad
    if cond:
        _ok += 1
        print(f"  PASS  {label}")
    else:
        _bad += 1
        print(f"  FAIL  {label}  {extra}")


class Resp:
    def __init__(self, *, status_code=200, text="", url="https://www.douyin.com/x", cookies=None):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.cookies = cookies or {}


def set_cfg(xhs: str, douyin: str) -> None:
    _CFG.write_text(json.dumps({"xhs_cookie": xhs, "douyin_cookie": douyin}), encoding="utf-8")
    base.reload_config()


def install_fetch(fn):
    cookie_guard.fetch = fn  # type: ignore[assignment]


FIXED_TS = 1763900000.0


def reset_state():
    """把状态与时间清干净，避免用例之间互相影响。"""
    cookie_guard.STATE.update(
        checked_at=0.0, ok=None, logged_in=None, user_id="", message="尚未校验",
        douyin_checked_at=0.0, douyin_ok=None, douyin_logged_in=None,
        douyin_user_id="", douyin_nickname="", douyin_message="尚未校验",
        keepalive_hours=6.0, runs=0, xhs_has_cookie=False, douyin_has_cookie=False,
    )


async def main() -> int:
    print("== 1. 没填抖音 Cookie：不发请求，状态=未知 ==")
    set_cfg("a1=1; web_session=x", "")
    calls = []

    async def spy(url, **kw):
        calls.append(url)
        return Resp(text="{}")

    install_fetch(spy)
    reset_state()
    await cookie_guard.check_douyin(persist=False)
    st = cookie_guard.status()
    check("没 Cookie 时不发探测请求", calls == [], calls)
    check("douyin_ok=None，message 提示未填", st["douyin_ok"] is None and "未填" in st["douyin_message"], st["douyin_message"])
    check("douyin_has_cookie=False", st["douyin_has_cookie"] is False)

    print("== 2. 有 Cookie 但没有登录票据：不发请求 ==")
    set_cfg("a1=1; web_session=x", "ttwid=1; __ac_nonce=2")
    calls.clear()
    reset_state()
    await cookie_guard.check_douyin(persist=False)
    st = cookie_guard.status()
    check("缺登录票据时不发探测请求", calls == [], calls)
    check("douyin_ok=None，提示缺票据", st["douyin_ok"] is None and "登录票据" in st["douyin_message"], st["douyin_message"])
    check("douyin_has_cookie=False（票据判定）", st["douyin_has_cookie"] is False)

    print("== 3. 已登录：status_code=0 + user ==")
    set_cfg("a1=1; web_session=x", "sessionid=1; sid_tt=1; ttwid=1")

    async def ok_fetch(url, **kw):
        return Resp(text=json.dumps({
            "status_code": 0,
            "user": {"uid": "84232489142", "nickname": "Derek", "sec_uid": "MS4wLjABAAAA"},
        }))

    install_fetch(ok_fetch)
    reset_state()
    await cookie_guard.check_douyin(persist=False)
    st = cookie_guard.status()
    check("douyin_ok=True / logged_in=True", st["douyin_ok"] is True and st["douyin_logged_in"] is True, st)
    check("带出 uid", st["douyin_user_id"] == "84232489142", st["douyin_user_id"])
    check("带出昵称", st["douyin_nickname"] == "Derek", st["douyin_nickname"])
    check("douyin_checked_ago 有值", bool(st["douyin_checked_ago"]), st["douyin_checked_ago"])
    check("douyin_message 含「有效」", "有效" in st["douyin_message"], st["douyin_message"])

    print("== 4. 失效：status_code=8「用户未登录」 ==")

    async def no_login_fetch(url, **kw):
        return Resp(text=json.dumps({
            "status_code": 8, "status_msg": "用户未登录", "user": None,
        }))

    install_fetch(no_login_fetch)
    reset_state()
    await cookie_guard.check_douyin(persist=False)
    st = cookie_guard.status()
    check("douyin_ok=False / logged_in=False", st["douyin_ok"] is False and st["douyin_logged_in"] is False, st)
    check("message 提到「用户未登录」", "用户未登录" in st["douyin_message"], st["douyin_message"])

    print("== 5. 200 但返回 HTML（验证墙）：判未知，不误报失效 ==")

    async def html_fetch(url, **kw):
        return Resp(text="<html><body>verify</body></html>")

    install_fetch(html_fetch)
    reset_state()
    await cookie_guard.check_douyin(persist=False)
    st = cookie_guard.status()
    check("douyin_ok=None（无法确认）", st["douyin_ok"] is None, st["douyin_ok"])
    check("message 提到「无法确认」", "无法确认" in st["douyin_message"], st["douyin_message"])

    print("== 6. 网络异常 / 5xx ==")

    async def boom(url, **kw):
        raise RuntimeError("connection reset")

    install_fetch(boom)
    reset_state()
    await cookie_guard.check_douyin(persist=False)
    st = cookie_guard.status()
    check("网络异常 → 未知", st["douyin_ok"] is None, st["douyin_ok"])
    check("异常信息进 message", "connection reset" in st["douyin_message"], st["douyin_message"])

    async def five(url, **kw):
        return Resp(status_code=500, text="oops")

    install_fetch(five)
    reset_state()
    await cookie_guard.check_douyin(persist=False)
    st = cookie_guard.status()
    check("5xx → 失效", st["douyin_ok"] is False, st["douyin_ok"])

    print("== 7. 两平台状态互不串味 ==")

    async def route(url, **kw):
        if "xiaohongshu.com" in url:
            return Resp(text='<script>window.__INITIAL_STATE__={"user":{"loggedIn":true,"userInfo":{"userId":"u1"}}}</script>')
        return Resp(text=json.dumps({"status_code": 0, "user": {"uid": "u2", "nickname": "nick2"}}))

    install_fetch(route)
    reset_state()
    await cookie_guard.check_xhs(persist=False)
    xhs_before = cookie_guard.status()
    check("先只探测小红书：xhs=有效，抖音仍未知",
          xhs_before["ok"] is True and xhs_before["douyin_ok"] is None,
          {"xhs": xhs_before["ok"], "dy": xhs_before["douyin_ok"]})
    await cookie_guard.check_douyin(persist=False)
    st = cookie_guard.status()
    check("再探测抖音：两边都对且都保留",
          st["ok"] is True and st["douyin_ok"] is True and st["user_id"] == "u1" and st["douyin_user_id"] == "u2",
          {"xhs": st["ok"], "dy": st["douyin_ok"], "xuid": st["user_id"], "dyid": st["douyin_user_id"]})
    check("两套 checked_ago 各自独立", bool(st["checked_ago"]) and bool(st["douyin_checked_ago"]), st)

    print("== 8. check_all 同时探测两平台 + status 字段齐全 ==")
    reset_state()
    await cookie_guard.check_all(persist=False)
    st = cookie_guard.status()
    check("check_all 后 xhs=有效", st["ok"] is True, st["ok"])
    check("check_all 后 douyin=有效", st["douyin_ok"] is True, st["douyin_ok"])
    check("has_cookie 两个都为 True", st["xhs_has_cookie"] and st["douyin_has_cookie"], st)
    need = ("douyin_ok", "douyin_logged_in", "douyin_user_id", "douyin_nickname",
            "douyin_message", "douyin_checked_at", "douyin_checked_ago")
    missing = [k for k in need if k not in st]
    check("status() 输出抖音字段齐全", not missing, missing)
    check("status() 不泄漏 Cookie 明文（无 a1=/web_session= 等）",
          "web_session=" not in json.dumps(st, ensure_ascii=False) and "sessionid=" not in json.dumps(st, ensure_ascii=False))

    print("\n%d/%d 通过" % (_ok, _ok + _bad))
    return 0 if _bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

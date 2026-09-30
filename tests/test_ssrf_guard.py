"""SSRF 防护回归测试。

背景：``/api/parse`` 接收的是**用户粘贴的任意文本**。曾经平台判定用的是子串匹配
（``"xhs" in url``），而解析器在提取不出 ID 时会把原始 URL 直接出网（短链展开），
于是 ``http://127.0.0.1:8765/?xhs=1``、``http://169.254.169.254/...?xhs=1``
这类地址会被真的请求出去 —— 即 SSRF。

本测试锁定三件事：
  1. 那批攻击载荷必须被 ``detect()`` 拒绝，且**不得发起任何请求**；
  2. 正常的小红书/抖音链接（含短链、分享文案）必须仍然可用；
  3. 主机名后缀匹配要精确（``evilxhscdn.com``、``xhscdn.com.evil.com`` 必须拒绝）。

不需要任何第三方依赖：出网被 mock，``httpx`` / ``gmssl`` 用最小桩顶掉
（``httpx`` 只在 ``base.fetch`` 的兜底路径用到，测试里不会走到）。

运行：
    python tests/test_ssrf_guard.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

# 让 `parsers` 可导入（本文件在 <repo>/tests/ 下，包在 <repo>/grabber/ 下）
REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "grabber"
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

def _module_available(name: str) -> bool:
    """模块是否真的可用（不抛异常）。

    不能直接用 find_spec：若 sys.modules[name] 已被换成没有 __spec__ 的桩，
    find_spec 会抛 `ValueError: <name>.__spec__ is None`。
    """
    mod = sys.modules.get(name)
    if mod is not None:
        return getattr(mod, "__spec__", None) is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# ── 最小桩：只在**真的缺失**时注入，绝不覆盖环境里的真实依赖 ────────────
# 判据用 _module_available：`"httpx" not in sys.modules` 在导入 parsers 之前必然成立，
# 会把桩装上、遮住真库（曾导致本套件里的真实网络测试误报失败）。
if not _module_available("httpx"):
    _stub = types.ModuleType("httpx")

    class _StubClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise RuntimeError("stub httpx 不应被调用")

    _stub.AsyncClient = _StubClient
    _stub.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stub"))
    _stub.__spec__ = None
    sys.modules["httpx"] = _stub

if not _module_available("gmssl"):
    _gm = types.ModuleType("gmssl")
    _gm.sm3 = types.ModuleType("gmssl.sm3")
    _gm.func = types.ModuleType("gmssl.func")
    _gm.sm3.sm3_hash = lambda *a, **k: ""
    _gm.func.bytes_to_list = lambda b: list(b)
    _gm.func.list_to_bytes = lambda l: bytes(l)
    sys.modules["gmssl"] = _gm
    sys.modules["gmssl.sm3"] = _gm.sm3
    sys.modules["gmssl.func"] = _gm.func

import parsers  # noqa: E402
import parsers.douyin as douyin  # noqa: E402
import parsers.xhs as xhs  # noqa: E402
from parsers.base import (  # noqa: E402
    DOUYIN_HOSTS,
    MEDIA_HOSTS,
    XHS_HOSTS,
    assert_host_allowed,
    host_allowed,
)

# ── 记录出网调用，全程不发真实请求 ───────────────────────────────────────
CALLS: list[str] = []


class _FakeResp:
    def __init__(self, url: str = "", text: str = ""):
        self.status_code = 200
        self.text = text
        self.url = url
        self.headers: dict = {}
        self.cookies: dict = {}


async def _fake_fetch(url, *, headers=None, impersonate=None, allow_hosts=None):
    # 复刻真实 fetch 的白名单语义，让测试覆盖到校验逻辑本身
    if allow_hosts is not None:
        assert_host_allowed(url, allow_hosts)
    CALLS.append(url)
    return _FakeResp(url=url, text="<html></html>")


xhs.fetch = _fake_fetch
douyin.fetch = _fake_fetch

_passed = _failed = 0


def _check(label: str, ok: bool, extra: str = "") -> None:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}  {extra}")


# 攻击载荷：内网、云元数据、回环（含 IPv6 与用户信息伪装）、任意外部主机、后缀伪装
ATTACKS = [
    "http://127.0.0.1:8765/api/health?xhs=1",
    "http://169.254.169.254/latest/meta-data/?xhs=1",
    "http://192.168.1.1/admin?douyin=1",
    "http://[::1]:8765/?xhs=1",
    "http://localhost:8765/?xhs=1",
    "http://evil.example.com/?xhs=1",
    "http://evil.example.com/?douyin=1",
    "http://evilxhscdn.com/?xhs=1",
    "http://xdouyin.com/?douyin=1",
    "http://xiaohongshu.com.evil.com/?xhs=1",
    "http:///?xhs=1",
    "http://xhs@127.0.0.1:8765/?xhs=1",
]

# 正常链接：(输入, 期望平台)
GOOD = [
    ("https://www.xiaohongshu.com/explore/0123456789abcdef01234567", "xhs"),
    ("https://www.xiaohongshu.com/discovery/item/0123456789abcdef01234567", "xhs"),
    ("http://xhslink.com/aBcDeF", "xhs"),
    ("https://xhslink.com/aBcDeF?x=1", "xhs"),
    # .cn 短链变体：实测分享文案里很常见，曾经因为白名单只写 .com 而被误拒
    ("https://xhslink.cn/o/2DZLmLsKajt", "xhs"),
    ("http://xhslink.cn/o/aBcDeF", "xhs"),
    ("https://www.douyin.com/video/7300000000000000000", "douyin"),
    ("https://v.douyin.com/iRabcdef/", "douyin"),
    ("https://www.iesdouyin.com/share/video/7300000000000000000", "douyin"),
    ("看看这个 https://www.douyin.com/video/7300000000000000000 挺好", "douyin"),
    ("3.14 复制打开小红书 https://xhslink.com/aBcDeF 复制此链接", "xhs"),
    # 真实分享文案（带前后中文与结尾句号），链接抽取要能正确截断
    ("军训二则 这个贝果好好吃啊啊啊 https://xhslink.cn/o/2DZLmLsKajt 把这段复制好，然后去【小红书】就能看笔记。", "xhs"),
]

# 主机名边界
HOST_CASES = [
    ("https://a.xhscdn.com/x.jpg", MEDIA_HOSTS, True, "官方子域放行"),
    ("https://xhscdn.com/x.jpg", MEDIA_HOSTS, True, "裸域放行"),
    ("http://evilxhscdn.com/x.jpg", MEDIA_HOSTS, False, "伪装后缀拒绝"),
    ("http://xhscdn.com.evil.com/x.jpg", MEDIA_HOSTS, False, "后缀被劫持拒绝"),
    ("https://www.xiaohongshu.com/explore/0123456789abcdef01234567", XHS_HOSTS, True, "xhs 放行"),
    ("https://www.douyin.com/video/1", DOUYIN_HOSTS, True, "douyin 放行"),
    ("https://www.xiaohongshu.com/explore/0123456789abcdef01234567", DOUYIN_HOSTS, False, "跨平台拒绝"),
]


def main() -> int:
    print("=" * 76)
    print("1) 攻击载荷必须被拒绝，且不得发起任何请求")
    print("=" * 76)
    for payload in ATTACKS:
        CALLS.clear()
        try:
            url = parsers.extract_url(payload)
            platform = parsers.detect(url)
            asyncio.run(parsers.parse(payload))
            _check(f"拒绝 {payload}", False, f"-> 被放行为 {platform} 且未报错")
        except ValueError:
            _check(f"拒绝 {payload}", not CALLS, f"但仍发起了请求: {CALLS}")
        except Exception as exc:  # noqa: BLE001
            _check(f"拒绝 {payload}", False, f"抛出非 ValueError: {type(exc).__name__}: {exc}")

    print()
    print("=" * 76)
    print("2) 正常链接必须仍然可用")
    print("=" * 76)
    for text, expect in GOOD:
        try:
            got = parsers.detect(parsers.extract_url(text))
            _check(f"{expect:6} <- {text[:54]}", got == expect, f"得到 {got}")
        except ValueError as exc:
            _check(f"{expect:6} <- {text[:54]}", False, f"被误拒: {exc}")

    print()
    print("=" * 76)
    print("3) 主机名后缀匹配边界")
    print("=" * 76)
    for url, hosts, expect, label in HOST_CASES:
        got = host_allowed(url, hosts)
        _check(f"{label:18} {url[:44]}", got == expect, f"得到 {got}")

    print()
    print("=" * 76)
    print("4) 重定向到非白名单主机必须被拦截")
    print("=" * 76)
    for target, label in [
        ("http://169.254.169.254/latest/meta-data/", "云元数据地址"),
        ("http://127.0.0.1:8765/", "本机回环"),
    ]:
        try:
            assert_host_allowed(target, XHS_HOSTS, what="跳转地址")
            _check(f"重定向到{label}被拒", False)
        except ValueError:
            _check(f"重定向到{label}被拒", True)

    print()
    print("=" * 76)
    print(f"结果: {_passed} 通过, {_failed} 失败")
    print("=" * 76)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

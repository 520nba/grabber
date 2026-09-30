"""验证请求指纹（impersonate）解析：自动适配 curl_cffi 改名、缓存、可用性回退。

背景：curl_cffi 会**改名**。实测 0.16.3 里原本写死在代码里的
``safari17_0_ios`` 已不存在（改成 ``safari18_0_ios`` / ``safari17_2_ios`` 这类
带下划线的形式），于是**每一次**抖音/小红书请求都抛 ImpersonateError、
静默降级到系统 curl，白白丢掉了 curl_cffi 的 TLS 指纹优势。

修法不是换成一个新名字（下次还会过期），而是探测 + 缓存 + 回退：
  · 调用点只写语义标记 "ios" / "chrome"；
  · 解析器按偏好顺序实测哪个可用，缓存结果；
  · 全不可用时返回 None，由 fetch 降级到系统 curl。

本测试分两层：
  A. 不依赖 curl_cffi / 网络（用假模块）——验证候选顺序、缓存、全部失败返回 None、
     语义标记不会被当成真实指纹去试；
  B. 真实环境（可选）——若装了 curl_cffi 就实测一次，并确认 fetch 的 via 带具体指纹。

运行：
    python tests/test_impersonate.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "grabber"))

_ok = _bad = 0


def check(label, cond, extra=""):
    global _ok, _bad
    if cond:
        _ok += 1
        print(f"  PASS  {label}")
    else:
        _bad += 1
        print(f"  FAIL  {label}  {extra}")


def _module_available(name: str) -> bool:
    mod = sys.modules.get(name)
    if mod is not None:
        return getattr(mod, "__spec__", None) is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


HAS_CURL_CFFI = _module_available("curl_cffi")

# 只在真缺 httpx 时打桩（base 顶层 import httpx）
if not _module_available("httpx"):
    _s = types.ModuleType("httpx")
    class _C:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    _s.AsyncClient = _C
    _s.__spec__ = None
    sys.modules["httpx"] = _s

# 同理 gmssl（abogus 顶层 import，本测试用不到）
if not _module_available("gmssl"):
    _g = types.ModuleType("gmssl")
    _g.sm3 = types.ModuleType("gmssl.sm3")
    _g.func = types.ModuleType("gmssl.func")
    _g.sm3.sm3_hash = lambda *a, **k: ""
    _g.func.bytes_to_list = lambda b: list(b)
    _g.func.list_to_bytes = lambda l: bytes(l)
    sys.modules["gmssl"] = _g
    sys.modules["gmssl.sm3"] = _g.sm3
    sys.modules["gmssl.func"] = _g.func

import parsers.base as B  # noqa: E402


def fake_curl_cffi(supported: set[str], record: list):
    """造一个假的 curl_cffi：只有 supported 里的指纹名能用。

    必须同时提供 `curl_cffi.requests` 与 `curl_cffi.requests.exceptions`
    （后者要有 ImpersonateError）——解析器就是靠捕获它来判定「名字不被支持」的。
    """
    class _Err(Exception):
        pass

    mod = types.ModuleType("curl_cffi")
    sub = types.ModuleType("curl_cffi.requests")
    exc = types.ModuleType("curl_cffi.requests.exceptions")
    exc.ImpersonateError = _Err

    def get(url, impersonate=None, timeout=None, proxy=None, **kw):
        record.append(impersonate)
        if impersonate not in supported:
            raise _Err(f"Impersonating {impersonate} is not supported")
        return types.SimpleNamespace(status_code=200, text="ok")

    sub.get = get
    sub.exceptions = exc
    mod.requests = sub
    return mod, sub, exc


def with_fake(supported: set[str]):
    """装上假 curl_cffi 并清空解析缓存，返回 (record, 还原函数)。"""
    record: list = []
    keys = ("curl_cffi", "curl_cffi.requests", "curl_cffi.requests.exceptions")
    saved = {k: sys.modules.get(k) for k in keys}
    mod, sub, exc = fake_curl_cffi(supported, record)
    sys.modules["curl_cffi"] = mod
    sys.modules["curl_cffi.requests"] = sub
    sys.modules["curl_cffi.requests.exceptions"] = exc
    cache, tried, warned = dict(B._IMPERSONATE_CACHE), dict(B._IMPERSONATE_TRIED), set(B._IMPERSONATE_WARNED)
    B._IMPERSONATE_CACHE.clear()
    B._IMPERSONATE_TRIED.clear()
    B._IMPERSONATE_WARNED.clear()

    def restore():
        B._IMPERSONATE_CACHE.clear(); B._IMPERSONATE_CACHE.update(cache)
        B._IMPERSONATE_TRIED.clear(); B._IMPERSONATE_TRIED.update(tried)
        B._IMPERSONATE_WARNED.clear(); B._IMPERSONATE_WARNED.update(warned)
        for k in keys:
            if saved[k] is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = saved[k]
    return record, restore


def main() -> int:
    print("=" * 76)
    print("A) 解析逻辑（假 curl_cffi，无网络）")
    print("=" * 76)

    # A1 只有旧命名可用 → 应回退到旧命名（老版本 curl_cffi 场景）
    rec, restore = with_fake({"safari17_0_ios", "chrome"})
    try:
        got = B._resolve_impersonate("ios")
        check("仅旧命名可用时回退到 safari17_0_ios", got == "safari17_0_ios", str(got))
        check("候选按顺序试过", rec and rec[0].startswith("safari1"), str(rec))
        check("纯语义标记 'ios' 没被当成指纹去试", "ios" not in rec, str(rec))
    finally:
        restore()

    # A2 新命名可用 → 优先新命名
    rec, restore = with_fake({"safari18_0_ios", "chrome"})
    try:
        got = B._resolve_impersonate("ios")
        check("新命名可用时选 safari18_0_ios", got == "safari18_0_ios", str(got))
        check("命中即停止（只试了一次）", len(rec) == 1, str(rec))
    finally:
        restore()

    # A3 缓存：第二次不再探测
    rec, restore = with_fake({"safari18_0_ios"})
    try:
        first = B._resolve_impersonate("ios")
        n_after_first = len(rec)
        second = B._resolve_impersonate("ios")
        check("第二次命中缓存（不再探测）", len(rec) == n_after_first, f"{rec}")
        check("两次结果一致", first == second == "safari18_0_ios", f"{first}/{second}")
    finally:
        restore()

    # A4 全部失败 → None（由 fetch 降级到系统 curl）
    rec, restore = with_fake(set())
    try:
        got = B._resolve_impersonate("ios")
        check("全部不可用时返回 None", got is None, str(got))
        check("确实把候选都试过了", len(rec) >= 4, str(rec))
        check("可在诊断里看到尝试记录", bool(B._IMPERSONATE_TRIED.get("ios")), str(B._IMPERSONATE_TRIED))
    finally:
        restore()

    # A5 chrome 也走同一套逻辑
    rec, restore = with_fake({"chrome"})
    try:
        check("chrome 可解析", B._resolve_impersonate("chrome") == "chrome")
    finally:
        restore()

    # A6 curl_cffi 完全不可用 → None，不抛异常
    # 注意：不能用 sys.modules.pop——环境里真装了 curl_cffi 时它会被重新导入成功。
    # 把 sys.modules[name] 设成 None，Python 的 import 会直接抛 ImportError，
    # 这才是「没装」的准确模拟。
    saved = sys.modules.get("curl_cffi", "MISSING")
    saved_sub = sys.modules.get("curl_cffi.requests", "MISSING")
    saved_cache = dict(B._IMPERSONATE_CACHE)
    B._IMPERSONATE_CACHE.clear()
    sys.modules["curl_cffi"] = None
    sys.modules["curl_cffi.requests"] = None
    try:
        got = B._resolve_impersonate("ios")
        check("curl_cffi 不可用时返回 None 且不抛异常", got is None, str(got))
        check("原因可读", bool(B._IMPERSONATE_TRIED.get("ios")), str(B._IMPERSONATE_TRIED))
    finally:
        B._IMPERSONATE_CACHE.clear(); B._IMPERSONATE_CACHE.update(saved_cache)
        if saved == "MISSING": sys.modules.pop("curl_cffi", None)
        else: sys.modules["curl_cffi"] = saved
        if saved_sub == "MISSING": sys.modules.pop("curl_cffi.requests", None)
        else: sys.modules["curl_cffi.requests"] = saved_sub

    print()
    print("=" * 76)
    print("B) 语义标记与候选表")
    print("=" * 76)
    check("'ios' 是语义标记（映射到候选表）", "ios" in B._IMPERSONATE_PREFIX)
    check("候选表含新旧两种命名",
          any(n.startswith("safari18") or n.startswith("safari17_2") for n in B._IOS_IMPERSONATE_CANDIDATES)
          and "safari17_0_ios" in B._IOS_IMPERSONATE_CANDIDATES,
          str(B._IOS_IMPERSONATE_CANDIDATES))
    check("调用点不再写死废弃名字",
          "safari17_0_ios" not in (REPO / "grabber" / "parsers" / "xhs.py").read_text(encoding="utf-8")
          and "safari17_0_ios" not in (REPO / "grabber" / "parsers" / "douyin.py").read_text(encoding="utf-8"))
    diag = B.impersonate_diagnostics()
    check("诊断接口含 resolved/candidates", "resolved" in diag and "candidates" in diag, str(list(diag)))

    print()
    print("=" * 76)
    print("C) 真实环境（可选）")
    print("=" * 76)
    if not HAS_CURL_CFFI:
        print("  跳过（未安装 curl_cffi）")
    else:
        cache, tried = dict(B._IMPERSONATE_CACHE), dict(B._IMPERSONATE_TRIED)
        B._IMPERSONATE_CACHE.clear(); B._IMPERSONATE_TRIED.clear()
        try:
            got = B._resolve_impersonate("ios")
            print(f"    本机 curl_cffi 解析 'ios' -> {got}")
            check("真实环境能解析出 iOS 指纹", bool(got), str(got))
            if got:
                check("解析结果确实在候选表里", got in B._IOS_IMPERSONATE_CANDIDATES, got)
        finally:
            B._IMPERSONATE_CACHE.clear(); B._IMPERSONATE_CACHE.update(cache)
            B._IMPERSONATE_TRIED.clear(); B._IMPERSONATE_TRIED.update(tried)

    print()
    print("=" * 76)
    print(f"结果: {_ok} 通过, {_bad} 失败")
    print("=" * 76)
    return 0 if _bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

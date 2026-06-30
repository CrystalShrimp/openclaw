#!/usr/bin/env python3
"""DeepSeek API 诊断工具。

逐项检查 DeepSeek API 配置和连通性，定位问题原因。

Usage:
  python scripts/diagnose_deepseek.py
  python scripts/diagnose_deepseek.py --base-url https://api.deepseek.com/v1 --api-key sk-xxx --model deepseek-chat
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# Fix Windows terminal encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import httpx
except ImportError:
    print("[ERROR] httpx 未安装，请运行: pip install httpx")
    sys.exit(1)


# ── helpers ──────────────────────────────────────────────────────────

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}[OK]{RESET}   {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}[FAIL]{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}[WARN]{RESET} {msg}")


def section(title: str) -> None:
    print(f"\n{BOLD}── {title} ──{RESET}")


# ── diagnostic steps ─────────────────────────────────────────────────

def check_config(base_url: str, api_key: str, model: str) -> bool:
    """Step 1: Check configuration values."""
    section("1. 配置检查")
    all_ok = True

    if not base_url:
        fail("DEEPSEEK_BASE_URL 为空")
        all_ok = False
    else:
        ok(f"Base URL: {base_url}")

    if not api_key:
        fail("DEEPSEEK_API_KEY 为空，请在 .env 中配置")
        all_ok = False
    elif len(api_key) < 20:
        warn(f"API Key 长度异常（仅 {len(api_key)} 字符），可能不完整: {api_key[:6]}...")
        all_ok = False
    else:
        ok(f"API Key: {api_key[:8]}...{api_key[-4:]} ({len(api_key)} 字符)")

    if not model:
        warn("DEEPSEEK_MODEL 为空，将使用默认值 deepseek-chat")
    else:
        ok(f"Model: {model}")

    return all_ok


def check_dns(base_url: str) -> bool:
    """Step 2: DNS resolution."""
    section("2. DNS 解析")
    import urllib.parse

    host = urllib.parse.urlparse(base_url).hostname or base_url
    try:
        import socket
        t0 = time.time()
        addrs = socket.getaddrinfo(host, None)
        elapsed = (time.time() - t0) * 1000
        ipv4s = {a[4][0] for a in addrs if a[0] == socket.AF_INET}
        ipv6s = {a[4][0] for a in addrs if a[0] == socket.AF_INET6}
        ok(f"DNS 解析成功 ({elapsed:.0f}ms)")
        print(f"         IPv4: {', '.join(ipv4s)}")
        if ipv6s:
            print(f"         IPv6: {', '.join(ipv6s)}")
        return True
    except socket.gaierror as e:
        fail(f"DNS 解析失败: {e}")
        warn("可能原因: 网络不通、DNS 被污染、需要代理")
        return False


def check_tcp(base_url: str) -> bool:
    """Step 3: TCP connection."""
    section("3. TCP 连接")
    import socket
    import urllib.parse

    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname or base_url
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        t0 = time.time()
        sock = socket.create_connection((host, port), timeout=10)
        elapsed = (time.time() - t0) * 1000
        sock.close()
        ok(f"TCP 连接成功 {host}:{port} ({elapsed:.0f}ms)")
        return True
    except socket.timeout:
        fail(f"TCP 连接超时 {host}:{port} (10s)")
        warn("可能原因: 防火墙拦截、需要代理、服务端不可达")
        return False
    except OSError as e:
        fail(f"TCP 连接失败: {e}")
        return False


def check_tls(base_url: str) -> bool:
    """Step 4: TLS handshake."""
    section("4. TLS 握手")
    import socket
    import ssl
    import urllib.parse

    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname or base_url
    port = parsed.port or 443

    if parsed.scheme != "https":
        warn("非 HTTPS，跳过 TLS 检查")
        return True

    try:
        t0 = time.time()
        ctx = ssl.create_default_context()
        sock = socket.create_connection((host, port), timeout=10)
        ssock = ctx.wrap_socket(sock, server_hostname=host)
        elapsed = (time.time() - t0) * 1000
        cipher = ssock.cipher()
        version = ssock.version()
        ssock.close()
        ok(f"TLS 握手成功 ({elapsed:.0f}ms)")
        print(f"         协议: {version}, 加密: {cipher[0]}")
        return True
    except ssl.SSLError as e:
        fail(f"TLS 握手失败: {e}")
        warn("可能原因: 证书问题、中间人攻击、SSL 版本不兼容")
        return False


def check_http(base_url: str, api_key: str, model: str) -> bool:
    """Step 5: HTTP request to /models endpoint (lightweight check)."""
    section("5. HTTP 连通性 (GET /models)")

    url = f"{base_url}/models"
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        t0 = time.time()
        resp = httpx.get(url, headers=headers, timeout=15)
        elapsed = (time.time() - t0) * 1000
        ok(f"HTTP 响应: {resp.status_code} ({elapsed:.0f}ms)")

        if resp.status_code == 401:
            fail("认证失败 (401 Unauthorized)")
            warn("可能原因: API Key 无效、已过期、或被禁用")
            _print_body(resp)
            return False
        elif resp.status_code == 403:
            fail("禁止访问 (403 Forbidden)")
            warn("可能原因: 账户权限不足、IP 被封禁")
            _print_body(resp)
            return False
        elif resp.status_code == 429:
            warn("速率限制 (429 Too Many Requests)")
            warn("API 可用但当前被限流，稍后重试")
            _print_body(resp)
            return True
        elif resp.status_code >= 500:
            fail(f"服务端错误 ({resp.status_code})")
            warn("DeepSeek 服务端异常，非本地问题")
            _print_body(resp)
            return False
        elif resp.status_code >= 400:
            fail(f"客户端错误 ({resp.status_code})")
            _print_body(resp)
            return False

        ok("API Key 认证通过")
        return True
    except httpx.ConnectError as e:
        fail(f"连接失败: {e}")
        warn("可能原因: 网络不通、需要代理")
        return False
    except httpx.TimeoutException:
        fail("HTTP 请求超时 (15s)")
        warn("可能原因: 网络慢、防火墙拦截、需要代理")
        return False


def check_chat(base_url: str, api_key: str, model: str) -> bool:
    """Step 6: Actual chat/completions request (same as classifier.py)."""
    section("6. Chat Completions 调用 (完整测试)")

    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": "回复 OK"},
        ],
        "max_tokens": 10,
        "temperature": 0,
    }

    print(f"  请求: POST {url}")
    print(f"  模型: {model}")

    try:
        t0 = time.time()
        resp = httpx.post(url, json=payload, headers=headers, timeout=30)
        elapsed = (time.time() - t0) * 1000
    except httpx.ConnectError as e:
        fail(f"连接失败: {e}")
        return False
    except httpx.TimeoutException:
        fail("请求超时 (30s)")
        warn("DeepSeek 服务响应过慢")
        return False

    print(f"  状态码: {resp.status_code}")
    print(f"  耗时: {elapsed:.0f}ms")

    if resp.status_code != 200:
        fail(f"请求失败: HTTP {resp.status_code}")
        _print_body(resp)
        _diagnose_status(resp.status_code, resp.text)
        return False

    try:
        data = resp.json()
    except Exception:
        fail(f"响应不是有效 JSON: {resp.text[:300]}")
        return False

    # Parse response
    try:
        content = data["choices"][0]["message"]["content"].strip()
        usage = data.get("usage", {})
        model_used = data.get("model", "?")
        ok(f"模型响应: {model_used}")
        print(f"  回复内容: {content!r}")
        print(f"  Token 用量: input={usage.get('prompt_tokens', '?')} output={usage.get('completion_tokens', '?')}")
    except (KeyError, IndexError) as e:
        fail(f"响应结构异常: {e}")
        print(f"  响应体: {_truncate(data, 500)}")
        return False

    ok("Chat Completions 调用成功!")
    return True


def check_classifier(base_url: str, api_key: str, model: str) -> bool:
    """Step 7: Test the actual classifier prompt (same as classifier.py)."""
    section("7. 分类器完整测试 (模拟 classifier.py)")

    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    system_prompt = (
        "你是一个任务难度分类器。根据用户的指令，判断任务难度。\n\n"
        "只回复一个词：easy 或 medium 或 hard"
    )
    test_prompt = "帮我看看 src/main.py 的内容"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": test_prompt[:500]},
        ],
        "max_tokens": 10,
        "temperature": 0,
    }

    print(f"  测试输入: {test_prompt!r}")

    try:
        t0 = time.time()
        resp = httpx.post(url, json=payload, headers=headers, timeout=30)
        elapsed = (time.time() - t0) * 1000
    except Exception as e:
        fail(f"请求异常: {e}")
        return False

    if resp.status_code != 200:
        fail(f"HTTP {resp.status_code}")
        _print_body(resp)
        return False

    data = resp.json()
    content = data["choices"][0]["message"]["content"].strip().lower()
    print(f"  分类结果: {content!r} ({elapsed:.0f}ms)")

    if content in ("easy", "medium", "hard"):
        ok(f"分类正常: {content}")
        return True
    else:
        warn(f"模型返回了非预期结果: {content!r}（分类器会回退到 medium）")
        return True


# ── utilities ────────────────────────────────────────────────────────

def _print_body(resp: httpx.Response) -> None:
    print(f"  响应体: {_truncate(resp.text, 500)}")


def _truncate(data, limit: int = 300) -> str:
    s = str(data)
    return s if len(s) <= limit else s[:limit] + "..."


def _diagnose_status(status_code: int, body: str) -> None:
    """Give specific diagnosis for common error codes."""
    if status_code == 400:
        warn("可能原因: 请求格式错误、模型名不对、参数无效")
    elif status_code == 401:
        warn("可能原因: API Key 无效或已过期")
    elif status_code == 402:
        warn("可能原因: 账户余额不足，请充值")
        print(f"  💡 登录 https://platform.deepseek.com 查看余额")
    elif status_code == 403:
        warn("可能原因: 账户权限不足或 IP 被封")
    elif status_code == 404:
        warn("可能原因: 模型名错误，请确认 model 参数")
        warn("  常用模型: deepseek-chat, deepseek-reasoner")
    elif status_code == 429:
        warn("可能原因: 触发速率限制，请降低请求频率或等待")
    elif status_code == 500:
        warn("DeepSeek 服务端内部错误，稍后重试")
    elif status_code == 503:
        warn("DeepSeek 服务不可用（维护/过载），稍后重试")


# ── main ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="DeepSeek API 诊断工具")
    parser.add_argument("--base-url", default=os.getenv("DEEPSEEK_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.getenv("DEEPSEEK_API_KEY", ""))
    parser.add_argument("--model", default=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"))
    args = parser.parse_args()

    print(f"{BOLD}DeepSeek API 诊断工具{RESET}")
    print("=" * 50)

    # Step 1: Config
    if not check_config(args.base_url, args.api_key, args.model):
        print(f"\n{RED}配置不完整，请检查 .env 文件中的 DEEPSEEK_* 配置项。{RESET}")
        sys.exit(1)

    results: list[tuple[str, bool]] = []

    # Step 2-3: Network
    results.append(("DNS 解析", check_dns(args.base_url)))
    if results[-1][1]:
        results.append(("TCP 连接", check_tcp(args.base_url)))
        if results[-1][1]:
            results.append(("TLS 握手", check_tls(args.base_url)))

    # Step 5: HTTP
    results.append(("HTTP 连通性", check_http(args.base_url, args.api_key, args.model)))

    # Step 6-7: API calls
    results.append(("Chat Completions", check_chat(args.base_url, args.api_key, args.model)))
    results.append(("分类器测试", check_classifier(args.base_url, args.api_key, args.model)))

    # Summary
    section("诊断结果汇总")
    all_pass = True
    for name, passed in results:
        status = f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"
        print(f"  {status}  {name}")
        if not passed:
            all_pass = False

    if all_pass:
        print(f"\n{GREEN}{BOLD}所有检查通过! DeepSeek API 工作正常。{RESET}")
    else:
        print(f"\n{RED}{BOLD}存在异常，请根据上方诊断信息排查。{RESET}")
        sys.exit(1)


if __name__ == "__main__":
    main()

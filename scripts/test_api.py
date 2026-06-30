#!/usr/bin/env python3
"""Test whether an API endpoint and key are working.

Usage:
  # Test with .env config
  python scripts/test_api.py

  # Test with custom params
  python scripts/test_api.py --base-url https://openrouter.ai/api/v1 --api-key sk-xxx --model anthropic/claude-3.5-haiku
"""
import argparse
import os
import sys
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import httpx
except ImportError:
    print("httpx not installed. Run: pip install httpx")
    sys.exit(1)


def test_api(base_url: str, api_key: str, model: str) -> None:
    url = f"{base_url}/messages"
    print(f"Testing: {url}")
    print(f"Model:   {model}")
    print(f"Key:     {api_key[:12]}...{api_key[-4:]}")
    print()

    payload = {
        "model": model,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "Say OK"}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    t0 = time.time()
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=30)
        elapsed = (time.time() - t0) * 1000
    except httpx.ConnectError as e:
        print(f"FAIL: Cannot connect to {base_url}")
        print(f"  Error: {e}")
        sys.exit(1)
    except httpx.TimeoutException:
        print(f"FAIL: Request timed out (30s)")
        sys.exit(1)

    print(f"Status:  {resp.status_code}")
    print(f"Latency: {elapsed:.0f}ms")

    if resp.status_code != 200:
        print(f"FAIL: HTTP {resp.status_code}")
        print(f"  Body: {resp.text[:500]}")
        sys.exit(1)

    data = resp.json()
    text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    reply = "\n".join(text_blocks) if text_blocks else "(no text)"
    usage = data.get("usage", {})

    print(f"Reply:   {reply}")
    print(f"Tokens:  in={usage.get('input_tokens', '?')} out={usage.get('output_tokens', '?')}")
    print(f"\nAPI OK")


def main():
    parser = argparse.ArgumentParser(description="Test API endpoint")
    parser.add_argument("--base-url", default=os.getenv("EASY_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.getenv("EASY_API_KEY", ""))
    parser.add_argument("--model", default=os.getenv("EASY_MODEL", "anthropic/claude-3.5-haiku"))
    args = parser.parse_args()

    if not args.base_url or not args.api_key:
        print("Missing --base-url or --api-key (and not found in .env)")
        sys.exit(1)

    test_api(args.base_url, args.api_key, args.model)


if __name__ == "__main__":
    main()

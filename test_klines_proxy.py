"""
Quick sanity test: can we fetch klines from BingX?
Run from host:  python test_klines_proxy.py
"""
import urllib.request
import json

URL = "https://open-api.bingx.com/openApi/swap/v2/quote/klines?symbol=LINK-USDT&interval=15m&limit=5"
req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=10) as resp:
    data = json.loads(resp.read().decode())

print("code:", data.get("code"))
print("len(data):", len(data.get("data", [])))
if data.get("data"):
    print("first candle:", data["data"][0])
    # Check timestamps
    ts = data["data"][0]["time"]
    print("timestamp (ms):", ts, " -> seconds:", ts // 1000)

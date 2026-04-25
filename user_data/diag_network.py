
import time
import socket
import requests
import json
import os

def test_connectivity():
    print("=== STARTING NETWORK DIAGNOSTIC ===")
    
    # 1. DNS check
    print("\n[1/4] Checking DNS for BingX...")
    start = time.time()
    try:
        ip = socket.gethostbyname("open-api-vst.bingx.com")
        print(f"✅ DNS OK: open-api-vst.bingx.com -> {ip} ({int((time.time()-start)*1000)}ms)")
    except Exception as e:
        print(f"❌ DNS FAILED: {e}")

    # 2. Proxy port check
    print("\n[2/4] Checking Proxy port (amneziawg2:1080)...")
    start = time.time()
    try:
        s = socket.create_connection(("amneziawg2", 1080), timeout=10)
        s.close()
        print(f"✅ Proxy port OK ({int((time.time()-start)*1000)}ms)")
    except Exception as e:
        print(f"❌ Proxy port FAILED (Is the container running?): {e}")

    # 3. Public API via Proxy
    print("\n[3/4] Checking BingX Public API via Proxy...")
    proxies = {
        'http': 'socks5h://amneziawg2:1080',
        'https': 'socks5h://amneziawg2:1080'
    }
    start = time.time()
    try:
        # Use a simple public endpoint
        res = requests.get("https://open-api-vst.bingx.com/openApi/swap/v2/quote/ticker", 
                           params={"symbol": "BTC-USDT"}, 
                           proxies=proxies, 
                           timeout=30)
        print(f"✅ Public API via Proxy OK: Status {res.status_code} ({int((time.time()-start)*1000)}ms)")
        print(f"   Data: {res.text[:100]}...")
    except Exception as e:
        print(f"❌ Public API via Proxy FAILED: {e}")

    # 4. Latency to amneziawg2 container
    print("\n[4/4] Checking ping-like latency to amneziawg2...")
    start = time.time()
    try:
        # Just a TCP connect/disconnect
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(("amneziawg2", 1080))
        s.close()
        print(f"✅ Latency OK: {int((time.time()-start)*1000)}ms")
    except Exception as e:
        print(f"❌ Latency test FAILED: {e}")

    print("\n=== DIAGNOSTIC COMPLETE ===")

if __name__ == "__main__":
    test_connectivity()

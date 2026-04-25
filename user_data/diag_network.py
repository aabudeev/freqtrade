
import time
import socket
import requests
import json
import os
import sys

def test_connectivity():
    print("=== STARTING ADVANCED NETWORK DIAGNOSTIC ===")
    
    # 1. DNS check
    print("\n[1/5] Checking DNS for BingX...")
    start = time.time()
    try:
        ip = socket.gethostbyname("open-api-vst.bingx.com")
        print(f"✅ DNS OK: open-api-vst.bingx.com -> {ip} ({int((time.time()-start)*1000)}ms)")
    except Exception as e:
        print(f"❌ DNS FAILED: {e}")

    # 2. Proxy port check
    print("\n[2/5] Checking Proxy port (amneziawg2:1080)...")
    start = time.time()
    try:
        s = socket.create_connection(("amneziawg2", 1080), timeout=10)
        s.close()
        print(f"✅ Proxy port OK ({int((time.time()-start)*1000)}ms)")
    except Exception as e:
        print(f"❌ Proxy port FAILED: {e}")

    # 3. Public API via Requests
    print("\n[3/5] Checking BingX Public API via Proxy (Requests)...")
    proxies = {
        'http': 'socks5h://amneziawg2:1080',
        'https': 'socks5h://amneziawg2:1080'
    }
    start = time.time()
    try:
        res = requests.get("https://open-api-vst.bingx.com/openApi/swap/v2/quote/ticker", 
                           params={"symbol": "BTC-USDT"}, 
                           proxies=proxies, 
                           timeout=30)
        print(f"✅ Public API OK: Status {res.status_code} ({int((time.time()-start)*1000)}ms)")
    except Exception as e:
        print(f"❌ Public API FAILED: {e}")

    # 4. Private API via CCXT (Full test)
    print("\n[4/5] Checking BingX Private API (Balance) via CCXT + Proxy...")
    try:
        # Load keys from config_vst.json
        config_path = 'user_data/config_vst.json'
        if not os.path.exists(config_path):
             config_path = 'user_data/config.json'
             
        with open(config_path, 'r') as f:
            config = json.load(f)
            exchange_config = config.get('exchange', {})
            
        import ccxt
        # Use sync CCXT for simplicity in script
        exchange = ccxt.bingx({
            'apiKey': exchange_config.get('key'),
            'secret': exchange_config.get('secret'),
            'proxies': {
                'http': 'socks5h://amneziawg2:1080',
                'https': 'socks5h://amneziawg2:1080'
            },
            'options': {'defaultType': 'swap'},
            'timeout': 30000
        })
        if exchange_config.get('sandbox'):
            exchange.set_sandbox_mode(True)
            
        start = time.time()
        balance = exchange.fetch_balance()
        vst_bal = balance['total'].get('VST', 0)
        usdt_bal = balance['total'].get('USDT', 0)
        print(f"✅ Private API OK: Balance VST={vst_bal}, USDT={usdt_bal} ({int((time.time()-start)*1000)}ms)")
    except Exception as e:
        print(f"❌ Private API FAILED: {e}")

    # 5. Latency to proxy
    print("\n[5/5] Internal latency to amneziawg2...")
    start = time.time()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(("amneziawg2", 1080))
        s.close()
        print(f"✅ Internal Latency: {int((time.time()-start)*1000)}ms")
    except Exception as e:
        print(f"❌ Internal Latency FAILED: {e}")

    print("\n=== DIAGNOSTIC COMPLETE ===")

if __name__ == "__main__":
    test_connectivity()

import ccxt
import os
import asyncio

async def main():
    exchange = ccxt.bingx({
        'apiKey': os.getenv('EXCHANGE_KEY'),
        'secret': os.getenv('EXCHANGE_SECRET'),
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'}
    })
    exchange.set_sandbox_mode(True)
    
    try:
        balance = exchange.fetch_balance()
        print("Sandbox balance keys:", list(balance.keys()))
        for k, v in balance.items():
            if isinstance(v, dict) and 'free' in v:
                print(f"{k}: free={v.get('free')} total={v.get('total')}")
    except Exception as e:
        print("Error:", e)

asyncio.run(main())

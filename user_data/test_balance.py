import ccxt
import os
import json
import asyncio

async def test():
    # Загружаем конфиги как это делает бот
    with open('user_data/config.json') as f:
        config = json.load(f)
    with open('user_data/config_vst.json') as f:
        vst_config = json.load(f)

    options = config['exchange']['ccxt_async_config'].copy()
    options.update({
        'apiKey': os.getenv('FREQTRADE__EXCHANGE__KEY'),
        'secret': os.getenv('FREQTRADE__EXCHANGE__SECRET'),
    })
    
    # Принудительно включаем песочницу
    exchange = ccxt.async_support.bingx(options)
    exchange.set_sandbox_mode(True)
    
    print(f"Проверка связи через прокси: {options.get('socksProxy')}")
    try:
        balance = await exchange.fetch_balance()
        print(f"✅ Баланс успешно получен! USDT (VST): {balance['total'].get('USDT', 'N/A')}")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
    finally:
        await exchange.close()

if __name__ == '__main__':
    asyncio.run(test())

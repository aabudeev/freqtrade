import ccxt
import os
import json

def test():
    # Загружаем конфиги
    with open('user_data/config.json') as f:
        config = json.load(f)
    with open('user_data/config_vst.json') as f:
        vst_config = json.load(f)

    # Используем синхронный CCXT для простоты теста
    options = config['exchange']['ccxt_async_config'].copy()
    
    # Убираем socksProxy из обычных опций, CCXT ожидает его в другом формате для синхронного режима
    proxy = options.pop('socksProxy', None)
    
    options.update({
        'apiKey': os.getenv('FREQTRADE__EXCHANGE__KEY'),
        'secret': os.getenv('FREQTRADE__EXCHANGE__SECRET'),
        'enableRateLimit': True,
    })
    
    if proxy:
        options['proxies'] = {
            'http': proxy.replace('socks5://', 'socks5h://'),
            'https': proxy.replace('socks5://', 'socks5h://'),
        }

    exchange = ccxt.bingx(options)
    exchange.set_sandbox_mode(True)
    
    print(f"Проверка связи (Sync) через: {proxy}")
    try:
        balance = exchange.fetch_balance()
        print(f"✅ УСПЕХ! Баланс USDT (VST): {balance['total'].get('USDT', 'N/A')}")
    except Exception as e:
        print(f"❌ ОШИБКА: {e}")

if __name__ == '__main__':
    test()

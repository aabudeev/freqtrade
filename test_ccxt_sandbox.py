import ccxt
b = ccxt.bingx({'options': {'sandboxMode': True}})
print("test" in b.urls['api']['swap'])

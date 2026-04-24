import os
import sys

from freqtrade.signals.parser import parse_signal_text

text = """📈 LONG 
 
▪Монета: DOGE
▪Плечо: 25-50х
▪Вход: от 0.09717 до 0.09425
▪Цель: 0.09814
▪Стоп: 0.09134"""

res = parse_signal_text(text)
print("Result:", res)

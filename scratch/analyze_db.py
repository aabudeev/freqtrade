import sqlite3
import json

db_path = "user_data/trades_signals.sqlite"

def analyze_leverage():
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check for all tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [t[0] for t in cursor.fetchall()]
    print(f"Tables in DB: {tables}")
    
    # In newer Freqtrade, custom_data is often in a separate table 'trade_custom_data'
    custom_data_map = {}
    if 'trade_custom_data' in tables:
        cursor.execute("SELECT ft_trade_id, cd_key, cd_value FROM trade_custom_data WHERE cd_key='signal_sl'")
        for row in cursor.fetchall():
            custom_data_map[row[0]] = row[2]
    
    # Query all trades
    cursor.execute("SELECT id, pair, is_short, open_rate, leverage FROM trades")
    trades = cursor.fetchall()
    
    print("\n--- Trade Analysis ---")
    print(f"{'ID':<3} | {'Pair':<12} | {'Side':<5} | {'Open':<8} | {'Signal SL':<10} | {'SL %':<8} | {'Max Lev':<8}")
    print("-" * 75)
    
    distances = []
    
    for t in trades:
        tid, pair, is_short, open_rate, leverage = t
        
        signal_sl = custom_data_map.get(tid)
        
        if signal_sl:
            sl_price = float(signal_sl)
            # Distance as percentage
            dist = abs(open_rate - sl_price) / open_rate
            # Max leverage to avoid liquidation (at 95% of distance)
            # 1/lev = dist -> lev = 1/dist
            # We want liquidation to be further than SL. 
            # Liquidation distance is approx 1/lev. 
            # So 1/lev > dist -> lev < 1/dist.
            max_lev = 0.95 / dist if dist > 0 else 0
            
            side_str = "SHORT" if is_short else "LONG"
            print(f"{tid:<3} | {pair:<12} | {side_str:<5} | {open_rate:<8.4f} | {sl_price:<10.4f} | {dist*100:<7.2f}% | {max_lev:<8.1f}x")
            distances.append(dist)
    
    if distances:
        avg_dist = sum(distances) / len(distances)
        max_dist = max(distances)
        print("-" * 75)
        print(f"Average SL distance: {avg_dist*100:.2f}%")
        print(f"Maximum SL distance: {max_dist*100:.2f}%")
        print(f"Recommended leverage (to survive all): {0.90 / max_dist:.1f}x")
        print(f"Recommended leverage (to survive average): {0.90 / avg_dist:.1f}x")

if __name__ == "__main__":
    analyze_leverage()

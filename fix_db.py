import sqlite3
import os

def fix_db():
    db_path = 'user_data/trades_signals.sqlite'
    if not os.path.exists(db_path):
        print(f"Error: Could not find database at {db_path}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    
    cur.execute('SELECT id, pair, open_rate, close_rate, amount, fee_open, fee_open_cost, fee_close, fee_close_cost, leverage, funding_fees FROM trades')
    trades = cur.fetchall()
    
    fixed_count = 0
    for row in trades:
        trade_id = row['id']
        open_rate = row['open_rate']
        amount = row['amount']
        leverage = row['leverage']
        fee_open = row['fee_open']
        funding_fees = row['funding_fees'] or 0.0
        
        # If fee_open is weirdly high (e.g. > 0.005 for futures, meaning >0.5% fee), it's likely multiplied by leverage!
        if fee_open and leverage and fee_open > 0.005:
            open_value = amount * open_rate
            fee_open_cost = row['fee_open_cost']
            
            if fee_open_cost and open_value > 0:
                correct_fee_open = fee_open_cost / open_value
                correct_fee_close = correct_fee_open
                
                close_rate = row['close_rate']
                if row['fee_close_cost'] and close_rate:
                    close_value = amount * close_rate
                    if close_value > 0:
                        correct_fee_close = row['fee_close_cost'] / close_value
                        
                print(f'Fixing Trade {trade_id} ({row["pair"]}): fee rate {fee_open:.6f} -> {correct_fee_open:.6f}')
                
                # Recalculate everything
                open_trade_value = open_value - fee_open_cost
                
                profit_abs = 0.0
                profit_ratio = 0.0
                if close_rate:
                    close_value = amount * close_rate
                    fee_close_cost = close_value * correct_fee_close
                    close_trade_value = close_value + fee_close_cost - funding_fees
                    profit_abs = open_trade_value - close_trade_value
                    
                    max_stake = open_value / leverage
                    profit_ratio = profit_abs / (max_stake * (1 - correct_fee_open))
                    
                    print(f'  -> New Profit Abs: {profit_abs:.4f}, Ratio: {profit_ratio:.4%}')
                
                cur.execute('''
                    UPDATE trades 
                    SET fee_open = ?, fee_close = ?, open_trade_value = ?, close_profit = ?, close_profit_abs = ?, realized_profit = ?
                    WHERE id = ?
                ''', (correct_fee_open, correct_fee_close, open_trade_value, profit_ratio, profit_abs, profit_abs, trade_id))
                fixed_count += 1
                
    conn.commit()
    conn.close()
    
    if fixed_count > 0:
        print(f"Successfully fixed {fixed_count} trades in the database!")
    else:
        print("No corrupted trades found. Database is fine.")

if __name__ == '__main__':
    fix_db()

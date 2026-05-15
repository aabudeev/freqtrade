import sqlite3
import re

db_path = "user_data/signals.db"

# Patterns from parser.py
_ENTRY_PRICE_PATTERN = re.compile(r"^.*?Вход:\s*(?:от\s*)?([\d\.]+)(?:\s*(?:-|до)\s*([\d\.]+))?\s*$", re.IGNORECASE | re.MULTILINE)
_ENTRY_STOP_PATTERN = re.compile(r"^.*?Стоп:\s*([\d\.]+)\s*$", re.IGNORECASE | re.MULTILINE)

def analyze_signals():
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("SELECT text FROM ingest_queue WHERE text LIKE '%Вход:%'")
    messages = cursor.fetchall()
    
    distances = []
    
    print("\n--- Raw Signal Analysis ---")
    print(f"{'Signal Text Snippet':<40} | {'Entry':<8} | {'Stop':<8} | {'Dist %':<8} | {'Max Lev'}")
    print("-" * 85)
    
    for (text,) in messages:
        price_match = _ENTRY_PRICE_PATTERN.search(text)
        stop_match = _ENTRY_STOP_PATTERN.search(text)
        
        if price_match and stop_match:
            p1 = float(price_match.group(1))
            p2 = float(price_match.group(2)) if price_match.group(2) else p1
            avg_entry = (p1 + p2) / 2.0
            stop_price = float(stop_match.group(1))
            
            dist = abs(avg_entry - stop_price) / avg_entry
            max_lev = 0.95 / dist if dist > 0 else 0
            
            snippet = text[:40].replace('\n', ' ')
            print(f"{snippet:<40} | {avg_entry:<8.4f} | {stop_price:<8.4f} | {dist*100:<7.2f}% | {max_lev:.1f}x")
            distances.append(dist)
            
    if distances:
        avg_dist = sum(distances) / len(distances)
        max_dist = max(distances)
        print("-" * 85)
        print(f"Total Signals Analyzed: {len(distances)}")
        print(f"Average Signal SL distance: {avg_dist*100:.2f}%")
        print(f"Maximum Signal SL distance: {max_dist*100:.2f}%")
        print(f"Recommended leverage (Safe): {0.90 / max_dist:.1f}x")
        print(f"Recommended leverage (Moderate): {0.90 / avg_dist:.1f}x")

if __name__ == "__main__":
    analyze_signals()

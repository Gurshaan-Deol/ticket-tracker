import sqlite3
conn = sqlite3.connect('/app/data/db.sqlite3')
rows = conn.execute("""
    SELECT name, quantity, is_available 
    FROM listings 
    WHERE event_id=2 AND name LIKE 'LAWN%'
    ORDER BY name, quantity
""").fetchall()
for r in rows: print(r)
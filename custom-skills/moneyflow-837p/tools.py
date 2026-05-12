import sqlite3

def query_moneyflow_db(query: str):
    conn = sqlite3.connect('moneyflow.db')
    return conn.execute(query).fetchall()

def parse_837p_file(filepath: str):
    # Hook to your 837P parser v2.1
    print(f'Parsing {filepath} for CuNtx MoneyFlow')
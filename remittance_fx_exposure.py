import sqlite3
from pathlib import Path
from typing import Tuple
import pandas as pd

DB_PATH = Path("remittance_fx.db")
DATA_PATH = Path("data/comprovantes_pix_10000_anomalias.csv")


def create_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS remittances (
            txn_id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            origin_currency TEXT NOT NULL,
            destination_currency TEXT NOT NULL,
            amount_origin REAL NOT NULL,
            fx_rate REAL NOT NULL,
            txn_timestamp TEXT NOT NULL
        );
        """
    )
    conn.commit()

def load_csv_to_db(conn: sqlite3.Connection, csv_path: Path) -> None:
    # Читаем Pix-датасет (разделитель ; )
    df_raw = pd.read_csv(csv_path, sep=";")

    print("Columns in raw CSV:", df_raw.columns)

    df = pd.DataFrame()

    # 1) ID транзакции
    df["txn_id"] = df_raw["EndToEndId"]

    # 2) Клиент (плательщик)
    df["customer_id"] = df_raw["Pagador_CPF_CNPJ"].astype(str)

    # 3) Валюты
    df["origin_currency"] = df_raw["Moeda"]          # из CSV (скорее всего BRL)
    df["destination_currency"] = "BRL"               # для простоты фиксируем

    # 4) Сумма
    df["amount_origin"] = df_raw["Valor"].astype(float)

    # 5) FX-курс (у Pix его нет → ставим 1.0 как заглушку)
    df["fx_rate"] = 1.0

    # 6) Время транзакции — мягкий парсинг, битые даты → NaT
    df["txn_timestamp"] = pd.to_datetime(
        df_raw["DataHora"],
        errors="coerce",   # не падать на ошибках, а ставить NaT
        format="mixed"     # пусть pandas сам подбирает формат
    )

    # Выкинем строки с битой датой
    before = len(df)
    df = df[df["txn_timestamp"].notna()].copy()
    after = len(df)
    print(f"Dropped {before - after} rows with invalid timestamps")

    # Грузим в БД
    df.to_sql("remittances", conn, if_exists="replace", index=False)




def daily_currency_exposure(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Считает дневную экспозицию по валюте отправителя:
    сумма amount_origin по каждой валюте и дате.
    """
    query = """
        SELECT
            DATE(txn_timestamp) AS txn_date,
            origin_currency,
            SUM(amount_origin) AS total_amount_origin,
            COUNT(*) AS txn_count
        FROM remittances
        GROUP BY txn_date, origin_currency
        ORDER BY txn_date, origin_currency;
    """
    return pd.read_sql_query(query, conn)


def top_currencies_by_volume(conn: sqlite3.Connection, limit: int = 5) -> pd.DataFrame:
    query = """
        SELECT
            origin_currency,
            SUM(amount_origin) AS total_volume
        FROM remittances
        GROUP BY origin_currency
        ORDER BY total_volume DESC
        LIMIT ?;
    """
    return pd.read_sql_query(query, conn, params=(limit,))


def avg_ticket_size_by_corridor(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Средний чек по FX-коридорам (origin -> destination).
    """
    query = """
        SELECT
            origin_currency || '->' || destination_currency AS corridor,
            AVG(amount_origin) AS avg_ticket_size,
            COUNT(*) AS txn_count
        FROM remittances
        GROUP BY origin_currency, destination_currency
        HAVING txn_count >= 5
        ORDER BY avg_ticket_size DESC;
    """
    return pd.read_sql_query(query, conn)


def realized_fx_revenue_estimate(conn: sqlite3.Connection, spread_bps: float = 20.0) -> Tuple[float, pd.DataFrame]:
    """
    Очень упрощённая оценка FX revenue:
    revenue ≈ notional_in_destination * spread.
    spread_bps – спред в базисных пунктах (например, 20 bps = 0.20%).
    """
    query = """
        SELECT
            txn_id,
            origin_currency,
            destination_currency,
            amount_origin,
            fx_rate,
            amount_origin * fx_rate AS amount_destination
        FROM remittances;
    """
    df = pd.read_sql_query(query, conn)
    spread = spread_bps / 10_000.0
    df["est_revenue"] = df["amount_destination"] * spread

    total_revenue = float(df["est_revenue"].sum())
    return total_revenue, df[["txn_id", "origin_currency", "destination_currency", "amount_origin", "amount_destination", "est_revenue"]]


def main():
    if not DATA_PATH.exists():
        raise FileNotFoundError(
            f"{DATA_PATH} not found. Put your remittance CSV file into the data/ folder."
        )

    conn = create_connection(DB_PATH)
    init_schema(conn)
    load_csv_to_db(conn, DATA_PATH)

    print("Daily FX exposure by origin currency:")
    exposure_df = daily_currency_exposure(conn)
    print(exposure_df.head())

    print("\nTop origin currencies by volume:")
    top_curr_df = top_currencies_by_volume(conn, limit=5)
    print(top_curr_df)

    print("\nAverage ticket size by FX corridor:")
    corridor_df = avg_ticket_size_by_corridor(conn)
    print(corridor_df.head())

    print("\nEstimated FX revenue (with 20 bps spread):")
    total_rev, rev_df = realized_fx_revenue_estimate(conn, spread_bps=20.0)
    print(f"Total estimated FX revenue: {total_rev:,.2f}")
    print(rev_df.head())

    conn.close()


if __name__ == "__main__":
    main()

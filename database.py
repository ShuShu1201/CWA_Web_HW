"""database.py - 負責 SQLite 資料庫建立、儲存與查詢操作 (Step 3).

資料庫檔案: data.db
資料表名稱: TemperatureForecasts

Schema:
CREATE TABLE IF NOT EXISTS TemperatureForecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    regionName TEXT NOT NULL,
    dataDate TEXT NOT NULL,
    minT REAL,
    maxT REAL
);

核心功能：
1. init_db()
2. insert_forecasts() - 具備 UPSERT 防重複機制，避免重複執行無限制膨脹資料
3. get_regions()
4. get_forecasts()
5. get_all_forecasts()
6. 支援 parameterized queries 防止 SQL Injection
7. 內建基本資料驗證
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pandas as pd

DEFAULT_DB_PATH = Path("data.db")


class DatabaseError(Exception):
    """自訂資料庫操作例外。"""


def get_connection(db_path: Union[str, Path] = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """建立並取得 SQLite 資料庫連線。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Union[str, Path] = DEFAULT_DB_PATH) -> None:
    """初始化 SQLite 資料庫與 TemperatureForecasts 資料表。

    建立必要之索引與唯一性條件以支援 UPSERT 避免重複寫入。
    """
    path = Path(db_path)
    with get_connection(path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS TemperatureForecasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                regionName TEXT NOT NULL,
                dataDate TEXT NOT NULL,
                minT REAL,
                maxT REAL
            );
            """
        )
        # 建立複合唯一索引，確保 (regionName, dataDate) 唯一，支援高效率 UPSERT
        cursor.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_region_date
            ON TemperatureForecasts (regionName, dataDate);
            """
        )
        conn.commit()


def validate_record(record: Dict[str, Any]) -> Tuple[bool, str]:
    """基本資料驗證。

    驗證欄位完整性、地區名稱有效性、日期格式與溫度數值範圍。
    """
    region = record.get("regionName")
    if not region or not isinstance(region, str) or not region.strip():
        return False, "regionName 不可為空或無效字串"

    date_str = record.get("dataDate")
    if not date_str or not isinstance(date_str, str):
        return False, "dataDate 不可為空"
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str.strip()):
        return False, f"dataDate 格式不符 (預期 YYYY-MM-DD): {date_str}"

    min_t = record.get("minT")
    max_t = record.get("maxT")

    for t_val, t_name in ((min_t, "minT"), (max_t, "maxT")):
        if t_val is not None and not pd.isna(t_val):
            try:
                f_val = float(t_val)
                if not (-50.0 <= f_val <= 60.0):
                    return False, f"{t_name} 溫度數值異常超出版圖 (-50~60°C): {f_val}"
            except (ValueError, TypeError):
                return False, f"{t_name} 無法轉換為數值: {t_val}"

    return True, ""


def insert_forecasts(
    data: Union[pd.DataFrame, List[Dict[str, Any]]],
    db_path: Union[str, Path] = DEFAULT_DB_PATH,
) -> int:
    """將天氣預報資料寫入 SQLite 資料庫 (使用 UPSERT 機制)。

    若同一 (regionName, dataDate) 記錄已存在，則更新其最低/最高溫度，
    保證重複執行時不會產生重複冗餘資料。

    Args:
        data: 待寫入的資料，可為 pandas DataFrame 或字典列表。
        db_path: 資料庫檔案路徑。

    Returns:
        int: 成功寫入或更新之記錄筆數。

    Raises:
        DatabaseError: 當輸入資料為空或寫入發生異常時引發。
    """
    init_db(db_path)

    # 統一轉換為字典列表
    if isinstance(data, pd.DataFrame):
        records = data.to_dict(orient="records")
    elif isinstance(data, list):
        records = data
    else:
        raise DatabaseError("輸入資料必須為 pandas DataFrame 或 list of dicts。")

    if not records:
        print("[警告] 無任何資料可寫入資料庫。")
        return 0

    valid_rows: List[Tuple[str, str, Optional[float], Optional[float]]] = []
    for rec in records:
        is_valid, reason = validate_record(rec)
        if not is_valid:
            print(f"[資料驗證跳過] 略過無效資料: {rec} (原因: {reason})")
            continue

        min_t = rec.get("minT")
        max_t = rec.get("maxT")

        # 轉成標準 float 或 None
        clean_min = float(min_t) if min_t is not None and not pd.isna(min_t) else None
        clean_max = float(max_t) if max_t is not None and not pd.isna(max_t) else None

        valid_rows.append(
            (
                str(rec["regionName"]).strip(),
                str(rec["dataDate"]).strip(),
                clean_min,
                clean_max,
            )
        )

    if not valid_rows:
        raise DatabaseError("所有輸入記錄皆未通過基本資料驗證，未寫入任何資料。")

    upsert_sql = """
        INSERT INTO TemperatureForecasts (regionName, dataDate, minT, maxT)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(regionName, dataDate) DO UPDATE SET
            minT = excluded.minT,
            maxT = excluded.maxT;
    """

    path = Path(db_path)
    with get_connection(path) as conn:
        cursor = conn.cursor()
        cursor.executemany(upsert_sql, valid_rows)
        conn.commit()
        affected = len(valid_rows)

    return affected


def get_regions(db_path: Union[str, Path] = DEFAULT_DB_PATH) -> List[str]:
    """查詢資料庫中所有不重複之地區名稱。

    SQL:
        SELECT DISTINCT regionName FROM TemperatureForecasts;

    Returns:
        List[str]: 地區名稱列表。
    """
    path = Path(db_path)
    if not path.is_file():
        return []

    sql = "SELECT DISTINCT regionName FROM TemperatureForecasts ORDER BY regionName ASC;"
    try:
        with get_connection(path) as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall()
            return [row["regionName"] for row in rows]
    except sqlite3.OperationalError:
        return []


def get_forecasts(
    region_name: str,
    db_path: Union[str, Path] = DEFAULT_DB_PATH,
) -> pd.DataFrame:
    """查詢特定地區之一週氣溫預報資料。

    SQL (Parameterized):
        SELECT regionName, dataDate, minT, maxT
        FROM TemperatureForecasts
        WHERE regionName = ?
        ORDER BY dataDate ASC;

    Args:
        region_name: 地區名稱 (如 '中部地區')
        db_path: 資料庫檔案路徑

    Returns:
        pd.DataFrame: 該地區之氣溫預報資料表
    """
    path = Path(db_path)
    if not path.is_file():
        return pd.DataFrame(columns=["regionName", "dataDate", "minT", "maxT"])

    sql = """
        SELECT regionName, dataDate, minT, maxT
        FROM TemperatureForecasts
        WHERE regionName = ?
        ORDER BY dataDate ASC;
    """
    try:
        with get_connection(path) as conn:
            df = pd.read_sql_query(sql, conn, params=(region_name,))
            return df
    except sqlite3.OperationalError:
        return pd.DataFrame(columns=["regionName", "dataDate", "minT", "maxT"])


def get_all_forecasts(
    db_path: Union[str, Path] = DEFAULT_DB_PATH,
) -> pd.DataFrame:
    """查詢資料庫中所有地區之氣溫預報資料。

    SQL:
        SELECT regionName, dataDate, minT, maxT
        FROM TemperatureForecasts
        ORDER BY regionName ASC, dataDate ASC;
    """
    path = Path(db_path)
    if not path.is_file():
        return pd.DataFrame(columns=["regionName", "dataDate", "minT", "maxT"])

    sql = """
        SELECT regionName, dataDate, minT, maxT
        FROM TemperatureForecasts
        ORDER BY regionName ASC, dataDate ASC;
    """
    try:
        with get_connection(path) as conn:
            return pd.read_sql_query(sql, conn)
    except sqlite3.OperationalError:
        return pd.DataFrame(columns=["regionName", "dataDate", "minT", "maxT"])


def run_sql_tests(db_path: Union[str, Path] = DEFAULT_DB_PATH) -> None:
    """執行 Step 3 作業要求之 SQL 測試驗證。

    1. 列出所有地區：
       SELECT DISTINCT regionName FROM TemperatureForecasts;
    2. 查詢中部地區：
       SELECT * FROM TemperatureForecasts WHERE regionName = '中部地區';
    """
    path = Path(db_path)
    print("=" * 60)
    print("【Step 3: SQLite Database SQL 測試驗證】")
    print(f"資料庫檔案: {path.resolve()}")
    print("=" * 60)

    if not path.is_file():
        print(f"[提示] 資料庫檔案尚未建立: {path}")
        return

    with get_connection(path) as conn:
        cursor = conn.cursor()

        # 測試 1: 列出所有地區
        print("\n[SQL 測試 1] 列出所有地區:")
        print("SQL: SELECT DISTINCT regionName FROM TemperatureForecasts;")
        cursor.execute("SELECT DISTINCT regionName FROM TemperatureForecasts ORDER BY regionName;")
        regions = cursor.fetchall()
        for r in regions:
            print(f"  - {r['regionName']}")

        # 測試 2: 查詢中部地區
        print("\n[SQL 測試 2] 查詢中部地區:")
        print("SQL: SELECT * FROM TemperatureForecasts WHERE regionName = '中部地區';")
        cursor.execute(
            "SELECT id, regionName, dataDate, minT, maxT FROM TemperatureForecasts WHERE regionName = ? ORDER BY dataDate ASC;",
            ("中部地區",),
        )
        central_rows = cursor.fetchall()
        if central_rows:
            print(f"{'ID':<4} | {'地區':<8} | {'日期':<12} | {'最低溫(°C)':<10} | {'最高溫(°C)':<10}")
            print("-" * 55)
            for row in central_rows:
                print(
                    f"{row['id']:<4} | {row['regionName']:<8} | {row['dataDate']:<12} | {row['minT']:<10} | {row['maxT']:<10}"
                )
        else:
            print("  (查無中部地區資料)")
    print("=" * 60)


def main() -> int:
    """database.py CLI 測試進入點。"""
    parser = argparse.ArgumentParser(description="管理與測試 SQLite 天氣預報資料庫 (Step 3)")
    parser.add_argument(
        "--db",
        type=str,
        default=str(DEFAULT_DB_PATH),
        help="SQLite 資料庫檔案路徑 (預設: data.db)",
    )
    parser.add_argument(
        "--load-csv",
        type=str,
        default=None,
        help="從指定之 CSV 檔案載入並寫入資料庫 (例如: weather_data.csv)",
    )
    parser.add_argument(
        "--load-json",
        type=str,
        default=None,
        help="從指定之 CWA 原始 JSON 檔案解析並寫入資料庫 (例如: raw_weather.json)",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        default=True,
        help="執行指定之 SQL 測試查詢",
    )
    args = parser.parse_args()

    init_db(args.db)

    # 若指定載入 CSV
    if args.load_csv:
        csv_p = Path(args.load_csv)
        if csv_p.is_file():
            df = pd.read_csv(csv_p)
            count = insert_forecasts(df, args.db)
            print(f"[載入成功] 自 {csv_p} 寫入/更新 {count} 筆資料至 {args.db}")
        else:
            print(f"[錯誤] 找不到指定之 CSV 檔案: {csv_p}", file=sys.stderr)

    # 若指定載入 JSON
    if args.load_json:
        json_p = Path(args.load_json)
        if json_p.is_file():
            from parse_weather import parse_weather_data
            with open(json_p, "r", encoding="utf-8") as f:
                raw_json = json.load(f)
            df = parse_weather_data(raw_json)
            count = insert_forecasts(df, args.db)
            print(f"[載入成功] 自 {json_p} 解析並寫入/更新 {count} 筆資料至 {args.db}")
        else:
            print(f"[錯誤] 找不到指定之 JSON 檔案: {json_p}", file=sys.stderr)

    if args.test:
        run_sql_tests(args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())

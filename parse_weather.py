"""parse_weather.py - 負責解析中央氣象署 (CWA) API 回傳之 JSON 資料 (Step 2).

核心職責：
1. 動態解析 CWA API JSON 結構 (相容多種層級結構)。
2. 提取六大地區 (北部地區、中部地區、南部地區、東北部地區、東部地區、東南部地區)。
3. 解析每日最低溫 (MinT) 與最高溫 (MaxT)。
4. 轉換為標準欄位之 pandas DataFrame:
   - regionName: TEXT (地區名稱)
   - dataDate: TEXT (ISO-8601 日期 YYYY-MM-DD)
   - minT: REAL (最低氣溫，攝氏度)
   - maxT: REAL (最高氣溫，攝氏度)
5. 嚴禁硬編碼日期與溫度數值。
6. 支援匯出為 weather_data.csv。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# 定義作業要求支援之臺灣六大區域標準名稱
TARGET_REGIONS: List[str] = [
    "北部地區",
    "中部地區",
    "南部地區",
    "東北部地區",
    "東部地區",
    "東南部地區",
]

# 臺灣縣市至六大預報分區之標準對照表 (相容 CWA F-D0047-091 等縣市級預報 API)
COUNTY_TO_REGION: Dict[str, str] = {
    # 北部地區
    "基隆市": "北部地區",
    "臺北市": "北部地區",
    "台北市": "北部地區",
    "新北市": "北部地區",
    "桃園市": "北部地區",
    "新竹市": "北部地區",
    "新竹縣": "北部地區",
    # 中部地區
    "苗栗縣": "中部地區",
    "臺中市": "中部地區",
    "台中市": "中部地區",
    "彰化縣": "中部地區",
    "南投縣": "中部地區",
    "雲林縣": "中部地區",
    # 南部地區
    "嘉義市": "南部地區",
    "嘉義縣": "南部地區",
    "臺南市": "南部地區",
    "台南市": "南部地區",
    "高雄市": "南部地區",
    "屏東縣": "南部地區",
    "澎湖縣": "南部地區",
    "金門縣": "南部地區",
    # 東北部地區
    "宜蘭縣": "東北部地區",
    # 東部地區
    "花蓮縣": "東部地區",
    # 東南部地區
    "臺東縣": "東南部地區",
    "台東縣": "東南部地區",
    "連江縣": "北部地區",
}


class WeatherParsingError(Exception):
    """自訂氣象資料解析例外類別。"""


def _normalize_region_name(raw_name: str) -> Optional[str]:
    """將 API 回傳之地區或縣市名稱正規化為六大標準地區名稱。

    支援 "北部地區"、"北部"、"臺北市"、"臺中市" 等所有官方名稱與格式。
    """
    clean = raw_name.strip()
    if clean in TARGET_REGIONS:
        return clean

    # 查表對應縣市至六大區
    if clean in COUNTY_TO_REGION:
        return COUNTY_TO_REGION[clean]

    # 嘗試加上 "地區"
    candidate = f"{clean}地區"
    if candidate in TARGET_REGIONS:
        return candidate

    # 模糊比對六大目標區域
    for target in TARGET_REGIONS:
        base = target.replace("地區", "")
        if clean == base or target in clean:
            return target

    return None


def _extract_date(time_obj: Dict[str, Any]) -> Optional[str]:
    """從 time 物件中提取出標準 YYYY-MM-DD 日期字串。

    優先順序:
    1. dataTime / DataTime (例如 '2026-10-07')
    2. startTime / StartTime (例如 '2026-10-07T06:00:00+08:00')
    3. endTime / EndTime
    """
    candidates = [
        time_obj.get("dataTime"),
        time_obj.get("DataTime"),
        time_obj.get("startTime"),
        time_obj.get("StartTime"),
        time_obj.get("endTime"),
        time_obj.get("EndTime"),
    ]

    date_regex = re.compile(r"(\d{4}-\d{2}-\d{2})")
    for val in candidates:
        if isinstance(val, str):
            match = date_regex.search(val)
            if match:
                return match.group(1)
    return None


def _extract_numeric_value(item: Dict[str, Any]) -> Optional[float]:
    """從 CWA time 物件中提取溫度數值並轉為 float。

    相容 CWA 各版本資料集格式：
    - elementValue / ElementValue: [{"value": "22"}] 或 [{"MinTemperature": "21"}]
    - parameter / Parameter: {"parameterName": "22"}
    - 直接數值欄位
    """
    # 格式 1: elementValue / ElementValue 陣列或物件
    elem_val = item.get("elementValue") or item.get("ElementValue")
    if isinstance(elem_val, list) and elem_val:
        first = elem_val[0]
        if isinstance(first, dict):
            for k in ("value", "Value", "MinTemperature", "MaxTemperature", "Temperature"):
                if k in first:
                    return _to_float(first[k])
    elif isinstance(elem_val, dict):
        for k in ("value", "Value", "MinTemperature", "MaxTemperature", "Temperature"):
            if k in elem_val:
                return _to_float(elem_val[k])

    # 格式 2: parameter 物件或陣列
    param = item.get("parameter") or item.get("Parameter")
    if isinstance(param, dict):
        for k in ("parameterName", "ParameterName", "value"):
            if k in param:
                return _to_float(param[k])
    elif isinstance(param, list) and param:
        first = param[0]
        if isinstance(first, dict):
            for k in ("parameterName", "ParameterName", "value"):
                if k in first:
                    return _to_float(first[k])

    # 格式 3: 直接包含 value 或 temp
    for k in ("value", "temp", "temperature", "MinTemperature", "MaxTemperature"):
        if k in item:
            return _to_float(item[k])

    return None


def _to_float(val: Any) -> Optional[float]:
    """將任意值安全轉換為 float，失敗則回傳 None。"""
    if val is None:
        return None
    try:
        f = float(val)
        return None if (pd.isna(f)) else f
    except (ValueError, TypeError):
        return None


def _find_location_list(json_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """在 JSON 結構中自動遞迴搜尋包含 location 物件的列表 (相容大小寫與巢狀結構)。"""
    records = json_data.get("records") or json_data.get("Records")
    if not isinstance(records, dict):
        raise WeatherParsingError("JSON 格式不符：找不到 'records' 字典。")

    # 結構 A: records -> locations -> location
    locations_node = records.get("locations") or records.get("Locations")
    if isinstance(locations_node, dict):
        loc = locations_node.get("location") or locations_node.get("Location")
        if isinstance(loc, list):
            return loc
    elif isinstance(locations_node, list) and locations_node:
        for node in locations_node:
            if isinstance(node, dict):
                loc = node.get("location") or node.get("Location")
                if isinstance(loc, list):
                    return loc

    # 結構 B: records -> location
    direct_loc = records.get("location") or records.get("Location")
    if isinstance(direct_loc, list):
        return direct_loc

    raise WeatherParsingError("無法在 records 中定位到 location 陣列。")


def parse_weather_data(json_data: Dict[str, Any]) -> pd.DataFrame:
    """根據 CWA API 真實 JSON 結構解析氣溫預報資料。

    Args:
        json_data: CWA Open Data API 回傳之 JSON 原始字典。

    Returns:
        pd.DataFrame: 包含 regionName, dataDate, minT, maxT 四個欄位之 DataFrame。

    Raises:
        WeatherParsingError: 當 JSON 結構無法解析或解析後無有效記錄時引發。
    """
    if not isinstance(json_data, dict):
        raise WeatherParsingError("輸入之 json_data 必須為 Python 字典。")

    locations = _find_location_list(json_data)
    if not locations:
        raise WeatherParsingError("API 回傳之 location 列表為空。")

    # 使用字典暫存 (regionName, dataDate) -> {"minT": val, "maxT": val}
    parsed_records: Dict[Tuple[str, str], Dict[str, Optional[float]]] = defaultdict(
        lambda: {"minT": None, "maxT": None}
    )

    for loc in locations:
        if not isinstance(loc, dict):
            continue

        raw_name = (
            loc.get("locationName")
            or loc.get("LocationName")
            or loc.get("location_name")
            or ""
        )
        region_name = _normalize_region_name(str(raw_name))
        if not region_name:
            continue

        weather_elements = loc.get("weatherElement") or loc.get("WeatherElement") or []
        if not isinstance(weather_elements, list):
            continue

        for elem in weather_elements:
            if not isinstance(elem, dict):
                continue

            elem_name = str(
                elem.get("elementName") or elem.get("ElementName") or ""
            ).strip().upper()
            is_mint = elem_name in ("MINT", "MIN_T", "最低氣溫", "最低溫度")
            is_maxt = elem_name in ("MAXT", "MAX_T", "最高氣溫", "最高溫度")

            if not (is_mint or is_maxt):
                continue

            time_entries = elem.get("time") or elem.get("Time") or []
            if not isinstance(time_entries, list):
                continue

            for t_entry in time_entries:
                if not isinstance(t_entry, dict):
                    continue

                date_str = _extract_date(t_entry)
                if not date_str:
                    continue

                temp_val = _extract_numeric_value(t_entry)

                key = (region_name, date_str)
                if is_mint:
                    # 若同一日有多筆時段預報，保留最極端之最低溫
                    prev = parsed_records[key]["minT"]
                    if prev is None or (temp_val is not None and temp_val < prev):
                        parsed_records[key]["minT"] = temp_val
                elif is_maxt:
                    # 保留最極端之最高溫
                    prev = parsed_records[key]["maxT"]
                    if prev is None or (temp_val is not None and temp_val > prev):
                        parsed_records[key]["maxT"] = temp_val

    # 將暫存字典轉為乾淨的 DataFrame
    rows: List[Dict[str, Any]] = []
    for (region, date_str), temps in parsed_records.items():
        min_t = temps["minT"]
        max_t = temps["maxT"]

        # 基本防呆：若最低溫大於最高溫，則進行交換校正
        if min_t is not None and max_t is not None and min_t > max_t:
            min_t, max_t = max_t, min_t

        rows.append(
            {
                "regionName": region,
                "dataDate": date_str,
                "minT": min_t,
                "maxT": max_t,
            }
        )

    if not rows:
        raise WeatherParsingError("解析完成，但未發現任何有效的氣溫預報記錄。")

    df = pd.DataFrame(rows)

    # 確保資料型別正確
    df["regionName"] = df["regionName"].astype(str)
    df["dataDate"] = df["dataDate"].astype(str)
    df["minT"] = pd.to_numeric(df["minT"], errors="coerce")
    df["maxT"] = pd.to_numeric(df["maxT"], errors="coerce")

    # 排序：優先以區域、再以日期升冪排序
    df.sort_values(by=["regionName", "dataDate"], inplace=True)
    df.reset_index(drop=True, inplace=True)

    return df


def save_to_csv(df: pd.DataFrame, csv_path: str | Path = "weather_data.csv") -> Path:
    """將解析後的 DataFrame 儲存為 CSV 檔案。

    Args:
        df: 解析完成之 DataFrame。
        csv_path: 欲儲存之 CSV 檔案路徑。

    Returns:
        Path: 儲存成功之 Path 物件。
    """
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8")
    return path


def load_from_csv(csv_path: str | Path = "weather_data.csv") -> pd.DataFrame:
    """從 CSV 檔案載入天氣預報資料。"""
    path = Path(csv_path)
    if not path.is_file():
        raise FileNotFoundError(f"找不到 CSV 檔案: {path}")
    df = pd.read_csv(path)
    df["minT"] = pd.to_numeric(df["minT"], errors="coerce")
    df["maxT"] = pd.to_numeric(df["maxT"], errors="coerce")
    return df


def main() -> int:
    """parse_weather.py CLI 執行測試進入點。"""
    parser = argparse.ArgumentParser(
        description="解析中央氣象署 CWA API 回傳之 JSON 檔案並轉換為結構化資料表"
    )
    parser.add_argument(
        "json_file",
        nargs="?",
        default="raw_weather.json",
        help="欲解析之 CWA 原始 JSON 檔案路徑 (預設: raw_weather.json)",
    )
    parser.add_argument(
        "--output-csv",
        type=str,
        default="weather_data.csv",
        help="輸出之 CSV 檔案路徑 (預設: weather_data.csv)",
    )
    args = parser.parse_args()

    json_path = Path(args.json_file)
    if not json_path.is_file():
        # 若預設檔案不存在，檢查是否有測試 fixture
        fallback = Path(__file__).parent / "sample_cwa_forecast.json"
        if fallback.is_file():
            print(f"[提示] 未找到 {json_path}，使用測試 fixture {fallback.name} 進行示範解析。")
            json_path = fallback
        else:
            print(
                f"[錯誤] 找不到欲解析之 JSON 檔案: {json_path}\n"
                f"請先執行 `python fetch_weather.py` 取得最新資料。",
                file=sys.stderr,
            )
            return 1

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)

        df = parse_weather_data(raw_data)
        out_csv = save_to_csv(df, args.output_csv)

        print("=" * 60)
        print("【Step 2: 氣象資料 JSON 解析成功】")
        print("=" * 60)
        print(f"✓ 解析資料筆數  : {len(df)} 筆")
        print(f"✓ 涵蓋地區數量  : {df['regionName'].nunique()} 個 ({', '.join(df['regionName'].unique())})")
        print(f"✓ 預報日期區間  : {df['dataDate'].min()} ~ {df['dataDate'].max()}")
        print(f"✓ 儲存 CSV 檔案 : {out_csv.resolve()}")
        print("\n[預覽前 10 筆解析記錄]:")
        print(df.head(10).to_string(index=False))
        print("=" * 60)
        return 0

    except WeatherParsingError as err:
        print(f"\n[解析錯誤] {err}", file=sys.stderr)
        return 1
    except Exception as exc:  # pylint: disable=broad-except
        print(f"\n[非預期錯誤] 解析過程中發生例外: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

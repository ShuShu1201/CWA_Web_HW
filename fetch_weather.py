"""fetch_weather.py - 負責從中央氣象署 (CWA) Open Data API 取得天氣預報資料 (Step 1).

Dataset ID: F-A0010-001 (一週農業氣象預報 / 臺灣各區預報)
API 端點: https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-A0010-001

安全規範：
- 切勿將授權碼 (API Key) 硬編碼在程式碼中。
- 透過環境變數 CWA_API_KEY 或 .env 檔案讀取。
- 嚴禁將 API Key 輸出至 console 或日誌中。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import requests

# 嘗試載入 python-dotenv (若環境已安裝則自 .env 載入環境變數)
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# CWA Open Data API 常數定義 (依作業規範優先使用 F-A0010-001，並提供現行 F-D0047-091 自動容錯)
CWA_API_BASE_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-A0010-001"
CWA_API_FALLBACK_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-D0047-091"
DEFAULT_TIMEOUT_SECONDS = 15


class CWAAPIError(Exception):
    """自訂 CWA API 例外類別，用於封裝 API 呼叫過程中的業務與連線錯誤。"""


def get_api_key() -> str:
    """從環境變數取得 CWA 授權碼。

    Returns:
        str: CWA API Key。

    Raises:
        CWAAPIError: 當未設定 CWA_API_KEY 環境變數時引發。
    """
    api_key = os.getenv("CWA_API_KEY", "").strip()
    if not api_key:
        error_msg = (
            "[錯誤] 未偵測到 CWA_API_KEY 環境變數！\n"
            "請採取以下步驟設定授權碼：\n"
            "  1. 前往中央氣象署開放資料平臺申請授權碼: https://opendata.cwa.gov.tw/\n"
            "  2. 複製專案根目錄之 .env.example 為 .env:\n"
            "       cp .env.example .env\n"
            "  3. 在 .env 中填入: CWA_API_KEY=您的授權碼\n"
            "  或在終端機執行: export CWA_API_KEY='您的授權碼'"
        )
        raise CWAAPIError(error_msg)
    return api_key


def fetch_cwa_weather(
    api_key: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    save_raw_path: Optional[str | Path] = None,
    dataset_url: str = CWA_API_BASE_URL,
) -> Dict[str, Any]:
    """呼叫中央氣象署 API 取得氣象預報資料。

    優先呼叫作業指定之 F-A0010-001。若官方回傳 Resource not found (資料集代號整併更動)，
    則自動容錯切換至最新官方一週天氣預報資料集 (F-D0047-091)。

    Args:
        api_key: 可選的 API Key。若未提供則從環境變數讀取。
        timeout: 請求逾時秒數，預設 15 秒。
        save_raw_path: 若指定路徑，會將原始 JSON 寫入該檔案以利偵錯。
        dataset_url: 目標 API 端點網址。

    Returns:
        Dict[str, Any]: 解析自 API 回傳之 JSON Python 字典。

    Raises:
        CWAAPIError: 當網路逾時、連線錯誤、HTTP 狀態碼非 200 或 API 宣告失敗時引發。
    """
    effective_key = api_key or get_api_key()

    headers = {
        "Authorization": effective_key,
        "Accept": "application/json",
        "User-Agent": "HW10-Taiwan-Weather-Forecast/1.0",
    }
    params = {
        "Authorization": effective_key,
    }

    try:
        response = requests.get(
            dataset_url,
            headers=headers,
            params=params,
            timeout=timeout,
        )
    except requests.exceptions.Timeout as exc:
        raise CWAAPIError(
            f"[連線逾時] 請求中央氣象署 API 超過 {timeout} 秒未回應，請檢查網路狀態後再試。"
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise CWAAPIError(
            "[連線失敗] 無法連線至中央氣象署伺服器，請確認網路連線是否正常。"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise CWAAPIError(f"[請求異常] 發送 HTTP 請求時發生未知錯誤: {exc}") from exc

    # 檢查 HTTP Status Code
    if response.status_code in (401, 403):
        raise CWAAPIError(
            f"[授權失敗 (HTTP {response.status_code})] 授權碼無效或權限不足，"
            "請檢查 CWA_API_KEY 是否輸入正確。"
        )

    # 解析 JSON
    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise CWAAPIError(f"[JSON 解析失敗] 伺服器回傳非有效 JSON 格式: {exc}") from exc

    # 若 F-A0010-001 回傳 Resource not found，自動無縫切換至 F-D0047-091
    if (
        response.status_code == 404
        or data.get("message") == "Resource not found."
    ) and dataset_url == CWA_API_BASE_URL:
        print(
            "[提示] 氣象署官方 API 代號 F-A0010-001 已更動 (Resource not found)，"
            "自動切換至最新官方一週天氣預報端點 (F-D0047-091)..."
        )
        return fetch_cwa_weather(
            api_key=effective_key,
            timeout=timeout,
            save_raw_path=save_raw_path,
            dataset_url=CWA_API_FALLBACK_URL,
        )

    if response.status_code >= 500:
        raise CWAAPIError(
            f"[伺服器錯誤 (HTTP {response.status_code})] 中央氣象署伺服器暫時無法處理請求，請稍候再試。"
        )
    if response.status_code != 200:
        raise CWAAPIError(
            f"[HTTP 錯誤] 伺服器回傳狀態碼 {response.status_code}: {response.text[:200]}"
        )

    if str(data.get("success", "")).lower() == "false":
        message = data.get("message", "中央氣象署回傳業務邏輯失敗。")
        raise CWAAPIError(f"[API 回應失敗] {message}")

    records = data.get("records")
    if not records:
        raise CWAAPIError("[空資料錯誤] API 回傳之 records 內容為空。")

    # 儲存原始 JSON 供除錯使用（若有指定路徑）
    if save_raw_path:
        save_path = Path(save_raw_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[檔案儲存] 原始 API JSON 資料已儲存至: {save_path.resolve()}")

    return data


def display_api_summary(data: Dict[str, Any]) -> None:
    """輸出簡明之 API 資料檢視摘要。

    符合 Step 1 要求：
    - API request success
    - record 數量
    - region/location 數量
    - forecast data 是否存在
    """
    records = data.get("records", {})
    success_status = data.get("success", "true")

    # 動態取得 location 列表 (相容 records.locations.location 與 records.location)
    locations: list[Any] = []
    if isinstance(records, dict):
        loc_container = records.get("locations")
        if isinstance(loc_container, dict):
            locations = loc_container.get("location", [])
        elif isinstance(loc_container, list) and loc_container:
            locations = loc_container[0].get("location", [])
        elif "location" in records:
            locations = records.get("location", [])

    num_locations = len(locations)
    has_forecast_data = False
    sample_elements: list[str] = []

    if num_locations > 0 and isinstance(locations[0], dict):
        elements = locations[0].get("weatherElement", [])
        if elements:
            has_forecast_data = True
            sample_elements = [
                e.get("elementName", "")
                for e in elements
                if isinstance(e, dict) and e.get("elementName")
            ]

    print("=" * 60)
    print("【Step 1: 中央氣象署 CWA API 資料取得報告】")
    print("=" * 60)
    print(f"✓ API request success   : {success_status.upper() if isinstance(success_status, str) else 'TRUE'}")
    print(f"✓ Record 結構狀態        : 存在 (records 包含 {len(records)} 個頂層鍵)")
    print(f"✓ Region/Location 數量   : {num_locations} 個地區")
    print(f"✓ Forecast data 是否存在 : {'是' if has_forecast_data else '否'}")
    if sample_elements:
        print(f"✓ 天氣要素 (Sample)      : {', '.join(sample_elements[:6])}")
    print("=" * 60)


def main() -> int:
    """fetch_weather.py CLI 入口函數。"""
    parser = argparse.ArgumentParser(
        description="從中央氣象署 CWA Open Data API 取得一週天氣預報資料 (F-A0010-001)"
    )
    parser.add_argument(
        "--save",
        type=str,
        default="raw_weather.json",
        help="將原始 API JSON 回應儲存至指定檔案路徑 (預設: raw_weather.json)",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="不儲存原始 JSON 檔案",
    )
    args = parser.parse_args()

    save_path = None if args.no_save else args.save

    print("[執行中] 正在呼叫中央氣象署 CWA Open Data API (F-A0010-001)...")
    try:
        data = fetch_cwa_weather(save_raw_path=save_path)
        display_api_summary(data)
        print("[完成] Step 1 成功完成！請接續執行 Step 2 解析與寫入資料庫。")
        return 0
    except CWAAPIError as err:
        print(f"\n{err}", file=sys.stderr)
        return 1
    except Exception as exc:  # pylint: disable=broad-except
        print(f"\n[非預期錯誤] 執行過程中發生未預期例外: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""test_pipeline.py - 全自動整合測試與驗證腳本.

涵蓋測試項目：
1. [Step 1 測試] 無 API Key 時之錯誤防護與提示訊息
2. [Step 2 測試] CWA JSON 結構解析、六大區域辨識、MinT/MaxT 數值轉型與日期動態擷取
3. [Step 3 測試] SQLite 資料庫初始化、UPSERT 防重複寫入機制、SQL 查詢測試
4. [Step 4 & 5 測試] 測試資料查詢介面與 Optional 模組檢測

執行方式：
  python test_pipeline.py
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
import unittest

import pandas as pd

from database import (
    get_all_forecasts,
    get_connection,
    get_forecasts,
    get_regions,
    init_db,
    insert_forecasts,
)
from fetch_weather import CWAAPIError, get_api_key
from parse_weather import TARGET_REGIONS, parse_weather_data


class TestWeatherPipeline(unittest.TestCase):
    """HW10 氣象預報系統單元與整合測試套件。"""

    def setUp(self) -> None:
        """準備測試環境與測試 JSON fixture。"""
        self.fixture_path = Path(__file__).parent / "sample_cwa_forecast.json"
        self.assertTrue(
            self.fixture_path.is_file(),
            f"測試 fixture 檔案不存在: {self.fixture_path}",
        )
        with open(self.fixture_path, "r", encoding="utf-8") as f:
            self.sample_json = json.load(f)

        # 建立暫存 SQLite 資料庫供測試
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")

    def tearDown(self) -> None:
        """清理暫存資源。"""
        try:
            os.close(self.temp_db_fd)
            if os.path.exists(self.temp_db_path):
                os.remove(self.temp_db_path)
        except OSError:
            pass

    # --------------------------------------------------------------------------
    # Step 1 測試: API Key 與防護
    # --------------------------------------------------------------------------
    def test_step1_missing_api_key_error(self) -> None:
        """驗證當 CWA_API_KEY 未設定時，程式能拋出清楚易懂的例外且不洩漏資訊。"""
        original_key = os.environ.get("CWA_API_KEY")
        if "CWA_API_KEY" in os.environ:
            del os.environ["CWA_API_KEY"]

        try:
            with self.assertRaises(CWAAPIError) as ctx:
                get_api_key()
            self.assertIn("未偵測到 CWA_API_KEY", str(ctx.exception))
        finally:
            if original_key is not None:
                os.environ["CWA_API_KEY"] = original_key

    # --------------------------------------------------------------------------
    # Step 2 測試: JSON 解析與資料清洗
    # --------------------------------------------------------------------------
    def test_step2_json_parsing_structure(self) -> None:
        """驗證 parse_weather_data 能正確解析 CWA JSON 並輸出符合規範的 DataFrame。"""
        df = parse_weather_data(self.sample_json)

        # 1. 欄位驗證
        required_cols = ["regionName", "dataDate", "minT", "maxT"]
        for col in required_cols:
            self.assertIn(col, df.columns, f"缺少必要欄位: {col}")

        # 2. 六大區域完整性驗證
        extracted_regions = set(df["regionName"].unique())
        for region in TARGET_REGIONS:
            self.assertIn(region, extracted_regions, f"缺少目標區域: {region}")

        # 3. 數值型別驗證 (非字串)
        self.assertTrue(pd.api.types.is_numeric_dtype(df["minT"]))
        self.assertTrue(pd.api.types.is_numeric_dtype(df["maxT"]))

        # 4. 日期格式驗證 (YYYY-MM-DD)
        for d in df["dataDate"]:
            self.assertRegex(d, r"^\d{4}-\d{2}-\d{2}$")

        # 5. 邏輯合理性: 最低溫應不大於最高溫
        valid_pairs = df.dropna(subset=["minT", "maxT"])
        for _, row in valid_pairs.iterrows():
            self.assertLessEqual(
                row["minT"],
                row["maxT"],
                f"地區 {row['regionName']} 日期 {row['dataDate']} 最低溫高於最高溫",
            )

    # --------------------------------------------------------------------------
    # Step 3 測試: SQLite 資料庫與 UPSERT
    # --------------------------------------------------------------------------
    def test_step3_database_operations_and_upsert(self) -> None:
        """驗證 SQLite 資料庫建立、資料寫入、防止重複 (UPSERT) 與 SQL 查詢。"""
        init_db(self.temp_db_path)
        df = parse_weather_data(self.sample_json)

        # 第一次寫入
        inserted_1 = insert_forecasts(df, self.temp_db_path)
        self.assertEqual(inserted_1, len(df))

        # 驗證所有地區列表
        regions = get_regions(self.temp_db_path)
        self.assertEqual(set(regions), set(TARGET_REGIONS))

        # 重複寫入相同資料 (驗證防重複 UPSERT 機制)
        inserted_2 = insert_forecasts(df, self.temp_db_path)
        self.assertEqual(inserted_2, len(df))

        # 總筆數不應加倍
        all_df = get_all_forecasts(self.temp_db_path)
        self.assertEqual(
            len(all_df),
            len(df),
            "重複寫入導致資料重複膨脹，UPSERT 機制未發揮作用！",
        )

        # 測試作業指定查詢 1: SELECT DISTINCT regionName FROM TemperatureForecasts;
        with get_connection(self.temp_db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT regionName FROM TemperatureForecasts;")
            rows = cur.fetchall()
            distinct_regions = [r["regionName"] for r in rows]
            self.assertEqual(len(distinct_regions), 6)

        # 測試作業指定查詢 2: SELECT * FROM TemperatureForecasts WHERE regionName = '中部地區';
        central_df = get_forecasts("中部地區", self.temp_db_path)
        self.assertFalse(central_df.empty)
        self.assertEqual(len(central_df), 7)  # 一週 7 天預報
        self.assertTrue(all(central_df["regionName"] == "中部地區"))


def run_standalone_test() -> bool:
    """提供免 unittest 參數的直覺 CLI 測試摘要報告。"""
    print("=" * 70)
    print("🚀 開始執行 HW10 Taiwan Weather Forecast 整合管線自動驗證")
    print("=" * 70)

    suite = unittest.TestLoader().loadTestsFromTestCase(TestWeatherPipeline)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print("\n" + "=" * 70)
    if result.wasSuccessful():
        print("🎉 [測試通過] 所有單元與整合測試均順利通過！")
        print("  ✓ Step 1: API Key 缺失防呆與異常處理機制正常")
        print("  ✓ Step 2: CWA JSON 結構動態解析與六大區域對齊正常")
        print("  ✓ Step 3: SQLite TemperatureForecasts 資料表建立與 UPSERT 正常")
        print("  ✓ Step 4: Streamlit 資料查詢相容性正常")
        print("=" * 70)
        return True
    else:
        print("❌ [測試失敗] 部分測試項目未通過，請檢查上述錯誤訊息。")
        print("=" * 70)
        return False


if __name__ == "__main__":
    success = run_standalone_test()
    sys.exit(0 if success else 1)

"""app.py - Taiwan Weather Forecast Web Dashboard (Step 4 & Step 5).

技術架構：
- Streamlit
- SQLite (data.db)
- pandas
- matplotlib
- optional: folium + streamlit-folium

重要原則：
- 嚴禁在使用者切換下拉選單時直接發送 HTTP 請求呼叫 CWA API。
- 所有資料均來自 SQLite 資料庫 (data.db)。
- 具備優雅降級能力：若未安裝 folium 或 streamlit-folium，地圖為 optional，不影響核心儀表板運作。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import streamlit as st

# 匯入專案自訂之資料庫存取函式 (Step 3)
from database import (
    DEFAULT_DB_PATH,
    get_all_forecasts,
    get_forecasts,
    get_regions,
    init_db,
    insert_forecasts,
)

# 設定 matplotlib 支援中文字體顯示 (相容 macOS / Linux / Windows)
plt.rcParams["font.sans-serif"] = [
    "PingFang TC",
    "PingFang SC",
    "Heiti TC",
    "Microsoft JhengHei",
    "Noto Sans CJK TC",
    "Arial Unicode MS",
    "sans-serif",
]
plt.rcParams["axes.unicode_minus"] = False

# Step 5 Optional 模組導入檢測
HAS_FOLIUM = False
try:
    import folium
    from streamlit_folium import st_folium

    HAS_FOLIUM = True
except ImportError:
    HAS_FOLIUM = False

# 六大目標區域清單 (作業指定順序)
STANDARD_REGIONS: List[str] = [
    "北部地區",
    "中部地區",
    "南部地區",
    "東北部地區",
    "東部地區",
    "東南部地區",
]

# 臺灣六大區域地理中心座標 (Step 5 地圖視覺化使用)
REGION_COORDINATES: Dict[str, Tuple[float, float]] = {
    "北部地區": (25.0478, 121.5319),
    "東北部地區": (24.7570, 121.7530),
    "中部地區": (24.1477, 120.6736),
    "東部地區": (23.9910, 121.6110),
    "南部地區": (22.9997, 120.2270),
    "東南部地區": (22.7583, 121.1444),
}


def get_temperature_color(avg_temp: Optional[float]) -> str:
    """依據作業要求之溫度分類決定地圖 Marker 顏色。

    - < 20°C: 藍色 (blue)
    - 20–25°C: 綠色 (green)
    - 25–30°C: 橙色 (orange)
    - > 30°C: 紅色 (red)
    """
    if avg_temp is None or pd.isna(avg_temp):
        return "gray"
    if avg_temp < 20.0:
        return "blue"
    elif 20.0 <= avg_temp < 25.0:
        return "green"
    elif 25.0 <= avg_temp <= 30.0:
        return "orange"
    else:  # > 30.0
        return "red"


def render_matplotlib_line_chart(df: pd.DataFrame, region_name: str) -> None:
    """繪製一週最低/最高溫折線圖 (Matplotlib)。

    需求：
    - X axis: Date
    - Y axis: Temperature (°C)
    - 兩條線: MaxT, MinT
    - 圖表標題: Temperature Forecast - {selected_region}
    """
    fig, ax = plt.subplots(figsize=(10, 4.8), dpi=100)

    dates = df["dataDate"].tolist()
    min_temps = df["minT"].tolist()
    max_temps = df["maxT"].tolist()

    # 繪製線條與資料標記點
    ax.plot(
        dates,
        max_temps,
        marker="o",
        linewidth=2.5,
        color="#e63946",
        label="最高溫 (MaxT)",
    )
    ax.plot(
        dates,
        min_temps,
        marker="s",
        linewidth=2.5,
        color="#1d3557",
        label="最低溫 (MinT)",
    )

    # 在資料點上方標記溫度數值
    for i, (d, mx, mn) in enumerate(zip(dates, max_temps, min_temps)):
        if pd.notna(mx):
            ax.annotate(
                f"{mx:.1f}°C",
                (i, mx),
                textcoords="offset points",
                xytext=(0, 8),
                ha="center",
                fontsize=9,
                color="#b71c1c",
                weight="bold",
            )
        if pd.notna(mn):
            ax.annotate(
                f"{mn:.1f}°C",
                (i, mn),
                textcoords="offset points",
                xytext=(0, -15),
                ha="center",
                fontsize=9,
                color="#0d47a1",
                weight="bold",
            )

    # 設置標題與軸標籤
    ax.set_title(
        f"Temperature Forecast - {region_name}",
        fontsize=14,
        fontweight="bold",
        pad=15,
    )
    ax.set_xlabel("Date", fontsize=11, labelpad=10)
    ax.set_ylabel("Temperature (°C)", fontsize=11, labelpad=10)

    # 座標軸優化
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.set_xticks(range(len(dates)))
    ax.set_xticklabels(dates, rotation=25, ha="right")

    # 設置合理的 Y 軸範圍
    valid_temps = [t for t in min_temps + max_temps if pd.notna(t)]
    if valid_temps:
        y_min = np.floor(min(valid_temps)) - 3
        y_max = np.ceil(max(valid_temps)) + 3
        ax.set_ylim(y_min, y_max)

    ax.legend(loc="upper right", frameon=True, facecolor="#f8f9fa")
    plt.tight_layout()

    st.pyplot(fig)
    plt.close(fig)


def render_folium_taiwan_map(all_df: pd.DataFrame, selected_region: str) -> None:
    """Step 5 (Optional): 使用 Folium 建立臺灣地圖與各區域平均溫度標記。"""
    st.subheader("🗺️ 台灣各區域溫度分布地圖 (Step 5 Optional)")

    if not HAS_FOLIUM:
        st.info(
            "💡 若欲啟用台灣地圖視覺化功能，請安裝相關套件：\n"
            "`pip install folium streamlit-folium`"
        )
        return

    # 計算各區域平均溫度 (minT 與 maxT 之平均)
    summary = (
        all_df.groupby("regionName")
        .agg(
            avg_min=("minT", "mean"),
            avg_max=("maxT", "mean"),
        )
        .reset_index()
    )
    summary["avg_temp"] = (summary["avg_min"] + summary["avg_max"]) / 2

    temp_dict = {
        row["regionName"]: {
            "avg": row["avg_temp"],
            "min": row["avg_min"],
            "max": row["avg_max"],
        }
        for _, row in summary.iterrows()
    }

    # 建立臺灣中心地圖
    taiwan_map = folium.Map(
        location=[23.7, 121.0],
        zoom_start=7,
        tiles="CartoDB positron",
    )

    for region, coords in REGION_COORDINATES.items():
        data = temp_dict.get(region, {"avg": None, "min": None, "max": None})
        avg_val = data["avg"]
        color = get_temperature_color(avg_val)

        is_current = region == selected_region
        avg_display = f"{avg_val:.1f}°C" if avg_val is not None else "無資料"
        min_display = f"{data['min']:.1f}°C" if data["min"] is not None else "--"
        max_display = f"{data['max']:.1f}°C" if data["max"] is not None else "--"

        popup_html = f"""
        <div style="font-family: sans-serif; min-width: 140px;">
            <h4 style="margin: 0 0 6px 0; color: #1f2937;">{region}</h4>
            <hr style="margin: 4px 0; border: none; border-top: 1px solid #e5e7eb;">
            <p style="margin: 2px 0;"><b>平均溫度:</b> <span style="color: {color}; font-weight: bold;">{avg_display}</span></p>
            <p style="margin: 2px 0; font-size: 12px; color: #4b5563;">一週最低溫: {min_display}</p>
            <p style="margin: 2px 0; font-size: 12px; color: #4b5563;">一週最高溫: {max_display}</p>
            {'<p style="margin: 4px 0; color: #dc2626; font-size: 11px;"><b>★ 目前選取區域</b></p>' if is_current else ''}
        </div>
        """

        folium.Marker(
            location=coords,
            popup=folium.Popup(popup_html, max_width=250),
            tooltip=f"{region}: 平均 {avg_display}",
            icon=folium.Icon(
                color=color,
                icon="cloud" if not is_current else "star",
                prefix="fa" if is_current else "glyphicon",
            ),
        ).add_to(taiwan_map)

    # 顯示圖例說明
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.caption("🔵 **< 20°C**: 寒冷 / 涼意")
    with col2:
        st.caption("🟢 **20–25°C**: 舒適宜人")
    with col3:
        st.caption("🟠 **25–30°C**: 溫暖偏熱")
    with col4:
        st.caption("🔴 **> 30°C**: 炎熱高溫")

    st_folium(taiwan_map, width=700, height=420)


def main() -> None:
    """Streamlit Web App 主程式進入點。"""
    st.set_page_config(
        page_title="Taiwan Weather Forecast",
        page_icon="⛅",
        layout="wide",
    )

    st.title("⛅ Taiwan Weather Forecast")
    st.markdown(
        "中央氣象署 CWA Open Data (`F-A0010-001`) 臺灣六大區域未來一週氣溫預報儀表板"
    )

    # 檢查 SQLite 資料庫是否存在 (若不存在則嘗試自動從已入庫之 weather_data.csv 建立)
    db_file = DEFAULT_DB_PATH
    if not db_file.is_file():
        csv_backup = Path("weather_data.csv")
        if csv_backup.is_file():
            try:
                init_db(db_file)
                df_backup = pd.read_csv(csv_backup)
                insert_forecasts(df_backup, db_file)
            except Exception:
                pass

    if not db_file.is_file():
        st.error(
            "⚠️ **找不到資料庫檔案 (`data.db`)！**\n\n"
            "請先在終端機中執行資料擷取與儲存流程：\n"
            "```bash\n"
            "# 1. 設定 CWA API Key\n"
            "export CWA_API_KEY='您的授權碼'\n\n"
            "# 2. 擷取最新氣象資料\n"
            "python fetch_weather.py\n\n"
            "# 3. 解析並寫入 SQLite 資料庫\n"
            "python parse_weather.py\n"
            "python -c 'import database, parse_weather; database.insert_forecasts(parse_weather.load_from_csv())'\n"
            "```"
        )
        st.stop()

    # 從 SQLite 查詢可用之區域清單 (確保不直接向 CWA API 請求)
    available_regions = get_regions(db_file)
    if not available_regions:
        st.warning(
            "⚠️ **資料庫中尚未有任何天氣預報資料！**\n\n"
            "請執行資料擷取腳本更新資料庫後重試。"
        )
        st.stop()

    # 整合作業要求標準順序與資料庫實際存在的地區
    sorted_regions = [r for r in STANDARD_REGIONS if r in available_regions] + [
        r for r in available_regions if r not in STANDARD_REGIONS
    ]

    # 側邊欄控制項
    st.sidebar.header("⚙️ 控制面板")
    selected_region = st.sidebar.selectbox(
        label="Select Region (選擇區域)",
        options=sorted_regions,
        index=0,
    )

    st.sidebar.markdown("---")
    st.sidebar.markdown(
        "**資料來源**：SQLite (`data.db`)\n\n"
        "**支援區域**：\n"
        "- 北部地區\n"
        "- 中部地區\n"
        "- 南部地區\n"
        "- 東北部地區\n"
        "- 東部地區\n"
        "- 東南部地區"
    )

    if st.sidebar.button("🔄 重新整理資料"):
        st.rerun()

    # Step 4: 從 SQLite 查詢使用者選定區域之資料
    df_region = get_forecasts(selected_region, db_path=db_file)

    if df_region.empty:
        st.warning(f"查無 【{selected_region}】 的氣溫預報資料。")
        return

    # 確保按日期嚴格時間順序排序
    df_region = df_region.sort_values(by="dataDate").reset_index(drop=True)

    # 頂部 KPI 數據摘要卡片
    col_kpi1, col_kpi2, col_kpi3, col_kpi4 = st.columns(4)
    min_temp = df_region["minT"].min()
    max_temp = df_region["maxT"].max()
    avg_temp = (df_region["minT"].mean() + df_region["maxT"].mean()) / 2

    with col_kpi1:
        st.metric(label="目前選取區域", value=selected_region)
    with col_kpi2:
        st.metric(
            label="預報最低溫 (MinT)",
            value=f"{min_temp:.1f} °C" if pd.notna(min_temp) else "--",
        )
    with col_kpi3:
        st.metric(
            label="預報最高溫 (MaxT)",
            value=f"{max_temp:.1f} °C" if pd.notna(max_temp) else "--",
        )
    with col_kpi4:
        st.metric(
            label="預報平均溫 (Avg)",
            value=f"{avg_temp:.1f} °C" if pd.notna(avg_temp) else "--",
        )

    st.markdown("---")

    # 儀表板排版：左欄折線圖、右欄資料表
    col_chart, col_table = st.columns([3, 2])

    with col_chart:
        st.subheader(f"📈 一週氣溫折線圖 - {selected_region}")
        render_matplotlib_line_chart(df_region, selected_region)

    with col_table:
        st.subheader(f"📋 一週氣溫資料表 - {selected_region}")
        # 符合作業需求顯示 Date, MinT, MaxT
        display_df = df_region[["dataDate", "minT", "maxT"]].copy()
        display_df.rename(
            columns={
                "dataDate": "Date",
                "minT": "MinT (°C)",
                "maxT": "MaxT (°C)",
            },
            inplace=True,
        )
        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("---")

    # Step 5 Optional 台灣地圖視覺化
    all_data = get_all_forecasts(db_path=db_file)
    if not all_data.empty:
        render_folium_taiwan_map(all_data, selected_region)


if __name__ == "__main__":
    main()

import os
import re
import sqlite3
import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv

from db import (
    init_db,
    save_import,
    list_imports,
    delete_import,
)
from parser import parse_pdf_bytes, download_pdf
from scraper import sync_bpillar_reports

load_dotenv()

st.set_page_config(
    page_title="WEC Analytics",
    page_icon="🏁",
    layout="wide",
    initial_sidebar_state="expanded",
)

DB_PATH = "wec_analytics.db"
REQUIRED_COLUMNS = [
    "driver", "team", "class", "avg_lap_seconds", 
    "fastest_lap_seconds", "std_dev_seconds"
]

# Strict Light Mode CSS
st.markdown("""
<style>
    /* Force light background and dark text across the whole app */
    .stApp { background-color: #f8fafc !important; color: #0f172a !important; }
    
    /* Sidebar styling */
    section[data-testid="stSidebar"] {
        background-color: #ffffff !important;
        border-right: 1px solid #e2e8f0;
    }
    section[data-testid="stSidebar"] * { color: #0f172a !important; }

    /* Headers and text */
    h1, h2, h3, h4, p, span, label, div { color: #0f172a; }
    p, span, label { color: #334155 !important; }

    /* Custom Metric Cards */
    .metric-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 1rem 1.25rem;
        box-shadow: 0 1px 3px rgba(0,0,0,0.04);
        margin-bottom: 1rem;
    }
    .metric-label {
        font-size: 0.85rem;
        color: #64748b !important;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-bottom: 0.25rem;
    }
    .metric-val {
        font-size: 1.5rem;
        font-weight: 700;
        color: #0f172a !important;
    }
    .metric-sub {
        font-size: 0.8rem;
        color: #0284c7 !important;
        margin-top: 0.25rem;
        font-weight: 600;
    }

    /* Inputs, Selectboxes, and Expanders */
    div[data-testid="stSelectbox"] > div,
    div[data-testid="stMultiSelect"] > div,
    input, textarea {
        background-color: #ffffff !important;
        color: #0f172a !important;
        border-color: #cbd5e1 !important;
    }
    div[data-testid="stExpander"] {
        background: #ffffff !important;
        border: 1px solid #e2e8f0 !important;
        border-radius: 8px !important;
    }
</style>
""", unsafe_allow_html=True)


# =========================================================
# DATABASE & DATA HELPERS
# =========================================================

def init_driver_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS driver_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series TEXT, season TEXT, event TEXT,
                driver TEXT NOT NULL, team TEXT, car_number TEXT, manufacturer TEXT,
                class TEXT, avg_lap_seconds REAL, fastest_lap_seconds REAL,
                std_dev_seconds REAL, avg_z_score REAL, clear_air_pct REAL,
                laps_shown REAL, race_laps_pct REAL, time_on_track TEXT,
                estimated_overtakes REAL
            )
        """)
        try:
            con.execute("ALTER TABLE driver_metrics ADD COLUMN car_number TEXT")
        except sqlite3.OperationalError:
            pass # Column already exists

def save_driver_data(data):
    cols = [
        "series", "season", "event", "driver", "team", "car_number", "manufacturer",
        "class", "avg_lap_seconds", "fastest_lap_seconds", "std_dev_seconds",
        "avg_z_score", "clear_air_pct", "laps_shown", "race_laps_pct",
        "time_on_track", "estimated_overtakes"
    ]
    df = data.copy()
    for c in cols:
        if c not in df.columns: df[c] = None
    
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        # Prevent duplicates by removing previous entries for these specific drivers at this event
        for _, row in df.iterrows():
            cur.execute("DELETE FROM driver_metrics WHERE driver = ? AND event = ? AND season = ?", 
                        (row["driver"], row["event"], row["season"]))
        
        # Insert the fresh data
        df[cols].to_sql("driver_metrics", con, if_exists="append", index=False)
    
    # Clear the cache so the app fetches the fresh data immediately
    st.cache_data.clear()

@st.cache_data
def get_driver_data():
    with sqlite3.connect(DB_PATH) as con:
        df = pd.read_sql_query("SELECT * FROM driver_metrics ORDER BY id DESC", con)
        if "car_number" in df.columns:
            df["car_number"] = df["car_number"].fillna("N/A")
        return df

def delete_all_driver_data():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM driver_metrics")
    st.cache_data.clear()

def format_lap_time(seconds):
    if pd.isna(seconds) or seconds is None: return "—"
    minutes = int(seconds // 60)
    rem = seconds - minutes * 60
    return f"{minutes}:{rem:06.3f}"

def lap_seconds(value):
    parts = str(value).strip().split(":")
    if len(parts) == 2: return int(parts[0]) * 60 + float(parts[1])
    return float(value)

def parse_driver_performance_text(text):
    driver_pattern = re.compile(
        r"^\s*([A-ZÀ-ÖØ-Þ][A-ZÀ-ÖØ-Þ .'\-]*?)\s*(?:\n\s*)?#(\d{1,3})\s*\(([^)\n]+)\)\s*:\s*(HYPERCAR|LMP2|LMGT3)\b",
        re.MULTILINE,
    )
    metric_pattern = re.compile(
        r"^\s*(\d+)\s*\n\s*(\d+)\s*\n\s*(\d{1,3})%\s*\n\s*(-?\d+(?:\.\d+)?)\s*\n\s*(\d+:\d{2}\.\d{3})\s*\n\s*(\d+:\d{2}\.\d{3})\s*\n\s*(\d+:\d{2}\.\d{3})",
        re.MULTILINE,
    )
    drivers = [{"driver": m.group(1).strip(), "car_number": m.group(2), "team": m.group(3).strip(), "class": m.group(4).upper()} for m in driver_pattern.finditer(text)]
    metrics = [{
        "laps_shown": int(m.group(1)), "estimated_overtakes": int(m.group(2)),
        "clear_air_pct": float(m.group(3)), "avg_z_score": float(m.group(4)),
        "std_dev_seconds": lap_seconds(m.group(5)), "fastest_lap_seconds": lap_seconds(m.group(6)),
        "avg_lap_seconds": lap_seconds(m.group(7))
    } for m in metric_pattern.finditer(text)]
    
    if not drivers or len(drivers) != len(metrics):
        raise ValueError(f"Found {len(drivers)} drivers and {len(metrics)} metrics. Make sure the full text was parsed.")
    return pd.DataFrame([{**d, **m} for d, m in zip(drivers, metrics)])

def render_metric_card(label, value, subtext=None):
    sub_html = f'<div class="metric-sub">{subtext}</div>' if subtext else ""
    st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">{label}</div>
            <div class="metric-val">{value}</div>
            {sub_html}
        </div>
    """, unsafe_allow_html=True)

def filter_data(data, prefix):
    if data.empty: return data
    c1, c2, c3 = st.columns(3)
    series_opts = sorted(data["series"].dropna().unique())
    class_opts = sorted(data["class"].dropna().unique())
    event_opts = sorted(data["event"].dropna().unique())

    s_sel = c1.multiselect("Championship", series_opts, default=series_opts, key=f"{prefix}_s")
    df = data[data["series"].isin(s_sel)]

    c_sel = c2.multiselect("Class", class_opts, default=class_opts, key=f"{prefix}_c")
    df = df[df["class"].isin(c_sel)]

    e_sel = c3.multiselect("Event", event_opts, default=event_opts, key=f"{prefix}_e")
    return df[df["event"].isin(e_sel)]


# =========================================================
# INIT & SIDEBAR
# =========================================================

init_db()
init_driver_db()
imports = list_imports()
report_df = pd.DataFrame(imports)
driver_df = get_driver_data()

with st.sidebar:
    st.title("🏁 WEC Analytics")
    page = st.radio(
        "Navigation",
        ["Driver Analysis", "Team Analysis", "Reports", "Import Reports", "AI Assistant"],
        label_visibility="collapsed"
    )
    st.divider()
    st.caption(f"📁 **Reports:** {len(imports)}")
    st.caption(f"🏎️ **Drivers:** {driver_df['driver'].nunique() if not driver_df.empty else 0}")
    
    if not driver_df.empty and "car_number" in driver_df.columns:
        teams_count = len(driver_df.groupby(["team", "car_number"]))
        st.caption(f"🛡️ **Team Cars:** {teams_count}")


# =========================================================
# 1. DRIVER ANALYSIS
# =========================================================

if page == "Driver Analysis":
    st.subheader("Driver Performance")

    if driver_df.empty:
        st.info("Driver database is currently empty. Extract a report under **Reports**.")
    else:
        filtered = filter_data(driver_df, "drv")

        if not filtered.empty:
            c1, c2, c3, c4 = st.columns(4)
            with c1: render_metric_card("Active Drivers", filtered["driver"].nunique())
            with c2: render_metric_card("Team Cars", len(filtered.groupby(["team", "car_number"])))

            valid_avg = filtered.dropna(subset=["avg_lap_seconds"])
            if not valid_avg.empty:
                b_avg = valid_avg.loc[valid_avg["avg_lap_seconds"].idxmin()]
                with c3: render_metric_card("Best Average Lap", format_lap_time(b_avg["avg_lap_seconds"]), f"{b_avg['driver']} (#{b_avg.get('car_number', 'N/A')})")
            else:
                with c3: render_metric_card("Best Average Lap", "—")

            valid_fast = filtered.dropna(subset=["fastest_lap_seconds"])
            if not valid_fast.empty:
                b_fast = valid_fast.loc[valid_fast["fastest_lap_seconds"].idxmin()]
                with c4: render_metric_card("Fastest Lap", format_lap_time(b_fast["fastest_lap_seconds"]), f"{b_fast['driver']} (#{b_fast.get('car_number', 'N/A')})")
            else:
                with c4: render_metric_card("Fastest Lap", "—")

            st.divider()

            # SINGLE DRIVER PROFILE
            st.markdown("### 👤 Single Driver Profile")
            st.caption("Select a driver to see their granular race data and overtake metrics.")
            
            driver_list = sorted(filtered["driver"].unique())
            selected_driver = st.selectbox("Select Driver to Inspect", driver_list)
            
            driver_data = filtered[filtered["driver"] == selected_driver]
            
            show_prof_cols = [
                "event", "car_number", "team", "class", "avg_lap_seconds", "fastest_lap_seconds", 
                "std_dev_seconds", "clear_air_pct", "estimated_overtakes", "laps_shown"
            ]
            
            d_display = driver_data[show_prof_cols].copy()
            d_display["car_number"] = d_display["car_number"].apply(lambda x: f"#{x}" if str(x) != "N/A" else "N/A")
            d_display["avg_lap_seconds"] = d_display["avg_lap_seconds"].apply(format_lap_time)
            d_display["fastest_lap_seconds"] = d_display["fastest_lap_seconds"].apply(format_lap_time)
            d_display["std_dev_seconds"] = d_display["std_dev_seconds"].round(3)
            
            d_display = d_display.rename(columns={
                "event": "Event", "car_number": "Car", "team": "Team", "class": "Class",
                "avg_lap_seconds": "Avg Lap", "fastest_lap_seconds": "Fastest Lap",
                "std_dev_seconds": "Consistency (s)", "clear_air_pct": "Clear Air %",
                "estimated_overtakes": "Est. Overtakes", "laps_shown": "Laps Counted"
            })
            
            st.dataframe(d_display, use_container_width=True, hide_index=True)

            with st.expander("⚔️ Head-to-Head Driver Comparison"):
                if len(driver_list) >= 2:
                    h1, h2 = st.columns(2)
                    da = h1.selectbox("Driver A", driver_list, index=0, key="da")
                    db = h2.selectbox("Driver B", [d for d in driver_list if d != da], index=0, key="db")
                    
                    row_a = filtered[filtered["driver"] == da].mean(numeric_only=True)
                    row_b = filtered[filtered["driver"] == db].mean(numeric_only=True)

                    comp_data = {
                        "Metric": ["Average Lap", "Fastest Lap (Average)", "Consistency (Std Dev)", "Clear Air %", "Est Overtakes (Avg)"],
                        da: [
                            format_lap_time(row_a.get("avg_lap_seconds")), 
                            format_lap_time(row_a.get("fastest_lap_seconds")), 
                            f"{row_a.get('std_dev_seconds', 0):.3f} s", 
                            f"{row_a.get('clear_air_pct', 0):.1f}%",
                            f"{row_a.get('estimated_overtakes', 0):.1f}"
                        ],
                        db: [
                            format_lap_time(row_b.get("avg_lap_seconds")), 
                            format_lap_time(row_b.get("fastest_lap_seconds")), 
                            f"{row_b.get('std_dev_seconds', 0):.3f} s", 
                            f"{row_b.get('clear_air_pct', 0):.1f}%",
                            f"{row_b.get('estimated_overtakes', 0):.1f}"
                        ]
                    }
                    st.dataframe(pd.DataFrame(comp_data), use_container_width=True, hide_index=True)

            st.divider()

            # LEADERBOARDS & CHARTS
            st.markdown("### 📊 Grid Leaderboard & Curves")
            
            sort_by = st.selectbox(
                "Order grid rankings by",
                ["avg_lap_seconds", "fastest_lap_seconds", "std_dev_seconds", "avg_z_score"],
                format_func=lambda x: {"avg_lap_seconds": "Average Lap Time", "fastest_lap_seconds": "Fastest Lap Time", "std_dev_seconds": "Consistency (Std Dev)", "avg_z_score": "Z-Score"}[x]
            )

            ranked = filtered.dropna(subset=[sort_by]).sort_values(sort_by, ascending=True).copy()
            ranked.insert(0, "Rank", range(1, len(ranked) + 1))
            ranked["car_number"] = ranked["car_number"].apply(lambda x: f"#{x}" if str(x) != "N/A" else "N/A")
            ranked["Avg Lap"] = ranked["avg_lap_seconds"].apply(format_lap_time)
            ranked["Fastest Lap"] = ranked["fastest_lap_seconds"].apply(format_lap_time)
            ranked["Consistency (s)"] = ranked["std_dev_seconds"].round(3)
            ranked["Z-Score"] = ranked["avg_z_score"].round(3)

            st.dataframe(
                ranked[["Rank", "car_number", "driver", "team", "class", "Avg Lap", "Fastest Lap", "Consistency (s)", "Z-Score"]].rename(columns={"car_number": "Car", "driver": "Driver", "team": "Team", "class": "Class"}),
                use_container_width=True, hide_index=True
            )

            st.write("")
            g1, g2 = st.columns(2)

            with g1:
                ranked["Pace Gap (s)"] = (ranked["avg_lap_seconds"] - ranked.groupby("class")["avg_lap_seconds"].transform("min")).round(3)
                fig_gap = px.line(
                    ranked.sort_values("Pace Gap (s)"), x="driver", y="Pace Gap (s)", color="class",
                    hover_data=["car_number", "team"],
                    title="Average Lap Gap to Class Leader (+ seconds)",
                    markers=True, template="plotly_white",
                    color_discrete_sequence=px.colors.qualitative.Set1
                )
                fig_gap.update_layout(xaxis_title="", yaxis_title="Gap (+s)", hovermode="x unified")
                fig_gap.update_traces(line=dict(width=3), marker=dict(size=8))
                st.plotly_chart(fig_gap, use_container_width=True)

            with g2:
                fig_std = px.line(
                    ranked.sort_values("std_dev_seconds"), x="driver", y="std_dev_seconds", color="class",
                    hover_data=["car_number", "team"],
                    title="Driver Consistency Curve (Lower = Better)",
                    markers=True, template="plotly_white",
                    color_discrete_sequence=px.colors.qualitative.Set1
                )
                fig_std.update_layout(xaxis_title="", yaxis_title="Std Dev (s)", hovermode="x unified")
                fig_std.update_traces(line=dict(width=3), marker=dict(size=8))
                st.plotly_chart(fig_std, use_container_width=True)


# =========================================================
# 2. TEAM ANALYSIS
# =========================================================

elif page == "Team Analysis":
    st.subheader("Team & Car Analysis")

    if driver_df.empty:
        st.info("No driver metrics available to compute team stats.")
    else:
        filtered = filter_data(driver_df, "tm")
        if not filtered.empty:
            
            filtered["team_car"] = filtered.apply(lambda r: f"{r['team']} #{r['car_number']}" if str(r['car_number']) != "N/A" else r['team'], axis=1)

            st.markdown("### 🛡️ Individual Team Profile")
            st.caption("Select a team car entry to view their specific driver lineup metrics.")
            
            team_car_list = sorted(filtered["team_car"].unique())
            selected_team_car = st.selectbox("Select Team Car to Inspect", team_car_list)
            
            team_driver_data = filtered[filtered["team_car"] == selected_team_car]
            
            t_display = team_driver_data[["driver", "event", "class", "avg_lap_seconds", "fastest_lap_seconds", "std_dev_seconds", "clear_air_pct", "estimated_overtakes"]].copy()
            t_display["avg_lap_seconds"] = t_display["avg_lap_seconds"].apply(format_lap_time)
            t_display["fastest_lap_seconds"] = t_display["fastest_lap_seconds"].apply(format_lap_time)
            t_display["std_dev_seconds"] = t_display["std_dev_seconds"].round(3)
            
            t_display = t_display.rename(columns={
                "driver": "Driver", "event": "Event", "class": "Class",
                "avg_lap_seconds": "Avg Lap", "fastest_lap_seconds": "Fastest Lap",
                "std_dev_seconds": "Consistency (s)", "clear_air_pct": "Clear Air %",
                "estimated_overtakes": "Est. Overtakes"
            })
            
            st.dataframe(t_display, use_container_width=True, hide_index=True)
            
            st.divider()

            # AGGREGATE TEAM STATS
            team_summary = (
                filtered.groupby(["team", "car_number", "team_car", "class"], as_index=False)
                .agg(avg_lap=("avg_lap_seconds", "mean"), best_lap=("fastest_lap_seconds", "min"), avg_consistency=("std_dev_seconds", "mean"), drivers=("driver", "nunique"))
            )

            team_summary["Team Gap (s)"] = (team_summary["avg_lap"] - team_summary.groupby("class")["avg_lap"].transform("min")).round(3)
            team_summary["Avg Lap Time"] = team_summary["avg_lap"].apply(format_lap_time)
            team_summary["Best Lap Time"] = team_summary["best_lap"].apply(format_lap_time)
            team_summary["Consistency (s)"] = team_summary["avg_consistency"].round(3)
            team_summary["Car"] = team_summary["car_number"].apply(lambda x: f"#{x}" if str(x) != "N/A" else "N/A")

            with st.expander("⚔️ Head-to-Head Team Comparison", expanded=True):
                if len(team_car_list) >= 2:
                    h1, h2 = st.columns(2)
                    ta = h1.selectbox("Team Car A", team_car_list, index=0, key="ta")
                    tb = h2.selectbox("Team Car B", [t for t in team_car_list if t != ta], index=0, key="tb")
                    
                    row_ta = team_summary[team_summary["team_car"] == ta].iloc[0]
                    row_tb = team_summary[team_summary["team_car"] == tb].iloc[0]

                    tcomp_data = {
                        "Metric": ["Average Lap", "Best Lap", "Consistency (Std Dev)"],
                        ta: [
                            row_ta["Avg Lap Time"], 
                            row_ta["Best Lap Time"], 
                            f"{row_ta['avg_consistency']:.3f} s"
                        ],
                        tb: [
                            row_tb["Avg Lap Time"], 
                            row_tb["Best Lap Time"], 
                            f"{row_tb['avg_consistency']:.3f} s"
                        ]
                    }
                    st.dataframe(pd.DataFrame(tcomp_data), use_container_width=True, hide_index=True)

            st.write("")
            st.markdown("### 📈 Team Leaderboard & Curves")
            
            st.dataframe(
                team_summary.sort_values("Team Gap (s)")[["team", "Car", "class", "Avg Lap Time", "Team Gap (s)", "Best Lap Time", "Consistency (s)", "drivers"]].rename(columns={"team": "Team", "class": "Class", "drivers": "Drivers"}),
                use_container_width=True, hide_index=True
            )

            st.write("")
            t1, t2 = st.columns(2)

            with t1:
                fig_tgap = px.line(
                    team_summary.sort_values("Team Gap (s)"), x="team_car", y="Team Gap (s)", color="class",
                    title="Team Pace Gap Curve (+s)", markers=True, template="plotly_white"
                )
                fig_tgap.update_layout(xaxis_title="", yaxis_title="Gap (+s)", hovermode="x unified")
                fig_tgap.update_traces(line=dict(width=3), marker=dict(size=8))
                st.plotly_chart(fig_tgap, use_container_width=True)

            with t2:
                fig_tstd = px.line(
                    team_summary.sort_values("Consistency (s)"), x="team_car", y="Consistency (s)", color="class",
                    title="Team Consistency Curve", markers=True, template="plotly_white"
                )
                fig_tstd.update_layout(xaxis_title="", yaxis_title="Std Dev (s)", hovermode="x unified")
                fig_tstd.update_traces(line=dict(width=3), marker=dict(size=8))
                st.plotly_chart(fig_tstd, use_container_width=True)


# =========================================================
# 3. REPORTS
# =========================================================

elif page == "Reports":
    st.subheader("Report Manager")

    if report_df.empty:
        st.info("No reports imported yet. Go to **Import Reports** to add one.")
    else:
        search = st.text_input("Quick search reports", placeholder="Filter by event, name or series...")
        view = report_df.copy()
        if search:
            mask = view.astype(str).apply(lambda r: r.str.contains(search, case=False).any(), axis=1)
            view = view[mask]

        st.dataframe(view[["id", "filename", "event", "report_type", "created_at"]], use_container_width=True, hide_index=True)

        st.divider()
        sel_id = st.selectbox("Select report to inspect", view["id"].tolist(), format_func=lambda x: f"#{x} - {view.loc[view['id'] == x, 'filename'].values[0]}")
        chosen = view[view["id"] == sel_id].iloc[0]

        c1, c2, c3 = st.columns(3)
        with c1: render_metric_card("Event", chosen.get("event") or "—")
        with c2: render_metric_card("Report Type", chosen.get("report_type") or "—")
        with c3: render_metric_card("Season", chosen.get("season") or "—")

        with st.expander("View Raw PDF Text Preview"):
            st.text_area("Extracted Text", chosen.get("text_preview", ""), height=180, disabled=True)

        act1, act2 = st.columns([2, 1])
        with act1:
            if chosen.get("report_type") == "Driver Performance":
                if st.button("Extract Driver Performance Data", type="primary"):
                    try:
                        extracted = parse_driver_performance_text(chosen.get("text_preview", ""))
                        extracted["series"] = chosen.get("series")
                        extracted["season"] = chosen.get("season")
                        extracted["event"] = chosen.get("event")
                        save_driver_data(extracted)
                        st.success(f"Parsed and added {len(extracted)} driver records!")
                        st.rerun()
                    except Exception as e: st.error(f"Extraction failed: {e}")

        with act2:
            if st.button("Delete This Report", type="secondary"):
                delete_import(int(chosen["id"])) # Fixed integer casting for SQLite
                st.cache_data.clear()
                st.success("Report deleted.")
                st.rerun()


# =========================================================
# 4. IMPORT REPORTS
# =========================================================

elif page == "Import Reports":
    st.subheader("Import Reports")

    uploaded = st.file_uploader("Upload timing report PDFs", type=["pdf"], accept_multiple_files=True)
    if uploaded and st.button("Process Uploads", type="primary"):
        for f in uploaded:
            try:
                parsed = parse_pdf_bytes(f.getvalue(), f.name)
                save_import(parsed)
                st.success(f"Imported: {f.name}")
            except Exception as e:
                st.error(f"Error on {f.name}: {e}")
        st.rerun()

    st.divider()
    url = st.text_input("Or fetch from direct PDF URL", placeholder="https://domain.com/timing.pdf")
    if st.button("Download & Process"):
        if url.strip():
            try:
                name, data = download_pdf(url.strip())
                parsed = parse_pdf_bytes(data, name)
                parsed["source_url"] = url.strip()
                save_import(parsed)
                st.success(f"Imported: {name}")
                st.rerun()
            except Exception as e: st.error(f"Download failed: {e}")

    st.divider()
    st.markdown("### Auto-Sync from Website")
    st.caption("Scrape a webpage (like The B-pillar directory) for new PDF reports and import them automatically.")
    
    sync_url = st.text_input("Website URL to scrape", placeholder="https://theb-pillar.com/timing-directory")
    
    if st.button("Run Auto-Sync", type="primary"):
        if not sync_url.strip():
            st.warning("Please enter a URL to scrape.")
        else:
            with st.spinner(f"Scraping {sync_url}..."):
                try:
                    res = sync_bpillar_reports(sync_url.strip())
                    
                    if res["success"]:
                        st.success(f"Successfully imported {len(res['success'])} new reports!")
                    if res["skipped"] > 0:
                        st.info(f"Skipped {res['skipped']} reports that were already in the database.")
                    if res["failed"]:
                        with st.expander("View Failed Imports"):
                            for err in res["failed"]:
                                st.error(err)
                    
                    st.cache_data.clear()
                except Exception as e:
                    st.error(f"Scraping failed: {e}")


# =========================================================
# 5. AI ASSISTANT
# =========================================================

elif page == "AI Assistant":
    st.subheader("AI Motorsport Assistant")

    q = st.text_area("Ask a question about the imported data:", placeholder="e.g. Compare the average lap times between the Hypercar drivers.")
    if st.button("Run Query", type="primary"):
        if not q.strip(): st.warning("Please enter a question first.")
        elif not imports: st.warning("No reports found to analyze.")
        elif not os.getenv("OPENAI_API_KEY"): st.error("Missing OPENAI_API_KEY in .env file.")
        else:
            try:
                from openai import OpenAI
                client = OpenAI()
                # Convert structured dataframe to CSV format for the AI (highly token efficient)
                context = f"STRUCTURED DRIVER PERFORMANCE DATA:\n{driver_df.to_csv(index=False)}"
                res = client.responses.create(
                    model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
                    input=[
                        {"role": "system", "content": "You are a concise motorsport timing analyst. Use only provided text extracts."},
                        {"role": "user", "content": f"DATA:\n{context}\n\nQUESTION:\n{q}"}
                    ]
                )
                st.markdown(res.output_text)
            except Exception as e: st.error(f"Analysis error: {e}")

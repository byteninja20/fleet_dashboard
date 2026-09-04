"""
Fleet Collection Dashboard (with persistent database)
------------------------------------------------------
- You upload a CSV whenever you have new/updated data -> it's saved to the database.
- Everyone else just opens the app and browses dates -> no upload needed.
- Re-uploading a date's data updates existing rows instead of duplicating them.

Setup required (one-time):
1. Create a free Postgres database at https://supabase.com (or any Postgres host).
2. Get its connection string (looks like: postgresql://user:pass@host:5432/dbname).
3. In Streamlit Cloud: App settings -> Secrets -> add:
       DB_URL = "postgresql://user:pass@host:5432/dbname"
4. Redeploy. The app will create its table automatically on first run.

Run locally with:
    streamlit run fleet_dashboard.py
"""

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

st.set_page_config(page_title="Fleet Collection Dashboard", layout="wide")

EXPECTED_COLUMNS = [
    "Date", "Delhivery ID", "Porter ID", "Driver Name", "Vehicle No",
    "Platform", "Source", "Net Earning", "Cash Collected", "Online Payment",
    "Wallet/App", "Total Collection", "Withdrawal", "MG Amount",
    "MG Yes/No", "Toll", "Other Expense", "Remarks",
]

# DB column names (snake_case) mapped to CSV header names
COLUMN_MAP = {
    "date": "Date",
    "delhivery_id": "Delhivery ID",
    "porter_id": "Porter ID",
    "driver_name": "Driver Name",
    "vehicle_no": "Vehicle No",
    "platform": "Platform",
    "source": "Source",
    "net_earning": "Net Earning",
    "cash_collected": "Cash Collected",
    "online_payment": "Online Payment",
    "wallet_app": "Wallet/App",
    "total_collection": "Total Collection",
    "withdrawal": "Withdrawal",
    "mg_amount": "MG Amount",
    "mg_yes_no": "MG Yes/No",
    "toll": "Toll",
    "other_expense": "Other Expense",
    "remarks": "Remarks",
}

NUMERIC_DB_COLUMNS = [
    "net_earning", "total_collection", "withdrawal", "mg_amount", "toll", "other_expense",
]


@st.cache_resource
def get_engine():
    db_url = st.secrets["DB_URL"]
    engine = create_engine(db_url, pool_pre_ping=True)

    # Each statement runs in its own transaction, so a failure in one
    # (e.g. the index) can never roll back an earlier one (e.g. the table).
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS fleet_data (
                id SERIAL PRIMARY KEY,
                date DATE NOT NULL,
                delhivery_id TEXT,
                porter_id TEXT,
                driver_name TEXT,
                vehicle_no TEXT,
                platform TEXT,
                source TEXT,
                net_earning NUMERIC,
                cash_collected TEXT,
                online_payment TEXT,
                wallet_app TEXT,
                total_collection NUMERIC,
                withdrawal NUMERIC,
                mg_amount NUMERIC,
                mg_yes_no TEXT,
                toll NUMERIC,
                other_expense NUMERIC,
                remarks TEXT
            )
        """))

    try:
        with engine.begin() as conn:
            conn.execute(text("""
                ALTER TABLE fleet_data DROP CONSTRAINT IF EXISTS fleet_data_date_driver_name_vehicle_no_key
            """))
    except Exception:
        pass  # constraint didn't exist under that name; safe to ignore

    try:
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS fleet_data_unique_row
                ON fleet_data (date, COALESCE(driver_name, ''), COALESCE(vehicle_no, ''), COALESCE(porter_id, ''))
            """))
    except Exception:
        pass  # likely pre-existing duplicate rows; table still usable, dedup index just won't apply yet

    return engine


def clean_numeric(series):
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce")


def parse_csv(file):
    df = pd.read_csv(file, dtype=str)
    df.columns = [c.strip() for c in df.columns]

    if "Date" not in df.columns:
        return None, "No 'Date' column found in the CSV header."

    df["Date"] = df["Date"].replace("", pd.NA).ffill()
    parsed_date = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
    if parsed_date.isna().all():
        return None, "Could not parse any dates in the 'Date' column."
    df["Date"] = parsed_date.dt.date

    for col in EXPECTED_COLUMNS:
        if col not in df.columns:
            df[col] = None

    return df, None


def upsert_rows(engine, df: pd.DataFrame):
    insert_sql = text("""
        INSERT INTO fleet_data (
            date, delhivery_id, porter_id, driver_name, vehicle_no, platform, source,
            net_earning, cash_collected, online_payment, wallet_app, total_collection,
            withdrawal, mg_amount, mg_yes_no, toll, other_expense, remarks
        ) VALUES (
            :date, :delhivery_id, :porter_id, :driver_name, :vehicle_no, :platform, :source,
            :net_earning, :cash_collected, :online_payment, :wallet_app, :total_collection,
            :withdrawal, :mg_amount, :mg_yes_no, :toll, :other_expense, :remarks
        )
        ON CONFLICT (date, COALESCE(driver_name, ''), COALESCE(vehicle_no, ''), COALESCE(porter_id, ''))
        DO UPDATE SET
            delhivery_id = EXCLUDED.delhivery_id,
            porter_id = EXCLUDED.porter_id,
            platform = EXCLUDED.platform,
            source = EXCLUDED.source,
            net_earning = EXCLUDED.net_earning,
            cash_collected = EXCLUDED.cash_collected,
            online_payment = EXCLUDED.online_payment,
            wallet_app = EXCLUDED.wallet_app,
            total_collection = EXCLUDED.total_collection,
            withdrawal = EXCLUDED.withdrawal,
            mg_amount = EXCLUDED.mg_amount,
            mg_yes_no = EXCLUDED.mg_yes_no,
            toll = EXCLUDED.toll,
            other_expense = EXCLUDED.other_expense,
            remarks = EXCLUDED.remarks
    """)

    rows = []
    for _, r in df.iterrows():
        row = {db_col: r.get(csv_col) for db_col, csv_col in COLUMN_MAP.items()}
        for num_col in NUMERIC_DB_COLUMNS:
            val = row.get(num_col)
            if pd.notna(val):
                cleaned = clean_numeric(pd.Series([val])).iloc[0]
                row[num_col] = float(cleaned) if pd.notna(cleaned) else None
            else:
                row[num_col] = None
        for k, v in row.items():
            if pd.isna(v):
                row[k] = None
            elif hasattr(v, "item"):  # convert any remaining numpy scalar types to native Python
                row[k] = v.item()
        rows.append(row)

    with engine.begin() as conn:
        for row in rows:
            conn.execute(insert_sql, row)

    return len(rows)


@st.cache_data(ttl=30)
def load_all_dates(_engine):
    with _engine.begin() as conn:
        result = conn.execute(text("SELECT DISTINCT date FROM fleet_data ORDER BY date"))
        return [r[0] for r in result]


@st.cache_data(ttl=30)
def load_date_rows(_engine, selected_date):
    with _engine.begin() as conn:
        result = conn.execute(
            text("SELECT * FROM fleet_data WHERE date = :d ORDER BY driver_name"),
            {"d": selected_date},
        )
        rows = [dict(r._mapping) for r in result]
    return pd.DataFrame(rows)


def style_table(view_df: pd.DataFrame) -> pd.DataFrame:
    if view_df.empty:
        return view_df

    rename_map = {db_col: csv_col for db_col, csv_col in COLUMN_MAP.items()}
    out = view_df.rename(columns=rename_map)
    display_cols = [c for c in EXPECTED_COLUMNS if c in out.columns]
    out = out[display_cols].copy()

    money_cols = ["Net Earning", "Total Collection", "Withdrawal", "MG Amount", "Toll", "Other Expense"]
    for col in money_cols:
        if col in out.columns:
            out[col] = out[col].apply(lambda v: f"\u20b9{v:,.0f}" if pd.notna(v) else "")

    if "Date" in out.columns:
        out["Date"] = pd.to_datetime(out["Date"]).dt.strftime("%d/%m/%Y")

    return out.fillna("")


@st.cache_data(ttl=30)
def load_vehicle_numbers(_engine):
    with _engine.begin() as conn:
        result = conn.execute(text("""
            SELECT DISTINCT vehicle_no FROM fleet_data
            WHERE vehicle_no IS NOT NULL AND vehicle_no <> ''
            ORDER BY vehicle_no
        """))
        return [r[0] for r in result]


@st.cache_data(ttl=30)
def load_available_months(_engine):
    with _engine.begin() as conn:
        result = conn.execute(text("""
            SELECT DISTINCT date_trunc('month', date)::date AS month
            FROM fleet_data
            ORDER BY month
        """))
        return [r[0] for r in result]


@st.cache_data(ttl=30)
def load_vehicle_month_report(_engine, vehicle_no, month_start):
    with _engine.begin() as conn:
        result = conn.execute(
            text("""
                SELECT
                    date,
                    COALESCE(net_earning, 0) + COALESCE(mg_amount, 0) AS income,
                    COALESCE(toll, 0) + COALESCE(other_expense, 0) AS expenses
                FROM fleet_data
                WHERE vehicle_no = :vehicle_no
                  AND date_trunc('month', date) = :month_start
                ORDER BY date
            """),
            {"vehicle_no": vehicle_no, "month_start": month_start},
        )
        rows = [dict(r._mapping) for r in result]
    return pd.DataFrame(rows)


def render_vehicle_monthly_report(engine):
    vehicles = load_vehicle_numbers(engine)
    months = load_available_months(engine)

    if not vehicles or not months:
        st.info("No data yet. Upload a CSV first.")
        return

    col_month, col_vehicle = st.columns(2)

    with col_month:
        month_labels = [pd.Timestamp(m).strftime("%B %Y") for m in months]
        selected_month_label = st.selectbox("Select month", options=month_labels, index=len(month_labels) - 1)
        selected_month = months[month_labels.index(selected_month_label)]

    with col_vehicle:
        selected_vehicle = st.selectbox("Select vehicle number", options=vehicles)

    report_df = load_vehicle_month_report(engine, selected_vehicle, selected_month)

    if report_df.empty:
        st.warning(f"No records for {selected_vehicle} in {selected_month_label}.")
        return

    display_df = report_df.copy()
    display_df["date"] = pd.to_datetime(display_df["date"]).dt.strftime("%d/%m/%Y")
    display_df = display_df.rename(columns={"date": "Date", "income": "Income", "expenses": "Expenses"})
    display_df["Net"] = display_df["Income"] - display_df["Expenses"]

    total_income = report_df["income"].sum()
    total_expenses = report_df["expenses"].sum()
    total_net = total_income - total_expenses

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Income", f"\u20b9{total_income:,.0f}")
    col2.metric("Total Expenses", f"\u20b9{total_expenses:,.0f}")
    col3.metric("Net", f"\u20b9{total_net:,.0f}")

    styled = display_df.copy()
    for col in ["Income", "Expenses", "Net"]:
        styled[col] = styled[col].apply(lambda v: f"\u20b9{v:,.0f}")

    st.dataframe(styled, use_container_width=True, hide_index=True)
    st.caption(f"{len(display_df)} day(s) with data for {selected_vehicle} in {selected_month_label}.")


def main():
    st.title("\U0001f69a Fleet Collection Dashboard")

    if "DB_URL" not in st.secrets:
        st.error(
            "No database connected yet. Add a `DB_URL` secret in your Streamlit Cloud "
            "app settings (Settings \u2192 Secrets) pointing to your Postgres database."
        )
        return

    engine = get_engine()

    tab_daily, tab_vehicle = st.tabs(["\U0001f4c5 Daily View", "\U0001f697 Vehicle Monthly Report"])

    with tab_daily:
        with st.expander("\U0001f4e4 Upload new / updated daily data (CSV)"):
            uploaded = st.file_uploader("Upload CSV file", type=["csv"])
            if uploaded is not None:
                df, error = parse_csv(uploaded)
                if error:
                    st.error(error)
                else:
                    count = upsert_rows(engine, df)
                    st.success(f"Saved {count} row(s) to the database.")
                    load_all_dates.clear()
                    load_date_rows.clear()
                    load_vehicle_numbers.clear()
                    load_available_months.clear()
                    load_vehicle_month_report.clear()

        dates = load_all_dates(engine)

        if not dates:
            st.info("No data yet. Upload a CSV above to get started.")
            return

        date_labels = [pd.Timestamp(d).strftime("%d/%m/%Y") for d in dates]

        if "date_idx" not in st.session_state:
            st.session_state.date_idx = len(dates) - 1

        st.session_state.date_idx = max(0, min(st.session_state.date_idx, len(dates) - 1))

        col_prev, col_current, col_next = st.columns([1, 3, 1])

        with col_prev:
            if st.button("\u25c0 Previous", use_container_width=True, disabled=st.session_state.date_idx == 0):
                st.session_state.date_idx -= 1

        with col_next:
            if st.button("Next \u25b6", use_container_width=True, disabled=st.session_state.date_idx == len(dates) - 1):
                st.session_state.date_idx += 1

        with col_current:
            selected_label = st.selectbox(
                "Tap to jump to a specific date",
                options=date_labels,
                index=st.session_state.date_idx,
                label_visibility="collapsed",
            )
            st.session_state.date_idx = date_labels.index(selected_label)

        selected_date = dates[st.session_state.date_idx]
        st.subheader(f"\U0001f4c5 {pd.Timestamp(selected_date).strftime('%d/%m/%Y')}")

        day_df = load_date_rows(engine, selected_date)

        if day_df.empty:
            st.warning("No records found for this date.")
        else:
            clean = style_table(day_df)
            st.dataframe(clean, use_container_width=True, hide_index=True)
            st.caption(f"{len(clean)} record(s) for this date.")

    with tab_vehicle:
        render_vehicle_monthly_report(engine)


if __name__ == "__main__":
    main()

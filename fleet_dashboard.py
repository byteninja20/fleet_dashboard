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

   [postgres]
   host = "your-host"
   port = 5432
   database = "postgres"
   user = "postgres"
   password = "your-password"
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
    from sqlalchemy.engine import URL

    db_url = URL.create(
        "postgresql+psycopg2",
        username=st.secrets["postgres"]["user"],
        password=st.secrets["postgres"]["password"],
        host=st.secrets["postgres"]["host"],
        port=int(st.secrets["postgres"]["port"]),
        database=st.secrets["postgres"]["database"],
    )

    engine = create_engine(db_url, pool_pre_ping=True)

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
                remarks TEXT,
                UNIQUE (date, driver_name, vehicle_no)
            )
        """))

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
        ON CONFLICT (date, driver_name, vehicle_no)
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
            row[num_col] = clean_numeric(pd.Series([val])).iloc[0] if pd.notna(val) else None
        for k, v in row.items():
            if pd.isna(v):
                row[k] = None
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


def main():
    st.title("\U0001f69a Fleet Collection Dashboard")

   if "postgres" not in st.secrets:
    st.error(
        "No database connected yet. Add the [postgres] credentials "
        "in Streamlit Cloud → Settings → Secrets."
    )
    return

    engine = get_engine()

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


if __name__ == "__main__":
    main()

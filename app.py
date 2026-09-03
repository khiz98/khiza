import os
import sqlite3
from datetime import date, datetime
from io import BytesIO

import pandas as pd
import streamlit as st

APP_TITLE = "Pew Pew Fund Manager"
DB_PATH = os.getenv("MICROFUND_DB", "microfund.db")

st.set_page_config(page_title=APP_TITLE, page_icon="💰", layout="wide")


def conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with conn() as c:
        c.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS members(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                joined_date TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                notes TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS transactions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_date TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount >= 0),
                member_id INTEGER,
                payment_method TEXT DEFAULT '',
                tx_type TEXT NOT NULL,
                comments TEXT DEFAULT '',
                source_ref TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(member_id) REFERENCES members(id)
            );
            CREATE TABLE IF NOT EXISTS loans(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id INTEGER NOT NULL,
                disbursed_date TEXT NOT NULL,
                principal REAL NOT NULL CHECK(principal > 0),
                monthly_rate REAL NOT NULL DEFAULT 0.02,
                purpose TEXT DEFAULT '',
                payment_method TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'Open',
                source_tx_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(member_id) REFERENCES members(id),
                FOREIGN KEY(source_tx_id) REFERENCES transactions(id)
            );
            CREATE TABLE IF NOT EXISTS loan_payments(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                loan_id INTEGER NOT NULL,
                payment_date TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount > 0),
                interest_component REAL NOT NULL DEFAULT 0,
                principal_component REAL NOT NULL DEFAULT 0,
                comments TEXT DEFAULT '',
                source_tx_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(loan_id) REFERENCES loans(id),
                FOREIGN KEY(source_tx_id) REFERENCES transactions(id)
            );
            CREATE TABLE IF NOT EXISTS member_earnings(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id INTEGER NOT NULL,
                earning_date TEXT NOT NULL,
                amount REAL NOT NULL,
                source_tx_id INTEGER,
                note TEXT DEFAULT '',
                FOREIGN KEY(member_id) REFERENCES members(id),
                FOREIGN KEY(source_tx_id) REFERENCES transactions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(tx_date);
            CREATE INDEX IF NOT EXISTS idx_tx_member ON transactions(member_id);
            CREATE INDEX IF NOT EXISTS idx_loans_member ON loans(member_id);
            """
        )
        defaults = {
            "monthly_loan_rate": "0.02",
            "min_monthly_saving": "250",
            "max_monthly_saving": "1000",
            "currency": "TJS",
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (k, v))


def setting(key, default=""):
    with conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    with conn() as c:
        c.execute(
            "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def df_query(sql, params=()):
    with conn() as c:
        return pd.read_sql_query(sql, c, params=params)


def execute(sql, params=()):
    with conn() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


def members_df(active_only=False):
    where = "WHERE active=1" if active_only else ""
    return df_query(f"SELECT id, name, joined_date, active, notes FROM members {where} ORDER BY name")


def get_member_id(name):
    with conn() as c:
        row = c.execute("SELECT id FROM members WHERE TRIM(name)=TRIM(?)", (name,)).fetchone()
        if row:
            return int(row[0])
        cur = c.execute(
            "INSERT INTO members(name, joined_date, active) VALUES (?,?,1)",
            (str(name).strip(), date.today().isoformat()),
        )
        return cur.lastrowid


def add_transaction(tx_date, amount, member_id, payment_method, tx_type, comments="", source_ref=""):
    amount = float(amount)
    if tx_type == "Savings Withdrawal":
        if member_id is None:
            raise ValueError("A savings withdrawal must be assigned to a member.")
        available = member_savings_balance(int(member_id), tx_date)
        if amount > available + 1e-9:
            member_name = df_query("SELECT name FROM members WHERE id=?", (int(member_id),))
            name = member_name.iloc[0]["name"] if not member_name.empty else "this member"
            raise ValueError(
                f"Withdrawal rejected: {name} has only {money(available)} available accumulated savings "
                f"as of {pd.Timestamp(tx_date).date()}. The requested withdrawal is {money(amount)}."
            )
    return execute(
        """INSERT INTO transactions(tx_date,amount,member_id,payment_method,tx_type,comments,source_ref)
           VALUES (?,?,?,?,?,?,?)""",
        (str(tx_date), amount, member_id, payment_method.strip(), tx_type, comments.strip(), source_ref),
    )


def loan_state(loan_id, as_of=None):
    as_of = pd.Timestamp(as_of or date.today())
    with conn() as c:
        loan = c.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
        if not loan:
            return None
        payments = c.execute(
            "SELECT * FROM loan_payments WHERE loan_id=? ORDER BY payment_date,id", (loan_id,)
        ).fetchall()

    principal = float(loan["principal"])
    rate = float(loan["monthly_rate"])
    last_date = pd.Timestamp(loan["disbursed_date"])
    accrued = 0.0
    paid_interest = 0.0
    paid_principal = 0.0

    for p in payments:
        pdate = pd.Timestamp(p["payment_date"])
        if pdate > as_of:
            break
        days = max((pdate - last_date).days, 0)
        accrued += principal * rate * (days / 30.0)
        interest_part = min(float(p["amount"]), accrued)
        principal_part = min(max(float(p["amount"]) - interest_part, 0), principal)
        accrued -= interest_part
        principal -= principal_part
        paid_interest += interest_part
        paid_principal += principal_part
        last_date = pdate

    days = max((as_of - last_date).days, 0)
    accrued += principal * rate * (days / 30.0)
    return {
        "principal_outstanding": max(principal, 0),
        "accrued_interest": max(accrued, 0),
        "total_due": max(principal + accrued, 0),
        "paid_principal": paid_principal,
        "paid_interest": paid_interest,
    }


def record_loan(member_id, amount, loan_date, purpose, method):
    tx_id = add_transaction(loan_date, amount, member_id, method, "Loan", purpose)
    rate = float(setting("monthly_loan_rate", "0.02"))
    return execute(
        """INSERT INTO loans(member_id,disbursed_date,principal,monthly_rate,purpose,payment_method,source_tx_id)
           VALUES (?,?,?,?,?,?,?)""",
        (member_id, str(loan_date), float(amount), rate, purpose, method, tx_id),
    )


def record_loan_payment(loan_id, amount, payment_date, comments, method=""):
    state = loan_state(loan_id, payment_date)
    if not state:
        raise ValueError("Loan not found")
    with conn() as c:
        loan = c.execute("SELECT member_id FROM loans WHERE id=?", (loan_id,)).fetchone()
    member_id = int(loan[0])
    interest_part = min(float(amount), state["accrued_interest"])
    principal_part = min(max(float(amount) - interest_part, 0), state["principal_outstanding"])
    accepted_amount = interest_part + principal_part
    if accepted_amount <= 0:
        raise ValueError("This loan is already fully paid.")
    tx_id = add_transaction(payment_date, accepted_amount, member_id, method, "Loan repayment", comments)
    execute(
        """INSERT INTO loan_payments(loan_id,payment_date,amount,interest_component,principal_component,comments,source_tx_id)
           VALUES (?,?,?,?,?,?,?)""",
        (loan_id, str(payment_date), accepted_amount, interest_part, principal_part, comments, tx_id),
    )
    remaining = loan_state(loan_id, payment_date)
    if remaining and remaining["principal_outstanding"] < 0.01 and remaining["accrued_interest"] < 0.01:
        execute("UPDATE loans SET status='Closed' WHERE id=?", (loan_id,))
    return interest_part, principal_part


def member_savings_balance(member_id, as_of=None):
    as_of = str(as_of or date.today())
    with conn() as c:
        row = c.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN tx_type='Saving' THEN amount ELSE 0 END),0) -
                 COALESCE(SUM(CASE WHEN tx_type='Savings Withdrawal' THEN amount ELSE 0 END),0)
               FROM transactions WHERE member_id=? AND tx_date<=?""",
            (member_id, as_of),
        ).fetchone()
        earn = c.execute(
            "SELECT COALESCE(SUM(amount),0) FROM member_earnings WHERE member_id=? AND earning_date<=?",
            (member_id, as_of),
        ).fetchone()[0]
    return float(row[0]) + float(earn)


def distribute_income(tx_id, amount, earning_date):
    m = members_df(active_only=True)
    balances = [(int(r.id), max(member_savings_balance(int(r.id), earning_date), 0)) for r in m.itertuples()]
    total = sum(x[1] for x in balances)
    if total <= 0:
        return 0
    with conn() as c:
        for member_id, bal in balances:
            share = float(amount) * bal / total
            c.execute(
                "INSERT INTO member_earnings(member_id,earning_date,amount,source_tx_id,note) VALUES (?,?,?,?,?)",
                (member_id, str(earning_date), share, tx_id, "Automatic proportional distribution"),
            )
    return len(balances)


def rebuild_imported_loan_book():
    """Rebuild loans/payments from imported legacy transactions using FIFO by member."""
    with conn() as c:
        c.execute("DELETE FROM loan_payments")
        c.execute("DELETE FROM loans")
        rows = c.execute(
            """SELECT id, tx_date, amount, member_id, payment_method, tx_type, comments
               FROM transactions WHERE tx_type IN ('Loan','Loan repayment') AND member_id IS NOT NULL
               ORDER BY tx_date,id"""
        ).fetchall()
        rate = float(setting("monthly_loan_rate", "0.02"))
        open_by_member = {}
        for r in rows:
            mid = int(r["member_id"])
            if r["tx_type"] == "Loan":
                cur = c.execute(
                    """INSERT INTO loans(member_id,disbursed_date,principal,monthly_rate,purpose,payment_method,source_tx_id)
                       VALUES (?,?,?,?,?,?,?)""",
                    (mid, r["tx_date"], r["amount"], rate, r["comments"] or "", r["payment_method"] or "", r["id"]),
                )
                open_by_member.setdefault(mid, []).append(cur.lastrowid)
                c.commit()
            else:
                remaining = float(r["amount"])
                for lid in list(open_by_member.get(mid, [])):
                    if remaining <= 0.005:
                        break
                    state = loan_state(lid, r["tx_date"])
                    if not state or state["total_due"] <= 0.005:
                        continue
                    alloc = min(remaining, state["total_due"])
                    interest_part = min(alloc, state["accrued_interest"])
                    principal_part = min(alloc - interest_part, state["principal_outstanding"])
                    applied = interest_part + principal_part
                    if applied > 0:
                        c.execute(
                            """INSERT INTO loan_payments(loan_id,payment_date,amount,interest_component,principal_component,comments,source_tx_id)
                               VALUES (?,?,?,?,?,?,?)""",
                            (lid, r["tx_date"], applied, interest_part, principal_part, r["comments"] or "Legacy FIFO allocation", r["id"]),
                        )
                        remaining -= applied
                        c.commit()
        loan_ids = [x[0] for x in c.execute("SELECT id FROM loans").fetchall()]
    for lid in loan_ids:
        s = loan_state(lid)
        if s and s["principal_outstanding"] < 0.01 and s["accrued_interest"] < 0.01:
            execute("UPDATE loans SET status='Closed' WHERE id=?", (lid,))


def import_legacy_excel(uploaded):
    xl = pd.ExcelFile(uploaded)
    sheet = "Transactions_detailed" if "Transactions_detailed" in xl.sheet_names else xl.sheet_names[0]
    raw = pd.read_excel(xl, sheet_name=sheet, header=None)
    header_idx = None
    for i, row in raw.iterrows():
        vals = [str(x).strip() for x in row.tolist() if pd.notna(x)]
        if "Date" in vals and any(v in vals for v in ["Amount TJS", "Amount", "From/To"]):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("Could not find the transaction table header.")
    data = pd.read_excel(xl, sheet_name=sheet, header=header_idx)
    data.columns = [str(c).strip() for c in data.columns]

    def col(*names):
        for n in names:
            if n in data.columns:
                return n
        return None

    date_col = col("Date")
    amount_col = col("Amount TJS", "Amount")
    member_col = col("From/To", "Member", "Name")
    method_col = col("Payment Method", "Payment method")
    type_col = col("tr_type_name", "Transaction Type", "Type")
    comment_col = col("Comments", "Comment")
    if not all([date_col, amount_col, member_col, type_col]):
        raise ValueError(f"Required columns not found. Found: {list(data.columns)}")

    count = 0
    with conn() as c:
        for _, r in data.iterrows():
            if pd.isna(r.get(date_col)) or pd.isna(r.get(amount_col)) or pd.isna(r.get(type_col)):
                continue
            try:
                amt = float(r.get(amount_col))
            except Exception:
                continue
            if amt < 0:
                continue
            dt = pd.to_datetime(r.get(date_col), dayfirst=True, errors="coerce")
            if pd.isna(dt):
                continue
            member_name = "" if pd.isna(r.get(member_col)) else str(r.get(member_col)).strip()
            tx_type = str(r.get(type_col)).strip()
            method = "" if not method_col or pd.isna(r.get(method_col)) else str(r.get(method_col)).strip()
            comment = "" if not comment_col or pd.isna(r.get(comment_col)) else str(r.get(comment_col)).strip()
            member_id = None
            if member_name and member_name.lower() not in {"alif deposit", "alif", "deposit", "nan"}:
                row = c.execute("SELECT id FROM members WHERE TRIM(name)=TRIM(?)", (member_name,)).fetchone()
                if row:
                    member_id = int(row[0])
                else:
                    cur = c.execute(
                        "INSERT INTO members(name,joined_date,active) VALUES (?,?,1)",
                        (member_name, dt.date().isoformat()),
                    )
                    member_id = cur.lastrowid
            source_ref = f"legacy:{sheet}:{dt.date().isoformat()}:{amt}:{member_name}:{tx_type}:{comment[:60]}"
            exists = c.execute("SELECT 1 FROM transactions WHERE source_ref=?", (source_ref,)).fetchone()
            if exists:
                continue
            c.execute(
                """INSERT INTO transactions(tx_date,amount,member_id,payment_method,tx_type,comments,source_ref)
                   VALUES (?,?,?,?,?,?,?)""",
                (dt.date().isoformat(), amt, member_id, method, tx_type, comment, source_ref),
            )
            count += 1
    rebuild_imported_loan_book()
    return count


def login_gate():
    try:
        password = st.secrets.get("APP_PASSWORD", "")
    except Exception:
        password = ""
    if not password:
        return True
    if st.session_state.get("authenticated"):
        return True
    st.title(APP_TITLE)
    entered = st.text_input("App password", type="password")
    if st.button("Sign in"):
        if entered == password:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password")
    return False


def money(x):
    return f"{float(x):,.2f} {setting('currency','TJS')}"


def dashboard():
    st.header("Dashboard")
    tx = df_query("SELECT * FROM transactions")
    members = members_df(active_only=True)
    savings = 0.0
    for r in members.itertuples():
        savings += member_savings_balance(int(r.id))
    loans = df_query("SELECT id FROM loans WHERE status='Open'")
    principal = interest = 0.0
    for lid in loans["id"].tolist() if not loans.empty else []:
        s = loan_state(int(lid))
        principal += s["principal_outstanding"]
        interest += s["accrued_interest"]
    cash_in = 0.0
    cash_out = 0.0
    if not tx.empty:
        cash_in = tx.loc[tx.tx_type.isin(["Saving", "Loan repayment", "Loan interest", "Interest on Deposit", "Extra", "Voluntary Contribution/Fines"]), "amount"].sum()
        cash_out = tx.loc[tx.tx_type.isin(["Loan", "Savings Withdrawal"]), "amount"].sum()
    available = cash_in - cash_out

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Member savings + earnings", money(savings))
    c2.metric("Outstanding loan principal", money(principal))
    c3.metric("Accrued loan interest", money(interest))
    c4.metric("Estimated available fund", money(available))

    if not tx.empty:
        tx["tx_date"] = pd.to_datetime(tx["tx_date"])
        monthly = tx.groupby([tx["tx_date"].dt.to_period("M").astype(str), "tx_type"])["amount"].sum().reset_index()
        pivot = monthly.pivot(index="tx_date", columns="tx_type", values="amount").fillna(0)
        st.subheader("Monthly cash activity")
        st.bar_chart(pivot)

    st.subheader("Member position")
    rows = []
    for r in members.itertuples():
        open_loans = df_query("SELECT id FROM loans WHERE member_id=? AND status='Open'", (int(r.id),))
        due = 0.0
        for lid in open_loans["id"].tolist() if not open_loans.empty else []:
            due += loan_state(int(lid))["total_due"]
        rows.append({"Member": r.name, "Savings balance": member_savings_balance(int(r.id)), "Loan due": due})
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("Add members or import your Excel file to populate the dashboard.")


def transactions_page():
    st.header("Transactions")
    m = members_df(active_only=True)
    names = [""] + m["name"].tolist()
    name_to_id = dict(zip(m["name"], m["id"]))
    types = ["Saving", "Savings Withdrawal", "Interest on Deposit", "Extra", "Voluntary Contribution/Fines", "Other"]

    with st.expander("Add general transaction", expanded=True):
        with st.form("tx_form", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            tx_date = c1.date_input("Date", value=date.today())
            amount = c2.number_input("Amount (TJS)", min_value=0.01, step=10.0)
            tx_type = c3.selectbox("Type", types)
            c4, c5 = st.columns(2)
            member = c4.selectbox("Member (optional)", names)
            method = c5.text_input("Payment method", placeholder="Alif Mobi / Deposit / Binance...")
            comments = st.text_input("Comments")
            distribute = st.checkbox("If this is fund income, distribute it to members proportionally")
            submitted = st.form_submit_button("Save transaction", type="primary")
            if submitted:
                mid = int(name_to_id[member]) if member else None
                try:
                    txid = add_transaction(tx_date, amount, mid, method, tx_type, comments)
                    if distribute and tx_type in {"Interest on Deposit", "Extra", "Voluntary Contribution/Fines"}:
                        n = distribute_income(txid, amount, tx_date)
                        st.success(f"Saved and distributed across {n} active members.")
                    else:
                        st.success("Transaction saved.")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

    st.subheader("Ledger")
    ledger = df_query(
        """SELECT t.id, t.tx_date AS Date, COALESCE(m.name,'Fund') AS Member,
                  t.tx_type AS Type, t.amount AS Amount_TJS, t.payment_method AS Payment_Method, t.comments AS Comments
           FROM transactions t LEFT JOIN members m ON m.id=t.member_id ORDER BY t.tx_date DESC,t.id DESC"""
    )
    st.dataframe(ledger, use_container_width=True, hide_index=True)


def loans_page():
    st.header("Loans")
    m = members_df(active_only=True)
    if m.empty:
        st.warning("Add at least one member first.")
        return
    name_to_id = dict(zip(m["name"], m["id"]))

    tab1, tab2, tab3 = st.tabs(["Loan portfolio", "Issue loan", "Record payment"])
    with tab1:
        loans = df_query(
            """SELECT l.id, m.name AS Member, l.disbursed_date AS Disbursed, l.principal AS Original_Principal,
                      l.monthly_rate AS Monthly_Rate, l.purpose AS Purpose, l.status AS Status
               FROM loans l JOIN members m ON m.id=l.member_id ORDER BY l.disbursed_date DESC,l.id DESC"""
        )
        if loans.empty:
            st.info("No loans yet.")
        else:
            rows = []
            for r in loans.itertuples():
                s = loan_state(int(r.id))
                rows.append({
                    "Loan ID": r.id, "Member": r.Member, "Disbursed": r.Disbursed,
                    "Original principal": r.Original_Principal,
                    "Principal outstanding": s["principal_outstanding"],
                    "Accrued interest": s["accrued_interest"], "Total due": s["total_due"],
                    "Rate/month": r.Monthly_Rate, "Purpose": r.Purpose, "Status": r.Status,
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    with tab2:
        with st.form("loan_form", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            member = c1.selectbox("Borrower", m["name"].tolist())
            amount = c2.number_input("Loan amount", min_value=1.0, step=50.0)
            d = c3.date_input("Disbursement date", date.today())
            method = st.text_input("Payment method")
            purpose = st.text_input("Purpose")
            if st.form_submit_button("Issue loan", type="primary"):
                lid = record_loan(int(name_to_id[member]), amount, d, purpose, method)
                st.success(f"Loan #{lid} issued. Interest will accrue automatically at {float(setting('monthly_loan_rate'))*100:.2f}% per month.")
                st.rerun()

    with tab3:
        open_loans = df_query(
            """SELECT l.id, m.name, l.disbursed_date, l.principal
               FROM loans l JOIN members m ON m.id=l.member_id WHERE l.status='Open' ORDER BY m.name,l.disbursed_date"""
        )
        if open_loans.empty:
            st.info("No open loans.")
        else:
            labels = {}
            for r in open_loans.itertuples():
                s = loan_state(int(r.id))
                labels[f"#{r.id} — {r.name} — due {money(s['total_due'])}"] = int(r.id)
            with st.form("pay_form", clear_on_submit=True):
                choice = st.selectbox("Loan", list(labels.keys()))
                amount = st.number_input("Payment amount", min_value=0.01, step=10.0)
                d = st.date_input("Payment date", date.today(), key="payment_date")
                method = st.text_input("Payment method", key="payment_method")
                comments = st.text_input("Comments", key="payment_comments")
                if st.form_submit_button("Apply payment", type="primary"):
                    i, p = record_loan_payment(labels[choice], amount, d, comments, method)
                    st.success(f"Payment applied automatically: {money(i)} interest + {money(p)} principal.")
                    st.rerun()


def members_page():
    st.header("Members")
    with st.form("member_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        name = c1.text_input("Member name")
        joined = c2.date_input("Joined date", date.today())
        notes = st.text_input("Notes")
        if st.form_submit_button("Add member", type="primary"):
            if name.strip():
                try:
                    execute("INSERT INTO members(name,joined_date,notes) VALUES (?,?,?)", (name.strip(), joined.isoformat(), notes))
                    st.success("Member added.")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("That member already exists.")
    m = members_df()
    if not m.empty:
        show = m.copy()
        show["active"] = show["active"].map({1: "Yes", 0: "No"})
        st.dataframe(show, use_container_width=True, hide_index=True)


def reports_page():
    st.header("Reports & Backup")
    members = members_df()
    if not members.empty:
        rows = []
        for r in members.itertuples():
            deposits = df_query("SELECT COALESCE(SUM(amount),0) x FROM transactions WHERE member_id=? AND tx_type='Saving'", (int(r.id),)).iloc[0,0]
            withdrawals = df_query("SELECT COALESCE(SUM(amount),0) x FROM transactions WHERE member_id=? AND tx_type='Savings Withdrawal'", (int(r.id),)).iloc[0,0]
            earnings = df_query("SELECT COALESCE(SUM(amount),0) x FROM member_earnings WHERE member_id=?", (int(r.id),)).iloc[0,0]
            loans = df_query("SELECT id FROM loans WHERE member_id=?", (int(r.id),))
            due = sum(loan_state(int(lid))["total_due"] for lid in loans["id"].tolist()) if not loans.empty else 0
            rows.append({"Member": r.name, "Deposits": deposits, "Withdrawals": withdrawals, "Distributed earnings": earnings, "Savings balance": member_savings_balance(int(r.id)), "Total loan due": due})
        report = pd.DataFrame(rows)
        st.dataframe(report, use_container_width=True, hide_index=True)
        st.download_button("Download member statement CSV", report.to_csv(index=False).encode("utf-8"), "member_statement.csv", "text/csv")

    ledger = df_query(
        """SELECT t.tx_date, COALESCE(m.name,'Fund') member, t.tx_type, t.amount, t.payment_method, t.comments
           FROM transactions t LEFT JOIN members m ON m.id=t.member_id ORDER BY t.tx_date,t.id"""
    )
    st.download_button("Download full ledger CSV", ledger.to_csv(index=False).encode("utf-8"), "full_ledger.csv", "text/csv")
    with open(DB_PATH, "rb") as f:
        st.download_button("Download complete database backup", f.read(), "microfund_backup.db", "application/octet-stream")


def import_page():
    st.header("Import your existing Excel")
    st.write("Upload the existing workbook. The importer looks for the **Transactions_detailed** sheet and avoids re-importing the same rows.")
    uploaded = st.file_uploader("Excel workbook", type=["xlsx", "xlsm"])
    if uploaded and st.button("Import transactions", type="primary"):
        try:
            n = import_legacy_excel(uploaded)
            st.success(f"Imported {n} new transaction(s). Historical loan repayments were allocated FIFO to each member's oldest open loan.")
        except Exception as e:
            st.exception(e)


def settings_page():
    st.header("Settings")
    with st.form("settings_form"):
        rate_pct = st.number_input("Loan interest rate per month (%)", min_value=0.0, max_value=100.0, value=float(setting("monthly_loan_rate", "0.02"))*100, step=0.1)
        c1, c2 = st.columns(2)
        min_save = c1.number_input("Minimum expected monthly saving", min_value=0.0, value=float(setting("min_monthly_saving", "250")), step=50.0)
        max_save = c2.number_input("Maximum expected monthly saving", min_value=0.0, value=float(setting("max_monthly_saving", "1000")), step=50.0)
        currency = st.text_input("Currency", value=setting("currency", "TJS"))
        if st.form_submit_button("Save settings", type="primary"):
            set_setting("monthly_loan_rate", rate_pct/100)
            set_setting("min_monthly_saving", min_save)
            set_setting("max_monthly_saving", max_save)
            set_setting("currency", currency.strip() or "TJS")
            st.success("Settings updated.")
            st.rerun()
    st.caption("Interest is calculated as simple interest on outstanding principal, prorated by days using a 30-day month.")


init_db()
if not login_gate():
    st.stop()

st.sidebar.title("💰 Pew Pew Fund")
page = st.sidebar.radio(
    "Navigation",
    ["Dashboard", "Transactions", "Loans", "Members", "Reports & Backup", "Import Excel", "Settings"],
)
st.sidebar.caption("Single-ledger savings & micro-loan manager")

if page == "Dashboard":
    dashboard()
elif page == "Transactions":
    transactions_page()
elif page == "Loans":
    loans_page()
elif page == "Members":
    members_page()
elif page == "Reports & Backup":
    reports_page()
elif page == "Import Excel":
    import_page()
else:
    settings_page()

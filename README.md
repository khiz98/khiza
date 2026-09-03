# Pew Pew Fund Manager

A Streamlit application for a friends' savings/deposit and micro-loan fund.

## What it automates

- Member registry
- Savings deposits and withdrawals
- Loan issuance
- Automatic loan interest accrual (default 2% monthly on outstanding principal, prorated daily on a 30-day month)
- Loan repayment allocation: accrued interest first, then principal
- Deposit/fund income distribution among active members based on their savings balances
- Dashboard with savings, outstanding principal, accrued interest and estimated available fund
- Full transaction ledger
- Member statements and CSV exports
- SQLite database backup
- Import of the legacy `Transactions_detailed` Excel sheet
- FIFO reconstruction of historical loan repayments to older open loans
- Optional password protection through Streamlit secrets

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Free web preview with Streamlit Community Cloud

1. Create a GitHub repository and upload `app.py` and `requirements.txt`.
2. Go to https://share.streamlit.io and sign in with GitHub.
3. Create an app, select the repository, and set the entrypoint to `app.py`.
4. Optional but recommended: in App Settings > Secrets, add:

```toml
APP_PASSWORD = "your-private-password"
```

5. Deploy. Streamlit will give you a `*.streamlit.app` URL.

## Important storage note

The default app uses a local SQLite file. This is ideal for trying the app locally and for a demo, but a cloud app can lose local data after a restart/redeploy. Use **Reports & Backup > Download complete database backup** frequently.

For a serious shared deployment, move the database to a persistent service such as Supabase/PostgreSQL before using it as the only copy of your fund records.

## Importing the existing workbook

Open **Import Excel** in the app and upload the current workbook. The importer finds the `Transactions_detailed` sheet, recognizes the columns Date, Amount TJS, From/To, Payment Method, tr_type_name and Comments, and skips rows that were already imported.

Historical loan repayments are allocated FIFO to each member's oldest open loan. Because some old spreadsheet rows combine principal and interest in one transaction/comment, review the reconstructed loan portfolio after the first import.

## Privacy

Do not commit your real Excel workbook, SQLite database, or password/secrets file to a public GitHub repository.

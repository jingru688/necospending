# Household Spending Tracker

Upload credit-card statement PDFs and have transactions recorded automatically.
Claude reads each statement, extracts every purchase (date, merchant, amount,
cardholder, card, guessed category), and stores them in a local SQLite database.
You can edit categories and reassign who spent it, and see dashboards by person,
category, and month.

## Setup

By default the app parses statements using your **Claude subscription** via the
local Claude Code binary — no per-token API cost.

```bash
cd spending-tracker
pip install -r requirements.txt

# one-time login with your Claude subscription (opens a browser):
"$(python -c 'import parser; print(parser.find_claude_bin())')" auth login --claudeai

streamlit run app.py
```

### Optional: API backend instead

To use the Anthropic API (billed per token, ~$0.05–0.15 per statement):

```bash
export SPENDING_BACKEND=api
export ANTHROPIC_API_KEY=sk-ant-...   # https://console.anthropic.com
streamlit run app.py
```

The app opens in your browser. Go to **Upload**, drop in one or more PDFs, and
click **Parse & record**.

- **Transactions** tab: edit category / person, then **Save edits**.
- **People** tab: map the cardholder names on statements to a person. Handy for
  shared accounts or name variants (`JANE A DOE` -> `Jane`). New cardholders are
  auto-attributed by name until you map them.
- **Dashboard** tab: totals by person, category, and month.

Duplicate transactions are skipped automatically (matched on date + amount +
merchant + card), so re-uploading a statement is safe.

## Deploy as an always-on website (both of you, any device)

This gives a real URL like `https://household-spending.streamlit.app` that's
always on (your Mac can be off). It needs the API backend (no Mac in the cloud)
and a durable database. All hosting is free; only the AI parsing costs ~$1-2/mo.

You'll create four free accounts. Do them in order:

### 1. GitHub — stores the code
- Create an account at https://github.com (if you don't have one).
- Create a new **empty** repo, e.g. `spending-tracker` (Private recommended).
- Push this folder to it (see "Push to GitHub" below).

### 2. Neon — free Postgres database (so data persists)
- Sign up at https://neon.tech (you can log in with GitHub).
- Create a project; copy the **connection string** (starts with `postgresql://`).

### 3. Anthropic API key — for parsing in the cloud
- Go to https://console.anthropic.com -> **API Keys** -> create a key.
- Add a little billing credit. Parsing costs a few cents per statement.

### 4. Streamlit Community Cloud — runs the app
- Sign up at https://share.streamlit.io with your GitHub account.
- **New app** -> pick your repo, branch `main`, main file `app.py`.
- Open **Advanced settings -> Secrets** and paste (see `.streamlit/secrets.toml.example`):
  ```toml
  SPENDING_BACKEND = "api"
  ANTHROPIC_API_KEY = "sk-ant-..."
  DATABASE_URL = "postgresql://USER:PASSWORD@HOST/DB?sslmode=require"
  APP_PASSWORD = "our-household-secret"
  ```
- Click **Deploy**. After a minute you get your `*.streamlit.app` URL.

Share the URL + `APP_PASSWORD` with your boyfriend. Both of you open it anywhere,
enter the password, and see the same data. To cut cost ~5x, also add
`SPENDING_MODEL = "claude-haiku-4-5"`.

### Push to GitHub

After creating the empty GitHub repo, from this folder:
```bash
git remote add origin https://github.com/YOUR_USERNAME/spending-tracker.git
git branch -M main
git push -u origin main
```
The `.gitignore` keeps your database and secrets out of the repo.

## Config reference

- `SPENDING_BACKEND` — `cli` (local, uses subscription) or `api` (cloud). Auto-set
  to `api` when an `ANTHROPIC_API_KEY` is present and no backend is chosen.
- `ANTHROPIC_API_KEY` — required for the `api` backend.
- `DATABASE_URL` — Postgres URL for durable cloud storage. Unset = local SQLite.
- `APP_PASSWORD` — password gate. Unset = open (local only; always set before deploying).
- `SPENDING_MODEL` — model override. Empty = backend default.
- `CLAUDE_BIN` — full path to the Claude Code binary, if auto-detection fails.

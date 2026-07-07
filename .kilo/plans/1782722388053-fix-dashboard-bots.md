# Fix: Bots not showing in dashboard

## Root cause
Two issues prevent bots from appearing in the dashboard:

1. **API server is not running** — port 5000 is not listening, so the dashboard (port 5173) cannot reach `http://localhost:5000/api/bots`.
2. **`bots` table is likely empty** — even if the API server starts, the 8 `config_*.yaml` files in `bot/` have never been imported into SQLite. The `init-db.ts` script exists but was never executed.

## Current state
- Dashboard (Vite): running on `:5173` ✓
- API server (Express): **stopped** on `:5000` ✗
- Python bots: none running
- DB file: `data/bot.db` exists (24 KB) — tables may be empty
- Config files present: `config_btc.yaml`, `config_eth.yaml`, `config_bnb.yaml`, `config_sol.yaml`, `config_xrp.yaml`, `config_trx.yaml`, `config_doge.yaml`, `config_ont.yaml`

## Fix steps (execute in order)

### 1. Initialize the database
```powershell
cd C:\DATA\bots\replit_scalper
pnpm --filter @workspace/api-server run init-db
```
This reads all `config_*.yaml` files from `bot/` and inserts/updates rows in the `bots` table.

Expected output: `Added BTCUSDT from config_btc.yaml`, etc. for all 8 symbols.

### 2. Start the API server
```powershell
cd C:\DATA\bots\replit_scalper
pnpm --filter @workspace/api-server run dev
```
The server reads `.env` (PORT=5000, DATABASE_PATH=data/bot.db) and starts listening.

Keep this terminal open. The server must stay running for the dashboard to work.

### 3. Verify in dashboard
Open `http://localhost:5173` — the Bots section should show 8 bot cards (BTC, ETH, BNB, SOL, XRP, TRX, DOGE, ONT).

## Additional checks
- If `init-db` fails, check that `js-yaml` is installed: `pnpm --filter @workspace/api-server install`
- If the API server crashes on startup, check `data/bot.db` permissions and that `PORT=5000` is not blocked
- The API server must be running before the dashboard can display bots — it does not cache bot data locally

## Rollback
If anything goes wrong:
- Stop API server: `Ctrl+C` in its terminal
- Delete DB to re-init: `Remove-Item data/bot.db` then re-run `init-db`
- Config YAML files are untouched — re-running `init-db` is idempotent (updates existing rows)

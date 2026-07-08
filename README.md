# Basket Command Center — self-hosted stock dashboard

A static dashboard (watchlist cards, detailed ticker cards, performance table) that updates
itself with end-of-day market data via GitHub Actions. No API keys, no server, no cost.

## How it works

- `tickers.txt` — your watchlist, one ticker per line
- `scripts/fetch_data.py` — fetches ~430 days of daily bars + market cap / beta / sector
  per ticker from Yahoo Finance (yfinance) and writes `docs/data.json`
- `.github/workflows/update-data.yml` — runs the fetch script every weekday at 21:45 UTC
  (after the US close), and also any time you edit `tickers.txt`
- `docs/index.html` — the dashboard; reads `data.json` and computes all returns client-side

## Setup (one time, ~5 minutes)

1. Create a new repository on GitHub and upload everything in this folder
   (keep the folder structure, including the hidden `.github` folder).
2. Go to **Settings → Pages** → under "Build and deployment", set Source to
   **Deploy from a branch**, branch **main**, folder **/docs**. Save.
3. Go to the **Actions** tab → enable workflows if prompted → open
   **Update market data** → click **Run workflow**. Wait ~2 minutes for it to finish.
4. Your dashboard is live at `https://<your-username>.github.io/<repo-name>/`.
   Bookmark it. Data refreshes automatically every weekday evening.

## Adding / removing tickers

Edit `tickers.txt` directly on github.com (pencil icon), add or delete a line, commit.
The workflow runs automatically and the site reflects it within a few minutes.
The × on a card only hides a ticker in your browser; the "Show" box brings it back.

## Privacy — read this

- **A GitHub Pages site is always publicly reachable by URL**, even if the repository is
  private. Private repo + Pages also requires a paid GitHub plan (Pro).
- If you need the site truly private, host these same files on **Cloudflare Pages** and put
  **Cloudflare Access** (free tier) in front of it — that gates the URL behind a login.
- Don't put anything in `tickers.txt` you wouldn't want public.

## Notes

- Data is end-of-day, not live. GitHub's cron is best-effort: the update usually runs within
  ~15 minutes of the scheduled time, occasionally later.
- Yahoo Finance is an unofficial data source; if a ticker fails, the dashboard shows the
  error for that row and the rest keeps working.
- Prices are unadjusted closes (auto_adjust=False), matching what a chart shows.

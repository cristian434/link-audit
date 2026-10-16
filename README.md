# Broken link audit

Crawls every page in a site's `sitemap.xml`, checks every link, takes a screenshot of each broken link on **desktop (1440 px)** and **mobile (iPhone 13)** with the link outlined in red, and writes a report:

- `report.html` – one self-contained file with screenshots embedded (share or attach it anywhere)
- `report.csv` – same data for Excel / Google Sheets
- `report.json` – raw data
- `screenshots/` – the individual images

For each broken link the report lists the HTTP status, link text, page section, which devices show it (desktop, mobile, both, or hidden), and every page it appears on. Pages listed in the sitemap that don't load are reported too.

## Run it automatically (GitHub Actions)

1. Create a GitHub repository (private is fine) and upload everything in this folder, including the hidden `.github` folder.
2. That's it. The audit runs every Monday at 12:00 UTC. To run it now, open the **Actions** tab, choose **Broken link audit**, and click **Run workflow**.
3. Open a finished run and download the **link-audit-N** file at the bottom of the page. It contains `report.html`.

### Optional settings

Go to the repository's **Settings → Secrets and variables → Actions**.

| Name | Type | What it does |
|---|---|---|
| `SLACK_WEBHOOK_URL` | Secret | Posts a one-line summary to a Slack channel after each run |
| `SITE_URL` | Variable | Audit a different site (default `https://getgoodlab.com`) |
| `CHECK_EXTERNAL` | Variable | `true` to also check outbound links. Sites that block bots are listed separately, not counted as broken |
| `PUBLISH_PAGES` | Variable | `true` to publish the latest report at a fixed URL with GitHub Pages. Enable Pages first (Settings → Pages → Source: GitHub Actions). On a public repo the report is public |

To change the schedule, edit the `cron` line in `.github/workflows/link-audit.yml` (it uses UTC).

## Run it on your computer

Requires Node.js 20 or newer.

```bash
npm install
npx playwright install chromium
SITE_URL=https://getgoodlab.com node audit.mjs
open audit-output/report.html
```

More options: `CONCURRENCY` (default 8), `MAX_PAGES` (default 1000), `SCREENSHOT_PAGES` (screenshots per broken link per device, default 2), `EXTRA_PAGES` (comma-separated paths that aren't in the sitemap), `FAIL_ON_BROKEN=true` (mark the run as failed when anything is broken).

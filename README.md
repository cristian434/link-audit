# Link audit

Crawls every page in a site's `sitemap.xml` and reports three kinds of problems:

1. **Broken links**: links that return an error (404, 401, 500), malformed URLs, and sitemap pages that don't load.
2. **Links to /dev/ (staging)**: every link into the Webflow staging folder, whatever it returns. Change the folder with `STAGING_PATHS`.
3. **Buttons and link boxes without a link**: links with no address, placeholder addresses (`#`, empty, `javascript:void(0)`), and button-styled boxes with nothing to click through to. Menus, tabs, sliders, lightboxes, popups and form buttons are skipped because scripts make them clickable.

For every item the report shows the link or button text, the page section, which devices show it (desktop, mobile, both, or hidden), every page it appears on, and screenshots on **desktop (1440 px)** and **mobile (iPhone 13)** with the element outlined in yellow.

Output, in `audit-output/`:

- `report.html` – one self-contained file with screenshots embedded, styled with the brand palette and fonts
- `report.csv` – same data for Excel / Google Sheets
- `report.json` – raw data
- `screenshots/` – the individual images

To change the report colors, edit the variables at the top of the `<style>` block in `audit.mjs` (search for "Brand palette").

## Run it automatically (GitHub Actions)

1. Create a GitHub repository (private is fine) and upload everything in this folder, including the hidden `.github` folder.
2. That's it. The audit runs every Monday at 12:00 UTC. To run it now, open the **Actions** tab, choose **Link audit**, and click **Run workflow**.
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

More options: `STAGING_PATHS` (comma-separated staging folders, default `/dev/`), `CONCURRENCY` (default 8), `MAX_PAGES` (default 1000), `SCREENSHOT_PAGES` (screenshots per broken link per device, default 2), `EXTRA_PAGES` (comma-separated paths that aren't in the sitemap), `FAIL_ON_BROKEN=true` (mark the run as failed when broken or /dev/ links exist).

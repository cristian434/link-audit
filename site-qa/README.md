# Site QA — live page vs Figma vs copy doc

One run = three links in, one HTML report out.

| Area | What it checks |
|---|---|
| Layout vs Figma | Each Figma section is found on the live page and shown side by side (with an overlay slider). Flags sections whose height differs >15%, sections that look visually different, design text missing on live, and Figma images/videos missing in the live section. |
| Typography & color | Every Figma text ≥10px is matched to the live element and compared: font family, weight, size (±1px), line height (±2px), letter spacing (±0.3px), color, italic and uppercase. |
| Images & video | Broken images, stretched/squashed images and videos, upscaled (blurry) images, missing alt, autoplay videos that don't play, aren't muted or lack `playsinline` on mobile, assets that fail to load. |
| Display | Horizontal scroll, elements past the screen edge, text clipped by its container, small tap targets on mobile, content hidden on load. Runs at the Figma width (desktop) and 390px (mobile, or the Figma mobile frame width). |
| Copy vs doc | Every line of the copy doc is matched to the live page: OK, punctuation-only, different (word diff), or missing. Also lists long live paragraphs that aren't in the doc. Labels like `H1:`, `Eyebrow:`, `CTA:` and notes in `[brackets]` are ignored. |
| Links & buttons | Links with no destination (`#`, empty, `javascript:`), broken links (4xx/5xx/no response), anchor links to ids that don't exist, malformed mailto/tel, unencoded spaces, redirects, and `<button>`s without a link to confirm. |

## Setup (once)

1. Copy `site_qa/` and `.github/workflows/site-qa.yml` into a GitHub repo (your `link-audit` repo works).
2. Figma token: Figma → Settings → Security → Personal access tokens → scope **File content: read**. Add it in the repo: Settings → Secrets and variables → Actions → New secret → `FIGMA_TOKEN`.
3. Copy doc access, pick one:
   - Share the Google Doc as **Anyone with the link → Viewer** (simplest), or
   - Create a Google Cloud service account with the Drive API enabled, share the doc with its email, and save its JSON key as the secret `GOOGLE_SA_JSON`.

## Run it

Actions tab → **Site QA (Figma + copy + links)** → **Run workflow** → paste the links → Run.
When it finishes, the run page shows a summary; download **site-qa-report** and open `report.html`.

The Figma link can point at the full-page frame or at any layer next to it (like the "COPY here" note): the tool walks up to the section and picks the page-sized frame closest to that layer. If a section holds two versions of the page, link a layer of the one you want.

Run locally instead:

```bash
pip install -r site_qa/requirements.txt && python -m playwright install chromium
export FIGMA_TOKEN=...
python site_qa/site_qa.py --site URL --figma URL --doc URL --out report
```

Optional: set `FAIL_ON_HIGH=true` to make the run fail when there are high-severity issues.

## Reading the results

- Visual match % is a rough structural score — use it to decide which sections to look at, then judge with the side-by-side / overlay. Sliders, CMS content and animations lower it.
- Elements hidden on load (FAQ answers, tabs) are reported as "hidden", not missing.
- LinkedIn and some social sites block bots (status 999/403): those links are listed as "verify manually".
- Tolerances are at the top of `site_qa.py` (`TOL`, `MIN_FIGMA_FONT`, `MATCH_OK`, `MATCH_DIFF`).

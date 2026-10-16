// Broken link audit: crawls the sitemap, checks every link, screenshots each
// broken link on desktop and mobile, and writes report.html, report.json and report.csv.
//
// Usage:  SITE_URL=https://getgoodlab.com node audit.mjs
// Options (environment variables):
//   SITE_URL              Site to audit (default https://getgoodlab.com)
//   OUT_DIR               Output folder (default ./audit-output)
//   CONCURRENCY           Parallel requests (default 8)
//   MAX_PAGES             Cap on pages crawled (default 1000)
//   CHECK_EXTERNAL        "true" to also check outbound links (default false)
//   SCREENSHOT_PAGES      Pages screenshotted per broken link (default 2)
//   EXTRA_PAGES           Comma-separated paths to crawl besides the sitemap
//   SLACK_WEBHOOK_URL     If set, posts a summary to Slack when done
//   FAIL_ON_BROKEN        "true" to exit with code 1 when broken links exist

import { parse } from 'node-html-parser';
import { chromium, devices } from 'playwright';
import fs from 'node:fs/promises';
import path from 'node:path';

const SITE = (process.env.SITE_URL || 'https://getgoodlab.com').replace(/\/$/, '');
const ORIGIN = new URL(SITE).origin;
const HOST = new URL(SITE).hostname.replace(/^www\./, '');
const OUT = process.env.OUT_DIR || './audit-output';
const CONC = +process.env.CONCURRENCY || 8;
const MAX_PAGES = +process.env.MAX_PAGES || 1000;
const CHECK_EXTERNAL = process.env.CHECK_EXTERNAL === 'true';
const SHOT_PAGES = +(process.env.SCREENSHOT_PAGES ?? 2);
const UA = 'Mozilla/5.0 (compatible; LinkAuditBot/1.0; +internal-site-audit)';

const DEVICES = {
  Desktop: { viewport: { width: 1440, height: 900 } },
  Mobile: { ...devices['iPhone 13'] },
};

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const isInternal = (u) => { try { return new URL(u).hostname.replace(/^www\./, '') === HOST; } catch { return false; } };
const strip = (u) => u.split('#')[0];

async function pool(items, n, fn) {
  let i = 0;
  const out = new Array(items.length);
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
    while (i < items.length) { const k = i++; out[k] = await fn(items[k], k); }
  }));
  return out;
}

async function get(url, { timeout = 20000, method = 'GET' } = {}) {
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const r = await fetch(url, { method, redirect: 'follow', headers: { 'user-agent': UA }, signal: AbortSignal.timeout(timeout) });
      if (r.status === 429 || r.status >= 500) { await new Promise((s) => setTimeout(s, 1500 * (attempt + 1))); if (attempt < 2) continue; }
      return r;
    } catch (e) { if (attempt === 2) throw e; await new Promise((s) => setTimeout(s, 1000)); }
  }
}

// ---------- 1. Discover pages ----------
async function sitemapUrls(url, seen = new Set()) {
  if (seen.has(url)) return [];
  seen.add(url);
  try {
    const r = await get(url);
    if (!r.ok) return [];
    const xml = await r.text();
    const locs = [...xml.matchAll(/<loc>\s*([^<]+?)\s*<\/loc>/g)].map((m) => m[1].replace(/&amp;/g, '&'));
    if (/<sitemapindex/i.test(xml)) return (await Promise.all(locs.map((l) => sitemapUrls(l, seen)))).flat();
    return locs;
  } catch { return []; }
}

// ---------- 2. Crawl pages and collect links ----------
function sectionOf(a) {
  let el = a.parentNode;
  while (el && el.tagName) {
    const tag = el.tagName.toLowerCase();
    const cls = (el.getAttribute('class') || '').trim().split(/\s+/)[0];
    if (['nav', 'header', 'footer', 'section', 'main', 'aside'].includes(tag)) return cls ? `${tag}.${cls}` : tag;
    el = el.parentNode;
  }
  return 'page body';
}

async function crawl(pages) {
  const links = new Map(); // url -> [{page, text, href, section}]
  const pageStatus = {};
  await pool(pages, CONC, async (page) => {
    try {
      const r = await get(page);
      pageStatus[page] = r.status;
      if (!r.ok || !(r.headers.get('content-type') || '').includes('html')) return;
      const root = parse(await r.text());
      for (const a of root.querySelectorAll('a[href]')) {
        const href = (a.getAttribute('href') || '').trim();
        if (!href || href.startsWith('#') || /^(mailto|tel|javascript|sms):/i.test(href)) continue;
        let abs;
        try { abs = new URL(href, page).href; } catch { abs = `INVALID:${href}`; }
        const text = (a.text || a.getAttribute('aria-label') || a.querySelector('img')?.getAttribute('alt') || '').replace(/\s+/g, ' ').trim().slice(0, 120);
        if (!links.has(abs)) links.set(abs, []);
        links.get(abs).push({ page, text, href, section: sectionOf(a) });
      }
    } catch (e) { pageStatus[page] = `ERR ${e.message}`; }
  });
  return { links, pageStatus };
}

// ---------- 3. Check links ----------
async function check(urls) {
  const res = {};
  await pool(urls, CONC, async (u) => {
    if (u.startsWith('INVALID:')) { res[u] = { status: 'invalid' }; return; }
    try {
      let r = await get(strip(u), { method: isInternal(u) ? 'GET' : 'HEAD' });
      if (!isInternal(u) && (r.status === 405 || r.status === 403)) r = await get(strip(u));
      res[u] = { status: r.status, final: r.url, redirected: r.redirected };
    } catch (e) { res[u] = { status: 'error', error: e.message }; }
  });
  return res;
}

// Sites that block bots answer 401/403/429/999 to automated requests even when the page works.
function classify(u, s) {
  if (s.status === 'invalid') return 'broken';
  if (typeof s.status === 'number' && s.status < 400) return 'ok';
  if (!isInternal(u) && [401, 403, 429, 999].includes(s.status)) return 'blocked';
  if (!isInternal(u) && s.status === 'error') return 'unreachable';
  return 'broken';
}

// ---------- 4. Screenshots + device visibility ----------
async function screenshots(broken, shotDir) {
  let shotCounter = 0;
  await fs.mkdir(shotDir, { recursive: true });
  const browser = await chromium.launch();
  const contexts = {};
  for (const [name, opts] of Object.entries(DEVICES)) contexts[name] = await browser.newContext({ ...opts, userAgent: undefined });
  const byPage = new Map(); // page -> [broken items]
  broken.forEach((b, i) => {
    b.id = i + 1;
    b.devices = {};
    b.shots = [];
    b.reserved = {};
    for (const p of b.pages.slice(0, Math.max(SHOT_PAGES, 1))) {
      if (!byPage.has(p)) byPage.set(p, []);
      byPage.get(p).push(b);
    }
  });

  const entries = [...byPage.entries()];
  for (const [devName, ctx] of Object.entries(contexts)) {
    await pool(entries, 3, async ([pageUrl, items]) => {
      const page = await ctx.newPage();
      try {
        await page.goto(pageUrl, { waitUntil: 'domcontentloaded', timeout: 45000 });
        await page.waitForTimeout(1500);
        await page.evaluate(() => document.querySelectorAll('[class*="cookie"],[id*="cookie"],[class*="consent"]').forEach((e) => e.remove()));
        for (const b of items) {
          const target = strip(b.url);
          const info = await page.evaluate((target) => {
            const as = [...document.querySelectorAll('a[href]')].filter((a) => { try { return a.href.split('#')[0] === target; } catch { return false; } });
            const vis = as.filter((a) => { const r = a.getBoundingClientRect(); const cs = getComputedStyle(a); return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; });
            as.forEach((a) => a.removeAttribute('data-audit-target'));
            if (vis[0]) vis[0].setAttribute('data-audit-target', '1');
            return { total: as.length, visible: vis.length };
          }, target);
          const rec = (b.devices[devName] ||= { visible: 0, total: 0 });
          rec.visible += info.visible; rec.total += info.total;
          if (!info.visible || (b.reserved[devName] = (b.reserved[devName] || 0) + 1) > SHOT_PAGES) continue;
          const el = page.locator('[data-audit-target="1"]').first();
          await el.evaluate((a) => {
            a.scrollIntoView({ block: 'center', inline: 'center' });
            a.style.outline = '4px solid #E11D48'; a.style.outlineOffset = '3px'; a.style.background = 'rgba(225,29,72,.12)';
          });
          await page.waitForTimeout(500);
          const file = `link${b.id}-${devName.toLowerCase()}-${++shotCounter}.jpg`;
          // Viewport capture with the link centered: shows the surrounding section on that device.
          await page.screenshot({ path: path.join(shotDir, file), type: 'jpeg', quality: 70, scale: 'css' });
          await el.evaluate((a) => { a.style.outline = ''; a.style.background = ''; a.removeAttribute('data-audit-target'); });
          b.shots.push({ device: devName, page: pageUrl, file });
        }
      } catch (e) {
        log(`  screenshot failed on ${pageUrl} (${devName}): ${e.message.split('\n')[0]}`);
      } finally { await page.close(); }
    });
  }
  await browser.close();
}

// ---------- 5. Report ----------
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const rel = (u) => (isInternal(u) ? u.replace(ORIGIN, '') || '/' : u);

function deviceLabel(b) {
  if (b.sitemapOnly) return 'Page itself fails';
  if (!b.devices) return 'Not checked';
  const seen = Object.entries(b.devices).filter(([, v]) => v.visible > 0).map(([k]) => k);
  if (!Object.keys(b.devices).length) return 'Not checked';
  if (!seen.length) return 'Hidden on all devices (still crawled by Google)';
  return seen.length === Object.keys(DEVICES).length ? 'Desktop and mobile' : `${seen.join(', ')} only`;
}

async function writeReport(data, shotDir) {
  const { broken, pages, affectedPages, totalInstances, checked, blocked, startedAt } = data;
  const imgs = {};
  for (const b of broken) for (const s of b.shots || []) imgs[s.file] = (await fs.readFile(path.join(shotDir, s.file))).toString('base64');

  const rows = broken.map((b) => `
  <details class="issue ${isInternal(b.url) ? 'int' : 'ext'}" data-q="${esc((b.url + ' ' + b.texts.join(' ') + ' ' + b.pages.join(' ')).toLowerCase())}">
    <summary>
      <span class="st">${esc(b.status)}</span>
      <span class="u"><code>${esc(rel(b.url))}</code><small>${esc(b.texts.slice(0, 2).join(' / ') || '(no link text)')}</small></span>
      <span class="dv">${esc(deviceLabel(b))}</span>
      <span class="pc"><b>${b.pages.length}</b> page${b.pages.length > 1 ? 's' : ''}</span>
    </summary>
    <div class="body">
      <p><strong>Broken URL:</strong> <a href="${esc(b.url)}" target="_blank" rel="noopener">${esc(b.url)}</a></p>
      <p><strong>Section:</strong> ${esc(b.sections.join(', '))}</p>
      <p><strong>Found on ${b.pages.length} page(s):</strong></p>
      <ul>${b.pages.map((p) => `<li><a href="${esc(p)}" target="_blank" rel="noopener">${esc(rel(p))}</a></li>`).join('')}</ul>
      ${b.shots?.length ? `<div class="shots">${b.shots.map((s) => `<figure><img loading="lazy" alt="${esc(s.device)} screenshot of the broken link on ${esc(rel(s.page))}" src="data:image/jpeg;base64,${imgs[s.file]}"><figcaption>${esc(s.device)} · ${esc(rel(s.page))}</figcaption></figure>`).join('')}</div>` : (b.sitemapOnly ? '<p class="muted">This page is listed in sitemap.xml but does not load. Remove it from the sitemap or restore it.</p>' : '<p class="muted">No screenshot: the link is not visible on any device viewport.</p>')}
    </div>
  </details>`).join('');

  const html = `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Broken link audit: ${esc(HOST)}</title>
<style>
:root{--bg:#F6F8F6;--card:#fff;--ink:#13241D;--mut:#56665F;--line:#DCE3DE;--red:#B42318;--redbg:#FCE9E6;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0F1814;--card:#16221C;--ink:#E7EFEA;--mut:#9DB0A6;--line:#2A3A32;--red:#FF8A7A;--redbg:#3A1D19;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.w{max-width:1100px;margin:0 auto;padding:40px 20px}h1{font-size:clamp(28px,4vw,44px);line-height:1.1;margin:0 0 10px}
.mut,.muted{color:var(--mut)}a{color:inherit}code{font-family:ui-monospace,Menlo,monospace;font-size:.9em;overflow-wrap:anywhere}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:28px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}.stat b{display:block;font-size:28px}
input{font:inherit;width:100%;max-width:420px;padding:10px 14px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);margin:0 0 16px}
.issue{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:8px}
summary{cursor:pointer;display:grid;grid-template-columns:70px minmax(0,1fr) 190px 80px;gap:12px;align-items:center;padding:12px 16px;list-style:none}
summary::-webkit-details-marker{display:none}
.st{background:var(--redbg);color:var(--red);font-weight:700;text-align:center;border-radius:5px;padding:2px 0;font-size:14px}
.u small{display:block;color:var(--mut);font-size:14px}.dv,.pc{font-size:14px;color:var(--mut)}.pc b{color:var(--ink);font-size:17px}
.body{padding:0 16px 16px}.body ul{margin:4px 0 12px;padding-left:20px}
.shots{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
figure{margin:0;border:1px solid var(--line);border-radius:8px;overflow:hidden}figure img{display:block;width:100%;height:auto}
figcaption{font-size:13px;color:var(--mut);padding:6px 10px}
@media (max-width:700px){summary{grid-template-columns:60px 1fr}.dv,.pc{grid-column:2}}
</style></head><body><div class="w">
<p class="mut">Broken link audit of <a href="${esc(SITE)}">${esc(HOST)}</a> · run ${esc(startedAt)}</p>
<h1>${affectedPages} of ${pages} pages have a broken link</h1>
<p class="mut">${broken.length} broken URL(s), appearing ${totalInstances} times. ${checked} unique links checked${blocked ? `; ${blocked} external link(s) refused automated checks and need a manual look` : ''}.</p>
<div class="stats">
<div class="stat"><b>${pages}</b>pages crawled</div><div class="stat"><b>${broken.length}</b>broken URLs</div>
<div class="stat"><b>${affectedPages}</b>pages affected</div><div class="stat"><b>${totalInstances}</b>broken link instances</div></div>
<input type="search" id="q" placeholder="Filter by URL, link text or page" aria-label="Filter broken links">
${rows || '<p>No broken links found.</p>'}
</div><script>document.getElementById('q').oninput=e=>{const q=e.target.value.toLowerCase();document.querySelectorAll('.issue').forEach(d=>d.style.display=d.dataset.q.includes(q)?'':'none')}</script></body></html>`;
  await fs.writeFile(path.join(OUT, 'report.html'), html);

  const csv = [['status', 'broken_url', 'link_text', 'section', 'devices', 'pages_affected', 'pages']]
    .concat(broken.map((b) => [b.status, b.url, b.texts.join(' | '), b.sections.join(' | '), deviceLabel(b), b.pages.length, b.pages.join(' | ')]))
    .map((r) => r.map((c) => `"${String(c).replace(/"/g, '""')}"`).join(',')).join('\n');
  await fs.writeFile(path.join(OUT, 'report.csv'), csv);
  await fs.writeFile(path.join(OUT, 'report.json'), JSON.stringify(data, null, 2));
}

async function notifySlack(summary) {
  if (!process.env.SLACK_WEBHOOK_URL) return;
  await fetch(process.env.SLACK_WEBHOOK_URL, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ text: summary }) })
    .catch((e) => log('Slack notify failed:', e.message));
}

// ---------- Main ----------
const startedAt = new Date().toISOString().replace('T', ' ').slice(0, 16) + ' UTC';
await fs.mkdir(OUT, { recursive: true });
log(`Auditing ${SITE}`);

let pages = await sitemapUrls(`${ORIGIN}/sitemap.xml`);
const extra = (process.env.EXTRA_PAGES || '').split(',').map((s) => s.trim()).filter(Boolean).map((p) => new URL(p, ORIGIN).href);
pages = [...new Set([`${ORIGIN}/`, ...pages, ...extra].filter(isInternal))].slice(0, MAX_PAGES);
log(`${pages.length} pages to crawl`);

const { links, pageStatus } = await crawl(pages);
const toCheck = [...links.keys()].filter((u) => isInternal(u) || u.startsWith('INVALID:') || (CHECK_EXTERNAL && /^https?:/.test(u)));
log(`${links.size} unique links found, checking ${toCheck.length}`);
const status = await check(toCheck);

const broken = [];
let blocked = 0;
for (const u of toCheck) {
  const kind = classify(u, status[u]);
  if (kind === 'blocked' || kind === 'unreachable') { blocked++; continue; }
  if (kind !== 'broken') continue;
  const occ = links.get(u);
  broken.push({
    url: u, status: status[u].status,
    pages: [...new Set(occ.map((o) => o.page))],
    texts: [...new Set(occ.map((o) => o.text).filter(Boolean))],
    sections: [...new Set(occ.map((o) => o.section))],
    instances: occ.length,
  });
}
// Pages in the sitemap that themselves fail
for (const [p, s] of Object.entries(pageStatus)) if (!(typeof s === 'number' && s < 400)) broken.push({ url: p, status: s, pages: ['(listed in sitemap.xml)'], texts: ['Sitemap entry'], sections: ['sitemap.xml'], instances: 1, sitemapOnly: true });
broken.sort((a, b) => b.pages.length - a.pages.length);
log(`${broken.length} broken URL(s)`);

const shotDir = path.join(OUT, 'screenshots');
const shootable = broken.filter((b) => !b.sitemapOnly);
if (shootable.length) { log('Taking screenshots…'); await screenshots(shootable, shotDir); }

const affected = new Set(shootable.flatMap((b) => b.pages));
const data = {
  site: SITE, startedAt, pages: pages.length, checked: toCheck.length, blocked,
  affectedPages: affected.size, totalInstances: shootable.reduce((n, b) => n + b.instances, 0), broken,
};
await writeReport(data, shotDir);
const summary = `Link audit for ${HOST}: ${broken.length} broken URL(s) on ${affected.size} of ${pages.length} pages.`;
log(summary, `Report: ${path.join(OUT, 'report.html')}`);
await notifySlack(summary + (process.env.REPORT_URL ? ` Report: ${process.env.REPORT_URL}` : ''));
if (process.env.FAIL_ON_BROKEN === 'true' && broken.length) process.exit(1);

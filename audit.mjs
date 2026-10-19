// Link audit: crawls the sitemap and reports three kinds of problems, each with
// desktop and mobile screenshots, device visibility and every page affected:
//   1. Broken links (404, 401, 500, malformed URLs, dead sitemap pages)
//   2. Staging links that point into /dev/ (or other STAGING_PATHS)
//   3. Buttons and link boxes with no link (no href, "#", empty or javascript: href,
//      or button-styled elements with nothing to click through to)
// Writes report.html (self-contained, screenshots embedded), report.csv and report.json.
//
// Usage:  SITE_URL=https://getgoodlab.com node audit.mjs
// Options (environment variables):
//   SITE_URL              Site to audit (default https://getgoodlab.com)
//   OUT_DIR               Output folder (default ./audit-output)
//   CONCURRENCY           Parallel requests (default 8)
//   MAX_PAGES             Cap on pages crawled (default 1000)
//   CHECK_EXTERNAL        "true" to also check outbound links (default false)
//   SCREENSHOT_PAGES      Pages screenshotted per item per device (default 2)
//   STAGING_PATHS         Comma-separated path prefixes treated as staging (default /dev/)
//   EXTRA_PAGES           Comma-separated paths to crawl besides the sitemap
//   SLACK_WEBHOOK_URL     If set, posts a summary to Slack when done
//   FAIL_ON_BROKEN        "true" to exit with code 1 when broken or staging links exist

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
const STAGING = (process.env.STAGING_PATHS || '/dev/').split(',').map((s) => s.trim()).filter(Boolean);
const UA = 'Mozilla/5.0 (compatible; LinkAuditBot/1.0; +internal-site-audit)';
const HIGHLIGHT = '#F6A017'; // brand yellow outline drawn around each item in screenshots

const DEVICES = {
  Desktop: { viewport: { width: 1440, height: 900 } },
  Mobile: { ...devices['iPhone 13'] },
};

const CATEGORIES = {
  broken: { title: 'Broken links', blurb: 'Links that return an error (404 Not Found, 401, 500), malformed URLs, and pages in the sitemap that fail to load.' },
  staging: { title: 'Links to /dev/ (staging)', blurb: 'Links that point into the staging folder. Visitors either get an error or land on an unfinished page. Relink them to the live page.' },
  nolink: { title: 'Buttons and link boxes without a link', blurb: 'Elements that look clickable but go nowhere: links with no address, placeholder "#" or empty addresses, and button-styled boxes with no link inside.' },
};

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const isInternal = (u) => { try { return new URL(u).hostname.replace(/^www\./, '') === HOST; } catch { return false; } };
const isStaging = (u) => { try { const p = new URL(u).pathname; return isInternal(u) && STAGING.some((s) => p === s.replace(/\/$/, '') || p.startsWith(s)); } catch { return false; } };
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
      if ((r.status === 429 || r.status >= 500) && attempt < 2) { await new Promise((s) => setTimeout(s, 1500 * (attempt + 1))); continue; }
      return r;
    } catch (e) { if (attempt === 2) throw e; await new Promise((s) => setTimeout(s, 1000)); }
  }
}

// ---------- Detection of buttons / link boxes without a link ----------
// Runs both in Node (node-html-parser) and in the browser (real DOM), so it only uses
// tagName, getAttribute, parentNode and querySelector. Returns [{el, kind, sig, text}].
function findNoLink(root) {
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const tag = (el) => String(el.tagName || '').toLowerCase();
  const cls = (el) => norm(el.getAttribute && el.getAttribute('class'));
  // Join text nodes with spaces so "<h3>CDP</h3><p>Learn more</p>" reads "CDP Learn more".
  const collect = (n) => (n.nodeType === 3 ? String(n.textContent != null ? n.textContent : n.text) : [...(n.childNodes || [])].map(collect).join(' '));
  const textOf = (el) => norm(collect(el)) ||
    norm(el.getAttribute('aria-label')) || norm(el.querySelector('img') && el.querySelector('img').getAttribute('alt'));
  const hasAncestor = (el, test) => { let p = el.parentNode; while (p && p.tagName) { if (test(p)) return true; p = p.parentNode; } return false; };
  // Things that are clickable through scripts on purpose (menus, tabs, lightboxes, sliders, popups).
  const INTERACTIVE = /(^|\s)(w-dropdown|w-nav|w-tab|w-lightbox|w-slider|w-icon-slider|w-embed|w-condition|w-form)|dropdown|menu-button|nav-button|hamburger|accordion|tab-link|lightbox|modal|popup|close|toggle|slider|swiper|faq/i;
  const scripted = (el) => el.getAttribute('aria-controls') != null || el.getAttribute('aria-expanded') != null ||
    el.getAttribute('data-w-id') != null || el.getAttribute('onclick') != null || ['tab', 'menuitem'].includes(el.getAttribute('role')) ||
    INTERACTIVE.test(cls(el)) || hasAncestor(el, (p) => INTERACTIVE.test(cls(p)) || tag(p) === 'form');
  const out = [];
  const push = (el, kind) => {
    const text = textOf(el).slice(0, 80);
    const c = cls(el).split(' ').slice(0, 2).join('.');
    out.push({ el, kind, text, sig: `${kind}|${tag(el)}${c ? '.' + c : ''}|${text}` });
  };
  for (const a of root.querySelectorAll('a')) {
    if (scripted(a)) continue;
    const href = a.getAttribute('href');
    if (href == null) { push(a, 'No link set'); continue; }
    const h = norm(href);
    if (h === '' || h === '#' || /^javascript:\s*(void\(0\)|;)?\s*;?$/i.test(h)) push(a, `Placeholder link (${h === '' ? 'empty' : h})`);
  }
  const BUTTONISH = /(^|[\s_-])(button|btn|cta)([\s_-]|$)/i;
  for (const el of root.querySelectorAll('div, span, p, li, section, figure')) {
    const c = cls(el);
    if (!c || !BUTTONISH.test(c) || scripted(el)) continue;
    if (/(^|[\s_-])(text|icon|label|wrapper|wrap|group|row|list|container|holder|inner|arrow)([\s_-]|$)/i.test(c)) continue;
    if (el.getAttribute('role') === 'button') continue;
    if (el.querySelector('a, button, input, select, textarea')) continue;
    if (hasAncestor(el, (p) => ['a', 'button', 'label'].includes(tag(p)))) continue;
    if (!textOf(el)) continue;
    push(el, 'Button with no link');
  }
  return out;
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

// ---------- 2. Crawl pages ----------
function sectionOf(el) {
  let p = el.parentNode;
  while (p && p.tagName) {
    const tag = p.tagName.toLowerCase();
    const cls = (p.getAttribute('class') || '').trim().split(/\s+/)[0];
    if (['nav', 'header', 'footer', 'section', 'main', 'aside'].includes(tag)) return cls ? `${tag}.${cls}` : tag;
    p = p.parentNode;
  }
  return 'page body';
}

async function crawl(pages) {
  const links = new Map();   // url -> [{page, text, href, section}]
  const noLinks = new Map(); // sig -> [{page, text, kind, section}]
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
      for (const n of findNoLink(root)) {
        if (!noLinks.has(n.sig)) noLinks.set(n.sig, []);
        noLinks.get(n.sig).push({ page, text: n.text, kind: n.kind, section: sectionOf(n.el) });
      }
    } catch (e) { pageStatus[page] = `ERR ${e.message}`; }
  });
  return { links, noLinks, pageStatus };
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
async function screenshots(items, shotDir) {
  let shotCounter = 0;
  await fs.mkdir(shotDir, { recursive: true });
  const browser = await chromium.launch();
  const byPage = new Map();
  for (const it of items) {
    it.devices = {}; it.shots = []; it.reserved = {};
    for (const p of it.pages.slice(0, Math.max(SHOT_PAGES, 1))) {
      if (!byPage.has(p)) byPage.set(p, []);
      byPage.get(p).push(it);
    }
  }
  const finder = findNoLink.toString();
  for (const [devName, opts] of Object.entries(DEVICES)) {
    const ctx = await browser.newContext(opts);
    await pool([...byPage.entries()], 3, async ([pageUrl, list]) => {
      const page = await ctx.newPage();
      try {
        await page.goto(pageUrl, { waitUntil: 'domcontentloaded', timeout: 45000 });
        await page.waitForTimeout(1500);
        await page.evaluate(() => document.querySelectorAll('[class*="cookie"],[id*="cookie"],[class*="consent"]').forEach((e) => e.remove()));
        for (const it of list) {
          const info = await page.evaluate(({ target, sig, finder }) => {
            document.querySelectorAll('[data-audit-target]').forEach((e) => e.removeAttribute('data-audit-target'));
            let els;
            if (sig) {
              const f = (0, eval)('(' + finder + ')');
              els = f(document).filter((n) => n.sig === sig).map((n) => n.el);
            } else {
              els = [...document.querySelectorAll('a[href]')].filter((a) => { try { return a.href.split('#')[0] === target; } catch { return false; } });
            }
            const vis = els.filter((a) => { const r = a.getBoundingClientRect(); const cs = getComputedStyle(a); return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && +cs.opacity !== 0; });
            if (vis[0]) vis[0].setAttribute('data-audit-target', '1');
            return { total: els.length, visible: vis.length };
          }, { target: it.url ? strip(it.url) : null, sig: it.sig || null, finder });
          const rec = (it.devices[devName] ||= { visible: 0, total: 0 });
          rec.visible += info.visible; rec.total += info.total;
          if (!info.visible || (it.reserved[devName] = (it.reserved[devName] || 0) + 1) > SHOT_PAGES) continue;
          const el = page.locator('[data-audit-target="1"]').first();
          await el.evaluate((a, color) => {
            a.scrollIntoView({ block: 'center', inline: 'center' });
            a.dataset.auditOutline = a.style.outline; a.dataset.auditOffset = a.style.outlineOffset;
            a.style.outline = `4px solid ${color}`; a.style.outlineOffset = '3px';
          }, HIGHLIGHT);
          await page.waitForTimeout(500);
          const file = `${it.category}${it.id}-${devName.toLowerCase()}-${++shotCounter}.jpg`;
          // Viewport capture with the item centered: shows the surrounding section on that device.
          await page.screenshot({ path: path.join(shotDir, file), type: 'jpeg', quality: 70, scale: 'css' });
          await el.evaluate((a) => { a.style.outline = a.dataset.auditOutline || ''; a.style.outlineOffset = a.dataset.auditOffset || ''; a.removeAttribute('data-audit-target'); });
          it.shots.push({ device: devName, page: pageUrl, file });
        }
      } catch (e) {
        log(`  screenshot failed on ${pageUrl} (${devName}): ${e.message.split('\n')[0]}`);
      } finally { await page.close(); }
    });
    await ctx.close();
  }
  await browser.close();
}

// ---------- 5. Report ----------
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const rel = (u) => (isInternal(u) ? u.replace(ORIGIN, '') || '/' : u);

function deviceLabel(it) {
  if (it.sitemapOnly) return 'Page itself fails';
  if (!it.devices || !Object.keys(it.devices).length) return 'Not checked';
  const seen = Object.entries(it.devices).filter(([, v]) => v.visible > 0).map(([k]) => k);
  if (!seen.length) return 'Hidden on all devices';
  return seen.length === Object.keys(DEVICES).length ? 'Desktop and mobile' : `${seen.join(', ')} only`;
}

function itemTitle(it) {
  return it.category === 'nolink' ? (it.texts[0] || '(no text)') : rel(it.url);
}

async function writeReport(data, shotDir) {
  const { items, pages, startedAt } = data;
  const imgs = {};
  for (const it of items) for (const s of it.shots || []) imgs[s.file] = (await fs.readFile(path.join(shotDir, s.file))).toString('base64');

  const count = (cat) => items.filter((i) => i.category === cat);
  const pagesIn = (list) => new Set(list.filter((i) => !i.sitemapOnly).flatMap((i) => i.pages)).size;
  const allAffected = pagesIn(items);

  const card = (it) => `
  <details class="issue" data-cat="${it.category}" data-q="${esc((itemTitle(it) + ' ' + (it.url || '') + ' ' + it.texts.join(' ') + ' ' + it.pages.join(' ')).toLowerCase())}">
    <summary>
      <span class="chip c-${it.category}">${esc(it.category === 'nolink' ? it.kind.replace(/ \(.*/, '') : it.status)}</span>
      <span class="u"><code>${esc(itemTitle(it))}</code><small>${esc(it.category === 'nolink' ? it.kind + ' · ' + it.element : (it.texts.slice(0, 2).join(' / ') || '(no link text)'))}</small></span>
      <span class="dv">${esc(deviceLabel(it))}</span>
      <span class="pc"><b>${it.pages.length}</b> page${it.pages.length === 1 ? '' : 's'}</span>
    </summary>
    <div class="body">
      ${it.url ? `<p><strong>Link:</strong> <a href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.url)}</a>${it.category === 'staging' ? ` <span class="muted">(returns ${esc(it.status)})</span>` : ''}</p>` : `<p><strong>Problem:</strong> ${esc(it.kind)}. Add the destination link, or remove the button if it isn't needed.</p>`}
      <p><strong>Section:</strong> ${esc(it.sections.join(', '))}</p>
      <p><strong>Devices:</strong> ${esc(deviceLabel(it))}</p>
      <p><strong>Found on ${it.pages.length} page${it.pages.length === 1 ? '' : 's'}:</strong></p>
      <ul>${it.pages.map((p) => (p.startsWith('http') ? `<li><a href="${esc(p)}" target="_blank" rel="noopener">${esc(rel(p))}</a></li>` : `<li>${esc(p)}</li>`)).join('')}</ul>
      ${it.shots?.length ? `<div class="shots">${it.shots.map((s) => `<figure><img loading="lazy" alt="${esc(s.device)} screenshot on ${esc(rel(s.page))}" src="data:image/jpeg;base64,${imgs[s.file]}"><figcaption><b>${esc(s.device)}</b> · ${esc(rel(s.page))}</figcaption></figure>`).join('')}</div>`
        : `<p class="muted">${it.sitemapOnly ? 'This page is listed in sitemap.xml but does not load. Remove it from the sitemap or restore it.' : 'No screenshot: the element is not visible on any device, but search engines still see it.'}</p>`}
    </div>
  </details>`;

  const section = (cat) => {
    const list = count(cat);
    return `<section class="cat" id="${cat}">
      <div class="cat-head"><h2>${CATEGORIES[cat].title}</h2><span class="cat-n">${list.length} item${list.length === 1 ? '' : 's'} · ${pagesIn(list)} page${pagesIn(list) === 1 ? '' : 's'}</span></div>
      <p class="muted">${CATEGORIES[cat].blurb}</p>
      ${list.map(card).join('') || '<p class="ok">Nothing found. 🎉</p>'}
    </section>`;
  };

  const html = `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link audit: ${esc(HOST)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
/* Brand palette (Brand Guidelines PDF). Edit these to rebrand the report. */
:root{
  --deep-green:#17303E;   /* Primary: Deep Green */
  --forest-green:#085257; /* Primary: Forest Green */
  --accent:#00A9C5;       /* Accent cyan: sampled from the logo, no hex given in the PDF */
  --yellow:#F6A017;       /* Complementary: Yellow */
  --dark-grey:#555859;    /* Complementary: Dark Grey */
  --light:#F7F8FA;        /* Complementary: light neutral */
  --bg:var(--light); --card:#FFFFFF; --ink:var(--deep-green); --muted:var(--dark-grey); --line:#DFE4E7;
  --hero-bg:var(--deep-green); --hero-ink:#FFFFFF;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0E1F28; --card:var(--deep-green); --ink:#EEF3F5; --muted:#A9B4B8; --line:#284553; --hero-bg:#0A171E; color-scheme:dark;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:400 16px/1.6 Inter,system-ui,-apple-system,"Segoe UI",sans-serif}
h1,h2{font-family:Montserrat,Inter,system-ui,sans-serif;font-weight:700;letter-spacing:-.01em}
a{color:var(--forest-green)}
@media (prefers-color-scheme:dark){a{color:var(--accent)}}
code{font-family:ui-monospace,Menlo,monospace;font-size:.9em;overflow-wrap:anywhere}
.muted{color:var(--muted)}
.w{max-width:1120px;margin:0 auto;padding:0 24px}
header{background:var(--hero-bg);color:var(--hero-ink);padding:48px 0 0}
.pre{font:500 13px/1 Inter,sans-serif;letter-spacing:.2em;text-transform:uppercase;color:var(--accent);margin:0 0 14px}
.pre a{color:inherit}
h1{font-size:clamp(30px,4.6vw,56px);line-height:1.08;margin:0 0 14px;max-width:20ch}
.lede{color:#C9D4D8;max-width:64ch;margin:0 0 32px;font-size:18px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);border-top:1px solid rgba(255,255,255,.14)}
.stat{padding:18px 18px 22px 0;border-right:1px solid rgba(255,255,255,.14)}
.stat:last-child{border-right:0}
.stat b{display:block;font:700 34px/1.1 Montserrat,sans-serif}
.stat span{font-size:14px;color:#C9D4D8}
.stat.y b{color:var(--yellow)}.stat.a b{color:var(--accent)}
.bar{height:6px;background:linear-gradient(90deg,var(--accent) 0 40%,var(--forest-green) 40% 75%,var(--yellow) 75%)}
nav.tabs{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line)}
nav.tabs .w{display:flex;gap:6px;flex-wrap:wrap;align-items:center;padding-top:12px;padding-bottom:12px}
nav.tabs button{font:500 14px Inter,sans-serif;border:1px solid var(--line);background:var(--card);color:var(--ink);padding:7px 14px;border-radius:999px;cursor:pointer}
nav.tabs button[aria-pressed="true"]{background:var(--forest-green);border-color:var(--forest-green);color:#fff}
nav.tabs input{font:inherit;font-size:14px;margin-left:auto;min-width:220px;padding:8px 14px;border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--ink)}
.cat{padding:40px 0 8px}
.cat-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;border-bottom:2px solid var(--accent);padding-bottom:8px;margin-bottom:8px}
.cat h2{margin:0;font-size:26px}
.cat-n{font-size:14px;color:var(--muted)}
.issue{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:8px 0}
summary{cursor:pointer;display:grid;grid-template-columns:118px minmax(0,1fr) 170px 82px;gap:14px;align-items:center;padding:13px 16px;list-style:none}
summary::-webkit-details-marker{display:none}
.chip{font:600 13px/1.2 Inter,sans-serif;text-align:center;border-radius:6px;padding:5px 6px}
.c-broken{background:var(--yellow);color:var(--deep-green)}
.c-staging{background:var(--forest-green);color:#fff}
.c-nolink{border:1.5px solid var(--accent);color:var(--ink)}
.u small{display:block;color:var(--muted);font-size:14px}
.dv,.pc{font-size:14px;color:var(--muted)}.pc b{color:var(--ink);font:700 18px Montserrat,sans-serif}
.body{padding:2px 16px 16px;border-top:1px solid var(--line)}
.body p{margin:10px 0}.body ul{margin:4px 0 14px;padding-left:20px}
.shots{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
figure{margin:0;align-self:start;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--bg)}
figure img{display:block;width:100%;height:auto}
figcaption{font-size:13px;color:var(--muted);padding:7px 10px}
.ok{color:var(--forest-green);font-weight:500}
footer{padding:40px 0 56px;color:var(--muted);font-size:14px}
@media (max-width:760px){.stats{grid-template-columns:repeat(2,1fr)}.stat:nth-child(2){border-right:0}
  summary{grid-template-columns:96px minmax(0,1fr)}.dv,.pc{grid-column:2}nav.tabs input{margin-left:0;flex:1}}
</style></head><body>
<header><div class="w">
  <p class="pre">Link audit · <a href="${esc(SITE)}">${esc(HOST)}</a> · ${esc(startedAt)}</p>
  <h1>${allAffected} of ${pages} pages need a link fix</h1>
  <p class="lede">${count('broken').length} broken link(s), ${count('staging').length} link(s) into ${esc(STAGING.join(', '))}, and ${count('nolink').length} button(s) or link box(es) with no link. Each item shows where it appears, on which devices, and a screenshot with the element outlined in yellow.</p>
  <div class="stats">
    <div class="stat"><b>${pages}</b><span>pages crawled</span></div>
    <div class="stat y"><b>${count('broken').length}</b><span>broken links</span></div>
    <div class="stat"><b>${count('staging').length}</b><span>links to /dev/</span></div>
    <div class="stat a"><b>${count('nolink').length}</b><span>buttons without a link</span></div>
  </div>
</div><div class="bar"></div></header>
<nav class="tabs" aria-label="Filter report"><div class="w">
  <button aria-pressed="true" data-f="all">All (${items.length})</button>
  <button aria-pressed="false" data-f="broken">Broken (${count('broken').length})</button>
  <button aria-pressed="false" data-f="staging">/dev/ (${count('staging').length})</button>
  <button aria-pressed="false" data-f="nolink">No link (${count('nolink').length})</button>
  <input type="search" id="q" placeholder="Search URL, text or page" aria-label="Search">
</div></nav>
<main class="w">${section('broken')}${section('staging')}${section('nolink')}</main>
<footer class="w">Crawled every URL in ${esc(ORIGIN)}/sitemap.xml plus the homepage. Every link was requested with redirects followed; screenshots were taken at 1440 px (desktop) and iPhone 13 (mobile). Menus, tabs, sliders, popups and form buttons are skipped in the "no link" check because scripts make them clickable.</footer>
<script>
let f='all',q='';
function apply(){document.querySelectorAll('.cat').forEach(s=>{s.style.display=(f==='all'||s.id===f)?'':'none'});
document.querySelectorAll('.issue').forEach(d=>{d.style.display=d.dataset.q.includes(q)?'':'none'})}
document.querySelectorAll('[data-f]').forEach(b=>b.onclick=()=>{f=b.dataset.f;document.querySelectorAll('[data-f]').forEach(x=>x.setAttribute('aria-pressed',x===b));apply()});
document.getElementById('q').oninput=e=>{q=e.target.value.toLowerCase();apply()};
</script></body></html>`;
  await fs.writeFile(path.join(OUT, 'report.html'), html);

  const csv = [['category', 'status_or_problem', 'url_or_text', 'link_text', 'section', 'devices', 'pages_affected', 'pages']]
    .concat(items.map((it) => [CATEGORIES[it.category].title, it.category === 'nolink' ? it.kind : it.status, it.url || it.texts[0] || '', it.texts.join(' | '), it.sections.join(' | '), deviceLabel(it), it.pages.length, it.pages.join(' | ')]))
    .map((r) => r.map((c) => `"${String(c).replace(/"/g, '""')}"`).join(',')).join('\n');
  await fs.writeFile(path.join(OUT, 'report.csv'), csv);
  await fs.writeFile(path.join(OUT, 'report.json'), JSON.stringify(data, (k, v) => (k === 'reserved' ? undefined : v), 2));
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

const { links, noLinks, pageStatus } = await crawl(pages);
const toCheck = [...links.keys()].filter((u) => isInternal(u) || u.startsWith('INVALID:') || (CHECK_EXTERNAL && /^https?:/.test(u)));
log(`${links.size} unique links found, checking ${toCheck.length}`);
const status = await check(toCheck);

const items = [];
const uniq = (arr) => [...new Set(arr)];
let blocked = 0;
for (const u of toCheck) {
  const occ = links.get(u);
  const base = { url: u, status: status[u].status, pages: uniq(occ.map((o) => o.page)), texts: uniq(occ.map((o) => o.text).filter(Boolean)), sections: uniq(occ.map((o) => o.section)), instances: occ.length };
  if (isStaging(u)) { items.push({ category: 'staging', ...base }); continue; }
  const kind = classify(u, status[u]);
  if (kind === 'blocked' || kind === 'unreachable') { blocked++; continue; }
  if (kind === 'broken') items.push({ category: 'broken', ...base });
}
for (const [p, s] of Object.entries(pageStatus)) if (!(typeof s === 'number' && s < 400)) items.push({ category: 'broken', url: p, status: s, pages: ['(listed in sitemap.xml)'], texts: ['Sitemap entry'], sections: ['sitemap.xml'], instances: 1, sitemapOnly: true });
for (const [sig, occ] of noLinks) {
  const [kind, element] = sig.split('|');
  items.push({ category: 'nolink', sig, kind, element, pages: uniq(occ.map((o) => o.page)), texts: uniq(occ.map((o) => o.text)).filter(Boolean), sections: uniq(occ.map((o) => o.section)), instances: occ.length });
}
const order = { broken: 0, staging: 1, nolink: 2 };
items.sort((a, b) => order[a.category] - order[b.category] || b.pages.length - a.pages.length);
items.forEach((it, i) => { it.id = i + 1; });
for (const c of Object.keys(CATEGORIES)) log(`${CATEGORIES[c].title}: ${items.filter((i) => i.category === c).length}`);

const shotDir = path.join(OUT, 'screenshots');
const shootable = items.filter((i) => !i.sitemapOnly);
if (shootable.length) { log('Taking screenshots…'); await screenshots(shootable, shotDir); }

const affected = new Set(shootable.flatMap((i) => i.pages));
const data = { site: SITE, startedAt, pages: pages.length, checked: toCheck.length, blocked, affectedPages: affected.size, items };
await writeReport(data, shotDir);
const n = (c) => items.filter((i) => i.category === c).length;
const summary = `Link audit for ${HOST}: ${n('broken')} broken, ${n('staging')} /dev/ and ${n('nolink')} no-link item(s) on ${affected.size} of ${pages.length} pages.`;
log(summary, `Report: ${path.join(OUT, 'report.html')}`);
await notifySlack(summary + (process.env.REPORT_URL ? ` Report: ${process.env.REPORT_URL}` : ''));
if (process.env.FAIL_ON_BROKEN === 'true' && (n('broken') || n('staging'))) process.exit(1);

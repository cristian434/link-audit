#!/usr/bin/env python3
"""
Site QA v2 — compares a live page against its Figma layout and its copy doc,
audits links and buttons, and writes an interactive HTML report with progress tracking.

Usage:
  python site_qa.py --site URL [--figma URL] [--figma-mobile URL] [--doc URL] [--out report]

Env:
  FIGMA_TOKEN       Figma personal access token (scope: file_content:read)
  GOOGLE_SA_JSON    optional: service-account JSON, for copy docs not shared by link
"""
import argparse, asyncio, base64, datetime as dt, difflib, hashlib, html, io, json, os, re, sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image, ImageDraw, ImageOps
from rapidfuzz import fuzz

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
CAT_LABEL = {"design": "Layout vs Figma", "style": "Typography & color", "media": "Images & video",
             "display": "Display", "copy": "Copy vs doc", "links": "Links & buttons"}

TOL = {"size": 1.0, "line_height": 2.0, "letter_spacing": 0.3, "color": 24}
MIN_FIGMA_FONT = 10
MATCH_OK = 90
MATCH_DIFF = 70


def norm(s, loose=False):
    s = (s or "").replace("\u00a0", " ").replace("\u200b", "").replace("\u2028", " ")
    if loose:
        s = (s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"')
              .replace("\u201d", '"').replace("\u2013", "-").replace("\u2014", "-")
              .replace("\u2026", "..."))
        s = re.sub(r"\s*-\s*", " - ", s)
        s = re.sub(r"[.!:;,]+$", "", s.strip())
        s = s.lower()
    return " ".join(s.split())


def word_diff(a, b):
    a, b = a.split(), b.split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if op == "equal":
            out.append(html.escape(" ".join(a[i1:i2])))
        if op in ("delete", "replace"):
            out.append(f"<del>{html.escape(' '.join(a[i1:i2]))}</del>")
        if op in ("insert", "replace"):
            out.append(f"<ins>{html.escape(' '.join(b[j1:j2]))}</ins>")
    return " ".join(out)


# ----------------------------------------------------------------------------- Thumbnails
class Shots:
    """Crops small, highlighted captures of elements from full-page screenshots.
    Captures are stored once and referenced by key, so repeated elements don't bloat the report."""

    def __init__(self):
        self.imgs, self.store = {}, {}

    def add_image(self, name, path):
        if path and os.path.exists(path):
            self.imgs[name] = Image.open(path).convert("RGB")

    def crop(self, name, box, pad=110, out_w=640, max_h=420, color=(255, 59, 48)):
        img = self.imgs.get(name)
        if img is None or not box or box.get("w", 0) <= 0 or box.get("h", 0) <= 0:
            return None
        x, y, w, h = (int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"]))
        key = hashlib.sha1(f"{name}:{x}:{y}:{w}:{h}".encode()).hexdigest()[:12]
        if key in self.store:
            return key
        l = max(0, x - pad)
        t = max(0, y - pad)
        r = min(img.width, x + w + pad)
        b = min(img.height, y + min(h, max_h) + pad)
        if r - l < 480:  # give small elements some context
            extra = (480 - (r - l)) // 2
            l, r = max(0, l - extra), min(img.width, r + extra)
        if r <= l or b <= t:
            return None
        c = img.crop((l, t, r, b)).copy()
        d = ImageDraw.Draw(c)
        d.rectangle((x - l - 3, y - t - 3, x - l + w + 3, y - t + min(h, max_h) + 3), outline=color, width=3)
        if c.width > out_w:
            c = c.resize((out_w, max(1, int(c.height * out_w / c.width))))
        buf = io.BytesIO()
        c.save(buf, "JPEG", quality=74)
        self.store[key] = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        return key


def img_b64(img, maxw=900, maxh=2600):
    img = img.copy()
    if img.width > maxw:
        img = img.resize((maxw, int(img.height * maxw / img.width)))
    if img.height > maxh:
        img = img.crop((0, 0, img.width, maxh))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=72)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# ----------------------------------------------------------------------------- Issues
class Issues:
    def __init__(self):
        self.items, self.seen = [], set()

    def add(self, cat, sev, code, title, *, view=None, loc=None, expected="", actual="", text="",
            diff=None, props=None, url=None, src=None, thumb=None, fthumb=None, box=None, extra=None):
        key = (cat, code, title, view, (loc or {}).get("element"), (loc or {}).get("section"),
               str(expected)[:200], str(actual)[:200], text[:120], url)
        if key in self.seen:
            return None
        self.seen.add(key)
        # stable id across runs: leaves out measured values, so progress carries over when you import it
        stable = (cat, code, view, (loc or {}).get("element"), (loc or {}).get("section"), text[:120], url, src,
                  str(expected)[:200] if code.startswith(("copy", "fig_text", "ref_", "stat", "btn")) else "")
        iid = hashlib.sha1("|".join(map(str, stable)).encode()).hexdigest()[:10]
        while any(i["id"] == iid for i in self.items):
            iid = hashlib.sha1((iid + "+").encode()).hexdigest()[:10]
        d = {"id": iid, "category": cat, "severity": sev, "code": code, "title": title, "view": view,
             "loc": loc or {}, "expected": expected, "actual": actual, "text": text, "diff": diff,
             "props": props, "url": url, "src": src, "thumb": thumb, "fthumb": fthumb, "box": box}
        if extra:
            d.update(extra)
        self.items.append(d)
        return d


def live_loc(view, live, el):
    """Human-readable location of a live element."""
    if not el:
        return {"view": view}
    sec = el.get("sec", -1)
    secs = live.get("sections", [])
    label = secs[sec]["label"] if 0 <= sec < len(secs) else ("Header / nav" if el.get("chrome") else "Page")
    return {"view": view, "section": label, "section_no": sec + 1 if sec >= 0 else None,
            "element": el.get("sel", ""), "y": el.get("y"), "x": el.get("x")}


# ----------------------------------------------------------------------------- Figma
class Figma:
    API = "https://api.figma.com/v1"

    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers["X-Figma-Token"] = token

    def get(self, path, **params):
        r = self.s.get(f"{self.API}{path}", params=params, timeout=120)
        if r.status_code == 403:
            raise SystemExit("Figma: 403 — the token can't read this file (check FIGMA_TOKEN and file access).")
        r.raise_for_status()
        return r.json()

    @staticmethod
    def parse_url(url):
        p = urllib.parse.urlparse(url)
        parts = p.path.strip("/").split("/")
        key = parts[parts.index("branch") + 1] if "branch" in parts else parts[1]
        node = urllib.parse.parse_qs(p.query).get("node-id", [None])[0]
        return key, (node.replace("-", ":") if node else None)

    def node(self, key, nid, depth=None):
        params = {"ids": nid}
        if depth:
            params["depth"] = depth
        return self.get(f"/files/{key}/nodes", **params)["nodes"][nid]["document"]

    def resolve_frame(self, key, nid, mobile=False):
        n = self.node(key, nid, depth=1)
        if self._is_page_frame(n, mobile):
            return nid
        doc = self.get(f"/files/{key}", ids=nid)["document"]
        path = self._path(doc, nid) or []
        anchor_x = None
        for anc in reversed(path):
            if anc["id"] == nid:
                continue
            full = self.node(key, anc["id"], depth=1)
            if self._is_page_frame(full, mobile):
                return full["id"]
            if anchor_x is None and full.get("absoluteBoundingBox"):
                anchor_x = full["absoluteBoundingBox"]["x"]
            if full["type"] in ("SECTION", "CANVAS"):
                cands = [c for c in full.get("children", []) if self._is_page_frame(c, mobile)]
                if cands:
                    ax = anchor_x if anchor_x is not None else (n.get("absoluteBoundingBox") or {}).get("x", 0)
                    cands.sort(key=lambda c: (abs(c["absoluteBoundingBox"]["x"] - ax),
                                              -c["absoluteBoundingBox"]["height"]))
                    return cands[0]["id"]
        raise SystemExit("Figma: couldn't find a page-sized frame from this link. "
                         "Copy the link of the full-page frame (select it in Figma → Copy link).")

    @staticmethod
    def _is_page_frame(n, mobile):
        if n.get("type") not in ("FRAME", "COMPONENT", "INSTANCE"):
            return False
        bb = n.get("absoluteBoundingBox") or {}
        w, h = bb.get("width", 0), bb.get("height", 0)
        return (300 <= w <= 500 and h >= 1500) if mobile else (1200 <= w <= 1920 and h >= 2000)

    def _path(self, n, target, acc=None):
        acc = (acc or []) + [n]
        if n.get("id") == target:
            return acc
        for c in n.get("children", []):
            r = self._path(c, target, acc)
            if r:
                return r
        return None

    def extract(self, key, frame_id, outdir, tag):
        frame = self.node(key, frame_id)
        fb = frame["absoluteBoundingBox"]
        kids = [c for c in frame.get("children", []) if c.get("visible") is not False]
        sections = [c for c in kids if c["absoluteBoundingBox"]["height"] >= 40
                    and c["absoluteBoundingBox"]["width"] >= fb["width"] * 0.5]
        if len(sections) == 1:
            sub = [c for c in sections[0].get("children", []) if c.get("visible") is not False
                   and c["absoluteBoundingBox"]["height"] >= 40]
            if len(sub) > 1:
                sections = sub
        sections = [{"idx": i, "id": c["id"], "name": c["name"],
                     "y": c["absoluteBoundingBox"]["y"] - fb["y"], "h": c["absoluteBoundingBox"]["height"]}
                    for i, c in enumerate(sections)]

        def sec_for(y):
            for s in sections:
                if s["y"] <= y < s["y"] + s["h"]:
                    return s["idx"]
            return None

        texts, media = [], []

        def walk(n):
            if n.get("visible") is False:
                return
            bb = n.get("absoluteBoundingBox") or {}
            if n["type"] == "TEXT":
                st = n.get("style", {})
                size = st.get("fontSize", 0)
                txt = norm(n.get("characters", ""))
                if size >= MIN_FIGMA_FONT and len(txt) >= 2:
                    fill = next((f for f in n.get("fills", []) if f.get("type") == "SOLID"
                                 and f.get("visible", True)), None)
                    color = None
                    if fill:
                        c = fill["color"]
                        color = [round(c["r"] * 255), round(c["g"] * 255), round(c["b"] * 255)]
                    y = bb.get("y", fb["y"]) - fb["y"]
                    texts.append({
                        "text": txt, "family": st.get("fontFamily"), "weight": st.get("fontWeight"),
                        "italic": st.get("italic", False), "size": size,
                        "lh": st.get("lineHeightPx"), "ls": st.get("letterSpacing", 0),
                        "case": st.get("textCase", "ORIGINAL"), "color": color,
                        "x": bb.get("x", 0) - fb["x"], "y": y, "w": bb.get("width", 0),
                        "h": bb.get("height", 0), "section": sec_for(y), "id": n["id"], "layer": n.get("name")})
            fills = n.get("fills") if isinstance(n.get("fills"), list) else []
            for f in fills:
                if f.get("type") in ("IMAGE", "VIDEO") and f.get("visible", True) \
                        and bb.get("width", 0) >= 80 and bb.get("height", 0) >= 80:
                    y = bb["y"] - fb["y"]
                    media.append({"kind": f["type"].lower(), "name": n["name"], "w": bb["width"],
                                  "h": bb["height"], "x": bb["x"] - fb["x"], "y": y,
                                  "section": sec_for(y), "scale": f.get("scaleMode")})
                    break
            for c in n.get("children", []):
                walk(c)

        walk(frame)
        # readable section labels: the biggest text inside each section
        for s in sections:
            st = sorted([t for t in texts if t["section"] == s["idx"]], key=lambda t: -t["size"])
            generic = re.match(r"^(frame|group|rectangle|section)\s*\d*", s["name"], re.I)
            s["label"] = (st[0]["text"][:60] if st else s["name"]) if generic else s["name"]
            if not generic and st:
                s["label"] = f"{s['name']} — {st[0]['text'][:50]}"
        ids = [frame_id] + [s["id"] for s in sections]
        urls = {}
        for i in range(0, len(ids), 10):
            urls.update(self.get(f"/images/{key}", ids=",".join(ids[i:i + 10]), format="png", scale=1)["images"])
        for s in sections:
            s["png"] = self._download(urls.get(s["id"]), f"{outdir}/figma_{tag}_s{s['idx']}.png")
        full = self._download(urls.get(frame_id), f"{outdir}/figma_{tag}_full.png")
        return {"id": frame_id, "name": frame["name"], "w": fb["width"], "h": fb["height"],
                "sections": sections, "texts": texts, "media": media, "png": full}

    @staticmethod
    def _download(url, path):
        if not url:
            return None
        r = requests.get(url, timeout=180)
        r.raise_for_status()
        open(path, "wb").write(r.content)
        return path


# ----------------------------------------------------------------------------- Live page
LIVE_JS = r"""
() => {
  const vis = e => {
    if (e.checkVisibility && !e.checkVisibility({opacityProperty:true, visibilityProperty:true})) return false;
    const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0;
  };
  const box = e => { const r = e.getBoundingClientRect();
    return {x: Math.round(r.x + scrollX), y: Math.round(r.y + scrollY), w: Math.round(r.width), h: Math.round(r.height)}; };
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const H = document.documentElement.scrollHeight;
  // find the wrapper whose children are the page sections
  let wrap = document.body;
  for (let i = 0; i < 8; i++) {
    const tall = [...wrap.children].filter(c => c.getBoundingClientRect().height > H * 0.6);
    if (tall.length === 1 && wrap.children.length <= 4) wrap = tall[0]; else break;
  }
  const secEls = [...wrap.children].filter(c => c.getBoundingClientRect().height > 30 && !['SCRIPT','STYLE','LINK'].includes(c.tagName));
  const cls = e => (typeof e.className === 'string' ? e.className.trim().split(/\s+/) : []).filter(Boolean);
  const nice = e => { const c = cls(e); return c.find(x => !x.startsWith('w-')) || c[0] || ''; };
  const secLabel = s => {
    const h = s.querySelector('h1,h2,h3');
    if (h && clean(h.innerText)) return clean(h.innerText).slice(0, 60);
    if (s.matches('nav,header,[class*="nav"]') || s.querySelector('nav')) return 'Header / nav';
    if (s.matches('footer,[class*="footer"]')) return 'Footer';
    const n = nice(s); return n ? n.replace(/[-_]/g, ' ') : s.tagName.toLowerCase();
  };
  const sections = secEls.map((s, i) => ({idx: i, label: secLabel(s), cls: nice(s), ...box(s)}));
  const secOf = el => { let p = el; while (p && p.parentElement && p.parentElement !== wrap) p = p.parentElement;
    return p && p.parentElement === wrap ? secEls.indexOf(p) : -1; };
  const goodId = e => e.id && !/^w-node-|^[0-9a-f-]{20,}$/i.test(e.id);
  const short = e => { let s = e.tagName.toLowerCase(); if (goodId(e)) return s + '#' + e.id; const n = nice(e); return n ? s + '.' + n : s; };
  const sel = e => { const parts = []; let p = e;
    for (let i = 0; p && p !== wrap && p !== document.body && parts.length < 4 && i < 12; i++, p = p.parentElement) {
      const s = short(p); if (p === e || s.includes('.') || s.includes('#')) parts.unshift(s); }
    return parts.join(' > '); };
  const chrome = e => !!e.closest('header, nav, footer, [class*="navbar"], [class*="footer"]');
  const linkOf = e => { const a = e.closest('a,button,[role="button"]'); return a ? {tag: a.tagName.toLowerCase(), href: a.getAttribute('href')} : null; };
  const L = e => ({sec: secOf(e), sel: sel(e), chrome: chrome(e), visible: vis(e), ...box(e)});

  const out = {texts: [], blocks: [], links: [], media: [], display: [], sections};
  const seen = new Set();
  const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (tw.nextNode()) {
    const t = tw.currentNode; if (!t.textContent.trim()) continue;
    const el = t.parentElement;
    if (!el || seen.has(el) || ['SCRIPT','STYLE','NOSCRIPT','TEMPLATE'].includes(el.tagName) || el.closest('svg')) continue;
    seen.add(el);
    const s = getComputedStyle(el);
    const shown = clean(el.innerText || el.textContent); if (!shown) continue;
    out.texts.push({text: shown, raw: clean(el.textContent), tag: el.tagName.toLowerCase(),
      htag: (el.closest('h1,h2,h3,h4,h5,h6') || {}).tagName ? el.closest('h1,h2,h3,h4,h5,h6').tagName.toLowerCase() : null,
      link: linkOf(el),
      family: s.fontFamily.split(',')[0].replace(/["']/g, '').trim(), weight: +s.fontWeight,
      italic: s.fontStyle === 'italic', size: parseFloat(s.fontSize),
      lh: s.lineHeight === 'normal' ? null : parseFloat(s.lineHeight),
      ls: s.letterSpacing === 'normal' ? 0 : parseFloat(s.letterSpacing),
      transform: s.textTransform, color: s.color, ...L(el)});
  }
  document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,li,blockquote,a,button,label,figcaption,td,th').forEach(el => {
    const shown = clean(el.innerText || el.textContent);
    if (!shown) return;
    const s = getComputedStyle(el);
    out.blocks.push({text: shown, raw: clean(el.textContent), tag: el.tagName.toLowerCase(),
      htag: /^h\d$/i.test(el.tagName) ? el.tagName.toLowerCase() : null, link: linkOf(el),
      size: parseFloat(s.fontSize), transform: s.textTransform, ...L(el)});
  });
  document.querySelectorAll('a, button, [role="button"], input[type="submit"]').forEach(el => {
    out.links.push({tag: el.tagName.toLowerCase(), href: el.getAttribute('href'), target: el.getAttribute('target'),
      text: clean(el.innerText || el.value || el.getAttribute('aria-label') || el.title).slice(0, 90),
      img: !!el.querySelector('img,svg'), inForm: !!el.closest('form'), type: el.getAttribute('type'),
      ariaControls: el.hasAttribute('aria-controls') || el.hasAttribute('aria-expanded') || el.hasAttribute('data-w-tab'),
      cls: cls(el).join(' ').slice(0, 80), ...L(el)});
  });
  document.querySelectorAll('img').forEach(m => out.media.push({kind: 'image', src: m.currentSrc || m.src, alt: m.getAttribute('alt'),
    complete: m.complete, nw: m.naturalWidth, nh: m.naturalHeight, fit: getComputedStyle(m).objectFit, ...L(m)}));
  document.querySelectorAll('video').forEach(v => out.media.push({kind: 'video', src: v.currentSrc || v.src || (v.querySelector('source')||{}).src,
    ready: v.readyState, error: v.error ? v.error.code : null, autoplay: v.autoplay, muted: v.muted, loop: v.loop,
    playsinline: v.playsInline, paused: v.paused, poster: v.poster, nw: v.videoWidth, nh: v.videoHeight,
    fit: getComputedStyle(v).objectFit, ...L(v)}));
  document.querySelectorAll('iframe').forEach(f => out.media.push({kind: 'iframe', src: f.src, ...L(f)}));
  document.querySelectorAll('body *').forEach(e => {
    const s = getComputedStyle(e); if (!s.backgroundImage.includes('url(')) return;
    const r = e.getBoundingClientRect(); if (r.width < 80 || r.height < 80) return;
    out.media.push({kind: 'background', src: s.backgroundImage.slice(5, -2), size: s.backgroundSize, ...L(e)});
  });
  const vw = document.documentElement.clientWidth;
  out.pageWidth = document.documentElement.scrollWidth; out.viewport = vw; out.pageHeight = H;
  const clipped = el => { for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX; if (o === 'hidden' || o === 'clip' || o === 'auto' || o === 'scroll') return true; } return false; };
  document.querySelectorAll('body *').forEach(e => {
    if (!vis(e)) return; const r = e.getBoundingClientRect();
    if (r.right > vw + 2 && r.width < vw * 3 && !clipped(e))
      out.display.push({type: 'overflow', tag: e.tagName.toLowerCase(), text: clean(e.innerText).slice(0, 60), right: Math.round(r.right), ...L(e)});
    const s = getComputedStyle(e);
    if (e.children.length === 0 && clean(e.innerText) && (s.overflow === 'hidden' || s.textOverflow === 'ellipsis')
        && (e.scrollWidth > e.clientWidth + 2 || e.scrollHeight > e.clientHeight + 4))
      out.display.push({type: 'clipped_text', tag: e.tagName.toLowerCase(), text: clean(e.innerText).slice(0, 80), ...L(e)});
  });
  out.ids = [...document.querySelectorAll('[id]')].map(e => e.id);
  return out;
}
"""


async def capture_live(url, width, outdir, tag):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": int(width), "height": 900}, user_agent=UA,
                                  device_scale_factor=1, is_mobile=width < 600, has_touch=width < 600)
        pg = await ctx.new_page()
        failed, console = [], []
        pg.on("response", lambda r: failed.append({"status": r.status, "url": r.url, "type": r.request.resource_type})
              if r.status >= 400 else None)
        pg.on("requestfailed", lambda r: failed.append({"status": "failed", "url": r.url,
                                                         "type": r.resource_type, "error": r.failure}))
        pg.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
        await pg.goto(url, wait_until="networkidle", timeout=120000)
        h = await pg.evaluate("document.documentElement.scrollHeight")
        y = 0
        while y < h:
            await pg.evaluate(f"window.scrollTo(0,{y})")
            await pg.wait_for_timeout(300)
            y += 500
            h = await pg.evaluate("document.documentElement.scrollHeight")
        await pg.wait_for_timeout(1500)
        await pg.evaluate("window.scrollTo(0,0)")
        await pg.wait_for_timeout(800)
        data = await pg.evaluate(LIVE_JS)
        shot = f"{outdir}/live_{tag}.png"
        await pg.screenshot(path=shot, full_page=True, animations="disabled")
        data.update({"failed": failed, "console": console[:30], "png": shot, "width": width})
        await b.close()
        return data


def parse_rgb(s):
    m = re.findall(r"[\d.]+", s or "")
    return [round(float(v)) for v in m[:3]] if len(m) >= 3 else None


def color_dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def hexc(c):
    return "#%02x%02x%02x" % tuple(c) if c else "—"


def similarity(a, b):
    import numpy as np
    w = 96
    h = max(8, int(a.height * w / a.width))
    x = np.asarray(ImageOps.autocontrast(a.convert("L").resize((w, h))), dtype=float).ravel()
    y = np.asarray(ImageOps.autocontrast(b.convert("L").resize((w, h))), dtype=float).ravel()
    if x.std() < 1 and y.std() < 1:
        return 100 if abs(x.mean() - y.mean()) < 10 else 0
    if x.std() < 1 or y.std() < 1:
        return 0
    return int(round(max(0.0, float(np.corrcoef(x, y)[0, 1])) * 100))


# ----------------------------------------------------------------------------- Design comparison
def compare_design(fig, live, view, shots, iss):
    lname, fname = f"live_{view}", f"figma_{view}"
    cands = [t for t in live["texts"] if len(t["text"]) >= 2]
    matches = {}
    scale = live["width"] / fig["w"]

    def fbox(t):
        return {"x": t["x"], "y": t["y"], "w": t["w"], "h": t["h"]}

    def floc(t):
        s = next((s for s in fig["sections"] if s["idx"] == t["section"]), None)
        return {"view": view, "section": s["label"] if s else "outside sections",
                "element": f"Figma layer “{t.get('layer') or t['text'][:30]}”", "y": round(t["y"])}

    for ft in fig["texts"]:
        best, score = None, 0
        for lt in cands:
            sc = fuzz.ratio(norm(ft["text"], True), norm(lt["text"], True))
            if sc > score or (sc == score and best and lt["visible"] and not best["visible"]):
                best, score = lt, sc
        if score < MATCH_DIFF:
            small = ft["size"] < 12
            iss.add("design", "low" if small else "medium", "fig_text_missing",
                    "Small design text not found on the live page (may be inside an illustration)" if small
                    else "Design text not found on the live page",
                    view=view, loc=floc(ft), expected=ft["text"][:200], actual="not found",
                    fthumb=shots.crop(fname, fbox(ft)))
            continue
        matches[ft["id"]] = (best, score)
        loc = live_loc(view, live, best)
        if not best["visible"]:
            iss.add("display", "low", "hidden_on_load", "Element is in the page but hidden on load (collapsed, tab, slider or animation)",
                    view=view, loc=loc, expected=ft["text"][:140], actual="hidden",
                    fthumb=shots.crop(fname, fbox(ft)))
            continue
        thumb, fthumb = shots.crop(lname, best), shots.crop(fname, fbox(ft))
        if score < MATCH_OK:
            iss.add("design", "low", "fig_text_diff", "Text differs from the Figma layout", view=view, loc=loc,
                    expected=ft["text"][:240], actual=best["text"][:240], diff=word_diff(ft["text"], best["text"]),
                    thumb=thumb, fthumb=fthumb, box=best)
        props, sev = style_diffs(ft, best)
        if props:
            iss.add("style", sev, "style", "Typography / color differs from Figma: " + ", ".join(p["prop"] for p in props),
                    view=view, loc=loc, text=best["text"][:100], props=props, thumb=thumb, fthumb=fthumb, box=best)

    # anchor every Figma section on the live page
    shot = shots.imgs.get(lname)
    anchors = []
    for s in fig["sections"]:
        sec_texts = sorted([t for t in fig["texts"] if t["section"] == s["idx"] and t["id"] in matches
                            and matches[t["id"]][1] >= MATCH_OK and matches[t["id"]][0]["visible"]],
                           key=lambda t: -t["size"])
        if s["idx"] == 0:
            anchors.append((s, 0))
        elif sec_texts:
            t = sec_texts[0]
            anchors.append((s, max(0, matches[t["id"]][0]["y"] - (t["y"] - s["y"]) * scale)))
        else:
            anchors.append((s, None))
    out = []
    for i, (s, top) in enumerate(anchors):
        nxt = next((a[1] for a in anchors[i + 1:] if a[1] is not None), None)
        entry = {"label": s["label"], "name": s["name"], "idx": s["idx"], "figma_h": round(s["h"]),
                 "live_h": None, "live_top": None, "similarity": None, "figma_img": None, "live_img": None}
        sloc = {"view": view, "section": s["label"], "element": f"Figma section “{s['name']}”"}
        if top is None:
            iss.add("design", "medium", "section_missing", "Section from Figma could not be found on the live page",
                    view=view, loc=sloc, expected="Section present in Figma", actual="no matching content",
                    fthumb=shots.crop(fname, {"x": 0, "y": s["y"], "w": fig["w"], "h": s["h"]}, pad=0, max_h=600))
        else:
            bottom = nxt if nxt is not None and nxt > top else min(shot.height, top + s["h"] * scale)
            live_h = bottom - top
            entry.update(live_h=round(live_h), live_top=round(top))
            if nxt is not None and abs(live_h - s["h"] * scale) / (s["h"] * scale) > 0.15:
                iss.add("design", "medium", "section_height",
                        "Section height differs more than 15% from Figma (spacing or layout)", view=view, loc=sloc,
                        expected=f"{round(s['h'] * scale)}px", actual=f"{round(live_h)}px")
            crop = shot.crop((0, int(top), shot.width, int(min(shot.height, top + max(live_h, 40)))))
            if s.get("png") and os.path.exists(s["png"]):
                fimg = Image.open(s["png"]).convert("RGB")
                sim = similarity(fimg, crop)
                entry["similarity"] = sim
                entry["figma_img"] = img_b64(fimg)
                if sim < 70:
                    iss.add("design", "medium" if sim < 55 else "low", "section_visual",
                            f"Section looks visually different from Figma ({sim}% match) — compare in Page map",
                            view=view, loc=sloc)
            entry["live_img"] = img_b64(crop)
            f = crop.width / 900 if crop.width > 900 else 1
            entry["live_shown_h"] = min(crop.height, 2600 * f)
        out.append(entry)

    live_media = [m for m in live["media"] if m["visible"] and m["w"] >= 80 and m["h"] >= 80]
    for i, (s, top) in enumerate(anchors):
        if top is None:
            continue
        nxt = next((a[1] for a in anchors[i + 1:] if a[1] is not None), top + s["h"] * scale)
        fm = [m for m in fig["media"] if m["section"] == s["idx"]]
        lm = [m for m in live_media if top <= m["y"] < nxt]
        sloc = {"view": view, "section": s["label"]}
        for m in fm:
            if m["kind"] == "video" and not any(x["kind"] in ("video", "iframe") for x in lm):
                iss.add("media", "medium", "video_missing", "Figma has a video here but the live section has none",
                        view=view, loc={**sloc, "element": f"Figma layer “{m['name']}”"}, expected="video", actual="none",
                        fthumb=shots.crop(fname, m))
        if fm and not lm:
            iss.add("media", "medium", "images_missing", "Images in the Figma section are missing on the live page",
                    view=view, loc=sloc, expected=f"{len(fm)} image(s)", actual="0",
                    fthumb=shots.crop(fname, fm[0]))
    return out


def style_diffs(ft, lt):
    props = []
    sev = "low"
    fam_f, fam_l = (ft["family"] or "").lower().replace(" ", ""), (lt["family"] or "").lower().replace(" ", "")
    if fam_f and fam_l and fam_f != fam_l:
        props.append({"prop": "font family", "figma": ft["family"], "live": lt["family"]}); sev = "high"
    if ft["weight"] and lt["weight"] and abs(ft["weight"] - lt["weight"]) >= 100:
        props.append({"prop": "weight", "figma": str(ft["weight"]), "live": str(lt["weight"])})
        sev = "medium" if sev == "low" else sev
    if abs(ft["size"] - lt["size"]) > TOL["size"]:
        props.append({"prop": "size", "figma": f"{ft['size']:g}px", "live": f"{lt['size']:g}px"})
        sev = "medium" if sev == "low" else sev
    if ft["lh"] and lt["lh"] and abs(ft["lh"] - lt["lh"]) > TOL["line_height"]:
        props.append({"prop": "line height", "figma": f"{ft['lh']:.0f}px", "live": f"{lt['lh']:.0f}px"})
    if abs((ft["ls"] or 0) - (lt["ls"] or 0)) > TOL["letter_spacing"]:
        props.append({"prop": "letter spacing", "figma": f"{ft['ls']:.2f}px", "live": f"{lt['ls']:.2f}px"})
    if bool(ft["italic"]) != bool(lt["italic"]):
        props.append({"prop": "italic", "figma": "yes" if ft["italic"] else "no", "live": "yes" if lt["italic"] else "no"})
    lc = parse_rgb(lt["color"])
    if ft["color"] and lc and color_dist(ft["color"], lc) > TOL["color"]:
        props.append({"prop": "color", "figma": hexc(ft["color"]), "live": hexc(lc), "swatch": True})
        sev = "medium" if sev == "low" else sev
    fig_upper = ft["case"] == "UPPER" or (ft["text"].isupper() and len(ft["text"]) > 3)
    live_upper = lt["transform"] == "uppercase" or (lt["text"].isupper() and len(lt["text"]) > 3)
    if fig_upper != live_upper:
        props.append({"prop": "letter case", "figma": "UPPERCASE" if fig_upper else "Normal",
                      "live": "UPPERCASE" if live_upper else "Normal"})
    return props, sev


# ----------------------------------------------------------------------------- Media & display
def check_media_display(live, view, shots, iss):
    lname = f"live_{view}"
    broken = []
    for m in live["media"]:
        if not m["visible"] and m["kind"] != "image":
            continue
        short = (m.get("src") or "")[-80:]
        loc = live_loc(view, live, m)
        if m["kind"] == "image":
            if m["complete"] and m["nw"] == 0 and m.get("src"):
                broken.append((m, short))
                continue
            if not m["visible"] or m["w"] < 40:
                continue
            th = lambda: shots.crop(lname, m)
            if m["alt"] is None:
                iss.add("media", "low", "alt_missing", "Image has no alt text", view=view, loc=loc,
                        expected="alt text", actual="missing", src=short, thumb=th(), box=m)
            if m["nw"] and m["nh"] and m["fit"] in ("fill", "") and m["h"] > 0:
                r_nat, r_ren = m["nw"] / m["nh"], m["w"] / m["h"]
                if abs(r_nat - r_ren) / r_nat > 0.04:
                    iss.add("media", "medium", "img_stretched", "Image is stretched or squashed", view=view, loc=loc,
                            expected=f"proportion {r_nat:.2f}", actual=f"proportion {r_ren:.2f}", src=short,
                            thumb=th(), box=m)
            if m["nw"] and m["nw"] < m["w"] * 0.9:
                iss.add("media", "low", "img_upscaled", "Image file is smaller than its display size — looks blurry",
                        view=view, loc=loc, expected=f"≥ {m['w']}px wide", actual=f"{m['nw']}px wide", src=short,
                        thumb=th(), box=m)
            if m["nw"] and m["nw"] > m["w"] * 3 and m["nw"] > 2000:
                iss.add("media", "info", "img_heavy", "Image file is much larger than needed (slows the page)",
                        view=view, loc=loc, expected=f"~{m['w'] * 2}px wide", actual=f"{m['nw']}px wide", src=short,
                        thumb=th(), box=m)
        elif m["kind"] == "video":
            th = shots.crop(lname, m)
            if m["error"] or m["ready"] == 0:
                iss.add("media", "high", "video_broken", "Video doesn't load", view=view, loc=loc, src=short, thumb=th, box=m)
            elif m["autoplay"] and m["paused"]:
                iss.add("media", "medium", "video_paused", "Autoplay video isn't playing", view=view, loc=loc,
                        expected="playing", actual="paused", src=short, thumb=th, box=m)
            if m["autoplay"] and not m["muted"]:
                iss.add("media", "medium", "video_unmuted", "Autoplay video isn't muted (browsers will block it)",
                        view=view, loc=loc, expected="muted", actual="not muted", src=short, thumb=th, box=m)
            if m["autoplay"] and not m["playsinline"] and live["width"] < 600:
                iss.add("media", "medium", "video_inline", "Video is missing playsinline (won't autoplay on iPhone)",
                        view=view, loc=loc, src=short, thumb=th, box=m)
            if m["nw"] and m["nh"] and m["h"] and m["fit"] in ("fill", "") and \
                    abs(m["nw"] / m["nh"] - m["w"] / m["h"]) / (m["nw"] / m["nh"]) > 0.04:
                iss.add("media", "medium", "video_stretched", "Video is stretched or squashed", view=view, loc=loc,
                        thumb=th, box=m)
    if len(broken) > 8:
        iss.add("media", "high", "img_broken_many",
                f"{len(broken)} images don't load (usually one shared cause: CDN, blocked host or bad export)",
                view=view, loc={"view": view, "section": "whole page"},
                actual=", ".join(b[1] for b in broken[:3]) + " …")
    else:
        for m, short in broken:
            iss.add("media", "high", "img_broken", "Broken image (doesn't load)", view=view, loc=live_loc(view, live, m),
                    src=short, thumb=shots.crop(lname, m), box=m)
    reported = {(m.get("src") or "")[-80:] for m in live["media"]}
    by_host = {}
    for f in live["failed"]:
        if f["type"] in ("image", "media", "stylesheet", "font", "script") and f["url"][-80:] not in reported:
            by_host.setdefault((urllib.parse.urlparse(f["url"]).netloc, f["type"]), []).append(f)
    for (host, typ), fs in by_host.items():
        cat = "media" if typ in ("image", "media") else "display"
        if len(fs) > 5:
            iss.add(cat, "high", "asset_failed_many", f"{len(fs)} {typ} files failed to load from {host}", view=view,
                    loc={"view": view, "section": "whole page"}, expected="200", actual=str(fs[0]["status"]),
                    src=fs[0]["url"][-90:])
        else:
            for f in fs:
                iss.add(cat, "high", "asset_failed", f"A {typ} file failed to load", view=view,
                        loc={"view": view, "section": "whole page"}, expected="200", actual=str(f["status"]),
                        src=f["url"][-90:])
    if live["pageWidth"] > live["viewport"] + 2:
        iss.add("display", "high", "hscroll", "Page scrolls sideways (content is wider than the screen)", view=view,
                loc={"view": view, "section": "whole page"}, expected=f"{live['viewport']}px", actual=f"{live['pageWidth']}px")
    n_over = 0
    for d in live["display"]:
        loc = live_loc(view, live, d)
        if d["type"] == "overflow" and n_over < 10:
            if iss.add("display", "medium", "overflow", "Element goes past the right edge of the screen", view=view,
                       loc=loc, expected=f"≤ {live['viewport']}px", actual=f"right edge at {d['right']}px",
                       text=d["text"], thumb=shots.crop(lname, d), box=d):
                n_over += 1
        if d["type"] == "clipped_text":
            iss.add("display", "medium", "clipped", "Text is cut off by its container", view=view, loc=loc,
                    text=d["text"], thumb=shots.crop(lname, d), box=d)
    if live["width"] < 600:
        small = [l for l in live["links"] if l["visible"] and 0 < min(l["w"], l["h"]) < 32]
        for l in small[:12]:
            iss.add("display", "low", "tap_small", "Tap target is smaller than 32px on mobile", view=view,
                    loc=live_loc(view, live, l), expected="≥ 44 × 44px", actual=f"{l['w']} × {l['h']}px",
                    text=l["text"] or l["href"] or "", thumb=shots.crop(lname, l), box=l)


# ----------------------------------------------------------------------------- Copy doc
ROLE_NAMES = {"title": "Title", "subtitle": "Subtitle", "paragraph": "Paragraph", "tag": "Section tag",
              "item_tag": "Item tag", "button": "Button", "stat": "Stat", "item_title": "Item title",
              "quote": "Quote", "caption": "Name / caption", "text": "Text"}
# order matters: first match wins
ROLE_LABELS = [
    ("tag", r"eyebrow|kicker|overline|section\s*tag|section\s*label|pre-?title|super-?title|tagline"),
    ("subtitle", r"h[2-6]|sub-?head(?:line|er|ing)?|sub-?title|section\s*(?:title|heading|headline)|subhead"),
    ("title", r"h1|headline|main\s*(?:title|heading)|hero\s*(?:title|headline)|title|heading|header"),
    ("button", r"cta|button|btn|link\s*text|primary\s*cta|secondary\s*cta"),
    ("stat", r"stat|stats|number|metric|figure|kpi|data\s*point"),
    ("item_tag", r"tag|label|pill|chip|badge|category"),
    ("item_title", r"card\s*title|item\s*title|step\s*title|feature\s*title|question|q"),
    ("quote", r"quote|testimonial|blockquote"),
    ("caption", r"caption|name|role|author|attribution|job\s*title"),
    ("paragraph", r"body(?:\s*copy)?|copy|paragraph|description|desc|text|intro|sub-?copy|supporting\s*copy|answer|a|p"),
]
LABEL_RE = re.compile(r"^\s*(?:[-•*▪◦]\s*)?(" + "|".join(f"(?:{r})" for _, r in ROLE_LABELS) +
                      r")\s*\d*\s*(?:\([^)]*\))?\s*[:|–—]\s*(.*)$", re.I)
REF_WORDS = (r"note|notes|nb|ref|reference|comment|comments|dev|developer|design|designer|image|img|visual|video|"
             r"graphic|illustration|icon|animation|alt(?:\s*text)?|link|url|href|seo|meta(?:\s*(?:title|description))?|"
             r"slug|og\s*\w*|page\s*title|instructions?|tbd|todo|option\s*[a-z0-9]|alternative|alt\s*option|previous|"
             r"old|current|source|context|purpose|goal|layout|placement|tone|feedback|question\s*for|open\s*question|"
             r"internal|placeholder|asset|assets|background|bg|cms|interaction|hover|template")
REF_RE = re.compile(rf"^\s*(?:[-•*]\s*)?(?:{REF_WORDS})\b\s*\d*\s*[:\-–—]", re.I)
SECTION_RE = re.compile(r"^\s*(?:section|module|block|page\s*section|s)\s*[\d#ivx]+\b.*$|"
                        r"^\s*\d+[.)]\s*(?:hero|section|module|block|banner|cta|faq|footer|header|proof|"
                        r"resources|logos?|stats?|testimonials?)\b.*$", re.I)
SECTION_WORDS = re.compile(r"\b(section|module|block|hero|banner|footer|header|navigation|nav|cta|faq|"
                           r"testimonials?|logos?|carousel|slider|accordion|component|page)\b", re.I)
SECTION_VOCAB = {"section", "module", "block", "hero", "banner", "footer", "header", "nav", "navigation", "cta",
                 "faq", "faqs", "testimonial", "testimonials", "logo", "logos", "bar", "carousel", "slider",
                 "accordion", "component", "page", "intro", "outro", "top", "bottom", "final", "main", "the",
                 "and", "of", "stats", "proof", "social", "grid", "cards", "resources", "form", "contact"}
URL_RE = re.compile(r"(https?://\S+|www\.\S+|(?<!\w)/[a-z0-9\-/_?=&#.%]+)", re.I)
NUM_RE = re.compile(r"[+\-~]?\$?\d[\d,.]*\s*(?:%|\+|x|k|m|b)?", re.I)


def fetch_doc(url):
    """Returns (kind, payload): ('docx', bytes) when formatting is available, else ('text', str)."""
    if os.path.exists(url):
        return ("docx", open(url, "rb").read()) if url.endswith(".docx") else ("text", open(url).read())
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise SystemExit("Copy doc: couldn't read the document id from the link.")
    fid = m.group(1)
    is_gdoc = "docs.google.com/document" in url
    pub = (f"https://docs.google.com/document/d/{fid}/export?format=docx" if is_gdoc
           else f"https://drive.google.com/uc?export=download&id={fid}")
    r = requests.get(pub, timeout=60, allow_redirects=True)
    if r.ok and "accounts.google.com" not in r.url and r.content[:2] == b"PK":
        return "docx", r.content
    sa = os.environ.get("GOOGLE_SA_JSON")
    if sa:
        from google.oauth2 import service_account
        import google.auth.transport.requests as gtr
        cred = service_account.Credentials.from_service_account_info(
            json.loads(sa), scopes=["https://www.googleapis.com/auth/drive.readonly"])
        cred.refresh(gtr.Request())
        h = {"Authorization": f"Bearer {cred.token}"}
        if is_gdoc:
            r = requests.get(f"https://www.googleapis.com/drive/v3/files/{fid}/export", headers=h, timeout=60,
                             params={"mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"})
        else:
            r = requests.get(f"https://www.googleapis.com/drive/v3/files/{fid}", headers=h, timeout=60,
                             params={"alt": "media", "supportsAllDrives": "true"})
        r.raise_for_status()
        return "docx", r.content
    raise SystemExit("Copy doc is private. Share it as 'Anyone with the link → Viewer', "
                     "or share it with the service account in GOOGLE_SA_JSON.")


def _run_fmt(p):
    """Formatting summary of a python-docx paragraph."""
    runs = [r for r in p.runs if r.text.strip()]
    if not runs:
        return {}

    def all_(f):
        return all(f(r) for r in runs)

    def gray(r):
        try:
            c = r.font.color.rgb
        except Exception:
            return False
        if c is None:
            return False
        rr, gg, bb = c[0], c[1], c[2]
        return max(rr, gg, bb) - min(rr, gg, bb) < 24 and 0x70 <= rr <= 0xD0

    return {"strike": all_(lambda r: bool(r.font.strike)), "italic": all_(lambda r: bool(r.italic)),
            "bold": all_(lambda r: bool(r.bold)), "gray": all_(gray),
            "highlight": any(r.font.highlight_color is not None for r in runs)}


def doc_paragraphs(kind, payload):
    """Yields dicts: text, style, fmt, table_role, table_kind ('copy'|'ref'|None), table_section."""
    if kind == "text":
        for ln in payload.splitlines():
            yield {"text": ln, "style": "", "fmt": {}}
        return
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    d = docx.Document(io.BytesIO(payload))
    for child in d.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(child, d)
            yield {"text": p.text, "style": p.style.name if p.style is not None else "", "fmt": _run_fmt(p)}
        elif tag == "tbl":
            yield from _table_rows(Table(child, d))


def _table_rows(t):
    rows = list(t.rows)
    if not rows:
        return
    header = [norm(c.text).lower() for c in rows[0].cells]

    def col(pat):
        return next((i for i, h in enumerate(header) if re.search(pat, h)), None)

    c_copy = col(r"^(final\s*)?(copy|text|content|website\s*copy|web\s*copy|english|en|headline\s*copy)")
    c_role = col(r"^(element|type|component|field|item|copy\s*type|format|tag|role)")
    c_sec = col(r"^(section|module|block|page\s*section|area)")
    c_refs = [i for i, h in enumerate(header) if re.search(
        r"note|comment|reference|instruction|design|dev|link|url|image|visual|context|feedback|status|old|previous", h)
        and i not in (c_copy, c_role, c_sec)]
    if c_copy is not None:
        for r in rows[1:]:
            cells = r.cells
            sec = norm(cells[c_sec].text) if c_sec is not None and c_sec < len(cells) else None
            role = norm(cells[c_role].text) if c_role is not None and c_role < len(cells) else None
            if sec:
                yield {"text": sec, "style": "", "fmt": {}, "section_marker": True}
            if c_copy < len(cells):
                for p in cells[c_copy].paragraphs:
                    yield {"text": p.text, "style": p.style.name if p.style is not None else "", "fmt": _run_fmt(p),
                           "table_role": role}
            for i in c_refs:
                if i < len(cells):
                    for p in cells[i].paragraphs:
                        if p.text.strip():
                            yield {"text": p.text, "style": "", "fmt": {}, "ref_reason": f"Table column “{header[i]}”"}
        return
    # two-column "Label | Copy" tables
    if all(len(r.cells) == 2 for r in rows) and sum(1 for r in rows if LABEL_RE.match(norm(r.cells[0].text) + ": x")) >= len(rows) / 2:
        for r in rows:
            label = norm(r.cells[0].text)
            for p in r.cells[1].paragraphs:
                yield {"text": p.text, "style": "", "fmt": _run_fmt(p), "table_role": label}
        return
    for r in rows:
        seen = set()
        for c in r.cells:
            if id(c._tc) in seen:
                continue
            seen.add(id(c._tc))
            for p in c.paragraphs:
                yield {"text": p.text, "style": p.style.name if p.style is not None else "", "fmt": _run_fmt(p)}


def role_from_label(label):
    for role, pat in ROLE_LABELS:
        if re.fullmatch(rf"(?:{pat})\s*\d*", label.strip(), re.I):
            return role
    return None


def classify_doc(paras):
    """Splits the doc into copy lines (with role) and reference-only lines (with reason)."""
    copy, refs = [], []
    section, n = "Start of doc", 0
    for p in paras:
        raw = norm(p["text"])
        n += 1
        if len(raw) < 2:
            continue
        loc = {"section": section, "line": n}
        fmt = p.get("fmt") or {}
        style = (p.get("style") or "").lower()
        if p.get("section_marker"):
            section = raw[:60]
            refs.append({"text": raw, "reason": "Section name (doc structure)", "loc": {"section": section, "line": n}})
            continue
        if p.get("ref_reason"):
            refs.append({"text": raw, "reason": p["ref_reason"], "loc": loc})
            continue
        # reference-only text
        reason = None
        if fmt.get("strike"):
            reason = "Strikethrough (removed copy)"
        elif fmt.get("gray"):
            reason = "Gray text (note)"
        elif re.fullmatch(r"[\[({<].*[\])}>]", raw):
            reason = "Text in brackets"
        elif raw.startswith(("//", "#", "*Note", "NB")):
            reason = "Comment marker"
        elif URL_RE.fullmatch(raw):
            reason = "Link only"
        elif SECTION_RE.match(raw):
            reason = "Section name (doc structure)"
        elif REF_RE.match(raw) and not LABEL_RE.match(raw):
            reason = "Note / instruction label (“" + raw.split(":")[0].split("-")[0].strip()[:30] + "”)"
        elif style.startswith("title") and re.search(r"\b(copy|draft|v\d+|version|doc|document|page|brief|deck)\b", raw, re.I):
            reason = "Document title / version"
        elif style.startswith(("heading", "title")) and SECTION_WORDS.search(raw) and len(raw.split()) <= 6:
            reason = "Section name (doc heading)"
        elif len(raw.split()) <= 4 and all(w in SECTION_VOCAB for w in re.findall(r"[a-z]+", raw.lower())) \
                and re.search(r"[a-z]", raw.lower()):
            reason = "Section name (doc structure)"
        if reason:
            if reason.startswith("Section name"):
                section = re.sub(r"^\s*(section|module|block)\s*[\d#ivx]*\s*[:.\-–—]?\s*", "", raw, flags=re.I)[:60] or raw[:60]
                loc = {"section": section, "line": n}
            refs.append({"text": raw, "reason": reason, "loc": loc})
            continue
        # copy line: role from table column, inline label, doc style, or shape
        role, text = None, raw
        if p.get("table_role"):
            role = role_from_label(p["table_role"])
        m = LABEL_RE.match(raw)
        if m and m.group(2).strip():
            role = role or role_from_label(m.group(1))
            text = m.group(2).strip()
        elif m:
            continue  # a label with nothing after it
        text = re.sub(r"^[-•*▪◦]\s+", "", text)
        link = None
        um = URL_RE.search(text)
        if um and len(text) > len(um.group(0)) + 2:
            link = um.group(0).rstrip(").,]")
            text = URL_RE.sub("", text)
            text = norm(re.sub(r"[(\[]\s*[)\]]", "", text))
            text = re.sub(r"\s*[(\[]\s*$", "", text).strip(" -–—→>|:")
        if not role:
            if style.startswith("title") or style == "heading 1":
                role = "title"
            elif style.startswith("heading"):
                role = "subtitle"
            elif NUM_RE.match(text) and len(text.split()) <= 4 and re.search(r"\d", text.split()[0]):
                role = "stat"
            elif text.isupper() and len(text.split()) <= 7:
                role = "tag"
            elif text.endswith("?") and len(text.split()) < 10:
                role = "subtitle"
            elif len(text.split()) >= 10 or re.search(r"[.!?]$", text):
                role = "paragraph"
            else:
                role = "text"
        if len(text) >= 2:
            copy.append({"text": text, "role": role, "link": link, "loc": loc, "fmt": fmt})
    return copy, refs


def live_role(c):
    """What kind of element a live text is, judged from tag and style."""
    if c.get("link"):
        return "button"
    h = c.get("htag")
    if h in ("h1",):
        return "title"
    if h:
        return "subtitle"
    if c.get("transform") == "uppercase" or (c["text"].isupper() and len(c["text"]) > 3):
        return "tag"
    if c.get("size", 16) >= 30:
        return "title"
    return "text"


def compare_copy(copy_lines, refs, live, view, shots, iss, page_url):
    lname = f"live_{view}"
    pool, seen = [], set()
    for c in live["blocks"] + live["texts"]:
        k = (c["text"], c["y"], c["x"])
        if k not in seen:
            seen.add(k)
            pool.append(c)
    rows, used = [], set()

    def best_match(text, role=None):
        best, score = None, 0
        nt = norm(text, True)
        for c in pool:
            sc = max(fuzz.ratio(nt, norm(c["text"], True)), fuzz.ratio(nt, norm(c.get("raw", ""), True)))
            if len(text) > 40 and sc < 85:
                sc = max(sc, fuzz.partial_ratio(nt, norm(c["text"], True)) - 5)
            if role and sc >= 75:
                lr = live_role(c)
                if role == "button" and lr == "button" or role in ("title", "subtitle") and c.get("htag") \
                        or role == "tag" and lr == "tag":
                    sc += 3
                if not c["visible"]:
                    sc -= 2
                if c.get("chrome") and role not in ("button", "text"):
                    sc -= 2
            if sc > score:
                best, score = c, sc
        return best, min(score, 100)

    for ln in copy_lines:
        text, role = ln["text"], ln["role"]
        best, score = best_match(text, role)
        doc_loc = {"view": "Copy doc", "section": ln["loc"]["section"], "element": f"line {ln['loc']['line']}"}
        row = {"doc": text, "role": role, "doc_loc": ln["loc"], "status": "missing", "live": "", "score": round(score),
               "notes": [], "issues": []}
        if best is not None and score >= 75:
            used.add(id(best))
            loc = live_loc(view, live, best)
            row.update(live=best["text"], loc=loc)
            th = shots.crop(lname, best)
            shown_ok = norm(text) in (norm(best["text"]), norm(best.get("raw", "")))
            loose_ok = norm(text, True) in (norm(best["text"], True), norm(best.get("raw", ""), True))
            if role in ("tag", "item_tag") and norm(text, True) in (norm(best["text"], True), norm(best.get("raw", ""), True)) \
                    and norm(text).lower() == norm(best["text"]).lower():
                shown_ok = True  # tags are shown uppercase by design; case alone isn't a copy error
            if shown_ok:
                row["status"] = "ok"
            elif loose_ok:
                row["status"] = "punctuation"
                i = iss.add("copy", "low", "copy_punct", f"{ROLE_NAMES[role]}: punctuation or capitalization differs from the doc",
                            view=view, loc=loc, expected=text, actual=best["text"], diff=word_diff(text, best["text"]),
                            thumb=th, box=best, extra={"doc_loc": doc_loc, "role": role})
                row["issues"].append(i["id"]) if i else None
            else:
                row["status"] = "diff"
                i = iss.add("copy", "medium", "copy_diff", f"{ROLE_NAMES[role]}: text on the site differs from the doc",
                            view=view, loc=loc, expected=text, actual=best["text"], diff=word_diff(text, best["text"]),
                            thumb=th, box=best, extra={"doc_loc": doc_loc, "role": role})
                row["issues"].append(i["id"]) if i else None
            if not best["visible"]:
                row["notes"].append("hidden on load")
            # role-specific checks
            if role == "stat":
                dn = [re.sub(r"[\s,]", "", x) for x in NUM_RE.findall(text) if re.search(r"\d", x)]
                ln_ = [re.sub(r"[\s,]", "", x) for x in NUM_RE.findall(best["text"]) if re.search(r"\d", x)]
                if dn and dn != ln_[:len(dn)]:
                    i = iss.add("copy", "high", "stat_diff", "Stat / number on the site doesn't match the doc",
                                view=view, loc=loc, expected=" ".join(dn), actual=" ".join(ln_) or "—", text=best["text"],
                                thumb=th, box=best, extra={"doc_loc": doc_loc, "role": role})
                    row["issues"].append(i["id"]) if i else None
                    row["status"] = "diff"
            if role == "button" and not best.get("link"):
                i = iss.add("copy", "medium", "btn_not_link", "Doc marks this as a button, but on the site it isn't clickable",
                            view=view, loc=loc, expected="link / button", actual=f"<{best['tag']}> text", text=best["text"],
                            thumb=th, box=best, extra={"doc_loc": doc_loc, "role": role})
                row["issues"].append(i["id"]) if i else None
            if role == "button" and ln.get("link") and best.get("link"):
                want = urllib.parse.urlparse(urllib.parse.urljoin(page_url, ln["link"]))
                got = urllib.parse.urlparse(urllib.parse.urljoin(page_url, best["link"].get("href") or ""))
                if (want.netloc, want.path.rstrip("/")) != (got.netloc, got.path.rstrip("/")):
                    i = iss.add("links", "medium", "btn_link_diff", "Button links to a different page than the doc says",
                                view=view, loc=loc, expected=ln["link"], actual=best["link"].get("href") or "(no link)",
                                text=best["text"], thumb=th, box=best, extra={"doc_loc": doc_loc, "role": role})
                    row["issues"].append(i["id"]) if i else None
            if role == "title" and not best.get("htag") and best.get("size", 0) < 28:
                row["notes"].append("not styled/marked as a heading")
            if role == "tag" and best.get("htag") in ("h1", "h2"):
                row["notes"].append("section tag uses a big heading tag (SEO)")
        else:
            i = iss.add("copy", "high", "copy_missing", f"{ROLE_NAMES[role]} from the doc is missing on the site",
                        view=view, loc={"view": view, "section": f"doc: {ln['loc']['section']}"}, expected=text,
                        actual="not found", extra={"doc_loc": doc_loc, "role": role})
            row["issues"].append(i["id"]) if i else None
            if best is not None and score >= 55:
                row["closest"] = best["text"][:200]
        rows.append(row)

    # reference-only text must not appear on the site
    ref_rows = []
    for r in refs:
        best, score = best_match(r["text"])
        found = best is not None and score >= 92 and best["visible"]
        row = {"doc": r["text"], "reason": r["reason"], "doc_loc": r["loc"], "found": False}
        if found:
            words = len(r["text"].split())
            loc = live_loc(view, live, best)
            row.update(found=True, live=best["text"], loc=loc)
            if words >= 3 and not r["reason"].startswith("Section name"):
                i = iss.add("copy", "high", "ref_on_site", "Reference-only text from the doc is showing on the site",
                            view=view, loc=loc, expected=f"not on the page ({r['reason']})", actual=best["text"],
                            thumb=shots.crop(lname, best), box=best,
                            extra={"doc_loc": {"view": "Copy doc", "section": r["loc"]["section"],
                                               "element": f"line {r['loc']['line']}"}})
                row["issue"] = i["id"] if i else None
            else:
                row["note"] = "Short label also used on the site — probably fine, verify"
        ref_rows.append(row)

    # long live paragraphs that are not in the doc
    for b in live["blocks"]:
        t = norm(b["text"], True)
        if id(b) in used or b["chrome"] or len(t) < 30 or not b["visible"] or \
                b["tag"] not in ("p", "h1", "h2", "h3", "h4", "li", "blockquote"):
            continue
        if max((fuzz.ratio(t, norm(l["text"], True)) for l in copy_lines), default=0) < 75 and \
                max((fuzz.partial_ratio(t, norm(l["text"], True)) for l in copy_lines if len(l["text"]) > 40),
                    default=0) < 85:
            iss.add("copy", "info", "extra_text", "Text on the site that isn't in the copy doc", view=view,
                    loc=live_loc(view, live, b), expected="—", actual=b["text"][:240],
                    thumb=shots.crop(lname, b), box=b)
    return rows, ref_rows


# ----------------------------------------------------------------------------- Links & buttons
def audit_links(live_runs, page_url, shots, iss):
    page_base = page_url.split("#")[0]
    targets = {}
    for view, live in live_runs:
        lname = f"live_{view}"
        ids = set(live["ids"])
        for l in live["links"]:
            label = l["text"] or ("[icon]" if l["img"] else "[no text]")
            href = (l["href"] or "").strip()
            loc = live_loc(view, live, l)
            th = (lambda l=l: shots.crop(lname, l)) if l["visible"] else (lambda: None)
            if l["tag"] == "a":
                if not href or href == "#" or href.lower().startswith("javascript:"):
                    if not l["ariaControls"] and l["visible"]:
                        iss.add("links", "high", "no_link", "Button / link has no destination", view=view, loc=loc,
                                expected="a real URL", actual=href or "(no href)", text=label, thumb=th(), box=l)
                    continue
                if not l["text"] and not l["img"] and l["visible"]:
                    iss.add("links", "low", "link_no_label", "Link has no text or accessible label", view=view, loc=loc,
                            actual=href, thumb=th(), box=l)
                if href.startswith("#") or href.startswith(page_base + "#"):
                    frag = href.split("#", 1)[1]
                    if frag and frag not in ids:
                        iss.add("links", "high", "anchor_missing", "Anchor link points to a section that doesn't exist",
                                view=view, loc=loc, expected=f"#{frag}", actual="no element with that id", text=label,
                                thumb=th(), box=l)
                    continue
                if href.startswith("mailto:"):
                    if not re.match(r"mailto:[^@\s]+@[^@\s]+\.[a-z]{2,}", href, re.I):
                        iss.add("links", "high", "bad_mailto", "Email link is malformed", view=view, loc=loc,
                                actual=href, text=label, thumb=th(), box=l)
                    continue
                if href.startswith("tel:"):
                    if len(re.sub(r"\D", "", href)) < 7:
                        iss.add("links", "high", "bad_tel", "Phone link is malformed", view=view, loc=loc,
                                actual=href, text=label, thumb=th(), box=l)
                    continue
                if " " in href:
                    iss.add("links", "low", "url_space", "URL has unencoded spaces (use %20)", view=view, loc=loc,
                            actual=href, text=label, thumb=th(), box=l)
                full = urllib.parse.urljoin(page_url, href)
                t = targets.setdefault(full, {"labels": set(), "first": None})
                t["labels"].add(label)
                if t["first"] is None and l["visible"]:
                    t["first"] = (view, live, l)
            elif l["visible"] and not l["inForm"] and not l["ariaControls"] and l["type"] != "submit":
                iss.add("links", "info", "button_no_href", "Button without a link — confirm it opens or does something",
                        view=view, loc=loc, actual=f"<{l['tag']}>", text=label, thumb=th(), box=l)

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                         "Accept-Language": "en-US,en;q=0.9"})

    def check(u):
        safe = urllib.parse.quote(u, safe=":/?#[]@!$&'()*+,;=%~")
        try:
            r = sess.head(safe, allow_redirects=True, timeout=25)
            if r.status_code in (400, 403, 404, 405, 429, 501) or r.status_code >= 500:
                r = sess.get(safe, allow_redirects=True, timeout=25, stream=True)
                r.close()
            return u, r.status_code, r.url
        except requests.RequestException as e:
            return u, f"error: {type(e).__name__}", None

    results = {}
    with ThreadPoolExecutor(12) as ex:
        for u, code, final in ex.map(check, targets):
            t = targets[u]
            labels = ", ".join(sorted(t["labels"]))[:120]
            results[u] = {"status": code, "final": final, "labels": labels}
            host = urllib.parse.urlparse(u).netloc
            kw = {"url": u, "text": labels}
            if t["first"]:
                view, live, l = t["first"]
                kw.update(view=view, loc=live_loc(view, live, l), thumb=shots.crop(f"live_{view}", l), box=l)
            if isinstance(code, str):
                iss.add("links", "high", "link_error", "Link doesn't respond", expected="200", actual=code, **kw)
            elif code in (999, 429) or (code == 403 and any(h in host for h in ("linkedin", "instagram", "facebook", "x.com", "twitter"))):
                iss.add("links", "info", "link_blocked", "Site blocks automated checks — open it once by hand",
                        expected="200", actual=str(code), **kw)
            elif code >= 400:
                iss.add("links", "high", "link_broken", "Broken link", expected="200", actual=str(code), **kw)
            elif final and urllib.parse.urlparse(final).netloc == host and \
                    urllib.parse.urlparse(final).path.rstrip("/") != urllib.parse.urlparse(u).path.rstrip("/"):
                iss.add("links", "low", "link_redirect", "Link redirects to another page — point it to the final URL",
                        expected=u, actual=final, **kw)
    return results


# ----------------------------------------------------------------------------- Report
def render_report(ctx, path):
    tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_template.html"), encoding="utf-8").read()
    data = json.dumps(ctx, ensure_ascii=False).replace("<", "\\u003c")
    out = tpl.replace("/*__DATA__*/null", data)
    open(path, "w", encoding="utf-8").write(out)


def summary_md(ctx):
    issues = ctx["issues"]
    c = {s: sum(1 for i in issues if i["severity"] == s) for s in SEV_ORDER}
    lines = [f"## Site QA — {ctx['site']}", "",
             f"**{c['high']} high · {c['medium']} medium · {c['low']} low · {c['info']} info**", "",
             "| Area | High | Medium | Low |", "|---|---|---|---|"]
    for k, v in CAT_LABEL.items():
        lines.append(f"| {v} | " + " | ".join(str(sum(1 for i in issues if i['category'] == k and i['severity'] == s))
                                            for s in ("high", "medium", "low")) + " |")
    lines += ["", "### High-priority issues", ""]
    for i in [i for i in issues if i["severity"] == "high"][:40]:
        loc = i.get("loc") or {}
        where = " › ".join(x for x in (loc.get("view"), loc.get("section")) if x)
        lines.append(f"- **{CAT_LABEL[i['category']]}** — {i['title']}"
                     + (f" · `{(i.get('text') or i.get('actual') or '')[:80]}`" if (i.get('text') or i.get('actual')) else "")
                     + (f" ({where})" if where else ""))
    lines += ["", "Download the **site-qa-report** artifact and open `report.html`."]
    return "\n".join(lines)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--figma", default="")
    ap.add_argument("--figma-mobile", default="")
    ap.add_argument("--doc", default="")
    ap.add_argument("--out", default="report")
    ap.add_argument("--mobile-width", type=int, default=390)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    iss, shots, notes = Issues(), Shots(), []
    now = dt.datetime.now(dt.timezone.utc)
    ctx = {"site": a.site, "figma": a.figma, "figma_mobile": a.figma_mobile, "doc": a.doc,
           "date": now.strftime("%Y-%m-%d %H:%M UTC"),
           "run_id": hashlib.sha1(f"{a.site}{now.isoformat()}".encode()).hexdigest()[:10], "views": []}

    fig = {"Desktop": None, "Mobile": None}
    if a.figma or a.figma_mobile:
        tok = os.environ.get("FIGMA_TOKEN")
        if not tok:
            raise SystemExit("FIGMA_TOKEN is not set.")
        fg = Figma(tok)
        for view, url, mobile in (("Desktop", a.figma, False), ("Mobile", a.figma_mobile, True)):
            if not url:
                continue
            key, nid = fg.parse_url(url)
            if not nid:
                raise SystemExit("A Figma link has no node-id. Copy the link of the page frame.")
            fid = fg.resolve_frame(key, nid, mobile=mobile)
            print(f"Figma {view} frame: {fid}")
            fig[view] = fg.extract(key, fid, a.out, view)
            shots.add_image(f"figma_{view}", fig[view]["png"])
            notes.append(f"{view} design: “{fig[view]['name']}” ({fig[view]['w']:.0f}×{fig[view]['h']:.0f})")

    widths = {"Desktop": int(fig["Desktop"]["w"]) if fig["Desktop"] else 1440,
              "Mobile": int(fig["Mobile"]["w"]) if fig["Mobile"] else a.mobile_width}
    live = {}
    for view in ("Desktop", "Mobile"):
        print(f"Capturing live page ({view}, {widths[view]}px)…")
        live[view] = await capture_live(a.site, widths[view], a.out, view)
        shots.add_image(f"live_{view}", live[view]["png"])

    for view in ("Desktop", "Mobile"):
        v = {"tag": view, "width": live[view]["width"], "sections": [], "live_sections": live[view]["sections"]}
        if fig[view]:
            v["sections"] = compare_design(fig[view], live[view], view, shots, iss)
        else:
            im = shots.imgs[f"live_{view}"]
            v["live_full"] = img_b64(im, 700, 7000)
            f = im.width / 700 if im.width > 700 else 1
            v["live_shown_h"] = min(im.height, 7000 * f)
        check_media_display(live[view], view, shots, iss)
        ctx["views"].append(v)

    ctx["copy_rows"], ctx["ref_rows"] = [], []
    if a.doc:
        print("Reading copy doc…")
        kind, payload = fetch_doc(a.doc)
        copy_lines, refs = classify_doc(list(doc_paragraphs(kind, payload)))
        notes.append(f"Copy doc: {len(copy_lines)} copy lines, {len(refs)} reference-only lines")
        ctx["copy_rows"], ctx["ref_rows"] = compare_copy(copy_lines, refs, live["Desktop"], "Desktop", shots, iss, a.site)

    print("Checking links…")
    res = audit_links([("Desktop", live["Desktop"]), ("Mobile", live["Mobile"])], a.site, shots, iss)
    ctx["links"] = [{"url": u, **v} for u, v in sorted(res.items())]

    items = sorted(iss.items, key=lambda i: (SEV_ORDER[i["severity"]], list(CAT_LABEL).index(i["category"]),
                                             (i.get("loc") or {}).get("y") or 0))
    for n, i in enumerate(items, 1):
        i["n"] = n
    ctx.update(issues=items, notes=notes, categories=CAT_LABEL, roles=ROLE_NAMES, thumbs=shots.store)

    render_report(ctx, f"{a.out}/report.html")
    json.dump({k: v for k, v in ctx.items() if k not in ("views", "thumbs")},
              open(f"{a.out}/report.json", "w"), indent=1, ensure_ascii=False)
    md = summary_md(ctx)
    open(f"{a.out}/summary.md", "w").write(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(md + "\n")
    print(md)
    if os.environ.get("FAIL_ON_HIGH", "false").lower() == "true" and any(i["severity"] == "high" for i in items):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

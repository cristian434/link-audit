#!/usr/bin/env python3
"""
Site QA — compares a live page against its Figma layout and its copy doc,
and audits every link and button.

Usage:
  python site_qa.py --site URL [--figma URL] [--figma-mobile URL] [--doc URL] [--out report]

Env:
  FIGMA_TOKEN       Figma personal access token (scope: file_content:read)
  GOOGLE_SA_JSON    optional: service-account JSON, for copy docs not shared by link
"""
import argparse, asyncio, base64, datetime as dt, difflib, html, io, json, os, re, sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image, ImageChops, ImageStat
from rapidfuzz import fuzz

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

# Tolerances for the style comparison
TOL = {"size": 1.0, "line_height": 2.0, "letter_spacing": 0.3, "color": 24}
MIN_FIGMA_FONT = 10      # smaller Figma text is usually UI drawn inside an illustration
MATCH_OK = 90            # text match score to compare styles
MATCH_DIFF = 70          # below this a Figma text counts as missing on live


def issue(cat, sev, title, where="", expected="", actual="", extra=None):
    d = {"category": cat, "severity": sev, "title": title, "where": where,
         "expected": expected, "actual": actual}
    if extra:
        d.update(extra)
    return d


def norm(s, loose=False):
    s = (s or "").replace("\u00a0", " ").replace("\u200b", "")
    if loose:
        s = (s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"')
              .replace("\u201d", '"').replace("\u2013", "-").replace("\u2014", "-")
              .replace("\u2026", "..."))
        s = re.sub(r"\s*-\s*", " - ", s)
        s = re.sub(r"[.!:;,]+$", "", s.strip())
        s = s.lower()
    return " ".join(s.split())


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
        """The link may point at a whole page frame, a section, a note or any layer.
        Walk up to the section/page and pick the page-sized frame closest to it."""
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
        sections = []
        for i, c in enumerate(frame.get("children", [])):
            if c.get("visible") is False:
                continue
            b = c["absoluteBoundingBox"]
            if b["height"] < 40 or b["width"] < fb["width"] * 0.5:
                continue
            sections.append({"idx": len(sections), "id": c["id"], "name": c["name"],
                             "y": b["y"] - fb["y"], "h": b["height"]})
        # a frame with a single wrapper child: use the wrapper's children as sections
        if len(sections) == 1 and frame["children"]:
            wrap = [c for c in frame["children"] if c["id"] == sections[0]["id"]][0]
            sub = [c for c in wrap.get("children", []) if c.get("visible") is not False
                   and c["absoluteBoundingBox"]["height"] >= 40]
            if len(sub) > 1:
                sections = [{"idx": i, "id": c["id"], "name": c["name"],
                             "y": c["absoluteBoundingBox"]["y"] - fb["y"],
                             "h": c["absoluteBoundingBox"]["height"]} for i, c in enumerate(sub)]

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
                        "x": bb.get("x", 0) - fb["x"], "y": y, "w": bb.get("width"),
                        "h": bb.get("height"), "section": sec_for(y), "id": n["id"]})
            for f in n.get("fills", []) if isinstance(n.get("fills"), list) else []:
                if f.get("type") in ("IMAGE", "VIDEO") and f.get("visible", True) \
                        and bb.get("width", 0) >= 80 and bb.get("height", 0) >= 80:
                    y = bb["y"] - fb["y"]
                    media.append({"kind": f["type"].lower(), "name": n["name"], "w": bb["width"],
                                  "h": bb["height"], "y": y, "section": sec_for(y),
                                  "scale": f.get("scaleMode")})
                    break
            for c in n.get("children", []):
                walk(c)

        walk(frame)
        # renders: whole frame + each section
        ids = [frame_id] + [s["id"] for s in sections]
        urls = {}
        for i in range(0, len(ids), 10):
            urls.update(self.get(f"/images/{key}", ids=",".join(ids[i:i + 10]),
                                 format="png", scale=1)["images"])
        for s in sections:
            s["png"] = self._download(urls.get(s["id"]), f"{outdir}/figma_{tag}_s{s['idx']}.png")
        full = self._download(urls.get(frame_id), f"{outdir}/figma_{tag}_full.png")
        return {"id": frame_id, "name": frame["name"], "w": fb["width"], "h": fb["height"],
                "sections": sections, "texts": texts, "media": media, "png": full}

    def _download(self, url, path):
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
  const inChrome = e => !!e.closest('header, nav, footer, [class*="navbar"], [class*="footer"]');
  const out = {texts: [], blocks: [], links: [], media: [], display: []};
  const seen = new Set();
  const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (tw.nextNode()) {
    const t = tw.currentNode; if (!t.textContent.trim()) continue;
    const el = t.parentElement;
    if (!el || seen.has(el) || ['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','svg'].includes(el.tagName)) continue;
    seen.add(el);
    const s = getComputedStyle(el);
    const text = (el.innerText || el.textContent).replace(/\s+/g, ' ').trim();
    if (!text) continue;
    out.texts.push({text, tag: el.tagName.toLowerCase(), visible: vis(el), chrome: inChrome(el),
      family: s.fontFamily.split(',')[0].replace(/["']/g, '').trim(), weight: +s.fontWeight,
      italic: s.fontStyle === 'italic', size: parseFloat(s.fontSize),
      lh: s.lineHeight === 'normal' ? null : parseFloat(s.lineHeight),
      ls: s.letterSpacing === 'normal' ? 0 : parseFloat(s.letterSpacing),
      transform: s.textTransform, color: s.color, ...box(el)});
  }
  document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,li,blockquote,a,button,label,figcaption,td,th').forEach(el => {
    const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (text) out.blocks.push({text, tag: el.tagName.toLowerCase(), visible: vis(el), chrome: inChrome(el), ...box(el)});
  });
  document.querySelectorAll('a, button, [role="button"], input[type="submit"]').forEach(el => {
    out.links.push({tag: el.tagName.toLowerCase(), href: el.getAttribute('href'), target: el.getAttribute('target'),
      text: (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '').replace(/\s+/g, ' ').trim().slice(0, 90),
      img: !!el.querySelector('img,svg'), inForm: !!el.closest('form'), type: el.getAttribute('type'),
      ariaControls: el.hasAttribute('aria-controls') || el.hasAttribute('aria-expanded') || el.hasAttribute('data-w-tab'),
      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 80),
      visible: vis(el), chrome: inChrome(el), ...box(el)});
  });
  document.querySelectorAll('img').forEach(m => out.media.push({kind: 'image', src: m.currentSrc || m.src, alt: m.getAttribute('alt'),
    complete: m.complete, nw: m.naturalWidth, nh: m.naturalHeight, fit: getComputedStyle(m).objectFit,
    loading: m.loading, visible: vis(m), ...box(m)}));
  document.querySelectorAll('video').forEach(v => out.media.push({kind: 'video', src: v.currentSrc || v.src || (v.querySelector('source')||{}).src,
    ready: v.readyState, error: v.error ? v.error.code : null, autoplay: v.autoplay, muted: v.muted, loop: v.loop,
    playsinline: v.playsInline, paused: v.paused, poster: v.poster, nw: v.videoWidth, nh: v.videoHeight,
    fit: getComputedStyle(v).objectFit, visible: vis(v), ...box(v)}));
  document.querySelectorAll('iframe').forEach(f => out.media.push({kind: 'iframe', src: f.src, visible: vis(f), ...box(f)}));
  document.querySelectorAll('body *').forEach(e => {
    const s = getComputedStyle(e); if (!s.backgroundImage.includes('url(')) return;
    const r = e.getBoundingClientRect(); if (r.width < 80 || r.height < 80) return;
    out.media.push({kind: 'background', src: s.backgroundImage.slice(5, -2), size: s.backgroundSize, visible: vis(e), ...box(e)});
  });
  // display checks
  const vw = document.documentElement.clientWidth;
  out.pageWidth = document.documentElement.scrollWidth; out.viewport = vw;
  out.pageHeight = document.documentElement.scrollHeight;
  const clipped = el => { for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX; if (o === 'hidden' || o === 'clip' || o === 'auto' || o === 'scroll') return true; } return false; };
  document.querySelectorAll('body *').forEach(e => {
    if (!vis(e)) return; const r = e.getBoundingClientRect();
    if (r.right > vw + 2 && r.width < vw * 3 && !clipped(e))
      out.display.push({type: 'overflow', tag: e.tagName.toLowerCase(), cls: (typeof e.className === 'string' ? e.className : '').slice(0, 60),
        text: (e.innerText || '').trim().slice(0, 60), right: Math.round(r.right), ...box(e)});
    const s = getComputedStyle(e);
    if (e.children.length === 0 && (e.innerText || '').trim() && (s.overflow === 'hidden' || s.textOverflow === 'ellipsis')
        && (e.scrollWidth > e.clientWidth + 2 || e.scrollHeight > e.clientHeight + 4))
      out.display.push({type: 'clipped_text', tag: e.tagName.toLowerCase(), text: e.innerText.trim().slice(0, 80), ...box(e)});
  });
  const ids = new Set([...document.querySelectorAll('[id]')].map(e => e.id));
  out.ids = [...ids];
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
        # scroll slowly so lazy images, videos and scroll animations run
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


# ----------------------------------------------------------------------------- Design comparison
def compare_design(fig, live, tag, outdir):
    issues, sections_out = [], []
    cands = [t for t in live["texts"] if len(t["text"]) >= 2]
    matches = {}

    for ft in fig["texts"]:
        best, score = None, 0
        for lt in cands:
            sc = fuzz.ratio(norm(ft["text"], True), norm(lt["text"], True))
            if sc > score or (sc == score and best and lt["visible"] and not best["visible"]):
                best, score = lt, sc
        where = f"{tag} · {sec_name(fig, ft['section'])}"
        if score < MATCH_DIFF:
            if ft["size"] < 12:
                issues.append(issue("design", "low", "Small design text not found on live (may be part of an illustration)",
                                    where, ft["text"][:160], "—"))
            else:
                issues.append(issue("design", "medium", "Design text not found on the live page", where,
                                    ft["text"][:160], "—"))
            continue
        matches[ft["id"]] = (best, score)
        if not best["visible"]:
            issues.append(issue("display", "low", "Element exists but is hidden on load (collapsed, tab or animation)",
                                where, ft["text"][:120], "hidden"))
            continue
        if score < MATCH_OK:
            issues.append(issue("design", "low", "Text differs from the Figma layout", where,
                                ft["text"][:200], best["text"][:200], {"diff": word_diff(ft["text"], best["text"])}))
        issues += style_diffs(ft, best, where)

    # section geometry & visual similarity, anchored on matched text
    shot = Image.open(live["png"]).convert("RGB")
    anchors = []
    for s in fig["sections"]:
        sec_texts = sorted([t for t in fig["texts"] if t["section"] == s["idx"] and t["id"] in matches
                            and matches[t["id"]][1] >= MATCH_OK and matches[t["id"]][0]["visible"]],
                           key=lambda t: -t["size"])
        if sec_texts:
            t = sec_texts[0]
            live_top = matches[t["id"]][0]["y"] - (t["y"] - s["y"]) * live["width"] / fig["w"]
            anchors.append((s, 0 if s["idx"] == 0 else max(0, live_top)))
        else:
            anchors.append((s, None))
    for i, (s, top) in enumerate(anchors):
        nxt = next((a[1] for a in anchors[i + 1:] if a[1] is not None), None)
        entry = {"name": s["name"], "idx": s["idx"], "figma_h": round(s["h"]), "live_h": None,
                 "similarity": None, "figma_img": None, "live_img": None}
        if top is None:
            issues.append(issue("design", "medium", "Section could not be located on the live page",
                                f"{tag} · {s['name']}", "Section present in Figma", "No matching content found"))
        else:
            bottom = nxt if nxt is not None and nxt > top else min(shot.height, top + s["h"])
            live_h = bottom - top
            entry["live_h"] = round(live_h)
            if nxt is not None and abs(live_h - s["h"]) / s["h"] > 0.15:
                issues.append(issue("design", "medium", "Section height differs more than 15% from Figma (spacing or layout)",
                                    f"{tag} · {s['name']}", f"{round(s['h'])}px", f"{round(live_h)}px"))
            crop = shot.crop((0, int(top), shot.width, int(min(shot.height, top + max(live_h, 40)))))
            if s.get("png") and os.path.exists(s["png"]):
                fimg = Image.open(s["png"]).convert("RGB")
                sim = similarity(fimg, crop)
                entry["similarity"] = sim
                entry["figma_img"] = img_b64(fimg)
                if sim < 70:
                    issues.append(issue("design", "medium" if sim < 55 else "low",
                                        f"Section looks visually different from Figma ({sim}% similar) — review side by side",
                                        f"{tag} · {s['name']}"))
            entry["live_img"] = img_b64(crop)
            entry["live_top"] = round(top)
        sections_out.append(entry)

    # media per section
    live_media = [m for m in live["media"] if m["visible"] and m["w"] >= 80 and m["h"] >= 80]
    for i, (s, top) in enumerate(anchors):
        if top is None:
            continue
        nxt = next((a[1] for a in anchors[i + 1:] if a[1] is not None), top + s["h"])
        fm = [m for m in fig["media"] if m["section"] == s["idx"]]
        lm = [m for m in live_media if top <= m["y"] < nxt]
        if any(m["kind"] == "video" for m in fm) and not any(m["kind"] in ("video", "iframe") for m in lm):
            issues.append(issue("media", "medium", "Figma has a video here but the live section shows no video",
                                f"{tag} · {s['name']}", "video", "none"))
        if fm and not lm:
            issues.append(issue("media", "medium", "Images in Figma are missing in the live section",
                                f"{tag} · {s['name']}", f"{len(fm)} image(s)", "0"))
    return issues, sections_out


def sec_name(fig, idx):
    if idx is None:
        return "outside sections"
    return next((s["name"] for s in fig["sections"] if s["idx"] == idx), "?")


def style_diffs(ft, lt, where):
    out, label = [], ft["text"][:70]
    fam_f, fam_l = (ft["family"] or "").lower().replace(" ", ""), (lt["family"] or "").lower().replace(" ", "")
    if fam_f and fam_l and fam_f != fam_l:
        out.append(issue("style", "high", "Font family differs", where, ft["family"], lt["family"], {"text": label}))
    if ft["weight"] and lt["weight"] and abs(ft["weight"] - lt["weight"]) >= 100:
        out.append(issue("style", "medium", "Font weight differs", where, str(ft["weight"]), str(lt["weight"]), {"text": label}))
    if abs(ft["size"] - lt["size"]) > TOL["size"]:
        out.append(issue("style", "medium", "Font size differs", where, f"{ft['size']:g}px", f"{lt['size']:g}px", {"text": label}))
    if ft["lh"] and lt["lh"] and abs(ft["lh"] - lt["lh"]) > TOL["line_height"]:
        out.append(issue("style", "low", "Line height differs", where, f"{ft['lh']:.0f}px", f"{lt['lh']:.0f}px", {"text": label}))
    if abs((ft["ls"] or 0) - (lt["ls"] or 0)) > TOL["letter_spacing"]:
        out.append(issue("style", "low", "Letter spacing differs", where, f"{ft['ls']:.2f}px", f"{lt['ls']:.2f}px", {"text": label}))
    if bool(ft["italic"]) != bool(lt["italic"]):
        out.append(issue("style", "low", "Italic style differs", where, str(ft["italic"]), str(lt["italic"]), {"text": label}))
    lc = parse_rgb(lt["color"])
    if ft["color"] and lc and color_dist(ft["color"], lc) > TOL["color"]:
        out.append(issue("style", "medium", "Text color differs", where, hexc(ft["color"]), hexc(lc),
                         {"text": label, "swatch": [hexc(ft["color"]), hexc(lc)]}))
    fig_upper = ft["case"] == "UPPER" or (ft["text"].isupper() and len(ft["text"]) > 3)
    live_upper = lt["transform"] == "uppercase" or (lt["text"].isupper() and len(lt["text"]) > 3)
    if fig_upper != live_upper:
        out.append(issue("style", "low", "Letter case differs (uppercase vs normal)", where,
                         "UPPERCASE" if fig_upper else "Normal", "UPPERCASE" if live_upper else "Normal", {"text": label}))
    return out


def similarity(a, b):
    """Structural similarity 0–100: correlation of downscaled, contrast-normalized grayscale."""
    import numpy as np
    from PIL import ImageOps
    w = 96
    h = max(8, int(a.height * w / a.width))
    x = np.asarray(ImageOps.autocontrast(a.convert("L").resize((w, h))), dtype=float).ravel()
    y = np.asarray(ImageOps.autocontrast(b.convert("L").resize((w, h))), dtype=float).ravel()
    if x.std() < 1 and y.std() < 1:
        return 100 if abs(x.mean() - y.mean()) < 10 else 0
    if x.std() < 1 or y.std() < 1:
        return 0
    return int(round(max(0.0, float(np.corrcoef(x, y)[0, 1])) * 100))


def img_b64(img, maxw=900):
    img = img.copy()
    if img.width > maxw:
        img = img.resize((maxw, int(img.height * maxw / img.width)))
    if img.height > 2400:
        img = img.crop((0, 0, img.width, 2400))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=72)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


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


# ----------------------------------------------------------------------------- Media & display checks
def check_media_display(live, tag):
    out, broken = [], []
    for m in live["media"]:
        if not m["visible"] and m["kind"] != "image":
            continue
        short = (m.get("src") or "")[-70:]
        if m["kind"] == "image":
            if m["complete"] and m["nw"] == 0 and m.get("src"):
                broken.append(short)
                continue
            if not m["visible"] or m["w"] < 40:
                continue
            if m["alt"] is None:
                out.append(issue("media", "low", "Image without alt attribute", tag, "alt text", short))
            if m["nw"] and m["nh"] and m["fit"] in ("fill", "") and m["h"] > 0:
                r_nat, r_ren = m["nw"] / m["nh"], m["w"] / m["h"]
                if abs(r_nat - r_ren) / r_nat > 0.04:
                    out.append(issue("media", "medium", "Image is stretched/squashed (aspect ratio distorted)", tag,
                                     f"ratio {r_nat:.2f}", f"ratio {r_ren:.2f}", {"src": short}))
            if m["nw"] and m["nw"] < m["w"] * 0.9:
                out.append(issue("media", "low", "Image is upscaled — will look blurry", tag,
                                 f"≥{m['w']}px wide file", f"{m['nw']}px file", {"src": short}))
            if m["nw"] and m["nw"] > m["w"] * 3 and m["nw"] > 2000:
                out.append(issue("media", "info", "Image file is much larger than displayed (performance)", tag,
                                 f"~{m['w'] * 2}px", f"{m['nw']}px", {"src": short}))
        elif m["kind"] == "video":
            if m["error"] or m["ready"] == 0:
                out.append(issue("media", "high", "Video doesn't load", tag, "", short))
            elif m["autoplay"] and m["paused"]:
                out.append(issue("media", "medium", "Autoplay video is not playing", tag, "playing", "paused", {"src": short}))
            if m["autoplay"] and not m["muted"]:
                out.append(issue("media", "medium", "Autoplay video isn't muted (browsers block it)", tag, "muted", "not muted"))
            if m["autoplay"] and not m["playsinline"] and live["width"] < 600:
                out.append(issue("media", "medium", "Video lacks playsinline (goes fullscreen / won't autoplay on iPhone)", tag))
            if m["nw"] and m["h"] and m["fit"] in ("fill", "") and abs(m["nw"] / m["nh"] - m["w"] / m["h"]) / (m["nw"] / m["nh"]) > 0.04:
                out.append(issue("media", "medium", "Video is stretched (aspect ratio distorted)", tag))
    broken = list(dict.fromkeys(broken))
    if len(broken) > 8:
        out.append(issue("media", "high", f"{len(broken)} images don't load (likely one shared cause: CDN, blocked host or bad export)",
                         tag, "", ", ".join(broken[:3]) + " …"))
    else:
        out += [issue("media", "high", "Broken image (doesn't load)", tag, "", b) for b in broken]
    reported = {(m.get("src") or "")[-70:] for m in live["media"]}
    failed = [f for f in live["failed"] if f["type"] in ("image", "media", "stylesheet", "font", "script")
              and f["url"][-70:] not in reported]
    by_host = {}
    for f in failed:
        by_host.setdefault((urllib.parse.urlparse(f["url"]).netloc, f["type"]), []).append(f)
    for (host, typ), fs in by_host.items():
        cat = "media" if typ in ("image", "media") else "display"
        if len(fs) > 5:
            out.append(issue(cat, "high", f"{len(fs)} {typ} files failed to load from {host}", tag, "200",
                             str(fs[0]["status"]), {"src": fs[0]["url"][-90:]}))
        else:
            for f in fs:
                out.append(issue(cat, "high", f"Asset failed to load ({typ})", tag, "200", str(f["status"]),
                                 {"src": f["url"][-90:]}))
    if live["pageWidth"] > live["viewport"] + 2:
        out.append(issue("display", "high", "Page scrolls horizontally (content wider than the screen)", tag,
                         f"{live['viewport']}px", f"{live['pageWidth']}px"))
    seen = set()
    for d in live["display"]:
        key = (d["type"], d.get("cls"), d.get("text"))
        if key in seen:
            continue
        seen.add(key)
        if d["type"] == "overflow" and len([1 for k in seen if k[0] == "overflow"]) <= 8:
            out.append(issue("display", "medium", "Element goes past the right edge of the screen", tag,
                             f"≤{live['viewport']}px", f"right edge {d['right']}px", {"text": f"<{d['tag']}> {d['cls']} {d['text']}"}))
        if d["type"] == "clipped_text":
            out.append(issue("display", "medium", "Text is cut off by its container", tag, "", d["text"]))
    if live["width"] < 600:
        small = [l for l in live["links"] if l["visible"] and (l["w"] < 32 or l["h"] < 32) and l["w"] > 0]
        for l in small[:10]:
            out.append(issue("display", "low", "Tap target smaller than 32px on mobile", tag, "≥ 44px",
                             f"{l['w']}×{l['h']}px", {"text": l["text"] or l["href"]}))
    return out


# ----------------------------------------------------------------------------- Copy doc
LABEL_RE = re.compile(r"^\s*(?:[-•*▪◦]\s*)?(?:H[1-6]|eyebrow|kicker|overline|headline|sub-?head(?:line|er)?|title|"
                      r"body(?:\s*copy)?|copy|cta|button|btn|link|label|tag|caption|description|desc|quote|"
                      r"stat|name|role|question|answer|q|a)\s*\d*\s*[:\-–—|]\s+", re.I)
SKIP_RE = re.compile(r"^\s*(\[.*\]|\(.*\)|https?://\S+|section\b.*|note\b.*|image\b.*|visual\b.*|"
                     r"design\b.*|dev\b.*|seo\b.*|meta\b.*|url\b.*|slug\b.*|alt\b.*|-{3,}|_{3,}|={3,})\s*$", re.I)


def fetch_doc(url):
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if os.path.exists(url):
        return docx_text(open(url, "rb").read()) if url.endswith(".docx") else open(url).read()
    if not m:
        raise SystemExit("Copy doc: couldn't read the document id from the link.")
    fid = m.group(1)
    is_gdoc = "docs.google.com/document" in url
    # 1) public / link-shared
    pub = (f"https://docs.google.com/document/d/{fid}/export?format=txt" if is_gdoc
           else f"https://drive.google.com/uc?export=download&id={fid}")
    r = requests.get(pub, timeout=60, allow_redirects=True)
    if r.ok and "accounts.google.com" not in r.url and "<html" not in r.text[:200].lower():
        return r.content.decode("utf-8-sig") if is_gdoc else docx_text(r.content)
    # 2) service account
    sa = os.environ.get("GOOGLE_SA_JSON")
    if sa:
        from google.oauth2 import service_account
        import google.auth.transport.requests as gtr
        cred = service_account.Credentials.from_service_account_info(
            json.loads(sa), scopes=["https://www.googleapis.com/auth/drive.readonly"])
        cred.refresh(gtr.Request())
        h = {"Authorization": f"Bearer {cred.token}"}
        if is_gdoc:
            r = requests.get(f"https://www.googleapis.com/drive/v3/files/{fid}/export",
                             params={"mimeType": "text/plain"}, headers=h, timeout=60)
            r.raise_for_status()
            return r.content.decode("utf-8-sig")
        r = requests.get(f"https://www.googleapis.com/drive/v3/files/{fid}", params={"alt": "media",
                         "supportsAllDrives": "true"}, headers=h, timeout=60)
        r.raise_for_status()
        return docx_text(r.content)
    raise SystemExit("Copy doc is private. Share it as 'Anyone with the link → Viewer', "
                     "or share it with the service account in GOOGLE_SA_JSON.")


def docx_text(b):
    import docx
    d = docx.Document(io.BytesIO(b))
    lines = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            for c in row.cells:
                lines.append(c.text)
    return "\n".join(lines)


def copy_lines(text):
    out = []
    for raw in text.splitlines():
        line = norm(raw)
        if len(line) < 3 or SKIP_RE.match(line):
            continue
        line = LABEL_RE.sub("", line).strip()
        line = re.sub(r"^[-•*▪◦]\s+", "", line)
        if len(line) >= 3 and not line.endswith(":"):
            out.append(line)
    return out


def compare_copy(lines, live):
    issues, rows = [], []
    blocks = [b for b in live["blocks"]] + [t for t in live["texts"]]
    used = set()
    for ln in lines:
        best, score = None, 0
        for b in blocks:
            sc = fuzz.ratio(norm(ln, True), norm(b["text"], True))
            if len(ln) > 40 and sc < 85:
                sc = max(sc, fuzz.partial_ratio(norm(ln, True), norm(b["text"], True)) - 5)
            if sc > score:
                best, score = b, sc
        if best is not None and score >= 70:
            used.add(norm(best["text"], True))
        strict = best is not None and norm(ln) == norm(best["text"])
        loose = best is not None and norm(ln, True) == norm(best["text"], True)
        if strict:
            status = "ok"
        elif loose:
            status = "punctuation"
            issues.append(issue("copy", "low", "Punctuation / capitalization differs from the copy doc", "",
                                ln, best["text"], {"diff": word_diff(ln, best["text"])}))
        elif score >= 75:
            status = "diff"
            issues.append(issue("copy", "medium", "Copy on the site differs from the copy doc", "",
                                ln, best["text"], {"diff": word_diff(ln, best["text"])}))
        else:
            status = "missing"
            issues.append(issue("copy", "high", "Copy from the doc is missing on the live page", "", ln, "—"))
        if status != "ok" and best is not None and not best.get("visible", True) and status != "missing":
            issues[-1]["title"] += " (element is hidden on load)"
        rows.append({"doc": ln, "live": best["text"] if best and score >= 75 else "", "status": status,
                     "score": round(score)})
    # long live paragraphs that are not in the doc
    for b in live["blocks"]:
        t = norm(b["text"], True)
        if b["chrome"] or len(t) < 30 or b["tag"] not in ("p", "h1", "h2", "h3", "h4", "li", "blockquote") or not b["visible"]:
            continue
        if t in used:
            continue
        if max((fuzz.ratio(t, norm(l, True)) for l in lines), default=0) < 75 and \
                max((fuzz.partial_ratio(t, norm(l, True)) for l in lines if len(l) > 40), default=0) < 85:
            issues.append(issue("copy", "info", "Text on the live page that isn't in the copy doc", "", "—", b["text"][:220]))
            used.add(t)
    return issues, rows


# ----------------------------------------------------------------------------- Links & buttons
def audit_links(live_runs, page_url):
    issues, results = [], {}
    page_base = page_url.split("#")[0]
    targets = {}
    seen_nolink = set()
    for tag, live in live_runs:
        ids = set(live["ids"])
        for l in live["links"]:
            label = l["text"] or ("[icon]" if l["img"] else "[no text]")
            href = (l["href"] or "").strip()
            where = f"{tag} · y≈{l['y']}px"
            if l["tag"] == "a":
                if not href or href == "#" or href.lower().startswith("javascript:"):
                    if not l["ariaControls"] and l["visible"]:
                        key = (label, href)
                        if key not in seen_nolink:
                            seen_nolink.add(key)
                            issues.append(issue("links", "high", "Button/link without a destination", where,
                                                "a real URL", href or "(no href)", {"text": label, "cls": l["cls"]}))
                    continue
                if not l["text"] and not l["img"] and l["visible"]:
                    issues.append(issue("links", "low", "Link has no text or accessible label", where, "", href))
                if href.startswith("#") or href.startswith(page_base + "#"):
                    frag = href.split("#", 1)[1]
                    if frag and frag not in ids:
                        issues.append(issue("links", "high", "Anchor link points to a section id that doesn't exist",
                                            where, f"#{frag}", "not found", {"text": label}))
                    continue
                if href.startswith("mailto:"):
                    if not re.match(r"mailto:[^@\s]+@[^@\s]+\.[a-z]{2,}", href, re.I):
                        issues.append(issue("links", "high", "Malformed email link", where, "", href, {"text": label}))
                    continue
                if href.startswith("tel:"):
                    if len(re.sub(r"\D", "", href)) < 7:
                        issues.append(issue("links", "high", "Malformed phone link", where, "", href, {"text": label}))
                    continue
                if " " in href:
                    issues.append(issue("links", "low", "URL contains unencoded spaces (use %20)", where, "",
                                        href, {"text": label}))
                full = urllib.parse.urljoin(page_url, href)
                targets.setdefault(full, set()).add(label)
            elif l["tag"] in ("button", "div", "span") and l["visible"] and not l["inForm"] \
                    and not l["ariaControls"] and l["type"] != "submit":
                key = ("btn", label)
                if key not in seen_nolink:
                    seen_nolink.add(key)
                    issues.append(issue("links", "info", "Button element without a link — confirm it opens something",
                                        where, "", f"<{l['tag']}>", {"text": label}))
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                         "Accept-Language": "en-US,en;q=0.9"})

    def check(u):
        safe = urllib.parse.quote(u, safe=":/?#[]@!$&'()*+,;=%~")
        try:
            r = sess.head(safe, allow_redirects=True, timeout=25)
            if r.status_code in (403, 404, 405, 429, 400, 501) or r.status_code >= 500:
                r = sess.get(safe, allow_redirects=True, timeout=25, stream=True)
                r.close()
            return u, r.status_code, r.url, len(r.history)
        except requests.RequestException as e:
            return u, f"error: {type(e).__name__}", None, 0

    with ThreadPoolExecutor(12) as ex:
        for u, code, final, hops in ex.map(check, targets):
            labels = ", ".join(sorted(targets[u]))[:120]
            results[u] = {"status": code, "final": final, "labels": labels}
            host = urllib.parse.urlparse(u).netloc
            if isinstance(code, str):
                issues.append(issue("links", "high", "Link fails (no response)", labels, "200", code, {"url": u}))
            elif code in (999, 429) or (code == 403 and any(h in host for h in ("linkedin", "instagram", "facebook", "x.com", "twitter"))):
                issues.append(issue("links", "info", "Site blocks automated checks — verify manually", labels,
                                    "200", str(code), {"url": u}))
            elif code >= 400:
                issues.append(issue("links", "high", "Broken link", labels, "200", str(code), {"url": u}))
            elif final and urllib.parse.urlparse(final).path.rstrip("/") != urllib.parse.urlparse(u).path.rstrip("/") \
                    and urllib.parse.urlparse(final).netloc == host:
                issues.append(issue("links", "low", "Link redirects to a different page — update to the final URL",
                                    labels, u, final, {"url": u}))
    return issues, results


# ----------------------------------------------------------------------------- Report
CAT_LABEL = {"design": "Layout vs Figma", "style": "Typography & color", "media": "Images & video",
             "display": "Display", "copy": "Copy vs doc", "links": "Links & buttons"}


def render_report(ctx, path):
    tpl = open(os.path.join(os.path.dirname(__file__), "report_template.html"), encoding="utf-8").read()
    data = json.dumps(ctx, ensure_ascii=False).replace("</", "<\\/")
    open(path, "w", encoding="utf-8").write(tpl.replace("/*__DATA__*/null", data))


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
        lines.append(f"- **{CAT_LABEL[i['category']]}** — {i['title']}"
                     + (f" · `{i.get('text') or i['actual']}`" if (i.get('text') or i['actual']) else "")
                     + (f" ({i['where']})" if i["where"] else ""))
    lines += ["", "Download the **site-qa-report** artifact and open `report.html` for screenshots and diffs."]
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
    issues, notes = [], []
    ctx = {"site": a.site, "figma": a.figma, "figma_mobile": a.figma_mobile, "doc": a.doc,
           "date": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "views": []}

    fig_d = fig_m = None
    if a.figma or a.figma_mobile:
        tok = os.environ.get("FIGMA_TOKEN")
        if not tok:
            raise SystemExit("FIGMA_TOKEN is not set.")
        fg = Figma(tok)
        if a.figma:
            key, nid = fg.parse_url(a.figma)
            fid = fg.resolve_frame(key, nid) if nid else None
            if not fid:
                raise SystemExit("The Figma link has no node-id. Copy the link of the page frame.")
            print(f"Figma desktop frame: {fid}")
            fig_d = fg.extract(key, fid, a.out, "desktop")
            notes.append(f"Desktop design frame: “{fig_d['name']}” ({fig_d['w']:.0f}×{fig_d['h']:.0f})")
        if a.figma_mobile:
            key, nid = fg.parse_url(a.figma_mobile)
            fid = fg.resolve_frame(key, nid, mobile=True)
            fig_m = fg.extract(key, fid, a.out, "mobile")
            notes.append(f"Mobile design frame: “{fig_m['name']}” ({fig_m['w']:.0f}×{fig_m['h']:.0f})")

    dw = int(fig_d["w"]) if fig_d else 1440
    mw = int(fig_m["w"]) if fig_m else a.mobile_width
    print("Capturing live page…")
    live_d = await capture_live(a.site, dw, a.out, "desktop")
    live_m = await capture_live(a.site, mw, a.out, "mobile")

    for tag, fig, live in (("Desktop", fig_d, live_d), ("Mobile", fig_m, live_m)):
        view = {"tag": tag, "width": live["width"], "sections": []}
        if fig:
            iss, secs = compare_design(fig, live, tag, a.out)
            issues += iss
            view["sections"] = secs
        else:
            view["live_full"] = img_b64(Image.open(live["png"]).convert("RGB"), 700)
        issues += check_media_display(live, tag)
        ctx["views"].append(view)

    copy_rows = []
    if a.doc:
        print("Reading copy doc…")
        lines = copy_lines(fetch_doc(a.doc))
        notes.append(f"Copy doc: {len(lines)} copy lines compared")
        iss, copy_rows = compare_copy(lines, live_d)
        issues += iss
    ctx["copy_rows"] = copy_rows

    print("Checking links…")
    iss, link_results = audit_links([("Desktop", live_d), ("Mobile", live_m)], a.site)
    issues += iss
    ctx["links"] = [{"url": u, **v} for u, v in sorted(link_results.items())]

    # dedupe identical issues
    uniq, seen = [], set()
    for i in issues:
        k = (i["category"], i["title"], i["where"], i["expected"], i["actual"], i.get("text"))
        if k not in seen:
            seen.add(k)
            uniq.append(i)
    uniq.sort(key=lambda i: (SEV_ORDER[i["severity"]], list(CAT_LABEL).index(i["category"])))
    ctx["issues"], ctx["notes"], ctx["categories"] = uniq, notes, CAT_LABEL

    render_report(ctx, f"{a.out}/report.html")
    json.dump({k: v for k, v in ctx.items() if k != "views"}, open(f"{a.out}/report.json", "w"), indent=1, ensure_ascii=False)
    md = summary_md(ctx)
    open(f"{a.out}/summary.md", "w").write(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(md + "\n")
    print(md)
    fail_on = os.environ.get("FAIL_ON_HIGH", "false").lower() == "true"
    if fail_on and any(i["severity"] == "high" for i in uniq):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

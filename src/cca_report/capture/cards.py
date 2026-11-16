"""Product-card detection in the live DOM.

Either the retailer YAML gives ``selectors.product_card`` or a heuristic finds the most
repeated element that contains an image and a price. Every card found is tagged with
``data-cca-card="<i>"`` so Playwright can screenshot it as an element, and its visible
text is returned line by line with style hints (struck-through, bold, font size) that the
DOM extractor uses to tell the old price from the final one.
"""

from __future__ import annotations

from typing import Any

from playwright.async_api import Page

DETECT_JS = r"""
(cfg) => {
  const priceRe = /(US\$|USD|B\/\.|\$|₡|Gs\.?|PYG|₲|UYU|\$U)\s?\d|\d[\d.,]*\s?(USD|Gs)/i;
  const visible = el => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  let cards = [];
  let how = 'selector';
  if (cfg.selector) {
    cards = Array.from(document.querySelectorAll(cfg.selector)).filter(visible);
  }
  if (!cards.length) {
    how = 'heuristic';
    const cand = [];
    for (const el of document.querySelectorAll('body *')) {
      if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'svg', 'PATH'].includes(el.tagName)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 120 || r.height < 150 || r.width > 760 || r.height > 1200) continue;
      if (!el.querySelector('img, picture, [style*="background-image"]')) continue;
      const t = (el.innerText || '').trim();
      if (!t || t.length > 1800 || !priceRe.test(t)) continue;
      cand.push(el);
    }
    // innermost elements that still hold image + price = the card
    const minimal = cand.filter(el => !cand.some(o => o !== el && el.contains(o)));
    const sig = el => el.tagName + '|' + Array.from(el.classList)
      .filter(c => !/\d{3,}|active|hover|selected|swiper-slide-(next|prev)/i.test(c)).sort().join('.');
    const groups = new Map();
    for (const el of minimal) {
      const k = sig(el);
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(el);
    }
    let best = [];
    for (const g of groups.values()) if (g.length > best.length) best = g;
    cards = best.length >= 2 ? best : minimal;
  }
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const STRUCK_CLASS = new RegExp(
    'tachad|strike|old[-_]?price|price[-_]?old|was[-_]?price|precio[-_]?(anterior|antes|lista)|compare[-_]?at',
    'i');
  const isStruck = (e, card) => {
    for (let a = e; a && a !== card.parentElement; a = a.parentElement) {
      const s = getComputedStyle(a);
      if (s.textDecorationLine.includes('line-through') || ['DEL', 'S', 'STRIKE'].includes(a.tagName)) return true;
      const cls = typeof a.className === 'string' ? a.className : '';
      if (STRUCK_CLASS.test(cls)) return true;
    }
    return false;
  };
  // Lines = what the user reads (innerText), so "$" "317" "." "33" split across nodes stays
  // one line. Style hints come from the deepest element whose text equals the line.
  const lineInfo = el => {
    const styleOf = new Map();
    const struck = [];
    for (const e of [el, ...el.querySelectorAll('*')]) {
      if (!visible(e)) continue;
      const t = norm(e.innerText);
      if (!t || t.length > 160) continue;
      const st = getComputedStyle(e);
      const strike = isStruck(e, el);
      if (strike && /\d/.test(t)) struck.push(t);
      styleOf.set(t, {strike, bold: parseInt(st.fontWeight) >= 600, size: parseFloat(st.fontSize)});
    }
    const lines = (el.innerText || '').split('\n').map(norm).filter(Boolean).map(text => {
      const s = styleOf.get(text) || {strike: false, bold: false, size: 0};
      return {text, strike: s.strike, bold: s.bold, size: s.size};
    });
    return {lines, struck: [...new Set(struck)]};
  };
  return {
    how,
    cards: cards.slice(0, cfg.limit || 80).map((el, i) => {
      el.setAttribute('data-cca-card', String(i));
      const r = el.getBoundingClientRect();
      const a = el.matches('a[href]') ? el : el.querySelector('a[href]');
      const img = el.querySelector('img');
      const nameEl = cfg.name ? el.querySelector(cfg.name) : null;
      return {
        name: nameEl ? nameEl.innerText.replace(/\s+/g, ' ').trim() : '',
        index: i,
        text: (el.innerText || '').trim(),
        ...(() => { const li = lineInfo(el); return {lines: li.lines, struck: li.struck}; })(),
        href: a ? a.href : '',
        img: img ? (img.currentSrc || img.src || '') : '',
        box: {x: r.x + scrollX, y: r.y + scrollY, width: r.width, height: r.height},
        tag: el.tagName.toLowerCase(),
        classes: el.className && el.className.baseVal === undefined ? String(el.className) : '',
      };
    }),
  };
}
"""


WAIT_PRICES_JS = r"""
() => {
  const re = /(US\$|USD|B\/\.|\$|₡|Gs\.?|PYG|₲|UYU)\s?\d/g;
  return ((document.body && document.body.innerText) || '').match(re)?.length || 0;
}
"""


async def wait_for_prices(page: Page, timeout_ms: int = 25_000) -> int:
    """Lazy catalogues (skeleton loaders, XHR grids): wait until the number of visible prices
    is > 0 and stable for two polls. Read-only: no clicks, no scrolling tricks."""
    last, stable, waited = -1, 0, 0
    while waited < timeout_ms:
        n = await page.evaluate(WAIT_PRICES_JS)
        stable = stable + 1 if n == last and n > 0 else 0
        if stable >= 2:
            return n
        last = n
        await page.wait_for_timeout(1000)
        waited += 1000
    return max(last, 0)


async def detect_cards(page: Page, selector: str = "", name: str = "", limit: int = 80) -> dict[str, Any]:
    return await page.evaluate(DETECT_JS, {"selector": selector, "name": name, "limit": limit})

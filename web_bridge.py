#!/usr/bin/env python3
"""
Web Bridge for Old Browsers — port 8888

Fetches any website and strips modern features (JavaScript, CSS, video,
HTML5 layout) returning classic HTML compatible with browsers as old as
IE 2 (1995), through IE 3/4/5 and Netscape 3/4, running on Windows 3.1,
95, or 98.

Layout is analyzed from embedded CSS (grid/flex) and reproduced with
<table> tags.  Images are proxied, converted to JPEG, and pre-resized
at the proxy level for IE2 compatibility.  SVGs are rasterized via
cairosvg.  YouTube pages are extracted from embedded JSON.

Usage:
    python3 web_bridge.py
    Then open  http://<server-ip>:8888  in your old browser.
    (the script prints the detected LAN IP on startup)

Requires:
    pip install requests beautifulsoup4 Pillow
"""

import io
import re
import sys
import base64
import threading
import http.server
import socketserver
import urllib.parse
from urllib.parse import urljoin, urlparse, quote, unquote
from collections import OrderedDict

try:
    import requests
    from requests.exceptions import RequestException
except ImportError:
    sys.exit("Missing dependency — run:  pip install requests")

try:
    from bs4 import BeautifulSoup, Comment, Tag, NavigableString
except ImportError:
    sys.exit("Missing dependency — run:  pip install beautifulsoup4")

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("Warning: Pillow not installed — images will not be converted.")

try:
    from readability import Document as _ReadabilityDocument
    HAS_READABILITY = True
except ImportError:
    HAS_READABILITY = False
    print("Warning: readability-lxml not installed — Reader mode disabled. "
          "Run: pip install readability-lxml")

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    HAS_SELENIUM = True
except ImportError:
    HAS_SELENIUM = False
    print("Warning: Selenium not installed — screenshots disabled.")

try:
    from webdriver_manager.chrome import ChromeDriverManager
    from webdriver_manager.core.os_manager import ChromeType
    HAS_WDM = True
except ImportError:
    HAS_WDM = False

# ── Configuration ──────────────────────────────────────────────────────────
PORT               = 8888
FETCH_TIMEOUT      = 20
MAX_IMG_W          = 640
MAX_IMG_H          = 480
MAX_HISTORY        = 30
SCREENSHOT_W       = 800
SCREENSHOT_H       = 600
SCREENSHOT_QUALITY = 70     # JPEG quality (1-100)

def _detect_lan_ip():
    """Detect the server's LAN IP for use when browsers omit Host header."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"

SERVER_IP = _detect_lan_ip()


# ── URL history (recent URLs visited through the proxy) ───────────────────

class _UrlHistory:
    """Thread-safe MRU list of recently visited URLs."""
    def __init__(self, maxlen=MAX_HISTORY):
        self._lock = threading.Lock()
        self._urls = OrderedDict()   # url → True, most-recent last
        self._maxlen = maxlen

    def add(self, url):
        with self._lock:
            if url in self._urls:
                self._urls.move_to_end(url)
            else:
                self._urls[url] = True
            while len(self._urls) > self._maxlen:
                self._urls.popitem(last=False)

    def recent(self, n=10):
        """Return up to n most-recent URLs (newest first)."""
        with self._lock:
            return list(reversed(self._urls))[:n]

_user_histories = {}              # client IP → _UrlHistory
_user_histories_lock = threading.Lock()
_MAX_TRACKED_IPS = 500

def _get_history(ip):
    """Return the _UrlHistory for a given client IP, creating if needed."""
    with _user_histories_lock:
        if ip not in _user_histories:
            # Evict oldest entry if we've hit the cap
            if len(_user_histories) >= _MAX_TRACKED_IPS:
                oldest = next(iter(_user_histories))
                del _user_histories[oldest]
            _user_histories[ip] = _UrlHistory()
        return _user_histories[ip]


BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; rv:128.0) "
    "Gecko/20100101 Firefox/128.0"
)
# Wikimedia requires a descriptive UA with contact info
WIKIMEDIA_UA = (
    "OldBrowserBridge/1.0 (Web bridge for classic browsers; "
    "compatible; +https://github.com/user/old-browser-bridge) "
    "Python-requests"
)
GOOGLEBOT_UA = "Googlebot/2.1 (+http://www.google.com/bot.html)"
FETCH_HEADERS = {
    "User-Agent":      BROWSER_UA,
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
}

def _fetch_headers_for(url):
    """Return appropriate headers — Wikimedia needs a bot-style UA."""
    if "wikipedia.org" in url or "wikimedia.org" in url or "wiktionary.org" in url:
        h = dict(FETCH_HEADERS)
        h["User-Agent"] = WIKIMEDIA_UA
        return h
    return FETCH_HEADERS

# Shared session — keeps cookies across requests (needed for Google, etc.)
_session = requests.Session()
_session.headers.update(FETCH_HEADERS)

# Cache for rasterized inline SVGs (hash → PNG bytes).
# Old browsers (IE<8) don't support data: URIs, so we serve these via /svg/.
import hashlib as _hashlib
_svg_cache = {}          # {hex_hash: png_bytes}
_SVG_CACHE_MAX = 500     # evict oldest when full

# ── Tag rules ──────────────────────────────────────────────────────────────

DROP_TAGS = frozenset({
    "script", "style", "base",
    "iframe", "video", "audio", "canvas", "object", "embed",
    "template", "slot", "portal",
    "transition", "transition-group",
})

# Tags removed but whose children are KEPT (unwrapped).
# html.parser treats <meta>/<noscript>/<link> as void elements and may
# nest all subsequent content inside them — decomposing would destroy
# the real article body.  Unwrapping safely removes just the tag.
UNWRAP_TAGS = frozenset({"meta", "noscript", "link"})

REMAP_TAGS = {
    "section":    "p",
    "article":    "p",
    "main":       "p",
    "header":     "p",
    "footer":     "p",
    "nav":        "p",
    "aside":      "p",
    "figure":     "p",
    "figcaption": "p",
    "hgroup":     "p",
    "details":    "p",
    "summary":    "b",
    "mark":       "b",
    "time":       "span",
    "output":     "span",
    "dialog":     "p",
    "menu":       "ul",
    "address":    "p",
    "cite":       "i",
    "abbr":       "span",
    "acronym":    "span",
    "dfn":        "i",
    "kbd":        "code",
    "samp":       "code",
    "var":        "i",
    "bdi":        "span",
    "bdo":        "span",
    "wbr":        None,
    "ruby":       "span",
    "rt":         None,
    "rp":         None,
    "data":       "span",
}

_STRIP_RE = re.compile(
    r"^(class|id|style|role|tabindex|aria-[a-z_-]+|data-[a-z_-]+"
    r"|on[a-z]+|contenteditable|draggable|hidden|spellcheck|translate"
    r"|loading|srcset|fetchpriority|decoding|crossorigin"
    r"|integrity|referrerpolicy|is|slot|part|ping|itemprop"
    r"|itemscope|itemtype|property|typeof|vocab|rel|rev)$",
    re.IGNORECASE,
)

_LAZY_SRC = ("data-src", "data-original", "data-lazy",
             "data-url", "data-lazy-src", "data-echo", "data-hi-res")

_MAIN_ID_RE  = re.compile(r"\b(main|content|article|post|entry|body|text|story|product|dp-container|listing)\b", re.I)
_MAIN_CLS_RE = re.compile(r"\b(main|content|article|post|entry|body|text|story|product|listing)\b", re.I)
# Patterns that strongly indicate article content (scored higher than generic matches)
_ARTICLE_ID_RE  = re.compile(r"\b(article|post|entry|story)\b", re.I)
_ARTICLE_CLS_RE = re.compile(r"\b(article|post|entry|story)\b", re.I)


# ── CSS layout parser ─────────────────────────────────────────────────────

def _parse_css_layouts(soup):
    """
    Parse all <style> tags in the document and return a dict mapping
    CSS selectors (class names and element IDs) to layout info:
       key -> {"display": "grid"|"flex"|...,
               "direction": "row"|"column",
               "columns": int,
               "col_widths": [str, ...] or None,
               "float": "left"|"right"|None,
               "width_pct": float or None,
               "flex_pct": float or None,
               "overflow_x": str or None}
    Keys are stored as:
       ".classname"  for class selectors
       "#idname"     for id selectors
    """
    layouts = {}

    for style_tag in soup.find_all("style"):
        css_text = style_tag.get_text()
        # Match both .classname and #idname selectors
        for match in re.finditer(
            r'([.#])([a-zA-Z0-9_-]+)\s*\{([^}]*)\}', css_text
        ):
            prefix   = match.group(1)   # "." or "#"
            name     = match.group(2)
            body     = match.group(3)
            key      = prefix + name
            info     = layouts.get(key, {})

            # display
            dm = re.search(r'display:\s*(grid|flex|-[a-z-]*(grid|flex))', body)
            if dm:
                val = dm.group(0)
                if "grid" in val:
                    info["display"] = "grid"
                elif "flex" in val:
                    info["display"] = "flex"

            # flex-direction
            fdm = re.search(r'flex-direction:\s*(row|column)', body)
            if fdm:
                info["direction"] = fdm.group(1)

            # flex shorthand: flex: G S B% → extract basis percentage
            fxm = re.search(r'flex:\s*\d+\s+\d+\s+(\d+(?:\.\d+)?)%', body)
            if fxm:
                info["flex_pct"] = float(fxm.group(1))

            # grid-template-columns — keep the SMALLEST column count
            gtc = re.search(r'grid-template-columns:\s*([^;]+)', body)
            if gtc:
                val = gtc.group(1).strip()
                rep = re.search(r'repeat\(\s*(\d+)', val)
                if rep:
                    new_cols = int(rep.group(1))
                else:
                    parts = val.split()
                    new_cols = len(parts)
                    info["col_widths"] = parts
                prev = info.get("columns")
                if prev is None or new_cols < prev:
                    info["columns"] = new_cols

            # float
            fm = re.search(r'float:\s*(left|right)', body)
            if fm:
                info["float"] = fm.group(1)

            # width percentage
            wm = re.search(r'width:\s*(?:calc\()?(?:100%\s*/\s*(\d+(?:\.\d+)?))', body)
            if wm:
                info["width_pct"] = round(100.0 / float(wm.group(1)))

            # overflow-x (carousel indicator)
            om = re.search(r'overflow-x:\s*(auto|scroll|hidden)', body)
            if om:
                info["overflow_x"] = om.group(1)

            # display:none
            if re.search(r'display:\s*none', body):
                info["hidden"] = True

            if info:
                layouts[key] = info

    return layouts


def _parse_css_img_sizes(soup):
    """Parse CSS rules that set width/height/max-width/max-height on img
    elements.  Returns a list of (ancestor_classes, width_px, height_px)
    tuples.  max-width/max-height are used as fallback when no explicit
    width/height is specified.
    ancestor_classes is a list of class names from the selector that must
    appear in the img's ancestor chain for the rule to match.
    """
    rules = []
    for style_tag in soup.find_all("style"):
        css_text = style_tag.get_text()
        # Match rules ending in 'img' with size properties
        for m in re.finditer(
            r'((?:[.#][a-zA-Z0-9_-]+[\s>]*)+)\s*img\s*\{([^}]*)\}',
            css_text
        ):
            selector_parts = m.group(1)
            body = m.group(2)
            # Prefer explicit width, fall back to max-width
            wm = re.search(r'(?:^|[;\s])width\s*:\s*(\d+)\s*px', body)
            if not wm:
                wm = re.search(r'(?:^|[;\s])max-width\s*:\s*(\d+)\s*px',
                               body)
            # Prefer explicit height, fall back to max-height
            hm = re.search(r'(?:^|[;\s])height\s*:\s*(\d+)\s*px', body)
            if not hm:
                hm = re.search(r'(?:^|[;\s])max-height\s*:\s*(\d+)\s*px',
                               body)
            if not wm and not hm:
                continue
            w = int(wm.group(1)) if wm else 0
            h = int(hm.group(1)) if hm else 0
            # Extract class names from selector (ignore IDs and tag names)
            classes = re.findall(r'\.([a-zA-Z0-9_-]+)', selector_parts)
            if classes:
                rules.append((classes, w, h))
    return rules


def _css_img_size(img_tag, css_img_rules):
    """Look up CSS-defined width/height for an <img> by matching its
    ancestor class chain against parsed CSS rules.
    Returns (width_str, height_str) or ("", "")."""
    if not css_img_rules:
        return "", ""
    # Build set of ancestor class names (fast lookup)
    ancestor_classes = set()
    for anc in img_tag.parents:
        if anc is None or not hasattr(anc, 'get'):
            break
        for c in anc.get("class", []):
            ancestor_classes.add(c)
    # Also include the img's own classes
    for c in img_tag.get("class", []):
        ancestor_classes.add(c)

    # Find the most specific matching rule (most ancestor classes)
    best_w, best_h, best_specificity = 0, 0, -1
    for classes, w, h in css_img_rules:
        if all(c in ancestor_classes for c in classes):
            if len(classes) > best_specificity:
                best_specificity = len(classes)
                best_w, best_h = w, h
    if best_specificity >= 0:
        return (str(best_w) if best_w else "",
                str(best_h) if best_h else "")
    return "", ""


def _get_layout(tag, css_layouts):
    """Look up the layout info for a tag by matching CSS classes or id."""
    # Check id first (more specific)
    tag_id = tag.get("id", "")
    if tag_id:
        layout = css_layouts.get("#" + tag_id)
        if layout:
            return layout
    # Then check classes
    for cls in tag.get("class", []):
        layout = css_layouts.get("." + cls)
        if layout:
            return layout
    return None


# ── Layout-aware table converter ───────────────────────────────────────────

def _convert_layout_to_tables(soup, css_layouts):
    """
    Walk the DOM bottom-up.  For every element whose CSS class indicates
    a grid or flex-row layout, replace its children arrangement with a
    <table> that approximates the original multi-column layout.
    """
    # Process deepest nodes first so inner grids are converted before outer
    all_tags = list(soup.find_all(True))
    all_tags.reverse()

    for tag in all_tags:
        if not isinstance(tag, Tag) or not tag.parent:
            continue

        layout = _get_layout(tag, css_layouts)
        if not layout:
            continue

        display   = layout.get("display")
        direction = layout.get("direction", "row")
        columns   = layout.get("columns")

        tag_children = [c for c in tag.children if isinstance(c, Tag)]
        if not tag_children:
            continue

        # ── CSS Grid with explicit column count ──
        if display == "grid" and columns and columns >= 2 and len(tag_children) >= 2:
            _wrap_in_grid_table(tag, tag_children, columns, soup)
            continue

        # ── Flexbox row with multiple children ──
        if display == "flex" and direction == "row" and len(tag_children) >= 2:
            # Detect horizontal carousels: overflow-x or many items (>4)
            is_carousel = (layout.get("overflow_x") in ("auto", "scroll")
                           or len(tag_children) > 4)
            if is_carousel:
                # Reflow carousel items into a grid (3 columns)
                _wrap_in_grid_table(tag, tag_children, 3, soup)
                continue

            meaningful = [c for c in tag_children
                          if len(c.get_text(strip=True)) > 20 or c.find("img")]
            if len(meaningful) >= 2:
                _wrap_in_flex_row_table(tag, tag_children, css_layouts, soup)
                continue


MAX_TABLE_COLS = 3   # hard cap — old browsers at 800×600 can't do more

def _wrap_in_grid_table(parent, children, columns, soup):
    """Convert children of a CSS grid container into a <table> with N columns."""
    columns = min(columns, MAX_TABLE_COLS)
    tbl = soup.new_tag("table", width="100%", border="0",
                       cellpadding="4", cellspacing="2")
    col_w = "{}%".format(100 // columns)

    row = None
    for i, child in enumerate(children):
        if i % columns == 0:
            row = soup.new_tag("tr")
            tbl.append(row)
        td = soup.new_tag("td", width=col_w, valign="top")
        # Move child into td
        child.extract()
        td.append(child)
        row.append(td)

    # Pad the last row if incomplete
    if row and len(list(row.children)) < columns:
        missing = columns - len(list(row.children))
        for _ in range(missing):
            row.append(soup.new_tag("td"))

    # Replace parent's children with the table
    parent.clear()
    parent.append(tbl)


def _wrap_in_flex_row_table(parent, children, css_layouts, soup):
    """Convert children of a flex-row container into a single-row <table>."""
    tbl = soup.new_tag("table", width="100%", border="0",
                       cellpadding="4", cellspacing="2")
    tr = soup.new_tag("tr")
    tbl.append(tr)

    for child in children:
        child_layout = _get_layout(child, css_layouts) if isinstance(child, Tag) else None
        w_pct = None
        if child_layout:
            w_pct = child_layout.get("width_pct") or child_layout.get("flex_pct")

        td = soup.new_tag("td", valign="top")
        if w_pct:
            td["width"] = "{}%".format(int(w_pct))
        child.extract()
        td.append(child)
        tr.append(td)

    parent.clear()
    parent.append(tbl)


# ── Structural layout: isolate page zones in independent tables ─────────────

def _structural_table_layout(soup):
    """
    Detect common page zones (header, nav, main content, sidebar, footer).
    Each zone becomes its own independent <table width="100%"> so that a
    misbehaving section (e.g. a carousel that is too wide) cannot stretch
    the entire page.

    Inside the main content area, each direct <section> child also gets
    wrapped in its own table for the same reason.
    """
    body = soup.find("body")
    if not body:
        return

    # Search the full tree — many modern sites deeply nest these elements
    header_el  = body.find("header") or body.find(
        lambda t: t.name == "div" and _has_class_hint(t, ("header", "banner", "masthead")))
    nav_el     = body.find("nav")
    main_el    = body.find("main") or body.find(
        lambda t: t.name == "div" and _has_class_hint(t, ("main", "content", "article")))
    aside_el   = body.find("aside") or body.find(
        lambda t: t.name == "div" and _has_class_hint(t, ("sidebar", "aside", "rail", "right-col", "secondary")))
    footer_el  = body.find("footer") or body.find(
        lambda t: t.name == "div" and _has_class_hint(t, ("footer",)))

    if not main_el:
        # Even without a recognized main, isolate top-level sections
        _isolate_sections(body, soup)
        return

    # ── Isolate sections inside <main> ──
    _isolate_sections(main_el, soup)

    # ── Build independent tables for each zone ──
    # Header
    if header_el:
        _wrap_zone(header_el, soup, bgcolor="#eeeeee")

    # Nav
    if nav_el:
        _wrap_zone(nav_el, soup, bgcolor="#dddddd")

    # Main + optional sidebar
    if aside_el:
        # Build a two-column table for main + sidebar
        tbl = soup.new_tag("table", width="100%", border="0",
                           cellpadding="0", cellspacing="0")
        tr = soup.new_tag("tr")
        td_main = soup.new_tag("td", width="75%", valign="top")
        td_side = soup.new_tag("td", width="25%", valign="top", bgcolor="#f5f5f5")
        main_el.replace_with(tbl)
        td_main.append(main_el)
        aside_el.extract()
        td_side.append(aside_el)
        tr.append(td_main)
        tr.append(td_side)
        tbl.append(tr)

    # Footer
    if footer_el:
        _wrap_zone(footer_el, soup, bgcolor="#eeeeee")


def _isolate_sections(container, soup):
    """
    Find the deepest container that holds multiple <section> (or similar)
    children and wrap each one in its own <table width="100%"> so that
    one overflowing section cannot stretch its siblings.
    """
    if not isinstance(container, Tag):
        return

    # Find the actual section container — drill through single-child
    # wrapper divs until we reach one with multiple block children
    target = container
    for _ in range(6):  # max depth
        block_kids = [c for c in target.children
                      if isinstance(c, Tag) and c.name in
                      ("section", "article", "div", "ul", "ol", "nav",
                       "aside", "header", "footer")]
        if len(block_kids) >= 2:
            break
        if len(block_kids) == 1:
            target = block_kids[0]
        else:
            return  # nothing meaningful

    children = list(target.children)
    for child in children:
        if not isinstance(child, Tag):
            continue
        if child.name in ("section", "article", "div", "ul", "ol", "nav",
                          "aside", "header", "footer"):
            if len(child.get_text(strip=True)) < 10 and not child.find("img"):
                continue
            wrapper = soup.new_tag("table", width="100%", border="0",
                                   cellpadding="0", cellspacing="0")
            tr = soup.new_tag("tr")
            td = soup.new_tag("td", valign="top")
            child.replace_with(wrapper)
            td.append(child)
            tr.append(td)
            wrapper.append(tr)


def _wrap_zone(element, soup, bgcolor=None):
    """Wrap a page zone (header/nav/footer) in its own independent table."""
    tbl = soup.new_tag("table", width="100%", border="0",
                       cellpadding="4", cellspacing="0")
    if bgcolor:
        tbl["bgcolor"] = bgcolor
    tr = soup.new_tag("tr")
    td = soup.new_tag("td", valign="top")
    element.replace_with(tbl)
    td.append(element)
    tr.append(td)
    tbl.append(tr)


def _has_class_hint(tag, keywords):
    classes = " ".join(tag.get("class", []))
    tag_id  = tag.get("id", "")
    combined = (classes + " " + tag_id).lower()
    return any(kw in combined for kw in keywords)


# ── Dropdown → <select> conversion ────────────────────────────────────────

_DROPDOWN_CLS_RE = re.compile(
    r"\b(dropdown|drop-down|collapsible|popup-menu|toggle-menu)\b", re.I
)

def _convert_dropdowns_to_select(soup, page_url, proxy_host, cp1256=False):
    """
    Detect dropdown menus (hidden lists of links activated by JS/CSS) and
    convert them to HTML 3.2 <select>+<form> combos that old browsers can use.

    Detection patterns:
      1. Wikipedia: div.vector-dropdown with label + list of links
      2. General: any element with "dropdown" in class containing a <ul> of links
      3. <ul> with role="menu" containing links
    """
    converted = set()

    # ── Pattern 1: Wikipedia vector-dropdown ──
    for dd in soup.find_all("div", class_=lambda c: c and "vector-dropdown" in " ".join(c)):
        if id(dd) in converted:
            continue
        label, items = _extract_dropdown_parts(dd, page_url, proxy_host)
        if len(items) >= 2:
            _replace_with_select(dd, label, items, soup, proxy_host, cp1256)
            converted.add(id(dd))

    # ── Pattern 2: general elements with "dropdown" class ──
    for el in soup.find_all(True):
        if id(el) in converted:
            continue
        cls_str = " ".join(el.get("class", []))
        if not _DROPDOWN_CLS_RE.search(cls_str):
            continue
        label, items = _extract_dropdown_parts(el, page_url, proxy_host)
        if len(items) >= 2:
            _replace_with_select(el, label, items, soup, proxy_host, cp1256)
            converted.add(id(el))

    # ── Pattern 3: <ul role="menu"> or <ul> with dropdown-menu class ──
    for ul in soup.find_all("ul"):
        if id(ul) in converted:
            continue
        role = ul.get("role", "")
        cls_str = " ".join(ul.get("class", []))
        if role == "menu" or "dropdown-menu" in cls_str:
            items = _extract_link_items(ul, page_url, proxy_host)
            if len(items) >= 2:
                label = _find_label_near(ul)
                _replace_with_select(ul, label, items, soup, proxy_host, cp1256)
                converted.add(id(ul))


def _extract_dropdown_parts(container, page_url, proxy_host):
    """
    Extract (label_text, [(display_text, proxied_url), ...]) from a
    dropdown container.
    """
    label = _find_dropdown_label(container)
    items = []

    # Find all <li> or direct <a> children inside the content area
    content_div = container.find("div", class_=lambda c: c and any(
        x in " ".join(c) for x in ("dropdown-content", "menu-content",
                                     "dropdown-list", "menu-list")))
    search_in = content_div if content_div else container

    items = _extract_link_items(search_in, page_url, proxy_host)
    return label, items


def _extract_link_items(container, page_url, proxy_host):
    """Extract (display_text, absolute_url) pairs from a container's links."""
    items = []
    seen = set()
    for a in container.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True)
        if not text or len(text) > 100:
            continue
        abs_url = _abs(href, page_url)
        if not abs_url or abs_url in seen:
            continue
        seen.add(abs_url)
        items.append((text, abs_url))
    return items


def _find_dropdown_label(container):
    """Try to find the label text for a dropdown container."""
    # Wikipedia: <span class="vector-dropdown-label-text">
    lbl = container.find("span", class_=lambda c: c and "label-text" in " ".join(c))
    if lbl:
        return lbl.get_text(strip=True)
    # Check for label, button, summary, or heading children
    for tag_name in ("label", "button", "summary", "b", "h3", "h4", "span"):
        cand = container.find(tag_name, recursive=False)
        if not cand:
            # try one level deeper
            cand = container.find(tag_name)
        if cand:
            text = cand.get_text(strip=True)
            if text and len(text) < 60:
                return text
    return "Menu"


def _find_label_near(element):
    """Find a label from the previous sibling of an element."""
    prev = element.find_previous_sibling(["button", "a", "label", "span",
                                           "b", "summary", "h3", "h4"])
    if prev:
        text = prev.get_text(strip=True)
        if text and len(text) < 60:
            return text
    return "Menu"


def _replace_with_select(element, label, items, soup, proxy_host, cp1256=False):
    """
    Replace an element with a compact <select> + Go button.
    Uses a <form method="GET" action="/get"> so old browsers can navigate
    without JavaScript.
    """
    form = soup.new_tag("form", method="GET", action="/get")
    form["style"] = ""  # will be stripped anyway

    if cp1256:
        h = soup.new_tag("input", type="hidden")
        h["name"] = "cp1256"
        h["value"] = "1"
        form.append(h)

    font = soup.new_tag("font", size="1")
    font.string = label + ": "
    form.append(font)

    select = soup.new_tag("select", attrs={"name": "url"})
    # Default option
    opt0 = soup.new_tag("option", value="")
    opt0.string = "-- {} ({}) --".format(label, len(items))
    select.append(opt0)

    for text, url in items:
        opt = soup.new_tag("option", value=url)
        opt.string = text
        select.append(opt)

    form.append(select)
    form.append(NavigableString(" "))
    btn = soup.new_tag("input", type="submit", value="Go")
    form.append(btn)

    element.replace_with(form)


# ── Non-renderable Unicode replacement ─────────────────────────────────────
#
# IE2 / Windows 95 cannot render CJK, Devanagari, Thai, and many other
# Unicode scripts.  These characters display as garbled text in an
# unbreakable line, causing the page to scroll horizontally.
#
# Whitelist approach: allow ONLY characters that Windows 95 can render
# (Basic Latin, Latin-1 Supplement, Latin Extended, Arabic, Hebrew, and
# common symbols/punctuation).  Everything else gets stripped and, if a
# long enough run, replaced with a space to allow line-wrapping.

def _is_renderable(ch):
    """Return True if *ch* can be displayed on Windows 95 / IE2."""
    c = ord(ch)
    if c < 0x0250:       # Basic Latin, Latin-1 Supp, Latin Ext-A/B
        return True
    if 0x0590 <= c <= 0x05FF:   # Hebrew
        return True
    if 0x0600 <= c <= 0x06FF:   # Arabic
        return True
    if 0x0750 <= c <= 0x077F:   # Arabic Supplement
        return True
    if 0x08A0 <= c <= 0x08FF:   # Arabic Extended-A
        return True
    if 0xFB50 <= c <= 0xFDFF:   # Arabic Presentation Forms-A
        return True
    if 0xFE70 <= c <= 0xFEFF:   # Arabic Presentation Forms-B
        return True
    # Common symbols, punctuation, math, currency (keep for readability)
    if 0x2000 <= c <= 0x206F:   # General Punctuation
        return True
    if 0x20A0 <= c <= 0x20CF:   # Currency Symbols
        return True
    if 0x2100 <= c <= 0x214F:   # Letterlike Symbols
        return True
    if 0x2190 <= c <= 0x21FF:   # Arrows
        return True
    if 0x2200 <= c <= 0x22FF:   # Mathematical Operators
        return True
    if 0x25A0 <= c <= 0x25FF:   # Geometric Shapes
        return True
    if 0x2600 <= c <= 0x26FF:   # Miscellaneous Symbols
        return True
    # Greek & Cyrillic (renderable with some Windows codepages)
    if 0x0370 <= c <= 0x03FF:   # Greek
        return True
    if 0x0400 <= c <= 0x04FF:   # Cyrillic
        return True
    return False


def _replace_unrenderable_text(soup):
    """
    Walk all text nodes and replace non-renderable characters.
    Short runs (1-2 chars) are silently dropped.
    Longer runs are replaced with a single space to allow line-wrapping.
    """
    for text_node in list(soup.find_all(string=True)):
        original = str(text_node)
        # Quick check: if all chars are low ASCII, skip
        if all(ord(c) < 0x0250 for c in original):
            continue
        result = []
        unrenderable_run = 0
        for ch in original:
            if _is_renderable(ch):
                if unrenderable_run > 0:
                    # Replace the run of unrenderable chars with a space
                    result.append(" ")
                    unrenderable_run = 0
                result.append(ch)
            else:
                unrenderable_run += 1
        # Trailing unrenderable run
        if unrenderable_run > 0:
            result.append(" ")
        replaced = "".join(result)
        if replaced != original:
            text_node.replace_with(replaced)


# ── RTL detection ──────────────────────────────────────────────────────────

_ARABIC_RE = re.compile(
    r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]"
)

def _detect_rtl(soup):
    """Return True if the page is RTL (Arabic, Hebrew, Farsi, Urdu, etc.)."""
    for tag_name in ("html", "body"):
        tag = soup.find(tag_name)
        if not tag:
            continue
        lang = tag.get("lang", "")
        if re.match(r"^(ar|he|fa|ur|yi|ps|sd|ug)\b", lang, re.IGNORECASE):
            return True
        if tag.get("dir", "").lower() == "rtl":
            return True
    # Fallback: if >30% of alphabetic characters are RTL script
    text = soup.get_text()
    rtl_count = len(_ARABIC_RE.findall(text))
    if rtl_count < 20:
        return False
    alpha_count = sum(1 for c in text if c.isalpha())
    return alpha_count > 0 and (rtl_count / alpha_count) > 0.3


# ── Forum detection & rendering ───────────────────────────────────────────

def _render_xenforo(soup, page_url, proxy_host, cp1256=False):
    """
    Detect XenForo forum pages and render them as clean HTML 3.2 tables.
    Returns HTML string if XenForo detected, else None.
    """
    def _fc(tag, cls_name, name=None):
        """Find element by class name (matches if cls_name is one of the
        element's classes).  BS4 class_ with a plain string does this."""
        if name:
            return tag.find(name, class_=cls_name)
        return tag.find(class_=cls_name)

    def _fca(tag, cls_name, name=None, recursive=True):
        """find_all variant."""
        if name:
            return tag.find_all(name, class_=cls_name, recursive=recursive)
        return tag.find_all(class_=cls_name, recursive=recursive)

    # Detect XenForo by its characteristic wrapper
    if not _fc(soup, "p-pageWrapper", "div"):
        return None
    pc = _fc(soup, "p-body-pageContent", "div")
    if not pc:
        return None

    parts = []

    # --- Forum index page: category blocks with sub-forums ---
    cat_blocks = _fca(pc, "block--category", "div")
    if cat_blocks:
        for cat in cat_blocks:
            header = _fc(cat, "block-header")
            cat_title = ""
            cat_desc = ""
            if header:
                ha = header.find("a")
                cat_title = ha.get_text(strip=True) if ha else \
                    header.get_text(strip=True)
                hd = _fc(header, "block-desc")
                if hd:
                    cat_desc = hd.get_text(strip=True)
            body = _fc(cat, "block-body")
            if not body:
                continue
            rows = []
            for node in body.find_all(
                    "div", class_=re.compile(r"node--forum|node--category")):
                title_el = _fc(node, "node-title", "h3")
                link_a = title_el.find("a", href=True) if title_el else None
                fname = link_a.get_text(strip=True) if link_a else ""
                fhref = link_a["href"] if link_a else ""
                desc_el = _fc(node, "node-description")
                fdesc = desc_el.get_text(strip=True) if desc_el else ""
                stats_el = _fc(node, "node-statsMeta")
                fstats = ""
                if stats_el:
                    sp = []
                    for dl in stats_el.find_all("dl"):
                        dt = dl.find("dt")
                        dd = dl.find("dd")
                        if dt and dd:
                            sp.append("{}: {}".format(
                                dt.get_text(strip=True),
                                dd.get_text(strip=True)))
                    fstats = ", ".join(sp)
                extra = _fc(node, "node-extra")
                latest = ""
                if extra:
                    la = _fc(extra, "node-extra-title", "a")
                    lt = extra.find("time")
                    lu = extra.find("a", class_="username")
                    lparts = []
                    if la:
                        ltxt = la.get_text(strip=True)[:50]
                        lhref = _proxy_page(
                            urljoin(page_url, la["href"]),
                            proxy_host, cp1256)
                        lparts.append('<a href="{}">{}</a>'.format(
                            lhref, ltxt))
                    if lu:
                        lparts.append(lu.get_text(strip=True))
                    if lt:
                        lparts.append(lt.get_text(strip=True))
                    latest = " &mdash; ".join(lparts)
                subforums = []
                sf_list = _fc(node, "node-subNodeList")
                if sf_list:
                    for sf_a in sf_list.find_all("a", href=True):
                        sf_name = sf_a.get_text(strip=True)
                        sf_href = _proxy_page(
                            urljoin(page_url, sf_a["href"]),
                            proxy_host, cp1256)
                        subforums.append(
                            '<a href="{}">{}</a>'.format(sf_href, sf_name))
                if not fname:
                    continue
                abs_href = _proxy_page(
                    urljoin(page_url, fhref), proxy_host, cp1256
                ) if fhref else ""
                row = "<tr>"
                if abs_href:
                    row += '<td><b><a href="{}">{}</a></b>'.format(
                        abs_href, fname)
                else:
                    row += "<td><b>{}</b>".format(fname)
                if fdesc:
                    row += '<br><font size="2">{}</font>'.format(fdesc)
                if subforums:
                    row += '<br><font size="1">Sub-forums: {}</font>'.format(
                        ", ".join(subforums))
                row += "</td>"
                row += '<td nowrap><font size="2">{}</font></td>'.format(
                    fstats)
                row += '<td><font size="2">{}</font></td>'.format(latest)
                row += "</tr>\n"
                rows.append(row)
            if rows:
                cat_hdr = '<b>{}</b>'.format(cat_title)
                if cat_desc:
                    cat_hdr += ' &mdash; {}'.format(cat_desc)
                parts.append(
                    '<table width="100%" border="0" cellpadding="2"'
                    ' cellspacing="0" bgcolor="#336699">'
                    '<tr><td colspan="3"><font color="#ffffff">{}'
                    '</font></td></tr></table>\n'.format(cat_hdr))
                parts.append(
                    '<table width="100%" border="0" cellpadding="3"'
                    ' cellspacing="1" bgcolor="#ffffff">\n'
                    '<tr bgcolor="#dddddd"><td><b>Forum</b></td>'
                    '<td><b>Stats</b></td>'
                    '<td><b>Last Post</b></td></tr>\n')
                parts.extend(rows)
                parts.append("</table><br>\n")

    # --- Thread listing page: structItem--thread ---
    threads = _fca(pc, "structItem--thread", "div")
    if threads:
        breadcrumb = _fc(soup, "p-breadcrumbs", "ul")
        if breadcrumb:
            crumbs = []
            for a in breadcrumb.find_all("a", href=True):
                txt = a.get_text(strip=True)
                if txt:
                    href = _proxy_page(urljoin(page_url, a["href"]),
                                       proxy_host, cp1256)
                    crumbs.append('<a href="{}">{}</a>'.format(href, txt))
            if crumbs:
                parts.append('<font size="2">{}</font><br>\n'.format(
                    " &gt; ".join(crumbs)))

        trows = []
        for t in threads:
            title_div = _fc(t, "structItem-title", "div")
            title_a = None
            ttxt = ""
            if title_div:
                title_a = title_div.find("a", href=True)
                ttxt = title_a.get_text(strip=True) if title_a else \
                    title_div.get_text(strip=True)
            sticky = _fc(t, "structItem-status--sticky", "i")
            prefix = "[Sticky] " if sticky else ""
            minor = _fc(t, "structItem-minor", "div")
            author = ""
            date = ""
            if minor:
                au = minor.find("a", class_="username")
                if au:
                    author = au.get_text(strip=True)
                tm = minor.find("time")
                if tm:
                    date = tm.get("data-short",
                                  tm.get_text(strip=True))
            meta = _fca(t, "pairs", "dl")
            stats_parts = []
            for dl in meta:
                dt = dl.find("dt")
                dd = dl.find("dd")
                if dt and dd:
                    stats_parts.append("{}: {}".format(
                        dt.get_text(strip=True),
                        dd.get_text(strip=True)))
            stats_txt = ", ".join(stats_parts)
            latest = ""
            cell_latest = _fc(t, "structItem-cell--latest", "div")
            if cell_latest:
                lt = cell_latest.find("time")
                lu = cell_latest.find("a", class_="username")
                lp = []
                if lt:
                    lp.append(lt.get("data-short", lt.get_text(strip=True)))
                if lu:
                    lp.append(lu.get_text(strip=True))
                latest = " ".join(lp)

            if not ttxt:
                continue
            thref = ""
            if title_a and title_a.get("href"):
                thref = _proxy_page(
                    urljoin(page_url, title_a["href"]),
                    proxy_host, cp1256)
            row = "<tr>"
            if thref:
                row += '<td><a href="{}">{}{}</a>'.format(
                    thref, prefix, ttxt)
            else:
                row += "<td>{}{}".format(prefix, ttxt)
            if author:
                row += '<br><font size="1">{}, {}</font>'.format(
                    author, date)
            row += "</td>"
            row += '<td nowrap><font size="2">{}</font></td>'.format(
                stats_txt)
            row += '<td nowrap><font size="2">{}</font></td>'.format(latest)
            row += "</tr>\n"
            trows.append(row)

        if trows:
            parts.append(
                '<table width="100%" border="0" cellpadding="3"'
                ' cellspacing="1" bgcolor="#ffffff">\n'
                '<tr bgcolor="#dddddd"><td><b>Thread</b></td>'
                '<td><b>Stats</b></td>'
                '<td><b>Last Post</b></td></tr>\n')
            parts.extend(trows)
            parts.append("</table>\n")

        # Pagination
        pnav = _fc(pc, "pageNav", "div")
        if pnav:
            page_links = []
            for a in pnav.find_all("a", href=True):
                ptxt = a.get_text(strip=True)
                if ptxt:
                    phref = _proxy_page(urljoin(page_url, a["href"]),
                                        proxy_host, cp1256)
                    page_links.append(
                        '<a href="{}">{}</a>'.format(phref, ptxt))
            if page_links:
                parts.append(
                    '<p><font size="2">Pages: {}</font></p>\n'.format(
                        " ".join(page_links)))

    # --- Thread / post view: message--post articles ---
    posts = _fca(pc, "message--post", "article")
    if not posts:
        # Posts may be inside a block--messages wrapper
        msg_block = _fc(pc, "block--messages", "div")
        if msg_block:
            posts = msg_block.find_all("article", class_="message--post")
    if posts:
        breadcrumb = _fc(soup, "p-breadcrumbs", "ul")
        if breadcrumb:
            crumbs = []
            for a in breadcrumb.find_all("a", href=True):
                txt = a.get_text(strip=True)
                if txt:
                    href = _proxy_page(urljoin(page_url, a["href"]),
                                       proxy_host, cp1256)
                    crumbs.append('<a href="{}">{}</a>'.format(href, txt))
            if crumbs:
                parts.append('<font size="2">{}</font><br>\n'.format(
                    " &gt; ".join(crumbs)))

        h1 = _fc(soup, "p-title-value", "h1")
        if h1:
            parts.append("<h2>{}</h2>\n".format(h1.get_text(strip=True)))

        for post in posts:
            author = post.get("data-author", "")
            tm = post.find("time")
            date = tm.get_text(strip=True) if tm else ""
            body_el = _fc(post, "message-body", "article")
            if not body_el:
                body_el = _fc(post, "message-body", "div")
            body_html = ""
            if body_el:
                bw = _fc(body_el, "bbWrapper", "div")
                if bw:
                    body_html = bw.decode_contents()
                else:
                    body_html = body_el.decode_contents()

            # Proxy images in post body
            body_html = re.sub(
                r'<img[^>]*\bsrc="([^"]+)"[^>]*/?>',
                lambda m: '<img src="{}">'.format(
                    _proxy_img(urljoin(page_url, m.group(1)), proxy_host)),
                body_html)
            # Proxy links in post body (both relative and absolute)
            body_html = re.sub(
                r'href="((?:https?://[^"]+|/[^"]*))"',
                lambda m: 'href="{}"'.format(
                    _proxy_page(urljoin(page_url, m.group(1)),
                                proxy_host, cp1256)),
                body_html)

            parts.append(
                '<table width="100%" border="0" cellpadding="4"'
                ' cellspacing="0" bgcolor="#f0f0f0">'
                '<tr><td><b>{}</b> &mdash; <font size="2">{}</font>'
                '</td></tr></table>\n'.format(author, date))
            parts.append(
                '<table width="100%" border="0" cellpadding="6"'
                ' cellspacing="0"><tr><td>{}</td></tr></table>\n'
                '<hr size="1" noshade>\n'.format(body_html))

        # Pagination
        pnav = _fc(pc, "pageNav", "div")
        if pnav:
            page_links = []
            for a in pnav.find_all("a", href=True):
                ptxt = a.get_text(strip=True)
                if ptxt:
                    phref = _proxy_page(urljoin(page_url, a["href"]),
                                        proxy_host, cp1256)
                    page_links.append(
                        '<a href="{}">{}</a>'.format(phref, ptxt))
            if page_links:
                parts.append(
                    '<p><font size="2">Pages: {}</font></p>\n'.format(
                        " ".join(page_links)))

    if not parts:
        return None

    return "\n".join(parts)


# ── Main content heuristic ─────────────────────────────────────────────────

def _find_main(soup):
    """
    Return the tag most likely to contain the main article content.
    Tries <main>, then id/class hints, then falls back to <body>.
    Article-specific ids/classes (article, post, entry, story) are
    preferred over generic ones (content, body, text).
    """
    tag = soup.find("main")
    if tag:
        return tag

    # Two tiers: article-specific (priority) and generic
    best_article = None
    best_article_len = 0
    best_generic = None
    best_generic_len = 0
    _SKIP_MAIN = frozenset({"script", "style", "head", "title", "noscript"})
    for candidate in soup.find_all(True):
        if candidate.name in _SKIP_MAIN:
            continue
        cid  = candidate.get("id", "")
        ccls = " ".join(candidate.get("class", []))
        is_article = (_ARTICLE_ID_RE.search(cid) or
                      _ARTICLE_CLS_RE.search(ccls))
        is_generic = (_MAIN_ID_RE.search(cid) or
                      _MAIN_CLS_RE.search(ccls))
        if not is_article and not is_generic:
            continue
        tlen = len(candidate.get_text(strip=True))
        if tlen <= 200:
            continue
        if is_article and tlen > best_article_len:
            best_article = candidate
            best_article_len = tlen
        elif not is_article and tlen > best_generic_len:
            best_generic = candidate
            best_generic_len = tlen

    # Prefer article-specific match, but only if it covers a substantial
    # portion of the generic match.  On homepages the generic container
    # (e.g. div.content) holds the whole page while an article-class element
    # may be just one small section — in that case prefer the generic one.
    if best_article is not None:
        if best_generic is None or best_article_len >= best_generic_len * 0.4:
            return best_article
    if best_generic is not None:
        return best_generic

    return soup.find("body") or soup


# ── URL helpers ────────────────────────────────────────────────────────────

def _proxy_page(url, proxy_host, cp1256=False):
    """Build a proxy link.  Use a path-based URL (/p/http://…) instead of
    query-string encoding (/get?url=http%3A%2F%2F…) so that very old
    browsers (IE2, Netscape 2) that mangle percent-encoded characters.

    All %-encoded sequences in the URL are decoded first so the link
    contains only plain characters.  CP-1256 mode uses /p1/ prefix to
    avoid colliding with the target URL's own query string."""
    clean_url = unquote(url)
    prefix = "/p1/" if cp1256 else "/p/"
    return "http://{}{}{}".format(proxy_host, prefix, clean_url)

def _rewrite_frameset(raw_html, page_url, proxy_host):
    """Rewrite a <frameset> page: proxy all frame src URLs and return
    the modified HTML directly (no further transformation needed)."""
    soup = BeautifulSoup(raw_html, "html.parser")
    title_tag = soup.find("title")
    title = title_tag.get_text(" ", strip=True) if title_tag else page_url
    for frame in soup.find_all("frame"):
        src = frame.get("src", "")
        if src:
            frame["src"] = _proxy_page(urljoin(page_url, src), proxy_host)
    # Also proxy background images in <body> inside <noframes>
    for body in soup.find_all("body"):
        bg = body.get("background", "")
        if bg:
            body["background"] = _proxy_img(urljoin(page_url, bg), proxy_host)
    return title, str(soup)


def _proxy_img(url, proxy_host, width=0, height=0):
    """Build an image proxy URL.  If width/height are given, append a
    size hint so the proxy can pre-resize (for IE2 which ignores HTML
    width/height attributes)."""
    base = "http://{}/img/{}".format(proxy_host, unquote(url))
    if width or height:
        base += "?_w={}&_h={}".format(int(width) if width else 0,
                                       int(height) if height else 0)
    return base


# ── Sabq.org extractor ─────────────────────────────────────────────────────

def _sabq_extract(raw, page_url, proxy_host, cp1256=False):
    """Extract content from sabq.org pages using embedded JSON data.
    Returns (title, html) or None if not a sabq.org URL or extraction fails."""
    parsed = urlparse(page_url)
    if parsed.hostname not in ("sabq.org", "www.sabq.org"):
        return None
    try:
        html_str = raw.decode("utf-8", errors="replace")
    except Exception:
        return None

    import re as _re, json as _json

    def _article_url(slug):
        return _proxy_page("https://sabq.org/article/" + slug,
                           proxy_host, cp1256)

    def _render_article(art, show_img=True):
        """Render a single article dict as HTML table row."""
        p = []
        title = _esc(art.get("title", ""))
        slug = art.get("slug", "")
        excerpt = _esc(art.get("excerpt", ""))
        cat = _esc(art.get("categoryName", ""))
        img_url = art.get("thumbnailUrl") or art.get("imageUrl", "")
        if img_url and not img_url.startswith("http"):
            img_url = "https://sabq.org" + img_url
        link = _article_url(slug) if slug else "#"
        p.append('<table border="0" cellpadding="4" cellspacing="0" '
                 'width="100%"><tr>')
        if show_img and img_url:
            p.append('<td valign="top" width="160">'
                     '<a href="{lnk}"><img src="{img}" border="0" '
                     'width="150" alt=""></a></td>'.format(
                         lnk=link,
                         img=_proxy_img(img_url, proxy_host, 150, 0)))
        p.append('<td valign="top">')
        if cat:
            p.append('<font color="#c0392b" size="2"><b>{}</b></font>'
                     '<br>'.format(cat))
        p.append('<a href="{}"><b>{}</b></a>'.format(link, title))
        if excerpt:
            p.append('<br><font size="2">{}</font>'.format(excerpt))
        views = art.get("views", 0)
        if views:
            p.append('<br><font size="1" color="gray">{} '
                     '\u0645\u0634\u0627\u0647\u062f\u0629</font>'
                     .format(views))
        p.append('</td></tr></table>')
        return "\n".join(p)

    path = parsed.path.rstrip("/")

    # ── Article page ──────────────────────────────────────────────────
    if path.startswith("/article/"):
        slug = path[len("/article/"):]

        # Fetch full article from API
        api_data = None
        try:
            _api_url = "https://sabq.org/api/articles/" + slug
            _api_resp = _session.get(_api_url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36"})
            if _api_resp.status_code == 200:
                api_data = _json.loads(_api_resp.text)
        except Exception:
            pass

        # Fallback to JSON-LD if API fails
        if not api_data:
            for m in _re.finditer(
                    r'<script[^>]*type="application/ld\+json"[^>]*>'
                    r'(.*?)</script>', html_str, _re.DOTALL):
                try:
                    d = _json.loads(m.group(1))
                    if d.get("@type") == "NewsArticle":
                        api_data = {
                            "title": d.get("headline", ""),
                            "excerpt": d.get("description", ""),
                            "content": "",
                            "imageUrl": (d.get("image", [""])[0]
                                         if isinstance(d.get("image"), list)
                                         else d.get("image", "")),
                            "category": {"nameAr":
                                         d.get("articleSection", "")},
                            "author": d.get("author", {}),
                            "publishedAt": d.get("datePublished", ""),
                            "seoMetadata": {"keywords":
                                            d.get("keywords", [])},
                        }
                        break
                except Exception:
                    continue
        if not api_data:
            return None

        title = api_data.get("title", "")
        subtitle = api_data.get("subtitle", "")
        excerpt = api_data.get("excerpt", "")
        content_html = api_data.get("content", "")
        img_url = api_data.get("imageUrl", "")
        cat_obj = api_data.get("category") or {}
        section = cat_obj.get("nameAr", "") if isinstance(cat_obj, dict) \
            else ""
        author_obj = api_data.get("author") or {}
        author = author_obj.get("name", "") if isinstance(author_obj, dict) \
            else ""
        date_pub = (api_data.get("publishedAt") or "")[:10]
        views = api_data.get("views", 0)
        seo = api_data.get("seoMetadata") or {}
        keywords = seo.get("keywords", []) if isinstance(seo, dict) else []

        parts = []
        # Logo bar with back link
        _logo_url = "https://sabq.org/assets/sabq-logo-D1EnGNyQ.png"
        parts.append('<table border="0" cellpadding="6" cellspacing="0" '
                     'width="100%" bgcolor="#0f172a"><tr>')
        parts.append('<td><a href="{}">'
                     '<img src="{}" border="0" width="100" alt="'
                     '\u0633\u0628\u0642"></a></td>'.format(
                         _proxy_page("https://sabq.org/", proxy_host,
                                     cp1256),
                         _proxy_img(_logo_url, proxy_host, 100, 0)))
        parts.append('<td align="left" valign="middle">'
                     '<a href="{}"><font color="white">'
                     '\u0627\u0644\u0631\u0626\u064a\u0633\u064a\u0629'
                     '</font></a></td>'.format(
                         _proxy_page("https://sabq.org/", proxy_host,
                                     cp1256)))
        parts.append('</tr></table>')

        # Title + metadata header
        parts.append('<table border="0" cellpadding="6" cellspacing="0" '
                     'width="100%" bgcolor="#f5f5f5"><tr><td>')
        parts.append('<font size="5"><b>{}</b></font>'.format(_esc(title)))
        if subtitle:
            parts.append('<br><font size="3" color="#555">{}</font>'
                         .format(_esc(subtitle)))
        meta = []
        if section:
            meta.append('<font color="#c0392b"><b>{}</b></font>'
                        .format(_esc(section)))
        if author:
            meta.append(_esc(author))
        if date_pub:
            meta.append(date_pub)
        if views:
            meta.append('{} \u0645\u0634\u0627\u0647\u062f\u0629'
                        .format(views))
        if meta:
            parts.append('<br><font size="2">{}</font>'
                         .format(" &middot; ".join(meta)))
        parts.append('</td></tr></table>')

        # Main image
        if img_url:
            if not img_url.startswith("http"):
                img_url = "https://sabq.org" + img_url
            parts.append('<p><img src="{}" border="0" width="600" '
                         'alt=""></p>'.format(
                             _proxy_img(img_url, proxy_host, 600, 0)))

        # Full article content from API
        if content_html:
            from bs4 import BeautifulSoup as _BS
            _csoup = _BS(content_html, "html.parser")
            # Process images first (before stripping attrs)
            for _img in _csoup.find_all("img"):
                _src = _img.get("src", "")
                if _src:
                    _img.attrs = {
                        "src": _proxy_img(_src, proxy_host, 580, 0),
                        "border": "0", "width": "580", "alt": ""
                    }
                else:
                    _img.decompose()
            # Strip style/class from all remaining elements
            for _p in _csoup.find_all(True):
                if _p.name == "img":
                    continue  # already handled
                for _attr in list(_p.attrs.keys()):
                    if _attr in ("style", "class"):
                        del _p.attrs[_attr]
            # Proxy links in article body
            for _a in _csoup.find_all("a", href=True):
                _href = _a["href"]
                if _href.startswith("/"):
                    _href = "https://sabq.org" + _href
                if _href.startswith(("http://", "https://")):
                    _a["href"] = _proxy_page(_href, proxy_host, cp1256)
            # Convert blockquotes (embedded tweets etc.) to indented text
            for _bq in _csoup.find_all("blockquote"):
                _text = _bq.get_text(" ", strip=True)
                if _text:
                    _bq.replace_with(_BS(
                        '<table border="0" cellpadding="8" '
                        'cellspacing="0" bgcolor="#f0f0f0" width="90%">'
                        '<tr><td><font size="2">{}</font></td></tr>'
                        '</table>'.format(_esc(_text)), "html.parser"))
                else:
                    _bq.decompose()
            # Remove div wrappers (unsupported in IE2)
            for _div in _csoup.find_all("div"):
                _div.unwrap()
            # Remove span wrappers (unsupported in IE2)
            for _span in _csoup.find_all("span"):
                _span.unwrap()
            body_html = str(_csoup)
            if body_html.strip():
                parts.append(body_html)
        elif excerpt:
            # Fallback to excerpt if no content
            parts.append('<p><font size="3">{}</font></p>'.format(
                _esc(excerpt)))

        if keywords:
            parts.append('<p><font size="2"><b>'
                         '\u0643\u0644\u0645\u0627\u062a '
                         '\u0645\u0641\u062a\u0627\u062d\u064a\u0629'
                         ':</b> {}</font></p>'.format(
                             " &middot; ".join(_esc(k) for k in keywords)))

        parts.append('<hr><p><a href="{}"><b>&larr; '
                     '\u0627\u0644\u0631\u0626\u064a\u0633\u064a\u0629'
                     '</b></a></p>'.format(
                         _proxy_page("https://sabq.org/", proxy_host,
                                     cp1256)))
        return _esc(title), "\n".join(parts)

    # ── Homepage ──────────────────────────────────────────────────────
    m = _re.search(r'window\.__HOMEPAGE_DATA__\s*=\s*({.*?});?\s*</script>',
                   html_str, _re.DOTALL)
    if not m:
        return None
    try:
        data = _json.loads(m.group(1))
    except Exception:
        return None

    parts = []
    title = "\u0635\u062d\u064a\u0641\u0629 \u0633\u0628\u0642"  # صحيفة سبق

    # ── Logo + category nav bar ───────────────────────────────────────
    _logo_url = "https://sabq.org/assets/sabq-logo-D1EnGNyQ.png"
    parts.append('<table border="0" cellpadding="6" cellspacing="0" '
                 'width="100%" bgcolor="#0f172a"><tr>')
    parts.append('<td width="120"><a href="{}">'
                 '<img src="{}" border="0" width="100" alt="'
                 '\u0633\u0628\u0642"></a></td>'.format(
                     _proxy_page("https://sabq.org/", proxy_host, cp1256),
                     _proxy_img(_logo_url, proxy_host, 100, 0)))
    # Collect unique categories from articles
    _seen_cats = {}
    for _sec in ("hero", "forYou", "editorPicks", "breaking", "deepDive"):
        for _art in data.get(_sec, []):
            _cn = _art.get("categoryName", "")
            _cat = _art.get("category") or {}
            if _cn and _cn not in _seen_cats:
                _seen_cats[_cn] = _cat.get("color", "")
    parts.append('<td valign="middle">')
    _cat_links = []
    for _cn in _seen_cats:
        _cat_links.append(
            '<a href="{}"><font color="white"><b>{}</b></font></a>'
            .format(_proxy_page("https://sabq.org/" + _cn,
                                proxy_host, cp1256), _esc(_cn)))
    parts.append(' &nbsp;&middot;&nbsp; '.join(_cat_links))
    parts.append('</td></tr></table>')

    # ── News ticker from <nav> ────────────────────────────────────────
    from bs4 import BeautifulSoup as _BS
    _soup = _BS(html_str, "html.parser")
    _nav = _soup.find("nav")
    if _nav:
        _ticker_links = []
        for _a in _nav.find_all("a", href=True):
            _href = _a.get("href", "")
            _txt = _a.get_text(strip=True)
            if _href.startswith("/article/") and _txt:
                _full = "https://sabq.org" + _href
                _ticker_links.append(
                    '<a href="{}"><font size="2">{}</font></a>'
                    .format(_proxy_page(_full, proxy_host, cp1256),
                            _esc(_txt)))
        if _ticker_links:
            parts.append(
                '<table border="0" cellpadding="4" cellspacing="0" '
                'width="100%" bgcolor="#fee2e2"><tr><td>'
                '<font size="2" color="#c0392b"><b>'
                '\u0622\u062e\u0631 \u0627\u0644\u0623\u062e\u0628\u0627'
                '\u0631</b></font> &nbsp; '
                + ' &nbsp;| '.join(_ticker_links[:10])
                + '</td></tr></table>')

    # Hero section
    hero = data.get("hero", [])
    if hero:
        parts.append('<table border="0" cellpadding="6" cellspacing="0" '
                     'width="100%" bgcolor="#1a1a2e"><tr><td>')
        parts.append('<font size="4" color="white"><b>'
                     '\u0623\u0628\u0631\u0632 '
                     '\u0627\u0644\u0623\u062e\u0628\u0627\u0631'
                     '</b></font>')
        parts.append('</td></tr></table>')
        for art in hero:
            parts.append(_render_article(art, show_img=True))
            parts.append('<hr size="1">')

    # For You section
    for_you = data.get("forYou", [])
    if for_you:
        parts.append('<table border="0" cellpadding="6" cellspacing="0" '
                     'width="100%" bgcolor="#2c3e50"><tr><td>')
        parts.append('<font size="4" color="white"><b>'
                     '\u0645\u062e\u062a\u0627\u0631 \u0644\u0643'
                     '</b></font>')
        parts.append('</td></tr></table>')
        for art in for_you:
            parts.append(_render_article(art, show_img=True))
            parts.append('<hr size="1">')

    # Editor Picks
    picks = data.get("editorPicks", [])
    if picks:
        parts.append('<table border="0" cellpadding="6" cellspacing="0" '
                     'width="100%" bgcolor="#8e44ad"><tr><td>')
        parts.append('<font size="4" color="white"><b>'
                     '\u0627\u062e\u062a\u064a\u0627\u0631\u0627\u062a '
                     '\u0627\u0644\u0645\u062d\u0631\u0631'
                     '</b></font>')
        parts.append('</td></tr></table>')
        for art in picks:
            parts.append(_render_article(art, show_img=True))
            parts.append('<hr size="1">')

    # Breaking news
    breaking = data.get("breaking", [])
    if breaking:
        parts.append('<table border="0" cellpadding="6" cellspacing="0" '
                     'width="100%" bgcolor="#c0392b"><tr><td>')
        parts.append('<font size="4" color="white"><b>'
                     '\u0639\u0627\u062c\u0644'
                     '</b></font>')
        parts.append('</td></tr></table>')
        for art in breaking:
            parts.append(_render_article(art, show_img=False))
            parts.append('<hr size="1">')

    # Deep Dive
    deep = data.get("deepDive", [])
    if deep:
        parts.append('<table border="0" cellpadding="6" cellspacing="0" '
                     'width="100%" bgcolor="#27ae60"><tr><td>')
        parts.append('<font size="4" color="white"><b>'
                     '\u062a\u0639\u0645\u0642'
                     '</b></font>')
        parts.append('</td></tr></table>')
        for art in deep:
            parts.append(_render_article(art, show_img=True))
            parts.append('<hr size="1">')

    # Trending topics
    trending = data.get("trending", [])
    if trending:
        parts.append('<table border="0" cellpadding="6" cellspacing="0" '
                     'width="100%" bgcolor="#e67e22"><tr><td>')
        parts.append('<font size="4" color="white"><b>'
                     '\u0627\u0644\u0623\u0643\u062b\u0631 '
                     '\u062a\u062f\u0627\u0648\u0644\u0627\u064b'
                     '</b></font>')
        parts.append('</td></tr></table>')
        parts.append('<table border="0" cellpadding="4" cellspacing="0" '
                     'width="100%">')
        for t in trending:
            topic = _esc(t.get("topic", ""))
            views = t.get("views", 0)
            articles = t.get("articles", 0)
            parts.append('<tr><td><b>{}</b></td>'
                         '<td align="left"><font size="2">{} '
                         '\u0645\u0642\u0627\u0644</font></td>'
                         '<td align="left"><font size="2">{} '
                         '\u0645\u0634\u0627\u0647\u062f\u0629'
                         '</font></td></tr>'.format(
                             topic, articles, views))
        parts.append('</table>')

    if not parts:
        return None

    return title, "\n".join(parts)


# ── YouTube extractor ──────────────────────────────────────────────────────

def _youtube_extract(raw, page_url, proxy_host, cp1256=False):
    """Extract content from YouTube pages using embedded JSON data.
    Returns (title, html) or None if not a YouTube URL or extraction fails."""
    parsed = urlparse(page_url)
    if parsed.hostname not in ("www.youtube.com", "youtube.com",
                                "m.youtube.com"):
        return None
    try:
        html_str = raw.decode("utf-8", errors="replace")
    except Exception:
        return None

    import re as _re, json as _json

    # Extract ytInitialData
    initial_data = None
    m = _re.search(r'var ytInitialData = ({.*?});</script>', html_str)
    if m:
        try:
            initial_data = _json.loads(m.group(1))
        except Exception:
            pass

    # Extract ytInitialPlayerResponse (video pages)
    player_data = None
    m2 = _re.search(r'var ytInitialPlayerResponse = ({.*?});</script>',
                     html_str)
    if m2:
        try:
            player_data = _json.loads(m2.group(1))
        except Exception:
            pass

    path = parsed.path
    query = dict(urllib.parse.parse_qsl(parsed.query))
    parts = []

    def _extract_vr(vr, shelf_title=""):
        """Extract video info from a videoRenderer dict."""
        vt = vr.get("title", {}).get("runs", [{}])[0].get("text", "")
        vid = vr.get("videoId", "")
        ch = vr.get("ownerText", vr.get("shortBylineText", {})).get(
            "runs", [{}])[0].get("text", "")
        vv = vr.get("viewCountText", {}).get(
            "simpleText", vr.get("shortViewCountText", {}).get(
                "simpleText", ""))
        th = ""
        thumbs = vr.get("thumbnail", {}).get("thumbnails", [])
        if thumbs:
            th = thumbs[-1].get("url", "")
        return (shelf_title, vt, vid, ch, vv, th)

    def _extract_gvr(gvr, shelf_title=""):
        """Extract video info from a gridVideoRenderer dict."""
        vt = gvr.get("title", {}).get("runs", [{}])[0].get(
            "text", gvr.get("title", {}).get("simpleText", ""))
        vid = gvr.get("videoId", "")
        ch = gvr.get("shortBylineText", {}).get(
            "runs", [{}])[0].get("text", "")
        vv = gvr.get("viewCountText", {}).get(
            "simpleText", gvr.get("shortViewCountText", {}).get(
                "simpleText", ""))
        th = ""
        thumbs = gvr.get("thumbnail", {}).get("thumbnails", [])
        if thumbs:
            th = thumbs[-1].get("url", "")
        return (shelf_title, vt, vid, ch, vv, th)

    # ── YouTube nav bar (logo + search + category links) ──────────────
    _yt_logo = "https://www.youtube.com/img/desktop/yt_1200.png"
    _yt_cp_field = ('<input type="hidden" name="cp1256" value="1">'
                    if cp1256 else '')
    _yt_cats = [
        ("\u0631\u0627\u0626\u062c", "/feed/trending"),      # رائج
        ("\u0645\u0648\u0633\u064a\u0642\u0649", "/feed/music"),  # موسيقى
        ("\u0623\u0644\u0639\u0627\u0628", "/gaming"),        # ألعاب
        ("\u0623\u062e\u0628\u0627\u0631", "/feed/news"),     # أخبار
        ("\u0631\u064a\u0627\u0636\u0629", "/feed/sports"),   # رياضة
    ]
    _yt_nav = ('<table border="0" cellpadding="4" cellspacing="0" '
               'width="100%" bgcolor="#ff0000"><tr>'
               '<td width="100"><a href="{home}">'
               '<img src="{logo}" border="0" width="90" alt="YouTube">'
               '</a></td>'
               '<td valign="middle">'
               '<form action="http://{host}/get" method="GET" '
               'style="margin:0;">'
               '<input type="hidden" name="url" '
               'value="https://www.youtube.com/results">{cp}'
               '<input type="text" name="search_query" size="30">'
               ' <input type="submit" value="Search">'
               '</form></td>'
               '<td align="left" valign="middle" nowrap>').format(
                   home=_proxy_page("https://www.youtube.com/",
                                    proxy_host, cp1256),
                   logo=_proxy_img(_yt_logo, proxy_host, 90, 0),
                   host=proxy_host,
                   cp=_yt_cp_field)
    _cat_parts = []
    for _cname, _cpath in _yt_cats:
        _cat_parts.append(
            '<a href="{}"><font color="white" size="2">'
            '<b>{}</b></font></a>'.format(
                _proxy_page("https://www.youtube.com" + _cpath,
                            proxy_host, cp1256),
                _cname))
    _yt_nav += ' &nbsp; '.join(_cat_parts)
    _yt_nav += '</td></tr></table>'
    parts.append(_yt_nav)

    # ── Video page (/watch?v=...) ──
    if path == "/watch" and "v" in query and player_data:
        vd = player_data.get("videoDetails", {})
        title = vd.get("title", "YouTube Video")
        author = vd.get("author", "")
        views = vd.get("viewCount", "")
        length = vd.get("lengthSeconds", "")
        desc = vd.get("shortDescription", "")
        thumb = vd.get("thumbnail", {}).get("thumbnails", [{}])[-1].get(
            "url", "")

        parts.append('<h2>{}</h2>'.format(_esc(title)))
        if thumb:
            parts.append('<p><img src="{}" width="440" alt="{}"></p>'.format(
                _proxy_img(thumb, proxy_host), _esc(title)))
        parts.append('<table border="0" cellpadding="2">')
        if author:
            parts.append('<tr><td><b>Channel:</b></td><td>{}</td></tr>'
                         .format(_esc(author)))
        if views:
            parts.append('<tr><td><b>Views:</b></td><td>{}</td></tr>'
                         .format(_esc("{:,}".format(int(views))
                                      if views.isdigit() else views)))
        if length:
            try:
                mins, secs = divmod(int(length), 60)
                parts.append(
                    '<tr><td><b>Length:</b></td><td>{}:{:02d}</td></tr>'
                    .format(mins, secs))
            except ValueError:
                pass
        parts.append('</table>')
        if desc:
            parts.append('<hr><p>{}</p>'.format(
                _esc(desc).replace("\n", "<br>")))

        # Related videos from ytInitialData
        if initial_data:
            related = []
            secondary = initial_data.get("contents", {}).get(
                "twoColumnWatchNextResults", {}).get(
                "secondaryResults", {}).get(
                "secondaryResults", {}).get("results", [])
            for item in secondary[:15]:
                cvr = item.get("compactVideoRenderer", {})
                if not cvr:
                    continue
                r_title = cvr.get("title", {}).get("simpleText", "")
                r_vid = cvr.get("videoId", "")
                r_channel = cvr.get("longBylineText", {}).get(
                    "runs", [{}])[0].get("text", "")
                r_views = cvr.get("viewCountText", {}).get(
                    "simpleText", "")
                r_length = cvr.get("lengthText", {}).get(
                    "simpleText", "")
                if r_title and r_vid:
                    related.append((r_title, r_vid, r_channel,
                                    r_views, r_length))
            if related:
                parts.append('<hr><h3>Related Videos</h3>')
                parts.append(
                    '<table border="1" cellpadding="3" cellspacing="1">')
                for r_title, r_vid, r_ch, r_v, r_l in related:
                    link = _proxy_page(
                        "https://www.youtube.com/watch?v=" + r_vid,
                        proxy_host, cp1256)
                    parts.append(
                        '<tr><td><a href="{}">{}</a></td>'
                        '<td>{}</td><td>{}</td><td>{}</td></tr>'
                        .format(link, _esc(r_title), _esc(r_ch),
                                _esc(r_v), _esc(r_l)))
                parts.append('</table>')

        return title, "\n".join(parts)

    # ── Playlist page (/playlist?list=...) ──
    if path == "/playlist" and "list" in query and initial_data:
        pl_header = initial_data.get("header", {}).get(
            "playlistHeaderRenderer", {})
        pl_title = pl_header.get("title", {}).get("simpleText", "Playlist")
        pl_desc = ""
        _desc_obj = pl_header.get("descriptionText", {})
        if isinstance(_desc_obj, dict):
            pl_desc = _desc_obj.get("simpleText", "")
        pl_count = pl_header.get("numVideosText", {}).get(
            "runs", [{}])[0].get("text", "")
        title = pl_title

        parts.append('<h2>{}</h2>'.format(_esc(pl_title)))
        _meta_parts = []
        if pl_count:
            _meta_parts.append(pl_count)
        if _meta_parts:
            parts.append('<p><font size="2">{}</font></p>'.format(
                " &middot; ".join(_meta_parts)))
        if pl_desc:
            parts.append('<p>{}</p>'.format(_esc(pl_desc)))

        # Extract playlist videos
        try:
            tabs = initial_data["contents"][
                "twoColumnBrowseResultsRenderer"]["tabs"]
            for _tab in tabs:
                _slr = _tab.get("tabRenderer", {}).get(
                    "content", {}).get("sectionListRenderer", {})
                for _sec in _slr.get("contents", []):
                    _isr = _sec.get("itemSectionRenderer", {})
                    for _c in _isr.get("contents", []):
                        _plvlr = _c.get("playlistVideoListRenderer", {})
                        for _vi in _plvlr.get("contents", []):
                            _pvr = _vi.get("playlistVideoRenderer", {})
                            if not _pvr:
                                continue
                            _vt = _pvr.get("title", {}).get(
                                "runs", [{}])[0].get("text", "")
                            _vid = _pvr.get("videoId", "")
                            _vch = _pvr.get("shortBylineText", {}).get(
                                "runs", [{}])[0].get("text", "")
                            _vl = _pvr.get("lengthText", {}).get(
                                "simpleText", "")
                            _vth = ""
                            _thumbs = _pvr.get("thumbnail", {}).get(
                                "thumbnails", [])
                            if _thumbs:
                                _vth = _thumbs[-1].get("url", "")
                            if _vt and _vid:
                                _link = _proxy_page(
                                    "https://www.youtube.com/watch?v="
                                    + _vid, proxy_host, cp1256)
                                parts.append(
                                    '<table border="0" cellpadding="2">'
                                    '<tr><td valign="top">')
                                if _vth:
                                    parts.append(
                                        '<a href="{}"><img src="{}" '
                                        'width="120"></a>'.format(
                                            _link,
                                            _proxy_img(_vth, proxy_host)))
                                parts.append(
                                    '</td><td valign="top">'
                                    '<b><a href="{}">{}</a></b>'.format(
                                        _link, _esc(_vt)))
                                if _vch:
                                    parts.append(
                                        '<br><font size="2">{}</font>'
                                        .format(_esc(_vch)))
                                if _vl:
                                    parts.append(
                                        '<br><font size="2">{}</font>'
                                        .format(_esc(_vl)))
                                parts.append('</td></tr></table>')
        except (KeyError, IndexError):
            pass

        return title, "\n".join(parts)

    # ── Search results (/results?search_query=...) ──
    if path == "/results" and initial_data:
        sq = query.get("search_query", "")
        title = "YouTube: " + sq if sq else "YouTube Search"
        results = []
        try:
            sections = initial_data["contents"][
                "twoColumnSearchResultsRenderer"]["primaryContents"][
                "sectionListRenderer"]["contents"]
            for section in sections:
                items = section.get("itemSectionRenderer", {}).get(
                    "contents", [])
                for item in items:
                    vr = item.get("videoRenderer", {})
                    if not vr:
                        continue
                    v_title = vr.get("title", {}).get("runs", [{}])[0].get(
                        "text", "")
                    v_id = vr.get("videoId", "")
                    v_channel = vr.get("ownerText", {}).get(
                        "runs", [{}])[0].get("text", "")
                    v_views = vr.get("viewCountText", {}).get(
                        "simpleText", "")
                    v_length = vr.get("lengthText", {}).get(
                        "simpleText", "")
                    v_desc = ""
                    snippets = vr.get("detailedMetadataSnippets", [])
                    if snippets:
                        runs = snippets[0].get("snippetText", {}).get(
                            "runs", [])
                        v_desc = "".join(r.get("text", "") for r in runs)
                    v_thumb = ""
                    thumbs = vr.get("thumbnail", {}).get("thumbnails", [])
                    if thumbs:
                        v_thumb = thumbs[-1].get("url", "")
                    if v_title and v_id:
                        results.append((v_title, v_id, v_channel,
                                        v_views, v_length, v_desc, v_thumb))
        except (KeyError, IndexError):
            pass

        if results:
            parts.append('<h2>Search: {}</h2>'.format(_esc(sq)))
            for v_title, v_id, v_ch, v_v, v_l, v_d, v_th in results[:20]:
                link = _proxy_page(
                    "https://www.youtube.com/watch?v=" + v_id, proxy_host,
                    cp1256)
                parts.append('<table border="0" cellpadding="2">'
                             '<tr><td valign="top">')
                if v_th:
                    parts.append(
                        '<a href="{}"><img src="{}" width="120"></a>'
                        .format(link, _proxy_img(v_th, proxy_host)))
                parts.append('</td><td valign="top">')
                parts.append('<b><a href="{}">{}</a></b>'.format(
                    link, _esc(v_title)))
                if v_ch:
                    parts.append('<br><font size="2">{}</font>'.format(
                        _esc(v_ch)))
                meta = " | ".join(x for x in (v_v, v_l) if x)
                if meta:
                    parts.append('<br><font size="2">{}</font>'.format(
                        _esc(meta)))
                if v_d:
                    parts.append(
                        '<br><font size="1" color="#666666">{}</font>'
                        .format(_esc(v_d)))
                parts.append('</td></tr></table><br>')
            return title, "\n".join(parts)

    # ── Homepage, category, or other browse page ──
    title = "YouTube"

    # Try to extract videos from browse pages (trending, music, gaming…)
    _browse_videos = []  # list of (shelf_title, title, videoId, channel,
                         #          views, thumb_url)
    if initial_data:
        try:
            tabs = initial_data.get("contents", {}).get(
                "twoColumnBrowseResultsRenderer", {}).get("tabs", [])
            for _tab in tabs:
                _tc = _tab.get("tabRenderer", {}).get("content", {})
                # richGridRenderer (music, trending)
                _rgr = _tc.get("richGridRenderer", {})
                for _ri in _rgr.get("contents", []):
                    _rsr = _ri.get("richSectionRenderer", {})
                    _rshelf = _rsr.get("content", {}).get(
                        "richShelfRenderer", {})
                    if _rshelf:
                        _st = _rshelf.get("title", {}).get(
                            "runs", [{}])[0].get("text", "")
                        for _sc in _rshelf.get("contents", []):
                            _vc = _sc.get("richItemRenderer", {}).get(
                                "content", {})
                            _vr = _vc.get("videoRenderer", {})
                            if _vr:
                                _browse_videos.append(_extract_vr(
                                    _vr, _st))
                            # lockupViewModel (music playlists)
                            _lvm = _vc.get("lockupViewModel", {})
                            if _lvm:
                                _meta = _lvm.get("metadata", {}).get(
                                    "lockupMetadataViewModel", {})
                                _lt = _meta.get("title", {}).get(
                                    "content", "")
                                _lid = _lvm.get("contentId", "")
                                _lth = ""
                                _ci = _lvm.get("contentImage", {})
                                _pt = _ci.get(
                                    "collectionThumbnailViewModel",
                                    _ci.get("thumbnailViewModel", {}))
                                if _pt:
                                    _tvm = _pt.get(
                                        "primaryThumbnail",
                                        _pt).get(
                                        "thumbnailViewModel",
                                        _pt).get("image", {})
                                    _srcs = _tvm.get("sources", [])
                                    if _srcs:
                                        _lth = _srcs[0].get("url", "")
                                if _lt and _lid:
                                    _browse_videos.append(
                                        (_st, _lt, _lid, "", "", _lth,
                                         True))
                # sectionListRenderer (gaming)
                _slr = _tc.get("sectionListRenderer", {})
                for _si in _slr.get("contents", []):
                    _isr = _si.get("itemSectionRenderer", {})
                    for _c in _isr.get("contents", []):
                        _sr = _c.get("shelfRenderer", {})
                        if _sr:
                            _st = _sr.get("title", {}).get(
                                "runs", [{}])[0].get(
                                "text", _sr.get("title", {}).get(
                                    "simpleText", ""))
                            _hlr = _sr.get("content", {}).get(
                                "horizontalListRenderer", {})
                            for _vi in _hlr.get("items", []):
                                _gvr = _vi.get("gridVideoRenderer", {})
                                if _gvr:
                                    _browse_videos.append(
                                        _extract_gvr(_gvr, _st))
                        _hcl = _c.get("horizontalCardListRenderer", {})
                        if _hcl:
                            for _card in _hcl.get("cards", []):
                                _gvr = _card.get(
                                    "gridVideoRenderer", {})
                                if _gvr:
                                    _browse_videos.append(
                                        _extract_gvr(_gvr, ""))
        except Exception:
            pass

    if _browse_videos:
        # Group by shelf title
        _current_shelf = None
        for _bv in _browse_videos:
            _is_playlist = len(_bv) > 6 and _bv[6]
            _st, _vt, _vid, _vch, _vv, _vth = _bv[:6]
            if _st and _st != _current_shelf:
                if _current_shelf is not None:
                    parts.append('<br>')
                parts.append(
                    '<table border="0" cellpadding="4" cellspacing="0" '
                    'width="100%" bgcolor="#e0e0e0"><tr><td>'
                    '<b>{}</b></td></tr></table>'.format(_esc(_st)))
                _current_shelf = _st
            if _is_playlist:
                _link = _proxy_page(
                    "https://www.youtube.com/playlist?list=" + _vid,
                    proxy_host, cp1256)
            else:
                _link = _proxy_page(
                    "https://www.youtube.com/watch?v=" + _vid,
                    proxy_host, cp1256)
            parts.append('<table border="0" cellpadding="2"><tr>'
                         '<td valign="top">')
            if _vth:
                parts.append(
                    '<a href="{}"><img src="{}" width="120"></a>'
                    .format(_link, _proxy_img(_vth, proxy_host)))
            parts.append('</td><td valign="top">')
            parts.append('<b><a href="{}">{}</a></b>'.format(
                _link, _esc(_vt)))
            if _vch:
                parts.append('<br><font size="2">{}</font>'.format(
                    _esc(_vch)))
            if _vv:
                parts.append('<br><font size="2">{}</font>'.format(
                    _esc(_vv)))
            parts.append('</td></tr></table>')
    else:
        parts.append('<p><b>Try searching for a video above, or browse '
                     'a channel directly.</b></p>')
        parts.append('<p>Example: '
                     '<a href="{}">youtube.com/results?search_query='
                     'retro+computing</a></p>'.format(
                         _proxy_page(
                             "https://www.youtube.com/results?"
                             "search_query=retro+computing", proxy_host,
                             cp1256)))

    return title, "\n".join(parts)


def _esc(text):
    """HTML-escape text for safe embedding."""
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))

def _abs(href, base_url):
    """Resolve href to absolute URL; return None if not http(s)."""
    if not href:
        return None
    href = href.strip()
    if href.startswith(("javascript:", "data:", "#")):
        return None
    if href.startswith(("mailto:", "tel:", "sms:")):
        return href
    try:
        abs_url = urljoin(base_url, href)
        scheme = urlparse(abs_url).scheme
        if scheme in ("http", "https"):
            return abs_url
    except Exception:
        pass
    return None

def _real_img_src(tag, base_url):
    """Return the effective absolute src of an <img>, handling lazy-load."""
    src = tag.get("src", "")
    if not src or src.startswith("data:") or len(src.strip()) < 5:
        for attr in _LAZY_SRC:
            lazy = tag.get(attr, "").strip()
            if lazy and not lazy.startswith("data:"):
                src = lazy
                break
    return _abs(src, base_url)


# ── JSON-LD article fallback ───────────────────────────────────────────────

def _extract_jsonld_article(soup):
    """
    Extract article content from <script type="application/ld+json"> blocks.
    Returns an HTML string suitable for display, or None if nothing useful
    is found.  This is used as a fallback for JS-rendered (SPA) pages whose
    static HTML contains only placeholders like "undefined".
    """
    import json as _json
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or ""
        if not raw.strip():
            continue
        try:
            data = _json.loads(raw)
        except (ValueError, _json.JSONDecodeError):
            continue
        # Normalize to a list
        items = data if isinstance(data, list) else [data]
        for item in items:
            body = _jsonld_body(item)
            if body and len(body) > 100:
                return body
    return None


def _jsonld_body(item):
    """Try to extract readable HTML from a single JSON-LD object."""
    if not isinstance(item, dict):
        return None
    typ = item.get("@type", "")

    # Article / NewsArticle / BlogPosting
    if typ in ("Article", "NewsArticle", "BlogPosting", "WebPage",
               "QAPage", "Report", "TechArticle"):
        parts = []
        name = item.get("headline") or item.get("name", "")
        if name:
            parts.append("<h2>{}</h2>".format(name))
        desc = item.get("description", "")
        if desc:
            parts.append("<p>{}</p>".format(desc))
        body = item.get("articleBody", "")
        if body:
            # articleBody may be plain text — wrap paragraphs
            for para in body.split("\n"):
                para = para.strip()
                if para:
                    parts.append("<p>{}</p>".format(para))
        # QAPage — look inside MainEntity
        main_entity = item.get("MainEntity") or item.get("mainEntity")
        if isinstance(main_entity, dict):
            q_text = main_entity.get("name") or main_entity.get("text", "")
            if q_text:
                parts.append("<h3>{}</h3>".format(q_text))
            answer = main_entity.get("acceptedAnswer") or main_entity.get("suggestedAnswer")
            if isinstance(answer, dict):
                ans_text = answer.get("text", "")
                if ans_text:
                    for para in ans_text.split("\n"):
                        para = para.strip()
                        if para:
                            parts.append("<p>{}</p>".format(para))
            elif isinstance(answer, list):
                for a in answer:
                    if isinstance(a, dict):
                        ans_text = a.get("text", "")
                        if ans_text:
                            parts.append("<p>{}</p>".format(ans_text))
        if parts:
            return "\n".join(parts)
    return None


def _extract_apollo_article(soup):
    """
    Extract article content from __APOLLO_STATE__ (used by Next.js/Apollo
    sites like Al Jazeera).  The state is a base64-encoded JSON blob
    embedded in a <script> tag.  Returns an HTML string or None.
    """
    import json as _json
    import base64 as _b64
    for script in soup.find_all("script"):
        text = script.string or ""
        if "__APOLLO_STATE__" not in text:
            continue
        try:
            start = text.index('"') + 1
            end = text.rindex('"')
            data = _json.loads(_b64.b64decode(text[start:end]))
        except Exception:
            continue
        # Find the Post entry with the longest content
        best = ""
        best_title = ""
        for key, val in data.items():
            if not isinstance(val, dict):
                continue
            content = val.get("content") or val.get("body") or ""
            if not isinstance(content, str) or len(content) <= len(best):
                continue
            best = content
            best_title = val.get("title") or val.get("headline") or ""
        if len(best) > 200:
            parts = []
            if best_title:
                parts.append("<h2>{}</h2>".format(best_title))
            parts.append(best)
            return "\n".join(parts)
    return None


# ── Readability (Mozilla) article extractor ───────────────────────────────

def _readability_extract(raw_html):
    """Run Mozilla's Readability algorithm (via readability-lxml) on the
    raw HTML and return (title, body_html_fragment) or None.

    Readability distills the main article content from noisy pages —
    the same technique used by Firefox Reader View and FrogFind.  We
    use it as:
      • primary content source for explicit Reader mode (/r/ route)
      • a candidate fallback inside transform_html when the existing
        _find_main() / JSON-LD / Apollo extractors don't find enough.
    Returns None when the library is unavailable, extraction fails, or
    the extracted content is too short to be useful."""
    if not HAS_READABILITY or not raw_html:
        return None
    try:
        if isinstance(raw_html, bytes):
            raw_html = raw_html.decode("utf-8", errors="replace")
        doc = _ReadabilityDocument(raw_html)
        summary = doc.summary(html_partial=True)
        if not summary:
            return None
        # Require a reasonable amount of plain text — homepages and
        # directory pages typically yield <300 chars and shouldn't
        # override the existing pipeline.
        plain = re.sub(r"<[^>]+>", " ", summary)
        plain = re.sub(r"\s+", " ", plain).strip()
        if len(plain) < 300:
            return None
        title = (doc.short_title() or doc.title() or "").strip()
        return title, summary
    except Exception:
        return None


# ── HTML transformer ───────────────────────────────────────────────────────

# Patterns used inside transform_html.  Hoisted to module scope so they are
# compiled once at import time rather than on every request.
_TEMPLATE_RE = re.compile(r"\{\{.*?\}\}")
_LOADING_RE = re.compile(
    r"جارٍ تحميل البيانات|Loading\.\.\.|جاري التحميل", re.I)
_HIGHCHARTS_RE = re.compile(r"highcharts")
_JUNK_CLS_RE = re.compile(
    r"\b(share-buttons|ad-space|banner\d|popup|overlay-modal|"
    r"notification-box|cookie|social-share|article-breif|"
    r"share-loader|comment_container)\b", re.I)
_SEARCH_INPUT_TYPE_RE = re.compile(r"^(text|search)$", re.I)
_CAROUSEL_CLS_RE = re.compile(
    r"\b(owl-carousel|slick-slider|slick-track|swiper-wrapper|"
    r"carousel-inner|flickity-slider|glide__slides)\b", re.I)
_JUNK_IMG_RE = re.compile(
    r"(close[_-]?icon|share[_-]?loader|spinner|loading|loader|"
    r"spacer|pixel|blank|arrow[_-]?icon|search[_-]?loader|"
    r"tools[_-]?logo)\b", re.I)
_AVATAR_RE = re.compile(
    r"(avatar|profile[_-]?(?:pic|img|image|photo)|"
    r"user[_-]?(?:pic|img|image|photo))", re.I)
_LINK_RE = re.compile(r'<a\s[^>]*href="[^"]*"[^>]*>.*?</a>', re.S)
_BR_RE = re.compile(r'\s*<br/?>\s*', re.S)


def transform_html(raw_html, page_url, proxy_host, cp1256=False):
    """
    Parse and transform raw HTML to old-browser-compatible HTML 3.2.
    Returns (title: str, content_html: str, is_rtl: bool).
    """
    soup = BeautifulSoup(raw_html, "html.parser")

    # 0. Detect RTL from the original, untouched markup
    is_rtl = _detect_rtl(soup)

    # 1. Detect and render forum pages (XenForo) with a dedicated renderer.
    #    This MUST run on the untouched DOM before any other transforms.
    forum_html = _render_xenforo(soup, page_url, proxy_host, cp1256)
    if forum_html is not None:
        title_tag = soup.find("title")
        title = title_tag.get_text(" ", strip=True) if title_tag else page_url
        # Rescue site header for the forum page too
        site_header_html = ""
        header_el = soup.find("header")
        nav_el = soup.find("nav")
        _header_zone = header_el or nav_el
        if _header_zone:
            logo_img = _header_zone.find("img")
            brand_link = None
            if logo_img:
                parent_a = logo_img.find_parent("a")
                if parent_a:
                    brand_link = parent_a
            hparts = []
            if logo_img:
                raw_src = logo_img.get("src", "")
                if raw_src:
                    psrc = _proxy_img(urljoin(page_url, raw_src), proxy_host)
                    hparts.append(
                        '<img src="{}" width="150">'.format(psrc))
            if brand_link:
                btxt = brand_link.get_text(strip=True)
                if btxt:
                    hparts.append(" <b>{}</b>".format(btxt))
            # Top-level nav links
            nav_el_top = soup.find("nav")
            nav_links = []
            if nav_el_top:
                top_ul = nav_el_top.find("ul")
                seen = set()
                if top_ul:
                    for li in top_ul.find_all("li", recursive=False):
                        a = li.find("a", href=True)
                        if a and a is not brand_link:
                            txt = a.get_text(strip=True)
                            href = a.get("href", "")
                            if txt and 2 <= len(txt) <= 40 and href:
                                ah = urljoin(page_url, href)
                                if ah not in seen:
                                    seen.add(ah)
                                    nav_links.append(
                                        '<a href="{}">{}</a>'.format(
                                            _proxy_page(ah, proxy_host,
                                                        cp1256), txt))
                                if len(nav_links) >= 12:
                                    break
            site_header_html = " ".join(hparts)
            if nav_links:
                nav_cells = ['<td nowrap><font size="2">{}</font></td>'.format(l)
                             for l in nav_links]
                site_header_html += (
                    '<br><table border="0" cellpadding="2" '
                    'cellspacing="0"><tr>' +
                    "".join(nav_cells) + '</tr></table>')
            if site_header_html:
                site_header_html = (
                    '<table width="100%" border="0" cellpadding="4"'
                    ' cellspacing="0" bgcolor="#eeeeee"><tr><td>'
                    + site_header_html +
                    '</td></tr></table><hr size="1" noshade>\n')
        return title, site_header_html + forum_html, is_rtl, False, None, None, {}

    # 1b. Parse CSS layout information BEFORE removing <style> tags
    css_layouts = _parse_css_layouts(soup)
    css_img_rules = _parse_css_img_sizes(soup)

    # 1c. Convert dropdown menus to <select> BEFORE stripping tags
    #     (needs class/id attrs and the original DOM structure intact)
    _convert_dropdowns_to_select(soup, page_url, proxy_host, cp1256)

    # 1d. Extract article content from embedded data BEFORE scripts are
    #     removed.  JS-rendered (SPA) sites may have empty/placeholder
    #     body content but include the real article text in JSON-LD or
    #     in JS state variables like __APOLLO_STATE__.
    jsonld_fallback = _extract_jsonld_article(soup)
    if not jsonld_fallback:
        jsonld_fallback = _extract_apollo_article(soup)

    # 1d-bis. Mozilla Readability: runs on the raw HTML (before tag
    #         stripping) and returns a distilled article body.  Used as
    #         another candidate alongside _find_main / JSON-LD / Apollo —
    #         the richest one wins later (step 13b).
    readability_result = _readability_extract(raw_html)
    readability_fallback = readability_result[1] if readability_result else None

    # 1e. Preserve <body> visual attributes before processing
    body_tag = soup.find("body")
    body_bg_img = None
    body_bgcolor = None
    body_attrs = {}  # text, link, vlink, alink colors
    if body_tag:
        bg = body_tag.get("background", "")
        if bg:
            body_bg_img = _proxy_img(urljoin(page_url, bg), proxy_host)
        bgc = body_tag.get("bgcolor", "")
        if bgc:
            body_bgcolor = bgc
        for attr in ("text", "link", "vlink", "alink"):
            val = body_tag.get(attr, "")
            if val:
                body_attrs[attr] = val

    # 2. Remove HTML comments
    for node in soup.find_all(string=lambda t: isinstance(t, Comment)):
        node.extract()

    # 2a. Remove JS framework template text and loading placeholders.
    #     Angular/Vue/Mustache templates like {{expr}} are useless without JS.
    #     Also remove common Arabic/English loading spinners.
    #     (_TEMPLATE_RE / _LOADING_RE are compiled at module scope.)
    for text_node in soup.find_all(string=True):
        if isinstance(text_node, Comment):
            continue
        s = str(text_node)
        if _TEMPLATE_RE.search(s):
            cleaned = _TEMPLATE_RE.sub("", s).strip()
            if cleaned:
                text_node.replace_with(cleaned)
            else:
                text_node.extract()
        elif _LOADING_RE.search(s):
            text_node.extract()

    # 2b. Convert inline <svg> elements to JPEG <img> tags.
    #     Old browsers can't render SVG at all, but cairosvg can rasterize
    #     them into PNG → JPEG.
    for svg_tag in soup.find_all("svg"):
        try:
            import cairosvg as _cairosvg
            # Inject default styles for Highcharts SVGs — cairosvg
            # doesn't know about the CSS classes Highcharts uses for
            # colors, so charts render as black blobs without this.
            if svg_tag.find(class_=_HIGHCHARTS_RE):
                _hc_style = svg_tag.find("style") or \
                    soup.new_tag("style")
                _hc_css = (
                    ".highcharts-background{fill:#fff}"
                    ".highcharts-color-0 .highcharts-area{fill:#7cb5ec;"
                    "fill-opacity:0.75}"
                    ".highcharts-color-0 .highcharts-graph{stroke:#7cb5ec;"
                    "stroke-width:2;fill:none}"
                    ".highcharts-color-1 .highcharts-area{fill:#90ed7d;"
                    "fill-opacity:0.75}"
                    ".highcharts-color-1 .highcharts-graph{stroke:#90ed7d;"
                    "stroke-width:2;fill:none}"
                    ".highcharts-axis-line{stroke:#ccd6eb;stroke-width:1}"
                    ".highcharts-grid-line{stroke:#e6e6e6;stroke-width:1}"
                    ".highcharts-tick{stroke:#ccd6eb;stroke-width:1}"
                    ".highcharts-axis-labels text{fill:#666;font-size:11px}"
                    ".highcharts-tracker-line{stroke-width:0;fill:none}"
                    ".highcharts-halo{fill:none}"
                )
                if not _hc_style.parent:
                    _hc_style.string = _hc_css
                    svg_tag.insert(0, _hc_style)
                else:
                    _hc_style.string = (_hc_style.string or "") + _hc_css
            # Fix SVGs that are invisible on white background.
            # 1) Tailwind "fill-current"/"stroke-current" classes inherit
            #    color from CSS context which cairosvg doesn't have.
            # 2) SVGs designed for dark backgrounds use fill="#FFF" (white)
            #    which becomes invisible on a white JPEG.
            # Detect white fills and replace with dark color.
            _svg_cls = " ".join(svg_tag.get("class", []))
            _needs_recolor = ("fill-current" in _svg_cls
                              or "stroke-current" in _svg_cls)
            if not _needs_recolor:
                # Check if child elements use white fill
                for _cel in svg_tag.find_all(True):
                    _cfill = (_cel.get("fill", "") or "").strip().lower()
                    if _cfill in ("#fff", "#ffffff", "white"):
                        _needs_recolor = True
                        break
            if _needs_recolor:
                # Replace white/missing fills with dark color
                for _cel in svg_tag.find_all(True):
                    _cfill = (_cel.get("fill", "") or "").strip().lower()
                    if _cfill in ("#fff", "#ffffff", "white"):
                        _cel["fill"] = "#333"
                    _cstroke = (_cel.get("stroke", "") or "").strip().lower()
                    if _cstroke in ("#fff", "#ffffff", "white"):
                        _cel["stroke"] = "#333"
                # Also set on root for inheritance
                if "fill-current" in _svg_cls:
                    svg_tag["fill"] = "#333"
                if "stroke-current" in _svg_cls:
                    svg_tag["stroke"] = "#333"

            svg_bytes = str(svg_tag).encode("utf-8")
            # Fix viewBox case: BeautifulSoup's html.parser lowercases
            # attributes, turning viewBox into viewbox.  cairosvg needs
            # the correct camelCase to parse coordinates properly.
            svg_bytes = svg_bytes.replace(b" viewbox=", b" viewBox=")
            # Determine a reasonable render width from attributes:
            # try width attr, then viewBox, then height attr.
            render_w = 0
            render_h = 0
            _vb_w = 0   # viewBox native dimensions
            _vb_h = 0
            for dim_attr, target in [("width", "w"), ("height", "h")]:
                val = str(svg_tag.get(dim_attr, "")).strip()
                try:
                    num = float(re.sub(r"[^0-9.]", "", val))
                    # Convert relative units to pixels (1em/rem ≈ 16px)
                    if "em" in val or "rem" in val:
                        n = int(num * 16)
                    elif "%" in val:
                        n = 0  # percentage — ignore, fall back to viewBox
                    else:
                        n = int(num)
                    if target == "w":
                        render_w = n
                    else:
                        render_h = n
                except (ValueError, TypeError):
                    pass
            # Also check inline style for width/height (e.g. "width:16px")
            _svg_style = svg_tag.get("style", "")
            if not render_w:
                _sw = re.search(r'width\s*:\s*(\d+)', _svg_style)
                if _sw:
                    render_w = int(_sw.group(1))
            if not render_h:
                _sh = re.search(r'height\s*:\s*(\d+)', _svg_style)
                if _sh:
                    render_h = int(_sh.group(1))
            # Try viewBox="0 0 W H" for size hints
            vb = svg_tag.get("viewbox", svg_tag.get("viewBox", ""))
            vb_parts = str(vb).split()
            if len(vb_parts) == 4:
                try:
                    _vb_w = int(float(vb_parts[2]))
                    _vb_h = int(float(vb_parts[3]))
                except (ValueError, TypeError):
                    pass
            if not render_w and _vb_w:
                render_w = _vb_w
            if not render_h and _vb_h:
                render_h = _vb_h
            # Default to 200 only if we truly have no size info
            if not render_w:
                render_w = render_h if render_h else 200
            # Cap small icon SVGs: if viewBox is tiny (≤48px, typical
            # for icons/stars) and no explicit large width was set,
            # render at native size — don't inflate to 200.
            _is_icon = (_vb_w and _vb_w <= 48 and _vb_h and _vb_h <= 48)
            if _is_icon and render_w > _vb_w * 2:
                render_w = _vb_w
                render_h = _vb_h
            render_w = min(render_w, MAX_IMG_W)
            png_data = _cairosvg.svg2png(bytestring=svg_bytes,
                                         output_width=render_w)
            # Convert PNG → JPEG (IE3 doesn't support PNG).
            # Composite onto white background first — PNG alpha channel
            # would otherwise become black in JPEG.
            from PIL import Image as _Img
            _pimg = _Img.open(io.BytesIO(png_data))
            _has_alpha = (_pimg.mode in ("RGBA", "LA", "PA")
                         or (_pimg.mode == "P"
                             and "transparency" in _pimg.info))
            if _has_alpha:
                _pimg = _pimg.convert("RGBA")
            _bg = _Img.new("RGB", _pimg.size, (255, 255, 255))
            if _pimg.mode == "RGBA":
                _bg.paste(_pimg, mask=_pimg.split()[3])
            else:
                _bg.paste(_pimg)
            _buf = io.BytesIO()
            _bg.save(_buf, format="JPEG", quality=85)
            jpg_data = _buf.getvalue()
            # Store in cache and reference via /svg/ URL (old browsers
            # don't support data: URIs)
            svg_hash = _hashlib.md5(svg_bytes).hexdigest()
            if len(_svg_cache) >= _SVG_CACHE_MAX:
                # evict oldest entry
                _svg_cache.pop(next(iter(_svg_cache)), None)
            _svg_cache[svg_hash] = jpg_data
            img_tag = soup.new_tag("img")
            img_tag["src"] = "http://{}/svg/{}.jpg".format(
                proxy_host, svg_hash)
            img_tag["width"] = str(render_w)
            if render_h:
                img_tag["height"] = str(render_h)
            # Mark as already-proxied so step 9 won't re-wrap in /img/
            img_tag["data-svg"] = "1"
            if svg_tag.get("alt"):
                img_tag["alt"] = svg_tag["alt"]
            svg_tag.replace_with(img_tag)
        except Exception:
            # Can't convert — remove the SVG
            svg_tag.decompose()

    # 3. Remove unwanted tags entirely (including <style> now that we parsed it)
    for tag in soup.find_all(DROP_TAGS):
        tag.decompose()

    # 3a. Remove elements with inline display:none — these are hidden
    #     (JS autocomplete, duplicate buttons, overlays, etc.)
    #     Skip void elements (img, br, hr, input…) — html.parser can
    #     misparse self-closing tags like <img … style="display:none"/>
    #     as open containers that swallow all subsequent content.
    _VOID_TAGS = frozenset({"img", "br", "hr", "input", "meta", "link",
                            "area", "base", "col", "embed", "source",
                            "track", "wbr", "param"})
    for el in list(soup.find_all(True, style=True)):
        if el.attrs is None:
            continue
        if el.name in _VOID_TAGS:
            continue
        style_val = el.get("style", "")
        if re.search(r'display\s*:\s*none', style_val, re.I):
            el.decompose()

    # 3c. Remove elements with classes/ids that indicate non-content
    #     (share buttons, ad spaces, popups, AI summaries, overlays)
    #     (_JUNK_CLS_RE is compiled at module scope.)
    for el in list(soup.find_all(True, class_=True)):
        if el.attrs is None:
            continue
        ccls = " ".join(el.get("class", []))
        if _JUNK_CLS_RE.search(ccls):
            el.decompose()

    # 3d-pre. Extract meta/link info BEFORE unwrapping (meta/link are in
    #         UNWRAP_TAGS and will lose their attributes after unwrap).
    _saved_og_site = ""
    _saved_icon_href = ""
    _og_m = soup.find("meta", attrs={"property": "og:site_name"})
    if _og_m:
        _saved_og_site = _og_m.get("content", "").strip()
    for _lnk in soup.find_all("link", rel=True):
        _lrel = " ".join(_lnk.get("rel", []))
        if "icon" in _lrel.lower():
            _saved_icon_href = _lnk.get("href", "")
            if "apple" in _lrel.lower():
                break  # prefer apple-touch-icon

    # 3d. Unwrap tags that may have swallowed article content
    for tag in soup.find_all(UNWRAP_TAGS):
        tag.unwrap()

    # 3c. Remove custom elements (Vue/Angular/Web Components) whose tag
    #     names contain a hyphen — these are never valid HTML 3.2 and
    #     their content is usually JS template placeholders.
    _HTML_TAGS = frozenset({
        "a", "abbr", "address", "area", "article", "aside", "b", "base",
        "bdo", "big", "blockquote", "body", "br", "button", "caption",
        "center", "cite", "code", "col", "colgroup", "dd", "del", "details",
        "dfn", "dir", "div", "dl", "dt", "em", "fieldset", "figcaption",
        "figure", "font", "footer", "form", "frame", "frameset", "h1", "h2", "h3", "h4", "h5",
        "h6", "head", "header", "hr", "html", "i", "iframe", "img", "input",
        "ins", "kbd", "label", "legend", "li", "link", "main", "map", "mark",
        "menu", "meta", "nav", "noscript", "ol", "optgroup", "option", "p",
        "param", "picture", "pre", "q", "s", "samp", "script", "section",
        "select", "small", "source", "span", "strike", "strong", "style",
        "sub", "summary", "sup", "svg", "table", "tbody", "td", "textarea",
        "tfoot", "th", "thead", "time", "title", "tr", "tt", "u", "ul",
        "var", "video", "wbr",
    })
    for tag in list(soup.find_all(True)):
        if tag.attrs is None:
            continue
        # Drop custom elements (tag name with hyphen or not in HTML spec)
        if tag.name and tag.name not in _HTML_TAGS:
            tag.decompose()
            continue
        # Drop elements with JS framework attributes (Vue v-bind/v-if/@,
        # Angular ng-*)
        if any(k.startswith(("v-", "@", "ng-")) for k in tag.attrs):
            tag.decompose()

    # 4. Replace <picture> with its <img> child
    for pic in soup.find_all("picture"):
        img = pic.find("img")
        if img:
            pic.replace_with(img)
        else:
            src_tag = pic.find("source")
            if src_tag:
                raw = src_tag.get("srcset") or src_tag.get("src") or ""
                first = raw.strip().split()[0].rstrip(",")
                if first:
                    new_img = soup.new_tag("img", src=first)
                    pic.replace_with(new_img)
                    continue
            pic.decompose()

    # 5. Isolate page zones and sections into independent tables FIRST,
    #    so that layout conversion inside one section can't affect others
    _structural_table_layout(soup)

    # 6. Convert CSS grid/flex layouts to <table> WITHIN each section
    #    (class attributes still available for lookup)
    _convert_layout_to_tables(soup, css_layouts)

    # 7. Identify main content area (while class/id still exist)
    main_el = _find_main(soup)

    # 7a. Rescue site header logo + title + top nav links.
    #     The site header is normally outside main_el and would be discarded.
    #     Extract the logo image (if any), page title/brand, and primary nav
    #     links, then prepend a compact logo bar into main_el.
    site_header_html = ""
    if isinstance(main_el, Tag):
        body = soup.find("body")
        header_el = (body.find("header") if body else None) or soup.find(
            lambda t: t.name == "div" and _has_class_hint(
                t, ("header", "banner", "masthead", "site-header",
                    "top-bar", "navbar", "head")))
        nav_el_top = (body.find("nav") if body else None) or soup.find(
            lambda t: t.name == "div" and _has_class_hint(
                t, ("nav", "main-nav", "site-nav", "navigation")))
        _header_zone = header_el or nav_el_top
        if _header_zone and main_el not in [_header_zone] + list(
                _header_zone.parents):
            # Collect logo image (first <img> in header)
            logo_img = _header_zone.find("img")
            # Collect brand/site name: the <a> wrapping the logo, or
            # the first short link that looks like a homepage link
            brand_link = None
            if logo_img:
                parent_a = logo_img.find_parent("a")
                if parent_a:
                    brand_link = parent_a
            if not brand_link:
                for a in _header_zone.find_all("a", href=True):
                    href = a.get("href", "")
                    # Homepage links: "/" or "https://site.com/"
                    if href in ("/", page_url) or href.rstrip("/") == \
                            urlparse(page_url).scheme + "://" + \
                            urlparse(page_url).netloc:
                        txt = a.get_text(strip=True)
                        if txt and len(txt) < 60:
                            brand_link = a
                            break
            # Collect top nav links (short text, primary label only).
            # Prefer top-level <li> children of the first <ul> inside
            # the nav — this avoids picking up dropdown sub-items that
            # crowd out the real top-level navigation.
            nav_links = []
            link_source = nav_el_top if nav_el_top and \
                nav_el_top is not _header_zone else _header_zone
            seen_hrefs = set()

            def _pick_link(a):
                if a is brand_link:
                    return None
                full_text = a.get_text(strip=True)
                txt = ""
                for desc in a.descendants:
                    if isinstance(desc, str):
                        t = desc.strip()
                        if t and 2 <= len(t) <= 40:
                            txt = t
                            break
                if not txt:
                    txt = full_text
                if not txt or len(txt) < 2 or len(txt) > 40:
                    return None
                href = a.get("href", "")
                if not href or href.startswith(("#", "javascript:")):
                    return None
                abs_href = _abs(href, page_url)
                if not abs_href or abs_href in seen_hrefs:
                    return None
                seen_hrefs.add(abs_href)
                return (txt, abs_href)

            # First pass: top-level <li> > <a> from the first <ul>
            top_ul = link_source.find("ul")
            if top_ul:
                for li in top_ul.find_all("li", recursive=False):
                    a = li.find("a", href=True)
                    if a:
                        pair = _pick_link(a)
                        if pair:
                            nav_links.append(pair)
                    if len(nav_links) >= 12:
                        break

            # Fallback: deep scan if the top-level approach found < 3
            if len(nav_links) < 3:
                nav_links.clear()
                seen_hrefs.clear()
                for a in link_source.find_all(
                        "a", href=True, recursive=True):
                    pair = _pick_link(a)
                    if pair:
                        nav_links.append(pair)
                    if len(nav_links) >= 12:
                        break
            # Build a compact logo bar if we have something to show
            if logo_img or brand_link or nav_links:
                parts = []
                if logo_img:
                    logo_img.extract()
                    # Already-proxied SVG images (from step 2b) need no
                    # further processing — just keep them as-is.
                    if logo_img.get("data-svg"):
                        del logo_img["data-svg"]
                        logo_img["width"] = "150"
                    else:
                        # Proxy the logo image src
                        raw_src = _real_img_src(logo_img, page_url)
                        if raw_src:
                            logo_img.attrs = {}
                            logo_img["src"] = _proxy_img(raw_src, proxy_host)
                            logo_img["width"] = "150"
                    parts.append(str(logo_img))
                if brand_link:
                    brand_text = brand_link.get_text(strip=True)
                    if brand_text:
                        parts.append(" <b>{}</b>".format(brand_text))
                site_header_html = " ".join(parts)
                if nav_links:
                    nav_cells = []
                    for txt, href in nav_links:
                        phref = _proxy_page(href, proxy_host, cp1256)
                        nav_cells.append(
                            '<td nowrap><font size="2">'
                            '<a href="{}">{}</a>'
                            '</font></td>'.format(phref, txt))
                    site_header_html += (
                        '<br><table border="0" cellpadding="2" '
                        'cellspacing="0"><tr>' +
                        "".join(nav_cells) +
                        '</tr></table>')
                site_header_html = (
                    '<table width="100%" border="0" cellpadding="4" '
                    'cellspacing="0" bgcolor="#eeeeee"><tr><td>'
                    + site_header_html +
                    '</td></tr></table><hr size="1" noshade>'
                )

    # 7a2. Fallback: if no HTML header/nav was found (JS-rendered header),
    #      build a minimal header from meta tags (og:site_name, favicon).
    if not site_header_html and isinstance(main_el, Tag):
        _og_site = _saved_og_site
        # Fallback: extract site name from <title> ("Article - SiteName")
        if not _og_site:
            _ttag = soup.find("title")
            if _ttag:
                _ttext = _ttag.get_text(strip=True)
                _sep = None
                for _s in (" - ", " | ", " – ", " — ", " :: "):
                    if _s in _ttext:
                        _sep = _s
                        break
                if _sep:
                    _og_site = _ttext.rsplit(_sep, 1)[-1].strip()
        _icon_href = _saved_icon_href
        # Build header if we have at least a site name
        if _og_site:
            _fb_parts = []
            if _icon_href:
                _icon_abs = _abs(_icon_href, page_url)
                if _icon_abs:
                    _fb_parts.append(
                        '<img src="{}" width="24" height="24" border="0">'
                        .format(_proxy_img(_icon_abs, proxy_host, 24, 24)))
            _home_url = urlparse(page_url).scheme + "://" + \
                urlparse(page_url).netloc + "/"
            _fb_parts.append(
                '<b><a href="{}">{}</a></b>'.format(
                    _proxy_page(_home_url, proxy_host, cp1256), _og_site))
            # Try to find top-level nav links from the page itself
            _fb_nav = []
            _fb_seen = set()
            _fb_body = soup.find("body")
            # Look for links in list-like structures near the top of the page
            for _fb_a in (soup.find_all("a", href=True) if _fb_body
                          else []):
                _fb_txt = _fb_a.get_text(strip=True)
                _fb_href = _fb_a.get("href", "")
                if not _fb_txt or len(_fb_txt) < 2 or len(_fb_txt) > 30:
                    continue
                if _fb_href.startswith(("#", "javascript:")):
                    continue
                _fb_abs = _abs(_fb_href, page_url)
                if not _fb_abs or _fb_abs in _fb_seen:
                    continue
                # Only include links to the same site
                if urlparse(_fb_abs).netloc != urlparse(page_url).netloc:
                    continue
                # Skip links that look like article/post links (long paths)
                _fb_path = urlparse(_fb_abs).path
                if len(_fb_path) > 30:
                    continue
                _fb_seen.add(_fb_abs)
                _fb_nav.append((_fb_txt, _fb_abs))
                if len(_fb_nav) >= 10:
                    break
            site_header_html = " ".join(_fb_parts)
            if _fb_nav:
                _fb_cells = []
                for _ft, _fh in _fb_nav:
                    _fph = _proxy_page(_fh, proxy_host, cp1256)
                    _fb_cells.append(
                        '<td nowrap><font size="2">'
                        '<a href="{}">{}</a>'
                        '</font></td>'.format(_fph, _ft))
                site_header_html += (
                    '<br><table border="0" cellpadding="2" '
                    'cellspacing="0"><tr>' +
                    "".join(_fb_cells) + '</tr></table>')
            site_header_html = (
                '<table width="100%" border="0" cellpadding="4" '
                'cellspacing="0" bgcolor="#eeeeee"><tr><td>'
                + site_header_html +
                '</td></tr></table><hr size="1" noshade>')

    # 7b. Rescue site search forms that live outside the main content
    #     (e.g. Wikipedia's search box in the header).  Move them into
    #     main_el so they survive content extraction.  Only rescue ONE
    #     search form to avoid duplicates (many sites include the same
    #     search box in both the header and a sidebar/mobile menu).
    if isinstance(main_el, Tag):
        rescued = False
        for form in soup.find_all("form"):
            if rescued:
                break
            # Skip forms already inside main_el
            if main_el in [form] + list(form.parents):
                continue
            # Look for a text/search input — that's a search form
            search_input = form.find("input",
                                     attrs={"type": _SEARCH_INPUT_TYPE_RE})
            if not search_input:
                continue
            name = search_input.get("name", "")
            if not name:
                continue
            # This looks like a site search form — move it into main_el
            form.extract()
            main_el.insert(0, soup.new_tag("hr"))
            main_el.insert(0, form)
            rescued = True

    # 7c. Convert <nav> elements to horizontal single-row tables.
    #     Nav menus should display horizontally, not as a vertical list.
    for nav in soup.find_all("nav"):
        links = nav.find_all("a", href=True)
        if len(links) >= 2:
            tbl = soup.new_tag("table", border="0", cellpadding="4",
                               cellspacing="0")
            tr = soup.new_tag("tr")
            tbl.append(tr)
            for a in links:
                text = a.get_text(strip=True)
                if not text or len(text) > 50:
                    continue
                td = soup.new_tag("td", nowrap="")
                a_copy = a.extract()
                td.append(a_copy)
                tr.append(td)
            if tr.find("td"):
                nav.clear()
                nav.append(tbl)

    # 7d. Convert carousel/slider containers to horizontal tables.
    #     JS carousels (owl-carousel, slick, swiper, etc.) display children
    #     horizontally but without JS they stack vertically.
    #     (_CAROUSEL_CLS_RE is compiled at module scope.)
    for el in list(soup.find_all(True, class_=True)):
        if el.attrs is None:
            continue
        cls = " ".join(el.get("class", []))
        if not _CAROUSEL_CLS_RE.search(cls):
            continue
        # Collect direct children that have content
        items = [c for c in el.find_all(True, recursive=False)
                 if c.get_text(strip=True)]
        if len(items) < 2:
            continue
        tbl = soup.new_tag("table", border="0", cellpadding="4",
                           cellspacing="2")
        tr = soup.new_tag("tr")
        tbl.append(tr)
        for item in items:
            td = soup.new_tag("td", valign="top")
            item.extract()
            td.append(item)
            tr.append(td)
        el.clear()
        el.append(tbl)

    # 8. Remap HTML5 semantic tags to HTML 3.2 equivalents
    for old_name, new_name in REMAP_TAGS.items():
        for tag in soup.find_all(old_name):
            if new_name is None:
                tag.unwrap()
            else:
                tag.name = new_name

    # 8b. Convert <textarea> to <input type="text"> (old browsers handle
    #     them fine and many modern sites use textarea for search fields)
    for ta in soup.find_all("textarea"):
        name = ta.get("name", "")
        if not name:
            ta.decompose()
            continue
        value = ta.get_text(strip=True)
        inp = soup.new_tag("input", type="text")
        inp["name"] = name
        if value:
            inp["value"] = value
        title = ta.get("title")
        if title:
            inp["size"] = "40"
        maxlen = ta.get("maxlength")
        if maxlen:
            inp["maxlength"] = maxlen
        ta.replace_with(inp)

    # 8c. Convert <pre> to <p> when it contains prose (not code).
    #     Many sites misuse <pre> for regular paragraphs, which prevents
    #     word-wrapping and causes horizontal scrolling.
    for pre in soup.find_all("pre"):
        # Keep <pre> if it contains <code> — that's real preformatted code.
        if pre.find("code"):
            continue
        pre.name = "p"

    # 8d. Convert <button> to <input type="submit"> (IE2 and other very
    #     old browsers don't support <button> and render it as plain text).
    for btn in soup.find_all("button"):
        btn_text = btn.get_text(strip=True) or "Submit"
        btn_name = btn.get("name", "")
        btn_value = btn.get("value", "")
        btn_type = btn.get("type", "submit").lower()
        if btn_type == "button":
            # Non-submitting button — useless without JS, remove it
            btn.decompose()
            continue
        # If the button has a name+value that differs from display text,
        # use a hidden input to carry the value and a plain submit for text.
        if btn_name and btn_value and btn_value != btn_text:
            hidden = soup.new_tag("input", type="hidden")
            hidden["name"] = btn_name
            hidden["value"] = btn_value
            btn.insert_before(hidden)
        submit = soup.new_tag("input", type="submit")
        submit["value"] = btn_text
        btn.replace_with(submit)

    # 8e. Remove file upload inputs — they require JS and don't work
    #     through the bridge (e.g. Google Lens image search).
    for finput in soup.find_all("input", attrs={"type": "file"}):
        finput.decompose()

    # 8e2. Convert <input type="range"> to plain text showing its value.
    #      Used e.g. by Saudi Exchange for 52-week range sliders.
    for rinput in soup.find_all("input", attrs={"type": "range"}):
        val = rinput.get("value", "")
        if val:
            rinput.replace_with(val)
        else:
            rinput.decompose()

    # 8f. Remove empty lists (e.g. JS autocomplete placeholders)
    for ul in list(soup.find_all(("ul", "ol"))):
        if not ul.get_text(strip=True):
            ul.decompose()

    # 8f2. Remove <ul> lists that duplicate a nearby <select>'s options.
    #      JS-driven sites (e.g. Saudi Exchange) often have a visible <select>
    #      AND a custom <ul> dropdown with the same items.
    _all_selects = soup.find_all("select")
    if _all_selects:
        _select_opts = set()
        for _sel in _all_selects:
            for _opt in _sel.find_all("option"):
                _ot = _opt.get_text(strip=True)
                if _ot:
                    _select_opts.add(_ot)
        if _select_opts:
            for ul in list(soup.find_all("ul")):
                _li_texts = [li.get_text(strip=True)
                             for li in ul.find_all("li") if li.get_text(strip=True)]
                if len(_li_texts) >= 3 and all(
                        t in _select_opts for t in _li_texts):
                    ul.decompose()

    # 8g. Replace non-renderable Unicode (CJK, Devanagari, Thai, etc.)
    #     with bracketed labels so the page layout doesn't break on IE2.
    _replace_unrenderable_text(soup)

    # 9. Fix <img> sources — proxy and cap width; drop SVGs (unconvertible)
    #    (_JUNK_IMG_RE is compiled at module scope.)
    for img in soup.find_all("img"):
        # Skip SVG images already converted and proxied in step 2b
        if img.get("data-svg"):
            del img["data-svg"]
            continue
        src = _real_img_src(img, page_url)
        alt    = img.get("alt", "")
        width  = img.get("width", "")
        height = img.get("height", "")
        # Extract size from inline style (e.g. style="width:65px; height:65px")
        img_style = img.get("style", "")
        if img_style:
            sw = re.search(r'(?:^|;)\s*width\s*:\s*(\d+)\s*px', img_style)
            sh = re.search(r'(?:^|;)\s*height\s*:\s*(\d+)\s*px',
                           img_style)
            smh = re.search(r'(?:^|;)\s*min-height\s*:\s*(\d+)\s*px',
                            img_style)
            if sw and not width:
                width = sw.group(1)
            if sh and not height:
                height = sh.group(1)
            elif smh and not height:
                height = smh.group(1)
        # CSS stylesheet sizes (e.g. .numbers .image img { width: 40px }
        # or .newsletter .image img { max-width: 180px })
        # These are more specific than Bootstrap utility classes like w-100.
        css_w, css_h = _css_img_size(img, css_img_rules)
        if css_w and not width:
            width = css_w
        if css_h and not height:
            height = css_h
        # Bootstrap w-100: only use as last resort when no other size is known
        if not width and not height:
            img_cls = " ".join(img.get("class", []))
            if "w-100" in img_cls:
                width = str(MAX_IMG_W)
        # URL size hints (e.g. -140x140.webp) represent the intended
        # display size (thumbnail).  They override tag attributes which
        # may contain the raw/original image dimensions.
        url_w = url_h = ""
        if src:
            size_m = re.search(r'[-_/](\d{2,4})x(\d{2,4})(?:\.|$)', src)
            if size_m:
                url_w, url_h = size_m.group(1), size_m.group(2)
        if url_w:
            width = url_w
        if url_h:
            height = url_h
        img.attrs = {}
        if not src:
            img.decompose()
            continue
        # Drop small utility/icon SVGs (close, share, loader, tools logo)
        src_lower = src.rstrip("/").lower()
        if src_lower.endswith(".svg") and _JUNK_IMG_RE.search(src_lower):
            img.decompose()
            continue
        # SVG images: proxy them through the image converter which will
        # rasterize via cairosvg → JPEG (falls back to 1x1 GIF if it fails).
        # SVGs without explicit size are almost always icons — cap to 48px.
        if src_lower.endswith(".svg"):
            if not width and not height:
                width, height = "48", "48"
        if alt:
            img["alt"] = alt
        # Cap avatar/profile images to 36x36 when no size is specified
        # (_AVATAR_RE is compiled at module scope.)
        if not width and not height:
            if _AVATAR_RE.search(src_lower) or _AVATAR_RE.search(alt.lower()):
                width, height = "36", "36"
        # Resolve final pixel values for width/height.
        # Use int(float()) to handle decimal values like "293.33"
        # (int(re.sub("[^0-9]","","293.33")) would give 29333!).
        final_w = final_h = 0
        if width:
            try:
                raw_w = int(float(re.sub(r"[^0-9.]", "", str(width))))
                final_w = min(raw_w, MAX_IMG_W)
                img["width"] = str(final_w)
                if height:
                    try:
                        raw_h = int(float(re.sub(r"[^0-9.]", "",
                                                  str(height))))
                        ratio = final_w / raw_w if raw_w else 1
                        final_h = min(int(raw_h * ratio), MAX_IMG_H)
                        img["height"] = str(final_h)
                    except (ValueError, ZeroDivisionError):
                        pass
            except ValueError:
                pass
        elif height:
            try:
                final_h = min(
                    int(float(re.sub(r"[^0-9.]", "", str(height)))),
                    MAX_IMG_H)
                img["height"] = str(final_h)
            except ValueError:
                pass
        # Pass size hints to image proxy so it pre-resizes (for IE2
        # which ignores HTML width/height attributes).
        img["src"] = _proxy_img(src, proxy_host, final_w, final_h)
        img["border"] = "0"

    # 10. Fix <a> hrefs — route through proxy
    for a in soup.find_all("a"):
        href = a.get("href", "")
        if href.startswith(("mailto:", "tel:")):
            a.attrs = {"href": href}
            continue
        if href.startswith("#"):
            a.attrs = {"href": href}
            continue
        abs_url = _abs(href, page_url)
        if abs_url:
            # Resolve DDG redirect links NOW so the proxy link points
            # directly at the target URL, avoiding issues with &-separated
            # DDG parameters getting mangled by CP-1256 encoding.
            abs_url = _resolve_ddg_redirect(abs_url)
            a.attrs = {"href": _proxy_page(abs_url, proxy_host, cp1256)}
        else:
            a.unwrap()
            continue

    # 10b. Fix <form> actions — route GET/POST forms through proxy
    for form in soup.find_all("form"):
        # Skip forms already created by _convert_dropdowns_to_select —
        # they have action="/get" and a <select name="url">.
        if form.get("action") == "/get" and form.find("select", attrs={"name": "url"}):
            continue
        action = form.get("action", "")
        abs_action = _abs(action, page_url) if action else page_url
        if not abs_action:
            abs_action = page_url
        # Rewrite: action → /get, add hidden field with real target URL.
        # Preserve the original method in a hidden field so the proxy
        # knows to POST to the target even if the browser sends GET.
        orig_method = (form.get("method") or "GET").upper()
        form["action"] = "/get"
        if not form.get("method"):
            form["method"] = "GET"
        # Remove any existing hidden 'url' input to avoid duplicates
        for old_hidden in form.find_all("input", attrs={"name": "url", "type": "hidden"}):
            old_hidden.decompose()
        hidden = soup.new_tag("input", type="hidden")
        hidden["name"] = "url"
        hidden["value"] = abs_action
        form.insert(0, hidden)
        if orig_method == "POST":
            meth_hidden = soup.new_tag("input", type="hidden")
            meth_hidden["name"] = "_proxy_method"
            meth_hidden["value"] = "POST"
            form.insert(1, meth_hidden)
        # Propagate CP-1256 encoding preference through forms
        if cp1256:
            cp_hidden = soup.new_tag("input", type="hidden")
            cp_hidden["name"] = "cp1256"
            cp_hidden["value"] = "1"
            form.insert(1, cp_hidden)

    # 10b2. Convert DuckDuckGo search POST forms to GET for old browser
    #       compatibility (IE3 may not send POST hidden fields reliably).
    #       DDG's HTML version supports both GET and POST.
    _ddg_host = urlparse(page_url).hostname or ""
    if "duckduckgo.com" in _ddg_host:
        for form in list(soup.find_all("form")):
            if (form.get("method") or "").lower() == "post" and \
                    form.find("input", attrs={"name": "q"}):
                form["method"] = "GET"
                # Remove _proxy_method=POST since we want GET
                for _pm in form.find_all("input",
                                         attrs={"name": "_proxy_method"}):
                    _pm.decompose()
            # Remove DDG tracking/state forms that have no search input
            # (they contain visible state_hidden text fields that confuse users)
            elif form.find("input", attrs={"name": "state_hidden"}):
                form.decompose()

        # 10b3. Inject visible DDG logo text with link for empty logo anchors.
        for a in soup.find_all("a", href=True):
            _a_cls = " ".join(a.get("class", []))
            if "logo" in _a_cls.lower() and not a.get_text(strip=True) \
                    and not a.find("img"):
                _logo_name = a.get("title", "") or "DuckDuckGo"
                a.string = ""
                _b = soup.new_tag("b")
                _b.string = _logo_name
                _font = soup.new_tag("font", size="5")
                _font.append(_b)
                a.append(_font)

    # 10c. Fill in empty <a> links that have a title attribute but no
    #      visible text or images (e.g. CSS-background logos like DDG).
    for a in soup.find_all("a", href=True):
        if not a.get_text(strip=True) and not a.find("img"):
            _a_title = a.get("title", "")
            if _a_title:
                a.string = _a_title

    # 10d. Ensure submit buttons have visible text (old browsers won't
    #      render a button with empty value).
    for inp in soup.find_all("input", attrs={"type": "submit"}):
        if not (inp.get("value") or "").strip():
            # Use alt or title attribute, or default to "Go"
            inp["value"] = inp.get("alt") or inp.get("title") or "Go"

    # 11. Strip modern/irrelevant attributes from all tags
    for tag in soup.find_all(True):
        bad_attrs = [a for a in list(tag.attrs) if _STRIP_RE.match(a)]
        for a in bad_attrs:
            del tag[a]

    # 11a. Add visible borders to content tables that lost their CSS styling.
    #      Tables generated by the bridge for layout already have border="0",
    #      so only tables WITHOUT a border attribute (original content tables)
    #      get border="1" for readability.
    for tbl in soup.find_all("table"):
        if not tbl.get("border"):
            tbl["border"] = "1"
            if not tbl.get("cellpadding"):
                tbl["cellpadding"] = "2"
            if not tbl.get("cellspacing"):
                tbl["cellspacing"] = "1"

    # 11a2. Remove duplicate <thead> in tables.  JS frameworks often clone
    #       the header row for a sticky/fixed-header effect.  Keep only the
    #       first <thead> in each table.
    for tbl in soup.find_all("table"):
        theads = tbl.find_all("thead")
        if len(theads) > 1:
            for dup in theads[1:]:
                dup.decompose()

    # 11a3. Linearize multi-column table rows that are too wide for old
    #       screens.  CSS grid/flex conversion may create 3+ column rows
    #       where each cell holds a large image (480px+).  On 640–800px
    #       screens this forces horizontal scrolling.  Convert such rows
    #       into stacked single-column rows.
    for tbl in soup.find_all("table"):
        for tr in list(tbl.find_all("tr", recursive=False)):
            tds = tr.find_all("td", recursive=False)
            if len(tds) < 3:
                continue
            # Check if any cell contains a large image
            has_big_img = False
            for td in tds:
                for img in td.find_all("img"):
                    try:
                        iw = int(img.get("width", 0) or 0)
                    except (ValueError, TypeError):
                        iw = 0
                    if iw >= 300:
                        has_big_img = True
                        break
                if has_big_img:
                    break
            if not has_big_img:
                continue
            # Linearize: convert each td to its own tr
            for td in tds[1:]:
                new_tr = soup.new_tag("tr")
                td.extract()
                td["width"] = ""
                td["colspan"] = str(len(tds))
                new_tr.append(td)
                tr.insert_after(new_tr)
                tr = new_tr  # insert next one after this
            # Fix the first cell
            first_td = tds[0]
            first_td["width"] = ""
            first_td["colspan"] = str(len(tds))

    # 11a4. Convert wide navigation menus to <select> dropdowns.
    #       Detect table rows with many (>6) nowrap cells that contain
    #       only links — these are horizontal nav menus that overflow on
    #       old 640–800px screens.  Replace with a compact <select>+Go.
    _NAV_CELL_LIMIT = 6
    for tbl in list(soup.find_all("table")):
        for tr in list(tbl.find_all("tr", recursive=False)):
            tds = tr.find_all("td", recursive=False)
            if len(tds) <= _NAV_CELL_LIMIT:
                continue
            # Check that most cells are link-only (nav pattern)
            nav_links = []
            is_nav = True
            for td in tds:
                a = td.find("a")
                if not a:
                    is_nav = False
                    break
                text = td.get_text(strip=True)
                link_text = a.get_text(strip=True)
                # Cell should contain mostly just the link text
                if text and link_text and len(text) <= len(link_text) + 5:
                    nav_links.append((link_text, a.get("href", "")))
                else:
                    is_nav = False
                    break
            if not is_nav or len(nav_links) <= _NAV_CELL_LIMIT:
                continue
            # Build <select> dropdown + Go button
            select = soup.new_tag("select", attrs={"name": "url"})
            select.append(soup.new_tag("option", value="",
                                       string="-- navigate --"))
            seen = set()
            for text, href in nav_links:
                if href in seen:
                    continue
                seen.add(href)
                opt = soup.new_tag("option", value=href)
                opt.string = text
                select.append(opt)
            form = soup.new_tag("form", method="GET", action="/get")
            font = soup.new_tag("font", size="2",
                                face="Arial,Helvetica,sans-serif")
            font.append(select)
            font.append(soup.new_tag("input", attrs={"type": "submit",
                                                      "value": " Go "}))
            form.append(font)
            # Replace the entire table (or just the row) with the dropdown
            # If the table has only this one row, replace the whole table
            all_rows = tbl.find_all("tr", recursive=False)
            if len(all_rows) == 1:
                tbl.replace_with(form)
            else:
                # Replace just this row's cells with a single spanning cell
                new_td = soup.new_tag("td", colspan=str(len(tds)))
                new_td.append(form)
                tr.clear()
                tr.append(new_td)

    # 11b. Replace <div> with HTML 3.2 equivalents.
    #      IE2 does not understand <div> and renders its content inline,
    #      causing everything to flow as one long horizontal line.
    #      Strategy: unwrap every <div>, inserting a <br> before it to
    #      preserve the visual line break a block element would create.
    _BLOCK_TAGS = frozenset({"p", "table", "tr", "td", "th", "ul", "ol",
                             "li", "h1", "h2", "h3", "h4", "h5", "h6",
                             "hr", "br", "blockquote", "form", "center"})
    for div in list(soup.find_all("div")):
        # Never unwrap the element selected as main content — its children
        # would scatter into the parent and decode_contents() would be empty.
        if div is main_el:
            continue
        # If the div's previous sibling is already a block element or <br>,
        # no extra <br> is needed.
        prev = div.previous_sibling
        while prev and isinstance(prev, NavigableString) and not prev.strip():
            prev = prev.previous_sibling
        needs_br = prev is not None and (
            not hasattr(prev, "name") or prev.name not in _BLOCK_TAGS
        )
        if needs_br:
            div.insert_before(soup.new_tag("br"))
        div.unwrap()

    # Also replace <span> — IE2 may not understand it either; unwrap cleanly.
    for span in list(soup.find_all("span")):
        span.unwrap()

    # 11c. (moved to post-processing step 15b on rendered HTML)

    # 12. Extract title
    title_tag = soup.find("title")
    title = title_tag.get_text(" ", strip=True) if title_tag else page_url

    # 13. Render main content (prepend site header/logo bar if rescued)
    if isinstance(main_el, Tag):
        content_html = site_header_html + main_el.decode_contents()
    else:
        content_html = site_header_html + str(main_el)

    # 13b. If the page is JS-rendered, fall back to embedded article data.
    #      Indicators: "undefined" placeholder text, or the embedded data
    #      is significantly longer than what the static HTML provides.
    js_only = False
    plain = re.sub(r"<[^>]+>", "", content_html)
    jsonld_plain = re.sub(r"<[^>]+>", "", jsonld_fallback or "")
    readability_plain = re.sub(r"<[^>]+>", "", readability_fallback or "")

    def _choose_richer(current_html, current_plain, cand_html, cand_plain,
                       min_len=500, ratio=2):
        """Return (html, plain) — swap to candidate when it is clearly
        richer than what we already have."""
        if (cand_html and len(cand_plain) > min_len
                and len(cand_plain) > len(current_plain) * ratio):
            return cand_html, cand_plain
        return current_html, current_plain

    if "undefined" in plain:
        # SPA stub — pick whichever embedded source we have
        js_only = True
        if jsonld_fallback:
            content_html, plain = jsonld_fallback, jsonld_plain
        if readability_fallback and len(readability_plain) > len(plain):
            content_html, plain = readability_fallback, readability_plain
    else:
        # JSON-LD / Apollo override (existing behaviour)
        new_html, new_plain = _choose_richer(
            content_html, plain, jsonld_fallback, jsonld_plain)
        if new_html is not content_html:
            js_only = True
            content_html, plain = new_html, new_plain
        # Readability override — weaker threshold (1.5x) since Readability
        # output is already clean and usually more focused than _find_main's
        # heuristic match.
        new_html, new_plain = _choose_richer(
            content_html, plain, readability_fallback, readability_plain,
            min_len=400, ratio=1.5)
        if new_html is not content_html:
            content_html, plain = new_html, new_plain

    # 14. Unescape &amp; inside href/src attributes.  BeautifulSoup encodes
    #     & → &amp; in attribute values, but very old browsers (IE2–IE5)
    #     may not decode &amp; back to & when following links.
    content_html = re.sub(
        r'(href|src|action)="([^"]*)"',
        lambda m: '{}="{}"'.format(m.group(1), m.group(2).replace("&amp;", "&")),
        content_html
    )

    # 15. Replace XHTML self-closing tags with HTML 3.2 form.
    #     BeautifulSoup renders void elements as <br/>, <hr/>, <img .../>
    #     but old browsers don't understand the /> syntax.
    content_html = content_html.replace("<br/>", "<br>")
    content_html = content_html.replace("<hr/>", "<hr>")
    content_html = re.sub(r"<(img\s[^>]*?)\s*/>", r"<\1>", content_html)

    # 15a2. Strip portal framework junk text (e.g. IBM WebSphere Portal).
    for _junk in ("LoginPortletPopupv2", "NO PORTLET SESSION YET",
                   "AddtoWatchlistv2", "MarketWatchTodaywatchV2Portlet"):
        content_html = content_html.replace(_junk, "")
    # Remove every standalone "التصرفات" / "Actions" that appears as
    # meaningless repeated text on Saudi Exchange portal pages.
    content_html = re.sub(
        r'[\u200f\s]*\u0627\u0644\u062a\u0635\u0631\u0641\u0627\u062a[\u200f\s]*',
        '', content_html)
    # Remove bare "Actions" portal placeholders (WebSphere Portal junk).
    # They appear as standalone text between empty block tags.
    content_html = re.sub(r'(?<=>)\s*Actions\s*(?=<)', '', content_html)

    # 15b. Convert runs of consecutive <a> links separated by <br> into
    #      horizontal table rows.  Nav menus become vertical after div
    #      unwrapping — render them side by side in a single-row table.
    #      (_LINK_RE / _BR_RE are compiled at module scope.)
    def _linearize_nav(html):
        """Find runs of 3+ links separated by <br> and wrap in a table."""
        result = []
        pos = 0
        while pos < len(html):
            m = _LINK_RE.search(html, pos)
            if not m:
                result.append(html[pos:])
                break
            # Try to collect a run of links from this point
            run_start = m.start()
            links = [m.group(0)]
            end = m.end()
            while True:
                br_m = _BR_RE.match(html, end)
                if not br_m:
                    break
                next_a = _LINK_RE.match(html, br_m.end())
                if not next_a:
                    break
                links.append(next_a.group(0))
                end = next_a.end()
            if len(links) >= 3:
                # Only convert if links look like a nav menu: all short
                # text, and average length ≤ 20 chars (nav labels are
                # brief; product titles or listing items are longer).
                texts = []
                all_short = True
                for lnk in links:
                    txt = re.sub(r'<[^>]+>', '', lnk).strip()
                    if len(txt) > 40:
                        all_short = False
                        break
                    texts.append(txt)
                avg_len = sum(len(t) for t in texts) / len(texts) if texts else 99
                if all_short and avg_len <= 20:
                    result.append(html[pos:run_start])
                    cells = ''.join(
                        '<td nowrap>{}</td>'.format(l) for l in links)
                    result.append(
                        '<table border="0" cellpadding="3" '
                        'cellspacing="0"><tr>{}</tr></table>'.format(cells))
                    pos = end
                    continue
            result.append(html[pos:m.end()])
            pos = m.end()
        return ''.join(result)
    content_html = _linearize_nav(content_html)

    # 16. Append a warning if the page required JavaScript to render.
    if js_only:
        content_html += (
            '<hr><p><font face="Arial,Helvetica" size="2" color="#cc0000">'
            '<b>Note:</b> This page requires JavaScript to display its full '
            'content. The text shown above may be incomplete or a summary only.'
            '</font></p>'
        )

    return title, content_html, is_rtl, js_only, body_bg_img, body_bgcolor, body_attrs


# ── Landing page HTML ──────────────────────────────────────────────────────

def _history_options(ip):
    """Return <option> HTML for recent URLs, or empty string if none."""
    urls = _get_history(ip).recent(10)
    if not urls:
        return ""
    opts = []
    for u in urls:
        safe = u.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
        label = u.replace("https://", "").replace("http://", "")
        if len(label) > 55:
            label = label[:52] + "..."
        label = label.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
        opts.append('<option value="{}">{}</option>'.format(safe, label))
    return "".join(opts)


def _detect_legacy_os(user_agent):
    """Detect if the client is running a legacy OS that needs CP-1256.
    Returns a short OS name string or empty string if not legacy."""
    if not user_agent:
        return ""
    ua = user_agent.lower()
    if "win16" in ua or "windows 3." in ua:
        return "Windows 3.x"
    if "windows 95" in ua or "win95" in ua:
        return "Windows 95"
    if "windows 98" in ua or "win98" in ua:
        return "Windows 98"
    if "windows nt 4" in ua:
        return "Windows NT 4.0"
    if "windows nt 5.0" in ua or "windows 2000" in ua:
        return "Windows 2000"
    if "windows ce" in ua:
        return "Windows CE"
    if "mac_powerpc" in ua or "macintosh" in ua:
        # Classic Mac OS (pre-OS X) or early OS X
        if "os x" not in ua and "macos" not in ua:
            return "Mac OS Classic"
    return ""


def _is_arabic_page(url):
    """Heuristic: does the URL likely serve Arabic content?"""
    u = url.lower()
    # Arabic TLDs / known Arabic sites
    arabic_hints = (
        ".sa/", ".sa?", ".ae/", ".ae?", ".eg/", ".eg?",
        ".kw/", ".kw?", ".qa/", ".qa?", ".bh/", ".bh?",
        ".om/", ".om?", ".jo/", ".jo?", ".lb/", ".lb?",
        ".iq/", ".iq?", ".sy/", ".sy?", ".ps/", ".ps?",
        ".ly/", ".ly?", ".tn/", ".tn?", ".dz/", ".dz?",
        ".ma/", ".ma?", ".sd/", ".sd?", ".ye/", ".ye?",
        "/ar/", "/ar?", "/arabic", "arabic.",
        "aljazeera", "alarabiya", "bbc.com/arabic",
        "alburaq", "misbar",
    )
    for hint in arabic_hints:
        if hint in u:
            return True
    # Check for Arabic percent-encoded chars (%D8, %D9 are Arabic UTF-8 lead bytes)
    if "%d8" in u or "%d9" in u or "%D8" in u or "%D9" in u:
        return True
    return False


# ── Landing page logo (generated once at startup) ────────────────────────
_LOGO_GIF = b""  # populated by _generate_logo()

def _generate_logo():
    """Load the logo GIF from disk (wb-logo.gif next to this script)."""
    global _LOGO_GIF
    try:
        import os as _los
        _logo_path = _los.path.join(
            _los.path.dirname(_los.path.abspath(__file__)), "wb-logo.gif")
        with open(_logo_path, "rb") as _f:
            _LOGO_GIF = _f.read()

        buf = _lio.BytesIO()
        img.save(buf, "GIF", optimize=True)
        _LOGO_GIF = buf.getvalue()
        print("  Logo   : generated ({} bytes)".format(len(_LOGO_GIF)))
    except Exception as exc:
        print("  Logo   : generation failed ({})".format(exc))


def _landing_html(ip, user_agent=""):
    legacy_os = _detect_legacy_os(user_agent)
    hist_opts = _history_options(ip)
    hist_html = ""
    if hist_opts:
        hist_html = (
            '  <tr>\n'
            '    <td><font face="Arial,Helvetica" size="2"><b>History:</b></font></td>\n'
            '    <td><select name="hist"><option value="">-- choose --</option>'
            '{}</select></td>\n'
            '  </tr>\n'.format(hist_opts)
        )
    meta_charset = ""
    cp1256_hidden = ""
    if legacy_os == "Windows 3.x":
        # IE5 on Windows 3.x shows blank pages with Arabic default encoding;
        # force Western charset to work around the bug.
        meta_charset = \
            '\n<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">'
    elif legacy_os:
        meta_charset = \
            '\n<meta http-equiv="Content-Type" content="text/html; charset=windows-1256">'
        cp1256_hidden = '\n  <input type="hidden" name="cp1256" value="1">'
    arabic_warning = ""
    return """\
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">
<html>
<head><title>Web Bridge for Old Browsers</title>{meta_charset}</head>
<body bgcolor="#d1d1d1" text="#000000" link="#000080" vlink="#800080">
<br><br>
<center>
<table width="440" border="2" cellpadding="0" cellspacing="2">
<tr><td>
<table width="100%" border="0">
<tr><td align="center" bgcolor="#d1d1d1">
  <img src="/wb-logo.gif" width="440" height="110" border="0"
       alt="Web Bridge for classic browsers">
</td></tr>
<tr><td bgcolor="#ffffff">
  <br>
  <font face="Arial,Helvetica,sans-serif" size="2">
    Search for something, type a web address, or pick from the history list.
    <br><br>
  </font>
  <form method="GET" action="/get">
  <input type="hidden" name="typed" value="1">{cp1256_hidden}
  <table border="0" cellpadding="4" cellspacing="0">
  <tr>
    <td><font face="Arial,Helvetica" size="2"><b>Navigate:</b></font></td>
    <td><input type="text" name="url" size="50" value=""></td>
  </tr>
{hist_html}\
  <tr>
    <td colspan="2" align="right">
      <input type="submit" value="  Go  ">
    </td>
  </tr>
  </table>
  </form>
</td></tr>
</table>
</td></tr>
</table>
<br>
<font face="Arial,Helvetica,sans-serif" size="2" color="#666666">
  Strips JavaScript * CSS * Video * SVG * Modern layout
  -- Returns HTML&nbsp;3.2
</font>
<br><br>
<table width="440" border="0" cellpadding="6" cellspacing="0">
<tr><td>
<font face="Arial,Helvetica,sans-serif" size="1" color="#cc0000">
  <b>Warning:</b> This web bridge fetches and processes remote web pages
  on your behalf.  Do not expose it to the open internet --
  it could be abused to access internal networks, consume
  excessive bandwidth and CPU, or relay malicious content.
  Run it only on a trusted local network.
</font>
</td></tr>
</table>
{arabic_warning}
</center>
</body>
</html>
""".format(meta_charset=meta_charset, cp1256_hidden=cp1256_hidden,
           hist_html=hist_html, arabic_warning=arabic_warning)


# ── Error page ─────────────────────────────────────────────────────────────

def _error_page(title, message):
    return ("""\
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">
<html><head><title>Error — Web Bridge</title></head>
<body bgcolor="#ffffff" text="#000000">
<table width="95%" border="0" cellpadding="10" align="center">
<tr><td>
<h2><font face="Arial,Helvetica" color="#cc0000">""" + title + """</font></h2>
<p><font face="Arial,Helvetica" size="2">""" + message + """</font></p>
<p><a href="/">[ Back to Home ]</a></p>
</td></tr>
</table>
</body></html>
""").encode("utf-8", errors="replace")


# ── Page shell ─────────────────────────────────────────────────────────────

def _page_shell(title, current_url, content_html, proxy_host,
                is_rtl=False, cp1256=False, client_ip="",
                body_bg_img=None, body_bgcolor=None, body_attrs=None,
                client_ua=""):
    escaped_url = current_url.replace('"', "%22").replace("'", "%27")
    safe_title = (
        title
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    if is_rtl:
        html_dir    = ' dir="rtl"'
        body_dir    = ' dir="rtl"'
        content_dir = ' dir="rtl" align="right"'
    else:
        html_dir    = ''
        body_dir    = ''
        content_dir = ''
    hist_opts = _history_options(client_ip)
    hist_select = ""
    if hist_opts:
        hist_select = (
            ' <select name="hist"><option value="">recent...</option>'
            '{}</select>'.format(hist_opts)
        )
    cp_checked = " checked" if cp1256 else ""
    meta_charset = ""
    _is_win3x = _detect_legacy_os(client_ua) == "Windows 3.x"
    if cp1256 and not _is_win3x:
        meta_charset = '\n    <meta http-equiv="Content-Type" content="text/html; charset=windows-1256">'
    elif _is_win3x:
        # IE5 on Windows 3.x shows blank pages with Arabic/non-Western
        # default encoding; force Western charset to work around the bug.
        meta_charset = '\n    <meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">'
    return """\
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">
<html{html_dir}>
<head>
<title>{title} -- Web Bridge</title>{meta_charset}
</head>
<body bgcolor="{body_bgcolor}" text="{body_text}" link="{body_link}" vlink="{body_vlink}" alink="{body_alink}" topmargin="0" marginheight="0"{body_bg}{body_dir}>

<table width="100%" border="0" cellpadding="3" cellspacing="0" bgcolor="#c0c0c0" dir="ltr">
<tr>
<td>
  <form method="GET" action="/get" style="margin: 0;">
  <input type="hidden" name="typed" value="1">
  <input type="text" name="url" value="{url}" size="40" dir="ltr">{hist_select}
  <input type="submit" value="Web Bridge">
  <font face="Arial,Helvetica" size="1">
  <input type="checkbox" name="cp1256" value="1"{cp_checked}> CP-1256</font>
  </form>
</td>
<td nowrap>
  <form method="GET" action="/screenshot" style="margin: 0;">
  <input type="hidden" name="url" value="{url}">
  <font face="Arial,Helvetica" size="1">
  <select name="res">
  <option value="">Screenshot...</option>
  <option value="640x480">640x480</option>
  <option value="800x600">800x600</option>
  <option value="1024x768">1024x768</option>
  <option value="1280x1024">1280x1024</option>
  <option value="1600x1200">1600x1200</option>
  </select>
  <input type="submit" value="&gt;">
  </font>
  </form>
</td>
<td align="right" valign="top" nowrap>
  &nbsp;
  <a href="{reader_href}"><font face="Arial,Helvetica" size="1">[ Reader ]</font></a>
  &nbsp;
  <a href="http://{proxy_host}/"><font face="Arial,Helvetica" size="1">[ Home ]</font></a>
  &nbsp;
</td>
</tr>
</table>


{content}

</body>
</html>
""".format(title=safe_title, url=escaped_url, content=content_html,
           html_dir=html_dir, body_dir=body_dir,
           body_bgcolor=body_bgcolor or "#ffffff",
           body_text=(body_attrs or {}).get("text", "#000000"),
           body_link=(body_attrs or {}).get("link", "#0000cc"),
           body_vlink=(body_attrs or {}).get("vlink", "#551a8b"),
           body_alink=(body_attrs or {}).get("alink", "#ff0000"),
           body_bg=' background="{}"'.format(body_bg_img) if body_bg_img else "",
           hist_select=hist_select, cp_checked=cp_checked,
           proxy_host=proxy_host, meta_charset=meta_charset,
           reader_href="/{}/{}".format("r1" if cp1256 else "r",
                                        unquote(current_url)))


# ── Screenshot ─────────────────────────────────────────────────────────────

def _take_screenshot(url, width=SCREENSHOT_W, height=SCREENSHOT_H):
    """Launch headless Chromium, navigate to url, return JPEG bytes."""
    opts = ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size={},{}".format(width, height))

    if HAS_WDM:
        service = ChromeService(ChromeDriverManager(
            chrome_type=ChromeType.CHROMIUM).install())
    else:
        service = ChromeService()          # rely on chromedriver in PATH

    driver = webdriver.Chrome(service=service, options=opts)
    try:
        driver.set_page_load_timeout(FETCH_TIMEOUT)
        driver.get(url)
        png_bytes = driver.get_screenshot_as_png()
    finally:
        driver.quit()

    # Convert PNG → JPEG (smaller, universally supported by old browsers)
    img = Image.open(io.BytesIO(png_bytes))
    img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=SCREENSHOT_QUALITY)
    return buf.getvalue()


# ── Image proxy ────────────────────────────────────────────────────────────

def _fetch_and_convert_image(url, target_w=0, target_h=0):
    """
    Fetch an image URL, resize and convert to JPEG via Pillow.
    SVGs are rasterized if cairosvg is available, otherwise skipped.
    If target_w/target_h are given, the image is pre-resized to those
    dimensions (for IE2 which ignores HTML width/height attributes).
    Returns (bytes, content_type) or raises on failure.
    """
    resp = _session.get(
        url, headers=_fetch_headers_for(url), timeout=FETCH_TIMEOUT, stream=True
    )
    resp.raise_for_status()
    raw = resp.content
    ctype = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()

    # SVG: old browsers can't render these at all
    is_svg = "svg" in ctype or url.rstrip("/").lower().endswith(".svg")
    if is_svg:
        # Try cairosvg → PNG → JPEG
        try:
            import cairosvg
            # Use target width for rasterization if available
            render_w = target_w if target_w else 200
            png_data = cairosvg.svg2png(bytestring=raw,
                                         output_width=render_w)
            if HAS_PIL:
                img = Image.open(io.BytesIO(png_data))
                # Composite onto white background (SVGs often have transparency)
                _has_alpha = (img.mode in ("RGBA", "LA", "PA")
                              or (img.mode == "P"
                                  and "transparency" in img.info))
                if _has_alpha:
                    img = img.convert("RGBA")
                    bg = Image.new("RGB", img.size, (255, 255, 255))
                    bg.paste(img, mask=img.split()[3])
                    img = bg
                elif img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                # Apply target size if specified
                if target_w or target_h:
                    img = _resize_to_target(img, target_w, target_h)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75)
                return buf.getvalue(), "image/jpeg"
            return png_data, "image/png"
        except Exception:
            # Can't convert SVG — return empty (will show 1x1 GIF fallback)
            raise ValueError("SVG cannot be converted")

    # GIF: natively supported by all old browsers — pass through as-is
    is_gif = "gif" in ctype or url.rstrip("/").lower().endswith(".gif")
    if is_gif:
        return raw, "image/gif"

    if not HAS_PIL:
        return raw, ctype

    try:
        img = Image.open(io.BytesIO(raw))
        _has_alpha = (img.mode in ("RGBA", "LA", "PA")
                      or (img.mode == "P" and "transparency" in img.info))
        if _has_alpha:
            # Composite onto white background to avoid black areas in JPEG
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        # Apply target size from HTML (pre-resize for IE2 compatibility)
        if target_w or target_h:
            img = _resize_to_target(img, target_w, target_h)
        elif img.width > MAX_IMG_W or img.height > MAX_IMG_H:
            img.thumbnail((MAX_IMG_W, MAX_IMG_H), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return raw, ctype


def _resize_to_target(img, target_w, target_h):
    """Resize a PIL Image to target dimensions, preserving aspect ratio.
    If only one dimension is given, the other is calculated from the
    image's natural aspect ratio."""
    orig_w, orig_h = img.size
    if target_w and target_h:
        w, h = target_w, target_h
    elif target_w:
        ratio = target_w / orig_w if orig_w else 1
        w, h = target_w, max(1, int(orig_h * ratio))
    elif target_h:
        ratio = target_h / orig_h if orig_h else 1
        w, h = max(1, int(orig_w * ratio)), target_h
    else:
        return img
    w = min(w, MAX_IMG_W)
    h = min(h, MAX_IMG_H)
    if w >= orig_w and h >= orig_h:
        return img  # don't upscale
    img.thumbnail((w, h), Image.LANCZOS)
    return img


# ── Search engine helpers ─────────────────────────────────────────────────

_GOOGLE_SEARCH_RE = re.compile(
    r'^https?://(?:www\.)?google\.[a-z.]+/search\b'
)
_DDG_REDIRECT_RE = re.compile(
    r'^https?://duckduckgo\.com/l/\?'
)

def _google_to_ddg(url):
    """
    Google Search requires JavaScript and won't serve HTML results.
    Redirect to DuckDuckGo's HTML-only endpoint, preserving the query.
    """
    if not _GOOGLE_SEARCH_RE.match(url):
        return url
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    query = qs.get("q", [""])[0]
    if not query:
        return url
    return "https://html.duckduckgo.com/html/?q=" + quote(query)


def _resolve_ddg_redirect(url):
    """
    DuckDuckGo result links go through /l/?uddg=<encoded_url>&rut=...
    which is a JS redirect that returns 400 if fetched directly.
    Extract the target URL from the uddg parameter.
    """
    if not _DDG_REDIRECT_RE.match(url):
        return url
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    target = qs.get("uddg", [""])[0]
    return target if target else url


# ── Page-fetch heuristics (compiled once at import) ───────────────────────

# Captcha/WAF challenge pages: only the first 10 KB is searched, since
# real challenge pages are tiny (<10 KB) and a recaptcha widget embedded
# deep in a real article should not trigger this.
_CAPTCHA_RE = re.compile(
    rb"captcha-delivery\.com|datadome|challenge-platform|turnstile|"
    rb"cf-challenge|h-captcha\.com|hcaptcha\.com|awswaf|challenge\.js|"
    rb"g-recaptcha|recaptcha/api|captcha_challenge", re.I)

# "JavaScript is required / disabled" notices that mean the page is a
# JS shell — search the whole page (may be buried deep in SPAs).
_JS_DISABLED_RE = re.compile(
    rb"javascript is not available|javascript is disabled|"
    rb"javascript is required|please enable javascript|"
    rb"you need to enable javascript|enable js and disable|"
    rb"please turn on javascript", re.I)

# Wider net for the headless-render fallback (used when the page is
# already known to be tiny and JS-only).
_JS_ONLY_RE = re.compile(
    rb"javascript is not available|javascript is disabled|"
    rb"javascript is required|please enable javascript|"
    rb"you need to enable javascript|enable js and disable|"
    rb"please turn on javascript|enable js|enable javascript|"
    rb"javascript required", re.I)

_HAS_SCRIPT_RE = re.compile(rb"<script\b", re.I)
_BODY_OPEN_RE = re.compile(rb"<body\b", re.I)
_SCRIPT_STYLE_BLOCK_RE = re.compile(
    rb"<(script|style)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_TAG_RE = re.compile(rb"<[^>]+>")


def _approx_body_text_len(raw, scan_limit=200000):
    """Cheap byte-level approximation of len(soup.find('body').get_text()).

    Replaces a per-request BeautifulSoup parse used only to decide whether
    a page is a JS-rendered SPA stub (very little visible text).  On a
    Pi-class host this runs ~10–30× faster than parsing the same chunk
    with html.parser, because we do tag stripping with two compiled
    regexes and length counting via str.split() in C."""
    body_idx = _BODY_OPEN_RE.search(raw, 0, 100000)
    start = body_idx.start() if body_idx else 0
    chunk = raw[start:start + scan_limit]
    chunk = _SCRIPT_STYLE_BLOCK_RE.sub(b" ", chunk)
    chunk = _TAG_RE.sub(b" ", chunk)
    # Whitespace-collapsed length is a good proxy for visible text length.
    return sum(len(w) for w in chunk.split())


# ── HTTP handler ───────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print("  {}  {}".format(self.address_string(), fmt % args))

    def log_error(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        # CP-1256 form encoding: when a page was served as CP-1256, the
        # browser encodes ALL form submissions (GET query strings AND POST
        # bodies) in CP-1256.  parse_qs defaults to UTF-8, which turns
        # the CP-1256 bytes into U+FFFD replacement characters.
        # Detect cp1256=1 in the raw query and re-parse if needed.
        # Also detect legacy OS browsers that may send CP-1256 without
        # the explicit flag (e.g. YouTube search form on auto-detected
        # CP-1256 pages).
        _force_cp1256 = "cp1256=1" in parsed.query
        if not _force_cp1256:
            client_ua = self.headers.get("User-Agent", "")
            if _detect_legacy_os(client_ua):
                # Check if the query string has non-UTF-8 percent-encoded
                # bytes (high bytes 0x80-0xFF that don't form valid UTF-8)
                try:
                    urllib.parse.unquote(parsed.query, encoding="utf-8",
                                        errors="strict")
                except (UnicodeDecodeError, ValueError):
                    _force_cp1256 = True
        if _force_cp1256:
            params = urllib.parse.parse_qs(parsed.query, encoding="cp1256")

        # For POST requests, merge body parameters into params
        if self.command == "POST":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw_body = self.rfile.read(length)
                # If cp1256 mode is active or Content-Type says so,
                # decode body as CP-1256.  Check both the query string
                # AND the raw POST body for cp1256=1 (the hidden field
                # may be in the body when the form method is POST).
                is_cp1256 = (b"cp1256=1" in raw_body
                             or "cp1256=1" in parsed.query)
                # The POST body is ASCII with percent-encoded values.
                # parse_qs URL-decodes %XX sequences and interprets the
                # resulting bytes using its encoding parameter.  When
                # CP-1256 is active the browser percent-encodes CP-1256
                # bytes, so we must tell parse_qs to decode them as
                # CP-1256, not UTF-8.
                body = raw_body.decode("ascii", errors="replace")
                if is_cp1256:
                    post_params = urllib.parse.parse_qs(body, encoding="cp1256")
                else:
                    post_params = urllib.parse.parse_qs(body)
                for k, v in post_params.items():
                    if k in params:
                        params[k].extend(v)
                    else:
                        params[k] = v
            except Exception:
                pass

        # Old browsers (HTTP/1.0) may not send a Host header.
        # Fall back to the server's detected LAN IP, not localhost.
        proxy_host = self.headers.get(
            "Host", "{}:{}".format(SERVER_IP, PORT))

        if path == "/":
            client_ua = self.headers.get("User-Agent", "")
            landing = _landing_html(self.client_address[0], client_ua)
            if _detect_legacy_os(client_ua):
                self._send(200, "text/html; charset=windows-1256",
                           landing.encode("cp1256", errors="xmlcharrefreplace"))
            else:
                self._send(200, "text/html; charset=utf-8",
                           landing.encode("utf-8"))

        elif path == "/wb-logo.gif":
            if _LOGO_GIF:
                self._send(200, "image/gif", _LOGO_GIF)
            else:
                self._send(404, "text/plain", b"logo not available")

        elif path == "/get":
            # /get?url=... — used by the address bar form and history
            hist = params.get("hist", [""])[0].strip()
            url  = params.get("url", [""])[0].strip()
            from_history = False
            if hist:
                url = hist
                from_history = True
            if not url:
                self._send(302, location="/")
                return
            # Forward extra params (from proxied forms) to the target URL.
            # If the request was a POST, send them as POST data to the
            # target (the original form used method=POST for a reason).
            # If GET, append them as query parameters.
            extra = {k: v for k, v in params.items()
                     if k not in ("url", "hist", "typed", "cp1256",
                                  "_proxy_method", "submit")}
            post_data = None
            orig_method = params.get("_proxy_method", [""])[0].upper()
            if (orig_method == "POST" or self.command == "POST") and extra:
                # Pass as dict so requests sets Content-Type correctly
                post_data = {k: v[0] if len(v) == 1 else v
                             for k, v in extra.items()}
            elif extra:
                sep = "&" if "?" in url else "?"
                url = url + sep + urllib.parse.urlencode(extra, doseq=True)
            is_search = False
            if not url.startswith(("http://", "https://")):
                # If it doesn't look like a domain, search DuckDuckGo
                if not re.match(r'^[A-Za-z0-9\u0600-\u06FF]'
                                r'[A-Za-z0-9.\u0600-\u06FF-]*'
                                r'\.[A-Za-z]{2,}', url.split("/")[0].split(":")[0]):
                    url = ("https://html.duckduckgo.com/html/?q="
                           + quote(url))
                    is_search = True
                else:
                    url = "https://" + url
            # Google Search is entirely JS-rendered; redirect to DuckDuckGo HTML
            url = _google_to_ddg(url)
            url = _resolve_ddg_redirect(url)
            # Only remember valid URLs typed in the address bar, not searches
            typed = params.get("typed", [""])[0]
            if typed == "1" and not from_history and not is_search:
                _get_history(self.client_address[0]).add(url)
            # CP-1256: explicit checkbox, forced by encoding detection,
            # or auto-detect (legacy OS + Arabic URL)
            use_cp1256 = (params.get("cp1256", [""])[0] == "1"
                          or _force_cp1256)
            if not use_cp1256:
                client_ua = self.headers.get("User-Agent", "")
                if _detect_legacy_os(client_ua) and _is_arabic_page(url):
                    use_cp1256 = True
            self._serve_page(url, proxy_host, use_cp1256,
                             post_data=post_data)

        elif path.startswith("/r/") or path.startswith("/r1/"):
            # /r/http://…  — Reader mode: force Mozilla Readability extraction
            # /r1/http://… — Reader mode, CP-1256 encoding
            if path.startswith("/r1/"):
                url = self.path[4:]
                use_cp1256 = True
                try:
                    url = url.encode("latin-1").decode("cp1256")
                except (UnicodeDecodeError, UnicodeEncodeError):
                    pass
            else:
                url = self.path[3:]
                use_cp1256 = False
                client_ua = self.headers.get("User-Agent", "")
                if _detect_legacy_os(client_ua) and _is_arabic_page(url):
                    use_cp1256 = True
            if not url:
                self._send(302, location="/")
                return
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            url = _google_to_ddg(url)
            url = _resolve_ddg_redirect(url)
            self._serve_page(url, proxy_host, use_cp1256, reader=True)

        elif path.startswith("/p/") or path.startswith("/p1/"):
            # /p/http://…  — path-based proxy link (no %-encoding)
            # /p1/http://… — same but with CP-1256 mode enabled
            if path.startswith("/p1/"):
                url = self.path[4:]      # everything after "/p1/"
                use_cp1256 = True
                # The page was served as CP-1256, so the browser sends
                # Arabic chars as CP-1256 bytes.  Python's HTTP server
                # decodes the request line as Latin-1, so we must
                # reverse that: encode back to Latin-1 (raw bytes),
                # then decode as CP-1256 to recover proper Unicode.
                try:
                    url = url.encode("latin-1").decode("cp1256")
                except (UnicodeDecodeError, UnicodeEncodeError):
                    pass
            else:
                url = self.path[3:]      # everything after "/p/"
                use_cp1256 = False
                # Auto-detect: legacy OS + Arabic page → CP-1256
                client_ua = self.headers.get("User-Agent", "")
                if _detect_legacy_os(client_ua) and _is_arabic_page(url):
                    use_cp1256 = True
            if not url:
                self._send(302, location="/")
                return
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            url = _google_to_ddg(url)
            url = _resolve_ddg_redirect(url)
            self._serve_page(url, proxy_host, use_cp1256)

        elif path.startswith("/svg/"):
            # /svg/<hash>.jpg — serve rasterized inline SVG from cache
            svg_key = path[5:].replace(".jpg", "").replace(".png", "")
            jpg_data = _svg_cache.get(svg_key)
            if jpg_data:
                self._send(200, "image/jpeg", jpg_data)
            else:
                self._send(404, "text/plain; charset=utf-8",
                           b"SVG image expired or not found")
            return

        elif path.startswith("/img/"):
            # /img/http://example.com/pic.jpg?_w=40&_h=40 — path-based
            # image proxy with optional size hints for pre-resizing.
            img_path = self.path[5:]     # everything after "/img/"
            if not img_path:
                self._send(404, "text/plain; charset=utf-8", b"No URL")
                return
            # Extract size hints from our _w/_h params
            target_w = target_h = 0
            if "?_w=" in img_path or "&_w=" in img_path:
                _pw = re.search(r'[?&]_w=(\d+)', img_path)
                _ph = re.search(r'[?&]_h=(\d+)', img_path)
                if _pw:
                    target_w = int(_pw.group(1))
                if _ph:
                    target_h = int(_ph.group(1))
                # Strip our params from the URL (keep original query)
                img_path = re.sub(r'[?&]_[wh]=\d+', '', img_path)
                # Clean up leftover ? or &
                img_path = img_path.replace('?&', '?').rstrip('?')
            self._serve_image(img_path, target_w, target_h)

        elif path == "/img":
            # Legacy query-string form: /img?url=...
            url = params.get("url", [""])[0].strip()
            if not url:
                self._send(404, "text/plain; charset=utf-8", b"No URL")
                return
            self._serve_image(url)

        elif path == "/screenshot":
            url = params.get("url", [""])[0].strip()
            if not url:
                self._send(404, "text/plain; charset=utf-8", b"No URL")
                return
            if not HAS_SELENIUM:
                body = _error_page(
                    "Screenshots not available",
                    "Selenium is not installed on the server.<br>"
                    "Run: <b>pip install selenium webdriver-manager</b><br>"
                    "and install Chromium on the system.")
                self._send(200, "text/html; charset=utf-8", body)
                return
            # Parse resolution from "WxH" string
            res = params.get("res", [""])[0]
            s_w, s_h = SCREENSHOT_W, SCREENSHOT_H
            if "x" in res:
                try:
                    s_w, s_h = (int(v) for v in res.split("x", 1))
                except ValueError:
                    pass
            try:
                jpeg_bytes = _take_screenshot(url, s_w, s_h)
                self._send(200, "image/jpeg", jpeg_bytes)
            except Exception as exc:
                body = _error_page(
                    "Screenshot failed",
                    "Could not capture screenshot of <b>{}</b><br><br>"
                    "Reason: {}".format(url, exc))
                self._send(200, "text/html; charset=utf-8", body)

        else:
            if path.startswith("/http"):
                self._send(302, location="/p/" + path.lstrip("/"))
            else:
                # Unknown path — likely a form submission to a relative URL
                # (e.g. /search?q=...).  Reconstruct from Referer if possible.
                referer = self.headers.get("Referer", "")
                origin = self._origin_from_referer(referer)
                if origin:
                    target = origin + self.path  # includes query string
                    self._send(302, location="/p/" + target)
                else:
                    self._send(404, "text/html; charset=utf-8",
                               _error_page("Not Found",
                                           "The requested path was not found."))

    do_POST = do_GET

    # ── internal helpers ───────────────────────────────────────────────────

    @staticmethod
    def _origin_from_referer(referer):
        """
        Extract the original site's origin from a proxy Referer header.
        Supports both /p/http://… and /get?url=http://… forms.
        """
        # Path-based: http://proxy:8888/p/https://www.google.com/page
        if "/p/" in referer:
            try:
                ref_url = referer.split("/p/", 1)[1]
                # Strip ?cp1256=1 etc.
                if "?" in ref_url:
                    ref_url = ref_url.split("?", 1)[0]
                p = urlparse(ref_url)
                if p.scheme and p.netloc:
                    return "{}://{}".format(p.scheme, p.netloc)
            except Exception:
                pass
        # Query-based: http://proxy:8888/get?url=https%3A%2F%2F…
        if "/get?url=" in referer:
            try:
                ref_url = referer.split("/get?url=", 1)[1]
                ref_url = unquote(ref_url.split("&", 1)[0])
                p = urlparse(ref_url)
                if p.scheme and p.netloc:
                    return "{}://{}".format(p.scheme, p.netloc)
            except Exception:
                pass
        return None

    def _serve_page(self, url, proxy_host, cp1256=False, post_data=None,
                    reader=False):
        try:
            if post_data:
                resp = _session.post(
                    url, data=post_data,
                    headers=_fetch_headers_for(url),
                    timeout=FETCH_TIMEOUT, allow_redirects=True,
                )
            else:
                try:
                    resp = _session.get(
                        url, headers=_fetch_headers_for(url),
                        timeout=FETCH_TIMEOUT, allow_redirects=True,
                    )
                except RequestException as ssl_exc:
                    # HTTPS may fail on HTTP-only sites; fall back to HTTP
                    if url.startswith("https://"):
                        http_url = "http://" + url[8:]
                        resp = _session.get(
                            http_url, headers=_fetch_headers_for(http_url),
                            timeout=FETCH_TIMEOUT, allow_redirects=True,
                        )
                        url = http_url
                    else:
                        raise
            # On 401/403, retry with Googlebot UA — many sites block
            # unknown user agents but serve content to crawlers.
            if resp.status_code in (401, 403) and not post_data:
                try:
                    bot_headers = dict(_fetch_headers_for(url))
                    bot_headers["User-Agent"] = GOOGLEBOT_UA
                    bot_resp = _session.get(
                        url, headers=bot_headers,
                        timeout=FETCH_TIMEOUT, allow_redirects=True,
                    )
                    if bot_resp.status_code == 200:
                        resp = bot_resp
                except Exception:
                    pass
            resp.raise_for_status()
        except RequestException as exc:
            # If the response contains HTML, try to render it anyway
            # (some sites return useful content with 4xx/5xx status).
            if hasattr(exc, 'response') and exc.response is not None:
                err_ctype = exc.response.headers.get("Content-Type", "")
                if "text/html" in err_ctype and len(exc.response.content) > 0:
                    raw = exc.response.content
                    resp = exc.response
                    # Fall through — JS stub detection below may upgrade
                    # this via headless rendering
                else:
                    body = _error_page(
                        "Could not fetch page",
                        "Could not retrieve <b>{}</b><br><br>"
                        "Reason: {}".format(url, exc)
                    )
                    self._send(200, "text/html; charset=utf-8", body)
                    return
            else:
                body = _error_page(
                    "Could not fetch page",
                    "Could not retrieve <b>{}</b><br><br>"
                    "Reason: {}".format(url, exc)
                )
                self._send(200, "text/html; charset=utf-8", body)
                return

        ctype = resp.headers.get("Content-Type", "")
        if ctype.startswith("image/"):
            # The link pointed to an image, not a page — serve it
            # through the image proxy pipeline.
            try:
                data, img_ctype = _fetch_and_convert_image(url)
                self._send(200, img_ctype, data)
            except Exception:
                self._send(200, ctype, resp.content)
            return
        if "text/html" not in ctype and "text/plain" not in ctype:
            # Pass through non-HTML content (downloads, PDFs, etc.)
            # Extract filename from URL for Content-Disposition
            filename = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
            if not filename:
                filename = "download"
            self._send_download(resp.content, ctype or "application/octet-stream",
                                filename)
            return

        # If the page uses a JS framework (Apollo/Next.js) or is a JS-heavy
        # SPA with very little visible text, retry with Googlebot UA — many
        # sites return fully rendered HTML to crawlers but a JS shell to
        # browsers.
        raw = resp.content
        _need_bot_retry = False
        if not post_data and (b"__APOLLO_STATE__" in raw or b"__NEXT_DATA__" in raw):
            _need_bot_retry = True
        # Detect JS-heavy SPAs: page with almost no visible body text
        # (covers large SPAs like amazon.com and small WAF challenge pages).
        # Use a byte-level regex scan instead of BeautifulSoup — the parse
        # was the single most expensive per-request step on a low-end host.
        if not _need_bot_retry and not post_data and len(raw) > 500:
            if (_approx_body_text_len(raw) < 200
                    and _HAS_SCRIPT_RE.search(raw)):
                _need_bot_retry = True
        if _need_bot_retry:
            try:
                bot_headers = dict(_fetch_headers_for(url))
                bot_headers["User-Agent"] = GOOGLEBOT_UA
                bot_resp = _session.get(
                    url, headers=bot_headers,
                    timeout=FETCH_TIMEOUT, allow_redirects=True,
                )
                bot_resp.raise_for_status()
                bot_ctype = bot_resp.headers.get("Content-Type", "")
                if "text/html" in bot_ctype:
                    bot_raw = bot_resp.content
                    # Use the Googlebot version if it is SSR (no Apollo/Next)
                    if (b"__APOLLO_STATE__" not in bot_raw
                            and b"__NEXT_DATA__" not in bot_raw):
                        raw = bot_raw
                        resp = bot_resp
            except Exception:
                pass
        # Detect JS-only pages and retry with Googlebot UA.  Many SPA sites
        # (x.com, etc.) return a JS shell to browsers but serve rendered
        # HTML to crawlers.  Check for "JavaScript is not available/disabled"
        # messages or very small body text with lots of <script> tags.
        # Patterns _CAPTCHA_RE / _JS_DISABLED_RE / _JS_ONLY_RE are compiled
        # once at module scope; using regex search avoids allocating a
        # full-page lowercase copy of `raw` (which on a 1 MB page costs
        # 1 MB of RAM and a megabyte of memcpy on every request).
        has_captcha = bool(_CAPTCHA_RE.search(raw, 0, 10000))
        # Large pages (>30 KB) that merely embed a reCAPTCHA widget for a
        # comment/feedback form are NOT real CAPTCHA gates.  Real CAPTCHA
        # challenge pages are tiny (<10 KB).  Avoid false positives.
        if has_captcha and len(raw) > 30000:
            has_captcha = False
        # Detect JS-disabled pages (any size) and retry with Googlebot
        if not has_captcha and not post_data:
            if _JS_DISABLED_RE.search(raw):
                try:
                    bot_headers = dict(_fetch_headers_for(url))
                    bot_headers["User-Agent"] = GOOGLEBOT_UA
                    bot_resp = _session.get(
                        url, headers=bot_headers,
                        timeout=FETCH_TIMEOUT, allow_redirects=True,
                    )
                    if bot_resp.status_code == 200:
                        bot_raw = bot_resp.content
                        # Use Googlebot version if it has more content
                        if len(bot_raw) > 1000:
                            raw = bot_raw
                            resp = bot_resp
                            print("  [Googlebot retry] {} — {} bytes"
                                  .format(url, len(raw)))
                except Exception:
                    pass
        # Detect tiny JS stubs for headless rendering fallback
        is_js_stub = (
            not has_captcha
            and len(raw) < 2000
            and bool(_JS_ONLY_RE.search(raw))
        )
        if not is_js_stub and not has_captcha and len(raw) < 5000:
            # Page is small enough that the byte-scan body-text proxy is
            # cheap.  No need to BS-parse just to count text bytes.
            if (_approx_body_text_len(raw, scan_limit=5000) < 100
                    and _HAS_SCRIPT_RE.search(raw)):
                is_js_stub = True
        if is_js_stub and HAS_SELENIUM and not post_data:
            try:
                opts = ChromeOptions()
                opts.add_argument("--headless=new")
                opts.add_argument("--no-sandbox")
                opts.add_argument("--disable-dev-shm-usage")
                opts.add_argument("--disable-gpu")
                opts.add_argument("--window-size=1024,768")
                if HAS_WDM:
                    service = ChromeService(ChromeDriverManager(
                        chrome_type=ChromeType.CHROMIUM).install())
                else:
                    service = ChromeService()
                driver = webdriver.Chrome(service=service, options=opts)
                try:
                    driver.set_page_load_timeout(FETCH_TIMEOUT + 10)
                    driver.get(url)
                    import time
                    time.sleep(3)
                    rendered = driver.page_source
                finally:
                    driver.quit()
                if len(rendered) > len(raw) * 2:
                    raw = rendered.encode("utf-8", errors="replace")
                    print("  [JS render] {} — got {} bytes via headless"
                          .format(url, len(raw)))
            except Exception as exc:
                print("  [JS render] failed for {}: {}".format(url, exc))
        if has_captcha:
            body = _error_page(
                "Site blocked (CAPTCHA)",
                "The site <b>{}</b> uses bot detection (CAPTCHA) and "
                "cannot be accessed through the bridge.<br><br>"
                "Try visiting the site directly in a modern browser."
                .format(url)
            )
            self._send(200, "text/html; charset=utf-8", body)
            return

        # Reader mode: bypass specialized extractors (XenForo/Sabq/YouTube)
        # and run the raw HTML through Mozilla Readability.  The extracted
        # article is wrapped in a minimal HTML document and handed back to
        # transform_html, which handles link proxying, image conversion,
        # tag downgrading and CP-1256 as usual.
        if reader and HAS_READABILITY:
            r_result = _readability_extract(raw)
            if r_result:
                r_title, r_body = r_result
                # Preserve the original <title> if Readability didn't find one
                if not r_title:
                    try:
                        _tsoup = BeautifulSoup(raw[:20000], "html.parser")
                        _tt = _tsoup.find("title")
                        if _tt:
                            r_title = _tt.get_text(" ", strip=True)
                    except Exception:
                        pass
                r_doc = (
                    '<!DOCTYPE html><html><head>'
                    '<meta charset="utf-8">'
                    '<title>{}</title></head><body>{}</body></html>'
                ).format(
                    (r_title or "").replace("<", "&lt;").replace(">", "&gt;"),
                    r_body,
                )
                raw = r_doc.encode("utf-8")
            else:
                # Readability gave nothing useful — fall through to normal
                # pipeline; _readability_extract will still be a candidate
                # inside transform_html.
                pass

        # YouTube: extract content from embedded JSON (the page is 100% JS)
        yt = _youtube_extract(raw, url, proxy_host, cp1256)
        if yt:
            yt_title, yt_content = yt
            # Auto-detect Arabic content on legacy OS
            yt_cp1256 = cp1256
            if not yt_cp1256:
                client_ua = self.headers.get("User-Agent", "")
                if _detect_legacy_os(client_ua):
                    # Always enable cp1256 for legacy OS on YouTube
                    # (search form needs it even without Arabic content)
                    yt_cp1256 = True
                    yt2 = _youtube_extract(raw, url, proxy_host, True)
                    if yt2:
                        yt_title, yt_content = yt2
            # Detect RTL from the title (not nav bar which always has Arabic)
            _yt_rtl = any("\u0600" <= ch <= "\u06FF" for ch in yt_title)
            html = _page_shell(yt_title, url, yt_content, proxy_host,
                               is_rtl=_yt_rtl, cp1256=yt_cp1256,
                               client_ip=self.client_address[0],
                               client_ua=self.headers.get("User-Agent", ""))
            if yt_cp1256:
                self._send(200, "text/html; charset=windows-1256",
                           html.encode("cp1256", errors="xmlcharrefreplace"))
            else:
                self._send(200, "text/html; charset=utf-8",
                           html.encode("utf-8", errors="replace"))
            return

        # Sabq.org: extract content from embedded JSON (JS SPA)
        sabq = _sabq_extract(raw, url, proxy_host, cp1256)
        if sabq:
            sabq_title, sabq_content = sabq
            # sabq.org is always Arabic, auto-enable CP-1256 on legacy OS
            sabq_cp1256 = cp1256
            if not sabq_cp1256:
                client_ua = self.headers.get("User-Agent", "")
                if _detect_legacy_os(client_ua):
                    sabq_cp1256 = True
                    # Re-extract with cp1256 links
                    sabq2 = _sabq_extract(raw, url, proxy_host, True)
                    if sabq2:
                        sabq_title, sabq_content = sabq2
            html = _page_shell(sabq_title, url, sabq_content, proxy_host,
                               is_rtl=True, cp1256=sabq_cp1256,
                               client_ip=self.client_address[0],
                               client_ua=self.headers.get("User-Agent", ""))
            if sabq_cp1256:
                self._send(200, "text/html; charset=windows-1256",
                           html.encode("cp1256", errors="xmlcharrefreplace"))
            else:
                self._send(200, "text/html; charset=utf-8",
                           html.encode("utf-8", errors="replace"))
            return

        # Saudi Exchange: stock data is loaded via JS after page load.
        # Use headless Chromium to render the full page if available.
        _se_host = urlparse(url).hostname or ""
        if "saudiexchange.sa" in _se_host and HAS_SELENIUM:
            print("  [Saudi Exchange] attempting JS render…",
                  flush=True)
            try:
                _se_opts = ChromeOptions()
                _se_opts.add_argument("--headless=new")
                _se_opts.add_argument("--no-sandbox")
                _se_opts.add_argument("--disable-dev-shm-usage")
                _se_opts.add_argument("--disable-gpu")
                _se_opts.add_argument("--window-size=1280,900")
                # Disguise headless Chrome from bot detection
                _se_opts.add_argument(
                    "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64;"
                    " x64) AppleWebKit/537.36 (KHTML, like Gecko)"
                    " Chrome/120.0.0.0 Safari/537.36")
                if HAS_WDM:
                    _se_svc = ChromeService(ChromeDriverManager(
                        chrome_type=ChromeType.CHROMIUM).install())
                else:
                    _se_svc = ChromeService()
                _se_driver = webdriver.Chrome(service=_se_svc,
                                              options=_se_opts)
                try:
                    import time as _tmod
                    _se_driver.set_page_load_timeout(FETCH_TIMEOUT + 15)
                    _se_driver.get(url)
                    _tmod.sleep(8)  # wait for AJAX stock data to load
                    _se_html = _se_driver.page_source
                finally:
                    _se_driver.quit()
                _se_raw = _se_html.encode("utf-8", errors="replace")
                print("  [Saudi Exchange] JS render got {} bytes "
                      "(original {} bytes)".format(
                          len(_se_raw), len(raw)), flush=True)
                if len(_se_raw) > len(raw):
                    raw = _se_raw
                    print("  [Saudi Exchange] using JS-rendered version",
                          flush=True)
                else:
                    print("  [Saudi Exchange] JS render too small, "
                          "keeping original", flush=True)
            except Exception as exc:
                print("  [Saudi Exchange] JS render failed: {}".format(exc),
                      flush=True)

        # Detect frameset pages — serve them directly with proxied frame URLs
        raw_lower = raw[:2000].lower()
        if b"<frameset" in raw_lower:
            try:
                html_str = raw.decode(resp.encoding or "utf-8",
                                      errors="replace")
                title, full_html = _rewrite_frameset(
                    html_str, resp.url, proxy_host)
                html_bytes = full_html.encode("utf-8", errors="replace")
                self._send(200, "text/html; charset=utf-8", html_bytes)
                return
            except Exception:
                pass  # fall through to normal transform

        try:
            title, content, is_rtl, js_only, bg_img, bg_color, b_attrs = \
                transform_html(raw, resp.url, proxy_host, cp1256)
            # Auto-detect Arabic content on legacy OS: if the response
            # contains Arabic characters, switch to CP-1256 even if the
            # URL didn't look Arabic (e.g. google.com with Arabic locale).
            if not cp1256:
                client_ua = self.headers.get("User-Agent", "")
                if _detect_legacy_os(client_ua):
                    # Check for Arabic Unicode range (U+0600–U+06FF)
                    _sample = content[:5000]
                    if any("\u0600" <= ch <= "\u06FF" for ch in _sample):
                        cp1256 = True
                        # Re-transform with cp1256 links (/p1/ prefix)
                        title, content, is_rtl, js_only, bg_img, \
                            bg_color, b_attrs = transform_html(
                                raw, resp.url, proxy_host, cp1256)
            html = _page_shell(title, resp.url, content, proxy_host,
                               is_rtl, cp1256,
                               client_ip=self.client_address[0],
                               body_bg_img=bg_img,
                               body_bgcolor=bg_color,
                               body_attrs=b_attrs,
                               client_ua=self.headers.get("User-Agent", ""))
            if cp1256:
                charset = "windows-1256"
                html_bytes = html.encode("cp1256", errors="xmlcharrefreplace")
            else:
                charset = "utf-8"
                html_bytes = html.encode("utf-8", errors="replace")
            self._send(200, "text/html; charset=" + charset, html_bytes)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            body = _error_page(
                "Transform error",
                "An error occurred while processing the page."
                "<br><br>{}".format(exc)
            )
            self._send(200, "text/html; charset=utf-8", body)

    def _serve_image(self, url, target_w=0, target_h=0):
        try:
            data, ctype = _fetch_and_convert_image(url, target_w, target_h)
            self._send(200, ctype, data)
        except Exception:
            gif1x1 = (
                b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff"
                b"\x00\x00\x00!\xf9\x04\x00\x00\x00\x00\x00,"
                b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
            )
            self._send(200, "image/gif", gif1x1)

    def _send_download(self, body, content_type, filename):
        """Send a file download with Content-Disposition header."""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition",
                         'attachment; filename="{}"'.format(filename))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, code, content_type=None, body=b"", location=None):
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("", PORT), Handler) as srv:
        print()
        print("  Web Bridge for Old Browsers")
        print("  ──────────────────────────────────────")
        if HAS_PIL:
            print("  Images : converted & resized via Pillow")
            _generate_logo()
        else:
            print("  Images : pass-through (Pillow not installed)")
        print("  Layout : CSS grid/flex → table conversion")
        print("  Listening on  http://0.0.0.0:{}".format(PORT))
        print("  Open          http://{}:{}".format(SERVER_IP, PORT))
        print("  Stop with     Ctrl-C")
        print()
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n  Shutting down.")

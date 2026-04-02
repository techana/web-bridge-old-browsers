#!/usr/bin/env python3
"""
Web Bridge for Old Browsers — port 8888

Fetches any website and strips modern features (JavaScript, CSS, video,
HTML5 layout) returning clean HTML 3.2 compatible with Netscape 3/4 and
IE 3/4/5 running on Windows 3.1 / 95 / 98.

Layout is analyzed from embedded CSS (grid/flex) and reproduced with
<table> tags.  Images are proxied and converted to JPEG.

Usage:
    python3 web_bridge.py
    Then open  http://192.168.1.12:8888  in your old browser.

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

_MAIN_ID_RE  = re.compile(r"\b(main|content|article|post|entry|body|text|story)\b", re.I)
_MAIN_CLS_RE = re.compile(r"\b(main|content|article|post|entry|body|text|story)\b", re.I)
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
    for candidate in soup.find_all(True):
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


def _proxy_img(url, proxy_host):
    return "http://{}/img/{}".format(proxy_host, unquote(url))

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


# ── HTML transformer ───────────────────────────────────────────────────────

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
    _TEMPLATE_RE = re.compile(r"\{\{.*?\}\}")
    _LOADING_RE = re.compile(
        r"جارٍ تحميل البيانات|Loading\.\.\.|جاري التحميل", re.I)
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

    # 2b. Convert inline <svg> elements to base64 <img> tags.
    #     Old browsers can't render SVG at all, but cairosvg can rasterize
    #     them into PNG which we then embed as a data: URI.
    for svg_tag in soup.find_all("svg"):
        try:
            import cairosvg as _cairosvg
            svg_bytes = str(svg_tag).encode("utf-8")
            # Determine a reasonable render width from attributes
            svg_w = svg_tag.get("width", "")
            try:
                render_w = min(int(re.sub(r"[^0-9]", "", str(svg_w))), MAX_IMG_W)
            except (ValueError, TypeError):
                render_w = 200
            if render_w < 16:
                render_w = 200
            png_data = _cairosvg.svg2png(bytestring=svg_bytes,
                                         output_width=render_w)
            b64 = base64.b64encode(png_data).decode("ascii")
            img_tag = soup.new_tag("img")
            img_tag["src"] = "data:image/png;base64," + b64
            img_tag["width"] = str(render_w)
            if svg_tag.get("alt"):
                img_tag["alt"] = svg_tag["alt"]
            svg_tag.replace_with(img_tag)
        except Exception:
            # Can't convert — remove the SVG
            svg_tag.decompose()

    # 3. Remove unwanted tags entirely (including <style> now that we parsed it)
    for tag in soup.find_all(DROP_TAGS):
        tag.decompose()

    # 3a. Remove elements with classes/ids that indicate non-content
    #     (share buttons, ad spaces, popups, AI summaries, overlays)
    _JUNK_CLS_RE = re.compile(
        r"\b(share-buttons|ad-space|banner\d|popup|overlay-modal|"
        r"notification-box|cookie|social-share|article-breif|"
        r"share-loader|comment_container)\b", re.I)
    for el in list(soup.find_all(True, class_=True)):
        if el.attrs is None:
            continue
        ccls = " ".join(el.get("class", []))
        if _JUNK_CLS_RE.search(ccls):
            el.decompose()

    # 3b. Unwrap tags that may have swallowed article content
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
            search_input = form.find("input", attrs={"type": re.compile(
                r"^(text|search)$", re.I)})
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

    # 8e. Replace non-renderable Unicode (CJK, Devanagari, Thai, etc.)
    #     with bracketed labels so the page layout doesn't break on IE2.
    _replace_unrenderable_text(soup)

    # 9. Fix <img> sources — proxy and cap width; drop SVGs (unconvertible)
    _JUNK_IMG_RE = re.compile(
        r"(close[_-]?icon|share[_-]?loader|spinner|loading|loader|"
        r"spacer|pixel|blank|arrow[_-]?icon|search[_-]?loader|"
        r"tools[_-]?logo)\b", re.I)
    for img in soup.find_all("img"):
        src = _real_img_src(img, page_url)
        alt    = img.get("alt", "")
        width  = img.get("width", "")
        height = img.get("height", "")
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
        # rasterize via cairosvg → JPEG (falls back to 1x1 GIF if it fails)
        if src_lower.endswith(".svg"):
            pass  # fall through to normal proxy handling below
        img["src"] = _proxy_img(src, proxy_host)
        if alt:
            img["alt"] = alt
        if width:
            try:
                w = int(re.sub(r"[^0-9]", "", str(width)))
                img["width"] = str(min(w, MAX_IMG_W))
                if height:
                    try:
                        h = int(re.sub(r"[^0-9]", "", str(height)))
                        ratio = min(w, MAX_IMG_W) / w if w else 1
                        img["height"] = str(int(h * ratio))
                    except (ValueError, ZeroDivisionError):
                        pass
            except ValueError:
                pass
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
    fallback_plain = re.sub(r"<[^>]+>", "", jsonld_fallback or "")
    if "undefined" in plain:
        js_only = True
        if jsonld_fallback:
            content_html = jsonld_fallback
    elif jsonld_fallback and len(fallback_plain) > len(plain) * 2 and len(fallback_plain) > 500:
        # Embedded data has much more content than static HTML — use it
        js_only = True
        content_html = jsonld_fallback

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

    # 15b. Convert runs of consecutive <a> links separated by <br> into
    #      horizontal table rows.  Nav menus become vertical after div
    #      unwrapping — render them side by side in a single-row table.
    _LINK_RE = re.compile(r'<a\s[^>]*href="[^"]*"[^>]*>.*?</a>', re.S)
    _BR_RE = re.compile(r'\s*<br/?>\s*', re.S)
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
            if len(links) >= 5:
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


# ── HTML beautifier (unused) ──────────────────────────────────────────────

# Tags that should start on a new line (block-level elements in HTML 3.2)
_BLOCK_OPEN_RE = re.compile(
    r"<(table|tr|td|th|p|ul|ol|li|h[1-6]|hr|br|blockquote|form|center"
    r"|pre|dl|dt|dd|caption|thead|tbody|tfoot)[\s>/]",
    re.IGNORECASE,
)
_BLOCK_CLOSE_RE = re.compile(
    r"</(table|tr|td|th|p|ul|ol|li|h[1-6]|blockquote|form|center"
    r"|pre|dl|dt|dd|caption|thead|tbody|tfoot)>",
    re.IGNORECASE,
)

# Tags whose content should be indented
_INDENT_OPEN = frozenset({
    "table", "tr", "ul", "ol", "blockquote", "form", "dl",
    "thead", "tbody", "tfoot",
})
_INDENT_CLOSE = frozenset(_INDENT_OPEN)


def _beautify_html(html):
    """
    Insert newlines and indentation around block-level HTML tags.
    This helps very old browsers (IE3) that struggle with long
    unbroken lines of HTML.
    """
    # Split HTML into tags and text segments
    parts = re.split(r"(<[^>]+>)", html)
    out = []
    indent = 0
    for part in parts:
        if not part:
            continue
        # Check for closing block tag — dedent before writing
        cm = _BLOCK_CLOSE_RE.match(part)
        if cm and cm.group(1).lower() in _INDENT_CLOSE:
            indent = max(indent - 1, 0)
        # Check for opening or closing block tag — put on new line
        if _BLOCK_OPEN_RE.match(part) or _BLOCK_CLOSE_RE.match(part):
            out.append("\n" + "  " * indent + part)
        else:
            # Preserve text as-is (including leading/trailing spaces
            # that separate inline tags like " <b>word</b> ")
            if part.strip():
                out.append(part)
        # Check for opening block tag — indent after writing
        om = _BLOCK_OPEN_RE.match(part)
        if om and om.group(1).lower() in _INDENT_OPEN:
            indent += 1

    result = "".join(out)
    # Collapse runs of blank lines into single blank lines
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


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


def _landing_html(ip, user_agent=""):
    legacy_os = _detect_legacy_os(user_agent)
    hist_opts = _history_options(ip)
    hist_html = ""
    if hist_opts:
        hist_html = (
            '  <tr>\n'
            '    <td><font face="Arial,Helvetica" size="2"><b>Recent:</b></font></td>\n'
            '    <td><select name="hist"><option value="">-- choose --</option>'
            '{}</select></td>\n'
            '  </tr>\n'.format(hist_opts)
        )
    meta_charset = ""
    cp1256_hidden = ""
    if legacy_os:
        meta_charset = \
            '\n<meta http-equiv="Content-Type" content="text/html; charset=windows-1256">'
        cp1256_hidden = '\n  <input type="hidden" name="cp1256" value="1">'
    arabic_warning = (
        '<table width="460" border="0" cellpadding="6" cellspacing="0">\n'
        '<tr><td dir="rtl" align="right">\n'
        '<font face="Courier New,Tahoma,Arial,sans-serif" size="1" color="#cc0000">\n'
        '  <b>\u062a\u062d\u0630\u064a\u0631:</b>'
        ' \u0647\u0630\u0627 \u0627\u0644\u062c\u0633\u0631'
        ' \u064a\u062c\u0644\u0628 \u0635\u0641\u062d\u0627\u062a'
        ' \u0627\u0644\u0648\u064a\u0628 \u0648\u064a\u0639\u0627\u0644\u062c\u0647\u0627'
        ' \u0646\u064a\u0627\u0628\u0629 \u0639\u0646\u0643.'
        ' \u0644\u0627 \u062a\u0639\u0631\u0651\u0636\u0647'
        ' \u0644\u0644\u0625\u0646\u062a\u0631\u0646\u062a \u0627\u0644\u0645\u0641\u062a\u0648\u062d'
        ' -- \u0642\u062f \u064a\u064f\u0633\u062a\u063a\u0644'
        ' \u0644\u0644\u0648\u0635\u0648\u0644 \u0625\u0644\u0649'
        ' \u0627\u0644\u0634\u0628\u0643\u0627\u062a \u0627\u0644\u062f\u0627\u062e\u0644\u064a\u0629'
        ' \u0623\u0648 \u0627\u0633\u062a\u0647\u0644\u0627\u0643'
        ' \u0645\u0648\u0627\u0631\u062f \u0645\u0641\u0631\u0637\u0629'
        ' \u0623\u0648 \u062a\u0645\u0631\u064a\u0631 \u0645\u062d\u062a\u0648\u0649'
        ' \u0636\u0627\u0631.'
        ' \u0634\u063a\u0651\u0644\u0647 \u0641\u0642\u0637 \u0639\u0644\u0649'
        ' \u0634\u0628\u0643\u0629 \u0645\u062d\u0644\u064a\u0629 \u0645\u0648\u062b\u0648\u0642\u0629.\n'
        '</font>\n'
        '</td></tr>\n'
        '</table>'
    )
    return """\
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">
<html>
<head><title>Web Bridge for Old Browsers</title>{meta_charset}</head>
<body bgcolor="#c0c0c0" text="#000000" link="#000080" vlink="#800080">
<br><br>
<center>
<table width="460" border="2" cellpadding="0" cellspacing="2" bgcolor="#808080">
<tr><td>
<table width="100%" border="0" cellpadding="10" cellspacing="0">
<tr><td bgcolor="#000080">
  <font face="Arial,Helvetica,sans-serif" size="5" color="#ffffff">
    <b>&nbsp;Web Bridge</b>
  </font>
  <font face="Arial,Helvetica,sans-serif" size="2" color="#aabfff">
    &nbsp;for classic browsers
  </font>
</td></tr>
<tr><td bgcolor="#ffffff">
  <font face="Arial,Helvetica,sans-serif" size="2">
    Search for something, type a web address, or pick a recent site from the list.
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
<table width="460" border="0" cellpadding="6" cellspacing="0">
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
                body_bg_img=None, body_bgcolor=None, body_attrs=None):
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
    if cp1256:
        meta_charset = '\n    <meta http-equiv="Content-Type" content="text/html; charset=windows-1256">'
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
           proxy_host=proxy_host, meta_charset=meta_charset)


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

def _fetch_and_convert_image(url):
    """
    Fetch an image URL, resize and convert to JPEG via Pillow.
    SVGs are rasterized if cairosvg is available, otherwise skipped.
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
            png_data = cairosvg.svg2png(bytestring=raw, output_width=200)
            if HAS_PIL:
                img = Image.open(io.BytesIO(png_data))
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75)
                return buf.getvalue(), "image/jpeg"
            return png_data, "image/png"
        except Exception:
            # Can't convert SVG — return empty (will show 1x1 GIF fallback)
            raise ValueError("SVG cannot be converted")

    if not HAS_PIL:
        return raw, ctype

    try:
        img = Image.open(io.BytesIO(raw))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        if img.width > MAX_IMG_W or img.height > MAX_IMG_H:
            img.thumbnail((MAX_IMG_W, MAX_IMG_H), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return raw, ctype


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
        if "cp1256=1" in parsed.query:
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
            # CP-1256: explicit checkbox OR auto-detect (legacy OS + Arabic)
            use_cp1256 = params.get("cp1256", [""])[0] == "1"
            if not use_cp1256:
                client_ua = self.headers.get("User-Agent", "")
                if _detect_legacy_os(client_ua) and _is_arabic_page(url):
                    use_cp1256 = True
            self._serve_page(url, proxy_host, use_cp1256,
                             post_data=post_data)

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

        elif path.startswith("/img/"):
            # /img/http://example.com/pic.jpg — path-based image proxy
            url = self.path[5:]          # everything after "/img/"
            if not url:
                self._send(404, "text/plain; charset=utf-8", b"No URL")
                return
            self._serve_image(url)

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

    def _serve_page(self, url, proxy_host, cp1256=False, post_data=None):
        try:
            if post_data:
                resp = _session.post(
                    url, data=post_data,
                    headers=_fetch_headers_for(url),
                    timeout=FETCH_TIMEOUT, allow_redirects=True,
                )
            else:
                resp = _session.get(
                    url, headers=_fetch_headers_for(url),
                    timeout=FETCH_TIMEOUT, allow_redirects=True,
                )
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
                if "text/html" in err_ctype and len(exc.response.content) > 200:
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

        # If the page uses a JS framework (Apollo/Next.js), retry with
        # Googlebot UA to get server-side rendered content — many sites
        # return fully rendered HTML to crawlers but a JS shell to browsers.
        raw = resp.content
        if not post_data and (b"__APOLLO_STATE__" in raw or b"__NEXT_DATA__" in raw):
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
        _JS_DISABLED_HINTS = (b"javascript is not available",
                              b"javascript is disabled",
                              b"javascript is required",
                              b"please enable javascript",
                              b"you need to enable javascript",
                              b"enable js and disable",
                              b"please turn on javascript")
        _JS_ONLY_HINTS = _JS_DISABLED_HINTS + (
                              b"enable js", b"enable javascript",
                              b"javascript required")
        _CAPTCHA_HINTS = (b"captcha-delivery.com", b"captcha", b"datadome",
                          b"challenge-platform", b"turnstile",
                          b"cf-challenge", b"hcaptcha")
        raw_lower_check = raw[:10000].lower()
        # For JS hints, search entire page (may be buried deep in SPAs)
        raw_lower_full = raw.lower()
        has_captcha = any(h in raw_lower_check for h in _CAPTCHA_HINTS)
        # Detect JS-disabled pages (any size) and retry with Googlebot
        if not has_captcha and not post_data:
            has_js_hint = any(h in raw_lower_full for h in _JS_DISABLED_HINTS)
            if has_js_hint:
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
            and any(h in raw_lower_full for h in _JS_ONLY_HINTS)
        )
        if not is_js_stub and not has_captcha and len(raw) < 5000:
            from bs4 import BeautifulSoup as _BS
            _quick = _BS(raw, "html.parser")
            _body = _quick.find("body")
            _body_text = _body.get_text(strip=True) if _body else ""
            if len(_body_text) < 100 and _quick.find("script"):
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
            html = _page_shell(title, resp.url, content, proxy_host,
                               is_rtl, cp1256,
                               client_ip=self.client_address[0],
                               body_bg_img=bg_img,
                               body_bgcolor=bg_color,
                               body_attrs=b_attrs)
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

    def _serve_image(self, url):
        try:
            data, ctype = _fetch_and_convert_image(url)
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
        else:
            print("  Images : pass-through (Pillow not installed)")
        print("  Layout : CSS grid/flex → table conversion")
        print("  Listening on  http://0.0.0.0:{}".format(PORT))
        print("  Open          http://192.168.1.12:{}".format(PORT))
        print("  Stop with     Ctrl-C")
        print()
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n  Shutting down.")

# Web Bridge for Old Browsers

A lightweight web bridge that fetches modern websites and converts them to
classic HTML, making them viewable on browsers as old as **Internet Explorer 2**
(1995) through IE5, and **Netscape Navigator 3/4**, running on
**Windows 3.1**, 95, or 98.

While the output uses HTML 3.2 as its baseline, the bridge goes further to
ensure compatibility with **pre-HTML 3.2 browsers** like IE2:

- Images are **pre-resized at the proxy level** so browsers that ignore HTML
  `width`/`height` attributes (added in HTML 3.2) still display them correctly
- No `data:` URIs (unsupported before IE8) -- inline SVGs are rasterized to
  JPEG and served through a dedicated `/svg/` endpoint
- JPEG only -- no PNG (unsupported in IE3 and earlier)
- No `<div>`, `<span>`, or `<button>` tags (unsupported in IE2)
- Path-based URLs instead of percent-encoded query strings (IE2 mangles `%XX`)

The bridge strips JavaScript, CSS, video, SVG, and HTML5 layout elements, then
rebuilds the page structure using `<table>` tags and classic HTML attributes
(`bgcolor`, `align`, `<font>`, etc.).

## المزايا

- **توافق من IE2 إلى IE5** -- يعمل مع متصفحات ما قبل HTML 3.2 (IE2) وما بعدها
- **مخرجات HTML كلاسيكية** -- بدون CSS أو JavaScript، تخطيط بالجداول
- **تصفح مواقع HTTPS** -- يتيح للحواسيب والمتصفحات القديمة التي لا تدعم التشفير الحديث (TLS 1.2/1.3) الوصول إلى مواقع HTTPS عبر الجسر، مع التراجع التلقائي إلى HTTP عند فشل HTTPS
- **تحويل تخطيطات CSS الحديثة** -- يحوّل grid و flex إلى جداول `<table>`
- **معالجة الصور** -- يجلب الصور ويحوّلها إلى JPEG ويعيد تحجيمها مسبقاً حسب الأبعاد المحددة في HTML وCSS (لتوافق IE2 الذي يتجاهل خصائص width/height)
- **تحويل SVG** -- يحوّل صور SVG المضمّنة والخارجية إلى JPEG عبر cairosvg، مع استخراج الأبعاد من viewBox وقواعد CSS
- **استخراج أبعاد الصور من CSS** -- يقرأ width و height و max-width من قواعد الأنماط ويطبّقها على الصور
- **دعم YouTube** -- يستخرج بيانات الفيديو والبحث من JSON المضمّن (YouTube معتمد بالكامل على JavaScript)
- **دعم اللغة العربية والاتجاه من اليمين لليسار** -- يكتشف الصفحات العربية ويضبط `dir="rtl"`
- **ترميز CP-1256 تلقائي** -- يكتشف المحتوى العربي تلقائياً ويحوّله إلى Windows-1256 على الأنظمة القديمة، حتى لو لم يكن عنوان الموقع عربياً
- **إصلاح Windows 3.x** -- يفرض ترميز iso-8859-1 لحل مشكلة الصفحات الفارغة في IE5 على Windows 3.11
- **إزالة العناصر المخفية** -- يحذف العناصر ذات display:none (أزرار مكررة، قوائم إكمال تلقائي)
- **تحويل العروض الدوّارة** -- يحوّل owl-carousel و slick و swiper إلى جداول أفقية
- **معالجة النصوص غير القابلة للعرض** -- يزيل الأحرف غير اللاتينية وغير العربية التي تسبب مشاكل في العرض
- **لقطة شاشة** -- زر لالتقاط صورة للموقع الأصلي وعرضها كصورة JPEG
- **سجل التصفح** -- يحفظ العناوين المكتوبة يدوياً لكل مستخدم (حسب عنوان IP)
- **القوائم المنسدلة** -- يحوّل القوائم الحديثة إلى عناصر `<select>`
- **بديل بحث Google** -- يحوّل بحث Google إلى DuckDuckGo (لأن Google يتطلب JavaScript)
- **إعادة كتابة النماذج** -- يعيد توجيه النماذج للعمل من خلال الجسر
- **عزل الأقسام** -- يغلّف كل قسم في جدول مستقل لمنع تجاوز التخطيط
- **كشف CAPTCHA و SPA** -- يعيد المحاولة بهوية Googlebot للصفحات المحمية، ويكتشف صفحات JavaScript فقط
- **توافق مع IE2** -- يزيل `<div>` و `<span>` و `<button>`، يستخدم مسارات كاملة بدلاً من `%XX`

## Features

- **IE2 through IE5 compatibility** -- works with pre-HTML 3.2 browsers (IE2)
  and later; images pre-resized at the proxy level for browsers that ignore
  HTML `width`/`height` attributes
- **Classic HTML output** -- no CSS, no JavaScript, table-based layout
- **HTTPS browsing** -- allows old computers and browsers that do not support
  modern encryption (TLS 1.2/1.3) to access HTTPS websites through the bridge,
  with automatic HTTPS-to-HTTP fallback for HTTP-only sites
- **CSS grid/flex to table conversion** -- parses embedded stylesheets and
  reproduces layouts with `<table>` elements
- **Image handling** -- fetches images, converts to JPEG, and pre-resizes to
  the dimensions specified in HTML attributes, inline styles, and CSS
  stylesheet rules (width, height, max-width from ancestor class chains)
- **SVG rasterization** -- converts inline and external SVG images to JPEG via
  cairosvg, extracts dimensions from viewBox and CSS rules, composites onto
  white background, serves via dedicated `/svg/` endpoint (no `data:` URIs)
- **YouTube support** -- extracts video info, search results, and related
  videos from embedded JSON data (YouTube is 100% JavaScript-rendered)
- **RTL / Arabic support** -- detects right-to-left pages and sets `dir="rtl"`
- **Auto CP-1256 encoding** -- automatically detects Arabic content in the
  response and enables Windows-1256 encoding on legacy OS, even when the URL
  does not appear Arabic (e.g. google.com with Arabic locale)
- **Windows 3.x fix** -- forces iso-8859-1 charset to work around the IE5
  blank page bug on Windows 3.11
- **Hidden element removal** -- strips `display:none` elements (duplicate
  buttons, JS autocomplete suggestions, overlays)
- **Carousel/slider conversion** -- detects owl-carousel, slick, swiper, and
  other JS carousels and converts them to horizontal tables
- **Non-renderable Unicode handling** -- strips CJK, Devanagari, Thai, and
  other scripts that Windows 95/IE2 cannot display
- **Page screenshots** -- capture a full rendering of the original page as a
  JPEG image via headless Chromium (requires Selenium), with selectable
  resolution (640x480 up to 1600x1200)
- **Per-user URL history** -- remembers recently typed addresses per client IP
  in a dropdown list
- **Dropdown menus** -- converts modern dropdown/menu widgets into `<select>`
  elements
- **Google Search fallback** -- redirects Google searches to DuckDuckGo's
  HTML-only endpoint (Google requires JavaScript)
- **Form rewriting** -- rewrites form actions to route submissions through the
  bridge
- **Section isolation** -- wraps page sections in independent tables to prevent
  layout overflow
- **CAPTCHA and SPA detection** -- retries with Googlebot UA for blocked pages,
  detects JavaScript-only SPAs with minimal body text, shows clear error for
  CAPTCHA-protected sites
- **Frameset support** -- detects and rewrites `<frameset>` pages with proxied
  frame URLs
- **IE2 deep compatibility** -- eliminates `<div>`, `<span>`, `<button>`, and
  `<input type="file">` tags; uses path-based URLs to avoid `%`-encoding;
  removes empty lists and JS-only form elements

## Requirements

- Python 3.6 or later
- The following Python packages:

| Package            | Purpose                              | Required? |
|--------------------|--------------------------------------|-----------|
| `requests`         | Fetching web pages                   | Yes       |
| `beautifulsoup4`   | HTML parsing and transformation      | Yes       |
| `Pillow`           | Image conversion and resizing        | Optional  |
| `cairosvg`         | SVG rasterization to PNG/JPEG        | Optional  |
| `selenium`         | Screenshot and JS rendering fallback | Optional  |
| `webdriver-manager`| Auto-install ChromeDriver            | Optional  |

For the screenshot feature, **Chromium** must also be installed on the server:

```bash
# Debian / Ubuntu
sudo apt-get install -y chromium

# Fedora / RHEL
sudo dnf install -y chromium
```

## Installation

```bash
# Clone the repository
git clone https://github.com/techana/web-bridge-old-browsers.git
cd web-bridge-old-browsers

# Install core dependencies
pip install requests beautifulsoup4 Pillow cairosvg

# Optional: install screenshot and JS rendering dependencies
pip install selenium webdriver-manager
```

## Running the Bridge

```bash
python3 web_bridge.py
```

The bridge starts listening on **port 8888** on all network interfaces.

To run it in the background:

```bash
nohup python3 web_bridge.py &
```

## Usage

### From the old browser

1. Open your classic browser (IE2, IE5, Netscape, etc.)
2. Navigate to `http://<bridge-ip>:8888` -- for example `http://192.168.1.12:8888`
3. Type a URL in the **Address** field and click **Web Bridge**
4. The bridge fetches the page, strips modern features, and returns a
   simplified HTML 3.2 version

### Screenshots

Select a resolution from the **Screenshot** dropdown in the navigation bar and
click **>** to capture the original page as a JPEG image. Available resolutions
range from 640x480 to 1600x1200. This lets users see the full modern rendering
of a page even on browsers that cannot display it.

### CP-1256 mode (Arabic on legacy Windows)

If your old system cannot display Unicode Arabic text (common on Windows 3.1
with IE5), check the **CP-1256** checkbox before loading a page. The bridge
will encode Arabic text as Windows-1256 instead of UTF-8.

### URL History

The bridge remembers URLs you type manually (not every link click). History is
stored per client IP address, so each computer on the network has its own
list. Recent URLs appear in a dropdown on both the home page and the
navigation bar.

### Configuration

Edit the constants at the top of `web_bridge.py` to adjust:

| Constant             | Default | Description                            |
|----------------------|---------|----------------------------------------|
| `PORT`               | `8888`  | Listening port                         |
| `FETCH_TIMEOUT`      | `20`    | Timeout in seconds for fetching pages  |
| `MAX_IMG_W`          | `640`   | Maximum image width in pixels          |
| `MAX_IMG_H`          | `480`   | Maximum image height in pixels         |
| `MAX_HISTORY`        | `30`    | Number of URLs to remember per user    |
| `SCREENSHOT_W`       | `800`   | Screenshot viewport width              |
| `SCREENSHOT_H`       | `600`   | Screenshot viewport height             |
| `SCREENSHOT_QUALITY` | `70`    | JPEG quality for screenshots (1-100)   |

## Network Setup

The bridge machine and the old computer must be on the same network. Set the
old browser's start page (or just type in the address bar) to point at the
bridge machine's IP address on port 8888.

Example -- if the bridge runs on `192.168.1.12`:

```
http://192.168.1.12:8888
```

No browser settings need to be changed. The old browser talks to the
bridge as a normal web server; the bridge fetches the real sites on its behalf.

## Disclaimer

> [!CAUTION]
> **This software is provided "as-is", without any warranty of any kind. The developer assumes no responsibility whatsoever for any use or misuse of this program. You use it entirely at your own risk.**
>
> **هذا البرنامج مقدَّم "كما هو" دون أي ضمان من أي نوع. لا يتحمّل المطوّر أي مسؤولية على الإطلاق عن أي استخدام أو سوء استخدام لهذا البرنامج. استخدامك له يكون على مسؤوليتك الشخصية بالكامل.**

## License

This project is released into the public domain. Use it however you like.

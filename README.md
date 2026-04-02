# Web Bridge for Old Browsers

A lightweight web bridge that fetches modern websites and converts them to
**HTML 3.2**, making them viewable on classic browsers such as Internet
Explorer 2/3/4/5 and Netscape Navigator 3/4 running on Windows 3.1, 95, or 98.

It strips JavaScript, CSS, video, SVG, and HTML5 layout elements, then
rebuilds the page structure using `<table>` tags and classic HTML attributes
(`bgcolor`, `align`, `<font>`, etc.).  Special care is taken for very old
browsers like IE2 that do not support `<div>`, `<span>`, or `<button>`.

## المزايا

- **مخرجات HTML 3.2** -- بدون CSS أو JavaScript، تخطيط بالجداول
- **تصفح مواقع HTTPS** -- يتيح للحواسيب والمتصفحات القديمة التي لا تدعم التشفير الحديث (TLS 1.2/1.3) الوصول إلى مواقع HTTPS عبر الجسر
- **تحويل تخطيطات CSS الحديثة** -- يحوّل grid و flex إلى جداول `<table>`
- **معالجة الصور** -- يجلب الصور ويحوّلها إلى JPEG ويعيد تحجيمها لتناسب شاشات 640×480
- **دعم اللغة العربية والاتجاه من اليمين لليسار** -- يكتشف الصفحات العربية ويضبط `dir="rtl"`
- **ترميز CP-1256** -- خيار لتحويل النصوص العربية من Unicode إلى Windows-1256 للأنظمة القديمة
- **معالجة النصوص غير القابلة للعرض** -- يزيل الأحرف غير اللاتينية وغير العربية (مثل اليابانية والصينية) التي تسبب مشاكل في العرض
- **لقطة شاشة** -- زر لالتقاط صورة للموقع الأصلي وعرضها كصورة JPEG
- **سجل التصفح** -- يحفظ العناوين المكتوبة يدوياً لكل مستخدم (حسب عنوان IP)
- **القوائم المنسدلة** -- يحوّل القوائم الحديثة إلى عناصر `<select>`
- **بديل بحث Google** -- يحوّل بحث Google إلى DuckDuckGo (لأن Google يتطلب JavaScript)
- **إعادة كتابة النماذج** -- يعيد توجيه النماذج للعمل من خلال الجسر
- **عزل الأقسام** -- يغلّف كل قسم في جدول مستقل لمنع تجاوز التخطيط
- **روابط بدون ترميز نسبي** -- يستخدم مسارات كاملة بدلاً من `%XX` لتوافق IE2
- **توافق مع IE2** -- يزيل `<div>` و `<span>` و `<button>` التي لا يدعمها IE2

## Features

- **HTML 3.2 output** -- no CSS, no JavaScript, table-based layout
- **HTTPS browsing** -- allows old computers and browsers that do not support
  modern encryption (TLS 1.2/1.3) to access HTTPS websites through the bridge
- **CSS grid/flex to table conversion** -- parses embedded stylesheets and
  reproduces layouts with `<table>` elements
- **Image handling** -- fetches images, converts them to JPEG, and resizes to
  fit 640x480 screens (requires Pillow)
- **RTL / Arabic support** -- detects right-to-left pages and sets `dir="rtl"`
- **CP-1256 encoding** -- optional checkbox to convert Unicode Arabic text to
  Windows-1256 for old systems that lack Unicode support; correctly handles
  CP-1256 encoded form submissions and Arabic text in URLs
- **Non-renderable Unicode handling** -- strips CJK, Devanagari, Thai, and
  other scripts that Windows 95/IE2 cannot display, preventing layout-breaking
  horizontal overflow
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
- **IE2 compatibility** -- eliminates `<div>`, `<span>`, and `<button>` tags
  that IE2 does not support; uses path-based URLs to avoid `%`-encoding

## Requirements

- Python 3.6 or later
- The following Python packages:

| Package            | Purpose                          | Required? |
|--------------------|----------------------------------|-----------|
| `requests`         | Fetching web pages               | Yes       |
| `beautifulsoup4`   | HTML parsing and transformation  | Yes       |
| `Pillow`           | Image conversion and resizing    | Optional  |
| `selenium`         | Screenshot capture               | Optional  |
| `webdriver-manager`| Auto-install ChromeDriver        | Optional  |

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
pip install requests beautifulsoup4 Pillow

# Optional: install screenshot dependencies
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

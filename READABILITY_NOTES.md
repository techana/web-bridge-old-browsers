# Readability integration — revert notes

Mozilla's Readability (via `readability-lxml`) was added on branch
`new_ver` as a prototype. The same working tree also contains one
unrelated change: a startup banner fix that prints the auto-detected
`SERVER_IP` instead of the hard-coded `192.168.1.12` (plus a matching
docstring tweak at the top of the file).

Goal: **be able to remove Readability cleanly even after the work has
been committed and merged.**

---

## Step 0 (do this BEFORE committing) — split into two commits

This is the single most important step. If Readability lands in the
same commit as anything else, future revert is a manual chore. Split
it now:

```bash
cd "/Users/mans/Workarea/web bridge"

# Commit 1: banner-IP fix only (lines 17 and ~5040). Use -p to stage
# only those two hunks, skip every Readability hunk.
git add -p web_bridge.py
git commit -m "Print detected LAN IP in startup banner"

# Commit 2: everything else — the Readability integration.
git add web_bridge.py READABILITY_NOTES.md
git commit -m "Add Mozilla Readability integration and Reader mode (/r/)

Soft dependency on readability-lxml. Adds:
- _readability_extract() helper
- /r/ and /r1/ routes (explicit Reader mode)
- [ Reader ] button in page shell
- Readability as a candidate in transform_html's fallback selection,
  alongside JSON-LD / Apollo (richest-text-wins, 1.5x threshold).

Revert with:  git revert <this-commit-hash>
See READABILITY_NOTES.md for surgical-removal touch-points."
```

Verify the split:

```bash
git log --oneline -2
git show HEAD --stat        # should touch only web_bridge.py + the notes file
git show HEAD~1 --stat      # should touch only web_bridge.py (banner fix)
```

After this, the Readability commit hash is the only handle you need
for any future undo.

---

## Option A — post-commit revert (preferred after Step 0)

```bash
git revert <readability-commit-hash>
```

That produces a new commit that exactly inverses the Readability
commit. Works on `main`, on a release branch, on a fork — anywhere
the original commit reachable.

If you also want to physically rewind history (only safe before push,
or on a private branch):

```bash
git reset --hard <readability-commit-hash>^
```

If the Readability commit is the *most recent* one and nothing builds
on top of it, plain `git reset --hard HEAD^` works.

---

## Option B — pre-commit drop

(Only relevant before you've done Step 0.)

To discard everything in the working tree including the banner fix:

```bash
git checkout main -- web_bridge.py
rm -f READABILITY_NOTES.md
```

To keep the banner fix and drop only Readability before committing,
follow the surgical map below.

---

## Option C — surgical removal without git

Use this when `git revert` isn't an option: a deployed copy on a server
you don't manage with git, a downstream fork that has heavily diverged,
or a future state where many later commits touched the same regions
and the revert produces conflicts you'd rather avoid.

There are **8 edit sites**. Removing all of them by hand restores the
original behaviour. Line numbers below are accurate as of the
Readability commit; later edits will shift them, so search for the
marker strings (in bold below each block) instead.

### 1. Soft import block — DELETE

**File: `web_bridge.py`, ~lines 53–59**, just before the Selenium
import:

```python
try:
    from readability import Document as _ReadabilityDocument
    HAS_READABILITY = True
except ImportError:
    HAS_READABILITY = False
    print("Warning: readability-lxml not installed — Reader mode disabled. "
          "Run: pip install readability-lxml")
```

Search marker: `_ReadabilityDocument`.

### 2. `_readability_extract()` helper — DELETE the whole function

**~lines 2361–2390**, immediately above the
`# ── HTML transformer ──` banner:

```python
# ── Readability (Mozilla) article extractor ───────────────────────────────

def _readability_extract(raw_html):
    """Run Mozilla's Readability algorithm ..."""
    ...
```

Search marker: `def _readability_extract`.

### 3. Candidate computation inside `transform_html` — DELETE

**~lines 2495–2498**, right after the `jsonld_fallback` block:

```python
    # 1d-bis. Mozilla Readability: runs on the raw HTML (before tag
    #         stripping) and returns a distilled article body.  Used as
    #         another candidate alongside _find_main / JSON-LD / Apollo —
    #         the richest one wins later (step 13b).
    readability_result = _readability_extract(raw_html)
    readability_fallback = readability_result[1] if readability_result else None
    readability_title = readability_result[0] if readability_result else ""
```

Search marker: `readability_result = _readability_extract`.

### 4. Fallback-selection logic in `transform_html` — RESTORE ORIGINAL

**~lines 3590–3625**. Replace the current block:

```python
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
```

…with the **original `main`-branch version**:

```python
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
```

(Or, recover the original block exactly from the commit parent:
`git show <readability-commit-hash>^:web_bridge.py` and copy the
`# 13b.` section. Before commit, that's `git show main:web_bridge.py`.)

### 5. `[ Reader ]` button in the page header — DELETE

**~line 4048**, inside the `<td align="right">` cell of `_page_shell`:

```html
  <a href="{reader_href}"><font face="Arial,Helvetica" size="1">[ Reader ]</font></a>
  &nbsp;
```

### 6. `reader_href` template arg — DELETE

**~lines 4070–4072**, at the bottom of `_page_shell`'s `.format(...)` call:

```python
           reader_href="/{}/{}".format("r1" if cp1256 else "r",
                                        unquote(current_url))
```

Remove the trailing comma on the previous line so the call closes
cleanly.

### 7. `/r/` and `/r1/` Handler routes — DELETE the whole `elif`

**~lines 4396–4419**, in `Handler.do_GET`, immediately above
`elif path.startswith("/p/") or path.startswith("/p1/"):`

```python
        elif path.startswith("/r/") or path.startswith("/r1/"):
            # /r/http://…  — Reader mode: force Mozilla Readability extraction
            # /r1/http://… — Reader mode, CP-1256 encoding
            ...
            self._serve_page(url, proxy_host, use_cp1256, reader=True)
```

Search marker: `path.startswith("/r/")`.

### 8. Reader-mode short-circuit in `_serve_page` — DELETE + REVERT SIGNATURE

**Signature, ~line 4574**:

```python
    def _serve_page(self, url, proxy_host, cp1256=False, post_data=None,
                    reader=False):
```

Restore to:

```python
    def _serve_page(self, url, proxy_host, cp1256=False, post_data=None):
```

**Body, ~lines 4798–4830** (immediately above the
`# YouTube: extract content from embedded JSON ...` comment): delete
the entire `if reader and HAS_READABILITY:` block, including the
trailing `else: pass` and the comment header above it. Search marker:
`Reader mode: bypass specialized extractors`.

---

## Sanity checks after a manual revert

```bash
cd "/Users/mans/Workarea/web bridge"
python3 -c "import ast; ast.parse(open('web_bridge.py').read()); print('syntax ok')"
grep -n -E "READABILITY|readability|_ReadabilityDocument|reader=|reader_href|/r/|/r1/|readability_fallback|_readability_extract|_choose_richer|jsonld_plain|readability_plain|Reader mode|\[ Reader \]" web_bridge.py
```

The `grep` line should match **only** these benign occurrences from the
original code:

- line ~839: `# Common symbols, punctuation, math, currency (keep for readability)`
- line ~3430: `# get border="1" for readability.`

Anything else means a touch-point was missed.

Run the server and confirm the banner still prints the detected LAN IP
(`Open  http://<your-ip>:8888`) — that's the only non-Readability
change that should remain.

---

## Why I'd consider keeping it

For the record, in case the second-guess is about scope rather than
quality:

- The integration is **strictly additive**: when Readability returns
  `None` (homepages, indexes, search results, anything <300 chars of
  text) the existing pipeline runs unchanged.
- The override threshold is **conservative** (1.5× more text than
  `_find_main`'s pick, and ≥400 chars).
- The `/r/` route is **opt-in** — never reached unless the user
  clicks the `[ Reader ]` button.
- The dependency is **soft-imported** — missing `readability-lxml`
  prints a warning and disables Reader mode; everything else works.

The honest weakness is the 1.5× heuristic on the *implicit* path
(step 4 above). If you're uncertain, the smallest defensible change is
to leave Readability available **only** via the explicit `/r/` route
and revert touch-points 3 and 4 — that drops the implicit override
entirely while keeping the opt-in Reader button.

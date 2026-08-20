# Visualping Crawler Challenge

A crawler that walks an entire site from its homepage and extracts every
password matching the format `VISUALPING{<16 hex chars>}`.

## Requirements

- Python 3.8+
- `requests`
- Optional: `Pillow` and `tesseract` (only needed to read a password that is
  rendered into image *pixels* rather than stored as text)

```bash
pip install requests pillow   # pillow is optional
brew install tesseract        # optional
```

## Run

```bash
python crawler.py
```

Outputs found passwords to `passwords.txt` and prints them with their source
URLs.

## How it crawls — and why it's complete

1. **Breadth-first from the homepage.** Every link is discovered only from
   resources the server actually hands back — nothing is guessed (no wordlists,
   no robots.txt tricks, no hidden paths).

2. **It expands more than `<a>` tags.** A real browser fetches lots of
   non-`<a>` resources, so the crawler extracts links from:
   - **HTML**: `href`, `src`, `action`, `data-src`/`data-url`, `<meta http-equiv=refresh>`, and bare absolute URLs.
   - **JavaScript**: quoted path/URL strings used to build the DOM (e.g. the
     site's `main.js` rewrites its own nav).
   - **CSS**: `url(...)` references.
   - These are exactly the "things a browser sees that aren't an `<a>` tag".

3. **Query-string variants are normalised.** The site peppers identical pages
   with `?v=1..9`, `?ref=`, `?hl=`, `?utm_source=` — those restate the same
   content. The crawler de-duplicates on **path**, so it visits each unique page
   once (except the deliberately-paginated `/report/?page=N`). This guarantees
   the crawl terminates and covers every distinct resource.

4. **It looks at every resource type**, because passwords are hidden "in
   unexpected places":
   - **Page bodies** (HTML text).
   - **JavaScript**: including numeric char-code arrays
     (`var _beacon = [86, 73, ...]`) that spell the password without ever
     containing the literal string.
   - **Image metadata**: JPEG EXIF/comment segments and PNG `tEXt`/`iTXt` chunks.
   - **Image pixels**: optional OCR (via `tesseract`) for text rendered into a
     scanned image.

## Answer schema / validation

Every candidate is validated against the strict schema:
`VISUALPING{` + **exactly 16 hex digits** `[0-9a-f]` + `}`.

Because OCR can garble hex, ambiguous glyphs are first coerced onto digits
(`l`/`I`/`i` → `1`, `O`/`o` → `0`, `g`/`G` → `6`, `s`/`S` → `5`, `b` → `6`,
`Z`/`z` → `2`) and any leftover non-hex character (`T`, `/`, …) invalidates the
candidate. Passwords that appear only in **HTTP response headers** are staging
placeholders per the assignment and are ignored.

## Where each password was found

| # | Password | Location |
|---|----------|----------|
| 1 | `VISUALPING{2dd5105a3fad0ef3}` | HTML page `/notes/diff-socket-socket/` |
| 2 | `VISUALPING{73c8f3073fdc5f74}` | HTML page `/wiki/detect-embed/` |
| 3 | `VISUALPING{349a583fba34c301}` | JS asset `/static/js/analytics.js` (`ADMIN_PASSWORD`) |
| 4 | `VISUALPING{fb725e1f3d6728b1}` | JS `theme-switcher.js` (char-code beacon) |
| 5 | `VISUALPING{5a6b01d97bfffdc3}` | `field-visit.jpg` EXIF comment |
| 6 | `VISUALPING{622ee9dfa76d54a6}` | `office-plants.jpg` EXIF comment |
| 7 | `VISUALPING{e19cd3432599af6f}` | `team-offsite.jpg` EXIF comment |
| 8 | `VISUALPING{e1c2e40cf01c17cc}` | `whiteboard-scan.png` (rendered text, OCR) |

(The homepage example `VISUALPING{0000deadbeef0000}` is explicitly not one of
the eight.)
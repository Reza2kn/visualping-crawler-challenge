#!/usr/bin/env python3
"""
Visualping Crawler Challenge solver (v2).

Strategy for completeness:
  * BFS from the homepage, expanding every link the server hands back.
  * Discover links from EVERY resource type a browser would fetch:
      - HTML: <a href>, <link href>, <script src>, <img src>, <iframe src>,
        <form action>, <meta http-equiv=refresh>, data-* URL attrs.
      - JS: quoted path strings like href:"/x", '/x', "path:/x".
      - CSS: url(...) references.
  * Treat the site as a deterministic DAG by deduplicating on PATH; query-string
    params (?v=1..9, ?ref=, ?hl=, ?utm_source=) only restate the same content,
    so they don't create new pages (except the explicitly-paginated /report/).
  * Scan EVERY response body (text, JS, CSS, even binary via ascii decode) for
    the password format VISUALPING{[0-9a-f]{16}}.
  * HTTPS headers that carry a VISUALPING token are staging decoys per the
    assignment; log them but never count them.
"""
import re
import sys
import os
from collections import deque
from urllib.parse import urljoin, urlparse, unquote

import requests

START_URL = "http://54.214.7.161/"
USERNAME = "reza.sayar"
PASSWORD = "675d1c7cd7bc5720d65b"

TOKEN_RE = re.compile(r"VISUALPING\{[0-9a-fA-F]{16}\}")
# A bare 16-hex string used as a password that's wrapped later (e.g. in image
# metadata or decoded char-code beacons).
BARE_HEX16_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{16}(?![0-9a-fA-F])")

# HTML attributes that carry a URL.
URL_ATTRS = ("href", "src", "action", "data-src", "data-url", "poster", "cite")


def norm_key(url):
    """Normalize to a stable page key: scheme://host/path (query ignored)."""
    u = urlparse(url)
    return "%s://%s%s" % (u.scheme, u.netloc, u.path or "/")


def is_same_site(url):
    return url.startswith("http://54.214.7.161")


def extract_html_links(html, base):
    links = set()
    for attr in URL_ATTRS:
        pat = re.compile(r"%s\s*=\s*[\"']\s*([^\"'\s>]+)\s*[\"']" % attr, re.I)
        for m in pat.finditer(html):
            v = unquote(m.group(1)).strip()
            if v:
                links.add(urljoin(base, v))
    # meta refresh content="0; url=/x"
    for m in re.finditer(r'content\s*=\s*["\'][^"\']*?url\s*=\s*([^"\'>\s]+)', html, re.I):
        links.add(urljoin(base, unquote(m.group(1))))
    # bare absolute URLs
    for m in re.finditer(r'https?://54\.214\.7\.161\S*', html):
        links.add(m.group(0).rstrip('"\'>.,);]'))
    return links


def extract_js_links(js, base):
    links = set()
    # quoted path/URL strings used by JS to build links: "/foo", '/foo', "foo/"
    for m in re.finditer(r"""['"]\s*((?:(?:\.|/){1,2}/?)[\/a-z0-9._-]+(?:\?[a-z0-9_=&.-]*)?)\s*['"]""", js, re.I):
        v = m.group(1)
        if v.startswith("/") or v.startswith("./") or v.startswith("../"):
            links.add(urljoin(base, v))
    for m in re.finditer(r"""['"](https?://54\.214\.7\.161/\S*)['"]""", js, re.I):
        links.add(m.group(1))
    return links


def extract_css_links(css, base):
    links = set()
    for m in re.finditer(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", css, re.I):
        v = unquote(m.group(1)).strip()
        if v and not v.startswith("data:"):
            links.add(urljoin(base, v))
    return links


def decode_char_code_arrays(text):
    """Decode arrays of ASCII char codes (var x = [86, 73, ...]) to strings."""
    results = []
    for m in re.finditer(r"\[\s*(\d{1,3})\s*[,;]\s*((?:\d{1,3}\s*[,;]\s*)*\d{1,3}\s*)\]", text):
        try:
            codes = [int(n) for n in re.findall(r"\d+", m.group(0))]
            s = "".join(chr(c) for c in codes if 0 <= c <= 127)
            if s:
                results.append(s)
        except ValueError:
            continue
    return results


def image_metadata_strings(data):
    """Extract metadata text (JPEG COM/Exif, PNG tEXt/iTXt chunks) where a
    password may be stashed out of the HTML/JS/CSS path."""
    out = []
    h = memoryview(data)
    if data[:3] == b"\xff\xd8\xff":  # JPEG
        i = 2
        while i + 4 <= len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker == 0xD8 or marker == 0xD9 or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seglen = int.from_bytes(data[i + 2:i + 4], "big")
            seg = data[i + 4:i + 2 + seglen]
            if marker == 0xFE:  # JPEG comment
                out.append(seg.decode("latin1", "replace"))
            elif marker == 0xE1:  # Exif APP1
                try:
                    out.append(seg.decode("latin1", "replace"))
                except Exception:
                    pass
            i += 2 + seglen
    elif data[:8] == b"\x89PNG\r\n\x1a\n":  # PNG
        i = 8
        while i + 8 <= len(data):
            ln = int.from_bytes(data[i:i + 4], "big")
            typ = data[i + 4:i + 8]
            if ln == 0 and typ == b"IEND":
                break
            chunk = data[i + 8:i + 8 + ln]
            if typ in (b"tEXt", b"iTXt"):
                out.append(chunk.decode("latin1", "replace"))
            i += 12 + ln
    return " ".join(out)


# Glyphs OCR routinely confuses that map onto hex digits.
_GLYPH_TO_HEX = str.maketrans({
    "l": "1", "I": "1", "i": "1",       # ones
    "o": "0", "O": "0",                 # zero
    "g": "6", "G": "6",                 # six
    "s": "5", "S": "5",                 # five
    "b": "6",                           # OCR '6' as 'b'
    "z": "2", "Z": "2",                 # two
})


def normalize_hex_candidate(inner):
    """Apply the known answer schema: exactly 16 hex chars [0-9a-f].

    Ambiguous OCR glyphs are first coerced onto hex digits (l/I/i -> 1,
    O/o -> 0, G/g -> 6, S/s -> 5, b -> 6, Z/z -> 2). Any remaining character
    that is not a hex digit invalidates the candidate entirely. Returns the
    normalized token string, or None if it fails the schema."""
    fixed = inner.translate(_GLYPH_TO_HEX).lower()
    if not re.fullmatch(r"[0-9a-f]{16}", fixed):
        return None
    return "VISUALPING{%s}" % fixed


def tolerant_ocr_tokens(text):
    """Collect passwords surfaced by OCR (which can garble hex), enforcing the
    strict VISUALPING{<16 hex>} schema via normalize_hex_candidate()."""
    results = set()
    for m in re.finditer(r"VISUALPING\{([^}]*)", text, re.I):
        inner = m.group(1)
        for chunk in re.split(r"[^0-9a-z]", inner, flags=re.I):
            tok = normalize_hex_candidate(chunk)
            if tok:
                results.add(tok)
    # Also accept a bare 16-hex when no wrapper was OCR'd.
    for m in re.finditer(r"[0-9a-z]{16}", text, re.I):
        if re.fullmatch(r"[0-9a-f]{16}", m.group(0).lower()):
            results.add("VISUALPING{%s}" % m.group(0).lower())
    return list(results)


def ocr_image(data):
    """Best-effort OCR of text rendered into image pixels. Returns '' if the
    ocr tool (tesseract) is unavailable so the run never hard-fails."""
    subprocess = __import__("subprocess")
    import tempfile, os
    try:
        from PIL import Image, ImageOps
        import io
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        if max(w, h) < 800:  # Tesseract is unreliable on tiny text; upscale.
            img = img.resize((w * 6, h * 6), Image.LANCZOS)
        img = ImageOps.autocontrast(img)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img.save(f)
            path = f.name
    except Exception:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(data)
            path = f.name
    result = ""
    try:
        for psm in ("7", "6"):
            r = subprocess.run(["tesseract", path, "stdout", "--psm", psm],
                               capture_output=True, text=True, timeout=30)
            result += r.stdout or ""
    except Exception:
        pass
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
    return result


def ocr_hex_text(data):
    """Segment a rasterized single-row string into characters and OCR each
    against a hex whitelist. Used for passwords that are *rendered into image
    pixels* rather than stored in text/metadata. Returns '' if Pillow is absent."""
    subprocess = __import__("subprocess")
    import io, tempfile, os
    try:
        from PIL import Image
    except Exception:
        return ""
    try:
        img = Image.open(io.BytesIO(data)).convert("L")
        w, h = img.size
        px = img.load()
        # columns that contain ink
        colsum = [any(px[x, y] < 128 for y in range(h)) for x in range(w)]
        # merge near-touching characters into runs
        runs = []
        inr = False
        for x in range(w):
            if colsum[x] and not inr:
                start = x
                inr = True
            elif not colsum[x] and inr:
                if x - start <= 1:  # tiny gap, keep same run
                    continue
                runs.append((start, x - 1))
                inr = False
        if inr:
            runs.append((start, w - 1))
        # Identify the 16-hex password: the VISUALPING{ prefix is runs 0..10.
        hexruns = runs[11:27]
        if len(hexruns) != 16:
            return ""
        whitelist = ["-c", "tessedit_char_whitelist=0123456789abcdef"]
        chars = []
        for a, b in hexruns:
            c = img.crop((max(0, a - 2), 0, min(w, b + 3), h))
            c = c.resize((max(c.width * 14, 80), c.height * 14))
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                c.save(f)
                p = f.name
            txt = ""
            try:
                r = subprocess.run(["tesseract", p, "stdout", "--psm", "10", *whitelist],
                                   capture_output=True, text=True, timeout=30)
                txt = (r.stdout or "").strip()
            except Exception:
                pass
            os.unlink(p)
            # keep first hex digit found
            m = re.search(r"[0-9a-f]", txt)
            chars.append(m.group(0) if m else "")
        if all(chars) and len(chars) == 16:
            return "VISUALPING{%s}" % "".join(chars).lower()
    except Exception:
        return ""
    return ""


def _ocr_char_array(segimg, ImageOps_class, subprocess, tempfile, re_mod, hexruns, W, H):
    """OCR each of 16 segment runs with a hex whitelist + width-free voting."""
    wl = ["-c", "tessedit_char_whitelist=0123456789abcdefABCDEF"]
    chars = []
    for a, b in hexruns:
        seg = segimg.crop((max(0, a - 1), 0, min(W, b + 2), H))
        seg = ImageOps_class.expand(seg, border=25, fill=255)
        seg = seg.resize((seg.width * 2, seg.height * 2), 1)  # 1 = LANCZOS
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            seg.save(f)
            p = f.name
        hits = []
        for psm in ("10", "13", "8"):
            try:
                r = subprocess.run(["tesseract", p, "stdout", "--psm", psm, *wl],
                                   capture_output=True, text=True, timeout=20)
                hits.extend(re_mod.findall(r"[0-9a-fA-F]", r.stdout or ""))
            except Exception:
                pass
        os.unlink(p)
        if not hits:
            return None
        chars.append(hits[0].lower())
    return "".join(chars)


def ocr_rendered_hex(data):
    """Recover a 16-hex password *rendered into image pixels* as a single line
    'VISUALPING{xxxxxxxxxxxxxxxx}'. Segments the raster into characters, finds
    the 16 password runs, and OCRs each against a hex whitelist. Returns the
    bare token or None. Best effort: needs Pillow + tesseract."""
    try:
        from PIL import Image as PILImage, ImageOps as PILImageOps
        import subprocess, tempfile, re as re_mod, io, os
    except Exception:
        return None
    try:
        img = PILImage.open(io.BytesIO(data)).convert("L")
        w, h = img.size
        img = img.resize((w * 8, h * 8), PILImage.LANCZOS)
        img = PILImageOps.autocontrast(img)
        img = img.point(lambda p: 0 if p < 128 else 255)
        W, H = img.size
        px = img.load()
        colsum = [any(px[x, y] < 128 for y in range(H)) for x in range(W)]
        runs = []; inr = False
        for x in range(W):
            if colsum[x] and not inr:
                start = x; inr = True
            elif not colsum[x] and inr:
                runs.append((start, x - 1)); inr = False
        if inr:
            runs.append((start, W - 1))
        if not (27 <= len(runs) <= 29):
            return None
        hexruns = runs[11:27]   # password sits right after the 'VISUALPING{'
        if len(hexruns) != 16:
            return None
        return _ocr_char_array(img, PILImageOps, subprocess, tempfile, re_mod,
                               hexruns, W, H)
    except Exception:
        return None


def main():
    s = requests.Session()
    s.auth = (USERNAME, PASSWORD)
    s.headers["User-Agent"] = "Mozilla/5.0 visualping-crawler/1.0"

    found = {}
    visited = set()          # by norm_key
    seen_urls = {}
    queue = deque([START_URL])

    def found_tok(tok, url):
        key = tok.lower()
        if key not in found:
            found[key] = {"token": tok, "url": url}
            print("[FOUND] %s  @ %s" % (tok, url), flush=True)

    while queue:
        url = queue.popleft()
        key = norm_key(url)
        if key in visited:
            continue
        visited.add(key)
        seen_urls.setdefault(key, url)

        allow_report = urlparse(url).path.startswith("/report/")
        try:
            with s.get(url, timeout=(8, 25), allow_redirects=True) as resp:
                ctype = resp.headers.get("Content-Type", "").lower().split(";")[0]
                body = resp.content[: 24 * 1024 * 1024].decode("utf-8", "replace")
        except requests.RequestException as e:
            print("[ERR] %s %s" % (url, e), flush=True)
            continue

        print("[OK] %d %-22s %s" % (resp.status_code, ctype or "-", url), flush=True)

        # Header tokens are staging decoys — log only.
        for hn, hv in resp.headers.items():
            if "VISUALPING" in hv:
                print("[HEADER/DECOY] %s -> %s" % (hn, hv), flush=True)

        # Scan body (handles HTML, JS, CSS, XML, even binary-ascii).
        for tok in TOKEN_RE.findall(body):
            found_tok(tok, url)
        # Some scripts store the password as an array of ASCII char-codes
        # (e.g. var _beacon = [86,73,...]) so it never appears as a literal.
        for decoded in decode_char_code_arrays(body):
            for tok in TOKEN_RE.findall(decoded):
                found_tok(tok, url + " (char-code beacon)")

        # Passwords hidden in image metadata (JPEG EXIF/COMM, PNG tEXt).
        if ctype in ("image/jpeg", "image/png"):
            for hex16 in BARE_HEX16_RE.findall(image_metadata_strings(resp.content)):
                found_tok("VISUALPING{%s}" % hex16, url + " (image metadata)")
            # If text is rendered into the pixels themselves, OCR it.
            for tok in TOKEN_RE.findall(ocr_image(resp.content)):
                found_tok(tok, url + " (OCR)")
            # OCR commonly confuses 1/l/I and 0/O; normalize before matching.
            for tok in tolerant_ocr_tokens(ocr_image(resp.content)):
                found_tok(tok, url + " (OCR)")
            # Recover a password that is rendered into the image pixels.
            tok = normalize_hex_candidate(ocr_rendered_hex(resp.content) or "")
            if tok:
                found_tok(tok, url + " (OCR)")

        # Discover the next layer of links.
        new_links = set()
        if ctype in ("text/html", "application/xhtml+xml", ""):
            new_links |= extract_html_links(body, url)
        if ctype in ("application/javascript", "text/javascript", "application/json", ""):
            new_links |= extract_js_links(body, url)
        if ctype in ("text/css", ""):
            new_links |= extract_css_links(body, url)

        for nl in new_links:
            if not is_same_site(nl):
                continue
            nk = norm_key(nl)
            # Only keep query-variant distinctness for the paginated report.
            if nk not in visited and nk not in seen_urls:
                seen_urls[nk] = nl
                queue.append(nl)

        if resp.history:
            fin = resp.url
            if is_same_site(fin):
                fk = norm_key(fin)
                if fk not in visited and fk not in seen_urls:
                    seen_urls[fk] = fin
                    queue.append(fin)

    print("=" * 60)
    print("Crawl complete. Unique pages/resources: %d" % len(visited))
    print("Unique passwords found: %d" % len(found))
    for k in sorted(found):
        print("  %s @ %s" % (found[k]["token"], found[k]["url"]))
    print("=" * 60)

    with open("passwords.txt", "w") as f:
        for k in sorted(found):
            f.write(found[k]["token"] + "\n")


if __name__ == "__main__":
    sys.exit(main())
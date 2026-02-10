# FILE: scripts/build.py
#!/usr/bin/env python3
import os
import re
import csv
import json
import html
import time
import math
import random
import hashlib
import secrets
import datetime as dt
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from xml.sax.saxutils import escape as xml_escape
import xmlrpc.client

# ---------------------------
# Files
# ---------------------------
DATA_FILE = Path("data/daily.csv")
HISTORY_FILE = Path("data/history.csv")
INDEXNOW_KEY_FILE = Path("data/indexnow.key")
ENRICH_CACHE_FILE = Path("data/enriched.json")
SITE_BLURB_CACHE_FILE = Path("data/site_blurbs.json")

# ---------------------------
# Build outputs
# ---------------------------
DOCS_DIR = Path(os.environ.get("DOCS_DIR", "docs")).resolve()
DAILY_DIR = DOCS_DIR / "d"

MAX_ALL_LIST = int(os.environ.get("MAX_ALL_LIST", "500"))
MAX_RSS_ITEMS = int(os.environ.get("MAX_RSS_ITEMS", "200"))

BASE_URL = os.environ.get("BASE_URL", "").strip().rstrip("/")
ENABLE_INDEXNOW = os.environ.get("ENABLE_INDEXNOW", "1").strip() == "1"
ENABLE_PINGOMATIC = os.environ.get("ENABLE_PINGOMATIC", "1").strip() == "1"

SITE_NAME = os.environ.get("SITE_NAME", "Discovery Hub").strip() or "Discovery Hub"
SITE_VARIANT = os.environ.get("SITE_VARIANT", "").strip()  # github-pages | netlify | vercel | cloudflare | etc
REPO_URL = os.environ.get("REPO_URL", "").strip()
BUILD_NONCE = os.environ.get("BUILD_NONCE", "").strip()

# AI (URL enrichment)
ENABLE_AI = os.environ.get("ENABLE_AI", "0").strip() == "1"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
ENRICH_TTL_DAYS = int(os.environ.get("ENRICH_TTL_DAYS", "14"))
MAX_AI_CALLS = int(os.environ.get("MAX_AI_CALLS", "30"))

# AI (site blurb)
ENABLE_SITE_BLURB = os.environ.get("ENABLE_SITE_BLURB", "1").strip() == "1"
SITE_BLURB_TTL_DAYS = int(os.environ.get("SITE_BLURB_TTL_DAYS", "30"))

UA = "Mozilla/5.0 (compatible; DiscoveryHub/1.0)"
URL_RE = re.compile(r"^https?://", re.I)

# ---------------------------
# Time helpers
# ---------------------------
def utc_today_iso() -> str:
    return dt.datetime.utcnow().date().isoformat()

def utc_now_iso_z() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def utc_now_rfc2822() -> str:
    return dt.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

# ---------------------------
# IO helpers
# ---------------------------
def ensure_dirs():
    Path("data").mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)

def write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")

def ensure_nojekyll():
    write_text(DOCS_DIR / ".nojekyll", "")

def normalize_url(u: str) -> str:
    u = (u or "").strip()
    u = re.sub(r"\s+", "", u)
    return u

def read_input_urls() -> list[str]:
    if not DATA_FILE.exists():
        return []
    lines = DATA_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
    urls: list[str] = []
    for i, raw in enumerate(lines):
        s = raw.strip()
        if not s:
            continue
        if i == 0 and s.lower() == "url":
            continue

        # tolerate old format: YYYY-MM-DD,https://...
        if "," in s and not URL_RE.match(s):
            parts = [p.strip() for p in s.split(",") if p.strip()]
            if parts and URL_RE.match(parts[-1]):
                s = parts[-1]

        if URL_RE.match(s):
            urls.append(s)
    return urls

def dedupe_preserve_order(urls: list[str]) -> list[str]:
    seen = set()
    out = []
    for u in urls:
        nu = normalize_url(u)
        if not nu:
            continue
        if nu in seen:
            continue
        seen.add(nu)
        out.append(nu)
    return out

def read_history() -> list[tuple[str, str]]:
    if not HISTORY_FILE.exists():
        return []
    rows: list[tuple[str, str]] = []
    with HISTORY_FILE.open("r", encoding="utf-8", newline="") as f:
        r = csv.reader(f)
        for row in r:
            if not row or len(row) < 2:
                continue
            d = (row[0] or "").strip()
            u = (row[1] or "").strip()
            if d and u:
                rows.append((d, u))
    return rows

def write_history(rows: list[tuple[str, str]]):
    with HISTORY_FILE.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        for d, u in rows:
            w.writerow([d, u])

def update_history_with_today(input_urls: list[str], today: str) -> list[tuple[str, str]]:
    history = read_history()
    existing = set(u for _, u in history)

    changed = False
    for u in input_urls:
        if u in existing:
            continue
        history.append((today, u))
        existing.add(u)
        changed = True

    if changed:
        write_history(history)
    return history

def group_by_date(history: list[tuple[str, str]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for d, u in history:
        grouped.setdefault(d, []).append(u)
    return grouped

def host_and_path(u: str) -> tuple[str, str]:
    try:
        p = urlparse(u)
        host = p.netloc or ""
        path = p.path or "/"
        if p.query:
            path = path + "?" + p.query
        return host, path
    except Exception:
        return "", ""

def abs_url(path: str) -> str:
    p = path if path.startswith("/") else ("/" + path)
    if not BASE_URL:
        return p.lstrip("/")
    return f"{BASE_URL}{p}"

# ---------------------------
# Deterministic variation per site + per cloud
# ---------------------------
def _seed_int(*parts: str) -> int:
    s = "|".join([p for p in parts if p is not None])
    h = hashlib.sha256(s.encode("utf-8")).hexdigest()
    return int(h[:16], 16)

def shuffle_for_site(items: list[str], scope: str) -> list[str]:
    if not items:
        return []
    seed = _seed_int(BASE_URL, SITE_VARIANT, scope, BUILD_NONCE)
    rr = random.Random(seed)
    out = list(items)
    rr.shuffle(out)
    return out

def pick_featured(items: list[str], n: int, scope: str) -> list[str]:
    if not items:
        return []
    seed = _seed_int(BASE_URL, SITE_VARIANT, scope, "featured", BUILD_NONCE)
    rr = random.Random(seed)
    pool = list(items)
    rr.shuffle(pool)
    return pool[: max(0, min(n, len(pool)))]

# ---------------------------
# Theme (unique color system)
# ---------------------------
def _hsl_to_hex(h: float, s: float, l: float) -> str:
    # h: 0..360, s/l: 0..1
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs(((h / 60.0) % 2) - 1))
    m = l - c / 2
    r = g = b = 0.0
    if 0 <= h < 60:
        r, g, b = c, x, 0
    elif 60 <= h < 120:
        r, g, b = x, c, 0
    elif 120 <= h < 180:
        r, g, b = 0, c, x
    elif 180 <= h < 240:
        r, g, b = 0, x, c
    elif 240 <= h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x
    r = int(round((r + m) * 255))
    g = int(round((g + m) * 255))
    b = int(round((b + m) * 255))
    return "#{:02x}{:02x}{:02x}".format(max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))

def theme_vars() -> dict:
    base_seed = _seed_int(BASE_URL, SITE_VARIANT, "theme")
    # create stable hue from seed, then nudge by variant
    hue = (base_seed % 36000) / 100.0
    variant_bias = {
        "github-pages": 8.0,
        "netlify": 140.0,
        "vercel": 260.0,
        "cloudflare": 32.0,
    }.get(SITE_VARIANT, 0.0)

    h1 = (hue + variant_bias) % 360.0
    h2 = (h1 + 70.0) % 360.0

    accent = _hsl_to_hex(h1, 0.78, 0.58)
    accent2 = _hsl_to_hex(h2, 0.70, 0.52)
    link = _hsl_to_hex((h1 + 20.0) % 360.0, 0.85, 0.70)

    # slightly different backgrounds per variant
    bg = "#0b1220"
    card = "rgba(17,26,43,0.72)"
    line = "rgba(255,255,255,0.12)"
    text = "#e5e7eb"
    muted = "#9ca3af"

    fonts = [
        'ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, "Noto Sans", "Liberation Sans", sans-serif',
        'ui-sans-serif, system-ui, -apple-system, "Segoe UI Variable", Segoe UI, Roboto, Arial, sans-serif',
        'ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif',
    ]
    f = fonts[base_seed % len(fonts)]

    # gradient fields vary per cloud
    g1 = f"radial-gradient(1200px 700px at 18% -12%, {accent}33, transparent 52%)"
    g2 = f"radial-gradient(1000px 600px at 112% 24%, {accent2}2e, transparent 48%)"
    g3 = f"radial-gradient(900px 520px at 46% 112%, {accent}18, transparent 55%)"

    return {
        "bg": bg,
        "text": text,
        "muted": muted,
        "line": line,
        "link": link,
        "accent": accent,
        "accent2": accent2,
        "card": card,
        "font": f,
        "g1": g1,
        "g2": g2,
        "g3": g3,
    }

# ---------------------------
# Schema
# ---------------------------
def itemlist_schema(title: str, urls: list[str], built_utc: str) -> str:
    schema = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": title,
        "description": f"Curated list updated on {built_utc} UTC",
        "numberOfItems": len(urls),
        "itemListElement": [
            {"@type": "ListItem", "position": i, "url": u}
            for i, u in enumerate(urls, start=1)
        ],
    }
    return json.dumps(schema, ensure_ascii=False)

def website_schema() -> str:
    data = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": SITE_NAME,
        "url": (BASE_URL or "").strip() or None,
    }
    return json.dumps({k: v for k, v in data.items() if v is not None}, ensure_ascii=False)

# ---------------------------
# HTTP helpers (Gemini + IndexNow)
# ---------------------------
def _http_post_json(url: str, payload: dict, headers: dict, timeout: float = 18.0) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return int(getattr(resp, "status", 200)), body
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        return int(e.code), body
    except URLError as e:
        return 0, str(e)
    except Exception as e:
        return 0, str(e)

def _http_get(url: str, timeout: float = 12.0) -> tuple[int, str]:
    req = Request(url, method="GET")
    req.add_header("User-Agent", UA)
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return int(getattr(resp, "status", 200)), body
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        return int(e.code), body
    except Exception as e:
        return 0, str(e)

def _days_old(utc_z: str) -> int:
    try:
        t = dt.datetime.strptime(utc_z, "%Y-%m-%dT%H:%M:%SZ")
        return (dt.datetime.utcnow() - t).days
    except Exception:
        return 999999

# ---------------------------
# URL enrichment cache (kept, improved prompt)
# ---------------------------
def _load_enrich_cache() -> dict:
    if not ENRICH_CACHE_FILE.exists():
        return {"version": 1, "items": {}}
    try:
        return json.loads(ENRICH_CACHE_FILE.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {"version": 1, "items": {}}

def _save_enrich_cache(cache: dict):
    ENRICH_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENRICH_CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

def _guess_kind(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "video"
    if "soundcloud.com" in host:
        return "audio"
    if "github.com" in host:
        return "repository"
    if "slideshare.net" in host:
        return "presentation"
    if "quora.com" in host:
        return "qa"
    if "trustpilot." in host:
        return "review"
    if "about.me" in host:
        return "profile"
    return "website"

def _fetch_basic_meta(url: str, timeout: float = 12.0) -> dict:
    req = Request(url, method="GET")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200))
            raw = resp.read(220_000)
            ctype = (resp.headers.get("Content-Type") or "").lower()
    except Exception as e:
        return {"http_status": 0, "error": str(e), "title": "", "description": "", "content_type": ""}

    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        text = raw.decode("latin-1", errors="ignore")

    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if m:
        title = html.unescape(re.sub(r"\s+", " ", m.group(1)).strip())

    desc = ""
    m = re.search(r'<meta[^>]+name=["\']description["\'][^>]*content=["\'](.*?)["\']', text, re.I | re.S)
    if not m:
        m = re.search(r'<meta[^>]+content=["\'](.*?)["\'][^>]*name=["\']description["\']', text, re.I | re.S)
    if m:
        desc = html.unescape(re.sub(r"\s+", " ", m.group(1)).strip())

    return {
        "http_status": status,
        "title": title[:180],
        "description": desc[:320],
        "content_type": ctype[:120],
    }

def _gemini_json(prompt: str) -> dict:
    if not GEMINI_API_KEY:
        return {}
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.35,
            "maxOutputTokens": 340,
            "responseMimeType": "application/json",
        },
    }
    status, body = _http_post_json(endpoint, payload, headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=18.0)
    if status < 200 or status >= 300:
        return {}
    try:
        obj = json.loads(body)
        txt = (
            obj.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        ).strip()
        if not txt:
            return {}
        out = json.loads(txt)
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}

def _gemini_summary(url: str, kind: str, title: str, description: str) -> dict:
    prompt = (
        "You write short neutral directory summaries for a link hub.\n"
        "Rules:\n"
        "- Neutral, factual tone. No hype words.\n"
        "- Do not claim official, verified, guaranteed.\n"
        "- Summary: 2 to 3 short sentences, max 360 characters.\n"
        "- Title: max 120 characters.\n"
        "- topics: 1 to 5 items, short.\n"
        "- Return only JSON with keys: title, summary, topics.\n\n"
        f"URL: {url}\n"
        f"Type: {kind}\n"
        f"Title signal: {title}\n"
        f"Description signal: {description}\n"
    )
    out = _gemini_json(prompt)
    t = (out.get("title") or title or "").strip()[:120]
    s = (out.get("summary") or "").strip()[:380]
    topics = out.get("topics") or []
    if not isinstance(topics, list):
        topics = []
    topics = [str(x).strip()[:32] for x in topics if str(x).strip()][:5]
    if not s:
        return {}
    return {"title": t, "summary": s, "topics": topics}

def enrich_urls(target_urls: list[str]) -> dict[str, dict]:
    cache = _load_enrich_cache()
    items = cache.get("items") or {}
    if not isinstance(items, dict):
        items = {}
        cache["items"] = items

    ai_calls_left = MAX_AI_CALLS

    for u in target_urls:
        if not URL_RE.match(u):
            continue

        kind = _guess_kind(u)
        cur = items.get(u) or {}
        fetched_utc = (cur.get("fetched_utc") or "").strip()

        needs_refresh = True
        if fetched_utc:
            needs_refresh = _days_old(fetched_utc) >= ENRICH_TTL_DAYS

        if not needs_refresh:
            continue

        basic = _fetch_basic_meta(u)
        title = (basic.get("title") or "").strip()
        desc = (basic.get("description") or "").strip()

        out = {
            "url": u,
            "kind": kind,
            "http_status": int(basic.get("http_status") or 0),
            "title": title[:180],
            "description": desc[:320],
            "summary": "",
            "topics": [],
            "fetched_utc": utc_now_iso_z(),
        }

        if ENABLE_AI and GEMINI_API_KEY and ai_calls_left > 0:
            ai_calls_left -= 1
            ai = _gemini_summary(u, kind, title, desc)
            if ai:
                out["title"] = (ai.get("title") or out["title"] or "").strip()[:180]
                out["summary"] = (ai.get("summary") or "").strip()[:420]
                out["topics"] = ai.get("topics") or []

        # fallback if AI off or empty
        if not out["summary"]:
            if desc:
                out["summary"] = desc[:360]
            elif title:
                out["summary"] = title[:260]
            else:
                out["summary"] = ""

        items[u] = out

        # small polite delay when AI is on
        if ENABLE_AI and GEMINI_API_KEY:
            time.sleep(0.08)

    cache["generated_utc"] = utc_now_iso_z()
    _save_enrich_cache(cache)
    return items

# ---------------------------
# Site blurb (per BASE_URL, cached)
# ---------------------------
def _load_site_blurb_cache() -> dict:
    if not SITE_BLURB_CACHE_FILE.exists():
        return {"version": 1, "items": {}}
    try:
        return json.loads(SITE_BLURB_CACHE_FILE.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {"version": 1, "items": {}}

def _save_site_blurb_cache(cache: dict):
    SITE_BLURB_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SITE_BLURB_CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

def _fallback_site_blurb() -> dict:
    label = SITE_VARIANT or "site"
    b = f"{SITE_NAME} is a small link hub that lists recently added URLs. This build is labeled {label}."
    m = f"{SITE_NAME} lists recently added links with simple summaries. Build label: {label}."
    return {"blurb": b[:420], "meta_description": m[:160], "fetched_utc": utc_now_iso_z()}

def _gemini_site_blurb() -> dict:
    if not GEMINI_API_KEY:
        return {}
    prompt = (
        "Write an ABOUT blurb for a link hub website.\n"
        "Rules:\n"
        "- Neutral, Google-friendly, no hype.\n"
        "- blurb: 2 short paragraphs, total max 520 characters.\n"
        "- meta_description: max 155 characters.\n"
        "- Avoid claims like official, verified, guaranteed.\n"
        "- Return only JSON keys: blurb, meta_description.\n\n"
        f"Site name: {SITE_NAME}\n"
        f"Build label: {SITE_VARIANT}\n"
        f"Site base URL: {BASE_URL}\n"
    )
    out = _gemini_json(prompt)
    blurb = (out.get("blurb") or "").strip()
    meta = (out.get("meta_description") or "").strip()
    if not blurb or not meta:
        return {}
    return {
        "blurb": blurb[:560],
        "meta_description": meta[:160],
        "fetched_utc": utc_now_iso_z(),
    }

def get_site_blurb() -> dict:
    if not ENABLE_SITE_BLURB:
        return _fallback_site_blurb()

    cache = _load_site_blurb_cache()
    items = cache.get("items") or {}
    if not isinstance(items, dict):
        items = {}
        cache["items"] = items

    key = (BASE_URL or "").strip() or (SITE_VARIANT or "local")
    cur = items.get(key) or {}
    fetched_utc = (cur.get("fetched_utc") or "").strip()

    if fetched_utc and _days_old(fetched_utc) < SITE_BLURB_TTL_DAYS:
        return cur

    if GEMINI_API_KEY:
        g = _gemini_site_blurb()
        if g:
            items[key] = g
            cache["generated_utc"] = utc_now_iso_z()
            _save_site_blurb_cache(cache)
            return g

    fb = _fallback_site_blurb()
    items[key] = fb
    cache["generated_utc"] = utc_now_iso_z()
    _save_site_blurb_cache(cache)
    return fb

# ---------------------------
# Rendering helpers
# ---------------------------
def esc(s: str) -> str:
    return html.escape(s or "", quote=True)

def render_topics(topics: list[str]) -> str:
    if not topics:
        return ""
    chips = []
    for t in topics[:5]:
        tt = esc(str(t).strip())
        if not tt:
            continue
        chips.append(f"<span class='chip'>{tt}</span>")
    return "<div class='chips'>" + "".join(chips) + "</div>" if chips else ""

def page_css() -> str:
    tv = theme_vars()
    return f"""
    :root {{
      --bg: {tv["bg"]};
      --text: {tv["text"]};
      --muted: {tv["muted"]};
      --line: {tv["line"]};
      --link: {tv["link"]};
      --accent: {tv["accent"]};
      --accent2: {tv["accent2"]};
      --card: {tv["card"]};
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: {tv["font"]};
      background: {tv["g1"]}, {tv["g2"]}, {tv["g3"]}, var(--bg);
      color: var(--text);
    }}
    .wrap {{ max-width: 1120px; margin: 0 auto; padding: 28px 18px 52px; }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 18px 18px;
      backdrop-filter: blur(10px);
    }}
    .topbar {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      margin: 2px 0 10px;
    }}
    h1 {{ margin: 0; font-size: 22px; letter-spacing: 0.2px; }}
    .subtle {{ color: var(--muted); font-size: 12px; }}
    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px 14px;
      align-items: center;
      margin: 8px 0 12px;
      color: var(--muted);
      font-size: 13px;
    }}
    .meta a {{ color: var(--link); text-decoration: none; }}
    .meta a:hover {{ text-decoration: underline; }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(0,0,0,0.18);
      white-space: nowrap;
    }}
    .blurb {{
      margin: 8px 0 14px;
      color: #cbd5e1;
      font-size: 13px;
      line-height: 1.6;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 12px;
      margin: 10px 0 14px;
    }}
    .panel {{
      grid-column: span 12;
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 14px;
      background: rgba(0,0,0,0.14);
      padding: 12px;
    }}
    .panel h2 {{
      margin: 0 0 8px;
      font-size: 14px;
      letter-spacing: 0.2px;
    }}
    .stats {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 6px;
    }}
    .stat {{
      padding: 8px 10px;
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 12px;
      background: rgba(0,0,0,0.10);
      font-size: 12px;
      color: #cbd5e1;
    }}
    .featured {{
      grid-column: span 12;
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 12px;
      margin-top: 10px;
    }}
    .fcard {{
      grid-column: span 12;
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 14px;
      background: rgba(0,0,0,0.14);
      padding: 12px;
    }}
    @media (min-width: 820px) {{
      .fcard {{ grid-column: span 6; }}
      .panel {{ grid-column: span 12; }}
    }}
    @media (min-width: 1020px) {{
      .fcard {{ grid-column: span 4; }}
    }}
    .f-title {{
      font-size: 13px;
      font-weight: 650;
      margin: 0 0 6px;
    }}
    .f-title a {{ color: var(--link); text-decoration: none; }}
    .f-title a:hover {{ text-decoration: underline; }}
    .f-summ {{
      font-size: 12px;
      color: #cbd5e1;
      line-height: 1.45;
      margin: 0 0 8px;
    }}
    .f-url {{
      font-size: 11px;
      color: var(--muted);
      word-break: break-all;
    }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
    .chip {{
      font-size: 11px;
      padding: 4px 8px;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.12);
      background: rgba(0,0,0,0.16);
      color: #d1d5db;
    }}
    .controls {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      justify-content: space-between;
      margin: 10px 0 10px;
    }}
    .search {{
      display: flex;
      gap: 8px;
      align-items: center;
      width: min(520px, 100%);
    }}
    .search input {{
      width: 100%;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,0.14);
      background: rgba(0,0,0,0.18);
      color: var(--text);
      outline: none;
    }}
    .search input::placeholder {{ color: rgba(229,231,235,0.55); }}
    .hint {{ font-size: 12px; color: var(--muted); }}
    .table-wrap {{
      overflow: auto;
      border-radius: 12px;
      border: 1px solid var(--line);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 820px;
      background: rgba(0,0,0,0.16);
    }}
    thead th {{
      text-align: left;
      font-size: 12px;
      color: var(--muted);
      font-weight: 650;
      padding: 10px 10px;
      border-bottom: 1px solid var(--line);
      position: sticky;
      top: 0;
      background: rgba(17,26,43,0.92);
      backdrop-filter: blur(10px);
    }}
    tbody td {{
      padding: 10px 10px;
      border-bottom: 1px solid rgba(255,255,255,0.08);
      vertical-align: top;
      font-size: 13px;
      line-height: 1.35;
    }}
    tbody tr:hover td {{ background: rgba(255,255,255,0.04); }}
    td.num {{ width: 62px; color: var(--muted); }}
    td.url a {{ color: var(--link); word-break: break-all; }}
    td.host {{ width: 230px; color: #d1d5db; }}
    td.path {{ color: #d1d5db; word-break: break-word; }}
    .meta-row td {{ padding-top: 0; }}
    .u-meta {{
      padding: 10px 12px;
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 12px;
      background: rgba(0,0,0,0.10);
      margin: 0 0 10px;
    }}
    .u-title {{ font-size: 13px; color: #e5e7eb; font-weight: 650; margin-bottom: 4px; }}
    .u-summ {{ font-size: 12px; color: #cbd5e1; margin-bottom: 6px; line-height: 1.5; }}
    .u-extra {{ font-size: 11px; color: var(--muted); }}
    .footer {{ margin-top: 14px; color: var(--muted); font-size: 12px; }}
    code {{ color: #d1fae5; }}
    .navlinks a {{
      color: var(--link);
      text-decoration: none;
      margin-right: 10px;
      font-size: 12px;
    }}
    .navlinks a:hover {{ text-decoration: underline; }}
    """

def render_head(title: str, canonical: str, meta_description: str, schema_json: str, extra_schema_json: str = "") -> str:
    sd = (meta_description or "").strip()
    if not sd:
        sd = f"{SITE_NAME} lists recently added links with simple summaries."
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{esc(title)}</title>
  <meta name="description" content="{esc(sd[:160])}" />
  <meta name="robots" content="index,follow" />
  <link rel="canonical" href="{esc(canonical)}" />
  <meta property="og:title" content="{esc(title[:70])}" />
  <meta property="og:description" content="{esc(sd[:200])}" />
  <meta property="og:type" content="website" />
  <script type="application/ld+json">{schema_json}</script>
  <script type="application/ld+json">{extra_schema_json or website_schema()}</script>
  <style>{page_css()}</style>
</head>
"""

def render_top_meta(url_count: int, built_utc: str, today_path: str, site_base: str) -> str:
    nav = (
        f"<div class='navlinks'>"
        f"<a href='{esc(abs_url('/all.html'))}'>all</a>"
        f"<a href='{esc(abs_url('/sitemap.xml'))}'>sitemap</a>"
        f"<a href='{esc(abs_url('/rss.xml'))}'>rss</a>"
        f"<a href='{esc(abs_url('/about.html'))}'>about</a>"
        f"<a href='{esc(abs_url('/status.html'))}'>status</a>"
        f"</div>"
    )
    return (
        f"<div class='topbar'>"
        f"<h1>{esc(SITE_NAME)}</h1>"
        f"<div class='subtle'>{esc(SITE_VARIANT or '')}</div>"
        f"</div>"
        f"{nav}"
        f"<div class='meta'>"
        f"<span class='badge'>URLs: <strong style='color:var(--text)'>{url_count}</strong></span>"
        f"<span class='badge'>Built: <strong style='color:var(--text)'>{esc(built_utc)} UTC</strong></span>"
        f"<span class='badge'><a href='{esc(abs_url('/' + today_path))}'>d/{esc(today_path.split('/')[-1])}</a></span>"
        f"<span class='badge'><a href='{esc(abs_url('/sitemap.xml'))}'>sitemap.xml</a></span>"
        f"<span class='badge'><a href='{esc(abs_url('/rss.xml'))}'>rss.xml</a></span>"
        f"<span class='badge'><a href='{esc(abs_url('/robots.txt'))}'>robots.txt</a></span>"
        f"</div>"
    )

def render_feature_cards(featured_urls: list[str], enrich: dict[str, dict]) -> str:
    cards = []
    for u in featured_urls:
        e = enrich.get(u) or {}
        title = (e.get("title") or "").strip() or u
        summ = (e.get("summary") or "").strip()
        kind = (e.get("kind") or "").strip()
        topics = e.get("topics") or []
        if not isinstance(topics, list):
            topics = []
        chips = render_topics([str(x) for x in topics]) if topics else ""
        cards.append(
            f"<div class='fcard'>"
            f"<div class='f-title'><a href='{esc(u)}' target='_blank' rel='noopener'>{esc(title[:120])}</a></div>"
            f"<div class='f-summ'>{esc(summ[:300])}</div>"
            f"{chips}"
            f"<div class='f-url'>{esc(kind)} | {esc(u)}</div>"
            f"</div>"
        )
    if not cards:
        return ""
    return "<div class='featured'>" + "".join(cards) + "</div>"

def render_table(urls: list[str], enrich: dict[str, dict]) -> str:
    rows = []
    n = 0
    for idx, u in enumerate(urls, start=1):
        host, path = host_and_path(u)
        rows.append(
            f"<tr class='data-row'>"
            f"<td class='num'>{idx}</td>"
            f"<td class='url'><a href='{esc(u)}' target='_blank' rel='noopener'>{esc(u)}</a></td>"
            f"<td class='host'>{esc(host)}</td>"
            f"<td class='path'>{esc(path)}</td>"
            f"</tr>"
        )
        e = enrich.get(u) or {}
        title = (e.get("title") or "").strip()
        summ = (e.get("summary") or "").strip()
        kind = (e.get("kind") or "").strip()
        fetched = (e.get("fetched_utc") or "").strip()
        topics = e.get("topics") or []
        if not isinstance(topics, list):
            topics = []

        if title or summ:
            chips = render_topics([str(x) for x in topics]) if topics else ""
            rows.append(
                f"<tr class='meta-row'>"
                f"<td class='num'></td>"
                f"<td colspan='3'>"
                f"<div class='u-meta'>"
                f"<div class='u-title'>{esc(title[:180])}</div>"
                f"<div class='u-summ'>{esc(summ[:420])}</div>"
                f"{chips}"
                f"<div class='u-extra'>{esc(kind)}"
                + (f" | {esc(fetched)}" if fetched else "")
                + f"</div>"
                f"</div>"
                f"</td>"
                f"</tr>"
            )
        n += 1
    return (
        "<div class='controls'>"
        "<div class='search'><input id='q' type='search' placeholder='Filter by host, title, or URL' autocomplete='off' /></div>"
        "<div class='hint'>Type to filter the list</div>"
        "</div>"
        "<div class='table-wrap'>"
        "<table>"
        "<thead><tr><th>#</th><th>URL</th><th>Host</th><th>Path</th></tr></thead>"
        "<tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
        "<script>"
        "const q=document.getElementById('q');"
        "function norm(s){return (s||'').toLowerCase();}"
        "q.addEventListener('input',()=>{"
        "const v=norm(q.value);"
        "const rows=[...document.querySelectorAll('tr.data-row')];"
        "for(const r of rows){"
        "const txt=norm(r.innerText);"
        "const show = !v || txt.includes(v);"
        "r.style.display = show ? '' : 'none';"
        "let nxt=r.nextElementSibling;"
        "if(nxt && nxt.classList.contains('meta-row')) nxt.style.display = show ? '' : 'none';"
        "}"
        "});"
        "</script>"
    )

# ---------------------------
# Page builders
# ---------------------------
def build_main_pages(history: list[tuple[str, str]], input_urls: list[str], enrich: dict[str, dict], site_blurb: dict, built_utc: str):
    today = utc_today_iso()
    grouped = group_by_date(history)
    today_urls = grouped.get(today, [])

    # Variation: different ordering per page and per cloud
    all_urls_unique = dedupe_preserve_order([u for _, u in history])
    all_for_display = shuffle_for_site(all_urls_unique, "all-display")[:MAX_ALL_LIST]
    index_for_display = shuffle_for_site(all_urls_unique, "index-display")[:MAX_ALL_LIST]

    # For daily pages, shuffle but keep within that day
    today_display = shuffle_for_site(list(today_urls), f"day-display-{today}")

    # Featured selection (varies by cloud + site)
    featured = pick_featured(all_urls_unique, 6, f"featured-{today}")
    featured_cards = render_feature_cards(featured, enrich)

    # Stats
    hosts = set()
    for u in all_urls_unique:
        h, _ = host_and_path(u)
        if h:
            hosts.add(h.lower())
    host_count = len(hosts)
    url_count = len(all_urls_unique)

    # today page path
    today_page_path = f"d/{today}.html"

    # index.html
    idx_schema_urls = shuffle_for_site(list(all_for_display), "index-schema")
    idx_schema = itemlist_schema(SITE_NAME, idx_schema_urls, built_utc)
    idx_head = render_head(
        SITE_NAME,
        abs_url("/"),
        site_blurb.get("meta_description") or "",
        idx_schema,
    )
    idx_body = (
        "<body><div class='wrap'><div class='card'>"
        + render_top_meta(url_count, built_utc, today_page_path, BASE_URL)
        + f"<div class='blurb'>{esc(site_blurb.get('blurb') or '')}</div>"
        + "<div class='grid'>"
        + "<div class='panel'>"
        + "<h2>Snapshot</h2>"
        + "<div class='stats'>"
        + f"<div class='stat'><strong>{url_count}</strong> URLs</div>"
        + f"<div class='stat'><strong>{host_count}</strong> unique hosts</div>"
        + f"<div class='stat'>Build label: <strong>{esc(SITE_VARIANT or 'site')}</strong></div>"
        + "</div>"
        + featured_cards
        + "</div>"
        + "</div>"
        + render_table(index_for_display, enrich)
        + "<div class='footer'>Generated from <code>data/daily.csv</code> and stored in <code>data/history.csv</code>.</div>"
        + "</div></div></body></html>"
    )
    write_text(DOCS_DIR / "index.html", idx_head + idx_body)

    # all.html
    all_schema_urls = shuffle_for_site(list(all_for_display), "all-schema")
    all_schema = itemlist_schema(SITE_NAME, all_schema_urls, built_utc)
    all_head = render_head(
        SITE_NAME,
        abs_url("/all.html"),
        site_blurb.get("meta_description") or "",
        all_schema,
    )
    all_body = (
        "<body><div class='wrap'><div class='card'>"
        + f"<div class='topbar'><h1>{esc(SITE_NAME)}</h1><div class='subtle'>{esc(SITE_VARIANT or '')}</div></div>"
        + "<div class='meta'>"
        + f"<span class='badge'>URLs: <strong style='color:var(--text)'>{url_count}</strong></span>"
        + f"<span class='badge'>Built: <strong style='color:var(--text)'>{esc(built_utc)} UTC</strong></span>"
        + f"<span class='badge'><a href='{esc(abs_url('/' + today_page_path))}'>{esc(today_page_path)}</a></span>"
        + f"<span class='badge'><a href='{esc(abs_url('/'))}'>home</a></span>"
        + f"<span class='badge'><a href='{esc(abs_url('/sitemap.xml'))}'>sitemap.xml</a></span>"
        + f"<span class='badge'><a href='{esc(abs_url('/rss.xml'))}'>rss.xml</a></span>"
        + "</div>"
        + f"<div class='blurb'>{esc(site_blurb.get('blurb') or '')}</div>"
        + render_table(all_for_display, enrich)
        + "<div class='footer'>Full list view (display order varies by deployment).</div>"
        + "</div></div></body></html>"
    )
    write_text(DOCS_DIR / "all.html", all_head + all_body)

    # daily pages
    for day, day_urls in grouped.items():
        # daily list based on first-seen, but display order varies per deployment
        day_unique = dedupe_preserve_order(day_urls)
        day_display = shuffle_for_site(list(day_unique), f"day-display-{day}")
        title = f"{SITE_NAME} {day}"
        schema_urls = shuffle_for_site(list(day_display), f"day-schema-{day}")
        schema = itemlist_schema(title, schema_urls, built_utc)
        head = render_head(
            title,
            abs_url(f"/d/{day}.html"),
            site_blurb.get("meta_description") or "",
            schema,
        )
        body = (
            "<body><div class='wrap'><div class='card'>"
            + f"<div class='topbar'><h1>{esc(title)}</h1><div class='subtle'>{esc(SITE_VARIANT or '')}</div></div>"
            + "<div class='meta'>"
            + f"<span class='badge'>URLs: <strong style='color:var(--text)'>{len(day_unique)}</strong></span>"
            + f"<span class='badge'>Built: <strong style='color:var(--text)'>{esc(built_utc)} UTC</strong></span>"
            + f"<span class='badge'><a href='{esc(abs_url('/all.html'))}'>all.html</a></span>"
            + f"<span class='badge'><a href='{esc(abs_url('/'))}'>home</a></span>"
            + f"<span class='badge'><a href='{esc(abs_url('/sitemap.xml'))}'>sitemap.xml</a></span>"
            + f"<span class='badge'><a href='{esc(abs_url('/rss.xml'))}'>rss.xml</a></span>"
            + "</div>"
            + f"<div class='blurb'>{esc(site_blurb.get('blurb') or '')}</div>"
            + render_table(day_display, enrich)
            + "<div class='footer'>Generated from <code>data/daily.csv</code> and stored in <code>data/history.csv</code>.</div>"
            + "</div></div></body></html>"
        )
        write_text(DAILY_DIR / f"{day}.html", head + body)

def build_static_pages(site_blurb: dict, built_utc: str):
    about_schema = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "AboutPage",
            "name": f"About {SITE_NAME}",
            "url": abs_url("/about.html"),
        },
        ensure_ascii=False,
    )
    head = render_head(
        f"About | {SITE_NAME}",
        abs_url("/about.html"),
        site_blurb.get("meta_description") or "",
        about_schema,
    )
    repo_line = f"<div class='stat'>Source: <a href='{esc(REPO_URL)}' target='_blank' rel='noopener'>{esc(REPO_URL)}</a></div>" if REPO_URL else ""
    body = (
        "<body><div class='wrap'><div class='card'>"
        f"<div class='topbar'><h1>About</h1><div class='subtle'>{esc(SITE_VARIANT or '')}</div></div>"
        f"<div class='blurb'>{esc(site_blurb.get('blurb') or '')}</div>"
        "<div class='panel'>"
        "<h2>What this hub does</h2>"
        "<div class='u-summ'>This site publishes a simple directory of links that were added to a daily list. Each deployment can present the same set of links with its own layout details and ordering.</div>"
        "<div class='stats'>"
        f"<div class='stat'>Built (UTC): <strong>{esc(built_utc)}</strong></div>"
        f"<div class='stat'>Build label: <strong>{esc(SITE_VARIANT or 'site')}</strong></div>"
        + repo_line +
        "</div>"
        "</div>"
        "<div class='panel'>"
        "<h2>Notes</h2>"
        "<div class='u-summ'>Links point to third party pages. Titles and summaries are taken from public page signals when available.</div>"
        "</div>"
        "<div class='footer'><a href='" + esc(abs_url("/")) + "'>Back to home</a></div>"
        "</div></div></body></html>"
    )
    write_text(DOCS_DIR / "about.html", head + body)

    status_schema = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "WebPage",
            "name": f"Status | {SITE_NAME}",
            "url": abs_url("/status.html"),
        },
        ensure_ascii=False,
    )
    head2 = render_head(
        f"Status | {SITE_NAME}",
        abs_url("/status.html"),
        site_blurb.get("meta_description") or "",
        status_schema,
    )
    body2 = (
        "<body><div class='wrap'><div class='card'>"
        f"<div class='topbar'><h1>Status</h1><div class='subtle'>{esc(SITE_VARIANT or '')}</div></div>"
        "<div class='panel'>"
        "<h2>Build information</h2>"
        "<div class='stats'>"
        f"<div class='stat'>Base URL: <strong>{esc(BASE_URL or '(relative)')}</strong></div>"
        f"<div class='stat'>Docs dir: <strong>{esc(str(DOCS_DIR))}</strong></div>"
        f"<div class='stat'>Built (UTC): <strong>{esc(built_utc)}</strong></div>"
        "</div>"
        "</div>"
        "<div class='panel'>"
        "<h2>Files</h2>"
        "<div class='u-summ'>home, all, daily pages, sitemap, RSS, robots, and IndexNow key file (if enabled).</div>"
        "</div>"
        "<div class='footer'><a href='" + esc(abs_url("/")) + "'>Back to home</a></div>"
        "</div></div></body></html>"
    )
    write_text(DOCS_DIR / "status.html", head2 + body2)

def build_robots():
    sitemap_url = abs_url("/sitemap.xml")
    content = "User-agent: *\nAllow: /\nSitemap: " + sitemap_url + "\n"
    write_text(DOCS_DIR / "robots.txt", content)

def build_sitemap(page_urls: list[str], built_utc: str):
    # sitemap contains only local pages, not external links
    items = []
    for u in page_urls:
        loc = xml_escape(u)
        items.append(f"<url><loc>{loc}</loc><lastmod>{built_utc}Z</lastmod></url>")
    xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">\n"
        + "\n".join(items)
        + "\n</urlset>\n"
    )
    write_text(DOCS_DIR / "sitemap.xml", xml)

def build_rss(external_urls: list[str], built_rfc2822: str):
    # RSS for external URLs (recent first)
    items = []
    for u in external_urls[:MAX_RSS_ITEMS]:
        esc_u = xml_escape(u)
        items.append(
            "<item>"
            f"<title>{esc_u}</title>"
            f"<link>{esc_u}</link>"
            f"<guid isPermaLink='true'>{esc_u}</guid>"
            f"<pubDate>{built_rfc2822}</pubDate>"
            f"<description>{esc_u}</description>"
            "</item>"
        )

    xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<rss version='2.0'>\n"
        "<channel>\n"
        f"<title>{xml_escape(SITE_NAME)} Feed</title>\n"
        f"<link>{xml_escape(abs_url('/all.html'))}</link>\n"
        "<description>Recent URLs added</description>\n"
        f"<lastBuildDate>{built_rfc2822}</lastBuildDate>\n"
        + "\n".join(items)
        + "\n</channel>\n</rss>\n"
    )
    write_text(DOCS_DIR / "rss.xml", xml)

def build_backlink_feed(external_urls: list[str], built_utc: str):
    # lightweight XML file ping target (kept simple)
    items = []
    for u in external_urls[:MAX_RSS_ITEMS]:
        items.append(f"<link>{xml_escape(u)}</link>")
    xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<links>\n"
        f"<updated>{xml_escape(built_utc)}Z</updated>\n"
        + "\n".join(items)
        + "\n</links>\n"
    )
    write_text(DOCS_DIR / "backlink-feed.xml", xml)

# ---------------------------
# IndexNow + Pingomatic
# ---------------------------
def ensure_indexnow_key() -> str:
    if INDEXNOW_KEY_FILE.exists():
        k = INDEXNOW_KEY_FILE.read_text(encoding="utf-8", errors="ignore").strip()
        if k:
            return k
    k = secrets.token_hex(16)
    INDEXNOW_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    INDEXNOW_KEY_FILE.write_text(k + "\n", encoding="utf-8", newline="\n")
    return k

def write_indexnow_key_file_to_site(key: str):
    # IndexNow requires a key file accessible under site root, named {key}.txt
    write_text(DOCS_DIR / f"{key}.txt", key + "\n")

def submit_indexnow(site_pages: list[str]):
    if not ENABLE_INDEXNOW:
        return
    if not BASE_URL:
        return

    key = ensure_indexnow_key()
    write_indexnow_key_file_to_site(key)

    try:
        host = urlparse(BASE_URL).netloc
    except Exception:
        host = ""

    if not host:
        return

    key_location = abs_url(f"/{key}.txt")
    payload = {
        "host": host,
        "key": key,
        "keyLocation": key_location,
        "urlList": list(dict.fromkeys(site_pages))[:10000],
    }

    # API endpoint that routes to participating engines
    endpoint = "https://api.indexnow.org/indexnow"
    status, body = _http_post_json(endpoint, payload, headers={}, timeout=18.0)

    # best-effort, no hard fail
    _ = (status, body)

def ping_pingomatic():
    if not ENABLE_PINGOMATIC:
        return
    if not BASE_URL:
        return
    try:
        proxy = xmlrpc.client.ServerProxy("http://rpc.pingomatic.com/", allow_none=True)
        # Ping the backlink-feed.xml (stable file)
        proxy.weblogUpdates.ping(SITE_NAME, abs_url("/backlink-feed.xml"))
    except Exception:
        pass

# ---------------------------
# Main
# ---------------------------
def main():
    ensure_dirs()
    ensure_nojekyll()

    built_utc = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    built_iso_z = utc_now_iso_z().replace("T", " ").replace("Z", "")
    built_rfc = utc_now_rfc2822()

    # Load input, update history
    input_urls = dedupe_preserve_order(read_input_urls())
    today = utc_today_iso()
    history = update_history_with_today(input_urls, today)

    # Enrich URLs (uses cache)
    all_urls_unique = dedupe_preserve_order([u for _, u in history])
    enrich = enrich_urls(all_urls_unique)

    # Site blurb (per BASE_URL)
    site_blurb = get_site_blurb()

    # Pages
    build_main_pages(history, input_urls, enrich, site_blurb, built_iso_z)
    build_static_pages(site_blurb, built_iso_z)
    build_robots()

    # Local pages list for sitemap + indexnow
    local_pages = []
    local_pages.append(abs_url("/"))
    local_pages.append(abs_url("/index.html"))
    local_pages.append(abs_url("/all.html"))
    local_pages.append(abs_url("/sitemap.xml"))
    local_pages.append(abs_url("/rss.xml"))
    local_pages.append(abs_url("/robots.txt"))
    local_pages.append(abs_url("/about.html"))
    local_pages.append(abs_url("/status.html"))
    local_pages.append(abs_url("/backlink-feed.xml"))

    # daily pages
    grouped = group_by_date(history)
    for day in grouped.keys():
        local_pages.append(abs_url(f"/d/{day}.html"))

    # sitemap uses ISO Z time
    built_lastmod = utc_now_iso_z()
    build_sitemap(local_pages, built_lastmod.replace("Z", ""))

    # RSS + backlink feed use external urls
    recent_external = list(reversed(all_urls_unique))  # newest last in history, reverse => newest first
    build_rss(recent_external, built_rfc)
    build_backlink_feed(recent_external, built_lastmod.replace("Z", ""))

    # Broadcast
    submit_indexnow(local_pages)
    ping_pingomatic()

if __name__ == "__main__":
    main()

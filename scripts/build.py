#!/usr/bin/env python3
import os
import re
import csv
import json
import html
import secrets
import datetime as dt
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from xml.sax.saxutils import escape as xml_escape

# ---------------------------
# Paths (DOCS_DIR now configurable per deploy)
# ---------------------------
DATA_FILE = Path("data/daily.csv")
HISTORY_FILE = Path("data/history.csv")
INDEXNOW_KEY_FILE = Path("data/indexnow.key")
ENRICH_CACHE_FILE = Path("data/enriched.json")
SITE_BLURB_CACHE_FILE = Path("data/site_blurbs.json")

DOCS_DIR = Path(os.environ.get("DOCS_DIR", "docs")).resolve()
DAILY_DIR = DOCS_DIR / "d"

MAX_ALL_LIST = int(os.environ.get("MAX_ALL_LIST", "500"))
MAX_RSS_ITEMS = int(os.environ.get("MAX_RSS_ITEMS", "200"))

BASE_URL = os.environ.get("BASE_URL", "").strip().rstrip("/")
ENABLE_INDEXNOW = os.environ.get("ENABLE_INDEXNOW", "1").strip() == "1"
ENABLE_PINGOMATIC = os.environ.get("ENABLE_PINGOMATIC", "1").strip() == "1"

# Site variant identity (helps make each domain slightly different)
SITE_NAME = os.environ.get("SITE_NAME", "Discovery Hub").strip() or "Discovery Hub"
SITE_VARIANT = os.environ.get("SITE_VARIANT", "").strip()  # netlify | vercel | cloudflare | github-pages etc

# AI enrichment (optional)
ENABLE_AI = os.environ.get("ENABLE_AI", "0").strip() == "1"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
ENRICH_TTL_DAYS = int(os.environ.get("ENRICH_TTL_DAYS", "14"))
MAX_AI_CALLS = int(os.environ.get("MAX_AI_CALLS", "30"))

# Domain-wise site blurb (optional, but this is what you asked for)
ENABLE_SITE_BLURB = os.environ.get("ENABLE_SITE_BLURB", "1").strip() == "1"
SITE_BLURB_TTL_DAYS = int(os.environ.get("SITE_BLURB_TTL_DAYS", "30"))

UA = "Mozilla/5.0 (compatible; DiscoveryHub/1.0)"
URL_RE = re.compile(r"^https?://", re.I)

def utc_today_iso() -> str:
    return dt.datetime.utcnow().date().isoformat()

def utc_now_iso_z() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def utc_now_rfc2822() -> str:
    return dt.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

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
    u = u.strip()
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
            d = row[0].strip()
            u = row[1].strip()
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

# ---------------------------
# AI helpers
# ---------------------------

def _http_post_json(url: str, payload: dict, headers: dict, timeout: float = 18.0) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    for k, v in headers.items():
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

def _utc_now_str() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def _days_old(utc_z: str) -> int:
    try:
        t = dt.datetime.strptime(utc_z, "%Y-%m-%dT%H:%M:%SZ")
        return (dt.datetime.utcnow() - t).days
    except Exception:
        return 999999

# ---------------------------
# URL enrichment (your existing feature, kept)
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
    ENRICH_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

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
            raw = resp.read(200_000)
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
        "title": title[:160],
        "description": desc[:280],
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
            "maxOutputTokens": 256,
            "responseMimeType": "application/json",
            "response_mime_type": "application/json",
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
        "You write short neutral directory summaries.\n"
        "Rules:\n"
        "- Do not make claims like official, best, verified.\n"
        "- Keep summary 1 to 2 sentences, max 240 characters.\n"
        "- Return only JSON with keys: title, summary, topics.\n"
        "- topics: 1 to 4 items, short words.\n\n"
        f"URL: {url}\n"
        f"Type: {kind}\n"
        f"Title signal: {title}\n"
        f"Description signal: {description}\n"
    )
    out = _gemini_json(prompt)
    t = (out.get("title") or title or "").strip()[:120]
    s = (out.get("summary") or "").strip()[:260]
    topics = out.get("topics") or []
    if not isinstance(topics, list):
        topics = []
    topics = [str(x).strip()[:30] for x in topics if str(x).strip()][:4]
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
    now = _utc_now_str()

    for u in target_urls:
        if not URL_RE.match(u):
            continue
        kind = _guess_kind(u)

        cur = items.get(u) or {}
        fetched_utc = cur.get("fetched_utc") or ""
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
            "title": title[:160],
            "description": desc[:280],
            "summary": (cur.get("summary") or "").strip()[:260],
            "topics": cur.get("topics") or [],
            "fetched_utc": now,
        }

        if ENABLE_AI and GEMINI_API_KEY and ai_calls_left > 0:
            ai = _gemini_summary(u, kind, title, desc)
            if ai:
                out["title"] = (ai.get("title") or out["title"]).strip()[:160]
                out["summary"] = (ai.get("summary") or out["summary"]).strip()[:260]
                out["topics"] = ai.get("topics") or out["topics"]
            ai_calls_left -= 1

        if not out["summary"]:
            if out["description"]:
                out["summary"] = out["description"][:240]
            elif out["title"]:
                out["summary"] = out["title"][:240]

        if not isinstance(out["topics"], list):
            out["topics"] = []
        out["topics"] = [str(x).strip()[:30] for x in out["topics"] if str(x).strip()][:4]

        items[u] = out

    cache["items"] = items
    cache["generated_utc"] = now
    _save_enrich_cache(cache)
    return items

# ---------------------------
# Site blurb (Gemini per domain)
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
    SITE_BLURB_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

def _fallback_site_blurb() -> str:
    host = (urlparse(BASE_URL).netloc or "").strip() if BASE_URL else ""
    v = SITE_VARIANT or host or "mirror"
    return f"{SITE_NAME} is a lightweight discovery page that lists recently added links. This build is labeled {v}."

def _gemini_site_blurb(total_urls: int, sample_hosts: list[str]) -> str:
    v = SITE_VARIANT or (urlparse(BASE_URL).netloc if BASE_URL else "mirror")
    host_list = ", ".join(sample_hosts[:6]) if sample_hosts else "mixed sources"
    prompt = (
        "Write a short unique intro for a link directory page.\n"
        "Rules:\n"
        "- Neutral tone, no marketing.\n"
        "- 2 to 3 sentences.\n"
        "- Mention the build label and that it is a link list.\n"
        "- Do not use words like official, best, verified.\n"
        "- Return only JSON with keys: blurb, meta_description.\n"
        "- meta_description max 155 characters.\n\n"
        f"Site name: {SITE_NAME}\n"
        f"Build label: {v}\n"
        f"Total links: {total_urls}\n"
        f"Sample hosts: {host_list}\n"
    )
    out = _gemini_json(prompt)
    blurb = (out.get("blurb") or "").strip()
    if blurb:
        blurb = re.sub(r"\s+", " ", blurb).strip()
        return blurb[:360]
    return ""

def get_site_blurb(all_urls: list[str]) -> tuple[str, str]:
    """
    Returns (blurb, meta_description)
    """
    cache = _load_site_blurb_cache()
    items = cache.get("items") or {}
    if not isinstance(items, dict):
        items = {}
        cache["items"] = items

    key = BASE_URL or ("variant:" + (SITE_VARIANT or "default"))
    cur = items.get(key) or {}
    fetched_utc = cur.get("fetched_utc") or ""
    needs_refresh = True
    if fetched_utc:
        needs_refresh = _days_old(fetched_utc) >= SITE_BLURB_TTL_DAYS

    if (not ENABLE_SITE_BLURB) or (not needs_refresh):
        blurb = (cur.get("blurb") or "").strip() or _fallback_site_blurb()
        meta = (cur.get("meta_description") or "").strip() or blurb
        return blurb[:360], meta[:155]

    # refresh
    hosts = []
    seen = set()
    for u in all_urls[:40]:
        h = (urlparse(u).netloc or "").lower()
        if h and h not in seen:
            seen.add(h)
            hosts.append(h)

    blurb = ""
    meta_desc = ""

    if ENABLE_AI and GEMINI_API_KEY:
        blurb = _gemini_site_blurb(len(all_urls), hosts)

    if not blurb:
        blurb = _fallback_site_blurb()

    meta_desc = blurb
    if ENABLE_AI and GEMINI_API_KEY:
        # try to get meta_description too from the same cached object if provided
        # if not, we keep blurb trimmed
        pass

    now = _utc_now_str()
    items[key] = {"blurb": blurb[:360], "meta_description": meta_desc[:155], "fetched_utc": now}
    cache["items"] = items
    cache["generated_utc"] = now
    _save_site_blurb_cache(cache)

    return blurb[:360], meta_desc[:155]

# ---------------------------
# HTML build
# ---------------------------

def html_page(
    title: str,
    canonical_url: str,
    urls: list[str],
    built_utc: str,
    badges: list[tuple[str, str]],
    enriched: dict[str, dict] | None = None,
    site_blurb: str = "",
    meta_desc: str = "",
) -> str:
    rows = []
    for i, u in enumerate(urls, start=1):
        host, path = host_and_path(u)
        rows.append(
            "<tr>"
            f"<td class='num'>{i}</td>"
            f"<td class='url'><a href='{u}' target='_blank' rel='noopener'>{u}</a></td>"
            f"<td class='host'>{xml_escape(host)}</td>"
            f"<td class='path'>{xml_escape(path)}</td>"
            "</tr>"
        )

        if enriched:
            meta = enriched.get(u) or {}
            summ = (meta.get("summary") or "").strip()
            t = (meta.get("title") or "").strip()
            topics = meta.get("topics") or []
            kind = (meta.get("kind") or "").strip()
            fetched = (meta.get("fetched_utc") or "").strip()

            if t or summ or topics:
                topic_txt = ", ".join([xml_escape(str(x)) for x in topics if str(x).strip()])
                parts = []
                if t:
                    parts.append(f"<div class='u-title'>{xml_escape(t)}</div>")
                if summ:
                    parts.append(f"<div class='u-summ'>{xml_escape(summ)}</div>")
                extra_bits = []
                if kind:
                    extra_bits.append(xml_escape(kind))
                if fetched:
                    extra_bits.append(xml_escape(fetched))
                if topic_txt:
                    extra_bits.append(f"tags: {topic_txt}")
                if extra_bits:
                    parts.append(f"<div class='u-extra'>{' | '.join(extra_bits)}</div>")

                rows.append(
                    "<tr class='meta-row'>"
                    "<td class='num'></td>"
                    f"<td colspan='3'><div class='u-meta'>{''.join(parts)}</div></td>"
                    "</tr>"
                )

    canonical_tag = f"<link rel='canonical' href='{canonical_url}' />" if canonical_url else ""
    schema_json = itemlist_schema(title, urls, built_utc)

    meta_tag = ""
    if meta_desc:
        meta_tag = f"<meta name='description' content='{xml_escape(meta_desc)}' />"

    badge_html = []
    badge_html.append(f"<span class='badge'>URLs: <strong style='color:var(--text)'>{len(urls)}</strong></span>")
    badge_html.append(f"<span class='badge'>Built: <strong style='color:var(--text)'>{xml_escape(built_utc)} UTC</strong></span>")
    for text, href in badges:
        badge_html.append(f"<span class='badge'><a href='{href}'>{xml_escape(text)}</a></span>")

    blurb_html = f"<div class='blurb'>{xml_escape(site_blurb)}</div>" if site_blurb else ""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{xml_escape(title)}</title>
  {meta_tag}
  <meta name="robots" content="index,follow" />
  {canonical_tag}
  <script type="application/ld+json">{schema_json}</script>
  <style>
    :root {{
      --bg: #0b1220;
      --text: #e5e7eb;
      --muted: #9ca3af;
      --line: rgba(255,255,255,0.12);
      --link: #93c5fd;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, "Noto Sans", "Liberation Sans", sans-serif;
      background: radial-gradient(1200px 700px at 30% -10%, rgba(59,130,246,0.22), transparent 50%),
                  radial-gradient(1000px 600px at 110% 20%, rgba(34,197,94,0.18), transparent 45%),
                  var(--bg);
      color: var(--text);
    }}
    .wrap {{ max-width: 1100px; margin: 0 auto; padding: 28px 18px 48px; }}
    .card {{
      background: rgba(17,26,43,0.72);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 18px 18px;
      backdrop-filter: blur(10px);
    }}
    h1 {{ margin: 0 0 8px; font-size: 22px; letter-spacing: 0.2px; }}
    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px 16px;
      align-items: center;
      margin: 8px 0 10px;
      color: var(--muted);
      font-size: 13px;
    }}
    .meta a {{ color: var(--link); text-decoration: none; }}
    .meta a:hover {{ text-decoration: underline; }}

    .blurb {{
      margin: 6px 0 14px;
      color: #cbd5e1;
      font-size: 13px;
      line-height: 1.55;
    }}

    .table-wrap {{
      overflow: auto;
      border-radius: 12px;
      border: 1px solid var(--line);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 760px;
      background: rgba(0,0,0,0.16);
    }}
    thead th {{
      text-align: left;
      font-size: 12px;
      color: var(--muted);
      font-weight: 600;
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
    td.host {{ width: 220px; color: #d1d5db; }}
    td.path {{ color: #d1d5db; word-break: break-word; }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(0,0,0,0.18);
    }}
    .footer {{ margin-top: 14px; color: var(--muted); font-size: 12px; }}
    code {{ color: #d1fae5; }}

    .meta-row td {{ padding-top: 0; }}
    .u-meta {{
      padding: 10px 12px;
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 12px;
      background: rgba(0,0,0,0.10);
      margin: 0 0 10px;
    }}
    .u-title {{ font-size: 13px; color: #e5e7eb; font-weight: 600; margin-bottom: 4px; }}
    .u-summ {{ font-size: 12px; color: #cbd5e1; margin-bottom: 6px; }}
    .u-extra {{ font-size: 11px; color: var(--muted); }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>{xml_escape(title)}</h1>
      <div class="meta">
        {"".join(badge_html)}
      </div>
      {blurb_html}

      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>URL</th>
              <th>Host</th>
              <th>Path</th>
            </tr>
          </thead>
          <tbody>
            {"".join(rows) if rows else "<tr><td class='num'>-</td><td colspan='3'>No URLs yet.</td></tr>"}
          </tbody>
        </table>
      </div>

      <div class="footer">
        Generated from <code>data/daily.csv</code> and stored in <code>data/history.csv</code>.
      </div>
    </div>
  </div>
</body>
</html>
"""

def build_sitemap(urls: list[str]) -> str:
    now = utc_now_iso_z()
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    ]
    for u in urls:
        parts.append("<url>")
        parts.append(f"<loc>{xml_escape(u)}</loc>")
        parts.append(f"<lastmod>{now}</lastmod>")
        parts.append("</url>")
    parts.append("</urlset>")
    return "\n".join(parts) + "\n"

def build_rss(channel_link: str, items: list[tuple[str, str]]) -> str:
    build_time = utc_now_rfc2822()
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<rss version='2.0'>",
        "<channel>",
        f"<title>{xml_escape(SITE_NAME)} Feed</title>",
        f"<link>{xml_escape(channel_link)}</link>",
        "<description>Recent URLs added</description>",
        f"<lastBuildDate>{xml_escape(build_time)}</lastBuildDate>",
    ]
    for d, u in items[:MAX_RSS_ITEMS]:
        out.append("<item>")
        out.append(f"<title>{xml_escape(u)}</title>")
        out.append(f"<link>{xml_escape(u)}</link>")
        out.append(f"<guid isPermaLink='true'>{xml_escape(u)}</guid>")
        out.append(f"<pubDate>{xml_escape(build_time)}</pubDate>")
        out.append(f"<description>{xml_escape(u)}</description>")
        out.append("</item>")
    out.append("</channel>")
    out.append("</rss>")
    return "\n".join(out) + "\n"

def build_robots() -> str:
    lines = ["User-agent: *", "Allow: /"]
    if BASE_URL:
        lines.append(f"Sitemap: {BASE_URL}/sitemap.xml")
    return "\n".join(lines) + "\n"

def http_post(url: str, data: bytes, content_type: str, timeout: float = 12.0) -> tuple[int, str]:
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", content_type)
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
    except URLError as e:
        return 0, str(e)

def get_or_create_indexnow_key() -> str:
    if INDEXNOW_KEY_FILE.exists():
        k = INDEXNOW_KEY_FILE.read_text(encoding="utf-8", errors="ignore").strip()
        if k:
            return k
    key = secrets.token_hex(16)
    INDEXNOW_KEY_FILE.write_text(key + "\n", encoding="utf-8", newline="\n")
    return key

def ensure_indexnow_key_file_served(key: str):
    write_text(DOCS_DIR / f"{key}.txt", key + "\n")

def indexnow_submit(key: str, submit_urls: list[str]):
    if not BASE_URL:
        return
    host = urlparse(BASE_URL).netloc
    if not host:
        return
    payload = {
        "host": host,
        "key": key,
        "keyLocation": f"{BASE_URL}/{key}.txt",
        "urlList": submit_urls[:10000],
    }
    status, _ = _http_post_json("https://api.indexnow.org/indexnow", payload, headers={}, timeout=12.0)
    return status

def pingomatic_ping(name: str, url: str):
    xml = (
        "<?xml version='1.0'?>"
        "<methodCall>"
        "<methodName>weblogUpdates.ping</methodName>"
        "<params>"
        f"<param><value><string>{xml_escape(name)}</string></value></param>"
        f"<param><value><string>{xml_escape(url)}</string></value></param>"
        "</params>"
        "</methodCall>"
    ).encode("utf-8")
    return http_post("http://rpc.pingomatic.com/", xml, "text/xml", timeout=12.0)

def main():
    ensure_dirs()
    ensure_nojekyll()

    today = utc_today_iso()
    built_utc = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    input_urls = dedupe_preserve_order(read_input_urls())
    all_urls = input_urls[:MAX_ALL_LIST]

    history = update_history_with_today(all_urls, today)
    grouped = group_by_date(history)

    enriched = enrich_urls(all_urls) if all_urls else {}

    blurb, meta_desc = get_site_blurb(all_urls)

    # all.html
    all_badges = [
        (f"d/{today}.html", abs_url(f"/d/{today}.html")),
        ("sitemap.xml", abs_url("/sitemap.xml")),
        ("rss.xml", abs_url("/rss.xml")),
        ("robots.txt", abs_url("/robots.txt")),
    ]
    write_text(
        DOCS_DIR / "all.html",
        html_page(
            SITE_NAME,
            abs_url("/all.html") if BASE_URL else "",
            all_urls,
            built_utc,
            all_badges,
            enriched=enriched,
            site_blurb=blurb,
            meta_desc=meta_desc,
        ),
    )

    # index.html (self canonical to /)
    index_badges = list(all_badges)
    write_text(
        DOCS_DIR / "index.html",
        html_page(
            SITE_NAME,
            abs_url("/") if BASE_URL else "",
            all_urls,
            built_utc,
            index_badges,
            enriched=enriched,
            site_blurb=blurb,
            meta_desc=meta_desc,
        ),
    )

    # daily pages
    for d in sorted(grouped.keys()):
        d_urls = grouped.get(d) or []
        d_badges = [
            ("all.html", abs_url("/all.html")),
            ("sitemap.xml", abs_url("/sitemap.xml")),
            ("rss.xml", abs_url("/rss.xml")),
        ]
        write_text(
            DAILY_DIR / f"{d}.html",
            html_page(
                f"{SITE_NAME} {d}",
                abs_url(f"/d/{d}.html") if BASE_URL else "",
                d_urls,
                built_utc,
                d_badges,
                enriched=enriched,
                site_blurb=blurb,
                meta_desc=meta_desc,
            ),
        )

    # robots, rss, sitemap
    write_text(DOCS_DIR / "robots.txt", build_robots())

    rss_items = list(reversed(history))  # newest first
    write_text(DOCS_DIR / "rss.xml", build_rss(abs_url("/all.html"), rss_items))

    sitemap_urls = [
        abs_url("/"),
        abs_url("/index.html"),
        abs_url("/all.html"),
        abs_url("/sitemap.xml"),
        abs_url("/rss.xml"),
        abs_url("/robots.txt"),
    ]
    for d in sorted(grouped.keys()):
        sitemap_urls.append(abs_url(f"/d/{d}.html"))
    write_text(DOCS_DIR / "sitemap.xml", build_sitemap(sitemap_urls))

    # IndexNow + Pingomatic
    if ENABLE_INDEXNOW and BASE_URL:
        key = get_or_create_indexnow_key()
        ensure_indexnow_key_file_served(key)
        submit_list = [
            abs_url(f"/d/{today}.html"),
            abs_url("/all.html"),
            abs_url("/"),
        ]
        indexnow_submit(key, submit_list)

    if ENABLE_PINGOMATIC and BASE_URL:
        pingomatic_ping(SITE_NAME, abs_url("/rss.xml"))

if __name__ == "__main__":
    main()

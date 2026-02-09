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

DATA_FILE = Path("data/daily.csv")
HISTORY_FILE = Path("data/history.csv")
INDEXNOW_KEY_FILE = Path("data/indexnow.key")
ENRICH_CACHE_FILE = Path("data/enriched.json")

DOCS_DIR = Path("docs")
DAILY_DIR = DOCS_DIR / "d"

MAX_ALL_LIST = int(os.environ.get("MAX_ALL_LIST", "500"))
MAX_RSS_ITEMS = int(os.environ.get("MAX_RSS_ITEMS", "200"))

BASE_URL = os.environ.get("BASE_URL", "").strip().rstrip("/")
ENABLE_INDEXNOW = os.environ.get("ENABLE_INDEXNOW", "1").strip() == "1"
ENABLE_PINGOMATIC = os.environ.get("ENABLE_PINGOMATIC", "1").strip() == "1"

# AI enrichment (optional)
ENABLE_AI = os.environ.get("ENABLE_AI", "0").strip() == "1"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
ENRICH_TTL_DAYS = int(os.environ.get("ENRICH_TTL_DAYS", "14"))
MAX_AI_CALLS = int(os.environ.get("MAX_AI_CALLS", "30"))

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

def normalize_url(u: str) -> str:
    u = u.strip()
    u = re.sub(r"\s+", "", u)
    return u

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
# AI enrichment (safe + cached)
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

def _utc_now_str() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def _days_old(utc_z: str) -> int:
    try:
        t = dt.datetime.strptime(utc_z, "%Y-%m-%dT%H:%M:%SZ")
        return (dt.datetime.utcnow() - t).days
    except Exception:
        return 999999

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
    """
    Pull only lightweight signals (title + meta description).
    No scraping of full content.
    """
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

    text = ""
    try:
        # try utf-8
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

def _gemini_summary(url: str, kind: str, title: str, description: str) -> dict:
    """
    Returns dict: {title, summary, topics[]}
    Neutral, short, no claims, no marketing.
    """
    if not GEMINI_API_KEY:
        return {}

    prompt = (
        "You write short neutral directory summaries.\n"
        "Rules:\n"
        "- Do not make claims like official, best, verified.\n"
        "- Do not add phone numbers, emails, addresses.\n"
        "- Keep summary 1 to 2 sentences, max 240 characters.\n"
        "- Return only JSON with keys: title, summary, topics.\n"
        "- topics: 1 to 4 items, short words.\n\n"
        f"URL: {url}\n"
        f"Type: {kind}\n"
        f"Title signal: {title}\n"
        f"Description signal: {description}\n"
    )

    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 256,
            # Send both, different docs show different casing
            "responseMimeType": "application/json",
            "response_mime_type": "application/json",
        },
    }

    status, body = _http_post_json(
        endpoint,
        payload,
        headers={"x-goog-api-key": GEMINI_API_KEY},
        timeout=18.0,
    )
    if status < 200 or status >= 300:
        return {}

    try:
        obj = json.loads(body)
        # candidates[0].content.parts[0].text
        txt = (
            obj.get("candidates", [{}])[0]
              .get("content", {})
              .get("parts", [{}])[0]
              .get("text", "")
        )
        txt = txt.strip()
        out = json.loads(txt) if txt else {}
        if not isinstance(out, dict):
            return {}
        # normalize
        t = (out.get("title") or title or "").strip()[:120]
        s = (out.get("summary") or "").strip()[:260]
        topics = out.get("topics") or []
        if not isinstance(topics, list):
            topics = []
        topics = [str(x).strip()[:30] for x in topics if str(x).strip()][:4]
        if not s:
            return {}
        return {"title": t, "summary": s, "topics": topics}
    except Exception:
        return {}

def enrich_urls(target_urls: list[str]) -> dict[str, dict]:
    """
    Cache-first enrichment.
    Only refresh items older than ENRICH_TTL_DAYS.
    AI calls are capped by MAX_AI_CALLS per run.
    """
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

        # fallback summary if AI not used or failed
        if not out["summary"]:
            if out["description"]:
                out["summary"] = out["description"][:240]
            elif out["title"]:
                out["summary"] = out["title"][:240]

        # keep topics clean
        if not isinstance(out["topics"], list):
            out["topics"] = []
        out["topics"] = [str(x).strip()[:30] for x in out["topics"] if str(x).strip()][:4]

        items[u] = out

    cache["items"] = items
    cache["generated_utc"] = now
    _save_enrich_cache(cache)

    # return map for template
    return items

# ---------------------------
# HTML build
# ---------------------------

def html_page(title: str, canonical_url: str, urls: list[str], built_utc: str, badges: list[tuple[str, str]], enriched: dict[str, dict] | None = None) -> str:
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

    badge_html = []
    badge_html.append(f"<span class='badge'>URLs: <strong style='color:var(--text)'>{len(urls)}</strong></span>")
    badge_html.append(f"<span class='badge'>Built: <strong style='color:var(--text)'>{xml_escape(built_utc)} UTC</strong></span>")
    for text, href in badges:
        badge_html.append(f"<span class='badge'><a href='{href}'>{xml_escape(text)}</a></span>")

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{xml_escape(title)}</title>
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
      margin: 8px 0 14px;
      color: var(--muted);
      font-size: 13px;
    }}
    .meta a {{ color: var(--link); text-decoration: none; }}
    .meta a:hover {{ text-decoration: underline; }}
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

    /* Enrichment row */
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
        "<title>Discovery Hub Feed</title>",
        f"<link>{xml_escape(channel_link)}</link>",
        "<description>Recent URLs added to Discovery Hub</description>",
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

def build_robots(base_url: str) -> str:
    lines = ["User-agent: *", "Allow: /"]
    if base_url:
        lines.append(f"Sitemap: {base_url}/sitemap.xml")
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

def indexnow_submit(base_url: str, key: str, submit_urls: list[str]):
    host = urlparse(base_url).netloc
    if not host:
        return
    payload = {
        "host": host,
        "key": key,
        "keyLocation": f"{base_url}/{key}.txt",
        "urlList": submit_urls[:10000],
    }
    data = json.dumps(payload).encode("utf-8")
    http_post("https://api.indexnow.org/indexnow", data, "application/json", timeout=12.0)

def ping_pingomatic(site_name: str, site_url: str):
    xml = f"""<?xml version="1.0"?>
<methodCall>
  <methodName>weblogUpdates.ping</methodName>
  <params>
    <param><value><string>{xml_escape(site_name)}</string></value></param>
    <param><value><string>{xml_escape(site_url)}</string></value></param>
  </params>
</methodCall>
""".encode("utf-8")
    http_post("http://rpc.pingomatic.com/", xml, "text/xml", timeout=12.0)

def main():
    ensure_dirs()
    ensure_nojekyll()

    today = utc_today_iso()
    built_utc = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    input_urls = dedupe_preserve_order(read_input_urls())
    history = update_history_with_today(input_urls, today)
    grouped = group_by_date(history)

    newest_first = list(reversed(history))
    recent_urls = [u for _, u in newest_first[:MAX_ALL_LIST]]

    # Enrich only what appears on main pages (fast + safe)
    enriched_map = {}
    if ENABLE_AI or True:
        target = dedupe_preserve_order(recent_urls + grouped.get(today, []))
        enriched_map = enrich_urls(target)

    for date_str, urls_for_day in grouped.items():
        canonical = f"{BASE_URL}/d/{date_str}.html" if BASE_URL else ""
        badges = []
        if BASE_URL:
            badges.append(("all.html", f"{BASE_URL}/all.html"))
            badges.append(("sitemap.xml", f"{BASE_URL}/sitemap.xml"))
            badges.append(("rss.xml", f"{BASE_URL}/rss.xml"))
        html_out = html_page(f"Discovery Hub {date_str}", canonical, urls_for_day, built_utc, badges, enriched=enriched_map)
        write_text(DAILY_DIR / f"{date_str}.html", html_out)

    canonical_all = f"{BASE_URL}/all.html" if BASE_URL else ""
    badges_all = []
    if BASE_URL:
        badges_all.append((f"d/{today}.html", f"{BASE_URL}/d/{today}.html"))
        badges_all.append(("sitemap.xml", f"{BASE_URL}/sitemap.xml"))
        badges_all.append(("rss.xml", f"{BASE_URL}/rss.xml"))
        badges_all.append(("robots.txt", f"{BASE_URL}/robots.txt"))

    all_html = html_page("Discovery Hub", canonical_all, recent_urls, built_utc, badges_all, enriched=enriched_map)
    write_text(DOCS_DIR / "all.html", all_html)
    write_text(DOCS_DIR / "index.html", all_html)

    internal_urls: list[str] = []
    if BASE_URL:
        internal_urls.extend([
            f"{BASE_URL}/",
            f"{BASE_URL}/index.html",
            f"{BASE_URL}/all.html",
            f"{BASE_URL}/sitemap.xml",
            f"{BASE_URL}/rss.xml",
            f"{BASE_URL}/robots.txt",
        ])
        for d in grouped.keys():
            internal_urls.append(f"{BASE_URL}/d/{d}.html")
    else:
        internal_urls.extend(["index.html", "all.html", "sitemap.xml", "rss.xml", "robots.txt"])
        for d in grouped.keys():
            internal_urls.append(f"d/{d}.html")

    write_text(DOCS_DIR / "sitemap.xml", build_sitemap(internal_urls))

    channel_link = f"{BASE_URL}/all.html" if BASE_URL else "all.html"
    write_text(DOCS_DIR / "rss.xml", build_rss(channel_link, newest_first))
    write_text(DOCS_DIR / "robots.txt", build_robots(BASE_URL))

    if BASE_URL and ENABLE_INDEXNOW:
        key = get_or_create_indexnow_key()
        ensure_indexnow_key_file_served(key)

        submit_list = [
            f"{BASE_URL}/",
            f"{BASE_URL}/all.html",
            f"{BASE_URL}/d/{today}.html",
            f"{BASE_URL}/sitemap.xml",
            f"{BASE_URL}/rss.xml",
            f"{BASE_URL}/robots.txt",
            f"{BASE_URL}/{key}.txt",
        ]
        indexnow_submit(BASE_URL, key, submit_list)

    if BASE_URL and ENABLE_PINGOMATIC:
        ping_pingomatic("Discovery Hub", f"{BASE_URL}/rss.xml")

if __name__ == "__main__":
    main()

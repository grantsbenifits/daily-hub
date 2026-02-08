#!/usr/bin/env python3
import os
import re
import csv
import json
import secrets
import datetime as dt
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

DATA_FILE = Path("data/daily.csv")
HISTORY_FILE = Path("data/history.csv")
INDEXNOW_KEY_FILE = Path("data/indexnow.key")

DOCS_DIR = Path("docs")
DAILY_DIR = DOCS_DIR / "d"

MAX_ALL_LIST = int(os.environ.get("MAX_ALL_LIST", "500"))
MAX_RSS_ITEMS = int(os.environ.get("MAX_RSS_ITEMS", "200"))

BASE_URL = os.environ.get("BASE_URL", "").strip()
BASE_URL = BASE_URL.rstrip("/")  # trailing slash clean

UA = "Mozilla/5.0 (compatible; DiscoveryHub/1.0)"

URL_RE = re.compile(r"^https?://", re.I)

def utc_today_iso() -> str:
    return dt.datetime.utcnow().date().isoformat()

def utc_now_rfc2822() -> str:
    # RFC 2822 for RSS
    return dt.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

def ensure_dirs():
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    (Path("data")).mkdir(parents=True, exist_ok=True)

def read_input_urls() -> list[str]:
    if not DATA_FILE.exists():
        return []

    lines = DATA_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
    urls = []

    # Support both:
    # 1) header csv: url \n https://...
    # 2) old format lines: YYYY-MM-DD,https://...
    for i, raw in enumerate(lines):
        s = raw.strip()
        if not s:
            continue
        if i == 0 and s.lower() == "url":
            continue

        # If someone pastes "date,url", keep last part
        if "," in s and (not URL_RE.match(s)):
            parts = [p.strip() for p in s.split(",") if p.strip()]
            if parts and URL_RE.match(parts[-1]):
                s = parts[-1]

        if URL_RE.match(s):
            urls.append(s)

    return urls

def normalize_url(u: str) -> str:
    u = u.strip()
    # remove trailing spaces and normalize common junk
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
    rows = []
    with HISTORY_FILE.open("r", encoding="utf-8", newline="") as f:
        r = csv.reader(f)
        for row in r:
            if not row or len(row) < 2:
                continue
            d = row[0].strip()
            u = row[1].strip()
            if not d or not u:
                continue
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

    new_added = 0
    for u in input_urls:
        if u in existing:
            continue
        history.append((today, u))
        existing.add(u)
        new_added += 1

    # keep stable order, do not sort
    if new_added > 0:
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

def html_page(title: str, base_url: str, urls: list[str], built_utc: str, extra_badges: list[tuple[str, str]]):
    def badge_html(label: str, value_html: str) -> str:
        return f"<span class='badge'>{label}: <strong style='color:var(--text)'>{value_html}</strong></span>"

    badges = []
    badges.append(badge_html("URLs", str(len(urls))))
    badges.append(badge_html("Built", built_utc + " UTC"))

    # core links
    if base_url:
        badges.append(f"<span class='badge'><a href='{base_url}/sitemap.xml'>sitemap.xml</a></span>")
        badges.append(f"<span class='badge'><a href='{base_url}/rss.xml'>rss.xml</a></span>")
        badges.append(f"<span class='badge'><a href='{base_url}/robots.txt'>robots.txt</a></span>")

    for text, href in extra_badges:
        badges.append(f"<span class='badge'><a href='{href}'>{text}</a></span>")

    rows_html = []
    for i, u in enumerate(urls, start=1):
        host, path = host_and_path(u)
        rows_html.append(
            "<tr>"
            f"<td class='num'>{i}</td>"
            f"<td class='url'><a href='{u}' rel='nofollow noopener' target='_blank'>{u}</a></td>"
            f"<td class='host'>{xml_escape(host)}</td>"
            f"<td class='path'>{xml_escape(path)}</td>"
            "</tr>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{xml_escape(title)}</title>
  <meta name="robots" content="index,follow" />
  <style>
    :root {{
      --bg: #0b1220;
      --panel: #111a2b;
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
    .footer {{
      margin-top: 14px;
      color: var(--muted);
      font-size: 12px;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(0,0,0,0.18);
    }}
    code {{ color: #d1fae5; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>{xml_escape(title)}</h1>
      <div class="meta">
        {"".join(badges)}
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
            {"".join(rows_html) if rows_html else "<tr><td class='num'>-</td><td colspan='3'>No URLs yet.</td></tr>"}
          </tbody>
        </table>
      </div>

      <div class="footer">
        This hub is generated from <code>data/daily.csv</code> and stored history in <code>data/history.csv</code>.
      </div>
    </div>
  </div>
</body>
</html>
"""

def write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")

def ensure_nojekyll():
    write_text(DOCS_DIR / ".nojekyll", "")

def write_index_html(base_url: str):
    # simple landing
    link_all = f"{base_url}/all.html" if base_url else "all.html"
    link_sitemap = f"{base_url}/sitemap.xml" if base_url else "sitemap.xml"
    link_rss = f"{base_url}/rss.xml" if base_url else "rss.xml"
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Discovery Hub</title>
  <meta name="robots" content="index,follow" />
</head>
<body style="font-family:system-ui,Segoe UI,Roboto,Arial; padding:24px;">
  <h1>Discovery Hub</h1>
  <ul>
    <li><a href="{link_all}">all.html</a></li>
    <li><a href="{link_sitemap}">sitemap.xml</a></li>
    <li><a href="{link_rss}">rss.xml</a></li>
  </ul>
</body>
</html>
"""
    write_text(DOCS_DIR / "index.html", html)

def build_sitemap(base_url: str, urls: list[str]) -> str:
    now = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
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

def build_rss(base_url: str, items: list[tuple[str, str]]) -> str:
    # items are (date, url) newest first
    channel_link = f"{base_url}/all.html" if base_url else "all.html"
    build_time = utc_now_rfc2822()
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        "<title>Discovery Hub Feed</title>",
        f"<link>{xml_escape(channel_link)}</link>",
        "<description>Recent URLs added to Discovery Hub</description>",
        f"<lastBuildDate>{xml_escape(build_time)}</lastBuildDate>",
    ]
    for d, u in items[:MAX_RSS_ITEMS]:
        host, path = host_and_path(u)
        title = f"{host}{path}"
        out.append("<item>")
        out.append(f"<title>{xml_escape(title)}</title>")
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

def get_or_create_indexnow_key() -> str:
    if INDEXNOW_KEY_FILE.exists():
        k = INDEXNOW_KEY_FILE.read_text(encoding="utf-8", errors="ignore").strip()
        if k:
            return k

    key = secrets.token_hex(16)
    INDEXNOW_KEY_FILE.write_text(key + "\n", encoding="utf-8", newline="\n")
    return key

def ensure_indexnow_key_file_served(key: str):
    # IndexNow needs: https://host/<key>.txt
    write_text(DOCS_DIR / f"{key}.txt", key + "\n")

def http_post(url: str, data: bytes, content_type: str, timeout: float = 12.0) -> tuple[int, str]:
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", content_type)
    req.add_header("User-Agent", UA)
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return int(resp.status), body
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        return int(e.code), body
    except URLError as e:
        return 0, str(e)

def indexnow_submit(base_url: str, key: str, submit_urls: list[str]):
    if not base_url:
        return

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
    # XML-RPC: weblogUpdates.ping
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
    today = utc_today_iso()
    built_utc = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    input_urls = dedupe_preserve_order(read_input_urls())

    # history keeps date for each first-seen URL
    history = update_history_with_today(input_urls, today)
    grouped = group_by_date(history)

    # build daily pages for each date (so old dates stay available)
    for date_str, urls_for_day in grouped.items():
        daily_title = f"Discovery Hub {date_str}"
        extra = []
        if BASE_URL:
            extra.append(("all.html", f"{BASE_URL}/all.html"))
        html = html_page(daily_title, BASE_URL, urls_for_day, built_utc, extra)
        write_text(DAILY_DIR / f"{date_str}.html", html)

    # all.html shows most recent URLs from history
    # show newest first
    newest_first = list(reversed(history))
    all_urls_recent = [u for _, u in newest_first[:MAX_ALL_LIST]]

    all_extra = []
    if BASE_URL:
        all_extra.append((f"d/{today}.html", f"{BASE_URL}/d/{today}.html"))
    all_html = html_page("Discovery Hub", BASE_URL, all_urls_recent, built_utc, all_extra)
    write_text(DOCS_DIR / "all.html", all_html)

    ensure_nojekyll()
    write_index_html(BASE_URL)

    # sitemap should include hub pages (not external urls)
    sitemap_urls = []
    if BASE_URL:
        sitemap_urls.extend([
            f"{BASE_URL}/",
            f"{BASE_URL}/index.html",
            f"{BASE_URL}/all.html",
            f"{BASE_URL}/sitemap.xml",
            f"{BASE_URL}/rss.xml",
            f"{BASE_URL}/robots.txt",
            f"{BASE_URL}/backlink-feed.xml",
        ])
        for d in grouped.keys():
            sitemap_urls.append(f"{BASE_URL}/d/{d}.html")
    else:
        # fallback relative paths
        sitemap_urls.extend([
            "index.html",
            "all.html",
            "sitemap.xml",
            "rss.xml",
            "robots.txt",
            "backlink-feed.xml",
        ])
        for d in grouped.keys():
            sitemap_urls.append(f"d/{d}.html")

    sitemap_xml = build_sitemap(BASE_URL, sitemap_urls)
    write_text(DOCS_DIR / "sitemap.xml", sitemap_xml)

    rss_xml = build_rss(BASE_URL, newest_first)
    write_text(DOCS_DIR / "rss.xml", rss_xml)
    write_text(DOCS_DIR / "backlink-feed.xml", rss_xml)

    robots_txt = build_robots(BASE_URL)
    write_text(DOCS_DIR / "robots.txt", robots_txt)

    # IndexNow key + submit
    key = get_or_create_indexnow_key()
    ensure_indexnow_key_file_served(key)

    if BASE_URL:
        submit_list = [
            f"{BASE_URL}/",
            f"{BASE_URL}/all.html",
            f"{BASE_URL}/d/{today}.html",
            f"{BASE_URL}/sitemap.xml",
            f"{BASE_URL}/rss.xml",
            f"{BASE_URL}/backlink-feed.xml",
            f"{BASE_URL}/robots.txt",
            f"{BASE_URL}/{key}.txt",
        ]
        indexnow_submit(BASE_URL, key, submit_list)
        # Pingomatic ping feed
        ping_pingomatic("Daily Backlink Hub", f"{BASE_URL}/backlink-feed.xml")

if __name__ == "__main__":
    main()

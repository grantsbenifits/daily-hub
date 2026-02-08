#!/usr/bin/env python3
"""
Discovery Hub (Core MVP)

Reads URLs from data/daily.csv and generates:
- docs/all.html
- docs/sitemap.xml
- docs/rss.xml
- docs/robots.txt
- docs/index.html (redirect to all.html)
"""

from __future__ import annotations

import csv
import html
import os
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse


DATA_FILE = os.environ.get("DATA_FILE", "data/daily.csv")
DOCS_DIR = os.environ.get("DOCS_DIR", "docs")
BASE_URL = os.environ.get("BASE_URL", "").strip()

def _ensure_trailing_slash(u: str) -> str:
    if not u:
        return ""
    return u if u.endswith("/") else (u + "/")

def normalize_url(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    if not (s.lower().startswith("http://") or s.lower().startswith("https://")):
        return None

    p = urlparse(s)
    scheme = (p.scheme or "https").lower()
    netloc = (p.netloc or "").strip().lower()

    # Strip default ports
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    path = p.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    # Drop fragment
    fragment = ""

    normalized = urlunparse((scheme, netloc, path, p.params, p.query, fragment))
    return normalized

def read_urls(csv_path: str) -> list[str]:
    if not os.path.exists(csv_path):
        return []

    urls: list[str] = []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        sample = f.read(4096)
        f.seek(0)

        # If file is plain list without header, handle it gracefully.
        has_header = ("url" in sample.splitlines()[0].strip().lower()) if sample.splitlines() else False

        if has_header:
            reader = csv.DictReader(f)
            for row in reader:
                raw = (row.get("url") or "").strip()
                if raw:
                    urls.append(raw)
        else:
            # One URL per line
            for line in f:
                line = line.strip()
                if not line or line.lower() == "url":
                    continue
                urls.append(line)

    return urls

def dedupe_preserve_order(urls: list[str]) -> list[str]:
    seen = set()
    out: list[str] = []
    for u in urls:
        nu = normalize_url(u)
        if not nu:
            continue
        if nu in seen:
            continue
        seen.add(nu)
        out.append(nu)
    return out

def build_all_html(urls: list[str], built_at: datetime, base_url: str) -> str:
    safe_base = html.escape(base_url)
    built_str = built_at.strftime("%Y-%m-%d %H:%M:%S UTC")

    rows = []
    for i, u in enumerate(urls, start=1):
        pu = urlparse(u)
        host = pu.netloc
        path = pu.path or "/"
        if pu.query:
            path = f"{path}?{pu.query}"
        rows.append(
            f"<tr>"
            f"<td class='num'>{i}</td>"
            f"<td class='url'><a href='{html.escape(u)}' rel='nofollow noopener' target='_blank'>{html.escape(u)}</a></td>"
            f"<td class='host'>{html.escape(host)}</td>"
            f"<td class='path'>{html.escape(path)}</td>"
            f"</tr>"
        )

    count = len(urls)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Discovery Hub</title>
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
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Discovery Hub</h1>
      <div class="meta">
        <span class="badge">URLs: <strong style="color:var(--text)">{count}</strong></span>
        <span class="badge">Built: <strong style="color:var(--text)">{built_str}</strong></span>
        <span class="badge"><a href="{safe_base}sitemap.xml">sitemap.xml</a></span>
        <span class="badge"><a href="{safe_base}rss.xml">rss.xml</a></span>
        <span class="badge"><a href="{safe_base}robots.txt">robots.txt</a></span>
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
            {''.join(rows) if rows else "<tr><td class='num'>-</td><td colspan='3'>No URLs found in data/daily.csv</td></tr>"}
          </tbody>
        </table>
      </div>

      <div class="footer">
        This page lists the URLs you pasted into <code>data/daily.csv</code>.
      </div>
    </div>
  </div>
</body>
</html>
"""

def build_index_html(base_url: str) -> str:
    safe_base = html.escape(base_url)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta http-equiv="refresh" content="0; url={safe_base}all.html" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Discovery Hub</title>
</head>
<body>
  <p>Redirecting to <a href="{safe_base}all.html">all.html</a>...</p>
</body>
</html>
"""

def build_robots_txt(base_url: str) -> str:
    base = _ensure_trailing_slash(base_url)
    return f"User-agent: *\nAllow: /\nSitemap: {base}sitemap.xml\n"

def build_sitemap_xml(base_url: str, built_at: datetime) -> str:
    base = _ensure_trailing_slash(base_url)
    lastmod = built_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    pages = [
        base,  # directory, serves index.html
        f"{base}index.html",
        f"{base}all.html",
        f"{base}rss.xml",
        f"{base}robots.txt",
        f"{base}sitemap.xml",
    ]

    items = []
    for p in pages:
        items.append(
            "<url>"
            f"<loc>{html.escape(p)}</loc>"
            f"<lastmod>{lastmod}</lastmod>"
            "</url>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(items)
        + "\n</urlset>\n"
    )

def build_rss_xml(urls: list[str], built_at: datetime, base_url: str) -> str:
    base = _ensure_trailing_slash(base_url)
    pub_date = built_at.strftime("%a, %d %b %Y %H:%M:%S GMT")

    def item_xml(u: str) -> str:
        title = u
        desc = f"External URL: {u}"
        guid = u
        return (
            "<item>"
            f"<title>{html.escape(title)}</title>"
            f"<link>{html.escape(u)}</link>"
            f"<guid isPermaLink=\"true\">{html.escape(guid)}</guid>"
            f"<pubDate>{pub_date}</pubDate>"
            f"<description>{html.escape(desc)}</description>"
            "</item>"
        )

    items = "\n".join(item_xml(u) for u in urls[:5000])

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        "<channel>\n"
        "<title>Discovery Hub Feed</title>\n"
        f"<link>{html.escape(base)}all.html</link>\n"
        "<description>Latest URLs from data/daily.csv</description>\n"
        f"<lastBuildDate>{pub_date}</lastBuildDate>\n"
        f"{items}\n"
        "</channel>\n"
        "</rss>\n"
    )

def write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)

def main() -> int:
    built_at = datetime.now(timezone.utc)
    base_url = _ensure_trailing_slash(BASE_URL) if BASE_URL else ""

    raw_urls = read_urls(DATA_FILE)
    urls = dedupe_preserve_order(raw_urls)

    all_html = build_all_html(urls, built_at, base_url)
    rss_xml = build_rss_xml(urls, built_at, base_url)
    robots_txt = build_robots_txt(base_url)
    sitemap_xml = build_sitemap_xml(base_url, built_at)
    index_html = build_index_html(base_url)

    write_file(os.path.join(DOCS_DIR, "all.html"), all_html)
    write_file(os.path.join(DOCS_DIR, "rss.xml"), rss_xml)
    write_file(os.path.join(DOCS_DIR, "robots.txt"), robots_txt)
    write_file(os.path.join(DOCS_DIR, "sitemap.xml"), sitemap_xml)
    write_file(os.path.join(DOCS_DIR, "index.html"), index_html)

    print(f"Built {len(urls)} URLs into {DOCS_DIR}/all.html")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

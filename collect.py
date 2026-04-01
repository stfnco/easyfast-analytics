#!/usr/bin/env python3
"""
Easyfast Analytics Collector
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Usage:
  python3 collect.py                     # collect today's data
  python3 collect.py --also-yesterday    # today + re-fetch yesterday
  python3 collect.py --date 2026-04-05   # specific date
  python3 collect.py --dry-run           # preview without writing files
  python3 collect.py --diagnose          # debug: inspect raw responses
"""

import sys, re, csv, io, json, argparse, os
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin

try:
    import requests
except ImportError:
    print("❌  pip3 install requests beautifulsoup4")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# ── Config ────────────────────────────────────────────────────────────────────
# Token: set POLAR_TOKEN env var (GitHub Actions secret), or leave hardcoded for local use
POLAR_TOKEN = os.environ.get("POLAR_TOKEN", "polar_oat_foTaGXfvpjzIKQxFTAogiMx0KKGfGaDYw5JZp3piWrd")
POLAR_BASE  = "https://api.polar.sh/v1"
# WORK_DIR: defaults to the folder where this script lives (works locally and in GitHub Actions)
WORK_DIR    = Path(os.environ.get("WORK_DIR", Path(__file__).parent))
CSV_PATH    = WORK_DIR / "easyfast_ranks_history.csv"
HTML_PATH   = WORK_DIR / "easyfast_dashboard.html"
FRAMER_URL       = "https://framer-ranks.com"
FRAMER_RANKS_JSON = "https://framer-ranks.com/ranks-data.json"
FRAMER_RANK_KEY   = "alltime-all"   # key inside each item's "ranks" dict; value = [rank, change]

# Author name on framer-ranks.com — used to auto-discover all your templates
AUTHOR_NAME = "Easyfast"

CSV_HEADERS = [
    "date","template","rank","change_1d",
    "price_type","price","checkouts","orders","revenue","conversion",
]

# ── Polar.sh ──────────────────────────────────────────────────────────────────
_s = requests.Session()
_s.headers.update({"Authorization": f"Bearer {POLAR_TOKEN}", "Accept": "application/json"})

def polar_get(path, params=None):
    r = _s.get(f"{POLAR_BASE}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def fetch_all_pages(endpoint, extra_params=None):
    """Fetch every page from a Polar list endpoint."""
    params = dict(extra_params or {})
    params["limit"] = 100
    params["page"]  = 1
    out = []
    while True:
        batch = polar_get(endpoint, params).get("items", [])
        out.extend(batch)
        if len(batch) < 100:
            break
        params["page"] += 1
    return out

def template_name(product_name):
    """'Makro — Full Site — Unlimited' → 'Makro'"""
    return product_name.split("—")[0].strip()

def discover_paid_templates(products):
    """
    Auto-detect paid templates and their canonical prices from Polar.sh products.
    Groups all tiers (Basic / Full Site / Unlimited) under one template name.
    Picks the "Full Site" tier price; falls back to the highest price found.
    Returns: {template_name: price_in_dollars}
    """
    from collections import defaultdict
    tier_prices = defaultdict(dict)   # {tmpl: {"basic": $, "full_site": $, "unlimited": $}}

    for p in products:
        name     = p.get("name", "")
        tmpl     = template_name(name)
        name_lc  = name.lower()

        # Collect the highest price_amount from this product's prices list
        price_cents = 0
        for pr in p.get("prices", []):
            amt = pr.get("price_amount", 0)
            if isinstance(amt, (int, float)) and amt > price_cents:
                price_cents = amt

        # Classify tier
        if "unlimited" in name_lc:
            tier = "unlimited"
        elif "full site" in name_lc or "full_site" in name_lc:
            tier = "full_site"
        else:
            tier = "basic"

        tier_prices[tmpl][tier] = price_cents / 100

    result = {}
    for tmpl, tiers in tier_prices.items():
        price = (tiers.get("full_site")
                 or tiers.get("unlimited")
                 or tiers.get("basic")
                 or (max(tiers.values()) if tiers else 0))
        result[tmpl] = int(price)
    return result

def get_polar_metrics(target_date, products):
    """
    Returns {template_name: {orders, revenue, checkouts, conversion}}
    Polar.sh date filter doesn't work → fetch all, filter by date in Python.
    """
    print(f"      Fetching all Polar.sh orders…")
    all_orders    = fetch_all_pages("/orders")
    print(f"      → {len(all_orders)} total orders")

    print(f"      Fetching all Polar.sh checkouts…")
    all_checkouts = fetch_all_pages("/checkouts")
    print(f"      → {len(all_checkouts)} total checkouts")

    # Filter to target date
    day_orders    = [o for o in all_orders    if o.get("created_at","").startswith(target_date) and o.get("paid")]
    day_checkouts = [c for c in all_checkouts if c.get("created_at","").startswith(target_date)]

    print(f"      → {len(day_orders)} orders on {target_date}, {len(day_checkouts)} checkouts")

    # Build product_id → template name map (merge all tiers under one name)
    pid_to_tmpl = {}
    for p in products:
        tmpl = template_name(p["name"])
        if tmpl in PAID_TEMPLATES:
            pid_to_tmpl[p["id"]] = tmpl

    metrics = {t: {"orders":0,"revenue":0.0,"checkouts":0,"conversion":0.0}
               for t in PAID_TEMPLATES}

    for o in day_orders:
        tmpl = pid_to_tmpl.get(o.get("product_id"))
        if tmpl:
            metrics[tmpl]["orders"]  += 1
            # net_amount is in cents
            metrics[tmpl]["revenue"] += o.get("net_amount", 0) / 100

    for c in day_checkouts:
        tmpl = pid_to_tmpl.get(c.get("product_id"))
        if tmpl:
            metrics[tmpl]["checkouts"] += 1

    for m in metrics.values():
        if m["checkouts"] > 0:
            m["conversion"] = round(m["orders"] / m["checkouts"] * 100, 2)

    return metrics

# ── Framer Ranks ───────────────────────────────────────────────────────────────
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0 Safari/537.36"

def _try_json_list(data, templates):
    """Try to extract {name: rank} from a JSON payload."""
    items = data if isinstance(data, list) else next(
        (data[k] for k in ["items","templates","data","rankings","results"]
         if isinstance(data.get(k), list)), None
    )
    if not items:
        return {}
    name_keys = ["name","title","template_name","templateName"]
    rank_keys = ["rank","position","rank_position","ranking","order","index"]
    result = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = next((item[k] for k in name_keys if k in item), None)
        rank = next((item[k] for k in rank_keys if k in item), None)
        if name and rank is not None:
            for tmpl in templates:
                if tmpl.lower() in str(name).lower():
                    try:
                        result[tmpl] = int(rank)
                    except (ValueError, TypeError):
                        pass
                    break
    return result

def fetch_ranks(paid_templates=None):
    """
    Fetch Framer Marketplace ranks from framer-ranks.com/ranks-data.json.

    The JSON has a top-level "items" array (8000+ entries). Each item has:
      {name, type, authorName, ranks: {"alltime-all": [rank, change], ...}, ...}

    We match items by name (case-insensitive) against ALL_TEMPLATES and use
    FRAMER_RANK_KEY (default: "alltime-all") as the rank to store.

    Fallback: if the JSON URL fails, scan JS bundles for any embedded data URL.
    """
    print("      Fetching framer-ranks.com/ranks-data.json…")
    api_headers = {"User-Agent": UA, "Accept": "application/json, */*",
                   "Referer": FRAMER_URL}

    # ── 1. Primary: direct JSON endpoint (discovered via browser network inspection) ──
    try:
        r = requests.get(FRAMER_RANKS_JSON, headers=api_headers, timeout=30)
        r.raise_for_status()
        data  = r.json()
        items = data.get("items", [])
        print(f"      → {len(items)} items in ranks-data.json")
        result = _parse_ranks_json(items, paid_templates)
        if result:
            print(f"      → matched {len(result)} templates")
            return result
        print("      ⚠  ranks-data.json loaded but no templates matched")
    except Exception as e:
        print(f"      ⚠  ranks-data.json failed: {e}")

    # ── 2. Fallback: look for data URLs in JS bundles ──
    print("      Falling back to JS bundle scan…")
    try:
        page = requests.get(FRAMER_URL, headers={"User-Agent": UA}, timeout=30)
        page.raise_for_status()
        srcs = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', page.text)
        found_urls = set()
        for src in srcs:
            full_src = urljoin(FRAMER_URL, src) if src.startswith("/") else src
            if not full_src.startswith("http"):
                continue
            try:
                js = requests.get(full_src, headers={"User-Agent": UA}, timeout=15).text
                for pat in [
                    r'["\`](https?://[^"\`\)]+ranks[^"\`\)]*\.json)["\`]',
                    r'["\`](https?://[^"\`\)]+data[^"\`\)]*\.json)["\`]',
                    r'"(https?://[a-z0-9-]+\.supabase\.co[^"]+)"',
                ]:
                    for u in re.findall(pat, js):
                        found_urls.add(u)
            except Exception:
                continue
        for url in found_urls:
            try:
                r = requests.get(url, headers=api_headers, timeout=15)
                if r.ok and "json" in r.headers.get("content-type",""):
                    data  = r.json()
                    items = data.get("items", data) if isinstance(data, dict) else data
                    result = _parse_ranks_json(items if isinstance(items, list) else [])
                    if result:
                        print(f"      → {len(result)} ranks via bundle URL: {url}")
                        return result
            except Exception:
                continue
    except Exception as e:
        print(f"      ⚠  Fallback scan failed: {e}")

    print("      ⚠  Couldn't fetch ranks. Run --diagnose to investigate.")
    return {}


def _parse_ranks_json(items, paid_templates=None):
    """
    Parse items list from ranks-data.json.
    Each item: {name, type, authorName, ranks: {"alltime-all": [rank, delta], ...}}

    Matching priority:
      1. Items whose authorName == AUTHOR_NAME (auto-discovers all your templates)
      2. Items whose name matches a key in paid_templates (explicit fallback list)

    Returns {template_name: rank_int}  — rank is None if the item exists but
    has no rank yet (new template not yet ranked on framer-ranks.com).
    """
    result = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_name   = item.get("name", "")
        item_author = item.get("authorName", "")
        ranks_map   = item.get("ranks", {})

        # Decide if this item belongs to us
        matched_name = None
        if item_author.lower() == AUTHOR_NAME.lower():
            matched_name = item_name
        elif paid_templates:
            for tmpl in paid_templates:
                if tmpl.lower() == item_name.lower():
                    matched_name = tmpl
                    break

        if not matched_name:
            continue

        # Extract rank (may be None for brand-new templates)
        rank_int = None
        if isinstance(ranks_map, dict):
            rank_entry = ranks_map.get(FRAMER_RANK_KEY)
            if isinstance(rank_entry, list) and len(rank_entry) >= 1 and rank_entry[0] is not None:
                try:
                    rank_int = int(rank_entry[0])
                except (ValueError, TypeError):
                    pass

        result[matched_name] = rank_int   # None = template exists but not ranked yet
    return result

# ── CSV ────────────────────────────────────────────────────────────────────────
def load_csv():
    if not CSV_PATH.exists():
        return []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def build_rows(existing, target_date, ranks, metrics, paid_templates_info):
    """
    paid_templates_info: {template_name: price}  — auto-discovered from Polar.sh
    ranks:               {template_name: rank_int or None}  — from framer-ranks.com
                         (None = template not yet ranked; missing key = not found at all)
    metrics:             {template_name: {orders, revenue, checkouts, conversion}}

    All paid templates are always written, even with 0 sales or no rank yet.
    """
    base = [r for r in existing if r["date"] != target_date]
    prev_date  = (date.fromisoformat(target_date) - timedelta(days=1)).isoformat()
    prev_ranks = {}
    for r in existing:
        if r["date"] == prev_date and r.get("rank"):
            try:
                prev_ranks[r["template"]] = int(r["rank"])
            except (ValueError, TypeError):
                pass

    new_rows = []
    for tmpl, price in sorted(paid_templates_info.items()):
        # Rank: may be None (unranked) or missing (not found on framer-ranks.com)
        rank = ranks.get(tmpl)       # None = unranked; key absent = not found

        if rank is not None:
            prev  = prev_ranks.get(tmpl, rank)
            delta = prev - rank
        else:
            delta = ""

        m = metrics.get(tmpl, {"orders":0,"revenue":0.0,"checkouts":0,"conversion":0.0})
        row = {
            "date":       target_date,
            "template":   tmpl,
            "rank":       rank if rank is not None else "",
            "change_1d":  delta,
            "price_type": "paid",
            "price":      price,
            "checkouts":  m["checkouts"],
            "orders":     m["orders"],
            "revenue":    round(m["revenue"], 2),
            "conversion": m["conversion"],
        }
        new_rows.append(row)

    all_rows = base + new_rows
    all_rows.sort(key=lambda r: (r["date"], r["template"]))
    return all_rows

def rows_to_text(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_HEADERS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()

def save_csv(rows, dry_run=False):
    text = rows_to_text(rows)
    if dry_run:
        print("\n── CSV (last 25 lines) ──")
        print("\n".join(text.strip().split("\n")[-25:]))
    else:
        CSV_PATH.write_text(text, encoding="utf-8")
        print(f"   ✅ CSV → {CSV_PATH}")
    return text

def update_dashboard(csv_text, dry_run=False):
    if not HTML_PATH.exists():
        return
    html = HTML_PATH.read_text(encoding="utf-8")
    new_html = re.sub(
        r"(const EMBEDDED_CSV = `)([^`]*?)(`)",
        lambda m: m.group(1) + csv_text.strip() + m.group(3),
        html, flags=re.DOTALL,
    )
    if new_html == html:
        print("   ⚠  EMBEDDED_CSV marker not found")
        return
    if not dry_run:
        HTML_PATH.write_text(new_html, encoding="utf-8")
        print(f"   ✅ Dashboard → {HTML_PATH}")
    else:
        print("   ✅ Dashboard would be updated (dry-run)")

# ── Diagnose ───────────────────────────────────────────────────────────────────
def diagnose():
    SEP = "─" * 60

    print(f"\n{SEP}\nPolar.sh — Products\n{SEP}")
    try:
        products = polar_get("/products", {"limit": 100}).get("items", [])
        for p in products:
            tmpl = template_name(p["name"])
            print(f"  [{tmpl}]  {p['name']}  id={p['id']}")
    except Exception as e:
        print(f"  ERROR: {e}")

    today = date.today().isoformat()
    print(f"\n{SEP}\nPolar.sh — Orders sample (all dates)\n{SEP}")
    try:
        data   = polar_get("/orders", {"limit": 5, "page": 1})
        orders = data.get("items", [])
        total  = data.get("pagination", {}).get("total_count") or data.get("total") or "?"
        print(f"  Total: {total}  |  Showing 5 most recent")
        for o in orders:
            tmpl = template_name(o.get("product", {}).get("name", "?"))
            print(f"  {o['created_at'][:10]}  {tmpl:<12}  "
                  f"net={o.get('net_amount',0)/100:.2f}  status={o.get('status')}")
    except Exception as e:
        print(f"  ERROR: {e}")

    print(f"\n{SEP}\nPolar.sh — Orders on {today}\n{SEP}")
    try:
        all_orders = fetch_all_pages("/orders")
        day = [o for o in all_orders if o.get("created_at","").startswith(today) and o.get("paid")]
        print(f"  {len(day)} paid orders today (out of {len(all_orders)} total)")
        for o in day:
            tmpl = template_name(o.get("product", {}).get("name", "?"))
            print(f"    {tmpl:<12}  ${o.get('net_amount',0)/100:.2f}  {o.get('billing_name','')}")
    except Exception as e:
        print(f"  ERROR: {e}")

    print(f"\n{SEP}\nPolar.sh — Checkouts on {today}\n{SEP}")
    try:
        all_co = fetch_all_pages("/checkouts")
        day_co = [c for c in all_co if c.get("created_at","").startswith(today)]
        print(f"  {len(day_co)} checkouts today (out of {len(all_co)} total)")
        for c in day_co:
            tmpl = template_name(c.get("product", {}).get("name", "?"))
            print(f"    {tmpl:<12}  status={c.get('status')}  {c.get('customer_email','')}")
    except Exception as e:
        print(f"  ERROR: {e}")

    print(f"\n{SEP}\nframer-ranks.com — ranks-data.json\n{SEP}")
    try:
        # Auto-discover paid templates first (same as main flow)
        try:
            products_d       = polar_get("/products", {"limit": 100}).get("items", [])
            paid_tmpl_info_d = discover_paid_templates(products_d)
        except Exception:
            paid_tmpl_info_d = {}

        api_headers = {"User-Agent": UA, "Accept": "application/json, */*", "Referer": FRAMER_URL}
        r = requests.get(FRAMER_RANKS_JSON, headers=api_headers, timeout=30)
        print(f"  HTTP {r.status_code}  |  {len(r.content):,} bytes")
        if r.ok:
            data  = r.json()
            items = data.get("items", [])
            print(f"  Total items: {len(items)}")
            matched = _parse_ranks_json(items, list(paid_tmpl_info_d.keys()))
            print(f"  Your templates ({AUTHOR_NAME}) — {len(matched)} found:")
            for tmpl in sorted(matched.keys()):
                rank = matched[tmpl]
                print(f"    {tmpl:<14}  {'#' + str(rank) if rank is not None else '(not ranked yet)'}")
            # Show raw entry for first matched template
            first = next(iter(matched), None)
            if first:
                item = next((i for i in items if i.get("name","").lower() == first.lower()), None)
                if item:
                    print(f"\n  Sample entry ({first}):")
                    print(f"    name={item.get('name')}  type={item.get('type')}  "
                          f"authorName={item.get('authorName')}")
                    print(f"    ranks={json.dumps(item.get('ranks', {}), indent=6)}")
    except Exception as e:
        print(f"  ERROR: {e}")
    print()

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Easyfast collector")
    ap.add_argument("--dry-run",        action="store_true")
    ap.add_argument("--diagnose",       action="store_true")
    ap.add_argument("--date",           default=None, metavar="YYYY-MM-DD")
    ap.add_argument("--also-yesterday", action="store_true")
    args = ap.parse_args()

    if args.diagnose:
        diagnose()
        return

    target = args.date or date.today().isoformat()
    dates  = ([( date.fromisoformat(target) - timedelta(days=1)).isoformat()]
              if args.also_yesterday else []) + [target]

    print(f"\n🚀  Easyfast Collector  —  {', '.join(dates)}")
    print("━" * 52)

    # ── 1. Discover paid templates from Polar.sh ──────────────────────────────
    print("\n🔑  Polar.sh products…")
    products         = []
    paid_tmpl_info   = {}   # {template_name: price}
    try:
        products       = polar_get("/products", {"limit": 100}).get("items", [])
        paid_tmpl_info = discover_paid_templates(products)
        print(f"   {len(paid_tmpl_info)} paid templates auto-discovered:")
        for t, p in sorted(paid_tmpl_info.items()):
            print(f"      {t:<14}  ${p}")
    except Exception as e:
        print(f"   ❌ {e}")

    if not paid_tmpl_info:
        print("   ⚠  No paid templates found — nothing to collect.")
        return

    # ── 2. Fetch ranks ────────────────────────────────────────────────────────
    print("\n🏆  Framer ranks…")
    ranks = {}
    try:
        ranks = fetch_ranks(list(paid_tmpl_info.keys()))
        ranked   = {k: v for k, v in ranks.items() if v is not None}
        unranked = [k for k, v in ranks.items() if v is None]
        print(f"   {len(ranked)}/{len(paid_tmpl_info)} templates ranked:")
        for name, rank in sorted(ranked.items(), key=lambda x: x[1]):
            print(f"      #{rank:<6}  {name}")
        if unranked:
            print(f"   Not ranked yet: {', '.join(unranked)}")
    except Exception as e:
        print(f"   ❌ {e}")

    existing = load_csv()
    rows = list(existing)

    # ── 3. Fetch all Polar orders/checkouts once ───────────────────────────────
    print("\n📦  Polar.sh data…")
    all_orders, all_checkouts = [], []
    pid_to_tmpl = {p["id"]: template_name(p["name"]) for p in products
                   if template_name(p["name"]) in paid_tmpl_info}
    if products:
        try:
            all_orders    = fetch_all_pages("/orders")
            all_checkouts = fetch_all_pages("/checkouts")
            print(f"   {len(all_orders)} total orders, {len(all_checkouts)} total checkouts")
        except Exception as e:
            print(f"   ❌ {e}")

    # ── 4. Per-date aggregation ───────────────────────────────────────────────
    for d in dates:
        print(f"\n📅  {d}")
        metrics = {t: {"orders":0,"revenue":0.0,"checkouts":0,"conversion":0.0}
                   for t in paid_tmpl_info}

        day_orders    = [o for o in all_orders    if o.get("created_at","").startswith(d) and o.get("paid")]
        day_checkouts = [c for c in all_checkouts if c.get("created_at","").startswith(d)]
        print(f"      {len(day_orders)} orders, {len(day_checkouts)} checkouts")

        for o in day_orders:
            tmpl = pid_to_tmpl.get(o.get("product_id"))
            if tmpl and tmpl in metrics:
                metrics[tmpl]["orders"]  += 1
                metrics[tmpl]["revenue"] += o.get("net_amount", 0) / 100
        for c in day_checkouts:
            tmpl = pid_to_tmpl.get(c.get("product_id"))
            if tmpl and tmpl in metrics:
                metrics[tmpl]["checkouts"] += 1
        for m in metrics.values():
            if m["checkouts"] > 0:
                m["conversion"] = round(m["orders"] / m["checkouts"] * 100, 2)

        for t, m in metrics.items():
            if m["orders"] > 0 or m["checkouts"] > 0:
                print(f"      {t:<14} {m['orders']} orders  "
                      f"${m['revenue']:.0f}  {m['checkouts']} checkouts  {m['conversion']}% conv")

        rows = build_rows(rows, d, ranks, metrics, paid_tmpl_info)

    print("\n💾  Saving…")
    csv_text = save_csv(rows, dry_run=args.dry_run)
    update_dashboard(csv_text, dry_run=args.dry_run)
    print("\n✅  Done!\n")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urlparse, unquote

import requests
from dotenv import load_dotenv

load_dotenv()

WEB_API = "https://api.search.brave.com/res/v1/web/search"
LOCAL_API = "https://api.search.brave.com/res/v1/local/place_search"

SOCIAL_HOSTS = {
    "instagram.com", "www.instagram.com",
    "facebook.com", "www.facebook.com",
    "tiktok.com", "www.tiktok.com",
    "youtube.com", "www.youtube.com",
    "x.com", "twitter.com",
    "linktr.ee", "www.linktr.ee",
}
BLACKLIST_HOSTS = SOCIAL_HOSTS | {
    "google.com", "www.google.com",
    "maps.google.com", "wa.me", "api.whatsapp.com",
    "whatsapp.com", "www.whatsapp.com",
    "wikipedia.org", "www.wikipedia.org",
    "yelp.com", "www.yelp.com",
}

ORDER_TERMS = [
    "dm to order", "dm for order", "order via dm", "order through dm",
    "whatsapp to order", "whatsapp order", "order on whatsapp",
    "order via whatsapp", "inbox to order", "message to order",
    "cash on delivery", "cod available", "cod",
    "order now", "place your order", "book your order",
    "dm for price", "dm for details", "dm for orders",
    "inbox for price", "inbox us", "call or whatsapp", "whatsapp us",
    "contact to order", "order ke liye", "order karne ke liye",
    "inbox karein", "rabta karein", "pre order", "made to order",
]
DELIVERY_TERMS = [
    "delivery all over pakistan", "delivery across pakistan",
    "nationwide delivery", "shipping all over pakistan",
    "ship across pakistan", "delivery available", "home delivery",
    "free delivery", "nationwide shipping", "all pakistan delivery",
    "delivery charges", "dispatch within",
]
PRICE_TERMS = [
    "pkr", "rs.", "rs ", "price", "prices", "starting from",
    "only rs", "sale", "discount", "% off", "special offer", "buy 1 get 1",
    "buy one get one", "best price", "lowest price", "price drop",
]

PHONE_RE = re.compile(
    r"(?<!\d)(?:(?:\+92|0092)\s*|0)\s*3\d{2}[\s\-]?\d{3}[\s\-]?\d{4}(?!\d)"
)
EMAIL_RE = re.compile(r"(?i)\b[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}\b")
URL_RE = re.compile(r"https?://[^\s<>'\"()]+", re.I)

@dataclass
class Lead:
    business_name: str = ""
    instagram_url: str = ""
    facebook_url: str = ""
    other_social_url: str = ""
    city: str = ""
    category: str = ""
    phone: str = ""
    whatsapp: str = ""
    email: str = ""
    website: str = ""
    website_type: str = ""
    website_status: str = ""
    social_selling_score: int = 0
    website_confidence: int = 0
    local_business: str = ""
    local_phone: str = ""
    local_website: str = ""
    local_address: str = ""
    lead_score: int = 0
    evidence: str = ""
    source_queries: str = ""
    snippets: str = ""

def load_db(path: str):
    con = sqlite3.connect(path)
    con.execute("""
    CREATE TABLE IF NOT EXISTS leads (
        key TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    con.commit()
    return con

def host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""

def clean_url(url: str) -> str:
    return (url or "").strip().rstrip(".,);]}>'\"")

def normalize_phone(value: str) -> str:
    d = re.sub(r"\D", "", value or "")
    if d.startswith("0092"):
        return "0" + d[4:]
    if d.startswith("92") and len(d) == 12:
        return "0" + d[2:]
    return d

def first_phone(text: str) -> str:
    m = PHONE_RE.search(text or "")
    return normalize_phone(m.group(0)) if m else ""

def first_email(text: str) -> str:
    m = EMAIL_RE.search(text or "")
    return m.group(0).lower() if m else ""

def urls_in(text: str):
    return [clean_url(x) for x in URL_RE.findall(text or "")]

def social_profile(url: str):
    h = host(url)
    if h not in SOCIAL_HOSTS:
        return None
    p = urlparse(url).path.strip("/")
    if not p:
        return None
    parts = p.split("/")
    if h == "instagram.com" and parts[0].lower() in {
        "p", "reel", "reels", "stories", "explore", "accounts", "direct"
    }:
        return None
    return url

def business_name_from_title(title: str, url: str) -> str:
    s = re.sub(r"\s*[•|]\s*(Instagram|Facebook|TikTok).*?$", "", title or "", flags=re.I)
    s = re.sub(r"\s*\(@[^)]+\)", "", s).strip()
    if s:
        return s
    sp = social_profile(url)
    if sp:
        return urlparse(sp).path.strip("/").split("/")[0]
    return ""

def terms_found(text: str, terms):
    t = (text or "").lower()
    return [x for x in terms if x in t]

def brave_headers(key: str):
    return {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": key,
    }

ENGINE = "brave"
DDG_DELAY = 3.0

def parse_pages(s: str):
    try:
        if "-" in s:
            a, b = s.split("-", 1)
            start, end = int(a), int(b)
        else:
            start, end = 1, int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid --pages '{s}' (use N or START-END, e.g. 3 or 2-4)")
    if start < 1 or end < start:
        raise argparse.ArgumentTypeError(f"invalid --pages '{s}' (need 1 <= START <= END)")
    return start, end

def ddg_search(query: str, pages=(1, 2), count=20):
    from ddgs import DDGS
    out = []
    for page in range(pages[0], pages[1] + 1):
        results = []
        for attempt in range(3):
            try:
                results = DDGS().text(
                    query, region="pk-en", safesearch="moderate",
                    max_results=min(count, 20), page=page,
                )
                break
            except Exception as e:
                if "No results" in str(e):
                    break
                wait = DDG_DELAY * (attempt + 2) * 2
                print(f"[ddg error] {e} - retrying in {wait:.0f}s")
                time.sleep(wait)
        for r in results or []:
            out.append({
                "url": r.get("href", ""),
                "title": r.get("title", ""),
                "description": r.get("body", ""),
            })
        time.sleep(DDG_DELAY)
        if not results:
            break
    return out

def web_search(key: str, query: str, pages=(1, 2), count=20):
    if ENGINE == "ddg":
        return ddg_search(query, pages, count)
    out = []
    for page in range(pages[0], pages[1] + 1):
        params = {
            "q": query,
            "count": min(count, 20),
            "offset": page - 1,
            "country": "PK",
            "search_lang": "en",
            "safesearch": "moderate",
            "extra_snippets": "true",
            "operators": "true",
        }
        try:
            r = requests.get(WEB_API, headers=brave_headers(key), params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            print(f"[web error] {e}")
            break
        results = data.get("web", {}).get("results", []) or []
        out.extend(results)
        if not data.get("query", {}).get("more_results_available"):
            break
        time.sleep(0.2)
    return out

def local_search(key: str, city: str, category: str):
    params = {
        "q": f"{category} {city}",
        "country": "PK",
        "search_lang": "en",
        "count": 20,
        "location": f"{city} Pakistan",
    }
    try:
        r = requests.get(LOCAL_API, headers=brave_headers(key), params=params, timeout=30)
        r.raise_for_status()
        return r.json().get("results", []) or []
    except requests.RequestException:
        return []

def make_queries(city, category, platform="both"):
    phrases = [
        # original
        '"DM to order"', '"WhatsApp to order"', '"order via WhatsApp"',
        '"cash on delivery"', '"COD available"', '"delivery all over Pakistan"',
        '"PKR"', '"inbox to order"',
        # ordering
        '"order now"', '"place your order"', '"book your order"',
        '"DM for price"', '"DM for details"', '"DM for orders"',
        '"inbox for price"', '"inbox us"', '"message us to order"',
        '"call or WhatsApp"', '"WhatsApp us"', '"contact to order"',
        '"order ke liye"', '"order karne ke liye"', '"inbox karein"', '"rabta karein"',
        # payment and delivery
        '"advance payment"', '"easypaisa"', '"jazzcash"', '"bank transfer"',
        '"free delivery"', '"delivery charges"', '"nationwide shipping"',
        '"all Pakistan delivery"', '"courier"', '"TCS"', '"Leopards"', '"Trax"',
        '"delivery in 3-5 days"', '"dispatch within"',
        # pricing and stock
        '"Rs."', '"only Rs"', '"limited stock"', '"in stock"', '"restocked"',
        '"new arrival"', '"new collection"', '"pre order"', '"made to order"',
        '"customized"',
        # sale and discounts
        '"sale"', '"sale sale sale"', '"flat 20% off"', '"flat 50% off"',
        '"up to 50% off"', '"discount"', '"discount offer"', '"special offer"',
        '"limited time offer"', '"clearance sale"', '"end of season sale"',
        '"eid sale"', '"summer sale"', '"winter sale"', '"buy 1 get 1"',
        '"buy one get one"', '"big sale"', '"mega sale"', '"on sale now"',
        '"price drop"', '"best price"', '"lowest price"', '"offer ends"',
        '"hurry up"', '"grab now"', '"while stocks last"',
        # small-seller identity
        '"home based"', '"home business"', '"small business"', '"online store"',
        '"shop online"', '"online shopping Pakistan"', '"online boutique"',
        '"handmade"', '"locally made"', '"women owned"',
    ]
    sites = {"instagram": ["instagram.com"], "facebook": ["facebook.com"],
             "both": ["instagram.com", "facebook.com"]}[platform]
    qs = []
    for phrase in phrases:
        for s in sites:
            qs.append(f'site:{s} "{city}" "{category}" {phrase}')
    for s in sites:
        qs.append(f'site:{s} "{city}" "{category}"')
    return qs

def discover(key, city, category, pages, per_query, platform="both"):
    leads = {}
    for q in make_queries(city, category, platform):
        print(f"[search] {q}")
        for r in web_search(key, q, pages=pages, count=per_query):
            url = clean_url(r.get("url", ""))
            title = r.get("title", "")
            desc = r.get("description", "")
            extras = " ".join(r.get("extra_snippets", []) or [])
            text = f"{title} {desc} {extras}"

            ig = ""
            fb = ""
            other = ""
            sp = social_profile(url)
            if sp:
                h = host(sp)
                if h == "instagram.com":
                    ig = sp
                elif h == "facebook.com":
                    fb = sp
                else:
                    other = sp
            if not (ig or fb or other):
                continue

            key_id = (ig or fb or other).lower()
            if key_id not in leads:
                leads[key_id] = Lead(
                    business_name=business_name_from_title(title, url),
                    instagram_url=ig,
                    facebook_url=fb,
                    other_social_url=other,
                    city=city,
                    category=category,
                    phone=first_phone(text),
                    whatsapp=first_phone(text) if "whatsapp" in text.lower() else "",
                    email=first_email(text),
                    source_queries=q,
                    snippets=text[:2500],
                )
            else:
                old = leads[key_id]
                old.source_queries += " | " + q
                old.snippets += " | " + text[:1500]
                old.phone = old.phone or first_phone(text)
                old.whatsapp = old.whatsapp or (first_phone(text) if "whatsapp" in text.lower() else "")
                old.email = old.email or first_email(text)
                old.business_name = old.business_name or business_name_from_title(title, url)
    return list(leads.values())

def enrich_local(key, lead: Lead):
    if not lead.business_name:
        return lead
    results = local_search(key, lead.city, lead.business_name)
    for r in results:
        title = r.get("title", "")
        if not title:
            continue
        lead.local_business = title
        lead.local_phone = lead.local_phone or r.get("phone", "") or ""
        lead.local_website = lead.local_website or r.get("website", "") or ""
        loc = r.get("address", {}) or {}
        if isinstance(loc, dict):
            lead.local_address = ", ".join(
                str(x) for x in [
                    loc.get("streetAddress"),
                    loc.get("addressLocality"),
                    loc.get("addressRegion"),
                    loc.get("postalCode"),
                ] if x
            )
        break
    return lead

def classify_website(url: str, html: str):
    h = host(url)
    if not h or h in BLACKLIST_HOSTS:
        return "social/profile"

    t = (html or "").lower()
    ecommerce_terms = [
        "add to cart", "add-to-cart", "buy now", "checkout",
        "shopping cart", "woocommerce", "shopify", "product-price",
        "productform", "cart page",
    ]
    business_terms = [
        "contact us", "about us", "our services", "our products",
        "get in touch", "location", "privacy policy",
    ]
    if any(x in t for x in ecommerce_terms):
        return "ecommerce"
    if any(x in t for x in business_terms):
        return "business/brochure"
    return "unknown"

def probe_website(domain: str):
    if not domain or domain in BLACKLIST_HOSTS:
        return None, None
    for scheme in ("https://", "http://"):
        try:
            r = requests.get(
                scheme + domain,
                timeout=8,
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; LeadResearch/2.0)"},
            )
            final_host = host(r.url)
            if final_host and final_host not in BLACKLIST_HOSTS and r.status_code < 500:
                return r.url, r.text[:500000]
        except requests.RequestException:
            pass
    return None, None

def website_enrich(key, lead: Lead):
    candidates = set()

    if lead.local_website:
        candidates.add(host(lead.local_website))

    for u in urls_in(lead.snippets):
        h = host(u)
        if h and h not in BLACKLIST_HOSTS:
            candidates.add(h)

    # Search exact business name and city for an independent domain.
    if lead.business_name:
        q = f'"{lead.business_name}" "{lead.city}"'
        for r in web_search(key, q, pages=(1, 1), count=10):
            h = host(r.get("url", ""))
            if h and h not in BLACKLIST_HOSTS:
                candidates.add(h)

    for d in list(candidates)[:10]:
        final_url, html = probe_website(d)
        if final_url:
            lead.website = final_url
            lead.website_type = classify_website(final_url, html)
            lead.website_status = "WEBSITE_FOUND"
            lead.website_confidence = 85 if d == host(lead.local_website) and lead.local_website else 70
            return lead

    lead.website_status = "NO_WEBSITE_LIKELY"
    lead.website_type = ""
    lead.website_confidence = 65
    return lead

def score_social(lead: Lead):
    text = (lead.snippets + " " + lead.source_queries).lower()
    score = 0
    evidence = []

    if lead.instagram_url:
        score += 25
        evidence.append("Instagram profile")
    if lead.facebook_url:
        score += 10
        evidence.append("Facebook page")
    if terms_found(text, ORDER_TERMS):
        score += 25
        evidence.append("ordering language")
    if "whatsapp" in text:
        score += 15
        evidence.append("WhatsApp")
    if "cod" in text or "cash on delivery" in text:
        score += 10
        evidence.append("COD")
    if terms_found(text, DELIVERY_TERMS):
        score += 10
        evidence.append("delivery")
    if terms_found(text, PRICE_TERMS):
        score += 10
        evidence.append("price/Pakistani currency")
    if lead.phone:
        score += 5
        evidence.append("phone")
    if lead.email:
        score += 5
        evidence.append("email")
    if lead.local_business:
        score += 5
        evidence.append("local business match")

    lead.social_selling_score = min(score, 100)
    lead.evidence = "; ".join(evidence)
    return lead

def final_score(lead: Lead):
    s = lead.social_selling_score
    if lead.website_status == "WEBSITE_FOUND":
        if lead.website_type == "ecommerce":
            s -= 70
        else:
            s -= 30
    elif lead.website_status == "NO_WEBSITE_LIKELY":
        s += 15
    if lead.whatsapp:
        s += 5
    if lead.local_business:
        s += 5
    lead.lead_score = max(0, min(100, s))
    return lead

def lead_key(l: Lead) -> str:
    return (l.instagram_url or l.facebook_url or l.business_name).lower().strip()

def merge(leads):
    merged = {}
    for l in leads:
        key = lead_key(l)
        if not key:
            continue
        if key not in merged:
            merged[key] = l
        else:
            a = merged[key]
            a.instagram_url = a.instagram_url or l.instagram_url
            a.facebook_url = a.facebook_url or l.facebook_url
            a.other_social_url = a.other_social_url or l.other_social_url
            a.phone = a.phone or l.phone
            a.whatsapp = a.whatsapp or l.whatsapp
            a.email = a.email or l.email
            a.business_name = a.business_name or l.business_name
            a.snippets = (a.snippets + " | " + l.snippets)[:8000]
            a.source_queries = (a.source_queries + " | " + l.source_queries)[:5000]
            if l.social_selling_score > a.social_selling_score:
                a.social_selling_score = l.social_selling_score
    return list(merged.values())

def db_keys(con) -> set:
    return {r[0] for r in con.execute("SELECT key FROM leads")}

def load_all_leads(con):
    fields = set(Lead.__dataclass_fields__)
    out = []
    for (data,) in con.execute("SELECT data FROM leads"):
        d = json.loads(data)
        out.append(Lead(**{k: v for k, v in d.items() if k in fields}))
    return out

def save_db(con, leads):
    for l in leads:
        key = lead_key(l)
        if not key:
            continue
        con.execute(
            "INSERT OR REPLACE INTO leads(key, data, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (key, json.dumps(asdict(l), ensure_ascii=False)),
        )
    con.commit()

def write_csv(leads, path):
    rows = [asdict(x) for x in leads]
    fields = list(Lead.__dataclass_fields__.keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

def split_leads(leads):
    no_site = [x for x in leads if x.website_status != "WEBSITE_FOUND"]
    with_site = [x for x in leads if x.website_status == "WEBSITE_FOUND"]
    return no_site, with_site

def write_xlsx(no_site, with_site, path):
    from openpyxl import Workbook
    fields = list(Lead.__dataclass_fields__.keys())
    wb = Workbook()
    for title, items in (("No Website", no_site), ("Website", with_site)):
        ws = wb.active if title == "No Website" else wb.create_sheet()
        ws.title = title
        ws.append(fields)
        for x in items:
            ws.append([asdict(x)[f] for f in fields])
        ws.freeze_panes = "A2"
    wb.save(path)

def write_json(leads, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(x) for x in leads], f, ensure_ascii=False, indent=2)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", nargs="+", default=["Karachi"])
    ap.add_argument("--categories", nargs="+", default=["clothing"])
    ap.add_argument("--pages", type=parse_pages, default=(1, 2), metavar="N|START-END",
                    help="search pages: 3 = pages 1-3, 2-4 = pages 2 to 4 (default 1-2)")
    ap.add_argument("--per-query", type=int, default=20)
    ap.add_argument("--output", default="leads.csv")
    ap.add_argument("--json-output", default="")
    ap.add_argument("--db", default="leads.sqlite")
    ap.add_argument("--engine", choices=["brave", "ddg"], default="ddg",
                    help="brave = Brave API (needs key); ddg = free DuckDuckGo, no key, slower")
    ap.add_argument("--platform", choices=["instagram", "facebook", "both"], default="both",
                    help="which social site to search (default: both)")
    ap.add_argument("--skip-local", action="store_true")
    ap.add_argument("--skip-website", action="store_true")
    args = ap.parse_args()

    global ENGINE
    ENGINE = args.engine
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    key = os.getenv("BRAVE_SEARCH_API_KEY")
    if ENGINE == "ddg":
        args.skip_local = True  # Brave local places API is not available on DDG
    elif not key:
        raise SystemExit("Set BRAVE_SEARCH_API_KEY in .env (or use --engine ddg)")

    all_leads = []
    for city in args.cities:
        for category in args.categories:
            print(f"\n=== {city} / {category} ===")
            all_leads.extend(
                discover(key, city, category, args.pages, args.per_query, args.platform)
            )

    all_leads = merge(all_leads)
    con = load_db(args.db)
    known = db_keys(con)
    found = len(all_leads)
    all_leads = [x for x in all_leads if lead_key(x) not in known]
    print(f"[discovery] {found} unique profiles, {found - len(all_leads)} already in DB, {len(all_leads)} new")

    for i, lead in enumerate(all_leads, 1):
        print(f"[enrich {i}/{len(all_leads)}] {lead.business_name}")
        score_social(lead)
        if not args.skip_local:
            lead = enrich_local(key, lead)
        if not args.skip_website:
            lead = website_enrich(key, lead)
        final_score(lead)
        time.sleep(0.1)

    new_leads = merge(all_leads)
    save_db(con, new_leads)
    all_leads = load_all_leads(con)
    con.close()
    all_leads.sort(key=lambda x: x.lead_score, reverse=True)

    write_csv(all_leads, args.output)
    no_site, with_site = split_leads(all_leads)
    base, _ = os.path.splitext(args.output)
    write_csv(no_site, base + "_no_website.csv")
    write_csv(with_site, base + "_website.csv")
    write_xlsx(no_site, with_site, base + ".xlsx")
    if args.json_output:
        write_json(all_leads, args.json_output)

    strong = sum(
        1 for x in all_leads
        if x.website_status == "NO_WEBSITE_LIKELY" and x.lead_score >= 60
    )

    print("\n========== DONE ==========")
    print(f"New leads this run: {len(new_leads)}")
    print(f"Total leads (all runs): {len(all_leads)}")
    print(f"Strong no-website leads: {strong}")
    print(f"CSV (all): {args.output}")
    print(f"No-website: {len(no_site)} | With website: {len(with_site)}")
    print(f"Excel (2 sheets): {base}.xlsx")
    print(f"Database: {args.db}")
    if args.json_output:
        print(f"JSON: {args.json_output}")

if __name__ == "__main__":
    main()

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
]
DELIVERY_TERMS = [
    "delivery all over pakistan", "delivery across pakistan",
    "nationwide delivery", "shipping all over pakistan",
    "ship across pakistan", "delivery available", "home delivery",
]
PRICE_TERMS = ["pkr", "rs.", "rs ", "price", "prices", "starting from"]

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

def ddg_search(query: str, pages=2, count=20):
    from ddgs import DDGS
    out = []
    for page in range(1, pages + 1):
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

def web_search(key: str, query: str, pages=2, count=20):
    if ENGINE == "ddg":
        return ddg_search(query, pages, count)
    out = []
    for page in range(pages):
        params = {
            "q": query,
            "count": min(count, 20),
            "offset": page,
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

def make_queries(city, category):
    phrases = [
        '"DM to order"', '"WhatsApp to order"', '"order via WhatsApp"',
        '"cash on delivery"', '"COD available"', '"delivery all over Pakistan"',
        '"PKR"', '"inbox to order"',
    ]
    qs = []
    for phrase in phrases:
        qs.append(f'site:instagram.com "{city}" "{category}" {phrase}')
        qs.append(f'site:facebook.com "{city}" "{category}" {phrase}')
    qs.append(f'site:instagram.com "{city}" "{category}"')
    qs.append(f'site:facebook.com "{city}" "{category}"')
    return qs

def discover(key, city, category, pages, per_query):
    leads = {}
    for q in make_queries(city, category):
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
        for r in web_search(key, q, pages=1, count=10):
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

def merge(leads):
    merged = {}
    for l in leads:
        key = (l.instagram_url or l.facebook_url or l.business_name).lower().strip()
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

def save_db(con, leads):
    for l in leads:
        key = (l.instagram_url or l.facebook_url or l.business_name).lower().strip()
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
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--per-query", type=int, default=20)
    ap.add_argument("--output", default="leads.csv")
    ap.add_argument("--json-output", default="")
    ap.add_argument("--db", default="leads.sqlite")
    ap.add_argument("--engine", choices=["brave", "ddg"], default="brave",
                    help="brave = Brave API (needs key); ddg = free DuckDuckGo, no key, slower")
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
                discover(key, city, category, args.pages, args.per_query)
            )

    all_leads = merge(all_leads)
    print(f"[discovery] {len(all_leads)} unique social profiles")

    for i, lead in enumerate(all_leads, 1):
        print(f"[enrich {i}/{len(all_leads)}] {lead.business_name}")
        score_social(lead)
        if not args.skip_local:
            lead = enrich_local(key, lead)
        if not args.skip_website:
            lead = website_enrich(key, lead)
        final_score(lead)
        time.sleep(0.1)

    all_leads = merge(all_leads)
    all_leads.sort(key=lambda x: x.lead_score, reverse=True)

    con = load_db(args.db)
    save_db(con, all_leads)
    con.close()

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
    print(f"Total leads: {len(all_leads)}")
    print(f"Strong no-website leads: {strong}")
    print(f"CSV (all): {args.output}")
    print(f"No-website: {len(no_site)} | With website: {len(with_site)}")
    print(f"Excel (2 sheets): {base}.xlsx")
    print(f"Database: {args.db}")
    if args.json_output:
        print(f"JSON: {args.json_output}")

if __name__ == "__main__":
    main()

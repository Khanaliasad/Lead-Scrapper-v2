# Social Commerce Lead Scraper — v2

Finds Pakistani businesses that sell through **Instagram / Facebook** and appear to have **no website**. These are prospects for web, ecommerce and SaaS services.

> A visual version of this guide is in `GUIDE.html` (open it in any browser).

## Purpose

Many small Pakistani shops take orders by DM or WhatsApp. They are good prospects for an online store, but there is no directory of them. v2 builds one from public search results: it searches for ordering language ("DM to order", "COD available", "PKR"), collects the profiles it finds, checks whether each business has an independent website, scores it, and exports ready-to-use lists.

It does **not** log in to Instagram/Facebook, bypass bot protection, or use private APIs. It reads only what a search engine already shows publicly.

## How it works

1. **Discover** – for each city × category it builds 18 queries (8 buying phrases × Instagram and Facebook, plus 2 plain queries), e.g. `site:instagram.com "Karachi" "clothing" "DM to order"`.
2. **Extract** – from each result it keeps real profile URLs only (posts/reels/stories are dropped) and pulls the business name, Pakistani mobile number (03xx / +92 3xx), email and WhatsApp number from the title and snippets.
3. **Merge** – the same profile found by several queries becomes one lead.
4. **Social score** (0–100) – evidence the business sells via social media.
5. **Local lookup** *(Brave only)* – Brave local places search adds address/phone/website.
6. **Website check** – collects candidate domains (local result, URLs in snippets, a search for `"business name" "city"`), loads up to 10, and classifies the first live one as `ecommerce`, `business/brochure` or `unknown`. If none loads the lead is `NO_WEBSITE_LIKELY`.
7. **Final lead score** (0–100) and export.

## Search engines

| | `--engine brave` (default) | `--engine ddg` |
|---|---|---|
| Key needed | Yes – `BRAVE_SEARCH_API_KEY` | No |
| Cost | ~US$5 per 1,000 requests (verify on dashboard) | Free |
| Speed | Fast | Slow (3 s pause between searches) |
| Local places lookup | Yes | No (auto-skipped) |
| Result quality | Cleaner, longer snippets | More directory pages, shorter snippets |
| Rate limits | Plan-based | Soft blocks are common; the tool retries with back-off |

## Setup

Python 3.10+.

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

For Brave only: copy `.env.example` to `.env` and set

```env
BRAVE_SEARCH_API_KEY=your_key_here
```

### Getting a Brave key
1. Register at https://api-dashboard.search.brave.com/register
2. Add a credit card (required even for free credits; it becomes a live billing card once free credit is used).
3. Pick the Search plan and open **API Keys → generate a subscription token**.
4. Set a spending cap if the dashboard offers one.

## Usage

```bash
# small test (recommended first run)
python lead_scraper_v2.py --cities Karachi --categories clothing --pages 1

# free, no key
python lead_scraper_v2.py --engine ddg --cities Karachi --categories clothing --pages 1

# bigger scan
python lead_scraper_v2.py --cities Karachi Lahore Islamabad --categories clothing abaya cosmetics --pages 2 --skip-local --output pakistan.csv
```

| Option | Default | Meaning |
|---|---|---|
| `--cities` | Karachi | One or more cities |
| `--categories` | clothing | One or more niches (quote multi-word ones) |
| `--pages` | 2 | Result pages per query |
| `--per-query` | 20 | Results per page (max 20) |
| `--output` | leads.csv | Base name for output files |
| `--json-output` | – | Also write JSON (e.g. for a Laravel importer) |
| `--db` | leads.sqlite | SQLite file for dedupe across runs |
| `--engine` | brave | `brave` or `ddg` |
| `--skip-local` | off | Skip Brave local places lookup (saves ~half the enrichment calls) |
| `--skip-website` | off | Skip the website check (faster; leads stay "no website") |

## Output

With `--output leads.csv`:

| File | Contents |
|---|---|
| `leads.csv` | All leads |
| `leads_no_website.csv` | Leads with no detected website (your main prospects) |
| `leads_website.csv` | Leads where a website was found |
| `leads.xlsx` | Same two lists as sheets **No Website** and **Website** |
| `leads.sqlite` | Persistent store keyed by profile URL |
| JSON | Only with `--json-output` |

Columns: `business_name`, `instagram_url`, `facebook_url`, `other_social_url`, `city`, `category`, `phone`, `whatsapp`, `email`, `website`, `website_type`, `website_status`, `social_selling_score`, `website_confidence`, `local_business`, `local_phone`, `local_website`, `local_address`, `lead_score`, `evidence`, `source_queries`, `snippets`.

## Scoring

**Social selling score (max 100):** Instagram profile +25, ordering language +25, WhatsApp +15, Facebook page +10, COD +10, delivery +10, price/PKR +10, phone +5, email +5, local business match +5.

**Lead score** starts from the social score, then: no website +15, website found −30, ecommerce site −70, WhatsApp number +5, local match +5 (clamped 0–100). A "strong" lead is `NO_WEBSITE_LIKELY` with score ≥ 60.

## Cost estimate (Brave)

Requests ≈ 18 × pages per city/category pair, plus ~2 per lead (local + website search). A 1-city/1-category test with `--pages 1` is ~100 requests (~$0.50). The full 8-city × 9-category scan is ~8,000+ requests (~$40). Use `--skip-local` and scale one city at a time.

## Limitations

- `NO_WEBSITE_LIKELY` is a heuristic, not proof. A new, unindexed or private site can be missed.
- Only search snippets are read, not full profiles, so many leads have no phone/email.
- The website check can take up to ~8 s per candidate domain, so large runs are slow.
- The phone pattern matches Pakistani mobiles only, not landlines.
- DuckDuckGo may return directory pages (e.g. "Karachi Clothing Shops") rather than single businesses.

## Responsible use

Contact data is public but its use for outreach is subject to local law, platform terms and anti-spam rules. Review leads before contacting anyone.

## Files

```
lead_scraper_v2.py   main script
requirements.txt     dependencies
.env.example         template for the Brave key
config_examples.txt  example commands
GUIDE.html           visual user guide
```

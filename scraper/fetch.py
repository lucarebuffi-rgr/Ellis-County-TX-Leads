#!/usr/bin/env python3
"""
Ellis County TX – Motivated Seller Lead Scraper
Clerk  : public.lgsonlinesolutions.com/ors.html
CAD    : Ellis CAD via Google Drive (fixed-width)
"""

import asyncio
import csv
import io
import json
import logging
import os
import re
import traceback
import zipfile
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

import gdown

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

BASE_URL        = "https://public.lgsonlinesolutions.com/ors.html"
LGS_USERNAME    = os.getenv("LGS_USERNAME", "")
LGS_PASSWORD    = os.getenv("LGS_PASSWORD", "")
GDRIVE_FILE_ID  = "1Y-bAKgEZ9jRRBPMhgUMbiyjZPbi9OMtP"
LOOKBACK_DAYS   = int(os.getenv("LOOKBACK_DAYS", "14"))

DOC_TYPES = {
    "Lis Pendens"         : ("pre_foreclosure", "Lis Pendens"),
    "Federal Tax"         : ("lien",            "Federal Tax Lien"),
    "State Tax Lien"      : ("lien",            "State Tax Lien"),
    "Abstract of Judgment": ("judgment",        "Abstract of Judgment"),
    "Judgment"            : ("judgment",        "Judgment"),
    "Probate"             : ("probate",         "Probate"),
    "Affidavit Heirs"     : ("probate",         "Affidavit of Heirship"),
    "Lien"                : ("lien",            "Lien"),
    "Mechanic Lien"       : ("lien",            "Mechanics Lien"),
    "Hospital Lien"       : ("lien",            "Hospital Lien"),
    "Divorce Decree"      : ("other",           "Divorce Decree"),
}

GRANTEE_IS_OWNER = {"Lien", "Federal Tax", "State Tax Lien", "Judgment",
                    "Abstract of Judgment", "Hospital Lien", "Mechanic Lien"}

NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V", "ESQ", "TRUSTEE", "TR",
                 "ETAL", "ET", "AL", "ET AL", "ETUX", "ET UX", "ESTATE"}

ENTITY_FILTERS = (
    "LLC", "INC", "CORP", "LTD", "LP ", "L.P.", "TRUST", "ASSOC", "HOMEOWNERS",
    "STATE OF", "CITY OF", "COUNTY OF", "DISTRICT", "MUNICIPALITY", "DEPT ",
    "ISD", "UTILITY", "AUTHORITY", "COMMISSION", "FEDERAL", "NATIONAL BANK",
    "MORTGAGE", "FINANCIAL", "INVESTMENT", "PROPERTIES", "REALTY", "HOLDINGS",
    "PARTNERS", "GROUP", "SERVICES", "MANAGEMENT", "SOLUTIONS", "ENTERPRISES",
    "N/A", "UNKNOWN", "PUBLIC", "ATTY GEN", "ATTY/GEN", "ELLIS COUNTY",
    "CITY OF ENNIS", "CITY OF WAXAHACHIE"
)

# ── Fixed-width column positions (same as Denton) ─────────────────────────────
ACCT_S,  ACCT_E  = 596,  608
NAME_S,  NAME_E  = 608,  658
ADDR_S,  ADDR_E  = 693,  743
CITY_S,  CITY_E  = 873,  923
STAT_S,  STAT_E  = 923,  925
ZIP_S,   ZIP_E   = 978,  987
SNUM_S,  SNUM_E  = 4443, 4463
SITUS_S, SITUS_E = 1049, 1099
SCITY_S, SCITY_E = 1109, 1139
SZIP_S,  SZIP_E  = 1139, 1149
PCLS_S,  PCLS_E  = 2731, 2741


def parse_date(raw: str) -> Optional[str]:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def strip_suffixes(tokens: list) -> list:
    return [t for t in tokens if t not in NAME_SUFFIXES]


def name_variants(full: str) -> list:
    full = re.sub(r"[^\w\s]", "", full.strip().upper())
    tokens = strip_suffixes(full.split())
    if not tokens:
        return [full]
    variants = set()
    variants.add(" ".join(tokens))
    if len(tokens) < 2:
        return list(variants)
    last  = tokens[0]
    first = tokens[1] if len(tokens) > 1 else ""
    mid   = tokens[2] if len(tokens) > 2 else ""
    variants.add(f"{last} {first} {mid}".strip())
    variants.add(f"{last}, {first} {mid}".strip())
    variants.add(f"{last} {first}")
    variants.add(f"{last}, {first}")
    variants.add(f"{first} {last}")
    if mid:
        variants.add(f"{first} {mid} {last}")
        variants.add(f"{first} {last}")
        if len(mid) == 1:
            variants.add(f"{last} {first}")
    return [v for v in variants if v]


def normalize_for_fuzzy(name: str) -> tuple:
    name = re.sub(r"[^\w\s]", "", name.strip().upper())
    tokens = strip_suffixes(name.split())
    filtered = [t for t in tokens if len(t) > 1]
    if len(filtered) >= 2:
        tokens = filtered
    if not tokens:
        return ("", set())
    return tokens[0], set(tokens[1:])


def is_entity(name: str) -> bool:
    n = name.strip().upper()
    if not n or n in ("N/A", "NA", "UNKNOWN", "PUBLIC", ""):
        return True
    tokens = [t for t in re.sub(r"[^\w\s]", "", n).split() if len(t) > 1]
    if len(tokens) < 2:
        return True
    return any(x in n for x in ENTITY_FILTERS)


# ── CAD LOADER ────────────────────────────────────────────────────────────────

def build_parcel_lookup() -> dict:
    lookup = {}
    log.info("Downloading Ellis CAD data via Google Drive ...")
    try:
        tmp_path = "/tmp/ellis_cad.zip"
        url = f"https://drive.google.com/uc?export=download&id={GDRIVE_FILE_ID}&confirm=t"
        gdown.download(url=url, output=tmp_path, quiet=False)

        zf    = zipfile.ZipFile(tmp_path)
        fname = next(
            (n for n in zf.namelist() if "APPRAISAL_INFO" in n.upper()),
            None
        )
        if not fname:
            log.error("Could not find APPRAISAL_INFO.TXT in ZIP")
            return lookup

        log.info(f"  Parsing {fname} ...")
        raw   = zf.read(fname).decode("latin-1")
        total = 0

        for line in raw.splitlines():
            if len(line) < PCLS_E:
                continue
            prop_class = line[PCLS_S:PCLS_E].strip()
            if not prop_class.startswith("A"):
                continue
            owner_name = line[NAME_S:NAME_E].strip().upper()
            if not owner_name or is_entity(owner_name):
                continue
            acct       = line[ACCT_S:ACCT_E].strip().lstrip("0") or line[ACCT_S:ACCT_E].strip()
            mail_addr  = line[ADDR_S:ADDR_E].strip()
            mail_city  = line[CITY_S:CITY_E].strip()
            mail_state = line[STAT_S:STAT_E].strip() or "TX"
            mail_zip   = line[ZIP_S:ZIP_E].strip()[:5]
            situs_num  = line[SNUM_S:SNUM_E].strip() if len(line) > SNUM_E else ""
            situs_st   = f"{situs_num} {line[SITUS_S:SITUS_E].strip()}".strip()
            situs_city = line[SCITY_S:SCITY_E].strip()
            situs_zip  = line[SZIP_S:SZIP_E].strip()[:5]

            parcel = {
                "prop_address": situs_st,
                "prop_city":    situs_city or "Waxahachie",
                "prop_state":   "TX",
                "prop_zip":     situs_zip,
                "mail_address": mail_addr,
                "mail_city":    mail_city,
                "mail_state":   mail_state,
                "mail_zip":     mail_zip,
            }
            for variant in name_variants(owner_name):
                lookup[variant] = parcel
            total += 1
            if total % 10000 == 0:
                log.info(f"  Processed {total:,} parcels ...")

        log.info(f"Ellis CAD lookup: {len(lookup):,} name variants from {total:,} parcels")
    except Exception:
        log.error(f"CAD lookup error:\n{traceback.format_exc()}")
    return lookup


# ── PLAYWRIGHT SCRAPER ────────────────────────────────────────────────────────

async def lgs_login(page) -> bool:
    try:
        await page.goto(BASE_URL, timeout=60_000, wait_until="networkidle")
        await page.wait_for_timeout(3000)

        # Try multiple selectors for username field
        username_sel = 'input[name="username"], input[name="user"], input[type="text"]:first-of-type'
        await page.wait_for_selector(username_sel, timeout=30_000)
        await page.fill(username_sel, LGS_USERNAME)

        password_sel = 'input[name="password"], input[type="password"]'
        await page.wait_for_selector(password_sel, timeout=10_000)
        await page.fill(password_sel, LGS_PASSWORD)

        await page.click('input[type="submit"], button[type="submit"], input[value="Login"], input[value="Log In"]')
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(2000)
        log.info("  Logged in to LGS")
        return True
    except Exception as e:
        log.error(f"  Login failed: {e}")
        return False


async def scrape_doc_type(page, rec_type: str, cat: str, cat_label: str,
                           date_from: str, date_to: str) -> list:
    records = []
    try:
        await page.goto(BASE_URL, timeout=60_000)
        await page.wait_for_load_state("domcontentloaded")

        # Select Ellis County Clerk
        await page.select_option('select', label="Ellis County Clerk")
        await page.wait_for_timeout(1000)

        # Click Property button
        await page.click('input[value="Property"], button:has-text("Property")')
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(1000)

        # Fill search form
        await page.fill('input[name="RecordType"]', rec_type)
        await page.fill('input[name="BegDate"]', date_from)
        await page.fill('input[name="EndDate"]', date_to)

        # Submit
        await page.click('input[value="Search"]')
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(2000)

        # Parse results table
        rows = await page.query_selector_all("table tr")
        for row in rows:
            cells = await row.query_selector_all("td")
            if len(cells) < 5:
                continue
            texts = [await c.inner_text() for c in cells]
            instrument   = texts[0].strip()
            date_raw     = texts[1].strip()
            name         = texts[2].strip()
            name_type    = texts[3].strip()
            rec_type_val = texts[4].strip()
            legal        = texts[5].strip() if len(texts) > 5 else ""

            if not instrument or not name:
                continue

            if name_type.upper() == "GRANTOR":
                grantor = name
                grantee = ""
            else:
                grantor = ""
                grantee = name

            records.append({
                "doc_num"  : instrument,
                "doc_type" : rec_type,
                "cat"      : cat,
                "cat_label": cat_label,
                "filed"    : parse_date(date_raw) or date_raw,
                "grantor"  : grantor,
                "grantee"  : grantee,
                "legal"    : legal,
                "amount"   : None,
                "clerk_url": BASE_URL,
                "_demo"    : False,
            })

        log.info(f"  {rec_type}: {len(records)} rows")

    except Exception as e:
        log.warning(f"  Error scraping {rec_type}: {e}")

    return records


async def scrape_all(date_from: str, date_to: str) -> list:
    if not HAS_PLAYWRIGHT:
        log.error("Playwright not available!")
        return []

    all_records = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        logged_in = await lgs_login(page)
        if not logged_in:
            log.error("Could not log in — aborting")
            await browser.close()
            return []

        for rec_type, (cat, cat_label) in DOC_TYPES.items():
            try:
                recs = await scrape_doc_type(page, rec_type, cat, cat_label,
                                             date_from, date_to)
                all_records.extend(recs)
            except Exception as e:
                log.warning(f"  Failed {rec_type}: {e}")

        await browser.close()

    return all_records


# ── DEMO DATA ─────────────────────────────────────────────────────────────────

def generate_demo_records(date_from: str, date_to: str) -> list:
    samples = [
        ("Lis Pendens",          "pre_foreclosure", "Lis Pendens",          "SMITH ROBERT",    "ROCKET MORTGAGE",  0),
        ("Judgment",             "judgment",        "Judgment",             "JONES MARY B",    "CAPITAL ONE",  87500),
        ("Federal Tax",          "lien",            "Federal Tax Lien",     "WILLIAMS DAVID",  "IRS",          45200),
        ("Abstract of Judgment", "judgment",        "Abstract of Judgment", "JOHNSON PAT",     "CITIBANK",     18700),
        ("Mechanic Lien",        "lien",            "Mechanics Lien",       "BROWN MICHAEL",   "ACME CONTR",   22000),
        ("Probate",              "probate",         "Probate",              "DAVIS JAMES EST", "ELLIS PROBATE",    0),
        ("State Tax Lien",       "lien",            "State Tax Lien",       "HENDERSON BOB",   "STATE OF TX",   9800),
        ("Lien",                 "lien",            "Lien",                 "RODRIGUEZ JUAN",  "WAXAHACHIE",    5000),
        ("Affidavit Heirs",      "probate",         "Affidavit of Heirship","GARCIA CARLOS",   "GARCIA MARIA",     0),
    ]
    base = datetime.strptime(date_from, "%m/%d/%Y")
    recs = []
    for i, (code, cat, cat_label, grantor, grantee, amt) in enumerate(samples):
        filed_dt = base + timedelta(days=i % LOOKBACK_DAYS)
        recs.append({
            "doc_num":   f"2026-DEMO-{i+1:04d}",
            "doc_type":  code,
            "cat":       cat,
            "cat_label": cat_label,
            "filed":     filed_dt.strftime("%Y-%m-%d"),
            "grantor":   grantor,
            "grantee":   grantee,
            "legal":     "DEMO RECORD",
            "amount":    float(amt) if amt else None,
            "clerk_url": BASE_URL,
            "_demo":     True,
        })
    return recs


# ── ENRICHMENT ────────────────────────────────────────────────────────────────

def enrich_with_parcel(records: list, lookup: dict) -> list:
    fuzzy_index = []
    seen = set()
    for variant, parcel in lookup.items():
        last, firsts = normalize_for_fuzzy(variant)
        key = (last, frozenset(firsts))
        if last and key not in seen:
            seen.add(key)
            fuzzy_index.append((last, firsts, parcel))

    matched = 0
    for rec in records:
        dtype  = rec.get("doc_type", "")
        owner  = (rec.get("grantee") if dtype in GRANTEE_IS_OWNER
                  else rec.get("grantor") or "").upper().strip()
        parcel = None

        if is_entity(owner):
            rec.setdefault("prop_address", "")
            rec.setdefault("prop_city",    "")
            rec.setdefault("prop_state",   "TX")
            rec.setdefault("prop_zip",     "")
            rec.setdefault("mail_address", "")
            rec.setdefault("mail_city",    "")
            rec.setdefault("mail_state",   "TX")
            rec.setdefault("mail_zip",     "")
            continue

        for variant in name_variants(owner):
            parcel = lookup.get(variant)
            if parcel:
                break

        if not parcel and owner:
            o_last, o_firsts = normalize_for_fuzzy(owner)
            if o_last and o_firsts:
                for c_last, c_firsts, candidate in fuzzy_index:
                    if c_last != o_last:
                        continue
                    if not c_firsts:
                        continue
                    if o_firsts & c_firsts:
                        parcel = candidate
                        break
                    o_str = " ".join(sorted(o_firsts))
                    c_str = " ".join(sorted(c_firsts))
                    if o_str and c_str and SequenceMatcher(
                            None, o_str, c_str).ratio() >= 0.85:
                        parcel = candidate
                        break

        if parcel:
            rec.update(parcel)
            matched += 1
        else:
            rec.setdefault("prop_address", "")
            rec.setdefault("prop_city",    "")
            rec.setdefault("prop_state",   "TX")
            rec.setdefault("prop_zip",     "")
            rec.setdefault("mail_address", "")
            rec.setdefault("mail_city",    "")
            rec.setdefault("mail_state",   "TX")
            rec.setdefault("mail_zip",     "")

    log.info(f"Parcel enrichment: {matched}/{len(records)} records matched")
    return records


# ── SCORING ───────────────────────────────────────────────────────────────────

def score_record(rec: dict) -> tuple:
    score = 30
    flags = []
    dtype  = rec.get("doc_type", "")
    amount = rec.get("amount") or 0

    if dtype == "Lis Pendens":            flags.append("Lis pendens")
    if dtype in ("Federal Tax",
                 "State Tax Lien"):       flags.append("Tax lien")
    if dtype in ("Judgment",
                 "Abstract of Judgment"): flags.append("Judgment lien")
    if dtype in ("Probate",
                 "Affidavit Heirs"):      flags.append("Probate / estate")
    if dtype == "Mechanic Lien":          flags.append("Mechanic lien")
    if dtype in ("Lien", "Hospital Lien"):flags.append("Lien")
    if dtype == "Divorce Decree":         flags.append("Divorce")

    try:
        filed = datetime.strptime(rec.get("filed", ""), "%Y-%m-%d")
        if (datetime.today() - filed).days <= 14:
            flags.append("New this week")
    except Exception:
        pass

    has_addr = bool(rec.get("prop_address") or rec.get("mail_address"))
    score += 10 * len(flags)
    if "Lis pendens" in flags:      score += 20
    if "Probate / estate" in flags: score += 10
    if "Tax lien" in flags:         score += 10
    if amount and amount > 100_000: score += 15
    elif amount and amount > 50_000: score += 10
    if "New this week" in flags:    score += 5
    if has_addr:                    score += 5
    return min(score, 100), flags


# ── OUTPUT ────────────────────────────────────────────────────────────────────

def build_output(raw_records: list, date_from: str, date_to: str) -> dict:
    seen_docs = set()
    out_records = []
    for raw in raw_records:
        try:
            doc_num = raw.get("doc_num", "")
            if doc_num and doc_num in seen_docs:
                continue
            if doc_num:
                seen_docs.add(doc_num)

            dtype = raw.get("doc_type", "")
            if dtype in GRANTEE_IS_OWNER:
                owner   = raw.get("grantee", "")
                grantee = raw.get("grantor", "")
            else:
                owner   = raw.get("grantor", "")
                grantee = raw.get("grantee", "")

            if not owner:
                continue

            score, flags = score_record({**raw, "owner": owner})

            out_records.append({
                "doc_num":      doc_num,
                "doc_type":     dtype,
                "filed":        raw.get("filed", ""),
                "cat":          raw.get("cat", "other"),
                "cat_label":    raw.get("cat_label", ""),
                "owner":        owner,
                "grantee":      grantee,
                "amount":       raw.get("amount"),
                "legal":        raw.get("legal", ""),
                "prop_address": raw.get("prop_address", ""),
                "prop_city":    raw.get("prop_city", ""),
                "prop_state":   raw.get("prop_state", "TX"),
                "prop_zip":     raw.get("prop_zip", ""),
                "mail_address": raw.get("mail_address", ""),
                "mail_city":    raw.get("mail_city", ""),
                "mail_state":   raw.get("mail_state", "TX"),
                "mail_zip":     raw.get("mail_zip", ""),
                "clerk_url":    raw.get("clerk_url", ""),
                "flags":        flags,
                "score":        score,
                "_demo":        raw.get("_demo", False),
            })
        except Exception:
            log.warning(f"Skipping: {traceback.format_exc()}")

    out_records = [r for r in out_records if not is_entity(r.get("owner", ""))]
    out_records = [r for r in out_records if not any(
        x in (r.get("owner", "")).upper() for x in ENTITY_FILTERS
    )]
    out_records.sort(key=lambda r: (-r["score"], r.get("filed", "") or ""))
    with_address = sum(1 for r in out_records if r["prop_address"] or r["mail_address"])

    return {
        "fetched_at":   datetime.utcnow().isoformat() + "Z",
        "source":       "Ellis County TX – LGS Online Solutions",
        "date_range":   {"from": date_from, "to": date_to},
        "total":        len(out_records),
        "with_address": with_address,
        "records":      out_records,
    }


def save_output(data: dict):
    for path in ["dashboard/records.json", "data/records.json"]:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2))
        log.info(f"Saved {data['total']} records → {path}")


def export_ghl_csv(data: dict):
    fieldnames = [
        "First Name", "Last Name", "Mailing Address", "Mailing City",
        "Mailing State", "Mailing Zip", "Property Address", "Property City",
        "Property State", "Property Zip", "Lead Type", "Document Type",
        "Date Filed", "Document Number", "Amount/Debt Owed", "Seller Score",
        "Motivated Seller Flags", "Source", "Public Records URL",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for r in data["records"]:
        parts = (r.get("owner", "")).split()
        writer.writerow({
            "First Name":             parts[0] if parts else "",
            "Last Name":              " ".join(parts[1:]) if len(parts) > 1 else "",
            "Mailing Address":        r.get("mail_address", ""),
            "Mailing City":           r.get("mail_city", ""),
            "Mailing State":          r.get("mail_state", "TX"),
            "Mailing Zip":            r.get("mail_zip", ""),
            "Property Address":       r.get("prop_address", ""),
            "Property City":          r.get("prop_city", ""),
            "Property State":         r.get("prop_state", "TX"),
            "Property Zip":           r.get("prop_zip", ""),
            "Lead Type":              r.get("cat_label", ""),
            "Document Type":          r.get("doc_type", ""),
            "Date Filed":             r.get("filed", ""),
            "Document Number":        r.get("doc_num", ""),
            "Amount/Debt Owed":       str(r.get("amount", "") or ""),
            "Seller Score":           str(r.get("score", "")),
            "Motivated Seller Flags": "|".join(r.get("flags", [])),
            "Source":                 "Ellis County TX",
            "Public Records URL":     r.get("clerk_url", ""),
        })
    Path("data/ghl_export.csv").write_text(buf.getvalue())
    log.info("GHL CSV saved")


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    today     = datetime.today()
    start     = today - timedelta(days=LOOKBACK_DAYS)
    date_from = start.strftime("%m/%d/%Y")
    date_to   = today.strftime("%m/%d/%Y")

    log.info("=== Ellis County TX Lead Scraper ===")
    log.info(f"Date range: {date_from} → {date_to}")

    if not LGS_USERNAME or not LGS_PASSWORD:
        log.error("LGS_USERNAME and LGS_PASSWORD env vars not set!")
        return

    log.info("Building parcel lookup ...")
    parcel_lookup = build_parcel_lookup()
    log.info(f"  {len(parcel_lookup):,} name variants indexed")

    log.info("Scraping clerk records ...")
    raw_records = await scrape_all(date_from, date_to)
    log.info(f"Total raw records: {len(raw_records)}")

    if not raw_records:
        log.warning("No live records – using demo data")
        raw_records = generate_demo_records(date_from, date_to)

    raw_records = enrich_with_parcel(raw_records, parcel_lookup)
    data = build_output(raw_records, date_from, date_to)
    save_output(data)
    export_ghl_csv(data)
    log.info(f"Done. {data['total']} leads | {data['with_address']} with address")


if __name__ == "__main__":
    asyncio.run(main())

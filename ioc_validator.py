#!/usr/bin/env python3
"""
IOC Validator — VirusTotal API v3
==================================
Validates IP addresses, domains, URLs, and file hashes (MD5/SHA1/SHA256)
against VirusTotal and writes results to an XLSX file.

Usage:
    python ioc_validator.py -i iocs.xlsx   -k <VT_API_KEY>
    python ioc_validator.py -i iocs.csv    -k <VT_API_KEY> --ioc-column indicator
    python ioc_validator.py -i iocs.xlsx   -k <VT_API_KEY> --delay 1 --resume

Rate limits (VirusTotal):
    Free  tier : 4 req/min  → use --delay 15  (default)
    Premium    : up to 1000 req/min → use --delay 0.1

90,000 IOCs @ free tier  ≈ 375 hours  (use --resume to run in sessions)
90,000 IOCs @ 500 req/min ≈  3 hours
"""

import re
import sys
import time
import json
import base64
import argparse
from pathlib import Path
from datetime import datetime

import requests
import pandas as pd
from tqdm import tqdm

# ---------------------------------------------------------------------------
# VirusTotal API
# ---------------------------------------------------------------------------
VT_BASE = "https://www.virustotal.com/api/v3"

# ---------------------------------------------------------------------------
# IOC type detection patterns
# ---------------------------------------------------------------------------
_MD5    = re.compile(r'^[a-fA-F0-9]{32}$')
_SHA1   = re.compile(r'^[a-fA-F0-9]{40}$')
_SHA256 = re.compile(r'^[a-fA-F0-9]{64}$')
_IPV4   = re.compile(
    r'^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}'
    r'(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$'
)
_IPV6   = re.compile(r'^[0-9a-fA-F:]{3,39}$')  # loose check, refined below
_URL    = re.compile(r'^https?://', re.IGNORECASE)
_DOMAIN = re.compile(
    r'^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$'
)


def detect_type(value: str) -> str:
    """Return one of: hash | ip | domain | url | unknown."""
    v = value.strip()
    if _SHA256.match(v):  return "hash"
    if _SHA1.match(v):    return "hash"
    if _MD5.match(v):     return "hash"
    if _IPV4.match(v):    return "ip"
    if _URL.match(v):     return "url"
    if _DOMAIN.match(v):  return "domain"
    # Fallback: bare IPv6 addresses (contains colons)
    if ":" in v and not "/" in v:  return "ip"
    return "unknown"


# ---------------------------------------------------------------------------
# VirusTotal HTTP helpers
# ---------------------------------------------------------------------------

def _vt_get(endpoint: str, api_key: str, retries: int = 5) -> dict | None:
    """
    GET /api/v3/<endpoint>.
    Returns parsed JSON on success, None on 404.
    Retries on 429 / transient errors with exponential back-off.
    Raises SystemExit on auth errors.
    """
    headers = {"x-apikey": api_key, "Accept": "application/json"}
    url     = f"{VT_BASE}/{endpoint}"

    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, timeout=30)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 404:
                return None  # unknown to VT

            if resp.status_code in (401, 403):
                sys.exit("[FATAL] VirusTotal rejected the API key — check it and retry.")

            if resp.status_code == 429:
                wait = 15 * (2 ** attempt)   # 15 s, 30 s, 60 s …
                tqdm.write(f"\n  [Rate-limited] Sleeping {wait}s (attempt {attempt+1}/{retries})")
                time.sleep(wait)
                continue

            # Any other error — short back-off then retry
            tqdm.write(f"  [HTTP {resp.status_code}] {endpoint!r} — retrying…")
            time.sleep(2 ** attempt)

        except requests.RequestException as exc:
            tqdm.write(f"  [Network error] {exc} — retrying…")
            time.sleep(2 ** attempt)

    return None  # exhausted retries


# ---------------------------------------------------------------------------
# Per-type result parsers
# ---------------------------------------------------------------------------

def _threats_from_results(analysis_results: dict) -> list[str]:
    """Collect distinct non-empty threat names from engine results."""
    seen: set[str] = set()
    out: list[str] = []
    for eng in analysis_results.values():
        if eng.get("category") in ("malicious", "suspicious"):
            name = (eng.get("result") or "").strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _parse_hash_result(data: dict) -> tuple[str, str]:
    attrs  = data.get("data", {}).get("attributes", {})
    stats  = attrs.get("last_analysis_stats", {})
    mal    = stats.get("malicious", 0)
    sus    = stats.get("suspicious", 0)
    total  = sum(stats.values()) or 1

    if mal + sus == 0:
        return "Clean", ""

    # Preferred: VT's own threat label
    label = (
        attrs.get("popular_threat_classification", {})
             .get("suggested_threat_label", "")
        or attrs.get("meaningful_name", "")
    )
    if not label:
        threats = _threats_from_results(attrs.get("last_analysis_results", {}))
        label = "; ".join(threats[:5])

    return f"Malicious ({mal}/{total} engines)", label


def _parse_network_result(data: dict) -> tuple[str, str]:
    attrs  = data.get("data", {}).get("attributes", {})
    stats  = attrs.get("last_analysis_stats", {})
    mal    = stats.get("malicious", 0)
    sus    = stats.get("suspicious", 0)
    total  = sum(stats.values()) or 1

    # Categories (e.g. "phishing", "malware distribution") are always useful
    categories = list(attrs.get("categories", {}).values())

    if mal + sus == 0:
        assoc = "; ".join(categories[:3]) if categories else ""
        return "Clean", assoc

    threats = _threats_from_results(attrs.get("last_analysis_results", {}))
    label   = "; ".join(threats[:5]) or "; ".join(categories[:3])
    return f"Malicious ({mal}/{total} engines)", label


# ---------------------------------------------------------------------------
# Main lookup
# ---------------------------------------------------------------------------

def lookup(ioc: str, ioc_type: str, api_key: str) -> tuple[str, str]:
    """
    Query VirusTotal for a single IOC.
    Returns (status_string, associated_threat_string).
    """
    if ioc_type == "hash":
        data = _vt_get(f"files/{ioc}", api_key)
        if data is None:
            return "Not Found in VT", ""
        return _parse_hash_result(data)

    if ioc_type == "ip":
        data = _vt_get(f"ip_addresses/{ioc}", api_key)
        if data is None:
            return "Not Found in VT", ""
        return _parse_network_result(data)

    if ioc_type == "domain":
        data = _vt_get(f"domains/{ioc}", api_key)
        if data is None:
            return "Not Found in VT", ""
        return _parse_network_result(data)

    if ioc_type == "url":
        # VT v3 URL lookups need the URL base64url-encoded (no padding)
        url_id = base64.urlsafe_b64encode(ioc.encode()).rstrip(b"=").decode()
        data   = _vt_get(f"urls/{url_id}", api_key)
        if data is None:
            return "Not Found in VT", ""
        return _parse_network_result(data)

    return "Unsupported IOC Type", ""


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------

def load_file(path: str) -> pd.DataFrame:
    suffix = Path(path).suffix.lower()
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(path, dtype=str)
    if suffix == ".csv":
        return pd.read_csv(path, dtype=str)
    raise ValueError(f"Unsupported file format '{suffix}'. Use .xlsx, .xls, or .csv.")


def find_ioc_column(df: pd.DataFrame) -> str:
    """Heuristic: find the column most likely to hold IOC values."""
    candidates = {
        "ioc", "indicator", "value", "observable", "hash",
        "ip", "domain", "url", "artifact", "indicator_value",
        "ioc_value", "observable_value", "threat_indicator",
    }
    for col in df.columns:
        if col.strip().lower() in candidates:
            return col
    return df.columns[0]   # fallback: first column


def load_checkpoint(path: str) -> dict:
    if Path(path).exists():
        with open(path) as f:
            return json.load(f)
    return {}


def save_checkpoint(path: str, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ioc_validator.py",
        description="Validate IOCs (IPs, domains, URLs, hashes) via VirusTotal API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic run with free-tier API key (15 s delay):
  python ioc_validator.py -i iocs.xlsx -k YOUR_API_KEY

  # Specify IOC column and custom output:
  python ioc_validator.py -i iocs.csv -k YOUR_API_KEY --ioc-column indicator -o results.xlsx

  # Premium key — reduce delay to 0.1 s:
  python ioc_validator.py -i iocs.xlsx -k YOUR_API_KEY --delay 0.1

  # Resume an interrupted run:
  python ioc_validator.py -i iocs.xlsx -k YOUR_API_KEY --resume
        """,
    )
    p.add_argument("-i", "--input",      required=True,
                   help="Input file (.xlsx, .xls, or .csv)")
    p.add_argument("-k", "--api-key",    required=True,
                   help="VirusTotal API key")
    p.add_argument("-o", "--output",
                   help="Output XLSX path (default: <input_stem>_validated_<date>.xlsx)")
    p.add_argument("--ioc-column",
                   help="Column containing IOC values (auto-detected if omitted)")
    p.add_argument("--delay",            type=float, default=15.0,
                   help="Seconds to wait between API calls (default: 15 — free tier). "
                        "Use 0.1 for premium accounts.")
    p.add_argument("--resume",           action="store_true",
                   help="Skip IOCs already in the checkpoint file and continue")
    p.add_argument("--checkpoint-every", type=int, default=50,
                   help="Save progress every N records (default: 50)")
    return p


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = build_parser().parse_args()

    # ── Output path ─────────────────────────────────────────────────────────
    stem = Path(args.input).stem
    today = datetime.now().strftime("%Y%m%d")
    output_path     = args.output or f"{stem}_validated_{today}.xlsx"
    checkpoint_path = f"{stem}_checkpoint.json"

    # ── Load data ────────────────────────────────────────────────────────────
    print(f"[*] Loading {args.input} …")
    try:
        df = load_file(args.input)
    except Exception as exc:
        sys.exit(f"[ERROR] Could not load input file: {exc}")

    total_rows = len(df)
    print(f"[*] {total_rows:,} rows loaded.")

    ioc_col = args.ioc_column or find_ioc_column(df)
    print(f"[*] IOC column : '{ioc_col}'")
    print(f"[*] Output     : {output_path}")

    # ── Checkpoint ───────────────────────────────────────────────────────────
    checkpoint: dict = {}
    if args.resume:
        checkpoint = load_checkpoint(checkpoint_path)
        print(f"[*] Checkpoint : {len(checkpoint):,} IOCs already processed — will skip them.")

    # ── Ensure result columns exist ──────────────────────────────────────────
    for col in ("ioc_type", "vt_status", "associated_threat"):
        if col not in df.columns:
            df[col] = ""

    # ── Count unique IOCs so we can give a realistic ETA ────────────────────
    unique_iocs  = df[ioc_col].dropna().unique()
    already_done = sum(1 for v in unique_iocs if str(v).strip() in checkpoint)
    to_query     = len(unique_iocs) - already_done
    eta_hours    = to_query * args.delay / 3600
    print(f"\n[*] Unique IOCs        : {len(unique_iocs):,}")
    print(f"[*] Already in cache   : {already_done:,}")
    print(f"[*] Need to query VT   : {to_query:,}")
    if to_query:
        print(f"[*] Estimated time     : {eta_hours:.1f} h  "
              f"(@ {args.delay}s/request — adjust with --delay)")
    print()

    # ── Main loop ────────────────────────────────────────────────────────────
    api_calls   = 0
    cache_hits  = 0

    with tqdm(total=total_rows, unit="ioc", dynamic_ncols=True) as bar:
        for idx, row in df.iterrows():
            raw = str(row[ioc_col]).strip()

            # Skip blank / NaN
            if not raw or raw.lower() in ("nan", "none", ""):
                df.at[idx, "ioc_type"]         = "empty"
                df.at[idx, "vt_status"]         = "Skipped (empty)"
                df.at[idx, "associated_threat"] = ""
                bar.update(1)
                continue

            ioc_type = detect_type(raw)

            # ── Cache hit ────────────────────────────────────────────────────
            if raw in checkpoint:
                cached = checkpoint[raw]
                df.at[idx, "ioc_type"]         = cached["ioc_type"]
                df.at[idx, "vt_status"]         = cached["vt_status"]
                df.at[idx, "associated_threat"] = cached["associated_threat"]
                cache_hits += 1
                bar.set_postfix(api=api_calls, cached=cache_hits, type=ioc_type)
                bar.update(1)
                continue

            # ── VirusTotal query ─────────────────────────────────────────────
            df.at[idx, "ioc_type"] = ioc_type

            if ioc_type == "unknown":
                status, threat = "Unsupported IOC Type", ""
            else:
                status, threat = lookup(raw, ioc_type, args.api_key)
                api_calls += 1
                time.sleep(args.delay)

            df.at[idx, "vt_status"]         = status
            df.at[idx, "associated_threat"] = threat

            # Cache result so duplicates later in the file are free
            checkpoint[raw] = {
                "ioc_type":         ioc_type,
                "vt_status":         status,
                "associated_threat": threat,
            }

            bar.set_postfix(api=api_calls, cached=cache_hits, type=ioc_type)
            bar.update(1)

            # Periodic save
            if api_calls % args.checkpoint_every == 0 and api_calls > 0:
                save_checkpoint(checkpoint_path, checkpoint)
                df.to_excel(output_path, index=False)
                tqdm.write(f"  [Checkpoint saved at {api_calls} API calls]")

    # ── Final save ───────────────────────────────────────────────────────────
    save_checkpoint(checkpoint_path, checkpoint)
    df.to_excel(output_path, index=False)

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'='*55}")
    print(f"  Output saved : {output_path}")
    print(f"  API calls    : {api_calls:,}  |  Cache hits : {cache_hits:,}")
    print(f"{'='*55}")
    print("\n  Status breakdown:")
    for status, count in df["vt_status"].value_counts().items():
        print(f"    {status:<45} {count:>7,}")
    print()

    if Path(checkpoint_path).exists():
        print(f"  Checkpoint   : {checkpoint_path}  (keep for --resume)")


if __name__ == "__main__":
    main()

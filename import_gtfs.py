#!/usr/bin/env python3
"""
Train Tracker Australia — GTFS Import Pipeline
Uses curl + unzip + Python sqlite3 (near C-speed).

Usage:
  python3 import_gtfs.py melbourne
  python3 import_gtfs.py sydney
  python3 import_gtfs.py all
"""

import csv
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# --- Config ---
DB_PATH = Path.home() / ".traintracker" / "gtfs.db"
DATA_DIR = Path.home() / ".traintracker" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# TfNSW API key from env
TFNSW_API_KEY = os.environ.get("TFNSW_API_KEY", "")

# --- Schema (matches Swift SQLiteStore) ---
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=-64000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS stops (
    stop_id TEXT NOT NULL,
    stop_name TEXT NOT NULL,
    stop_lat REAL NOT NULL,
    stop_lon REAL NOT NULL,
    location_type INTEGER DEFAULT 0,
    parent_station TEXT,
    platform_code TEXT,
    region TEXT NOT NULL DEFAULT 'sydney',
    PRIMARY KEY (stop_id, region)
);

CREATE INDEX IF NOT EXISTS idx_stops_name ON stops(stop_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_stops_parent ON stops(parent_station);
CREATE INDEX IF NOT EXISTS idx_stops_region ON stops(region);

CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT NOT NULL,
    agency_id TEXT NOT NULL,
    route_short_name TEXT NOT NULL,
    route_long_name TEXT NOT NULL,
    route_type INTEGER NOT NULL,
    route_color TEXT,
    route_text_color TEXT,
    region TEXT NOT NULL DEFAULT 'sydney',
    PRIMARY KEY (route_id, region)
);

CREATE INDEX IF NOT EXISTS idx_routes_region ON routes(region);
CREATE INDEX IF NOT EXISTS idx_routes_type ON routes(region, route_type);

CREATE TABLE IF NOT EXISTS trips (
    trip_id TEXT NOT NULL,
    route_id TEXT NOT NULL,
    service_id TEXT NOT NULL,
    direction_id INTEGER DEFAULT 0,
    trip_headsign TEXT,
    shape_id TEXT,
    region TEXT NOT NULL DEFAULT 'sydney',
    PRIMARY KEY (trip_id, region)
);

CREATE INDEX IF NOT EXISTS idx_trips_route ON trips(route_id, region);
CREATE INDEX IF NOT EXISTS idx_trips_service ON trips(service_id, region);

CREATE TABLE IF NOT EXISTS stop_times (
    trip_id TEXT NOT NULL,
    stop_id TEXT NOT NULL,
    arrival_seconds INTEGER NOT NULL,
    departure_seconds INTEGER NOT NULL,
    stop_sequence INTEGER NOT NULL,
    region TEXT NOT NULL DEFAULT 'sydney'
);

CREATE INDEX IF NOT EXISTS idx_stop_times_stop ON stop_times(stop_id, region, departure_seconds);
CREATE INDEX IF NOT EXISTS idx_stop_times_trip ON stop_times(trip_id, region);

CREATE TABLE IF NOT EXISTS calendar (
    service_id TEXT NOT NULL,
    monday INTEGER NOT NULL,
    tuesday INTEGER NOT NULL,
    wednesday INTEGER NOT NULL,
    thursday INTEGER NOT NULL,
    friday INTEGER NOT NULL,
    saturday INTEGER NOT NULL,
    sunday INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT 'sydney',
    PRIMARY KEY (service_id, region)
);

CREATE INDEX IF NOT EXISTS idx_calendar_date ON calendar(start_date, end_date, region);

CREATE TABLE IF NOT EXISTS calendar_dates (
    service_id TEXT NOT NULL,
    date TEXT NOT NULL,
    exception_type INTEGER NOT NULL,
    region TEXT NOT NULL DEFAULT 'sydney'
);

CREATE INDEX IF NOT EXISTS idx_calendar_dates_svc ON calendar_dates(service_id, region);
CREATE INDEX IF NOT EXISTS idx_calendar_dates_date ON calendar_dates(date, region);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# --- Helpers ---

def human_size(n):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def parse_time_to_seconds(t):
    """Parse HH:MM:SS or H:MM:SS to total seconds."""
    parts = t.strip().split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return 0


# --- Download ---

def download_sydney():
    """Download TfNSW GTFS via curl (reliable)."""
    dest = DATA_DIR / "sydney_gtfs.zip"
    if dest.exists() and dest.stat().st_size > 1024 * 1024:
        print(f"[TfNSW] ⏭️  Skipping download — {dest.name} already exists ({human_size(dest.stat().st_size)})")
        return dest

    if not TFNSW_API_KEY:
        print("❌ TFNSW_API_KEY not set. export TFNSW_API_KEY='apikey ...'")
        sys.exit(1)

    print("[TfNSW] ⬇️  Downloading static GTFS (~1.7GB)...")
    url = "https://api.transport.nsw.gov.au/v1/publictransport/timetables/complete/gtfs"
    st = time.time()
    subprocess.run([
        "curl", "-L", "-o", str(dest),
        "-H", f"Authorization: apikey {TFNSW_API_KEY}",
        "-f", "--progress-bar", url
    ], check=True)
    elapsed = time.time() - st
    size = dest.stat().st_size
    print(f"[TfNSW] ✅ Downloaded: {human_size(size)} in {elapsed:.1f}s")
    return dest


def download_melbourne():
    """Download PTV GTFS via curl."""
    dest = DATA_DIR / "melbourne_gtfs.zip"
    if dest.exists() and dest.stat().st_size > 1024 * 1024:
        print(f"[PTV] ⏭️  Skipping download — {dest.name} already exists ({human_size(dest.stat().st_size)})")
        return dest

    print("[PTV] ⬇️  Downloading static GTFS (~260MB)...")
    url = "https://opendata.transport.vic.gov.au/dataset/3f4e292e-7f8a-4ffe-831f-1953be0fe448/resource/fb152201-859f-4882-9206-b768060b50ad/download/gtfs.zip"
    st = time.time()
    subprocess.run([
        "curl", "-L", "-o", str(dest), "--progress-bar", url
    ], check=True)
    elapsed = time.time() - st
    size = dest.stat().st_size
    print(f"[PTV] ✅ Downloaded: {human_size(size)} in {elapsed:.1f}s")
    return dest


# --- Extraction ---

def extract_gtfs(zip_path, temp_dir):
    """Extract ZIP (and nested ZIPs) using system unzip."""
    print(f"[Extract] 📦 Unzipping {zip_path.name}...")
    subprocess.run(["unzip", "-o", "-q", str(zip_path), "-d", str(temp_dir)], check=True)

    # Handle PTV nested ZIPs: numbered folders with google_transit.zip inside
    count = 0
    for root, dirs, files in os.walk(temp_dir):
        if "google_transit.zip" in files:
            nested = Path(root) / "google_transit.zip"
            print(f"[Extract]   📦 Extracting nested: {Path(root).name}/google_transit.zip")
            subprocess.run(["unzip", "-o", "-q", str(nested), "-d", str(root)], check=True)
            nested.unlink()
            count += 1
    if count:
        print(f"[Extract]   ✅ Extracted {count} nested ZIP(s)")


def find_gtfs_txt_files(extract_dir):
    """Find directories containing GTFS .txt files, sorted by size."""
    required = {"trips.txt", "stop_times.txt", "stops.txt", "routes.txt"}
    groups = []

    for root, dirs, files in os.walk(extract_dir):
        txt_files = [f for f in files if f.endswith(".txt")]
        found = {f for f in txt_files}
        if found & required:
            name = Path(root).name if Path(root) != extract_dir else "root"
            groups.append({
                "name": name,
                "path": Path(root),
                "files": txt_files
            })

    return groups


# --- Import ---

def import_group(conn, group, region):
    """Import one GTFS file group into SQLite."""
    group_dir = group["path"]
    name = group["name"]
    print(f"\n[Import] 📋 Processing group: {name}")

    def load_csv(filename, fmt_func=None):
        fpath = group_dir / filename
        if not fpath.exists():
            return 0
        count = 0
        with open(fpath, "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if fmt_func:
                    fmt_func(row, region, conn)
                count += 1
        return count

    c = conn.cursor()

    # agency
    if (group_dir / "agency.txt").exists():
        n = load_csv("agency.txt")
        print(f"  agency: {n} records (skipped — not stored)")

    # stops
    if (group_dir / "stops.txt").exists():
        print(f"  stops: loading...", end="", flush=True)
        st = time.time()
        with open(group_dir / "stops.txt", "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            batch = []
            total = 0
            for row in reader:
                if not row.get("stop_id"):
                    continue
                batch.append((
                    row["stop_id"],
                    row.get("stop_name", ""),
                    float(row.get("stop_lat", 0) or 0),
                    float(row.get("stop_lon", 0) or 0),
                    int(row.get("location_type", 0) or 0),
                    row.get("parent_station") or None,
                    row.get("platform_code") or None,
                    region
                ))
                if len(batch) >= 50000:
                    c.executemany(
                        "INSERT OR REPLACE INTO stops VALUES (?,?,?,?,?,?,?,?)", batch
                    )
                    total += len(batch)
                    batch.clear()
            if batch:
                c.executemany(
                    "INSERT OR REPLACE INTO stops VALUES (?,?,?,?,?,?,?,?)", batch
                )
                total += len(batch)
        elapsed = time.time() - st
        print(f" {total} total ({elapsed:.1f}s)")

    # routes
    if (group_dir / "routes.txt").exists():
        print(f"  routes: loading...", end="", flush=True)
        st = time.time()
        with open(group_dir / "routes.txt", "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            batch = []
            total = 0
            for row in reader:
                if not row.get("route_id"):
                    continue
                batch.append((
                    row["route_id"],
                    row.get("agency_id", ""),
                    row.get("route_short_name", ""),
                    row.get("route_long_name", ""),
                    int(row.get("route_type", 3) or 3),
                    row.get("route_color") or None,
                    row.get("route_text_color") or None,
                    region
                ))
                if len(batch) >= 50000:
                    c.executemany(
                        "INSERT OR REPLACE INTO routes VALUES (?,?,?,?,?,?,?,?)", batch
                    )
                    total += len(batch)
                    batch.clear()
            if batch:
                c.executemany(
                    "INSERT OR REPLACE INTO routes VALUES (?,?,?,?,?,?,?,?)", batch
                )
                total += len(batch)
        elapsed = time.time() - st
        print(f" {total} total ({elapsed:.1f}s)")

    # calendar
    if (group_dir / "calendar.txt").exists():
        print(f"  calendar: loading...", end="", flush=True)
        st = time.time()
        with open(group_dir / "calendar.txt", "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            batch = []
            total = 0
            for row in reader:
                if not row.get("service_id"):
                    continue
                batch.append((
                    row["service_id"],
                    int(row.get("monday", 0) or 0),
                    int(row.get("tuesday", 0) or 0),
                    int(row.get("wednesday", 0) or 0),
                    int(row.get("thursday", 0) or 0),
                    int(row.get("friday", 0) or 0),
                    int(row.get("saturday", 0) or 0),
                    int(row.get("sunday", 0) or 0),
                    row.get("start_date", ""),
                    row.get("end_date", ""),
                    region
                ))
                if len(batch) >= 50000:
                    c.executemany(
                        "INSERT OR REPLACE INTO calendar VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch
                    )
                    total += len(batch)
                    batch.clear()
            if batch:
                c.executemany(
                    "INSERT OR REPLACE INTO calendar VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch
                )
                total += len(batch)
        elapsed = time.time() - st
        print(f" {total} total ({elapsed:.1f}s)")

    # calendar_dates
    if (group_dir / "calendar_dates.txt").exists():
        print(f"  calendar_dates: loading...", end="", flush=True)
        st = time.time()
        with open(group_dir / "calendar_dates.txt", "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            batch = []
            total = 0
            for row in reader:
                if not row.get("service_id"):
                    continue
                batch.append((
                    row["service_id"],
                    row.get("date", ""),
                    int(row.get("exception_type", 1) or 1),
                    region
                ))
                if len(batch) >= 100000:
                    c.executemany(
                        "INSERT INTO calendar_dates VALUES (?,?,?,?)", batch
                    )
                    total += len(batch)
                    batch.clear()
            if batch:
                c.executemany(
                    "INSERT INTO calendar_dates VALUES (?,?,?,?)", batch
                )
                total += len(batch)
        elapsed = time.time() - st
        print(f" {total} total ({elapsed:.1f}s)")

    # trips
    if (group_dir / "trips.txt").exists():
        print(f"  trips: loading...", end="", flush=True)
        st = time.time()
        with open(group_dir / "trips.txt", "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            batch = []
            total = 0
            for row in reader:
                if not row.get("trip_id"):
                    continue
                batch.append((
                    row["trip_id"],
                    row.get("route_id", ""),
                    row.get("service_id", ""),
                    int(row.get("direction_id", 0) or 0),
                    row.get("trip_headsign") or None,
                    row.get("shape_id") or None,
                    region
                ))
                if len(batch) >= 50000:
                    c.executemany(
                        "INSERT OR REPLACE INTO trips VALUES (?,?,?,?,?,?,?)", batch
                    )
                    total += len(batch)
                    batch.clear()
            if batch:
                c.executemany(
                    "INSERT OR REPLACE INTO trips VALUES (?,?,?,?,?,?,?)", batch
                )
                total += len(batch)
        elapsed = time.time() - st
        print(f" {total} total ({elapsed:.1f}s)")

    # stop_times (largest table — use memory-efficient streaming)
    if (group_dir / "stop_times.txt").exists():
        print(f"  stop_times: loading...", end="", flush=True)
        st = time.time()
        with open(group_dir / "stop_times.txt", "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            batch = []
            total = 0
            for row in reader:
                if not row.get("trip_id"):
                    continue
                batch.append((
                    row["trip_id"],
                    row.get("stop_id", ""),
                    parse_time_to_seconds(row.get("arrival_time", "00:00:00")),
                    parse_time_to_seconds(row.get("departure_time", "00:00:00")),
                    int(row.get("stop_sequence", 0) or 0),
                    region
                ))
                if len(batch) >= 100000:
                    c.executemany(
                        "INSERT INTO stop_times VALUES (?,?,?,?,?,?)", batch
                    )
                    total += len(batch)
                    batch.clear()
                    elapsed = time.time() - st
                    print(f"\r  stop_times: {total:,} rows ({elapsed:.1f}s)...", end="", flush=True)
            if batch:
                c.executemany(
                    "INSERT INTO stop_times VALUES (?,?,?,?,?,?)", batch
                )
                total += len(batch)
        elapsed = time.time() - st
        print(f"\r  stop_times: {total:,} total ({elapsed:.1f}s)")

    c.close()


# --- Main ---

def import_region(region):
    st_total = time.time()

    # Download
    if region == "sydney":
        zip_path = download_sydney()
    elif region == "melbourne":
        zip_path = download_melbourne()
    else:
        print(f"❌ Unknown region: {region}")
        return

    # Extract
    tmp = tempfile.mkdtemp(prefix=f"gtfs_{region}_")
    try:
        extract_gtfs(zip_path, tmp)
        groups = find_gtfs_txt_files(tmp)
        print(f"[Extract] 📂 Found {len(groups)} GTFS file group(s)")
        for g in groups:
            print(f"  📁 {g['name']}: {len(g['files'])} .txt files")

        # DB
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.executescript(SCHEMA)
        conn.commit()  # explicitly commit schema changes first

        # Clear region (outside of bulk import transaction)
        tables = ["stops", "routes", "trips", "stop_times", "calendar", "calendar_dates"]
        for tbl in tables:
            conn.execute(f"DELETE FROM {tbl} WHERE region = ?", (region,))
        conn.commit()

        # Import in one big transaction for speed
        conn.execute("BEGIN")
        for group in groups:
            import_group(conn, group, region)
        conn.execute("COMMIT")

        # Stats
        c = conn.cursor()
        print(f"\n{'='*50}")
        print(f"✅ {region.upper()} import complete!")
        total = 0
        for tbl in tables:
            c.execute(f"SELECT COUNT(*) FROM {tbl} WHERE region = ?", (region,))
            n = c.fetchone()[0]
            total += n
            print(f"  {tbl}: {n:,}")
        print(f"  Total: {total:,} records")
        total_elapsed = time.time() - st_total
        print(f"  Total time: {total_elapsed:.1f}s ({total_elapsed/60:.1f} min)")

        # Save meta
        from datetime import datetime
        conn.execute(
            "INSERT OR REPLACE INTO meta VALUES (?, ?)",
            (f"last_import_{region}", datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 import_gtfs.py <region>")
        print("  region: sydney | melbourne | all")
        sys.exit(1)

    region = sys.argv[1]
    if region == "all":
        for r in ["melbourne", "sydney"]:
            print(f"\n{'='*60}")
            print(f"🚂 Importing {r.upper()} GTFS Data")
            print(f"{'='*60}")
            import_region(r)
    else:
        print(f"\n🚂 Importing {region.upper()} GTFS Data")
        import_region(region)


if __name__ == "__main__":
    main()

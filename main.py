#!/usr/bin/env python3
"""
Converts Google Takeout CSV files to GPX/KML files for Organic Maps import.
Processes all CSV files in a directory, extracts coordinates using Playwright,
and creates individual GPX files plus one merged GPX with all entries.
"""
import time, argparse, logging, sys, re, os, csv, json
from pathlib import Path
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import xml.etree.ElementTree as ET
from xml.dom import minidom

CALIBRATION_TIMESTAMP = None
CALIBRATION_VALIDITY_SECONDS = 300  # 5 minutes

# Google Maps URL patterns
AT_RE = re.compile(r'/@(-?\d+\.\d+),(-?\d+\.\d+)')
BrokenURL = "https://www.google.com/maps/place/Kungstr%C3%A4dg%C3%A5rden+%2F+King"

# Keywords for consent buttons
REJECT_SUBSTRINGS = ['reject', 'rechazar', 'rechazar todo', 'reject all', 'rechazar todo', 'rechazar-todo', 'rifiuta', 'refuser', 'nie zgadzam']
ACCEPT_SUBSTRINGS = ['accept', 'accept all', 'aceptar', 'aceitar', 'accept all', 'aceptar todo', 'akzeptieren', 'tout accepter']

# Global variable to store broken link coordinates
BROKEN_LINK_COORDS = None

# Shared Playwright browser/context/page reused across the whole run.
# Goal: spawn a single chromium-headless-shell process per script execution,
# instead of one per URL (firewall-friendly, much faster).
_SHARED = {"browser": None, "context": None, "page": None}


def _get_shared_page(pw, headless):
    """Return a (page, context) reusing a single browser for the whole run."""
    if _SHARED["page"] is None:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(locale="en-US")
        page = context.new_page()
        _SHARED["browser"] = browser
        _SHARED["context"] = context
        _SHARED["page"] = page
    return _SHARED["page"], _SHARED["context"]


def _close_shared_browser():
    """Close the shared browser at the end of the run."""
    if _SHARED["browser"] is not None:
        try:
            _SHARED["browser"].close()
        except Exception:
            pass
        _SHARED["browser"] = None
        _SHARED["context"] = None
        _SHARED["page"] = None


def extract_coords(u):
    if not u: return None
    m = AT_RE.search(u)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None

def find_and_click_by_element(frame, logger):
    """
    Iterate actual button elements, check aria-label and inner text for
    keywords, click the best candidate. Prefer reject substrings.
    """
    try:
        buttons = frame.query_selector_all("button")
    except Exception as e:
        logger.debug("query_selector_all failed: %s", e)
        return False

    # prefer reject; first pass for reject, second pass for accept
    for pass_keywords in (REJECT_SUBSTRINGS, ACCEPT_SUBSTRINGS):
        for b in buttons:
            try:
                aria = (b.get_attribute("aria-label") or "").lower()
            except Exception:
                aria = ""
            try:
                txt = (b.inner_text() or "").lower()
            except Exception:
                txt = ""
            combined = aria + " " + txt
            for kw in pass_keywords:
                if kw in combined:
                    try:
                        logger.debug("Clicking element: aria=%r text=%r (kw=%r)", aria, txt, kw)
                        b.click(timeout=10000)
                        return True
                    except Exception as e:
                        logger.debug("Click failed for matching element: %s", e)
    return False

def get_broken_link_coords(pw, logger, headless):
    """
    Navigate to known broken link and extract its coordinates.
    Returns tuple of (lat, lon) or None if unable to extract.
    """
    global BROKEN_LINK_COORDS
    global CALIBRATION_TIMESTAMP
    CALIBRATION_TIMESTAMP = time.time()

    logger.info("=== Calibrating broken link detector ===")
    page, context = _get_shared_page(pw, headless)
    logger.info("Navigating to broken link: %s", BrokenURL)
    
    try:
        page.goto(BrokenURL, wait_until="networkidle", timeout=60000)
    except PlaywrightTimeout:
        logger.warning("Broken link navigation timed out; continuing")
    
    # Try to handle consent and get coordinates
    max_attempts = 10
    
    for attempt in range(1, max_attempts + 1):
        logger.debug("Broken link calibration attempt #%d", attempt)
        
        coords = extract_coords(page.url)
        if coords:
            logger.info("Broken link coordinates found: %s", coords)
            BROKEN_LINK_COORDS = coords
            return coords
        
        # Try clicking consent buttons
        clicked = False
        frames = [page] + page.frames
        for f in frames:
            try:
                if find_and_click_by_element(f, logger):
                    clicked = True
                    break
            except Exception as e:
                logger.debug("Error during calibration click: %s", e)
        
        if clicked:
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            coords = extract_coords(page.url)
            if coords:
                logger.info("Broken link coordinates found after click: %s", coords)
                BROKEN_LINK_COORDS = coords
                browser.close()
                return coords
        
        time.sleep(2)
    
    logger.warning("Could not extract broken link coordinates after %d attempts", max_attempts)
    return None

def get_coordinates_from_url(url, pw, logger, headless, timeout=60):
    """
    Process a single URL and return coordinates.
    Returns (0, 0) if coordinates match broken link pattern.
    Returns None if timeout or error.
    """
    page, context = _get_shared_page(pw, headless)
    
    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
    except PlaywrightTimeout:
        logger.debug("Initial navigation timed out; continuing")

    start_time = time.time()
    attempt = 0
    
    while True:
        attempt += 1
        elapsed = time.time() - start_time
        logger.debug("Attempt #%d (%.1fs)", attempt, elapsed)

        # Quick success check
        coords = extract_coords(page.url)
        if coords:
            # Check if coordinates match broken link
            if BROKEN_LINK_COORDS and coords == BROKEN_LINK_COORDS:
                logger.warning("BROKEN LINK DETECTED: Coordinates match known broken link pattern")
                return (0, 0)
            
            logger.debug("Valid coords found: %s", coords)
            return coords

        clicked = False
        frames = [page] + page.frames
        for f in frames:
            try:
                if find_and_click_by_element(f, logger):
                    clicked = True
                    break
            except Exception as e:
                logger.debug("Error scanning frame: %s", e)

        if clicked:
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            coords = extract_coords(page.url)
            if coords:
                if BROKEN_LINK_COORDS and coords == BROKEN_LINK_COORDS:
                    logger.warning("BROKEN LINK DETECTED: Coordinates match known broken link pattern")
                    browser.close()
                    return (0, 0)
                
                logger.debug("Valid coords found after click: %s", coords)
                browser.close()
                return coords

        # Try a reload after setting consent cookie
        if attempt % 6 == 0:
            try:
                context.add_cookies([{
                    "name": "CONSENT", "value": "YES+1",
                    "domain": ".google.com", "path": "/", "httpOnly": False, "secure": True
                }])
                page.reload(wait_until="networkidle", timeout=30000)
            except Exception as e:
                logger.debug("Cookie heuristic or reload failed: %s", e)

        if elapsed > timeout:
            logger.warning("Timeout exceeded (%.1fs). Giving up.", elapsed)
            return None

        time.sleep(2)

def create_gpx(entries, output_path):
    """
    Creates a GPX file from a list of entries.
    Each entry is a dict with: title, note, url, tags, comment, lat, lon
    """
    gpx = ET.Element('gpx', {
        'version': '1.1',
        'creator': 'Google Takeout to GPX Converter',
        'xmlns': 'http://www.topografix.com/GPX/1/1',
        'xmlns:xsi': 'http://www.w3.org/2001/XMLSchema-instance',
        'xsi:schemaLocation': 'http://www.topografix.com/GPX/1/1 http://www.topografix.com/GPX/1/1/gpx.xsd'
    })
    
    for entry in entries:
        wpt = ET.SubElement(gpx, 'wpt', {
            'lat': str(entry['lat']),
            'lon': str(entry['lon'])
        })
        
        name = ET.SubElement(wpt, 'name')
        name.text = entry['title']
        
        desc_parts = []
        if entry.get('note'):
            desc_parts.append(f"Note: {entry['note']}")
        if entry.get('tags'):
            desc_parts.append(f"Tags: {entry['tags']}")
        if entry.get('comment'):
            desc_parts.append(f"Comment: {entry['comment']}")
        if entry.get('url'):
            desc_parts.append(f"URL: {entry['url']}")
        
        if desc_parts:
            desc = ET.SubElement(wpt, 'desc')
            desc.text = '\n'.join(desc_parts)
    
    # Pretty print
    xml_str = minidom.parseString(ET.tostring(gpx)).toprettyxml(indent="  ")
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(xml_str)

# def create_kml(entries, output_path):
#     """
#     Creates a KML file from a list of entries.
#     Each entry is a dict with: title, note, url, tags, comment, lat, lon
#     """
#     kml = ET.Element('kml', {'xmlns': 'http://www.opengis.net/kml/2.2'})
#     document = ET.SubElement(kml, 'Document')
#     
#     for entry in entries:
#         placemark = ET.SubElement(document, 'Placemark')
#         
#         name = ET.SubElement(placemark, 'name')
#         name.text = entry['title']
#         
#         desc_parts = []
#         if entry.get('note'):
#             desc_parts.append(f"<b>Note:</b> {entry['note']}")
#         if entry.get('tags'):
#             desc_parts.append(f"<b>Tags:</b> {entry['tags']}")
#         if entry.get('comment'):
#             desc_parts.append(f"<b>Comment:</b> {entry['comment']}")
#         if entry.get('url'):
#             desc_parts.append(f'<a href="{entry["url"]}">Link</a>')
#         
#         if desc_parts:
#             desc = ET.SubElement(placemark, 'description')
#             desc.text = '<br/>'.join(desc_parts)
#         
#         point = ET.SubElement(placemark, 'Point')
#         coords = ET.SubElement(point, 'coordinates')
#         coords.text = f"{entry['lon']},{entry['lat']},0"
#     
#     xml_str = minidom.parseString(ET.tostring(kml)).toprettyxml(indent="  ")
#     with open(output_path, 'w', encoding='utf-8') as f:
#         f.write(xml_str)

def parse_existing_gpx_outputs(output_dir):
    """
    Parse any existing .gpx files in output_dir and return a mapping:
      { basename_without_ext: { title -> (lat, lon) } }

    This lets the script know which titles are already present and with which coords.
    """
    existing = {}
    try:
        for gpx_file in Path(output_dir).glob("*.gpx"):
            basename = gpx_file.stem
            existing.setdefault(basename, {})
            try:
                tree = ET.parse(gpx_file)
                root = tree.getroot()
                # GPX namespace handling: support no-namespace and default GPX namespace
                ns = {'default': root.tag.split('}')[0].strip('{')} if '}' in root.tag else {}
                # find all wpt elements
                for wpt in root.findall('.//{http://www.topografix.com/GPX/1/1}wpt') if ns else root.findall('.//wpt'):
                    name_elem = wpt.find('{http://www.topografix.com/GPX/1/1}name') if ns else wpt.find('name')
                    lat = wpt.get('lat')
                    lon = wpt.get('lon')
                    if name_elem is None:
                        continue
                    title = (name_elem.text or "").strip()
                    try:
                        latf = float(lat) if lat is not None else None
                        lonf = float(lon) if lon is not None else None
                    except Exception:
                        latf = None
                        lonf = None
                    existing[basename][title] = (latf, lonf)
            except Exception:
                # if parsing fails, ignore this file but continue
                continue
    except Exception:
        return {}
    return existing


def load_url_cache(cache_file, logger):
    """
    Load URL cache from JSON file.
    Returns: dict {url: {"lat": float, "lon": float, "title": str, "note": str, "tags": str, "comment": str, "updated_at": str}}
    Backward compatible with old format (no title/note/tags/comment).
    """
    if not cache_file:
        return {}

    cache_path = Path(cache_file)
    if not cache_path.exists():
        return {}

    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except Exception as e:
        logger.warning("Could not read cache file %s: %s", cache_path, e)
        return {}

    if not isinstance(raw, dict):
        logger.warning("Cache file %s has invalid format (expected object)", cache_path)
        return {}

    cache = {}
    for url, val in raw.items():
        if not isinstance(url, str) or not isinstance(val, dict):
            continue
        try:
            lat = float(val.get("lat"))
            lon = float(val.get("lon"))
        except Exception:
            continue
        cache[url] = {
            "lat": lat,
            "lon": lon,
            "title": val.get("title", ""),
            "note": val.get("note", ""),
            "tags": val.get("tags", ""),
            "comment": val.get("comment", ""),
            "updated_at": val.get("updated_at", "")
        }

    logger.info("Loaded %d cached URL resolution(s) from %s", len(cache), cache_path)
    return cache


def persist_url_cache(cache_file, cache, logger):
    """Persist URL cache atomically to JSON file. Includes all fields for GPX recovery."""
    if not cache_file:
        return

    cache_path = Path(cache_file)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")

    payload = {
        url: {
            "lat": val["lat"],
            "lon": val["lon"],
            "title": val.get("title", ""),
            "note": val.get("note", ""),
            "tags": val.get("tags", ""),
            "comment": val.get("comment", ""),
            "updated_at": val.get("updated_at", "")
        }
        for url, val in cache.items()
    }

    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_path, cache_path)


def rebuild_gpx_from_cache(cache_file, output_dir, logger):
    """
    Reconstruct GPX files from cache JSON in case of crash.
    Builds one GPX per unique title's first URL occurrence.
    Returns: number of GPX files created.
    """
    if not cache_file:
        return 0

    cache_path = Path(cache_file)
    if not cache_path.exists():
        logger.warning("Cache file not found: %s", cache_path)
        return 0

    cache = load_url_cache(cache_file, logger)
    if not cache:
        logger.warning("Cache is empty")
        return 0

    logger.info("Rebuilding GPX from cache (%d cached entries)...", len(cache))

    # Group by title to build entries
    entries = []
    seen_titles = set()

    for url, val in sorted(cache.items()):
        title = val.get("title", "").strip()
        if not title or title in seen_titles:
            continue

        seen_titles.add(title)
        lat = val.get("lat")
        lon = val.get("lon")

        if lat is None or lon is None:
            continue

        entries.append({
            "title": title,
            "note": val.get("note", ""),
            "url": url,
            "tags": val.get("tags", ""),
            "comment": val.get("comment", ""),
            "lat": lat,
            "lon": lon
        })

    if not entries:
        logger.warning("No valid entries found in cache")
        return 0

    # Create a single merged GPX from recovered entries
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    recovery_gpx = output_dir / "recovered_from_cache.gpx"

    create_gpx(entries, recovery_gpx)
    logger.info("Created recovery GPX: %s (%d waypoints)", recovery_gpx, len(entries))

    return 1

def process_csv_file(csv_path, pw, logger, headless, existing_titles_coords=None, url_cache=None, cache_file=None):
    """
    Process a single CSV file and return list of entries with coordinates.
    existing_titles_coords: dict mapping title -> (lat, lon) read from an existing GPX for this csv (may be None).
    Behavior:
      - If title exists in existing_titles_coords and has valid coords (not None and not (0,0)),
        use those coords and do NOT fetch.
      - Otherwise attempt to fetch coords via get_coordinates_from_url.
    Returns: (entries_list, failed_titles_list)
    """
    entries = []
    failed = []

    logger.info(f"Processing CSV: {csv_path}")

    # normalize existing map for quick lookup
    existing_map = existing_titles_coords or {}

    with _open_csv_at_header(csv_path) as f:
        reader = csv.DictReader(f)

        for row in reader:
            title = row.get('Title', '').strip()
            note = row.get('Note', '').strip()
            url = row.get('URL', '').strip()
            tags = row.get('Tags', '').strip()
            comment = row.get('Comment', '').strip()

            if not title:
                logger.warning(f"Skipping row without title in {csv_path}")
                continue

            # If the title already exists in an existing GPX and has valid coordinates, reuse them
            if title in existing_map:
                latlon = existing_map[title]
                if latlon and latlon[0] is not None and latlon[1] is not None and (latlon != (0, 0)):
                    logger.info(f"Using existing coords for '{title}' from GPX: ({latlon[0]}, {latlon[1]})")
                    entries.append({
                        'title': title,
                        'note': note,
                        'url': url,
                        'tags': tags,
                        'comment': comment,
                        'lat': latlon[0],
                        'lon': latlon[1]
                    })
                    continue
                else:
                    logger.info(f"Title '{title}' present in GPX but coords are missing/invalid -> will attempt fetch")

            # If no URL, cannot fetch
            if not url:
                logger.warning(f"Skipping '{title}': no URL provided")
                failed.append(title)
                continue

            # URL cache lookup first
            cached = (url_cache or {}).get(url)
            if cached:
                cached_lat = cached.get('lat')
                cached_lon = cached.get('lon')
                if cached_lat is not None and cached_lon is not None:
                    logger.info(f"Using cached coords for '{title}': ({cached_lat}, {cached_lon})")
                    entries.append({
                        'title': title,
                        'note': note,
                        'url': url,
                        'tags': tags,
                        'comment': comment,
                        'lat': cached_lat,
                        'lon': cached_lon
                    })
                    continue

            logger.info(f"Fetching coords for: {title}")
            coords = get_coordinates_from_url(url, pw, logger, headless)

            if coords is None:
                logger.warning(f"Failed to get coordinates for '{title}'")
                failed.append(title)
            elif coords == (0, 0):
                logger.warning(f"Broken link detected for '{title}'")
                failed.append(title)
            else:
                entries.append({
                    'title': title,
                    'note': note,
                    'url': url,
                    'tags': tags,
                    'comment': comment,
                    'lat': coords[0],
                    'lon': coords[1]
                })
                logger.info(f"✓ '{title}' -> ({coords[0]}, {coords[1]})")

                # Persist each successful URL resolution immediately (with full entry data for recovery)
                if url_cache is not None:
                    url_cache[url] = {
                        "lat": coords[0],
                        "lon": coords[1],
                        "title": title,
                        "note": note,
                        "tags": tags,
                        "comment": comment,
                        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                    }
                    try:
                        persist_url_cache(cache_file, url_cache, logger)
                    except Exception as e:
                        logger.warning("Could not persist cache file %s: %s", cache_file, e)

            time.sleep(1)  # Be nice to Google

    return entries, failed



REQUIRED_HEADER = ["Title", "Note", "URL", "Tags", "Comment"]


def _open_csv_at_header(csv_path, max_skip=10):
    """
    Open a CSV and return a file object positioned at the header row.
    Tolerates parasitic lines before the header (free-form title, blank lines)
    that Google Takeout sometimes inserts in named-list exports.
    Raises ValueError if the header is not found within `max_skip` lines.
    """
    f = open(csv_path, 'r', encoding='utf-8', newline='')
    for _ in range(max_skip):
        pos = f.tell()
        line = f.readline()
        if not line:
            break
        try:
            row = next(csv.reader([line]))
        except StopIteration:
            continue
        if [h.strip() for h in row] == REQUIRED_HEADER:
            f.seek(pos)
            return f
    f.close()
    raise ValueError(
        f"Header {REQUIRED_HEADER} not found in first {max_skip} lines of {csv_path}"
    )

def check_structure(directory):
    """
    Verify every CSV file in the directory contains the required header row
    (Title,Note,URL,Tags,Comment) within its first lines.
    """
    offenders = []

    for entry in os.listdir(directory):
        if entry.lower().endswith('.csv'):
            csv_path = os.path.join(directory, entry)
            try:
                f = _open_csv_at_header(csv_path)
                f.close()
            except (ValueError, StopIteration):
                offenders.append(entry)

    if offenders:
        print("Error: all CSV files must contain the header row:", ", ".join(REQUIRED_HEADER), file=sys.stderr)
        print("Non-compliant files:", ", ".join(offenders), file=sys.stderr)
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(
        description='Convert Google Takeout CSV files to GPX format for Organic Maps'
    )
    parser.add_argument('directory', nargs='?', default='.', 
                        help='Directory containing CSV files (default: current directory)')
    parser.add_argument('--headless', type=int, default=1, choices=[0,1], 
                        help='0=headful browser, 1=headless (default: 1)')
    parser.add_argument('--debug', action='store_true', 
                        help='Enable debug logging')
    parser.add_argument('--log-dir', default=None,
                        help='Directory to write timestamped log file (optional)')
    parser.add_argument('--cache-file', default=None,
                        help='Path to JSON cache file for URL->coordinates (optional)')
    parser.add_argument('--recover-from-cache', action='store_true',
                        help='Recover GPX from cache file only (ignores CSV files)')
    args = parser.parse_args()
    
    # Setup logging
    logger = logging.getLogger("csv2gpx")
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if args.debug else logging.INFO)

    if args.log_dir:
        log_path = Path(args.log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fh = logging.FileHandler(log_path / f"conversion_{ts}.log", encoding="utf-8")
        fh.setFormatter(formatter)
        fh.setLevel(logging.DEBUG if args.debug else logging.INFO)
        logger.addHandler(fh)
        logger.info(f"Logging to file: {log_path / f'conversion_{ts}.log'}")

    # Validate directory (needed before resolving default cache path)
    input_dir = Path(args.directory)
    if not input_dir.is_dir():
        logger.error(f"Error: '{input_dir}' is not a directory")
        return 1

    # Cache file path: explicit arg wins, otherwise default to <input_dir>/out/url_coords_cache.json
    cache_file_path = args.cache_file
    if not cache_file_path:
        default_out = input_dir / "out"
        default_out.mkdir(parents=True, exist_ok=True)
        cache_file_path = str(default_out / "url_coords_cache.json")
        logger.info(f"No --cache-file given, defaulting to: {cache_file_path}")
    else:
        logger.info(f"Using URL cache file: {cache_file_path}")

    url_cache = load_url_cache(cache_file_path, logger)

    # Recovery mode: rebuild from cache only
    if args.recover_from_cache:
        logger.info("=== RECOVERY MODE: Rebuilding GPX from cache ===")
        output_dir = input_dir / "out"
        output_dir.mkdir(parents=True, exist_ok=True)
        count = rebuild_gpx_from_cache(cache_file_path, output_dir, logger)
        logger.info(f"Recovery complete: {count} GPX file(s) created")
        return 0

    check_structure(input_dir)

    # Create output directory
    output_dir = input_dir / "out"
    output_dir.mkdir(exist_ok=True)
    logger.info(f"Output directory: {output_dir}")
    
    # Find all CSV files
    csv_files = list(input_dir.glob("*.csv"))
    if not csv_files:
        logger.error(f"No CSV files found in {input_dir}")
        return 1
    
    logger.info(f"Found {len(csv_files)} CSV file(s)")
    
    # Check for existing output files
    start_index = 0
    resume_titles = set()
    resume_map = {}
    
    if output_dir.exists():
        existing_files = list(output_dir.glob("*.gpx"))
        if existing_files:
            print(f"\nFound {len(existing_files)} existing GPX file(s) in {output_dir}")
            response = input("Override existing files (o) or continue where left off (c)? [o/c]: ").strip().lower()
            
            if response == 'c':
                # Build per-file map of existing titles -> coords
                existing_map = parse_existing_gpx_outputs(output_dir)
                if existing_map:
                    # resume_map structure: { csv_basename: { title: (lat, lon), ... }, ... }
                    resume_map = existing_map
                    total_entries = sum(len(v) for v in existing_map.values())
                    logger.info(f"Resuming using existing outputs; found {total_entries} entries across {len(existing_map)} GPX file(s)")
                else:
                    logger.info("No existing GPX contents detected; starting from scratch")

            else:
                logger.info("Will override existing files")

    with sync_playwright() as pw:
        # Calibrate broken link detector
        headless_bool = bool(args.headless)
        broken_coords = get_broken_link_coords(pw, logger, headless_bool)
        if not broken_coords:
            logger.warning("Could not calibrate broken link detector")
        else:
            logger.info(f"=== Broken link detector calibrated: {broken_coords} ===\n")
        
        all_entries = []
        all_failed = []
        run_start = time.time()
        total_places = sum(
            max(0, sum(1 for _ in _open_csv_at_header(p)) - 1)
            for p in csv_files
        )
        places_done = 0
        logger.info(f"Total lieux à traiter (estimation) : {total_places}")

        # Process each CSV file
        for idx, csv_path in enumerate(csv_files):
            if idx < start_index:
                logger.info(f"Skipping already completed file: {csv_path.name}")
                continue
            
            # Check if recalibration is needed
            if CALIBRATION_TIMESTAMP and (time.time() - CALIBRATION_TIMESTAMP) > CALIBRATION_VALIDITY_SECONDS:
                logger.info("Calibration expired (>5 min), recalibrating...")
                broken_coords = get_broken_link_coords(pw, logger, headless_bool)
                if broken_coords:
                    logger.info(f"Recalibrated: {broken_coords}\n")
            
            # Use skip_titles only for the resume file
            # Always process each CSV file, but provide any existing titles/coords for that file
            skip_for_this_file = resume_map.get(csv_path.stem, {}) if resume_map else {}

            # Fast resume path:
            # If every titled row in CSV already has valid coords in existing GPX,
            # skip reprocessing and avoid rewriting the per-file GPX.
            if skip_for_this_file:
                csv_rows = []
                with _open_csv_at_header(csv_path) as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        title = row.get('Title', '').strip()
                        if not title:
                            continue
                        csv_rows.append({
                            'title': title,
                            'note': row.get('Note', '').strip(),
                            'url': row.get('URL', '').strip(),
                            'tags': row.get('Tags', '').strip(),
                            'comment': row.get('Comment', '').strip(),
                        })

                can_skip_file = len(csv_rows) > 0 and all(
                    r['title'] in skip_for_this_file
                    and skip_for_this_file[r['title']]
                    and skip_for_this_file[r['title']][0] is not None
                    and skip_for_this_file[r['title']][1] is not None
                    and skip_for_this_file[r['title']] != (0, 0)
                    for r in csv_rows
                )

                if can_skip_file:
                    resumed_entries = []
                    for r in csv_rows:
                        lat, lon = skip_for_this_file[r['title']]
                        resumed_entries.append({
                            'title': r['title'],
                            'note': r['note'],
                            'url': r['url'],
                            'tags': r['tags'],
                            'comment': r['comment'],
                            'lat': lat,
                            'lon': lon,
                        })

                    all_entries.extend(resumed_entries)
                    places_done += len(csv_rows)
                    logger.info(
                        f"Resume skip: {csv_path.name} already complete in existing GPX "
                        f"({len(csv_rows)} waypoints) — no rewrite"
                    )
                    logger.info("")
                    continue

            entries, failed = process_csv_file(
                csv_path,
                pw,
                logger,
                headless_bool,
                existing_titles_coords=skip_for_this_file,
                url_cache=url_cache,
                cache_file=cache_file_path,
            )

            places_done += len(entries) + len(failed)
            if places_done > 0 and run_start:
                elapsed = time.time() - run_start
                rate = elapsed / places_done
                remaining = max(0, total_places - places_done)
                eta_s = int(rate * remaining)
                eta_h, eta_m = divmod(eta_s // 60, 60)
                logger.info(
                    f"Progression : {places_done}/{total_places} lieux "
                    f"— ETA : {eta_h}h{eta_m:02d}m"
                )

            if entries:
                # Create individual GPX file
                basename = csv_path.stem
                gpx_path = output_dir / f"{basename}.gpx"
                create_gpx(entries, gpx_path)
                logger.info(f"✓ Created: {gpx_path} ({len(entries)} waypoints)")
                
                # # Uncomment to also create KML files:
                # kml_path = output_dir / f"{basename}.kml"
                # create_kml(entries, kml_path)
                # logger.info(f"✓ Created: {kml_path}")
                
                all_entries.extend(entries)
            
            if failed:
                logger.warning(f"Failed entries from {csv_path.name}: {', '.join(failed)}")
                all_failed.extend(failed)
            
            logger.info("")  # Blank line between files
        
        # Create merged GPX with all entries
        if all_entries:
            merged_path = output_dir / "merged_all.gpx"
            create_gpx(all_entries, merged_path)
            logger.info(f"\n✓✓✓ Created merged file: {merged_path} ({len(all_entries)} total waypoints)")
        
        # Summary
        logger.info(f"\n{'='*60}")
        logger.info(f"SUMMARY:")
        logger.info(f"  Total waypoints: {len(all_entries)}")
        logger.info(f"  Failed entries: {len(all_failed)}")
        if all_failed:
            logger.info(f"  Failed titles: {', '.join(all_failed)}")
        logger.info(f"{'='*60}")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
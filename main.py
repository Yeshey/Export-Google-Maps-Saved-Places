#!/usr/bin/env python3
"""
Converts Google Takeout CSV files to GPX/KML files for Organic Maps import.
Processes all CSV files in a directory, extracts coordinates using Playwright,
and creates individual GPX files plus one merged GPX with all entries.
"""
import time, argparse, logging, sys, re, os, csv
from pathlib import Path
from datetime import datetime
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
    browser = pw.chromium.launch(headless=headless)
    context = browser.new_context(locale="en-US")
    page = context.new_page()
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
            browser.close()
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
    browser.close()
    return None

def get_coordinates_from_url(url, pw, logger, headless, timeout=60):
    """
    Process a single URL and return coordinates.
    Returns (0, 0) if coordinates match broken link pattern.
    Returns None if timeout or error.
    """
    browser = pw.chromium.launch(headless=headless)
    context = browser.new_context(locale="en-US")
    page = context.new_page()
    
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
                browser.close()
                return (0, 0)
            
            logger.debug("Valid coords found: %s", coords)
            browser.close()
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
            browser.close()
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

def parse_gpx_file(gpx_path):
    """
    Parse a single .gpx file and return a list of entries.
    Each entry is a dict with: title, note, url, tags, comment, lat, lon
    Missing/optional fields are set to empty strings; coords default to None.
    """
    entries = []
    try:
        tree = ET.parse(gpx_path)
        root = tree.getroot()
        # GPX namespace handling: support no-namespace and default GPX namespace
        has_ns = '}' in root.tag
        wpt_xpath = './/{http://www.topografix.com/GPX/1/1}wpt' if has_ns else './/wpt'
        name_tag = '{http://www.topografix.com/GPX/1/1}name' if has_ns else 'name'
        desc_tag = '{http://www.topografix.com/GPX/1/1}desc' if has_ns else 'desc'

        for wpt in root.findall(wpt_xpath):
            name_elem = wpt.find(name_tag)
            if name_elem is None:
                continue
            title = (name_elem.text or "").strip()
            if not title:
                continue
            try:
                lat = float(wpt.get('lat'))
                lon = float(wpt.get('lon'))
            except Exception:
                lat = None
                lon = None

            note = url = tags = comment = ""
            desc_elem = wpt.find(desc_tag)
            if desc_elem is not None and desc_elem.text:
                for line in desc_elem.text.splitlines():
                    if line.startswith("Note: "):
                        note = line[len("Note: "):]
                    elif line.startswith("Tags: "):
                        tags = line[len("Tags: "):]
                    elif line.startswith("Comment: "):
                        comment = line[len("Comment: "):]
                    elif line.startswith("URL: "):
                        url = line[len("URL: "):]

            entries.append({
                'title': title,
                'note': note,
                'url': url,
                'tags': tags,
                'comment': comment,
                'lat': lat,
                'lon': lon
            })
    except Exception:
        return []
    return entries



def process_csv_file(csv_path, pw, logger, headless, output_path=None, resume=False):
    """
    Process a single CSV file and return list of entries with coordinates.

    If output_path is provided, the GPX file is updated incrementally after each
    successful coordinate lookup. If resume is True, any waypoints already present
    in output_path are loaded first and skipped during CSV processing.

    Returns: (entries_list, failed_titles_list)
    """
    entries = []
    failed = []

    logger.info(f"Processing CSV: {csv_path}")

    if output_path:
        if resume and output_path.exists():
            entries = parse_gpx_file(output_path)
            logger.info(f"Resuming from {output_path}: loaded {len(entries)} existing waypoints")
        elif output_path.exists():
            # Overwrite mode: remove stale output so the new file is built from scratch
            try:
                output_path.unlink()
                logger.info(f"Removed stale output: {output_path}")
            except Exception as e:
                logger.warning(f"Could not remove {output_path}: {e}")

    # Track titles already present in the output file to avoid re-fetching
    processed_titles = {e['title'] for e in entries}

    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        if reader.fieldnames:
            reader.fieldnames = [h.strip() for h in reader.fieldnames]

        for row in reader:
            title = row.get('Title', '').strip()
            note = row.get('Note', '').strip()
            url = row.get('URL', '').strip()
            tags = row.get('Tags', '').strip()
            comment = row.get('Comment', '').strip()

            if not title:
                if not (note or url or tags or comment):
                    logger.debug(f"Skipping blank row in {csv_path}")
                else:
                    logger.warning(f"Skipping row without title in {csv_path}")
                continue

            # If the title is already present in the output GPX, skip it
            if title in processed_titles:
                logger.info(f"Skipping '{title}': already present in {output_path}")
                continue

            # If no URL, cannot fetch
            if not url:
                logger.warning(f"Skipping '{title}': no URL provided")
                failed.append(title)
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
                entry = {
                    'title': title,
                    'note': note,
                    'url': url,
                    'tags': tags,
                    'comment': comment,
                    'lat': coords[0],
                    'lon': coords[1]
                }
                entries.append(entry)
                processed_titles.add(title)
                logger.info(f"✓ '{title}' -> ({coords[0]}, {coords[1]})")

                # Persist progress immediately
                if output_path:
                    create_gpx(entries, output_path)
                    logger.info(f"  Saved progress to {output_path} ({len(entries)} waypoints)")

            time.sleep(1)  # Be nice to Google

    return entries, failed


def check_structure(directory):
    """
    Verify every CSV file in the directory has the exact header:
    Title,Note,URL,Tags,Comment
    """
    required = ["Title", "Note", "URL", "Tags", "Comment"]
    offenders = []

    for entry in os.listdir(directory):
        if entry.lower().endswith('.csv'):
            csv_path = os.path.join(directory, entry)
            with open(csv_path, newline='', encoding='utf-8') as f:
                try:
                    header = next(csv.reader(f))
                except StopIteration:
                    offenders.append(entry)
                    continue
                if [h.strip() for h in header] != required:
                    offenders.append(entry)

    if offenders:
        print("Error: all CSV files must have the header row:", ", ".join(required), file=sys.stderr)
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
    args = parser.parse_args()
    
    # Setup logging
    logger = logging.getLogger("csv2gpx")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if args.debug else logging.INFO)
    
    # Validate directory
    input_dir = Path(args.directory)
    if not input_dir.is_dir():
        logger.error(f"Error: '{input_dir}' is not a directory")
        return 1

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
    
    # Check for existing output files and decide whether to resume
    resume = False
    if output_dir.exists():
        existing_files = list(output_dir.glob("*.gpx"))
        if existing_files:
            print(f"\nFound {len(existing_files)} existing GPX file(s) in {output_dir}")
            response = input("Override existing files (o) or continue where left off (c)? [o/c]: ").strip().lower()
            if response == 'c':
                resume = True
                total_entries = sum(len(parse_gpx_file(f)) for f in existing_files)
                logger.info(f"Resuming using existing outputs; found {total_entries} entries across {len(existing_files)} GPX file(s)")
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

        # Process each CSV file
        for idx, csv_path in enumerate(csv_files):
            # Check if recalibration is needed
            if CALIBRATION_TIMESTAMP and (time.time() - CALIBRATION_TIMESTAMP) > CALIBRATION_VALIDITY_SECONDS:
                logger.info("Calibration expired (>5 min), recalibrating...")
                broken_coords = get_broken_link_coords(pw, logger, headless_bool)
                if broken_coords:
                    logger.info(f"Recalibrated: {broken_coords}\n")

            basename = csv_path.stem
            gpx_path = output_dir / f"{basename}.gpx"
            entries, failed = process_csv_file(csv_path, pw, logger, headless_bool, output_path=gpx_path, resume=resume)

            if entries:
                logger.info(f"✓ Finalized: {gpx_path} ({len(entries)} waypoints)")

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
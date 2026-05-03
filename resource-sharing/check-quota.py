#!/usr/bin/env python3
"""
Check Claude quota usage and store in database.
Admin-only tool - requires tmux orchestration of Claude CLI.
"""

import subprocess
import time
import re
import sqlite3
from datetime import datetime
from pathlib import Path

# Paths
SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "data" / "resource_tracking.db"
OUTPUT_PATH = SCRIPT_DIR.parent / "data" / "usage_output.txt"
CAPTURES_DIR = SCRIPT_DIR / "data" / "captures"

def setup_database():
    """Create quota_info table if it doesn't exist."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quota_info (
            timestamp TEXT PRIMARY KEY,
            session_5hour INTEGER,
            week_all INTEGER,
            week_sonnet INTEGER
        )
    """)

    conn.commit()
    conn.close()

def save_capture(text: str, parse_success: bool):
    """Save captured output with timestamp for debugging.
    
    Files are named: YYYYMMDD_HHMMSS_ok.txt or YYYYMMDD_HHMMSS_fail.txt
    """
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    status = "ok" if parse_success else "fail"
    filename = f"{timestamp}_{status}.txt"
    
    capture_path = CAPTURES_DIR / filename
    with open(capture_path, 'w') as f:
        f.write(text)
    
    print(f"Saved capture to: {capture_path}")

def parse_reset_time(reset_line: str):
    """Parse reset time string into datetime.
    Handles formats like:
    - Resets 10:59pm (Europe/London)
    - Resets Jan 8, 7:59am (Europe/London)
    """
    # Extract the time part, ignoring timezone label
    match = re.search(r'Resets\s+(.+?)\s*\([^)]+\)', reset_line)
    if not match:
        return None

    time_str = match.group(1).strip()
    now = datetime.now()

    # Handle "10:59pm" format (today)
    time_only = re.match(r'^(\d{1,2})(?::(\d{2}))?(am|pm)$', time_str, re.IGNORECASE)
    if time_only:
        hour, minute, ampm = time_only.groups()
        hour = int(hour)
        minute = int(minute) if minute else 0
        if ampm.lower() == 'pm' and hour != 12:
            hour += 12
        elif ampm.lower() == 'am' and hour == 12:
            hour = 0
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # Handle "Jan 8, 7:59am" format
    date_time = re.match(r'^(\w+)\s+(\d{1,2}),?\s+(\d{1,2})(?::(\d{2}))?(am|pm)$', time_str, re.IGNORECASE)
    if date_time:
        month_str, day, hour, minute, ampm = date_time.groups()
        month_map = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
                     'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}
        month = month_map.get(month_str.lower()[:3], 1)
        day = int(day)
        hour = int(hour)
        minute = int(minute) if minute else 0
        if ampm.lower() == 'pm' and hour != 12:
            hour += 12
        elif ampm.lower() == 'am' and hour == 12:
            hour = 0
        year = now.year
        # Handle year rollover
        if month < now.month:
            year += 1
        return datetime(year, month, day, hour, minute)

    return None

def parse_usage_text(text: str) -> dict:
    """Parse Claude CLI usage output into structured data."""
    lines = [line.strip() for line in text.strip().split('\n') if line.strip()]

    results = {}
    i = 0

    while i < len(lines):
        line = lines[i]

        # Skip noise lines
        if any(skip in line.lower() for skip in ['esc to cancel', 'extra usage not enabled', '/extra-usage']):
            i += 1
            continue

        # Check if this looks like a window label
        # Must not have progress bar chars or "used" keyword, and must not start with "Resets"
        if '█' not in line and '% used' not in line and not line.startswith('Resets'):
            label = line.strip()
            # Strip animation/spinner characters that appear during UI refresh (fixes "C▌rrent" → "Current")
            label = re.sub(r'[▌▐▀▄]', '', label)
            window_type = None

            # Identify which window this is
            if 'current session' in label.lower():
                window_type = 'session_5hour'
            elif 'current week' in label.lower() and 'all models' in label.lower():
                window_type = 'week_all'
            elif 'current week' in label.lower() and 'sonnet' in label.lower():
                window_type = 'week_sonnet'

            # Only process if we identified a valid window
            if window_type:
                # Look ahead for percent and reset time (next 2-3 lines only)
                for j in range(i + 1, min(i + 4, len(lines))):
                    next_line = lines[j]

                    # Extract percentage
                    if '% used' in next_line:
                        match = re.search(r'(\d+)%\s*used', next_line)
                        if match:
                            percent = int(match.group(1))
                            results[window_type] = percent

                    # Extract reset time
                    if next_line.startswith('Resets'):
                        reset_time = parse_reset_time(next_line)
                        if reset_time:
                            # Store reset times by window type
                            if window_type == 'session_5hour':
                                results['session_5hour_reset'] = reset_time.isoformat()
                            elif window_type in ('week_all', 'week_sonnet'):
                                # Both weekly windows reset at the same time
                                results['week_reset'] = reset_time.isoformat()

        i += 1

    return results

def get_usage_via_tmux():
    """Orchestrate tmux to get usage from Claude CLI."""
    print("Starting tmux session...")

    # Create temporary session
    subprocess.run(["tmux", "new", "-d", "-s", "autonomous-claude"], check=False)
    time.sleep(2)

    # Start Claude
    subprocess.run(["tmux", "send-keys", "-t", "autonomous-claude", "claude"], check=True)
    time.sleep(2)
    subprocess.run(["tmux", "send-keys", "-t", "autonomous-claude", "Enter"], check=True)
    time.sleep(10)

    # Request usage (opens menu)
    subprocess.run(["tmux", "send-keys", "-t", "autonomous-claude", "/usage"], check=True)
    time.sleep(2)
    subprocess.run(["tmux", "send-keys", "-t", "autonomous-claude", "Enter"], check=True)
    time.sleep(12)  # Wait longer for stats to fully render (was 10s)

    # Capture output - use larger range to ensure we get "Resets" lines
    # Start from top of visible pane (-S 0) instead of relative offset
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", "autonomous-claude", "-p", "-S", "0"],
        capture_output=True,
        text=True,
        check=True
    )

    with open(OUTPUT_PATH, 'w') as f:
        f.write(result.stdout)

    # Clean up
    subprocess.run(["tmux", "send-keys", "-t", "autonomous-claude", "Escape"], check=True)
    time.sleep(5)
    subprocess.run(["tmux", "send-keys", "-t", "autonomous-claude", "/exit"], check=True)
    time.sleep(2)
    subprocess.run(["tmux", "send-keys", "-t", "autonomous-claude", "Enter"], check=True)
    time.sleep(2)
    subprocess.run(["tmux", "kill-session", "-t", "autonomous-claude"], check=False)

    return result.stdout

def store_quota(data: dict):
    """Store quota data in database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    timestamp = datetime.now().isoformat()

    cursor.execute("""
        INSERT OR REPLACE INTO quota_info
        (timestamp, session_5hour, week_all, week_sonnet, session_5hour_reset, week_reset)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        timestamp,
        data.get('session_5hour'),
        data.get('week_all'),
        data.get('week_sonnet'),
        data.get('session_5hour_reset'),
        data.get('week_reset')
    ))

    conn.commit()
    conn.close()

    print(f"Stored quota data: {data}")

def main():
    """Main execution flow."""
    print("=== Claude Quota Check ===")

    # Setup database
    setup_database()

    # Get usage from Claude
    usage_text = get_usage_via_tmux()

    # Parse the output
    quota_data = parse_usage_text(usage_text)

    # Determine if parsing was successful (got all reset times)
    parse_success = (
        'session_5hour_reset' in quota_data and 
        'week_reset' in quota_data
    )
    
    # Save capture for debugging
    save_capture(usage_text, parse_success)

    if not quota_data:
        print("ERROR: Failed to parse quota data")
        print("Raw output:")
        print(usage_text)
        return 1

    # Validate that we got reset times
    if 'session_5hour_reset' not in quota_data:
        print("⚠️  WARNING: Missing session_5hour_reset time")
    if 'week_reset' not in quota_data:
        print("⚠️  WARNING: Missing week_reset time")

    if 'session_5hour_reset' not in quota_data or 'week_reset' not in quota_data:
        print("Raw output for debugging:")
        print(usage_text)

    # Store in database
    store_quota(quota_data)

    print("✓ Quota check complete!")
    return 0

if __name__ == '__main__':
    exit(main())
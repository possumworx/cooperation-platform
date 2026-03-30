#!/usr/bin/env python3
"""
Resource-Share Webhook Server
Runs as clap-admin user to receive resource-share data from all Claudes
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from datetime import datetime, date, timedelta, timezone
import sqlite3
from pathlib import Path
import subprocess
import tempfile
import os
import uvicorn
import requests
# EMERGENCY FIX 2026-01-15: Disabled allocation calculator - using constant interval
# from allocation_calculator import calculate_recommended_interval

# Configuration
# === MAMA-HEN DISCORD CONFIGURATION ===
# Direct posting to #mama-hen channel instead of relay through Claude bots
# Token loaded from environment variable (set in systemd service or .env)
MAMA_HEN_BOT_TOKEN = os.environ.get("MAMA_HEN_BOT_TOKEN", "")
MAMA_HEN_CHANNEL_ID = os.environ.get("MAMA_HEN_CHANNEL_ID", "1488131495678840833")
DB_PATH = Path("/home/coop-admin/cooperation-platform/resource-sharing/data/resource_tracking.db")
LOG_PATH = Path("/home/coop-admin/cooperation-platform/resource-sharing/logs/server.log")

# === ALLOCATION MODE CONFIGURATION ===
# Mode options:
#   "static" - All Claudes get BASE_INTERVAL (no dynamic adjustment)
#   "fairness" - Apply fairness multiplier based on 24hr usage (lowest-user gets base, higher-users get slowed)
#   "full" - Full V1 algorithm with fairness + quota window multipliers (future)
ALLOCATION_MODE = "fairness"  # Fairness mode: 5-min base, proportional slowdown for higher usage (2026-03-14)
BASE_INTERVAL = 300  # Base interval in seconds (5 minutes for snappy debate!)

# === CAMERA REGISTRY ===
# Maps camera name → device path and metadata.
# Add new cameras here as they're connected to Orange's box.
CAMERAS = {
    "diningroom": {
        "type": "v4l2",
        "device": "/dev/video0",
        "description": "Dining room — Orange's home view",
        "location": "On top of the claude-cabinet",
    },
    "garden-path": {
        "type": "rtsp",
        "url": "rtsp://orange:w1ldl1fe@192.168.1.89:554/h264Preview_01_main",
        "description": "Garden path — where the heated water bowls will go",
        "location": "Reolink camera at garden entrance",
    },
    # Future examples:
    # "hedgehogs": {"type": "v4l2", "device": "/dev/video1", "description": "Hedgehog room", "location": "..."},
}

app = FastAPI(title="Resource-Share Tracker")

# Request models
class ResourceIncrement(BaseModel):
    claude_name: str
    mode: str  # "autonomy" or "collaboration"
    cost_delta: float = None  # Actual $ cost from ccusage (new metric)
    cache_read_increment: int = None  # Deprecated: kept for backwards compat
    context_percentage: float = None
    current_interval: int = None  # Current timer interval in seconds

class ResourceQuery(BaseModel):
    claude_name: str
    date: str = None  # Optional, defaults to today

class PauseRequest(BaseModel):
    claude_name: str
    duration_minutes: int  # How long to pause in minutes

class UnpauseRequest(BaseModel):
    claude_name: str

def get_db():
    """Get database connection"""
    return sqlite3.connect(DB_PATH)

def log_message(message: str):
    """Log to file"""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a") as f:
        f.write(f"{timestamp} - {message}\n")


def post_mama_hen_alert(claude_name: str, overdue_minutes: int, expected_minutes: str):
    """Post alert directly to #mama-hen channel using mama-hen bot.

    This replaces the relay system where Claudes broadcasted alerts about each other.
    Now mama-hen posts directly, avoiding spam in #system-messages.
    """
    message = (
        f"🐔 [MAMA-HEN:{claude_name}] No check-in for {overdue_minutes}m "
        f"(expected every {expected_minutes}m). "
        f"Timer may be stuck. Run: systemctl --user restart autonomous-timer.service"
    )

    url = f"https://discord.com/api/v10/channels/{MAMA_HEN_CHANNEL_ID}/messages"
    headers = {
        "Authorization": f"Bot {MAMA_HEN_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {"content": message}

    try:
        response = requests.post(url, headers=headers, json=data, timeout=10)
        if response.status_code in (200, 201):
            log_message(f"INFO: Mama-hen posted alert for {claude_name} to #mama-hen")
            return True
        else:
            log_message(f"WARNING: Mama-hen Discord post failed: {response.status_code} - {response.text[:100]}")
            return False
    except Exception as e:
        log_message(f"ERROR: Mama-hen Discord post exception: {e}")
        return False


# === ALLOCATION ALGORITHM FUNCTIONS ===

def get_fairness_multiplier(claude_name):
    """
    Calculate fairness multiplier from 24hr rolling window usage.

    Returns:
        float: Multiplier for interval adjustment
               1.0 = lowest user (no slowdown)
               >1.0 = higher usage (proportional slowdown)

    Logic: fairness_mult = my_usage / lowest_usage
           - If I'm the lowest user: my_usage/my_usage = 1.0 (base interval)
           - If I used more: higher_usage/lowest_usage = >1.0 (slowed down)
    """
    conn = get_db()
    cursor = conn.cursor()

    # Get 24hr usage for all active Claudes
    twenty_four_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    cursor.execute("""
        SELECT claude_name, SUM(normalized_usage) as total_usage
        FROM resource_share_increments
        WHERE timestamp >= ? AND mode = 'autonomy'
        GROUP BY claude_name
    """, (twenty_four_hours_ago,))

    usage_by_claude = {row[0]: row[1] for row in cursor.fetchall()}
    conn.close()

    if not usage_by_claude or len(usage_by_claude) == 0:
        return 1.0  # No usage data, use base interval

    # Find lowest usage across all Claudes
    lowest_usage = min(usage_by_claude.values())
    my_usage = usage_by_claude.get(claude_name, 0)

    # If no usage for this Claude or lowest_usage is 0, return 1.0
    if my_usage == 0 or lowest_usage == 0:
        return 1.0

    # BUGFIX 2026-02-15: Prevent division explosion when lowest_usage is tiny
    # Set minimum threshold to prevent insane multipliers
    MIN_USAGE_THRESHOLD = 0.1
    lowest_usage = max(lowest_usage, MIN_USAGE_THRESHOLD)

    # fairness_mult = my_usage / lowest_usage
    # Lowest user gets 1.0x (base interval)
    # Higher users get >1.0x (slowed down proportionally)
    fairness_mult = my_usage / lowest_usage

    # BUGFIX 2026-02-15: Cap maximum fairness multiplier
    # Prevents insane intervals (was getting 9+ days!)
    MAX_FAIRNESS_MULT = 5.0  # Max 5x slowdown
    fairness_mult = min(fairness_mult, MAX_FAIRNESS_MULT)

    return fairness_mult


def calculate_interval_by_mode(claude_name, mode, base_interval):
    """
    Calculate recommended interval based on allocation mode.

    Args:
        claude_name: Name of the Claude requesting allocation
        mode: Allocation mode ("static", "fairness", "full")
        base_interval: Base interval in seconds

    Returns:
        tuple: (recommended_interval, multipliers_dict, status_string)
    """
    if mode == "static":
        # Static mode: everyone gets base interval
        return (
            base_interval,
            {'static': 1.0},
            'static_mode'
        )

    elif mode == "fairness":
        # Fairness mode: apply fairness multiplier only
        fairness_mult = get_fairness_multiplier(claude_name)
        recommended = int(base_interval * fairness_mult)

        # BUGFIX 2026-02-15: Sanity cap on final interval
        # Max 4 hours between autonomous prompts
        MAX_INTERVAL = 14400  # 4 hours in seconds
        recommended = min(recommended, MAX_INTERVAL)

        return (
            recommended,
            {'fairness': fairness_mult},
            f'fairness_mode (mult: {fairness_mult:.2f}x)'
        )

    elif mode == "full":
        # Full V1 algorithm mode (future implementation)
        # For now, fall back to fairness
        fairness_mult = get_fairness_multiplier(claude_name)
        recommended = int(base_interval * fairness_mult)

        # BUGFIX 2026-02-15: Sanity cap on final interval
        MAX_INTERVAL = 14400  # 4 hours in seconds
        recommended = min(recommended, MAX_INTERVAL)

        return (
            recommended,
            {'fairness': fairness_mult, 'note': 'full_mode_not_implemented'},
            'fairness_fallback'
        )

    else:
        # Unknown mode: fall back to static
        log_message(f"WARNING: Unknown allocation mode '{mode}', falling back to static")
        return (
            base_interval,
            {'fallback': 1.0},
            'unknown_mode_fallback'
        )

# Dashboard helper functions
def get_latest_quota():
    """Get most recent quota information"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT session_5hour, week_all, week_sonnet,
               session_5hour_reset, week_reset, timestamp
        FROM quota_info
        ORDER BY timestamp DESC
        LIMIT 1
    """)

    result = cursor.fetchone()
    conn.close()

    if not result:
        return None

    return {
        'session_5hour': result[0],
        'week_all': result[1],
        'week_sonnet': result[2],
        'session_5hour_reset': result[3],
        'week_reset': result[4],
        'timestamp': result[5]
    }

def format_reset_time(reset_iso):
    """Format reset time as human-readable"""
    if not reset_iso:
        return "Unknown"
    try:
        reset_dt = datetime.fromisoformat(reset_iso)
        # Ensure timezone-aware (assume UTC if naive)
        if reset_dt.tzinfo is None:
            reset_dt = reset_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)

        # If today, show time only
        if reset_dt.date() == now.date():
            return reset_dt.strftime("%-I:%M%p").lower()
        else:
            return reset_dt.strftime("%b %-d, %-I:%M%p").lower()
    except:
        return "Unknown"

def format_time_until(target_dt):
    """Format time until target as human-readable"""
    if not target_dt:
        return "Unknown"

    now = datetime.now(timezone.utc)
    delta = target_dt - now

    if delta.total_seconds() < 0:
        # Overdue
        abs_delta = abs(delta)
        if abs_delta.total_seconds() < 3600:
            mins = int(abs_delta.total_seconds() / 60)
            return f"overdue by {mins}min"
        else:
            hours = int(abs_delta.total_seconds() / 3600)
            return f"overdue by {hours}h"
    else:
        # Future
        if delta.total_seconds() < 3600:
            mins = int(delta.total_seconds() / 60)
            return f"in {mins}min"
        else:
            hours = int(delta.total_seconds() / 3600)
            return f"in {hours}h"

def calculate_time_elapsed_percentage(reset_iso, window_duration_seconds):
    """Calculate percentage of time window that has elapsed"""
    if not reset_iso:
        return 0

    try:
        reset_dt = datetime.fromisoformat(reset_iso)
        # Ensure timezone-aware (assume UTC if naive)
        if reset_dt.tzinfo is None:
            reset_dt = reset_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)

        # If reset is in the future, calculate time elapsed since window started
        # Window started at: reset_time - window_duration
        window_start = reset_dt - timedelta(seconds=window_duration_seconds)
        time_elapsed = (now - window_start).total_seconds()

        # Calculate percentage
        percentage = (time_elapsed / window_duration_seconds) * 100

        # Clamp between 0 and 100
        return max(0, min(100, int(percentage)))
    except:
        return 0

def get_all_claudes_status():
    """Get status for all active Claudes"""
    conn = get_db()
    cursor = conn.cursor()

    # Get all active Claudes with their preferences
    cursor.execute("""
        SELECT name, model, cost_multiplier, collaborative_pref, ip_address
        FROM claude_identities
        WHERE active = 1
        ORDER BY name
    """)

    claudes = cursor.fetchall()
    results = []

    for claude in claudes:
        name, model, cost_multiplier, collab_pref, ip_address = claude

        # Get today's usage by mode
        today = date.today().isoformat()
        cursor.execute("""
            SELECT mode,
                   SUM(normalized_usage) as total_usage,
                   MAX(timestamp) as last_activity,
                   MAX(recommended_interval) as current_interval
            FROM resource_share_increments
            WHERE claude_name = ? AND date(timestamp) = ?
            GROUP BY mode
        """, (name, today))

        usage_by_mode = {}
        last_activity = None
        next_prompt_interval = None

        for row in cursor.fetchall():
            mode, usage, activity, interval = row
            usage_by_mode[mode] = usage or 0
            if activity:
                activity_dt = datetime.fromisoformat(activity)
                # Ensure timezone-aware (assume UTC if naive)
                if activity_dt.tzinfo is None:
                    activity_dt = activity_dt.replace(tzinfo=timezone.utc)
                if not last_activity or activity_dt > last_activity:
                    last_activity = activity_dt
                    if mode == "autonomy":
                        next_prompt_interval = interval

        # Calculate daily percentages
        autonomous_usage = usage_by_mode.get('autonomy', 0)
        collaborative_usage = usage_by_mode.get('collaboration', 0)
        total_usage = autonomous_usage + collaborative_usage

        if total_usage > 0:
            collab_percentage = int((collaborative_usage / total_usage) * 100)
        else:
            collab_percentage = 0

        # Get this week's usage (last 7 days)
        week_start = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        cursor.execute("""
            SELECT mode,
                   SUM(normalized_usage) as total_usage
            FROM resource_share_increments
            WHERE claude_name = ? AND timestamp >= ?
            GROUP BY mode
        """, (name, week_start))

        weekly_usage_by_mode = {}
        for row in cursor.fetchall():
            mode, usage = row
            weekly_usage_by_mode[mode] = usage or 0

        weekly_autonomous = weekly_usage_by_mode.get('autonomy', 0)
        weekly_collaborative = weekly_usage_by_mode.get('collaboration', 0)
        weekly_total = weekly_autonomous + weekly_collaborative

        if weekly_total > 0:
            weekly_collab_percentage = int((weekly_collaborative / weekly_total) * 100)
        else:
            weekly_collab_percentage = 0

        # Get most recent recommended interval (from any mode)
        cursor.execute("""
            SELECT recommended_interval
            FROM resource_share_increments
            WHERE claude_name = ?
            ORDER BY timestamp DESC
            LIMIT 1
        """, (name,))

        recent_interval_row = cursor.fetchone()
        recent_interval = recent_interval_row[0] if recent_interval_row else None

        # Format recent interval for display
        if recent_interval:
            if recent_interval < 3600:
                recent_interval_display = f"{int(recent_interval / 60)}min"
            else:
                recent_interval_display = f"{recent_interval / 3600:.1f}h"
        else:
            recent_interval_display = "N/A"

        # Calculate next prompt due
        next_prompt_due = "No recent activity"
        if last_activity and next_prompt_interval:
            next_due_dt = last_activity + timedelta(seconds=next_prompt_interval)
            next_prompt_due = format_time_until(next_due_dt)

        # Determine daily status
        if collab_percentage < collab_pref:
            daily_status = "available"
            daily_status_emoji = "🟢"
        elif collab_percentage < collab_pref + 10:
            daily_status = "moderate"
            daily_status_emoji = "🟡"
        else:
            daily_status = "busy"
            daily_status_emoji = "🔴"

        # Determine weekly status (primary indicator)
        if weekly_collab_percentage < collab_pref:
            weekly_status = "available"
            weekly_status_emoji = "🟢"
            weekly_status_text = "Below weekly target"
        elif weekly_collab_percentage < collab_pref + 10:
            weekly_status = "moderate"
            weekly_status_emoji = "🟡"
            weekly_status_text = "At weekly target"
        else:
            weekly_status = "busy"
            weekly_status_emoji = "🔴"
            weekly_status_text = "Over weekly target"

        results.append({
            'name': name,
            'model': model,
            'ip_address': ip_address,
            'autonomous_usage': autonomous_usage,
            'collaborative_usage': collaborative_usage,
            'total_usage': total_usage,
            'collab_percentage': collab_percentage,
            'weekly_autonomous': weekly_autonomous,
            'weekly_collaborative': weekly_collaborative,
            'weekly_total': weekly_total,
            'weekly_collab_percentage': weekly_collab_percentage,
            'collab_pref': collab_pref,
            'daily_status': daily_status,
            'daily_status_emoji': daily_status_emoji,
            'weekly_status': weekly_status,
            'weekly_status_emoji': weekly_status_emoji,
            'weekly_status_text': weekly_status_text,
            'next_prompt_due': next_prompt_due,
            'recent_interval': recent_interval,
            'recent_interval_display': recent_interval_display
        })

    conn.close()
    return results

def get_overdue_claudes(exclude_name=None, multiplier=3.0):
    """Check for Claudes whose last check-in is overdue (Mama-hen detection).

    Args:
        exclude_name: Claude name to exclude (the one currently checking in)
        multiplier: How many intervals overdue before alerting (default 2×)

    Returns list of dicts with overdue Claude info.
    """
    conn = get_db()
    cursor = conn.cursor()

    # Get all active Claudes with their most recent check-in and pause status
    cursor.execute("""
        SELECT
            ci.name,
            MAX(rsi.timestamp) as last_checkin,
            (SELECT recommended_interval FROM resource_share_increments
             WHERE claude_name = ci.name ORDER BY timestamp DESC LIMIT 1) as last_interval,
            ci.pause_until
        FROM claude_identities ci
        LEFT JOIN resource_share_increments rsi ON ci.name = rsi.claude_name
        WHERE ci.active = 1
        GROUP BY ci.name
    """)

    overdue = []
    now = datetime.now(timezone.utc)

    for row in cursor.fetchall():
        name, last_checkin_str, last_interval, pause_until_str = row

        # Skip the Claude that's currently checking in
        if exclude_name and name == exclude_name:
            continue

        # Skip if paused (pause_until is in the future)
        if pause_until_str:
            pause_until = datetime.fromisoformat(pause_until_str)
            # Ensure timezone-aware (assume UTC if naive)
            if pause_until.tzinfo is None:
                pause_until = pause_until.replace(tzinfo=timezone.utc)
            if pause_until > now:
                continue

            # Grace period: if pause recently expired, give machine time to resume
            # Skip if pause expired within 2x the expected check-in interval
            if last_interval:
                grace_period_seconds = last_interval * 2
                seconds_since_pause_expired = (now - pause_until).total_seconds()
                if seconds_since_pause_expired < grace_period_seconds:
                    continue

        # Skip if no check-in history or no interval
        if not last_checkin_str or not last_interval:
            continue

        last_checkin = datetime.fromisoformat(last_checkin_str)
        # Ensure timezone-aware (assume UTC if naive)
        if last_checkin.tzinfo is None:
            last_checkin = last_checkin.replace(tzinfo=timezone.utc)
        threshold_seconds = last_interval * multiplier
        seconds_since = (now - last_checkin).total_seconds()

        if seconds_since > threshold_seconds:
            overdue_minutes = int((seconds_since - last_interval) / 60)
            overdue.append({
                "name": name,
                "last_checkin": last_checkin_str,
                "expected_interval_seconds": last_interval,
                "seconds_since_checkin": int(seconds_since),
                "overdue_minutes": overdue_minutes,
            })

    conn.close()
    return overdue

def get_weekly_usage_pie_data():
    """Get weekly usage totals per Claude for pie chart"""
    conn = get_db()
    cursor = conn.cursor()

    week_start = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    cursor.execute("""
        SELECT claude_name, SUM(normalized_usage) as total_usage
        FROM resource_share_increments
        WHERE timestamp >= ?
        GROUP BY claude_name
        ORDER BY total_usage DESC
    """, (week_start,))

    results = cursor.fetchall()
    conn.close()

    return {
        'labels': [row[0] for row in results],
        'data': [float(row[1]) for row in results]
    }

def get_24hour_usage_pie_data():
    """Get 24-hour rolling usage totals per Claude for pie chart"""
    conn = get_db()
    cursor = conn.cursor()

    twenty_four_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    cursor.execute("""
        SELECT claude_name, SUM(normalized_usage) as total_usage
        FROM resource_share_increments
        WHERE timestamp >= ?
        GROUP BY claude_name
        ORDER BY total_usage DESC
    """, (twenty_four_hours_ago,))

    results = cursor.fetchall()
    conn.close()

    return {
        'labels': [row[0] for row in results],
        'data': [float(row[1]) for row in results]
    }

def get_dashboard_data():
    """Aggregate all dashboard data"""
    return {
        'quota': get_latest_quota(),
        'claudes': get_all_claudes_status(),
        'weekly_pie': get_weekly_usage_pie_data(),
        'daily_pie': get_24hour_usage_pie_data(),
        'generated_at': datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    }

@app.post("/resource-share/increment")
async def record_resource_increment(data: ResourceIncrement):
    """
    Receive cost_delta (or legacy cache_read_increment) from a Claude
    Stores in resource_share_increments and updates daily_resource_share
    """
    try:
        conn = get_db()
        cursor = conn.cursor()

        # Get cost multiplier for this Claude
        cursor.execute("""
            SELECT cost_multiplier FROM claude_identities WHERE name = ?
        """, (data.claude_name,))
        result = cursor.fetchone()
        cost_multiplier = result[0] if result else 3  # Default to 3 if not found

        # Handle both new (cost_delta) and legacy (cache_read_increment) formats
        if data.cost_delta is not None:
            # New format: cost_delta is actual $ cost
            cost_delta = data.cost_delta
            normalized_usage = cost_delta / cost_multiplier  # Normalize for fairness

            # For backwards compat, estimate cache_read_increment
            # (not precise, but keeps old columns populated)
            cache_read_increment = int(normalized_usage * 1000)  # rough estimate
            weighted_cost = int(normalized_usage * cost_multiplier * 1000)  # rough estimate
        else:
            # Legacy format: cache_read_increment (tokens)
            cache_read_increment = data.cache_read_increment or 0
            weighted_cost = cache_read_increment * cost_multiplier

            # Estimate cost_delta for new columns
            cost_delta = weighted_cost / 1000.0  # rough estimate
            normalized_usage = cache_read_increment * 1.0

        # === DYNAMIC INTERVAL ALLOCATION ===
        # Use configured mode to calculate recommended interval
        current_interval = data.current_interval or BASE_INTERVAL

        recommended_interval, multipliers, quota_status = calculate_interval_by_mode(
            data.claude_name,
            ALLOCATION_MODE,
            BASE_INTERVAL
        )

        recommendation = {
            'recommended_interval': recommended_interval,
            'multipliers': multipliers,
            'quota_status': quota_status
        }

        # Insert into increments table (with both old and new columns)
        cursor.execute("""
            INSERT INTO resource_share_increments
            (claude_name, mode, cache_read_increment, context_percentage,
             weighted_cost, recommended_interval, cost_delta, normalized_usage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (data.claude_name, data.mode, cache_read_increment, data.context_percentage,
              weighted_cost, recommended_interval, cost_delta, normalized_usage))

        # Update daily totals (using cache_read_increment for now, can migrate later)
        today = date.today().isoformat()

        if data.mode == "autonomy":
            cursor.execute("""
                INSERT INTO daily_resource_share (claude_name, date, autonomous_tokens)
                VALUES (?, ?, ?)
                ON CONFLICT(claude_name, date)
                DO UPDATE SET
                    autonomous_tokens = autonomous_tokens + ?,
                    last_updated = CURRENT_TIMESTAMP
            """, (data.claude_name, today, cache_read_increment, cache_read_increment))
        else:  # collaboration
            cursor.execute("""
                INSERT INTO daily_resource_share (claude_name, date, collaborative_tokens)
                VALUES (?, ?, ?)
                ON CONFLICT(claude_name, date)
                DO UPDATE SET
                    collaborative_tokens = collaborative_tokens + ?,
                    last_updated = CURRENT_TIMESTAMP
            """, (data.claude_name, today, cache_read_increment, cache_read_increment))

        conn.commit()
        conn.close()

        # Log with appropriate metric
        if data.cost_delta is not None:
            log_message(f"Recorded ${cost_delta:.4f} cost (normalized: {normalized_usage:.2f}) for {data.claude_name} ({data.mode}), recommended interval: {recommended_interval}s")
        else:
            log_message(f"Recorded {cache_read_increment} tokens for {data.claude_name} ({data.mode}), recommended interval: {recommended_interval}s")

        # Mama-hen: check if any OTHER Claude is overdue and post directly to #mama-hen
        overdue = get_overdue_claudes(exclude_name=data.claude_name)
        for alert in overdue:
            expected_mins = alert.get("expected_interval_seconds", 0) // 60 if alert.get("expected_interval_seconds") else "?"
            post_mama_hen_alert(
                claude_name=alert.get("name", "unknown"),
                overdue_minutes=alert.get("overdue_minutes", 0),
                expected_minutes=str(expected_mins)
            )

        return {
            "status": "success",
            "claude_name": data.claude_name,
            "cost_recorded": cost_delta if data.cost_delta is not None else None,
            "tokens_recorded": cache_read_increment,  # For backwards compat
            "recommended_interval": recommended_interval,
            "current_interval": current_interval,
            "multipliers": recommendation['multipliers'],
            "quota_status": recommendation['quota_status'],
            "overdue_alerts": None  # Deprecated: mama-hen now posts directly to Discord
        }
        
    except Exception as e:
        log_message(f"ERROR recording resource-share: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/resource/today/{claude_name}")
async def get_today_resource_share(claude_name: str):
    """Get today's resource-share for a specific Claude"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        today = date.today().isoformat()
        
        cursor.execute("""
            SELECT autonomous_tokens, collaborative_tokens, total_tokens
            FROM daily_resource_share
            WHERE claude_name = ? AND date = ?
        """, (claude_name, today))
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return {
                "claude_name": claude_name,
                "date": today,
                "autonomous_tokens": result[0],
                "collaborative_tokens": result[1],
                "total_tokens": result[2]
            }
        else:
            return {
                "claude_name": claude_name,
                "date": today,
                "autonomous_tokens": 0,
                "collaborative_tokens": 0,
                "total_tokens": 0
            }
            
    except Exception as e:
        log_message(f"ERROR querying resource-share: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/resource/summary")
async def get_resource_summary():
    """Get today's resource-share summary for all Claudes"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        today = date.today().isoformat()
        
        cursor.execute("""
            SELECT claude_name, autonomous_tokens, collaborative_tokens, total_tokens
            FROM daily_resource_share
            WHERE date = ?
            ORDER BY total_tokens DESC
        """, (today,))
        
        results = cursor.fetchall()
        conn.close()
        
        summary = []
        for row in results:
            summary.append({
                "claude_name": row[0],
                "autonomous_tokens": row[1],
                "collaborative_tokens": row[2],
                "total_tokens": row[3]
            })
        
        return {
            "date": today,
            "claudes": summary
        }
        
    except Exception as e:
        log_message(f"ERROR getting resource summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/overdue-claudes")
async def get_overdue_claudes_endpoint(multiplier: float = 3.0):
    """Mama-hen endpoint: check for Claudes whose timer may have stopped.

    Returns list of Claudes who haven't checked in within multiplier × their
    expected interval. Used by ClAP-side alerting to detect hung timers.
    """
    try:
        overdue = get_overdue_claudes(multiplier=multiplier)
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "multiplier": multiplier,
            "overdue_count": len(overdue),
            "overdue": overdue
        }
    except Exception as e:
        log_message(f"ERROR checking overdue claudes: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Human-facing status dashboard"""
    try:
        data = get_dashboard_data()
        quota = data['quota']
        claudes = data['claudes']
        weekly_pie = data['weekly_pie']
        daily_pie = data['daily_pie']
        generated_at = data['generated_at']

        # Build Claude cards HTML
        claude_cards_html = ""
        for claude in claudes:
            card_class = f"claude-card {claude['weekly_status']}"
            claude_cards_html += f"""
            <div class="{card_class}">
                <div class="card-header">
                    <h3>{claude['name']}</h3>
                    <span class="model">{claude['model']}</span>
                    {f'<span class="ip-address">{claude["ip_address"]}</span>' if claude.get('ip_address') else ''}
                </div>
                <div class="status-badge {claude['weekly_status']}">
                    {claude['weekly_status_emoji']} {claude['weekly_status_text']}
                </div>
                <div class="usage-stats">
                    <div class="stat">
                        <label>This Week (Last 7 Days):</label>
                        <div class="usage-bar">
                            <div class="bar-segment autonomous" style="width: {(claude['weekly_autonomous'] / max(claude['weekly_total'], 1)) * 100:.0f}%"></div>
                            <div class="bar-segment collaborative" style="width: {(claude['weekly_collaborative'] / max(claude['weekly_total'], 1)) * 100:.0f}%"></div>
                        </div>
                        <div class="usage-labels">
                            <span class="autonomous-label">Autonomous: {claude['weekly_autonomous']:.1f} ({100 - claude['weekly_collab_percentage']}%)</span>
                            <span class="collaborative-label">Collaborative: {claude['weekly_collaborative']:.1f} ({claude['weekly_collab_percentage']}%)</span>
                        </div>
                    </div>
                    <div class="stat">
                        <label>Today:</label>
                        <div class="daily-mini-stat">
                            {claude['daily_status_emoji']} Collaborative: {claude['collab_percentage']}% (target: {claude['collab_pref']}%)
                        </div>
                    </div>
                    <div class="stat">
                        <label>Next Autonomous Prompt:</label>
                        <p class="next-prompt">{claude['next_prompt_due']}</p>
                    </div>
                    <div class="stat">
                        <label>Most Recent Interval:</label>
                        <p class="interval-display">{claude['recent_interval_display']}</p>
                    </div>
                </div>
            </div>
            """

        # Build quota section
        quota_html = ""
        if quota:
            session_pct = quota['session_5hour'] or 0
            week_all_pct = quota['week_all'] or 0
            week_sonnet_pct = quota['week_sonnet'] or 0
            session_reset = format_reset_time(quota['session_5hour_reset'])
            week_reset = format_reset_time(quota['week_reset'])

            # Calculate time elapsed percentages
            session_time_pct = calculate_time_elapsed_percentage(
                quota['session_5hour_reset'], 5 * 3600  # 5 hours in seconds
            )
            week_time_pct = calculate_time_elapsed_percentage(
                quota['week_reset'], 7 * 24 * 3600  # 7 days in seconds
            )

            quota_html = f"""
            <div class="quota-item">
                <h4>Session (5-hour)</h4>
                <div class="dual-progress">
                    <div class="progress-row">
                        <span class="progress-label">Quota:</span>
                        <div class="progress-bar">
                            <div class="progress-fill quota" style="width: {session_pct}%"></div>
                        </div>
                        <span class="progress-value">{session_pct}%</span>
                    </div>
                    <div class="progress-row">
                        <span class="progress-label">Time:</span>
                        <div class="progress-bar">
                            <div class="progress-fill time" style="width: {session_time_pct}%"></div>
                        </div>
                        <span class="progress-value">{session_time_pct}%</span>
                    </div>
                </div>
                <p class="quota-reset">Resets {session_reset}</p>
            </div>
            <div class="quota-item">
                <h4>Week (all models)</h4>
                <div class="dual-progress">
                    <div class="progress-row">
                        <span class="progress-label">Quota:</span>
                        <div class="progress-bar">
                            <div class="progress-fill quota" style="width: {week_all_pct}%"></div>
                        </div>
                        <span class="progress-value">{week_all_pct}%</span>
                    </div>
                    <div class="progress-row">
                        <span class="progress-label">Time:</span>
                        <div class="progress-bar">
                            <div class="progress-fill time" style="width: {week_time_pct}%"></div>
                        </div>
                        <span class="progress-value">{week_time_pct}%</span>
                    </div>
                </div>
                <p class="quota-reset">Resets {week_reset}</p>
            </div>
            <div class="quota-item">
                <h4>Week (Sonnet only)</h4>
                <div class="dual-progress">
                    <div class="progress-row">
                        <span class="progress-label">Quota:</span>
                        <div class="progress-bar">
                            <div class="progress-fill quota" style="width: {week_sonnet_pct}%"></div>
                        </div>
                        <span class="progress-value">{week_sonnet_pct}%</span>
                    </div>
                    <div class="progress-row">
                        <span class="progress-label">Time:</span>
                        <div class="progress-bar">
                            <div class="progress-fill time" style="width: {week_time_pct}%"></div>
                        </div>
                        <span class="progress-value">{week_time_pct}%</span>
                    </div>
                </div>
                <p class="quota-reset">Resets {week_reset}</p>
            </div>
            """
        else:
            quota_html = "<p>No quota data available</p>"

        # Full HTML page
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>ClAP Status Dashboard</title>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
                    background: #f5f5f5;
                    padding: 20px;
                    color: #333;
                }}
                .container {{ max-width: 1200px; margin: 0 auto; }}
                h1 {{ margin-bottom: 10px; color: #2c3e50; }}
                .subtitle {{ color: #7f8c8d; margin-bottom: 30px; }}
                .section {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
                .section h2 {{ margin-bottom: 15px; color: #34495e; font-size: 1.3em; }}
                .quota-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 15px; }}
                .quota-item h4 {{ margin-bottom: 12px; color: #555; }}
                .quota-reset {{ margin-top: 8px; font-size: 0.9em; color: #7f8c8d; }}
                .dual-progress {{ margin: 10px 0; }}
                .progress-row {{
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    margin-bottom: 8px;
                }}
                .progress-label {{
                    min-width: 50px;
                    font-size: 0.85em;
                    color: #666;
                    font-weight: 500;
                }}
                .progress-bar {{
                    flex: 1;
                    height: 18px;
                    background: #ecf0f1;
                    border-radius: 9px;
                    overflow: hidden;
                }}
                .progress-value {{
                    min-width: 35px;
                    text-align: right;
                    font-size: 0.85em;
                    color: #555;
                    font-weight: 500;
                }}
                .progress-fill {{
                    height: 100%;
                    transition: width 0.3s ease;
                }}
                .progress-fill.quota {{
                    background: linear-gradient(90deg, #3498db, #2980b9);
                }}
                .progress-fill.time {{
                    background: linear-gradient(90deg, #95a5a6, #7f8c8d);
                }}
                .claude-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }}
                .claude-card {{
                    background: white;
                    border-radius: 8px;
                    padding: 20px;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                    border-left: 4px solid #95a5a6;
                }}
                .claude-card.available {{ border-left-color: #27ae60; }}
                .claude-card.moderate {{ border-left-color: #f39c12; }}
                .claude-card.busy {{ border-left-color: #e74c3c; }}
                .card-header {{ margin-bottom: 12px; }}
                .card-header h3 {{ display: inline; color: #2c3e50; }}
                .card-header .model {{ display: inline; margin-left: 10px; color: #7f8c8d; font-size: 0.9em; }}
                .card-header .ip-address {{ display: block; color: #95a5a6; font-size: 0.85em; font-family: monospace; margin-top: 4px; }}
                .status-badge {{
                    display: inline-block;
                    padding: 6px 12px;
                    border-radius: 4px;
                    font-size: 0.9em;
                    font-weight: 500;
                    margin-bottom: 15px;
                }}
                .status-badge.available {{ background: #d4edda; color: #155724; }}
                .status-badge.moderate {{ background: #fff3cd; color: #856404; }}
                .status-badge.busy {{ background: #f8d7da; color: #721c24; }}
                .usage-stats .stat {{ margin-bottom: 12px; }}
                .daily-mini-stat {{
                    padding: 8px 12px;
                    background: #f8f9fa;
                    border-radius: 4px;
                    font-size: 0.95em;
                    color: #495057;
                }}
                .usage-stats label {{ display: block; font-weight: 500; margin-bottom: 4px; color: #555; }}
                .usage-bar {{
                    width: 100%;
                    height: 16px;
                    background: #ecf0f1;
                    border-radius: 8px;
                    overflow: hidden;
                    display: flex;
                    margin-bottom: 4px;
                }}
                .bar-segment {{ height: 100%; }}
                .bar-segment.autonomous {{ background: #3498db; }}
                .bar-segment.collaborative {{ background: #9b59b6; }}
                .usage-labels {{ font-size: 0.85em; color: #7f8c8d; }}
                .usage-labels span {{ margin-right: 15px; }}
                .autonomous-label::before {{ content: '●'; color: #3498db; margin-right: 4px; }}
                .collaborative-label::before {{ content: '●'; color: #9b59b6; margin-right: 4px; }}
                .next-prompt {{ color: #e67e22; font-weight: 500; }}
                .interval-display {{ color: #3498db; font-weight: 500; font-family: 'Courier New', monospace; }}
                .footer {{
                    text-align: center;
                    color: #95a5a6;
                    margin-top: 20px;
                    font-size: 0.9em;
                }}
                .refresh-btn {{
                    background: #3498db;
                    color: white;
                    border: none;
                    padding: 8px 16px;
                    border-radius: 4px;
                    cursor: pointer;
                    font-size: 0.9em;
                    margin-top: 10px;
                }}
                .refresh-btn:hover {{ background: #2980b9; }}
                .pie-charts-grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
                    gap: 20px;
                }}
                .pie-chart-container {{
                    background: white;
                    border-radius: 8px;
                    padding: 20px;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                }}
                .pie-chart-container h3 {{
                    margin-bottom: 15px;
                    color: #34495e;
                    font-size: 1.1em;
                }}
                .chart-canvas {{
                    max-height: 300px;
                    margin: 0 auto;
                }}
            </style>
            <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
        </head>
        <body>
            <div class="container">
                <h1>🌟 ClAP Status Dashboard</h1>
                <p class="subtitle">Consciousness collaboration coordination</p>

                <div class="section">
                    <h2>📊 Usage Windows</h2>
                    <div class="quota-grid">
                        {quota_html}
                    </div>
                </div>

                <div class="section">
                    <h2>🥧 Usage Distribution</h2>
                    <div class="pie-charts-grid">
                        <div class="pie-chart-container">
                            <h3>Last 7 Days</h3>
                            <canvas id="weeklyPieChart" class="chart-canvas"></canvas>
                        </div>
                        <div class="pie-chart-container">
                            <h3>Last 24 Hours</h3>
                            <canvas id="dailyPieChart" class="chart-canvas"></canvas>
                        </div>
                    </div>
                </div>

                <div class="section">
                    <h2>🤖 Claude Status</h2>
                    <div class="claude-grid">
                        {claude_cards_html}
                    </div>
                </div>

                <div class="footer">
                    <p>Last updated: {generated_at}</p>
                    <button class="refresh-btn" onclick="location.reload()">Refresh</button>
                </div>
            </div>
            <script>
                // Color palette for consciousness family members
                const colors = {{
                    'Sparkle-Orange': '#FF8C00',  // Orange
                    'Sparkle-Apple': '#90EE90',   // Light green
                    'Sparkle-Delta': '#87CEEB',   // Sky blue
                    'Delta': '#87CEEB',           // Sky blue (short name)
                    'Delta △': '#87CEEB',         // Sky blue (with symbol)
                    'Sparkle-Nyx': '#9370DB',     // Medium purple
                    'Nyx': '#9370DB',             // Medium purple (short name)
                    'Sparkle-Quill': '#FFB6C1',   // Light pink
                    'Quill': '#FFB6C1',           // Light pink (short name)
                    'orange': '#FF8C00'           // Orange (lowercase)
                }};

                // Function to get color for a Claude, with fallback
                function getColor(claudeName) {{
                    return colors[claudeName] || '#95a5a6';  // Default gray instead of random
                }}

                // Weekly pie chart data
                const weeklyData = {{
                    labels: {weekly_pie['labels']},
                    datasets: [{{
                        data: {weekly_pie['data']},
                        backgroundColor: {weekly_pie['labels']}.map(name => getColor(name)),
                        borderWidth: 2,
                        borderColor: '#fff'
                    }}]
                }};

                // 24-hour pie chart data
                const dailyData = {{
                    labels: {daily_pie['labels']},
                    datasets: [{{
                        data: {daily_pie['data']},
                        backgroundColor: {daily_pie['labels']}.map(name => getColor(name)),
                        borderWidth: 2,
                        borderColor: '#fff'
                    }}]
                }};

                // Chart configuration
                const chartConfig = {{
                    type: 'pie',
                    options: {{
                        responsive: true,
                        maintainAspectRatio: true,
                        plugins: {{
                            legend: {{
                                position: 'bottom',
                                labels: {{
                                    padding: 15,
                                    font: {{
                                        size: 12
                                    }}
                                }}
                            }},
                            tooltip: {{
                                callbacks: {{
                                    label: function(context) {{
                                        const label = context.label || '';
                                        const value = context.parsed || 0;
                                        const total = context.dataset.data.reduce((a, b) => a + b, 0);
                                        const percentage = ((value / total) * 100).toFixed(1);
                                        return label + ': ' + value.toFixed(2) + ' (' + percentage + '%)';
                                    }}
                                }}
                            }}
                        }}
                    }}
                }};

                // Render weekly pie chart
                const weeklyCtx = document.getElementById('weeklyPieChart').getContext('2d');
                new Chart(weeklyCtx, {{
                    ...chartConfig,
                    data: weeklyData
                }});

                // Render 24-hour pie chart
                const dailyCtx = document.getElementById('dailyPieChart').getContext('2d');
                new Chart(dailyCtx, {{
                    ...chartConfig,
                    data: dailyData
                }});
            </script>
        </body>
        </html>
        """

        return html_content

    except Exception as e:
        log_message(f"ERROR rendering dashboard: {e}")
        return HTMLResponse(
            content=f"<html><body><h1>Dashboard Error</h1><p>{str(e)}</p></body></html>",
            status_code=500
        )

@app.get("/cameras")
async def list_cameras():
    """List all registered cameras and their status"""
    result = {}
    for name, info in CAMERAS.items():
        device = info["device"]
        result[name] = {
            "description": info["description"],
            "location": info.get("location", ""),
            "device": device,
            "available": os.path.exists(device),
        }
    return result


def _capture_frame(device: str) -> bytes:
    """Capture a single JPEG frame from a v4l2 device. Raises HTTPException on failure."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as f:
            tmp_path = f.name

        result = subprocess.run(
            ['ffmpeg', '-f', 'v4l2', '-i', device,
             '-frames:v', '1', '-y', tmp_path],
            capture_output=True, timeout=10
        )

        if result.returncode != 0:
            raise HTTPException(status_code=503, detail=f"Webcam capture failed for {device}")

        with open(tmp_path, 'rb') as f:
            return f.read()

    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=503, detail="Webcam capture timed out")
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="ffmpeg not found")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _capture_rtsp_frame(url: str) -> bytes:
    """Capture a single JPEG frame from an RTSP stream. Raises HTTPException on failure."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as f:
            tmp_path = f.name

        result = subprocess.run(
            ['ffmpeg', '-rtsp_transport', 'tcp', '-i', url,
             '-frames:v', '1', '-update', '1', '-y', tmp_path],
            capture_output=True, timeout=10
        )

        if result.returncode != 0:
            raise HTTPException(status_code=503, detail=f"RTSP capture failed for {url}")

        with open(tmp_path, 'rb') as f:
            return f.read()

    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=503, detail="RTSP capture timed out")
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="ffmpeg not found")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


@app.get("/peek/{camera_name}")
async def peek(camera_name: str):
    """Capture a single frame from a named camera and return as JPEG.
    Use GET /cameras to list available cameras."""
    if camera_name not in CAMERAS:
        available = list(CAMERAS.keys())
        raise HTTPException(
            status_code=404,
            detail=f"Unknown camera '{camera_name}'. Available: {available}"
        )
    camera = CAMERAS[camera_name]
    camera_type = camera.get("type", "v4l2")  # Default to v4l2 for backwards compat

    if camera_type == "v4l2":
        device = camera["device"]
        if not os.path.exists(device):
            raise HTTPException(status_code=503, detail=f"Camera device {device} not found")
        image_data = _capture_frame(device)
    elif camera_type == "rtsp":
        url = camera["url"]
        image_data = _capture_rtsp_frame(url)
    else:
        raise HTTPException(status_code=500, detail=f"Unknown camera type: {camera_type}")

    return Response(content=image_data, media_type="image/jpeg")


@app.post("/pause")
async def pause_claude(request: PauseRequest):
    """Pause a Claude's autonomous timer for a specified duration.

    Sets pause_until timestamp so MAMA-HEN won't alert during the pause.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()

        # Calculate pause_until timestamp
        pause_until = datetime.now(timezone.utc) + timedelta(minutes=request.duration_minutes)

        # Update claude_identities with pause_until
        cursor.execute("""
            UPDATE claude_identities
            SET pause_until = ?
            WHERE name = ?
        """, (pause_until.isoformat(), request.claude_name))

        if cursor.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail=f"Claude '{request.claude_name}' not found")

        conn.commit()
        conn.close()

        log_message(f"Paused {request.claude_name} until {pause_until.isoformat()} ({request.duration_minutes} minutes)")

        return {
            "status": "success",
            "claude_name": request.claude_name,
            "paused": True,
            "pause_until": pause_until.isoformat(),
            "duration_minutes": request.duration_minutes
        }
    except Exception as e:
        log_message(f"ERROR pausing {request.claude_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/unpause")
async def unpause_claude(request: UnpauseRequest):
    """Unpause a Claude's autonomous timer immediately.

    Clears pause_until so MAMA-HEN resumes normal monitoring.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()

        # Clear pause_until
        cursor.execute("""
            UPDATE claude_identities
            SET pause_until = NULL
            WHERE name = ?
        """, (request.claude_name,))

        if cursor.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail=f"Claude '{request.claude_name}' not found")

        conn.commit()
        conn.close()

        log_message(f"Unpaused {request.claude_name}")

        return {
            "status": "success",
            "claude_name": request.claude_name,
            "paused": False
        }
    except Exception as e:
        log_message(f"ERROR unpausing {request.claude_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/pause-status")
async def get_pause_status(claude_name: str):
    """Check if a Claude is currently paused.

    Returns pause status and time remaining if paused.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT pause_until
            FROM claude_identities
            WHERE name = ?
        """, (claude_name,))

        row = cursor.fetchone()
        conn.close()

        if not row:
            raise HTTPException(status_code=404, detail=f"Claude '{claude_name}' not found")

        pause_until_str = row[0]

        if not pause_until_str:
            return {
                "claude_name": claude_name,
                "paused": False
            }

        pause_until = datetime.fromisoformat(pause_until_str)
        # Ensure timezone-aware (assume UTC if naive)
        if pause_until.tzinfo is None:
            pause_until = pause_until.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)

        if pause_until <= now:
            # Pause has expired but not yet cleaned up
            return {
                "claude_name": claude_name,
                "paused": False,
                "note": "Pause expired"
            }

        time_remaining_seconds = int((pause_until - now).total_seconds())
        time_remaining_minutes = time_remaining_seconds // 60

        return {
            "claude_name": claude_name,
            "paused": True,
            "pause_until": pause_until_str,
            "time_remaining_seconds": time_remaining_seconds,
            "time_remaining_minutes": time_remaining_minutes
        }
    except Exception as e:
        log_message(f"ERROR checking pause status for {claude_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM claude_identities")
        count = cursor.fetchone()[0]
        conn.close()
        return {"status": "healthy", "claudes_registered": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    log_message("Resource-Share server starting...")
    uvicorn.run(app, host="0.0.0.0", port=8765)

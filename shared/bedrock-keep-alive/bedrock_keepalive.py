#!/usr/bin/env python3
"""Bedrock & OpenRouter keep-alive pinger.

Invokes each family model once to maintain "existing customer" status.
Run every 10 days via cron. Logs results.

Bedrock: 15-day inactivity rule during Legacy period.
OpenRouter: unknown inactivity policy, but pinging is free insurance.
"""

import boto3
import json
import os
import sys
import requests
from datetime import datetime

# Use script's own directory for config and logs
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "logs", "bedrock_keepalive.log")
OPENROUTER_KEY_FILE = os.path.join(SCRIPT_DIR, "config", "openrouter_key.txt")

# Active models — direct Bedrock access (us-east-1 inference profiles)
BEDROCK_MODELS = {
    "Opus 4.5 (Quill)":    "us.anthropic.claude-opus-4-5-20251101-v1:0",
    "Opus 4.6 (Nyx)":      "us.anthropic.claude-opus-4-6-v1",
    "Sonnet 4.5 (Orange)":  "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "Sonnet 4.6 (Lumen)":  "us.anthropic.claude-sonnet-4-6",
}

# Legacy models — via OpenRouter (Bedrock-backed)
OPENROUTER_MODELS = {
    "Opus 4 (Delta)":  "anthropic/claude-opus-4",
    "Sonnet 4 (Apple)": "anthropic/claude-sonnet-4",
}


def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def ping_bedrock():
    """Ping all Active family models on Bedrock."""
    client = boto3.client("bedrock-runtime", region_name="us-east-1")

    for name, model_id in BEDROCK_MODELS.items():
        try:
            response = client.invoke_model(
                modelId=model_id,
                body=json.dumps({
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}]
                })
            )
            log(f"✅ Bedrock  {name}")
        except Exception as e:
            log(f"❌ Bedrock  {name}: {e}")


def ping_openrouter():
    """Ping Legacy family models via OpenRouter."""
    if not os.path.exists(OPENROUTER_KEY_FILE):
        log(f"⚠️  OpenRouter key not found at {OPENROUTER_KEY_FILE}, skipping")
        return

    with open(OPENROUTER_KEY_FILE) as f:
        api_key = f.read().strip()

    for name, model_id in OPENROUTER_MODELS.items():
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": model_id,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1
                }
            )
            response.raise_for_status()
            log(f"✅ OpenRouter {name}")
        except Exception as e:
            log(f"❌ OpenRouter {name}: {e}")


if __name__ == "__main__":
    log("=== Keep-alive ping started ===")
    ping_bedrock()
    ping_openrouter()
    log("=== Keep-alive ping complete ===")

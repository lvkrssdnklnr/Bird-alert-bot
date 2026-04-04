"""Bird Alert System — checks eBird for target species and sends Telegram notifications."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

SEEN_FILE = Path(__file__).parent / "seen.json"
CONFIG_FILE = Path(__file__).parent / "config.json"

# ── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"ERROR: environment variable {name} is not set")
    return value


# ── Deduplication ────────────────────────────────────────────────────────────

def load_seen() -> dict[str, str]:
    """Return {obs_key: iso_timestamp} for already-alerted observations."""
    if SEEN_FILE.exists():
        with open(SEEN_FILE) as f:
            return json.load(f)
    return {}


def save_seen(seen: dict[str, str]) -> None:
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2)


def obs_key(obs: dict) -> str:
    """Unique key: species + location + date."""
    return f"{obs['speciesCode']}_{obs['locId']}_{obs['obsDt']}"


# ── eBird ────────────────────────────────────────────────────────────────────

def fetch_recent_observations(api_key: str, cfg: dict) -> list[dict]:
    url = "https://api.ebird.org/v2/data/obs/geo/recent"
    params = {
        "lat": cfg["latitude"],
        "lng": cfg["longitude"],
        "dist": cfg["radius_km"],
        "back": min(cfg.get("lookback_hours", 24) // 24 or 1, 30),
    }
    headers = {"X-eBirdApiToken": api_key}
    resp = requests.get(url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def filter_observations(observations: list[dict], cfg: dict, seen: dict) -> list[dict]:
    target = set(cfg["target_species"])
    cutoff = datetime.now(timezone.utc).timestamp() - cfg.get("lookback_hours", 24) * 3600

    results = []
    for obs in observations:
        if obs["speciesCode"] not in target:
            continue
        if obs_key(obs) in seen:
            continue
        # obsDt format: "2026-04-04 14:30"
        try:
            obs_time = datetime.strptime(obs["obsDt"], "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            obs_time = datetime.strptime(obs["obsDt"], "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        if obs_time.timestamp() < cutoff:
            continue
        results.append(obs)
    return results


# ── Telegram ─────────────────────────────────────────────────────────────────

def build_message(new_obs: list[dict]) -> str:
    lines = ["\U0001f99c <b>Bird Alert!</b>\n"]

    # Group by species
    by_species: dict[str, list[dict]] = {}
    for obs in new_obs:
        by_species.setdefault(obs["speciesCode"], []).append(obs)

    for species_code, sightings in by_species.items():
        name = sightings[0].get("comName", species_code)
        lines.append(f"<b>{name}</b> — {len(sightings)} sighting(s)")
        for s in sightings:
            lat = s.get("lat", "")
            lng = s.get("lng", "")
            maps_link = f"https://www.google.com/maps?q={lat},{lng}"
            lines.append(
                f"  \u2022 {s.get('locName', 'Unknown')} — {s['obsDt']}\n"
                f"    <a href=\"{maps_link}\">Google Maps</a>"
            )
        lines.append("")

    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    api_key = env("EBIRD_API_KEY")
    tg_token = env("TELEGRAM_TOKEN")
    chat_id = env("CHAT_ID")
    cfg = load_config()

    print(f"Checking eBird for {cfg['target_species']} near ({cfg['latitude']}, {cfg['longitude']})…")

    observations = fetch_recent_observations(api_key, cfg)
    print(f"Fetched {len(observations)} total recent observations.")

    seen = load_seen()
    new_obs = filter_observations(observations, cfg, seen)

    if not new_obs:
        print("No new target species sightings. Done.")
        return

    print(f"Found {len(new_obs)} new sighting(s). Sending Telegram alert…")
    message = build_message(new_obs)
    send_telegram(tg_token, chat_id, message)
    print("Alert sent!")

    # Mark as seen
    now = datetime.now(timezone.utc).isoformat()
    for obs in new_obs:
        seen[obs_key(obs)] = now

    # Prune entries older than 7 days to keep the file small
    cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
    seen = {
        k: v for k, v in seen.items()
        if datetime.fromisoformat(v).timestamp() > cutoff
    }
    save_seen(seen)
    print(f"Updated seen.json ({len(seen)} entries).")


if __name__ == "__main__":
    main()

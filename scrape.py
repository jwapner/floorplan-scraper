import json
import os
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://www.thegunther.com/floorplans"
STATE_FILE = Path("state.json")


def fetch_floorplans():
    r = requests.get(URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    text = soup.get_text("\n", strip=True).splitlines()
    lines = [line.strip() for line in text if line.strip()]

    results = []
    current = None

    for line in lines:
        # catches names like A5, A7, S5, etc.
        if len(line) <= 6 and any(ch.isdigit() for ch in line) and any(ch.isalpha() for ch in line):
            current = {"floorplan": line, "availability": None, "available_on": None}
            results.append(current)
            continue

        if current:
            if "Available" in line and current["availability"] is None:
                current["availability"] = line
            if line.startswith("Available On:"):
                current["available_on"] = line.replace("Available On:", "").strip()

    cleaned = []
    seen = set()
    for row in results:
        key = (row["floorplan"], row["availability"], row["available_on"])
        if key not in seen:
            seen.add(key)
            cleaned.append(row)

    return cleaned


def load_previous():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return []


def save_current(data):
    STATE_FILE.write_text(json.dumps(data, indent=2))


def send_email(new_items):
    sender = os.environ["ALERT_EMAIL"]
    recipient = os.environ["ALERT_TO"]
    password = os.environ["ALERT_APP_PASSWORD"]

    body = "New floorplan availability found:\n\n"
    for item in new_items:
        body += f"- {item[0]} | {item[1]} | {item[2]}\n"

    msg = MIMEText(body)
    msg["Subject"] = "Gunther floorplan alert"
    msg["From"] = sender
    msg["To"] = recipient

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


def main():
    current = fetch_floorplans()
    previous = load_previous()

    prev_keys = {
        (x["floorplan"], x.get("availability"), x.get("available_on"))
        for x in previous
    }
    curr_keys = {
        (x["floorplan"], x.get("availability"), x.get("available_on"))
        for x in current
    }

    new_items = sorted(curr_keys - prev_keys)

    if new_items:
        print("New availability found:")
        for item in new_items:
            print(item)
        send_email(new_items)
    else:
        print("No changes found.")

    save_current(current)


if __name__ == "__main__":
    main()

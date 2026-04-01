import json
import os
import re
import smtplib
import random
import time
from email.mime.text import MIMEText
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

URL = "https://www.thegunther.com/floorplans"
STATE_FILE = Path("state.json")


def get_page_html(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/146.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 2000},
        )

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)

            page.wait_for_timeout(random.randint(3000, 7000))

            html = page.content()
            return html

        finally:
            browser.close()


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_floorplans(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = [normalize_whitespace(x) for x in text.splitlines() if normalize_whitespace(x)]

    results = []
    current = None

    plan_re = re.compile(r"^[A-Z]{1,5}\d{0,3}$")
    price_re = re.compile(r"^\$[\d,]+(?:/\s*month)?$", re.IGNORECASE)
    starting_price_re = re.compile(r"^Starting at \$[\d,]+$", re.IGNORECASE)
    available_on_re = re.compile(r"Available On:\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})")
    count_re = re.compile(r"^\d+\s+Available$", re.IGNORECASE)
    beds_re = re.compile(r"^\d+(?:\.\d+)?\s*Beds?$", re.IGNORECASE)
    baths_re = re.compile(r"^\d+(?:\.\d+)?\s*Baths?$", re.IGNORECASE)
    sqft_re = re.compile(r"^\d+\s*Sq\.?\s*Ft\.?$", re.IGNORECASE)

    for line in lines:
        if plan_re.match(line):
            if current and (
                current.get("availability_count")
                or current.get("available_on")
                or current.get("price")
            ):
                results.append(current)

            current = {
                "floorplan": line,
                "beds": None,
                "baths": None,
                "sqft": None,
                "availability_count": None,
                "price": None,
                "available_on": None,
            }
            continue

        if not current:
            continue

        if beds_re.match(line) and current["beds"] is None:
            current["beds"] = line
        elif baths_re.match(line) and current["baths"] is None:
            current["baths"] = line
        elif sqft_re.match(line) and current["sqft"] is None:
            current["sqft"] = line
        elif count_re.match(line) and current["availability_count"] is None:
            current["availability_count"] = line
        elif price_re.match(line) and current["price"] is None:
            current["price"] = line
        elif starting_price_re.match(line) and current["price"] is None:
            current["price"] = line
        else:
            m = available_on_re.search(line)
            if m and current["available_on"] is None:
                current["available_on"] = m.group(1)

    if current and (
        current.get("availability_count")
        or current.get("available_on")
        or current.get("price")
    ):
        results.append(current)

    seen = set()
    cleaned = []
    for row in results:
        key = (
            row["floorplan"],
            row["availability_count"],
            row["price"],
            row["available_on"],
        )
        if key not in seen:
            seen.add(key)
            cleaned.append(row)

    cleaned = [
        row for row in cleaned
        if row.get("availability_count") or row.get("available_on")
    ]

    cleaned.sort(key=lambda x: x["floorplan"])
    return cleaned


def load_state() -> dict:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())

        if isinstance(data, list):
            return {
                "floorplans": data,
                "changed_today": False,
            }

        if isinstance(data, dict):
            data.setdefault("floorplans", [])
            data.setdefault("changed_today", False)
            return data

    return {
        "floorplans": [],
        "changed_today": False,
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def format_row(row: dict) -> str:
    return (
        f'{row["floorplan"]} | '
        f'{row.get("availability_count") or "No count"} | '
        f'{row.get("price") or "No price"} | '
        f'Available On: {row.get("available_on") or "N/A"}'
    )


def send_email(subject: str, body: str) -> None:
    sender = os.getenv("ALERT_EMAIL")
    recipients = [x.strip() for x in os.getenv("ALERT_TO", "").split(",") if x.strip()]
    password = os.getenv("ALERT_APP_PASSWORD")

    if not sender or not recipients or not password:
        print("Email secrets not set. Skipping email.")
        print(body)
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, password)
        smtp.sendmail(sender, recipients, msg.as_string())


def check_floorplans() -> None:
    delay = random.randint(0, 1200)  # 0–20 minutes
    print(f"Sleeping for {delay} seconds before scraping...")
    time.sleep(delay)
    
    state = load_state()
    previous = state.get("floorplans", [])

    html = get_page_html(URL)
    current = parse_floorplans(html)

    if not previous:
        print("No existing floorplan state found. Saving initial snapshot without alert.")
        state["floorplans"] = current
        save_state(state)
        return

    prev_map = {row["floorplan"]: row for row in previous}
    curr_map = {row["floorplan"]: row for row in current}

    changes = []

    for floorplan, curr in curr_map.items():
        prev = prev_map.get(floorplan)
        if prev is None:
            changes.append(f"NEW: {format_row(curr)}")
            continue

        changed_fields = []
        for field in ["availability_count", "price", "available_on", "beds", "baths", "sqft"]:
            if prev.get(field) != curr.get(field):
                changed_fields.append(
                    f'{field}: "{prev.get(field)}" -> "{curr.get(field)}"'
                )

        if changed_fields:
            changes.append(
                f'CHANGED: {floorplan}\n  ' + "\n  ".join(changed_fields)
            )

    for floorplan, prev in prev_map.items():
        if floorplan not in curr_map:
            changes.append(f"REMOVED: {format_row(prev)}")

    if changes:
        body = "Gunther floorplan changes detected on /floorplans:\n\n" + "\n\n".join(changes)
        print(body)
        send_email("Gunther floorplan update", body)
        state["changed_today"] = True
    else:
        print("No changes found.")

    state["floorplans"] = current
    save_state(state)


def send_daily_no_changes_email() -> None:
    state = load_state()

    if state.get("changed_today", False):
        print("Changes occurred today. No daily no-changes email sent.")
    else:
        body = "No floorplan changes were detected today."
        print(body)
        send_email("Gunther daily update", body)

    state["changed_today"] = False
    save_state(state)


if __name__ == "__main__":
    mode = os.getenv("RUN_MODE", "check")

    if mode == "daily_summary":
        send_daily_no_changes_email()
    else:
        check_floorplans()

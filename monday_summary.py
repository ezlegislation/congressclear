import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import logging
import time
import sqlite3
from utils import load_config, fetch_bill_details, summarize_text_concise, DB_PATH, get_tweepy_client
from urllib.parse import urlparse, parse_qs

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

config = load_config()
CONGRESS_API_KEY = config.get("congress_api_key")
GEMINI_API_KEY = config.get("gemini_api_key")
TWITTER_HANDLE = config.get("twitter_handle")

if not all([CONGRESS_API_KEY, GEMINI_API_KEY, TWITTER_HANDLE]):
    logging.error("Missing required config values in config.json.")
    exit(1)

RSS_URL = "https://docs.house.gov/BillsThisWeek-RSS.xml"

def fetch_with_retries(url, max_retries=3, delay=5):
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=30)
            if response.status_code == 200:
                return response
            logging.warning(f"Attempt {attempt + 1}: Received status {response.status_code}")
        except requests.exceptions.Timeout:
            logging.warning(f"Attempt {attempt + 1}: Request timed out")
        time.sleep(delay)
    return None

def get_current_monday():
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    return monday.strftime("%b %d, %Y"), monday.strftime("%Y-%m-%d")  # "Mar 24, 2025", "2025-03-24"

def get_latest_entry_for_week(feed, week_start_date):
    week_entries = []
    for entry in feed.entries:
        url = entry.link
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        entry_date = query_params.get('date', [None])[0]
        if entry_date == week_start_date:
            week_entries.append(entry)
    if not week_entries:
        logging.info("No entries for this week.")
        return None
    latest = sorted(week_entries, key=lambda x: x.updated, reverse=True)[0]
    logging.info(f"Latest entry found: {latest.title}")
    return latest

def parse_rss_entry(entry):
    soup = BeautifulSoup(entry.content[0].value, "html.parser")
    classifications = {}
    current_classification = None
    for element in soup.find_all(["h3", "table"]):
        if element.name == "h3":
            current_classification = "suspension" if "suspension" in element.text.lower() else "rule"
            classifications[current_classification] = []
        elif element.name == "table" and current_classification:
            for row in element.find_all("tr", class_="floorItem"):
                cells = row.find_all("td")
                if len(cells) >= 2:
                    bill_number = cells[0].text.strip()
                    classifications[current_classification].append(bill_number)
    return classifications

def generate_summary(bill_details):
    with open("prompts/monday_summarize.txt", "r") as f:
        prompt = f.read()
    bill_text = bill_details["text"]
    bill_title = bill_details["title"]
    status = bill_details["status"]
    congress = bill_details["congress"]
    bill_id = bill_details["bill_id"]
    bill_type = bill_id.split('.')[0].lower().replace("h.r.", "hr").replace("s.", "s")
    bill_number = bill_id.split('.')[1]
    return summarize_text_concise(bill_text, bill_title, status, congress, bill_type, bill_number, prompt)

def format_bill(bill_details, summary, summary_post_id):
    bill_id = bill_details["bill_id"]
    title = bill_details["title"]
    sponsor_name = bill_details["sponsor_name"]
    sponsor_party_state = bill_details["sponsor_party_state"]
    introduced_date = bill_details["introduced_date"]
    congress = bill_details["formatted_congress"]
    link = bill_details["link"]

    bill_str = f"{bill_id} ({congress}) - {title}\n"
    bill_str += f"Introduced by {sponsor_name} [{sponsor_party_state}] on {introduced_date}\n\n"
    if summary:
        bill_str += f"{summary}\n\n"
    if summary_post_id:
        bill_str += f"To read a summary of the bill, visit: https://x.com/{TWITTER_HANDLE}/status/{summary_post_id}\n"
    else:
        bill_str += f"For more details, visit: {link}\n"
    return bill_str

def get_summary_post_id(bill_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT summary_post_id FROM bills WHERE bill_id = ?", (bill_id,))
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else None
    except sqlite3.Error as e:
        logging.error(f"Database error fetching summary_post_id for {bill_id}: {e}")
        return None

def main():
    formatted_week_start_date, week_start_date = get_current_monday()

    rss_response = fetch_with_retries(RSS_URL)
    if not rss_response:
        logging.error("Failed to fetch RSS feed.")
        print("Failed to fetch RSS feed.")
        return
    print("RSS feed fetched successfully.")

    feed = feedparser.parse(rss_response.content)
    print(f"Number of entries in feed: {len(feed.entries)}")

    latest_entry = get_latest_entry_for_week(feed, week_start_date)
    if not latest_entry:
        print("No latest entry found for the week.")
        return
    print(f"Latest entry: {latest_entry.title}")

    classifications = parse_rss_entry(latest_entry)
    print("Classifications:", classifications)

    output = f"Bills To Be Considered This Week on the House Floor - {formatted_week_start_date} (119th Congress)\n\n"
    valid_categories = [(cls, bills) for cls, bills in classifications.items() if any(bills)]
    
    for i, (classification, bills) in enumerate(valid_categories):
        filtered_bills = [bill for bill in bills if bill]
        if classification == "suspension":
            output += "Items under suspension of rules:\n\n"
        else:
            output += "Items pursuant to a rule:\n\n"
        for j, bill_id in enumerate(filtered_bills):
            print(f"Processing bill: {bill_id}")
            bill_details = fetch_bill_details(bill_id, CONGRESS_API_KEY)
            if not bill_details:
                print(f"Failed to fetch details for {bill_id}")
                continue
            summary = generate_summary(bill_details)
            summary_post_id = get_summary_post_id(bill_id)
            bill_str = format_bill(bill_details, summary, summary_post_id)
            output += bill_str
            if j < len(filtered_bills) - 1:  # Separator between bills
                output += "\n-\n\n"
        if i < len(valid_categories) - 1:  # Separator between categories
            output += "\n==\n\n"
    
    schedule_link = f"https://docs.house.gov/floor/Default.aspx?date={week_start_date}"
    output += f"\n==\n\nSchedule subject to change. For the latest schedule, visit: {schedule_link}\n"
    output += "Source: Congress.gov\n"

    print(output)
    logging.info(output)

    # Tweet the summary
    client = get_tweepy_client()
    try:
        tweet_response = client.create_tweet(text=output)
        logging.info(f"Tweeted summary successfully: Tweet ID {tweet_response.data['id']}")
        print(f"Tweeted summary: https://x.com/{TWITTER_HANDLE}/status/{tweet_response.data['id']}")
    except Exception as e:
        logging.error(f"Failed to tweet summary: {e}")
        print(f"Failed to tweet summary: {e}")

if __name__ == "__main__":
    main()
import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import logging
import time
from utils import load_config, fetch_bill_details, get_latest_summary_post_id, summarize_bill

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load configuration
config = load_config()
CONGRESS_API_KEY = config.get("congress_api_key")
GEMINI_API_KEY = config.get("gemini_api_key")
TWITTER_HANDLE = config.get("twitter_handle")

# Check for required config values
if not all([CONGRESS_API_KEY, GEMINI_API_KEY, TWITTER_HANDLE]):
    logging.error("Missing required config values in config.json. Check 'congress_api_key', 'gemini_api_key', and 'twitter_handle'.")
    exit(1)

# Constants
RSS_URL = "http://docs.house.gov/floor/RSS.aspx"

def fetch_with_retries(url, max_retries=3, delay=5):
    """Fetch a URL with retries and delays."""
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
    """Calculate the current week's Monday."""
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    return monday.strftime("%Y-%m-%d")

def get_latest_entry_for_week(feed, week_start_date):
    """Get the most recent RSS entry for the given week."""
    week_entries = [entry for entry in feed.entries if week_start_date in entry.title]
    if not week_entries:
        logging.info("No entries for this week. Congress may not be in session.")
        return None
    return sorted(week_entries, key=lambda x: x.updated, reverse=True)[0]

def parse_rss_entry(entry):
    """Parse the RSS entry to extract bills and their classifications."""
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

def generate_summary(bill_text):
    """Generate a 1-2 sentence summary using Gemini."""
    with open("prompts/monday_summarize.txt", "r") as f:
        prompt = f.read()
    return summarize_bill(bill_text, prompt, GEMINI_API_KEY)

def format_bill(bill_details, summary, summary_post_id):
    """Format the bill details, summary, and link."""
    bill_id = bill_details["bill_id"]
    title = bill_details["title"]
    sponsor = bill_details["sponsor"]
    introduced_date = bill_details["introduced_date"]
    congress = bill_details["congress"]
    status = bill_details["status"]

    # Construct the bill string
    bill_str = f"- Bill {status}: {bill_id} ({congress}) - {title}\n"
    bill_str += f"  Introduced by {sponsor} on {introduced_date}\n"

    # Add summary if available
    if summary:
        bill_str += f"  {summary}\n"

    # Add link
    if summary_post_id:
        bill_str += f"  To read a summary of the bill, visit: https://x.com/{TWITTER_HANDLE}/status/{summary_post_id}\n"
    else:
        congress_link = f"https://www.congress.gov/bill/{congress}/house-bill/{bill_id.split('.')[1]}"
        bill_str += f"  For more details, visit: {congress_link}\n"

    return bill_str

def main():
    """Main function to generate the Monday summary."""
    # Calculate current Monday
    week_start_date = get_current_monday()

    # Fetch RSS feed with retries
    rss_response = fetch_with_retries(RSS_URL)
    if not rss_response:
        logging.error("Failed to fetch RSS feed after retries.")
        return

    # Parse RSS feed
    feed = feedparser.parse(rss_response.content)
    latest_entry = get_latest_entry_for_week(feed, week_start_date)
    if not latest_entry:
        return

    # Parse bills from the latest entry
    classifications = parse_rss_entry(latest_entry)

    # Prepare output
    output = f"Bills To Be Considered This Week on the House Floor - {week_start_date} (119th Congress)\n\n"

    for classification, bills in classifications.items():
        output += f"{classification.capitalize()} Bills:\n\n"
        for bill_id in bills:
            # Get bill details from Congress API
            bill_details = fetch_bill_details(bill_id, CONGRESS_API_KEY)
            if not bill_details:
                continue

            # Get bill text and generate summary
            bill_text = bill_details["text"]
            summary = generate_summary(bill_text)

            # Get latest summary post ID
            summary_post_id = get_latest_summary_post_id(bill_id)

            # Format the bill
            bill_str = format_bill(bill_details, summary, summary_post_id)
            output += bill_str + "\n"

        output += "\n"  # Extra line break after each classification

    # Add schedule link
    schedule_link = f"https://docs.house.gov/floor/Default.aspx?date={week_start_date}"
    output += f"Schedule subject to change. For the latest schedule, visit: {schedule_link}\n"
    output += "Source: Congress.gov\n"

    # Log or print the output
    logging.info(output)

if __name__ == "__main__":
    main()
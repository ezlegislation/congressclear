import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import pytz
import utils
import logging

# Set up logging
logging.basicConfig(
    filename='/home/srrdx9mw12tk/congressclear/monday_summary.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Constants
known_prefixes = ["H.R.", "S.", "H.J. Res.", "S.J. Res.", "H. Con. Res.", "S. Con. Res.", "H. Res.", "S. Res."]
POST_TO_X = False  # Set to True to post to X; False for dry-run mode

def get_monday_date(offset=0):
    """Get the Monday date for the current or next week in Eastern Time."""
    eastern = pytz.timezone('America/New_York')
    today = datetime.now(eastern)
    monday = today - timedelta(days=today.weekday()) + timedelta(days=7 * offset)
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)

def parse_bill_id(bill_id):
    """Parse a bill ID (e.g., 'H.R. 123') into bill type and number."""
    for prefix in known_prefixes:
        if bill_id.startswith(prefix):
            number = bill_id[len(prefix):].strip()
            bill_type = {
                'H.R.': 'hr', 'S.': 's', 'H.J. Res.': 'hjres', 'S.J. Res.': 'sjres',
                'H. Con. Res.': 'hconres', 'S. Con. Res.': 'sconres',
                'H. Res.': 'hres', 'S. Res.': 'sres'
            }[prefix]
            return bill_type, number
    logging.error(f"Invalid bill ID format: {bill_id}")
    return None, None

def find_next_table(content, start):
    """Find the next <table class='floorItems'> after the given position in HTML content."""
    table_start = content.find('<table class="floorItems">', start)
    if table_start == -1:
        return None
    table_end = content.find('</table>', table_start)
    if table_end == -1:
        return None
    return content[table_start:table_end + len('</table>')]

def extract_bills(table):
    """Extract bill IDs and titles from an HTML table."""
    bills = []
    start = 0
    while True:
        tr_start = table.find('<tr class="floorItem">', start)
        if tr_start == -1:
            break
        tr_end = table.find('</tr>', tr_start)
        if tr_end == -1:
            break
        tr_content = table[tr_start:tr_end]
        legis_num_start = tr_content.find('<td class="legisNum">') + len('<td class="legisNum">')
        legis_num_end = tr_content.find('</td>', legis_num_start)
        bill_id = tr_content[legis_num_start:legis_num_end].strip()
        floor_text_start = tr_content.find('<td class="floorText">') + len('<td class="floorText">')
        floor_text_end = tr_content.find('</td>', floor_text_start)
        title = tr_content[floor_text_start:floor_text_end].strip()
        bills.append((bill_id, title))
        start = tr_end
    return bills

def fetch_and_extract_bills():
    """Fetch the RSS feed and extract bills for the current or next week."""
    url = "https://docs.house.gov/BillsThisWeek-RSS.xml"
    logging.info(f"Fetching RSS feed from {url}")
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as e:
        logging.error(f"Failed to fetch RSS feed: {e}")
        return False, None, None, None

    namespaces = {'atom': 'http://www.w3.org/2005/Atom'}
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as e:
        logging.error(f"Failed to parse RSS XML: {e}")
        return False, None, None, None

    for offset in [0, 1]:  # Check current week, then next week
        monday = get_monday_date(offset)
        target_week = f"Text of Bills to Be Considered the Week of {monday.strftime('%B %d, %Y')}"
        logging.info(f"Searching for schedule: {target_week}")
        entries = [
            (entry, datetime.fromisoformat(entry.find('atom:updated', namespaces).text.replace('-04:00', '')))
            for entry in root.findall('.//atom:entry', namespaces)
            if target_week in entry.find('atom:title', namespaces).text
        ]
        if entries:
            latest_entry = max(entries, key=lambda x: x[1])[0]
            logging.info(f"Using entry: {latest_entry.find('atom:title', namespaces).text}")
            content = latest_entry.find('atom:content', namespaces).text

            # Parse bills from content
            categories = {
                "suspension": '<h3>Items that may be considered under suspension of the rules</h3>',
                "rule": '<h3>Items that may be considered pursuant to a rule</h3>'
            }
            bills = {"suspension": [], "rule": []}
            for cat, header in categories.items():
                header_pos = content.find(header)
                if header_pos != -1:
                    table = find_next_table(content, header_pos)
                    if table:
                        bills[cat] = extract_bills(table)
                        logging.info(f"Found {len(bills[cat])} {cat} bills")
                    else:
                        logging.info(f"No table found for {cat} bills")
                else:
                    logging.info(f"No {cat} section found")
            if bills["suspension"] or bills["rule"]:
                return True, bills, monday, offset
    logging.info("No legislative business scheduled for current or next week")
    return False, None, None, None

def build_tweet_with_summaries(bills, monday):
    """Build the tweet text with bill summaries using the template."""
    congress = utils.get_current_congress()  # Dynamic Congress number (e.g., "119")
    congress_session = utils.format_congress(congress)  # Formatted (e.g., "119th")
    week_date = monday.strftime('%B %d, %Y')
    schedule_link = f"http://docs.house.gov/floor/Default.aspx?date={monday.strftime('%Y-%m-%d')}"

    # Process suspension bills
    suspension_entries = []
    for bill_id, title in bills["suspension"]:
        bill_type, number = parse_bill_id(bill_id)
        if bill_type and number:
            bill_data = utils.fetch_bill_data(congress, bill_type, number)
            if bill_data:
                summary = utils.summarize_text(
                    bill_data['text'], title, bill_data['status'],
                    congress, bill_type, number
                )
                if summary:
                    summary = utils.clean_summary(summary)
                    logging.info(f"Generated summary for {bill_id}: {summary}")
                else:
                    summary = "Summary unavailable"
                    logging.warning(f"Failed to generate summary for {bill_id}")
                entry = (
                    f"- Bill {bill_data['status']}: {bill_data['formatted_bill_type']}{number} "
                    f"({bill_data['formatted_congress']}) - {title}\n"
                    f"  Introduced by {bill_data['sponsor_name']} [{bill_data['sponsor_party_state']}] "
                    f"on {bill_data['introduced_date']}\n"
                    f"  {summary}"
                )
                if bill_data.get('summary_post_id'):
                    entry += f"\n  To read a summary of the bill, visit: https://x.com/ezlegislation/status/{bill_data['summary_post_id']}"
                suspension_entries.append(entry)
            else:
                logging.warning(f"Failed to fetch data for {bill_id}")
                suspension_entries.append(f"- {bill_id}: {title}\n  Summary unavailable")
        else:
            suspension_entries.append(f"- {bill_id}: {title}\n  Summary unavailable")

    # Process rule bills
    rule_entries = []
    for bill_id, title in bills["rule"]:
        bill_type, number = parse_bill_id(bill_id)
        if bill_type and number:
            bill_data = utils.fetch_bill_data(congress, bill_type, number)
            if bill_data:
                summary = utils.summarize_text(
                    bill_data['text'], title, bill_data['status'],
                    congress, bill_type, number
                )
                if summary:
                    summary = utils.clean_summary(summary)
                    logging.info(f"Generated summary for {bill_id}: {summary}")
                else:
                    summary = "Summary unavailable"
                    logging.warning(f"Failed to generate summary for {bill_id}")
                entry = (
                    f"- Bill {bill_data['status']}: {bill_data['formatted_bill_type']}{number} "
                    f"({bill_data['formatted_congress']}) - {title}\n"
                    f"  Introduced by {bill_data['sponsor_name']} [{bill_data['sponsor_party_state']}] "
                    f"on {bill_data['introduced_date']}\n"
                    f"  {summary}"
                )
                if bill_data.get('summary_post_id'):
                    entry += f"\n  To read a summary of the bill, visit: https://x.com/ezlegislation/status/{bill_data['summary_post_id']}"
                rule_entries.append(entry)
            else:
                logging.warning(f"Failed to fetch data for {bill_id}")
                rule_entries.append(f"- {bill_id}: {title}\n  Summary unavailable")
        else:
            rule_entries.append(f"- {bill_id}: {title}\n  Summary unavailable")

    # Format sections
    suspension_section = "\n\n".join(suspension_entries) if suspension_entries else "No suspension bills scheduled."
    rule_section = "\n\n".join(rule_entries) if rule_entries else "No rule bills scheduled."

    # Load and populate template
    template = utils.load_template('monday_post.txt')
    if not template:
        logging.error("Failed to load monday_post.txt template")
        return None

    try:
        tweet_text = template.format(
            week_date=week_date,
            congress_session=congress_session,
            suspension_section=suspension_section,
            rule_section=rule_section,
            schedule_link=schedule_link
        )
        logging.info("Tweet text generated successfully")
        return tweet_text
    except KeyError as e:
        logging.error(f"Template formatting error: missing key {e}")
        return None

if __name__ == "__main__":
    """Main execution block."""
    success, bills, monday, offset = fetch_and_extract_bills()
    if success:
        tweet_text = build_tweet_with_summaries(bills, monday)
        if tweet_text:
            print("Simulated Tweet:\n", tweet_text)
            if POST_TO_X:
                try:
                    client = utils.get_tweepy_client()
                    client.create_tweet(text=tweet_text)
                    logging.info("Tweet posted successfully")
                except Exception as e:
                    logging.error(f"Failed to post tweet: {e}")
            else:
                logging.info("Dry-run mode: Tweet not posted")
        else:
            logging.error("Failed to generate tweet text")
    else:
        logging.info("No schedule found—nothing to post")
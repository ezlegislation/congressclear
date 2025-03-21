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
    format='%(asctime)s - %(levelname)s - %(message)s',
    force=True
)

# Constants
known_prefixes = ["H.R.", "S.", "H.J. Res.", "S.J. Res.", "H. Con. Res.", "S. Con. Res.", "H. Res.", "S. Res."]
POST_TO_X = False

def get_monday_date(offset=0):
    """Get the Monday date for the current or next week in Eastern Time."""
    eastern = pytz.timezone('America/New_York')
    today = datetime.now(eastern)
    monday = today - timedelta(days=today.weekday()) + timedelta(days=7 * offset)
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)

def parse_bill_id(bill_id):
    """Parse a bill ID (e.g., 'H.R. 123') into bill type and number, cleaning HTML."""
    bill_id = ''.join(c for c in bill_id if c.isalnum() or c in '. ')
    print(f"Parsing bill ID: {bill_id}")
    for prefix in known_prefixes:
        if bill_id.startswith(prefix):
            number = bill_id[len(prefix):].strip()
            bill_type = {
                'H.R.': 'hr', 'S.': 's', 'H.J. Res.': 'hjres', 'S.J. Res.': 'sjres',
                'H. Con. Res.': 'hconres', 'S. Con. Res.': 'sconres',
                'H. Res.': 'hres', 'S. Res.': 'sres'
            }[prefix]
            print(f"Parsed: bill_type={bill_type}, number={number}")
            return bill_type, number
    logging.error(f"Invalid bill ID format: {bill_id}")
    print(f"Failed to parse bill ID: {bill_id}")
    return None, None

def find_next_table(content, start):
    """Find the next <table class='floorItems'> after the given position."""
    table_start = content.find('<table class="floorItems"', start)
    if table_start == -1:
        return None
    tag_end = content.find('>', table_start)
    if tag_end == -1:
        return None
    table_end = content.find('</table>', tag_end)
    if table_end == -1:
        return None
    return content[table_start:table_end + len('</table>')]

def extract_bills(table):
    """Extract bill IDs and titles from an HTML table, stripping HTML."""
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
        raw_bill_id = tr_content[legis_num_start:legis_num_end].strip()
        bill_id = ''
        in_tag = False
        for char in raw_bill_id:
            if char == '<':
                in_tag = True
            elif char == '>':
                in_tag = False
            elif not in_tag:
                bill_id += char
        bill_id = bill_id.strip()
        floor_text_start = tr_content.find('<td class="floorText">') + len('<td class="floorText">')
        floor_text_end = tr_content.find('</td>', floor_text_start)
        raw_title = tr_content[floor_text_start:floor_text_end].strip()
        title = ''
        in_tag = False
        for char in raw_title:
            if char == '<':
                in_tag = True
            elif char == '>':
                in_tag = False
            elif not in_tag:
                title += char
        title = title.strip()
        print(f"Extracted bill: ID={bill_id}, Title={title}")
        bills.append((bill_id, title))
        start = tr_end
    return bills

def fetch_and_extract_bills():
    """Fetch the RSS feed and extract bills with categories for the current or next week."""
    print("Entering fetch_and_extract_bills")
    url = "https://docs.house.gov/BillsThisWeek-RSS.xml"
    logging.info(f"Fetching RSS feed from {url}")
    print("Before requests.get")
    try:
        response = requests.get(url, timeout=30)
        print("After requests.get, status:", response.status_code)
        response.raise_for_status()
    except requests.RequestException as e:
        logging.error(f"Failed to fetch RSS feed: {e}")
        print("Failed to fetch RSS: ", str(e))
        return False, None, None, None

    print("Before parsing XML")
    namespaces = {'atom': 'http://www.w3.org/2005/Atom'}
    try:
        root = ET.fromstring(response.content)
        print("After parsing XML")
    except ET.ParseError as e:
        logging.error(f"Failed to parse RSS XML: {e}")
        print("Failed to parse XML: ", str(e))
        return False, None, None, None

    print("Starting offset loop")
    for offset in [0, 1]:
        monday = get_monday_date(offset)
        target_week = f"Text of Bills to Be Considered the Week of {monday.strftime('%b %d, %Y')}"
        logging.info(f"Searching for schedule: {target_week}")
        print(f"Checking offset {offset}: {target_week}")
        entries = [
            (entry, datetime.fromisoformat(entry.find('atom:updated', namespaces).text.replace('-04:00', '')))
            for entry in root.findall('.//atom:entry', namespaces)
            if target_week in entry.find('atom:title', namespaces).text
        ]
        if entries:
            latest_entry = max(entries, key=lambda x: x[1])[0]
            logging.info(f"Using entry: {latest_entry.find('atom:title', namespaces).text}")
            print("Found entries, using:", latest_entry.find('atom:title', namespaces).text)
            content = latest_entry.find('atom:content', namespaces).text
            print("Content snippet:", content[:200])

            categories = {
                "suspension": 'Items that may be considered under suspension of the rules',
                "rule": 'Items that may be considered pursuant to a rule'
            }
            all_bills = []
            for cat, header in categories.items():
                header_pos = content.find(header)
                if header_pos != -1:
                    table = find_next_table(content, header_pos)
                    if table:
                        bills = extract_bills(table)
                        for bill_id, title in bills:
                            all_bills.append((bill_id, title, cat))
                        logging.info(f"Found {len(bills)} {cat} bills")
                        print(f"Extracted {len(bills)} {cat} bills")
                    else:
                        logging.info(f"No table found for {cat} bills")
                        print(f"No table for {cat} bills")
                else:
                    logging.info(f"No {cat} section found")
                    print(f"No {cat} section")

            if all_bills:
                print(f"Total bills found: {len(all_bills)}")
                return True, all_bills, monday, offset
            else:
                print("No bills found in tables")
                return False, None, None, None
    logging.info("No legislative business scheduled for current or next week")
    print("No schedule found, returning False")
    return False, None, None, None

def build_tweet_with_summaries(bills, monday):
    """Build the tweet text with bill summaries using the template."""
    print("Entering build_tweet_with_summaries")
    congress = utils.get_current_congress()
    congress_session = utils.format_congress(congress)
    week_date = monday.strftime('%B %d, %Y')
    schedule_link = f"http://docs.house.gov/floor/Default.aspx?date={monday.strftime('%Y-%m-%d')}"

    # Collect bill data and texts for batch summary
    bill_texts = []
    bill_details = []
    for bill_id, title, category in bills:
        print(f"Processing bill: ID={bill_id}, Title={title}, Category={category}")
        bill_type, number = parse_bill_id(bill_id)
        if bill_type and number:
            bill_data = utils.fetch_bill_data(congress, bill_type, number)
            if bill_data:
                print(f"Bill data for {bill_id}: {bill_data}")
                bill_texts.append(f"Title: {title}, Text: {bill_data['text'] if bill_data['text'] else 'No text available'}")
                bill_details.append((bill_id, title, bill_data, category))
            else:
                logging.warning(f"Failed to fetch data for {bill_id}")
                print(f"No bill data returned for {bill_id}")
                bill_details.append((bill_id, title, None, category))
        else:
            logging.warning(f"Invalid bill ID: {bill_id}")
            print(f"Invalid bill ID: {bill_id}")
            bill_details.append((bill_id, title, None, category))

    # Batch summarize with Gemini
    summaries = {}
    if bill_texts:
        prompt = utils.load_prompt('monday_summarize.txt')
        if prompt:
            formatted_prompt = prompt.format(bill_texts="\n".join(bill_texts))
            print(f"Full prompt sent to Gemini:\n{formatted_prompt}")
            summary_response = utils.summarize_text(formatted_prompt, "", "", "", "", "")
            print(f"Gemini raw response:\n{summary_response}")
            if summary_response and summary_response != "Summary unavailable due to insufficient data":
                lines = summary_response.split('\n')
                current_bill = None
                for line in lines:
                    line = line.strip()
                    for bill_id, _, _, _ in bill_details:
                        if line.startswith(bill_id):
                            current_bill = bill_id
                            summaries[current_bill] = line[len(bill_id):].strip().lstrip(' -')
                            break
                    elif current_bill and line:
                        summaries[current_bill] += f"\n{line}"
                print(f"Parsed summaries: {summaries}")
            else:
                logging.warning("Gemini returned no valid summary")
                print("Gemini response invalid or empty")
        else:
            logging.error("Failed to load monday_summarize.txt")
            print("Prompt file monday_summarize.txt not found")

    # Build tweet sections
    suspension_entries = []
    rule_entries = []
    for bill_id, title, bill_data, category in bill_details:
        print(f"Building entry for {bill_id}")
        if bill_data:
            entry = (
                f"- Bill {bill_data['status']}: {bill_data['formatted_bill_type']}{bill_data['number']} "
                f"({bill_data['formatted_congress']})\n  {title}\n"
                f"  Introduced by {bill_data['sponsor_name']} [{bill_data['sponsor_party_state']}] "
                f"on {bill_data['introduced_date']}"
            )
            summary = summaries.get(bill_id, "")
            if summary:
                entry += f"\n  {summary}"
            if bill_data.get('summary_post_id'):
                entry += f"\n  To read a summary of the bill, visit: https://x.com/ezlegislation/status/{bill_data['summary_post_id']}"
            else:
                entry += f"\n  For more details, visit: {bill_data['link']}"
        else:
            bill_type, number = parse_bill_id(bill_id)
            entry = (
                f"- {bill_id}\n  {title}\n  Summary unavailable"
            )
            if bill_type and number:
                entry += f"\n  For more details, visit: https://www.congress.gov/bill/{congress}th-congress/{bill_type}-bill/{number}"
            else:
                entry += "\n  For more details, visit: https://www.congress.gov"

        print(f"Entry for {bill_id}:\n{entry}")
        if category == "suspension":
            suspension_entries.append(entry)
        else:
            rule_entries.append(entry)

    suspension_section = "\n\n".join(suspension_entries) if suspension_entries else "No suspension bills scheduled."
    rule_section = "\n\n".join(rule_entries) if rule_entries else "No rule bills scheduled."
    print(f"Suspension entries: {len(suspension_entries)}, Rule entries: {len(rule_entries)}")
    print(f"Suspension section:\n{suspension_section}")
    print(f"Rule section:\n{rule_section}")

    template = utils.load_template('monday_post.txt')
    if not template:
        logging.error("Failed to load monday_post.txt template")
        print("Template file monday_post.txt not found")
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
        print("Tweet generated")
        return tweet_text
    except KeyError as e:
        logging.error(f"Template formatting error: missing key {e}")
        print(f"Template error: missing key {e}")
        return None

if __name__ == "__main__":
    print("Entering main block")
    print("Before fetch_and_extract_bills")
    success, bills, monday, offset = fetch_and_extract_bills()
    print("After fetch_and_extract_bills, success=", success)
    if success:
        print("Inside if success block")
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
                    print(f"Failed to post tweet: {e}")
            else:
                logging.info("Dry-run mode: Tweet not posted")
        else:
            logging.error("Failed to generate tweet text")
            print("Failed to generate tweet text")
    else:
        logging.info("No schedule found—nothing to post")
        print("No schedule found—nothing to post")
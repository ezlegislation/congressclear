import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import pytz
import utils

# List of valid bill prefixes and their corresponding API bill types
known_prefixes = ["H.R.", "S.", "H.J. Res.", "S.J. Res.", "H. Con. Res.", "S. Con. Res.", "H. Res.", "S. Res."]
prefix_to_type = {
    "H.R.": "hr",
    "S.": "s",
    "H.J. Res.": "hjres",
    "S.J. Res.": "sjres",
    "H. Con. Res.": "hconres",
    "S. Con. Res.": "sconres",
    "H. Res.": "hres",
    "S. Res.": "sres",
}

def get_monday_date(offset=0):
    """Get this week's or next week's Monday in Eastern Time."""
    eastern = pytz.timezone('America/New_York')
    today = datetime.now(eastern)
    monday = today - timedelta(days=today.weekday()) + timedelta(days=7 * offset)
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)

def fetch_and_extract_bills():
    url = "https://docs.house.gov/BillsThisWeek-RSS.xml"
    print(f"Attempting to fetch RSS from {url}")
    response = requests.get(url, timeout=30)
    if response.status_code != 200:
        print(f"Failed to load RSS: Status code {response.status_code}")
        return False, None, None, None
    
    print("Successfully loaded RSS feed!")
    print(f"Content length: {len(response.content)} bytes")
    
    namespaces = {'atom': 'http://www.w3.org/2005/Atom'}
    root = ET.fromstring(response.content)
    
    for offset in [0, 1]:
        monday = get_monday_date(offset)
        target_week = f"Text of Bills to Be Considered the Week of {monday.strftime('%b %d, %Y')}"
        print(f"Checking for: {target_week}")
        
        latest_entry = None
        latest_time = None
        for entry in root.findall('.//atom:entry', namespaces):
            title = entry.find('atom:title', namespaces).text
            if target_week in title:
                updated = datetime.fromisoformat(entry.find('atom:updated', namespaces).text.replace('-04:00', ''))
                if not latest_time or updated > latest_time:
                    latest_time = updated
                    latest_entry = entry
        
        if latest_entry:
            print(f"Using latest entry: {latest_entry.find('atom:title', namespaces).text}")
            content = latest_entry.find('atom:content', namespaces).text
            sub_root = ET.fromstring(f"<root>{content}</root>")
            bills = {}
            last_h3 = None
            for elem in sub_root.iter():
                if elem.tag == 'h3':
                    last_h3 = elem.text
                elif elem.tag == 'table' and elem.get('class') == 'floorItems':
                    if last_h3:
                        category = last_h3
                        print(f"Processing table with category: {category}")
                        for tr in elem.findall('.//tr[@class="floorItem"]'):
                            legis_num = tr.find('.//td[@class="legisNum"]')
                            if legis_num is None:
                                continue
                            bill_id = legis_num.text.strip()
                            if not bill_id or not any(bill_id.startswith(prefix) for prefix in known_prefixes):
                                print(f"Skipping invalid bill_id: '{bill_id}'")
                                continue
                            title = tr.find('.//td[@class="floorText"]').text.strip()
                            suspension = "suspension of the rules" in category.lower()
                            bills[bill_id] = {'title': title, 'suspension': suspension}
            
            if bills:
                print(f"Extracted {len(bills)} unique bills:")
                for bill_id, data in bills.items():
                    print(f"{bill_id}: {data['title']} ({'Suspension' if data['suspension'] else 'Rule'})")
                tweet_content = build_tweet_with_summaries(bills, monday)
                print("\nSimulated Tweet:\n")
                print(tweet_content)
                return True, bills, monday, offset
    
    print("No legislative business scheduled for current or next week—exiting gracefully")
    return False, None, None, None

def build_tweet_with_summaries(bills, monday):
    congress_session = "119th"
    week_date = monday.strftime('%B %d, %Y')
    schedule_link = f"http://docs.house.gov/floor/Default.aspx?date={monday.strftime('%Y-%m-%d')}"
    
    # Load the template from templates/monday_post.txt
    template = utils.load_template('monday_post.txt')
    
    suspension_section = ""
    rule_section = ""
    for bill_id, data in bills.items():
        summary = generate_summary(bill_id, data['title'])
        entry = f"{bill_id}: {data['title']}\nSummary: {summary if summary else 'Unable to generate summary'}\n\n"
        if data['suspension']:
            suspension_section += entry
        else:
            rule_section += entry
    
    tweet = template.format(
        week_date=week_date,
        congress_session=congress_session,
        suspension_section=suspension_section.strip(),
        rule_section=rule_section.strip(),
        schedule_link=schedule_link
    )
    return tweet

def generate_summary(bill_id, title):
    congress = "119"
    for prefix in known_prefixes:
        if bill_id.startswith(prefix):
            number = bill_id[len(prefix):].strip()
            bill_type = prefix_to_type[prefix]
            break
    else:
        print(f"Unknown prefix for {bill_id}")
        return None
    
    bill_data = utils.fetch_bill_data(congress, bill_type, number)
    if not bill_data:
        print(f"Failed to fetch bill data for {bill_id}")
        return None
    
    bill_text = bill_data.get('text', '')
    bill_status = bill_data.get('status', '')
    
    try:
        summary = utils.summarize_text(bill_text, title, bill_status, congress, bill_type, number)
        return summary
    except Exception as e:
        print(f"Error generating summary for {bill_id}: {e}")
        return None

if __name__ == "__main__":
    success, bills, monday, offset = fetch_and_extract_bills()
    if success:
        print("Schedule found—ready to process (no posting yet)")
    else:
        print("No schedule found—nothing to post")
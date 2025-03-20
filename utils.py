import requests
import sqlite3
import json
import logging
import time
import re
import os
import tweepy
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET
import google.generativeai as genai
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
import hashlib

# Centralized logging setup function
def setup_logging(filename):
    """Set up logging with a specified filename."""
    logging.basicConfig(filename=filename, level=logging.INFO,
                        format='%(asctime)s %(levelname)s: %(message)s')

# Initial logging setup for utils.py
setup_logging('/home/srrdx9mw12tk/congressclear/scraper.log')

# Determine database path based on the presence of test_mode.txt
if os.path.exists('/home/srrdx9mw12tk/congressclear/test_mode.txt'):
    DB_PATH = '/home/srrdx9mw12tk/congressclear/test.db'  # Testing database
    logging.info("Test mode enabled: Using test.db")
else:
    DB_PATH = '/home/srrdx9mw12tk/congressclear/congress.db'  # Production database
    logging.info("Production mode: Using congress.db")

# Load config function
def load_config():
    """Load the configuration from config.json."""
    with open('/home/srrdx9mw12tk/congressclear/config.json', 'r') as f:
        return json.load(f)

config = load_config()
congress_api_key = config["congress_api_key"]
genai.configure(api_key=config["gemini_api_key"])
email_from = config["email"]
email_to = config["email_to"]
sendgrid_api_key = config["sendgrid_api_key"]

# Load hashtag configuration
with open('/home/srrdx9mw12tk/congressclear/hashtags.json', 'r') as f:
    hashtag_config = json.load(f)
HASHTAG_POOL = hashtag_config["subjects"]
STATE_HASHTAGS = hashtag_config["states"]
MANDATORY_HASHTAGS = hashtag_config["mandatory"]

# Legislation type mapping for Congress.gov URLs
LEGISLATION_TYPE_MAP = {
    'hr': 'house-bill',
    's': 'senate-bill',
    'sjres': 'senate-joint-resolution',
    'hjres': 'house-joint-resolution'
}

# Tweepy client initialization
def get_tweepy_client():
    """Initialize and return the Tweepy client."""
    config = load_config()
    max_attempts = 6
    for attempt in range(max_attempts):
        try:
            client = tweepy.Client(
                consumer_key=config["api_key"],
                consumer_secret=config["api_secret"],
                access_token=config["access_token"],
                access_token_secret=config["access_token_secret"]
            )
            test_response = client.create_tweet(text=f"Test tweet from @ezlegislation at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            client.delete_tweet(id=test_response.data['id'])
            return client
        except tweepy.TweepyException as e:
            if "429" in str(e):
                wait_time = 4 * 60 * 60  # 4 hours
                if attempt < max_attempts - 1:
                    logging.info(f"Rate limit hit (429) on attempt {attempt + 1}. Waiting {wait_time/3600:.2f} hours...")
                    time.sleep(wait_time)
                else:
                    logging.error("Max attempts reached with 429 error. Unable to initialize Tweepy client.")
                    raise
            else:
                logging.error(f"Tweepy init error: {e}")
                raise

# Tweet count management
tweet_count_file = "/home/srrdx9mw12tk/congressclear/tweet_count.json"

def load_tweet_count():
    """Load the tweet count and last reset date from the tweet_count.json file."""
    try:
        with open(tweet_count_file, 'r') as f:
            data = json.load(f)
            last_reset = datetime.fromisoformat(data.get('date', datetime.now().isoformat()))
            return data.get('count', 0), last_reset
    except:
        return 0, datetime.now()

def save_tweet_count(count, date):
    """Save the tweet count and reset date to the tweet_count.json file."""
    with open(tweet_count_file, 'w') as f:
        json.dump({'count': count, 'date': date.isoformat()}, f)

def handle_rate_limit(tweeted_count, last_reset, daily_limit):
    """Handle rate limit by waiting until the next reset period if limit is reached."""
    if tweeted_count >= daily_limit:
        wait_time = (last_reset + timedelta(days=1) - datetime.now()).total_seconds()
        if wait_time > 0:
            logging.info(f"Hit daily limit ({daily_limit}). Waiting {wait_time/3600:.2f} hours.")
            time.sleep(wait_time)
        tweeted_count = 0
        last_reset = datetime.now()
        save_tweet_count(tweeted_count, last_reset)
    return tweeted_count, last_reset

def get_ordinal_suffix(congress):
    congress = int(congress)
    if 11 <= congress % 100 <= 13:
        return "th"
    else:
        return {1: "st", 2: "nd", 3: "rd"}.get(congress % 10, "th")

def format_bill_type(bill_type):
    mapping = {'hr': 'H.R.', 's': 'S.', 'sjres': 'S.J.Res.', 'hjres': 'H.J.Res.'}
    return mapping.get(bill_type.lower(), bill_type.upper())

def format_congress(congress):
    suffix = get_ordinal_suffix(congress)
    return f"{congress}{suffix}"

def fetch_with_retries(url, max_retries=3):
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            logging.error(f"Attempt {attempt + 1}/{max_retries} failed for {url}: {e}")
            time.sleep(2 ** attempt)
    logging.error(f"Max retries reached for {url}")
    return None

def extract_all_text(element):
    text = element.text or ''
    for child in element:
        text += extract_all_text(child)
        if child.tail:
            text += child.tail
    return text.strip()

def clean_summary(summary):
    """Convert Gemini's \n\n and \n into literal line breaks for plain text output."""
    if summary == "Summary unavailable due to insufficient data":
        return summary
    if summary:
        # Replace \n\n with two literal line breaks, \n with one
        summary = summary.replace('\n\n', '\n\n').replace('\n', '\n')
        # Remove any trailing newlines
        summary = summary.rstrip('\n')
        return summary
    return summary

def fetch_bill_text(congress, bill_type, number):
    url = f"https://api.congress.gov/v3/bill/{congress}/{bill_type}/{number}/text?api_key={congress_api_key}&format=json"
    logging.info(f"Fetching text metadata: {url}")
    response = fetch_with_retries(url)
    if response:
        try:
            text_data = response.json().get('textVersions', [])
            logging.info(f"Available text versions: {len(text_data)}")
            for version in text_data:
                for fmt in version.get('formats', []):
                    if 'XML' in fmt.get('type', '').upper():
                        text_url = fmt.get('url')
                        logging.info(f"Fetching XML text from: {text_url}")
                        text_response = fetch_with_retries(text_url)
                        if text_response:
                            content_type = text_response.headers.get('Content-Type', '').lower()
                            if 'xml' not in content_type:
                                logging.warning(f"Non-XML response for {text_url}: {content_type}")
                                continue
                            try:
                                root = ET.fromstring(text_response.text)
                                readable_text = extract_all_text(root)
                                if readable_text:
                                    logging.info(f"Text extracted: {readable_text[:100]}...")
                                    return readable_text
                                else:
                                    logging.info("No readable text found in XML")
                            except ET.ParseError:
                                logging.error(f"XML parsing error for {text_url}")
                                continue
        except (json.JSONDecodeError, KeyError):
            logging.error(f"Error parsing text metadata for {url}")
    logging.info(f"No usable text for {congress}/{bill_type}/{number}")
    return None

def fetch_sponsor(congress, bill_type, number):
    url = f"https://api.congress.gov/v3/bill/{congress}/{bill_type}/{number}?api_key={congress_api_key}&format=json"
    response = fetch_with_retries(url)
    if response:
        try:
            sponsor = response.json()['bill'].get('sponsors', [{}])[0]
            name = f"{sponsor.get('firstName', '')} {sponsor.get('middleName', '') or ''} {sponsor.get('lastName', '')}".strip()
            if not name:
                logging.error(f"No valid sponsor name for {congress}/{bill_type}/{number}")
                return None
            return {'name': name, 'party_state': f"{sponsor.get('party', 'Unknown')}-{sponsor.get('state', 'Unknown')}"}
        except (json.JSONDecodeError, KeyError):
            logging.error(f"Error parsing sponsor data for {congress}/{bill_type}/{number}")
    return None

def fetch_bill_actions(congress, bill_type, number):
    url = f"https://api.congress.gov/v3/bill/{congress}/{bill_type}/{number}/actions?api_key={congress_api_key}&format=json"
    response = fetch_with_retries(url)
    if response:
        try:
            actions = response.json().get('actions', [])
            unique_actions = []
            seen = set()
            for action in actions:
                key = (action.get('actionDate'), action.get('text'))
                if key not in seen:
                    unique_actions.append(action)
                    seen.add(key)
            return unique_actions
        except json.JSONDecodeError:
            logging.error(f"Error parsing actions for {congress}/{bill_type}/{number}")
    return []

def fetch_crs_summary(congress, bill_type, number):
    url = f"https://api.congress.gov/v3/summaries/{congress}/{bill_type}/{number}?api_key={congress_api_key}&format=json"
    response = fetch_with_retries(url)
    if response:
        try:
            summaries = response.json().get('summaries', [])
            return summaries[0].get('text', '') if summaries else None
        except json.JSONDecodeError:
            logging.error(f"Error parsing CRS summary for {congress}/{bill_type}/{number}")
    return None

def fetch_amendments(congress, bill_type, number):
    url = f"https://api.congress.gov/v3/bill/{congress}/{bill_type}/{number}/amendments?api_key={congress_api_key}&format=json"
    response = fetch_with_retries(url)
    if response:
        try:
            return response.json().get('amendments', [])
        except json.JSONDecodeError:
            logging.error(f"Error parsing amendments for {congress}/{bill_type}/{number}")
    return []

def determine_status(actions):
    """Determine the status of a bill based on its actions, using flexible substring checks."""
    for action in sorted(actions, key=lambda x: x.get('actionDate', ''), reverse=True):
        text = action.get('text', '').lower()  # Case-insensitive matching
        if "became public law" in text or "signed by president" in text:
            return "Became Law"
        elif "vetoed" in text:
            return "Vetoed by President"
        elif "passed" in text and "senate" in text:
            return "Passed Senate"
        elif "passed" in text and "house" in text:
            return "Passed House"
        elif "introduced" in text:
            return "Introduced"
    return "Introduced"

def fetch_bill_data(congress, bill_type, number):
    actions = fetch_bill_actions(congress, bill_type, number)
    sponsor = fetch_sponsor(congress, bill_type, number)
    if not sponsor:
        logging.info(f"Skipping {congress}/{bill_type}/{number} - No sponsor data")
        add_skipped_bill(congress, bill_type, number, "No sponsor data")
        return None
    
    bill_details = fetch_with_retries(f"https://api.congress.gov/v3/bill/{congress}/{bill_type}/{number}?api_key={congress_api_key}&format=json")
    if not bill_details:
        logging.info(f"Skipping {congress}/{bill_type}/{number} - Failed to fetch details")
        add_skipped_bill(congress, bill_type, number, "Failed to fetch details")
        return None
    
    try:
        bill_info = bill_details.json()['bill']
        title = bill_info.get('title')
        number_str = bill_info.get('number')
        type_str = bill_info.get('type', '').lower()
        if not title or not number_str or not type_str:
            logging.info(f"Skipping {congress}/{bill_type}/{number} - Missing title, number, or type")
            add_skipped_bill(congress, bill_type, number, "Missing required fields")
            return None
    except (json.JSONDecodeError, KeyError):
        logging.error(f"Error parsing bill details for {congress}/{bill_type}/{number}")
        add_skipped_bill(congress, bill_type, number, "Parsing error")
        return None

    if type_str not in ['s', 'hr', 'sjres', 'hjres']:
        logging.info(f"Skipping {title} - Not a bill/joint resolution ({type_str})")
        return None

    bill_text = fetch_bill_text(congress, bill_type, number)
    amendments = fetch_amendments(congress, bill_type, number)
    actions_list = "\n".join([f"{format_date(a['actionDate'])} - {a['text']}" for a in sorted(actions, key=lambda x: x.get('actionDate', ''), reverse=True)][:3]) or "No actions available"
    amendments_list = "No recent amendments"
    if amendments:
        amendment_entries = []
        for a in amendments[-3:]:
            action_date = a.get('latestAction', {}).get('actionDate') if 'latestAction' in a else None
            amendment_entries.append(f"{format_date(action_date) if action_date else 'Unknown Date'} - Amendment {a['number']} added")
        amendments_list = "\n".join(amendment_entries)

    if bill_text:
        crs_summary = None
        logging.info(f"Bill text available for {congress}/{bill_type}/{number}, skipping CRS summary fetch")
    else:
        crs_summary = fetch_crs_summary(congress, bill_type, number)
        logging.info(f"Bill text not available for {congress}/{bill_type}/{number}, fetching CRS summary")

    return {
        'congress': congress,
        'formatted_congress': format_congress(congress),
        'bill_type': bill_type,
        'formatted_bill_type': format_bill_type(bill_type),
        'number': number,
        'title': title,
        'status': determine_status(actions),
        'text': bill_text,
        'sponsor_name': sponsor['name'],
        'sponsor_party_state': sponsor['party_state'],
        'introduced_date': format_date(min(a['actionDate'] for a in actions if 'actionDate' in a)) if actions else 'Unknown',
        'crs_summary': crs_summary,
        'actions_list': actions_list,
        'amendments_list': amendments_list,
        'link': format_bill_link(congress, bill_type, number),
        'amendments': amendments,
        'text_hash': hashlib.md5(bill_text.encode()).hexdigest() if bill_text else None,
        'actions': actions
    }

def format_date(date_str):
    if not date_str or not isinstance(date_str, str):
        return "Unknown"
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').strftime('%B %d, %Y')
    except ValueError:
        return date_str

def format_bill_link(congress, bill_type, number):
    formatted_type = LEGISLATION_TYPE_MAP.get(bill_type.lower(), bill_type.lower())
    return f"https://www.congress.gov/bill/{congress}th-congress/{formatted_type}/{number}"

def init_db():
    """Initialize the database with the necessary tables."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS bills 
                     (title TEXT, status TEXT, tweeted INTEGER, last_checked TEXT, text_hash TEXT, 
                      last_text TEXT, actions_json TEXT, summary TEXT, post_id TEXT, post_ids TEXT, 
                      summary_post_id TEXT, tweet_hash TEXT, amendments_json TEXT, 
                      PRIMARY KEY (title, status))''')
        c.execute('''CREATE TABLE IF NOT EXISTS skipped_bills 
                     (title TEXT, congress TEXT, bill_type TEXT, number TEXT, retry_count INTEGER, 
                      last_attempt TEXT, reason TEXT, PRIMARY KEY (title, congress, bill_type, number))''')
        conn.commit()

def check_bill(title, status):
    """Check if a bill has been tweeted and return its tweeted status, text hash, actions, and tweet hash."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT tweeted, text_hash, actions_json, amendments_json, summary_post_id, post_ids, tweet_hash FROM bills WHERE title=? AND status=?", (title, status))
        return c.fetchone()

def save_bill(data):
    """Save bill data to the database."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO bills 
            (title, status, tweeted, last_checked, text_hash, last_text, actions_json, summary, post_id, post_ids, summary_post_id, tweet_hash, amendments_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data['title'], data['status'], data.get('tweeted', 0), datetime.now().isoformat(),
            data.get('text_hash'), data.get('text'), json.dumps(data.get('actions', [])),
            data.get('summary'), data.get('post_id'), json.dumps(data.get('post_ids', [])),
            data.get('summary_post_id'), data.get('tweet_hash'), json.dumps(data.get('amendments', []))
        ))
        conn.commit()

def add_skipped_bill(congress, bill_type, number, reason):
    """Add a bill to the skipped_bills table with a retry count."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT retry_count FROM skipped_bills WHERE congress=? AND bill_type=? AND number=?", (congress, bill_type, number))
        result = c.fetchone()
        retry_count = (result[0] + 1) if result else 1
        if retry_count <= 3:
            c.execute("""
                INSERT OR REPLACE INTO skipped_bills 
                (title, congress, bill_type, number, retry_count, last_attempt, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (f"{bill_type.upper()}.{number}", congress, bill_type, number, retry_count, datetime.now().isoformat(), reason))
            logging.info(f"Added {congress}/{bill_type}/{number} to skipped_bills - Retry count: {retry_count}")
        conn.commit()

def retry_skipped_bills():
    """Retry processing bills that were previously skipped."""
    results = []
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT congress, bill_type, number, retry_count, reason FROM skipped_bills WHERE retry_count <= 3")
            skipped = c.fetchall()
            
            if not skipped:
                logging.info("No skipped bills to retry.")
                return results
            
            for congress, bill_type, number, retry_count, reason in skipped:
                logging.info(f"Retrying {congress}/{bill_type}/{number} (Attempt {retry_count + 1}/3) - Reason: {reason}")
                bill_data = fetch_bill_data(congress, bill_type, number)
                
                if bill_data:
                    logging.info(f"Successfully fetched {bill_data['title']} on retry")
                    save_bill(bill_data)
                    c.execute("DELETE FROM skipped_bills WHERE congress=? AND bill_type=? AND number=?", 
                              (congress, bill_type, number))
                    conn.commit()
                    logging.info(f"Removed {congress}/{bill_type}/{number} from skipped_bills after successful retry")
                    results.append(bill_data)
                else:
                    logging.info(f"Retry failed for {congress}/{bill_type}/{number}")
                    if retry_count + 1 > 3:
                        logging.info(f"Max retries exceeded for {congress}/{bill_type}/{number}")
                        send_email(
                            f"Max Retries Exceeded: {bill_type.upper()}.{number}",
                            f"Bill {congress}/{bill_type}/{number} failed after 3 attempts. Last reason: {reason}"
                        )
                    else:
                        c.execute("UPDATE skipped_bills SET retry_count=?, last_attempt=?, reason=? WHERE congress=? AND bill_type=? AND number=?", 
                                  (retry_count + 1, datetime.now().isoformat(), reason, congress, bill_type, number))
                        conn.commit()
    except Exception as e:
        logging.error(f"Error in retry_skipped_bills: {e}")
    return results

def load_tweet_template(filename):
    """Load the tweet template from the templates directory."""
    try:
        with open(f'/home/srrdx9mw12tk/congressclear/templates/{filename}', 'r') as f:
            return f.read()
    except FileNotFoundError:
        logging.error(f"Template {filename} not found")
        return ""

def load_prompt(filename):
    """Load the prompt template from the prompts directory."""
    try:
        with open(f'/home/srrdx9mw12tk/congressclear/prompts/{filename}', 'r') as f:
            return f.read()
    except FileNotFoundError:
        logging.error(f"Prompt {filename} not found")
        return ""

def get_template(bill_data):
    """Determine which template to use based on bill data, with debug logging."""
    tweeted = bill_data.get('tweeted', 0)
    logging.info(f"Template for {bill_data['title']} - Tweeted: {tweeted}, Status: {bill_data['status']}")
    if tweeted == 0:
        logging.info("Using new_bill.txt due to tweeted = 0")
        return "new_bill.txt"
    status = determine_status(bill_data.get('actions', []))  # Use actions directly
    if bill_data.get('amendments') and bill_data.get('text_hash'):
        return "amendment_summary.txt"
    elif bill_data.get('amendments'):
        return "amendment.txt"
    elif status != "Introduced":
        return "status_update.txt"
    elif bill_data.get('text'):
        return "new_bill.txt"
    return "no_text_available.txt"

def process_template(template, data, hashtags=None):
    """Process the template with single newlines between sections."""
    # Handle conditional {if crs_summary:...}else:...} logic
    pattern = r'\{if crs_summary:(.*?)\nelse:(.*?)\n\}'
    match = re.search(pattern, template, re.DOTALL)
    if match:
        if_part = match.group(1).strip()
        else_part = match.group(2).strip()
        if data.get('crs_summary'):
            replacement = if_part
        else:
            replacement = else_part
        template = re.sub(pattern, replacement, template, flags=re.DOTALL)
    
    # Format the template with data, preserving its original newlines
    try:
        tweet_text = template.format(**data)
    except KeyError as e:
        logging.error(f"Missing key in bill_data: {e} - Data: {data}")
        return template
    except Exception as e:
        logging.error(f"Template formatting failed: {e} - Data: {data}")
        return template

    # Append hashtags with a single newline if provided
    if hashtags:
        tweet_text += "\n" + hashtags
    
    logging.info(f"Processed tweet text: {repr(tweet_text)}")
    return tweet_text

def summarize_text(text, bill_title, status, congress, bill_type, number):
    """Summarize the bill text using Gemini AI with retries and validation."""
    if not text or len(text) < 30:
        logging.info(f"No summary for {bill_type.upper()}.{number}: Text too short or empty")
        return "Summary unavailable due to insufficient data"
    
    prompt_template = load_prompt('summarize.txt')
    prompt = prompt_template.format(bill_title=bill_title, bill_text=text)
    last_summary = None
    
    for attempt in range(5):
        try:
            logging.info(f"Attempting summary generation for {bill_type.upper()}.{number}, attempt {attempt + 1}")
            model = genai.GenerativeModel('gemini-1.5-flash')
            summary = model.generate_content(prompt).text.strip()
            logging.info(f"Raw AI summary: {repr(summary)}")
            last_summary = summary
            if validate_summary(summary):
                return summary
            logging.info(f"Summary validation failed, retrying attempt {attempt + 1}")
            time.sleep(30)
        except Exception as e:
            logging.error(f"Summary attempt {attempt + 1} failed for {bill_type.upper()}.{number}: {str(e)}")
            time.sleep(30)
    
    send_email(
        f"Summary Failure: {bill_title} ({congress}/{bill_type}/{number})",
        f"Bill: {bill_type.upper()}.{number}\nText Sample: {text[:100]}...\nLast Summary: {last_summary}\nError: 5 attempts failed"
    )
    return "Summary unavailable due to insufficient data"

def validate_summary(summary):
    """Validate the summary for quality using Gemini."""
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(
            f"Check summary for vagueness (e.g., 'unknown'): {summary}\nRespond 'valid' or 'retry'"
        )
        return response.text.strip().lower() == 'valid'
    except Exception as e:
        logging.error(f"Validation error: {e}")
        time.sleep(30)
        return False

def get_hashtags(text, sponsor_state):
    """Generate hashtags using Gemini AI."""
    if not text:
        return MANDATORY_HASHTAGS[:5]
    hashtag_list = [tag for sublist in HASHTAG_POOL.values() for tag in sublist] + list(STATE_HASHTAGS.values())
    prompt = f"Select up to 5 relevant hashtags from: {', '.join(hashtag_list)}\nExclude: {', '.join(MANDATORY_HASHTAGS)}\nText: {text}\nInclude state tags for {sponsor_state} if relevant. Return hashtags separated by spaces."
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        hashtags = model.generate_content(prompt).text.strip().split()
        return list(dict.fromkeys(MANDATORY_HASHTAGS + hashtags))[:5]
    except Exception as e:
        logging.error(f"Hashtag error: {e}")
        return MANDATORY_HASHTAGS[:5]

def send_email(subject, body):
    """Send an email using SendGrid."""
    message = Mail(from_email=email_from, to_emails=email_to, subject=subject, plain_text_content=body)
    try:
        sg = SendGridAPIClient(sendgrid_api_key)
        sg.send(message)
        logging.info(f"Email sent: {subject}")
    except Exception as e:
        logging.error(f"Failed to send email: {e}")

def summarize_amendment_diff(old_text, new_text, bill_title):
    """Summarize the amendment changes using Gemini AI with retries and validation."""
    if not old_text or not new_text:
        logging.info(f"No amendment summary for {bill_title}: Missing old or new text")
        return "Amendment summary unavailable due to missing text data"
    
    prompt_template = load_prompt('amendment_diff.txt')
    prompt = prompt_template.format(bill_title=bill_title, old_text=old_text, new_text=new_text)
    last_summary = None
    
    for attempt in range(5):
        try:
            logging.info(f"Attempting amendment summary generation for {bill_title}, attempt {attempt + 1}")
            model = genai.GenerativeModel('gemini-1.5-flash')
            summary = model.generate_content(prompt).text.strip()
            logging.info(f"Raw AI amendment summary: {repr(summary)}")
            last_summary = summary
            if validate_summary(summary):
                return summary
            logging.info(f"Amendment summary validation failed, retrying attempt {attempt + 1}")
            time.sleep(30)
        except Exception as e:
            logging.error(f"Amendment summary attempt {attempt + 1} failed for {bill_title}: {str(e)}")
            time.sleep(30)
    
    send_email(
        f"Amendment Summary Failure: {bill_title}",
        f"Bill: {bill_title}\nOld Text Sample: {old_text[:100]}...\nNew Text Sample: {new_text[:100]}...\nLast Summary: {last_summary}\nError: 5 attempts failed"
    )
    return "Amendment summary unavailable due to insufficient data"
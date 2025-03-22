import os
import logging
import utils
from datetime import datetime, timedelta
import feedparser
from bs4 import BeautifulSoup
import sqlite3
import json

utils.configure_logging(os.path.join(utils.BASE_PATH, 'monday_summary.log'))

def get_previous_monday():
    today = datetime.now().date()
    days_since_monday = today.weekday()
    if days_since_monday == 0:  # If today is Monday
        previous_monday = today - timedelta(days=7)
    else:
        previous_monday = today - timedelta(days=days_since_monday)
    return previous_monday

def get_latest_entry_for_week(entries, monday_date):
    for entry in entries:
        try:
            pub_date = datetime.strptime(entry.published, '%a, %d %b %Y %H:%M:%S %z')
            if pub_date.date() >= monday_date:
                return entry
        except ValueError:
            logging.error(f"Failed to parse date: {entry.published}")
            continue
    return None

def parse_rss_entry(entry):
    soup = BeautifulSoup(entry.content[0].value, 'html.parser')
    bills = []
    current_category = None
    for element in soup.find_all(['h3', 'td']):
        if element.name == 'h3':
            if 'Suspension' in element.text:
                current_category = 'Items under suspension of rules'
            elif 'Rule' in element.text:
                current_category = 'Items pursuant to a rule'
            else:
                current_category = None
        elif element.name == 'td' and current_category:
            bill_id = element.text.strip()
            if bill_id.startswith(('H.R.', 'S.', 'H.J.Res.', 'S.J.Res.')):
                attempt = 0
                max_attempts = 3
                bill_details = None
                while attempt < max_attempts:
                    bill_details = utils.fetch_bill_details(bill_id, utils.congress_api_key)
                    if bill_details and all(key in bill_details for key in ['title', 'sponsor_name', 'introduced_date', 'link']):
                        break
                    attempt += 1
                    logging.warning(f"Retry {attempt}/{max_attempts} for {bill_id} - incomplete or failed fetch")
                    time.sleep(30)  # 30-second delay
                if not bill_details or not all(key in bill_details for key in ['title', 'sponsor_name', 'introduced_date', 'link']):
                    logging.error(f"Failed to fetch complete details for {bill_id} after {max_attempts} attempts")
                    continue
                summary = utils.summarize_text_concise(bill_details['text'], bill_details['title'], bill_details['status'], bill_details['congress'], bill_details['bill_type'], bill_details['number'], utils.load_prompt('monday_summarize.txt'))
                bills.append((current_category, bill_id, bill_details, summary))
    return bills

def fetch_previous_tweets():
    with sqlite3.connect(utils.utils.DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT post_id FROM bills WHERE post_id IS NOT NULL AND summary_post_id IS NOT NULL")
        return [row[0] for row in c.fetchall()]

def save_to_db(bills, monday_date):
    with sqlite3.connect(utils.utils.DB_PATH) as conn:
        c = conn.cursor()
        for _, bill_id, bill_details, summary in bills:
            c.execute("""
                INSERT OR REPLACE INTO bills 
                (title, status, tweeted, last_checked, summary, bill_id, congress)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                bill_details['title'], 
                bill_details['status'], 
                1,  # tweeted
                datetime.now().isoformat(), 
                summary, 
                bill_id,
                bill_details['congress']
            ))
        conn.commit()

def main():
    previous_monday = get_previous_monday()
    logging.info(f"Generating summary for week starting {previous_monday}")

    rss_url = 'https://docs.house.gov/BillsThisWeek-RSS.xml'
    feed = utils.fetch_with_retries(rss_url)
    if not feed:
        logging.error("Failed to fetch RSS feed")
        return

    entries = feedparser.parse(feed.content).entries
    latest_entry = get_latest_entry_for_week(entries, previous_monday)
    if not latest_entry:
        logging.info("No latest entry found for this week")
        return

    bills = parse_rss_entry(latest_entry)
    if not bills:
        logging.info("No bills found in latest entry")
        return

    tweet_text = f"House Floor Summary for week of {previous_monday.strftime('%b %-d, %Y')}:\n\n"
    for category, bill_id, bill_details, summary in bills:
        tweet_text += f"[{bill_id}] ({bill_details['congress']}) - {bill_details['title']}\n"
        tweet_text += f"Introduced by {bill_details['sponsor_name']} [{bill_details['sponsor_party_state']}] on {bill_details['introduced_date']}\n"
        tweet_text += f"{summary}\n"
        tweet_text += f"For more details, visit: {bill_details['link']}\n\n"
        if category == 'Items under suspension of rules':
            tweet_text += "-\n"
        elif category == 'Items pursuant to a rule':
            tweet_text += "==\n"
    tweet_text += f"Schedule: https://docs.house.gov/floor/\nSource: Congress.gov"

    if len(tweet_text) > 280:
        logging.warning(f"Tweet exceeds 280 characters: {len(tweet_text)}. Truncating...")
        tweet_text = tweet_text[:277] + "..."

    formatted_tweet = utils.format_tweet(tweet_text, {}, is_summary=True)
    if formatted_tweet:
        logging.info(f"Tweet before posting: {formatted_tweet}")
        tweet_id = utils.post_tweet(formatted_tweet)
        logging.info(f"Tweeted summary: {formatted_tweet[:100]}... - ID: {tweet_id}")
        
        save_to_db(bills, previous_monday)
        
        previous_tweets = fetch_previous_tweets()
        for old_tweet_id in previous_tweets:
            try:
                client = utils.get_tweepy_client()
                client.delete_tweet(old_tweet_id)
                logging.info(f"Deleted previous tweet ID: {old_tweet_id}")
            except Exception as e:
                logging.error(f"Failed to delete tweet ID: {old_tweet_id}: {str(e)}")
    else:
        logging.error("Failed to format Monday summary tweet")

if __name__ == "__main__":
    main()
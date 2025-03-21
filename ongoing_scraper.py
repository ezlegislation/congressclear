import utils
import logging
import time
import os
from datetime import datetime, timedelta
import json
import hashlib

utils.setup_logging('/home/srrdx9mw12tk/congressclear/ongoing_scraper.log')
client = utils.get_tweepy_client()

def get_current_congress():
    """Determine the current Congress based on the date."""
    now = datetime.now()
    year = now.year
    if now.month < 3 or (now.month == 3 and now.day < 3):
        year -= 1  # If before March 3rd, use the previous term
    congress_start_year = 1789  # First Congress
    congress_number = 1 + ((year - congress_start_year) // 2)
    return congress_number

def OngoingScraper():
    if not os.path.exists("/home/srrdx9mw12tk/congressclear/retro_complete.txt"):
        logging.info("Retro mode not complete - exiting")
        exit(0)

    utils.init_db()
    congress = str(get_current_congress())
    while True:
        skipped_results = utils.retry_skipped_bills()
        
        tweeted_count, last_reset = utils.load_tweet_count()
        if datetime.now() - last_reset > timedelta(days=1):
            tweeted_count = 0
            utils.save_tweet_count(tweeted_count, datetime.now())

        url = f"https://api.congress.gov/v3/bill?api_key={utils.congress_api_key}&limit=250&congress={congress}&fromDateTime={(datetime.now() - timedelta(days=7)).isoformat()[:10]}T00:00:00Z"
        response = utils.fetch_with_retries(url)
        if not response:
            logging.error("Failed to fetch bill list - waiting 1 hour")
            time.sleep(3600)
            continue
        try:
            data = response.json()
            bills = data.get('bills', [])
            logging.info(f"Retrieved {len(bills)} bills")
        except Exception as e:
            logging.error(f"Error parsing bill list: {e} - Raw: {response.text[:100]}")
            time.sleep(3600)
            continue

        for bill in bills + skipped_results:
            if isinstance(bill, dict) and 'title' not in bill:  # Handle skipped_results
                bill_title = bill.get('title', 'Unknown')
                number = bill.get('number', 'Unknown')
                bill_type = bill.get('bill_type', '').lower()
                congress = bill.get('congress')
            else:
                bill_title = bill.get('title')
                number = bill.get('number')
                bill_type = bill.get('type', '').lower()
            
            if not bill_title or not number or not bill_type:
                logging.info(f"Skipping {bill_title or 'unknown'} - Missing required fields")
                utils.add_skipped_bill(congress, bill_type, number, "Missing required fields")
                continue

            if bill_type not in ['s', 'hr', 'sjres', 'hjres']:
                logging.info(f"Skipping {bill_title} - Not a bill/joint resolution ({bill_type})")
                continue

            bill_number = number.split('.')[1] if '.' in number else number
            bill_data = utils.fetch_bill_data(congress, bill_type, bill_number)
            if not bill_data:
                logging.info(f"Skipping {bill_title} - Invalid bill data")
                continue

            bill_id = f"{bill_type.upper()}.{bill_number}"
            check = utils.check_bill(bill_data['title'], bill_data['status'])
            tweeted = check[0] if check else 0
            old_text_hash = check[1] if check else None
            old_actions = json.loads(check[2]) if check and check[2] else []
            old_amendments = json.loads(check[3]) if check and check[3] else []
            text_hash = bill_data['text_hash']

            # Debug logging
            logging.info(f"{bill_id} - Tweeted from check: {tweeted}, Bill data tweeted: {bill_data.get('tweeted')}")

            daily_limit = 50 if datetime.now() < datetime(2025, 3, 22) else 17
            tweeted_count, last_reset = utils.handle_rate_limit(tweeted_count, last_reset, daily_limit)

            if tweeted:
                if old_text_hash != text_hash:
                    template_name = "amendment_summary.txt"
                    if bill_data['text']:
                        summary = utils.summarize_text(bill_data['text'], bill_data['title'], bill_data['status'], congress, bill_type, bill_number)
                        bill_data['summary'] = utils.clean_summary(summary) if summary else None
                    else:
                        bill_data['summary'] = None
                        template_name = "no_text_available.txt"
                elif len(bill_data['amendments']) > len(old_amendments):
                    template_name = "amendment.txt"
                    bill_data['summary'] = ""
                else:
                    logging.info(f"Skipping {bill_id} - No significant updates")
                    continue
            else:
                template_name = utils.get_template(bill_data)
                logging.info(f"{bill_id} - Template chosen: {template_name}")
                if bill_data['text']:
                    summary = utils.summarize_text(bill_data['text'], bill_data['title'], bill_data['status'], congress, bill_type, bill_number)
                    bill_data['summary'] = utils.clean_summary(summary) if summary else None
                    if not bill_data['summary']:
                        template_name = "no_text_available.txt"

            # Process tweet with hashtags in utils.process_template
            hashtags = " ".join(utils.get_hashtags(bill_data['text'] or bill_data['crs_summary'] or '', bill_data['sponsor_party_state'].split('-')[1]))
            tweet = utils.process_template(utils.load_tweet_template(template_name), bill_data, hashtags=hashtags)
            tweet_hash = hashlib.md5(tweet.encode()).hexdigest()

            if check and check[-1] == tweet_hash:
                logging.info(f"Skipping {bill_id} - Duplicate tweet hash")
                continue

            try:
                logging.info(f"Tweet before posting: {repr(tweet)}")
                tweet_response = client.create_tweet(text=tweet)
                tweeted_count += 1
                bill_data['tweeted'] = 1
                bill_data['post_id'] = str(tweet_response.data['id'])
                bill_data['post_ids'] = [str(tweet_response.data['id'])] if not check else json.loads(check[5]) + [str(tweet_response.data['id'])]
                bill_data['summary_post_id'] = str(tweet_response.data['id']) if 'summary' in template_name else (check[5][0] if check else None)
                bill_data['tweet_hash'] = tweet_hash
                utils.save_bill(bill_data)
                logging.info(f"Tweeted {bill_id}: {tweet[:100]}... - ID: {tweet_response.data['id']}")
                utils.save_tweet_count(tweeted_count, last_reset)
                # Adjust sleep time: 30 min before March 23, 2025, 60 min after
                sleep_time = 1800 if datetime.now() < datetime(2025, 3, 23) else 3600
                time.sleep(sleep_time)
            except tweepy.TweepyException as e:
                logging.error(f"Tweet error for {bill_id}: {e}")
                if "429" in str(e):
                    tweeted_count, last_reset = utils.handle_rate_limit(tweeted_count, last_reset, daily_limit)
                else:
                    time.sleep(60)

        logging.info("Cycle complete - waiting 1 hour")
        time.sleep(3600)

if __name__ == "__main__":
    OngoingScraper()
import utils
import logging
import time
import os
from datetime import datetime, timedelta
import json
import hashlib

# Set up logging as per the original configuration
utils.setup_logging('/home/srrdx9mw12tk/congressclear/ongoing_scraper.log')
client = utils.get_tweepy_client()

def OngoingScraper():
    # Check if retro mode is complete, exit if not
    if not os.path.exists("/home/srrdx9mw12tk/congressclear/retro_complete.txt"):
        logging.info("Retro mode not complete - exiting")
        exit(0)

    utils.init_db()
    congress = utils.get_current_congress()  # Use utils.get_current_congress()
    while True:
        skipped_results = utils.retry_skipped_bills()
        
        # Load and reset tweet count if more than a day has passed
        tweeted_count, last_reset = utils.load_tweet_count()
        if datetime.now() - last_reset > timedelta(days=1):
            tweeted_count = 0
            utils.save_tweet_count(tweeted_count, datetime.now())

        # Construct URL for fetching recent bills
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
            # Handle skipped_results dictionary format
            if isinstance(bill, dict) and 'title' not in bill:
                bill_title = bill.get('title', 'Unknown')
                number = bill.get('number', 'Unknown')
                bill_type = bill.get('bill_type', '').lower()
                congress = bill.get('congress')
            else:
                bill_title = bill.get('title')
                number = bill.get('number')
                bill_type = bill.get('type', '').lower()
            
            # Skip bills missing required fields
            if not bill_title or not number or not bill_type:
                logging.info(f"Skipping {bill_title or 'unknown'} - Missing required fields")
                utils.add_skipped_bill(congress, bill_type, number, "Missing required fields")
                continue

            # Filter out non-bill/joint resolution types
            if bill_type not in ['s', 'hr', 'sjres', 'hjres']:
                logging.info(f"Skipping {bill_title} - Not a bill/joint resolution ({bill_type})")
                continue

            bill_number = number.split('.')[1] if '.' in number else number
            bill_id = f"{bill_type.upper()}.{bill_number}"
            bill_data = utils.fetch_bill_details(bill_id, utils.congress_api_key)
            if not bill_data:
                logging.info(f"Skipping {bill_title} - Invalid bill data")
                continue

            # NEW: Validate tweet data before proceeding
            if not utils.validate_tweet_data(bill_data):
                logging.warning(f"Bill {bill_id} has incomplete data, marking for retry.")
                utils.add_skipped_bill(congress, bill_type, bill_number, "Incomplete data")
                continue

            # Check bill status in database
            check = utils.check_bill(bill_data['title'], bill_data['status'])
            tweeted = check[0] if check else 0
            old_text_hash = check[1] if check else None
            old_amendments = json.loads(check[3]) if check and check[3] else []
            text_hash = bill_data['text_hash']

            logging.info(f"{bill_id} - Tweeted from check: {tweeted}, Bill data tweeted: {bill_data.get('tweeted')}")

            # Handle tweet rate limits
            daily_limit = 50 if datetime.now() < datetime(2025, 3, 22) else 17
            tweeted_count, last_reset = utils.handle_rate_limit(tweeted_count, last_reset, daily_limit)

            if tweeted:
                if old_text_hash != text_hash and bill_data['text']:
                    template_name = "amendment_summary.txt"
                    summary = utils.summarize_text(bill_data['text'], bill_data['title'], bill_data['status'], congress, bill_type, bill_number)
                    bill_data['summary'] = utils.clean_summary(summary) if summary else None
                    old_text = check[5] if check and check[5] else ""  # last_text from DB
                    amendment_summary = utils.summarize_amendment_diff(old_text, bill_data['text'], bill_data['title'])
                    bill_data['amendment_summary'] = utils.clean_summary(amendment_summary) if amendment_summary else None
                elif len(bill_data['amendments']) > len(old_amendments):
                    template_name = "amendment.txt"
                    # Filter new amendments since last tweet
                    old_amendment_numbers = {a['number'] for a in old_amendments}
                    new_amendments = [a for a in bill_data['amendments'] if a['number'] not in old_amendment_numbers]
                    amendments_list = "\n".join(
                        f"{utils.format_date(a.get('latestAction', {}).get('actionDate', 'Unknown'))} - Amendment {a['number']} added"
                        for a in sorted(new_amendments, key=lambda x: x.get('latestAction', {}).get('actionDate', ''), reverse=True)
                    ) or "No new amendments"
                    bill_data['amendments_list'] = amendments_list
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

            # Set post_link to summary_post_id (latest summary tweet)
            bill_data['post_link'] = f"https://x.com/ezlegislation/status/{check[4]}" if check and check[4] else "No summary available yet"

            # NEW: Generate hashtags including party hashtag
            state = bill_data['sponsor_party_state'].split('-')[1] if '-' in bill_data['sponsor_party_state'] else ''
            gemini_hashtags = utils.get_hashtags(bill_data['text'] or bill_data['crs_summary'] or '', state)
            party_hashtag = utils.get_party_hashtag(bill_data['sponsor_party_state'])
            hashtags = " ".join([h for h in gemini_hashtags + [party_hashtag] if h])  # Filter out empty hashtags to avoid extra spaces

            tweet = utils.process_template(utils.load_tweet_template(template_name), bill_data, hashtags=hashtags)
            tweet_hash = hashlib.md5(tweet.encode()).hexdigest()

            # Skip if tweet is a duplicate
            if check and check[-1] == tweet_hash:
                logging.info(f"Skipping {bill_id} - Duplicate tweet hash")
                continue

            try:
                logging.info(f"Tweet before posting: {repr(tweet)}")
                tweet_response = client.create_tweet(text=tweet)
                tweeted_count += 1
                bill_data['tweeted'] = 1
                bill_data['post_id'] = str(tweet_response.data['id'])
                # Filter post_ids to summary tweets only
                post_ids = json.loads(check[5]) if check and check[5] else []
                if template_name in ["new_bill.txt", "amendment_summary.txt"]:
                    post_ids.append({"id": str(tweet_response.data['id']), "timestamp": datetime.now().isoformat()})
                    bill_data['summary_post_id'] = str(tweet_response.data['id'])
                bill_data['post_ids'] = post_ids
                bill_data['tweet_hash'] = tweet_hash
                utils.save_bill(bill_data)
                logging.info(f"Tweeted {bill_id}: {tweet[:100]}... - ID: {tweet_response.data['id']}")
                utils.save_tweet_count(tweeted_count, last_reset)
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
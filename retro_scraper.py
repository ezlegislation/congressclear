import utils
import logging
import time
import os
from datetime import datetime, timedelta
import json
import hashlib

# Clear existing handlers and set up logging
logging.getLogger().handlers = []
utils.setup_logging('/home/srrdx9mw12tk/congressclear/retro_scraper.log')

client = utils.get_tweepy_client()

cutoff_date = datetime.now() - timedelta(days=365)  # March 18, 2024
idle_cutoff_date = datetime.now() - timedelta(days=180)  # 6 months prior
retro_start_file = "/home/srrdx9mw12tk/congressclear/retro_start.txt"
progress_file = "/home/srrdx9mw12tk/congressclear/retro_progress.txt"

def RetroScraper():
      logging.info("Initializing database")
      utils.init_db()
      logging.info("Starting retry_skipped_bills")
      skipped_results = utils.retry_skipped_bills()
      logging.info(f"Retry results: {len(skipped_results)} bills to process")
      for bill_data in skipped_results:
          congress = bill_data['congress']
          bill_type = bill_data['bill_type']
          bill_number = bill_data['number']
          bill_id = f"{bill_type.upper()}.{bill_number}"
          logging.info(f"Processing retried bill: {bill_id} - {bill_data['title']}")
          check = utils.check_bill(bill_data['title'], bill_data['status'])
          tweeted = check[0] if check else 0
          old_text_hash = check[1] if check else None
          old_actions = json.loads(check[2]) if check and check[2] else []
          text_hash = bill_data['text_hash']

          logging.info(f"{bill_id} - Tweeted from check: {tweeted}, Bill data tweeted: {bill_data.get('tweeted')}")

          if tweeted and old_text_hash == text_hash and sorted(old_actions, key=lambda x: x['actionDate']) == sorted(bill_data['actions'], key=lambda x: x['actionDate']):
              logging.info(f"Skipping {bill_id} - No changes")
              continue

          if bill_data.get('text'):
              summary = utils.summarize_text(bill_data['text'], bill_data['title'], bill_data['status'], congress, bill_type, bill_number)
              if summary:
                  bill_data['summary'] = utils.clean_summary(summary)
                  template_name = utils.get_template(bill_data)
                  logging.info(f"{bill_id} - Text available and summarized, using {template_name}")
              else:
                  bill_data['summary'] = None
                  template_name = "no_text_available.txt"
                  logging.info(f"{bill_id} - Text available but summarization failed, using no_text_available.txt")
          else:
              bill_data['summary'] = None
              template_name = "no_text_available.txt"
              logging.info(f"{bill_id} - No text available, using no_text_available.txt")

          if not utils.validate_tweet_data(bill_data):
              logging.warning(f"Bill {bill_id} has incomplete data, marking for retry.")
              utils.add_skipped_bill(congress, bill_type, bill_number, "Incomplete data")
              continue

          state = bill_data['sponsor_party_state'].split('-')[1] if '-' in bill_data['sponsor_party_state'] else ''
          party_hashtag = utils.get_party_hashtag(bill_data['sponsor_party_state'])
          gemini_hashtags = utils.get_hashtags(bill_data['text'] or bill_data['crs_summary'] or '', state)
          hashtags = gemini_hashtags + " " + party_hashtag
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
              post_ids = json.loads(check[5]) if check and check[5] else []
              if template_name in ["new_bill.txt", "no_text_available.txt"]:
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
    
    try:
        with open(retro_start_file, 'r') as f:
            retro_start = datetime.fromisoformat(f.read().strip())
        retro_complete = datetime.now() > retro_start + timedelta(days=2)
    except FileNotFoundError:
        retro_start = datetime.now()
        retro_complete = False

    if not retro_complete:
        logging.info("Running retro mode - oldest to newest with sort=updateDate+asc")
        tweeted_count, last_reset = utils.load_tweet_count()
        if datetime.now() - last_reset > timedelta(days=1):
            tweeted_count = 0
            utils.save_tweet_count(tweeted_count, datetime.now())

        congresses = ["118", "119"]
        
        try:
            with open(progress_file, 'r') as f:
                progress = json.load(f)
                start_congress = progress.get("congress", "118")
                start_offset = progress.get("offset", 0)
                logging.info(f"Resuming from congress {start_congress}, offset {start_offset}")
        except FileNotFoundError:
            start_congress = "118"
            start_offset = 0
            logging.info(f"No progress file, starting from congress 118, offset {start_offset}")

        for congress in congresses:
            if congress < start_congress:
                continue
            offset = start_offset if congress == start_congress else 0

            while True:
                daily_limit = 50 if datetime.now() < datetime(2025, 3, 22) else 17
                tweeted_count, last_reset = utils.handle_rate_limit(tweeted_count, last_reset, daily_limit)

                url = f"https://api.congress.gov/v3/bill/{congress}?api_key={utils.congress_api_key}&limit=250&offset={offset}&sort=updateDate+asc&fromDateTime=2024-03-18T00:00:00Z"
                response = utils.fetch_with_retries(url)
                if not response:
                    logging.error("Failed to fetch bill list - skipping offset")
                    offset += 250
                    continue
                try:
                    data = response.json()
                    bills = data.get('bills', [])
                    logging.info(f"Retrieved {len(bills)} bills at offset {offset} for Congress {congress}")
                    for i, bill in enumerate(bills[:5]):
                        logging.info(f"Bill {i+1}: {bill.get('number')} - {bill.get('title', 'No title')} - Action: {bill.get('latestAction', {}).get('actionDate', 'No date')}")
                    if not bills:
                        logging.info(f"No more bills at offset {offset} for Congress {congress} - moving to next Congress")
                        break
                except Exception as e:
                    logging.error(f"Error parsing bill list: {e} - Raw: {response.text[:100]}")
                    offset += 250
                    continue

                for bill in bills:
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

                    action_date = datetime.strptime(bill.get('latestAction', {}).get('actionDate', '2024-01-01'), '%Y-%m-%d') if bill.get('latestAction') else cutoff_date
                    if action_date < cutoff_date:
                        logging.info(f"Skipping {bill_type.upper()}.{number} - Too old (action date: {action_date})")
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
                    text_hash = bill_data['text_hash']

                    # Debug logging
                    logging.info(f"{bill_id} - Tweeted from check: {tweeted}, Bill data tweeted: {bill_data.get('tweeted')}")

                    if tweeted and old_text_hash == text_hash and sorted(old_actions, key=lambda x: x['actionDate']) == sorted(bill_data['actions'], key=lambda x: x['actionDate']):
                        logging.info(f"Skipping {bill_id} - No changes")
                        continue
                    if action_date < idle_cutoff_date and bill_data['status'] == "Introduced":
                        logging.info(f"Skipping {bill_id} - Idle")
                        continue

                    # Add validate_tweet_data check
                    if not utils.validate_tweet_data(bill_data):
                        logging.warning(f"Bill {bill_id} has incomplete data, marking for retry.")
                        utils.add_skipped_bill(congress, bill_type, bill_number, "Incomplete data")
                        continue

                    # Template selection logic
                    if bill_data.get('text'):
                        # If text exists, attempt summarization and use the status-based template
                        summary = utils.summarize_text(bill_data['text'], bill_data['title'], bill_data['status'], congress, bill_type, bill_number)
                        if summary:
                            bill_data['summary'] = utils.clean_summary(summary)
                            template_name = utils.get_template(bill_data)  # Use status-based template
                            logging.info(f"{bill_id} - Text available and summarized, using {template_name}")
                        else:
                            bill_data['summary'] = None
                            template_name = "no_text_available.txt"  # Fallback if summarization fails
                            logging.info(f"{bill_id} - Text available but summarization failed, using no_text_available.txt")
                    else:
                        # If no text, use no_text_available.txt
                        bill_data['summary'] = None
                        template_name = "no_text_available.txt"
                        logging.info(f"{bill_id} - No text available, using no_text_available.txt")

                    # Generate hashtags, including party hashtag
                    state = bill_data['sponsor_party_state'].split('-')[1] if '-' in bill_data['sponsor_party_state'] else ''
                    party_hashtag = utils.get_party_hashtag(bill_data['sponsor_party_state'])
                    gemini_hashtags = utils.get_hashtags(bill_data['text'] or bill_data['crs_summary'] or '', state)
                    hashtags = gemini_hashtags + " " + party_hashtag
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
                        # Filter post_ids to summary tweets only
                        post_ids = json.loads(check[5]) if check and check[5] else []
                        if template_name in ["new_bill.txt", "no_text_available.txt"]:
                            post_ids.append({"id": str(tweet_response.data['id']), "timestamp": datetime.now().isoformat()})
                            bill_data['summary_post contund_id'] = str(tweet_response.data['id'])
                        bill_data['post_ids'] = post_ids
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

                with open(progress_file, 'w') as f:
                    json.dump({"congress": congress, "offset": offset}, f)
                offset += 250

            logging.info(f"Completed Congress {congress}")
            if congress == "118":
                start_offset = 0

        with open("/home/srrdx9mw12tk/congressclear/retro_complete.txt", 'w') as f:
            f.write(datetime.now().isoformat())
        logging.info("Retro mode complete - launching ongoing_scraper")
        os.system("python3 /home/srrdx9mw12tk/congressclear/ongoing_scraper.py &")

if __name__ == "__main__":
    RetroScraper()
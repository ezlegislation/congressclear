import os
import json
import logging
import os
import time
from datetime import datetime

import utils

utils.configure_logging(os.path.join(utils.BASE_PATH, 'ongoing_scraper.log'))

def process_bill(bill_data, tweeted_from_check=0):
    congress = bill_data.get('congress', '118')
    bill_type = bill_data.get('type', '').lower()
    number = bill_data.get('number', '')
    title = bill_data.get('title', '')
    formatted_bill_type = utils.format_bill_type(bill_type)
    bill_id = f"{formatted_bill_type}.{number}"
    tweeted = tweeted_from_check
    bill_data['tweeted'] = tweeted

    logging.info(f"{bill_id} - Tweeted from check: {tweeted}, Bill data tweeted: {bill_data.get('tweeted_status')}")

    if tweeted == 0:
        template_name = bill_data.get('template', 'new_bill.txt')
        status = bill_data.get('status', 'Introduced')
        logging.info(f"Template for {title} - Tweeted: {tweeted}, Status: {status}")
        logging.info(f"{bill_id} - Template chosen: {template_name}")

        if tweeted == 0 and template_name == 'new_bill.txt':
            logging.info(f"Using new_bill.txt due to tweeted = 0")
        elif tweeted > 0 and status == 'Introduced':
            logging.info(f"{bill_id} already tweeted with status {status}")
            return

        with open(os.path.join(utils.BASE_PATH, 'templates', template_name), 'r') as file:
            template = file.read()

        bill_text = bill_data.get('text', '')
        if bill_text:
            attempt = 0
            max_attempts = 5
            while attempt < max_attempts:
                attempt += 1
                logging.info(f"Attempting summary generation for {bill_id}, attempt {attempt}")
                summary = utils.summarize_text(bill_text, title, status, congress, bill_type, number)
                if summary and summary != "Summary unavailable due to insufficient data":
                    break
                if attempt < max_attempts:
                    time.sleep(30)
            if not summary or summary == "Summary unavailable due to insufficient data":
                if bill_text:
                    utils.send_email(f"Summary Failure: {bill_id}", f"Failed to generate summary for {bill_id} after {max_attempts} attempts despite available text.")
                template_name = 'no_text_available.txt'
                with open(os.path.join(utils.BASE_PATH, 'templates', template_name), 'r') as file:
                    template = file.read()
            bill_data['summary'] = summary
        else:
            template_name = 'no_text_available.txt'
            with open(os.path.join(utils.BASE_PATH, 'templates', template_name), 'r') as file:
                template = file.read()
            bill_data['summary'] = None

        tweet_text = utils.format_tweet(template, bill_data, bill_data['summary'])
        if tweet_text:
            logging.info(f"Tweet before posting: {tweet_text}")
            tweet_id = utils.post_tweet(tweet_text)
            logging.info(f"Tweeted {bill_id}: {tweet_text[:100]}... - ID: {tweet_id}")
            utils.save_bill(bill_data)
            time.sleep(1800)  # 30-minute delay
        else:
            logging.info(f"Skipped tweeting {bill_id} - formatting failed")
    else:
        text_hash = hashlib.md5(bill_data.get('text', '').encode()).hexdigest()
        stored_hash = bill_data.get('text_hash', '')
        if text_hash != stored_hash and bill_data.get('text'):
            template_name = 'amendment_summary.txt'
            logging.info(f"{bill_id} - Template chosen: {template_name}")
            with open(os.path.join(utils.BASE_PATH, 'templates', template_name), 'r') as file:
                template = file.read()
            
            summary = utils.summarize_text(bill_data['text'], title, bill_data.get('status', 'Introduced'), congress, bill_type, number)
            tweet_text = utils.format_tweet(template, bill_data, summary)
            if tweet_text:
                logging.info(f"Tweet before posting: {tweet_text}")
                tweet_id = utils.post_tweet(tweet_text)
                logging.info(f"Tweeted amendment for {bill_id}: {tweet_text[:100]}... - ID: {tweet_id}")
                bill_data['text_hash'] = text_hash
                utils.save_bill(bill_data)
                time.sleep(1800)  # 30-minute delay
            else:
                logging.info(f"Skipped tweeting amendment for {bill_id} - formatting failed")
        else:
            logging.info(f"Skipping {bill_id} - Idle")

def main():
    if not os.path.exists('retro_complete.txt'):
        logging.info("Retro scraper not complete - exiting")
        return
    
    logging.info("Running ongoing mode - newest first with sort=updateDate+desc")
    while True:
        url = f"https://api.congress.gov/v3/bill?sort=updateDate+desc&limit=250&api_key={utils.congress_api_key}"
        response = utils.fetch_with_retries(url)
        if response.status_code != 200:
            logging.error(f"Failed to fetch bills: {response.status_code}")
            time.sleep(3600)  # Retry in 1 hour
            continue

        data = response.json()
        bills = data.get('bills', [])
        if not bills:
            logging.info("No new bills found - waiting")
            time.sleep(3600)  # 1-hour retry
            continue

        for bill in bills:
            bill_data = utils.build_bill_data(bill)
            tweeted_from_check = utils.check_bill(bill_data['title'], bill_data.get('status', 'Introduced'))
            tweeted_from_check = tweeted_from_check[0] if tweeted_from_check else 0
            if tweeted_from_check == 0 or utils.is_amended(bill_data):
                bill_details = utils.fetch_bill_data(bill_data['congress'], bill_data['type'], bill_data['number'])
                if bill_details:
                    bill_data.update(bill_details)  # Merge fetched text and details

            process_bill(bill_data, tweeted_from_check)

if __name__ == "__main__":
    main()
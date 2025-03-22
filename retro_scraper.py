import os
import json
import logging
import os
import time
from datetime import datetime

import utils

utils.setup_logging(os.path.join(utils.BASE_PATH, 'retro_scraper.log'))

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
        logging.info(f"Skipping {bill_id} - Idle")

def main():
    logging.info("Running retro mode - oldest to newest with sort=updateDate+asc")

    progress_file = 'retro_progress.txt'
    if os.path.exists(progress_file):
        with open(progress_file, 'r') as f:
            progress = json.load(f)
            congress = progress.get('congress', 118)
            offset = progress.get('offset', 0)
    else:
        congress = 118
        offset = 0
        logging.info(f"No progress file, starting from congress {congress}, offset {offset}")

    limit = 250

    while True:
        url = f"https://api.congress.gov/v3/bill/{congress}?offset={offset}&limit={limit}&sort=updateDate+asc&api_key={utils.CONGRESS_API_KEY}"
        response = utils.fetch_with_retries(url)
        if response.status_code != 200:
            logging.error(f"Failed to fetch bills: {response.status_code}")
            break

        data = response.json()
        bills = data.get('bills', [])
        if not bills:
            if congress == 118:
                congress = 119
                offset = 0
                logging.info(f"Finished Congress 118, moving to Congress 119")
                with open(progress_file, 'w') as f:
                    json.dump({'congress': congress, 'offset': offset}, f)
                continue
            else:
                logging.info("No more bills to process")
                break

        logging.info(f"Retrieved {len(bills)} bills at offset {offset} for Congress {congress}")
        for i, bill in enumerate(bills, start=1):
            bill_data = utils.build_bill_data(bill, congress)
            logging.info(f"Bill {i}: {bill_data['title']} - Action: {bill_data['actions'].get('actionDate', 'N/A')}")

            if bill_data['type'] not in ['s', 'hr', 'sjres', 'hjres']:
                logging.info(f"Skipping {bill_data['title']} - Not a bill/joint resolution ({bill_data['type']})")
                continue

            tweeted_from_check = utils.check_bill(bill_data['title'], bill_data.get('status', 'Introduced'))
            tweeted_from_check = tweeted_from_check[0] if tweeted_from_check else 0
            if tweeted_from_check == 0:
                bill_details = utils.fetch_bill_data(congress, bill_data['type'], bill_data['number'])
                if bill_details:
                    bill_data.update(bill_details)  # Merge fetched text and details

            process_bill(bill_data, tweeted_from_check)

        offset += limit
        with open(progress_file, 'w') as f:
            json.dump({'congress': congress, 'offset': offset}, f)

    if os.path.exists(progress_file):
        os.remove(progress_file)
    with open('retro_complete.txt', 'w') as f:
        f.write("Retro scraping complete")
    logging.info("Retro scraping complete - launching ongoing_scraper")
    os.system(f"/bin/bash {os.path.join(utils.BASE_PATH, 'start_ongoing.sh')}")

if __name__ == "__main__":
    main()
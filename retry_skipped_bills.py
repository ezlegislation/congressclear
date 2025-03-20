import sqlite3
import logging
import time
import hashlib
from datetime import datetime
import utils  # Import the full utils module

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[logging.FileHandler('retry_skipped_bills.log', mode='a'), logging.StreamHandler()]
)

def retry_skipped_bills():
    logging.info("Starting retry_skipped_bills process")
    
    accepted_types = ['s', 'hr', 'sjres', 'hjres']  # Only retry bills and joint resolutions
    
    with sqlite3.connect('congress.db') as conn:
        c = conn.cursor()
        # Query using title and status to match utils.py schema
        c.execute('SELECT title, status, congress, bill_type, number FROM bills WHERE (summary IS NULL OR last_text IS NULL) AND tweeted = 0')
        skipped_bills = c.fetchall()

        if not skipped_bills:
            logging.info("No skipped bills to retry")
            return

        for bill in skipped_bills:
            title, status, congress, bill_type, number = bill
            bill_id = f"{bill_type.upper()}.{number}"
            
            if bill_type.lower() not in accepted_types:
                logging.info(f"Skipping retry for {bill_id} - not an accepted bill type")
                continue

            logging.info(f"Retrying bill {bill_id}")

            # Fetch full bill data instead of just text
            bill_data = utils.fetch_bill_data(congress, bill_type, number)
            if not bill_data:
                logging.info(f"Failed to fetch data for {bill_id}, keeping as skipped")
                continue

            if bill_data['text']:
                summary = utils.summarize_text(bill_data['text'], title, status, congress, bill_type, number)
                if summary and summary != "Summary unavailable due to insufficient data":
                    bill_data['summary'] = utils.clean_summary(summary)
                    logging.info(f"Successfully summarized {bill_id}: {bill_data['summary'][:100]}...")
                else:
                    bill_data['summary'] = None
                    logging.warning(f"Failed to generate valid summary for {bill_id}, keeping as skipped")
            else:
                bill_data['summary'] = None
                logging.info(f"No text available for {bill_id}, keeping as skipped")

            # Save full bill data to database
            utils.save_bill(bill_data)
            logging.info(f"Updated database for {bill_id}")

            time.sleep(5)  # Delay to avoid overwhelming APIs

    logging.info("Retry_skipped_bills process completed")

if __name__ == "__main__":
    retry_skipped_bills()
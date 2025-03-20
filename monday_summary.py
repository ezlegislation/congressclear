import sqlite3
import tweepy
from datetime import datetime, timedelta
import pytz

with open('config.json', 'r') as f:
    config = json.load(f)

api_key = config["api_key"]
api_secret = config["api_secret"]
access_token = config["access_token"]
access_secret = config["access_secret"]

client = tweepy.Client(consumer_key=api_key, consumer_secret=api_secret, access_token=access_token, access_token_secret=access_secret)
conn = sqlite3.connect('bills.db')
c = conn.cursor()

eastern = pytz.timezone('America/New_York')
last_monday = datetime.now(eastern) - timedelta(days=datetime.now(eastern).weekday())
start_of_last_week = last_monday - timedelta(days=7)
end_of_last_week = last_monday - timedelta(days=1)

c.execute("SELECT title, status, tweet_link FROM bills WHERE last_checked BETWEEN ? AND ? AND tweet_link IS NOT NULL",
          (start_of_last_week.isoformat(), end_of_last_week.isoformat()))
bills = c.fetchall()

summary_text = f"Legislative Summary for {start_of_last_week.strftime('%B %d')} - {end_of_last_week.strftime('%B %d')}:\n\n"
for bill in bills:
    summary_text += f"{bill[0]} ({bill[1]}): {bill[2]}\n"

if len(summary_text) > 280:
    parts = []
    current_part = f"Legislative Summary for {start_of_last_week.strftime('%B %d')} - {end_of_last_week.strftime('%B %d')}:\n\n"
    for bill in bills:
        line = f"{bill[0]} ({bill[1]}): {bill[2]}\n"
        if len(current_part) + len(line) > 280:
            parts.append(current_part)
            current_part = f"(Continued)\n{line}"
        else:
            current_part += line
    parts.append(current_part)
    for i, part in enumerate(parts):
        client.create_tweet(text=f"[{i+1}/{len(parts)}] {part}")
else:
    client.create_tweet(text=summary_text)

conn.close()
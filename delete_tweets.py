import tweepy
import json
import time

# Load credentials from config.json
with open('config.json', 'r') as f:
    config = json.load(f)

# Bearer Token for fetching tweet IDs
bearer_token = config["bearer_token"]

# OAuth 1.0a credentials for deleting tweets
api_key = config["api_key"]
api_secret = config["api_secret"]
access_token = config["access_token"]
access_token_secret = config["access_secret"]

# Initialize Tweepy Client with Bearer Token for fetching tweets
client_bearer = tweepy.Client(bearer_token=bearer_token)

# Initialize Tweepy Client with OAuth 1.0a for deleting tweets
auth = tweepy.OAuth1UserHandler(
    api_key,
    api_secret,
    access_token,
    access_token_secret
)
client_oauth = tweepy.Client(
    consumer_key=api_key,
    consumer_secret=api_secret,
    access_token=access_token,
    access_token_secret=access_token_secret
)

# Function to get user ID by username using Bearer Token
def get_user_id(client, username):
    print(f"Fetching user ID for {username}...")
    try:
        user = client.get_user(username=username)
        return user.data.id
    except tweepy.TweepyException as e:
        print(f"Error fetching user ID: {e}")
        exit(1)

# Function to fetch tweet IDs using Bearer Token
def get_tweet_ids(client, user_id):
    tweet_ids = []
    next_token = None
    print("Fetching tweet IDs from your timeline...")
    while True:
        try:
            tweets = client.get_users_tweets(
                id=user_id,
                max_results=100,  # Fetch 100 tweets per request
                tweet_fields=["id"],
                pagination_token=next_token
            )
            if tweets.data:
                tweet_ids.extend([tweet.id for tweet in tweets.data])
                print(f"Retrieved {len(tweet_ids)} tweet IDs so far...")
            next_token = tweets.meta.get("next_token")
            if not next_token:
                break  # No more tweets to fetch
        except tweepy.TweepyException as e:
            print(f"Error fetching tweets: {e}")
            break
    return tweet_ids

# Function to delete tweets with OAuth 1.0a and rate limit handling
def delete_tweets(client, tweet_ids):
    total = len(tweet_ids)
    print(f"Starting deletion of {total} tweets...")
    for i, tid in enumerate(tweet_ids, 1):
        try:
            client.delete_tweet(tid)
            print(f"Deleted tweet {tid} ({i}/{total})")
        except tweepy.TweepyException as e:
            if '429' in str(e):
                print("Rate limit reached. Waiting 15 minutes...")
                time.sleep(15 * 60)
                try:
                    client.delete_tweet(tid)
                    print(f"Deleted tweet {tid} after rate limit wait ({i}/{total})")
                except tweepy.TweepyException as e:
                    print(f"Failed to delete tweet {tid} after wait: {e} ({i}/{total})")
            else:
                print(f"Error deleting tweet {tid}: {e} ({i}/{total})")

# Main execution
if __name__ == "__main__":
    # Step 1: Fetch user ID using Bearer Token
    username = "ezlegislation"  # Replace with your Twitter handle if different
    user_id = get_user_id(client_bearer, username)

    # Step 2: Fetch tweet IDs using Bearer Token
    tweet_ids = get_tweet_ids(client_bearer, user_id)

    # Step 3: Confirm deletion
    if not tweet_ids:
        print("No tweets found to delete.")
    else:
        print(f"Found {len(tweet_ids)} tweets. Delete them? (y/n)")
        if input().lower() == 'y':
            # Step 4: Delete tweets using OAuth 1.0a
            delete_tweets(client_oauth, tweet_ids)
            print("Deletion process completed!")
        else:
            print("Deletion cancelled.")
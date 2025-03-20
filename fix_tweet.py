import logging
logging.basicConfig(filename='/home/srrdx9mw12tk/congressclear/app.log', level=logging.DEBUG)
logging.debug("Starting fix_tweet.py")
# Your existing imports and code follow

import requests
from xml.etree import ElementTree as ET
import google.generativeai as genai
import json
from flask import Flask, request, render_template
from datetime import datetime  # Added this import

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True

with open('config.json', 'r') as f:
    config = json.load(f)
congress_api_key = config["congress_api_key"]
genai.configure(api_key=config["gemini_api_key"])

with open('hashtags.json', 'r') as f:
    hashtag_config = json.load(f)
HASHTAG_POOL = hashtag_config["subjects"]
STATE_HASHTAGS = hashtag_config["states"]
MANDATORY_HASHTAGS = hashtag_config["mandatory"]

def fetch_with_retries(url, max_retries=3, delay=5):
    for attempt in range(max_retries):
        try:
            response = requests.get(url)
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            print(f"Attempt {attempt + 1} failed for {url}: {e}")
            if attempt < max_retries - 1:
                time.sleep(delay)
            else:
                return None

def fetch_bill_text(congress, bill_type, number):
    text_url = f"https://api.congress.gov/v3/bill/{congress}/{bill_type}/{number}/text?api_key={congress_api_key}&format=json"
    text_response = fetch_with_retries(text_url)
    if text_response is None:
        return None
    text_data = text_response.json().get('textVersions', [])
    for version in text_data:
        formats = version.get('formats', [])
        for format_item in formats:
            text_fetch_url = format_item.get('url', '')
            if 'xml' in format_item.get('contentType', '').lower() or 'xml' in text_fetch_url.lower():
                text_content_response = fetch_with_retries(text_fetch_url)
                if text_content_response:
                    try:
                        root = ET.fromstring(text_content_response.text)
                        text_elements = root.findall('.//text')
                        readable_text = " ".join(elem.text.strip() for elem in text_elements if elem.text and elem.text.strip())
                        if readable_text:
                            return readable_text
                    except ET.ParseError:
                        continue
    return None

def fetch_sponsor(congress, bill_type, number):
    detail_url = f"https://api.congress.gov/v3/bill/{congress}/{bill_type}/{number}?api_key={congress_api_key}&format=json"
    detail_response = fetch_with_retries(detail_url)
    if detail_response:
        detail_data = detail_response.json()['bill']
        sponsors = detail_data.get('sponsors', [])
        if sponsors:
            sponsor = sponsors[0]
            first_name = sponsor.get('firstName', '')
            last_name = sponsor.get('lastName', '')
            name = f"{first_name} {last_name}".strip()
            party = sponsor.get('party', 'Unknown')
            state = sponsor.get('state', 'Unknown')
            return {'name': name, 'party_state': f"{party}-{state}"}
    return {'name': 'Unknown', 'party_state': 'Unknown'}

def fetch_bill_actions(congress, bill_type, number):
    actions_url = f"https://api.congress.gov/v3/bill/{congress}/{bill_type}/{number}/actions?api_key={congress_api_key}&format=json"
    response = fetch_with_retries(actions_url)
    return response.json().get('actions', []) if response else []

def load_template(template_name):
    try:
        with open(f"templates/{template_name}.txt", 'r') as f:
            return f.read()
    except FileNotFoundError:
        if template_name == "new_bill":
            return (
                "Bill {status}: {bill_prefix}{number} ({congress_session}) - {bill_title}\n"
                "Introduced by {sponsor_name} [{sponsor_party_state}] on {introduced_date}\n\n"
                "Impact: {impact}\n\n"
                "{summary}\n\n"
                "Latest Action(s):\n{actions_list}\n\n"
                "For more details, visit:\n{link}\n\n"
                "Source: Congress.gov"
            )
        return ""

def summarize_text(text, bill_title, status, prompt_file="prompts/summarize.txt"):
    if not text or len(text) < 30:
        return "No specific impact identified", f"The bill’s full text is not yet available. It has been {status.lower()}."
    model = genai.GenerativeModel('gemini-1.5-flash')
    with open(prompt_file, 'r') as f:
        prompt_template = f.read()
    prompt = prompt_template.format(bill_title=bill_title, bill_text=text)
    try:
        response = model.generate_content(prompt)
        full_summary = response.text.replace('**', '').strip()
        paragraphs = full_summary.split('\n\n')
        impact = paragraphs[0].strip() if paragraphs else "No specific impact identified"
        summary = '\n'.join(paragraphs[1:]).strip() if len(paragraphs) > 1 else full_summary
        return impact, summary
    except Exception as e:
        print(f"Error summarizing: {e}")
        return "Summary failed", "Could not generate summary."

def get_hashtags(text, sponsor_state):
    if not text:
        return MANDATORY_HASHTAGS[:5]
    model = genai.GenerativeModel('gemini-1.5-flash')
    hashtag_list = [tag for sublist in HASHTAG_POOL.values() for tag in sublist] + list(STATE_HASHTAGS.values())
    prompt = f"Given this bill text:\n\n{text}\n\nSelect up to 5 relevant hashtags from this list (exclude {', '.join(MANDATORY_HASHTAGS)}):\n\n{', '.join(hashtag_list)}\n\nProvide only the hashtags, separated by spaces."
    if sponsor_state and sponsor_state in STATE_HASHTAGS:
        prompt += f"\n\nInclude #{sponsor_state} and {STATE_HASHTAGS[sponsor_state]} if relevant."
    try:
        response = model.generate_content(prompt)
        selected_hashtags = response.text.strip().split()
        all_hashtags = list(dict.fromkeys(MANDATORY_HASHTAGS + selected_hashtags))[:5]
        return all_hashtags
    except Exception:
        return MANDATORY_HASHTAGS[:5]

@app.route('/', methods=['GET'])
def show_form():
    return render_template('index.html')

@app.route('/generate', methods=['POST'])
def generate_tweet():
    bill_number = request.form.get('bill_number', '').strip()
    bill_type_input = request.form.get('bill_type', '').lower()
    congress = request.form.get('congress', '').strip()
    prompt_file = request.form.get('prompt_file', 'prompts/summarize.txt')

    if not bill_number or not bill_type_input or not congress:
        return render_template('index.html', error="Please fill all fields.")

    bill_type = "s" if bill_type_input.startswith('s') else "hr"
    bill_prefix = "S." if bill_type == "s" else "H.R."
    bill_id = f"{bill_prefix}{bill_number}"

    bill_text = fetch_bill_text(congress, bill_type, bill_number)
    sponsor_info = fetch_sponsor(congress, bill_type, bill_number)
    actions = fetch_bill_actions(congress, bill_type, bill_number)

    sorted_actions = sorted(actions, key=lambda x: x['actionDate'], reverse=True)
    status = "Introduced"
    chamber = "Senate" if bill_type == "s" else "House"
    for action in sorted_actions:
        if "Became Public Law" in action['text'] or "Signed by President" in action['text']:
            status = "Became Law"
            break
        elif "Passed" in action['text']:
            status = f"Passed {chamber}"
            break
        elif "Introduced" in action['text']:
            status = f"Introduced in {chamber}"

    detail_url = f"https://api.congress.gov/v3/bill/{congress}/{bill_type}/{bill_number}?api_key={congress_api_key}&format=json"
    detail_response = fetch_with_retries(detail_url)
    bill_title = detail_response.json()['bill']['title'] if detail_response and 'bill' in detail_response.json() else bill_id

    introduced_date = datetime.strptime(sorted_actions[-1]['actionDate'], '%Y-%m-%d').strftime('%B %d, %Y') if sorted_actions else "Unknown"
    actions_list = "\n".join([f"{datetime.strptime(a['actionDate'], '%Y-%m-%d').strftime('%B %d, %Y')} - {a['text']}" for a in sorted_actions[:3]]) if sorted_actions else "No actions"
    congress_session = f"{congress}th Congress"
    link = f"https://www.congress.gov/bill/{congress}th-congress/{'senate-bill' if bill_type == 's' else 'house-bill'}/{bill_number}"

    if bill_text:
        impact, summary = summarize_text(bill_text, bill_title, status, prompt_file)
        template_name = "new_bill"
    else:
        impact, summary = "", ""
        template_name = "no_text_available"

    hashtags = get_hashtags(bill_text, sponsor_info['party_state'].split('-')[1])
    tweet_content = load_template(template_name).format(
        status=status,
        bill_prefix=bill_prefix,
        number=bill_number,
        congress_session=congress_session,
        bill_title=bill_title,
        sponsor_name=sponsor_info['name'],
        sponsor_party_state=sponsor_info['party_state'],
        introduced_date=introduced_date,
        impact=impact,
        summary=summary,
        actions_list=actions_list,
        link=link
    ) + "\n\n" + " ".join(hashtags)

    return render_template('result.html', tweet_content=tweet_content, bill_id=bill_id)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
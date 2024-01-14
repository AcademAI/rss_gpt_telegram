# RSS to Telegram with GPT
Make unique posts with GPT off RSS feeds for your telegram channel!

# Features
1. Fetch Arxiv and Habr RSS feeds 
2. Article summarize with gpt4free
3. Control posts in bridge channel

## Setup
1. `git clone https://github.com/AcademAI/rss_gpt_telegram` 
2. Fill in the `.env` file with values

2 options from here:

### Via console:
```
pip install -r requirements.txt
python main.py
```

### Via Docker:
```
docker build --tag rss_gpt_tg . 
docker run rss_gpt_tg
```

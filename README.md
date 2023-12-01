# Telegram to VK
RSS feed parser with GPT to post in your channel

# Features
1. Fetch Arxiv and Habr RSS feeds 
2. Article summarize with gpt4free
3. Posts to Telegram
4. Admin commands for turning on/off rss feeds

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

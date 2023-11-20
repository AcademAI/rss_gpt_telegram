# Telegram to VK
RSS telegram parser with GPT in python. 

# Features
1. Fetch Arxiv and Habr RSS feeds 
2. Article summarize with gpt4free
3. Posts to Telegram, repost to VK
4. Supports editing on-the-fly & copies your photos/videos to the VK aswell.
5. Admin commands for turning on/off rss feeds

## Setup
1. `git clone https://github.com/AcademAI/telega2vkposter` 
2. Fill in the `.env` file with values

2 options from here:

### Via console:
```
pip install -r requirements.txt
python main.py
```

### Via Docker:
```
docker build --tag telegram_to_vk . 
docker run telegram_to_vk
```

import logging
import os
import re
import tenacity
import g4f
import time
import feedparser
import asyncio
import shelve

from tenacity import retry, stop_after_attempt
from aiogram import Bot, Dispatcher, types, executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.types import ContentType
from dotenv import load_dotenv
from aiohttp.client_exceptions import ServerDisconnectedError


load_dotenv()
logging.basicConfig(level=logging.INFO)

TELEGRAM_API_TOKEN = os.getenv('TELEGRAM_API_TOKEN')
TELEGRAM_POST_BRIDGE = os.getenv('TELEGRAM_POST_BRIDGE')
TELEGRAM_TARGET_CHANNEL = os.getenv('TELEGRAM_TARGET_CHANNEL')

PROXY_LOGIN = os.getenv('PROXY_LOGIN')
PROXY_PASSWORD = os.getenv('PROXY_PASSWORD')
PROXY_IP = os.getenv('PROXY_IP')
PROXY_PORT = os.getenv('PROXY_PORT')

db = shelve.open('data.db', writeback=True)
bot = Bot(token=TELEGRAM_API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)


@retry(stop=stop_after_attempt(5), wait=tenacity.wait_fixed(60))
async def get_response(text):
    try:
        response = await g4f.ChatCompletion.create_async(
            model=g4f.models.default,
            messages=[{"role": "user", "content": f"""You are an expert copywriter specializing in social media. 
            Your task is to create a post in RUSSIAN based on the article: {text}\n
            Use the structure as a template, but don't write itself in the post.\n
            1) Introduction to the article topic
            2) Usecases of the topic
            3) Conclusion of the article
            4) Link to the article
            """}],
            provider=g4f.Provider.You,
            proxy=f'http://{PROXY_LOGIN}:{PROXY_PASSWORD}@{PROXY_IP}:{PROXY_PORT}'
            
        )
        print('\nGPT сделал пост уникальным\n')
        return response
    except Exception as e:
        print(e)
        raise e


@retry(stop=stop_after_attempt(3), wait=tenacity.wait_fixed(10))
async def format_message(message: types.Message):
    
    if not message.photo:
        text = message.text
        response = await get_response(text)
        message.text = response
        return message
    else:
        text = message["caption"]
        response = await get_response(text)
        message.text = response
        #print(message)
        return message

@retry(stop=stop_after_attempt(3), wait=tenacity.wait_fixed(10))
async def parse_rss_feed(url):
    feed = feedparser.parse(url)
    entries = feed.entries

    if not entries:
        print(f"No entries found in feed {url}")
        return None, None

    entry = entries[0]
    title = entry.title
    link = entry.link
    description = entry.description
    post = title + "\n\n" + description + "\n\n" + link
    print(f"\nrss спарсил пост: \n\n {post}")
    return post, link

def add_post_to_db(msg_id, message_text):
    try:
        new_id = max(int(k) for k in db.keys()
                     if k.isdigit()) + 1
    except:
        new_id = 1
    db[str(new_id)] = {
        'message_id': msg_id,
        'message_text': message_text,
    }
    return new_id


@retry(stop=stop_after_attempt(3), wait=tenacity.wait_fixed(10), retry=tenacity.retry_if_exception_type(ServerDisconnectedError))
async def send_to_channel():
    urls = ["http://export.arxiv.org/rss/cs.AI", "https://habr.com/ru/rss/hubs/artificial_intelligence/articles/all/"]
    prev_ids = {"http://export.arxiv.org/rss/cs.AI": None, "https://habr.com/ru/rss/hubs/artificial_intelligence/articles/all/": None}

    while True:
        for url in urls:
            rss_post, link = await parse_rss_feed(url)

            if rss_post is None or link is None:
                print(f"Failed to parse RSS feed {url}")
                await asyncio.sleep(21600)
                continue

            rss_post_id = link.split('/')[-1].split('=')[1].split('&')[0] if url == "https://habr.com/ru/rss/hubs/artificial_intelligence/articles/all/" else link.split('/')[-1]

            if rss_post_id is None:
                print(f"rss_post_id is None for {url}, skipping")
                await asyncio.sleep(21600)
                continue

            if rss_post_id == prev_ids[url]:
                print(f"{rss_post_id} == {prev_ids[url]}, skip")
                await asyncio.sleep(21600)
                continue

            prev_ids[url] = rss_post_id
            
            msg = types.Message()
            msg.text = rss_post
            msg_id = msg.message_id

            formatted_message = await format_message(msg)
            try:
                post_id = add_post_to_db(msg_id, formatted_message.text)
                await bot.send_message(TELEGRAM_POST_BRIDGE, text=formatted_message.text)
                await bot.send_message(TELEGRAM_POST_BRIDGE, text=post_id)
            except Exception as e:
                print(f"Failed to send post {e}")

@dp.channel_post_handler(regexp=r"\d+\+")
@retry(stop=stop_after_attempt(3), wait=tenacity.wait_fixed(10), retry=tenacity.retry_if_exception_type(ServerDisconnectedError))
async def handle_post(message: types.Message):
    if message.chat and str(message.chat.id) == str(TELEGRAM_POST_BRIDGE):
        post_id = str(message.text).strip('+')
        post = db.get(post_id)
        if post is None:
            await bot.send_message(TELEGRAM_POST_BRIDGE, text='`ERROR NO POST ID IN DB`')
            return
        try:
            msg = types.Message()
            msg.text = post['message_text']
            await bot.send_message(TELEGRAM_TARGET_CHANNEL,  text=msg.text)
            await bot.send_message(TELEGRAM_POST_BRIDGE, text='`SUCCESS`')

        except Exception as e:
            await bot.send_message(TELEGRAM_POST_BRIDGE, text='`ERROR`')

async def main():
   await asyncio.gather(
       dp.start_polling(),
       send_to_channel()
   )

if __name__ == '__main__':
   asyncio.run(main())
import logging
import os
import tenacity
import g4f
import time
import feedparser
import asyncio

from tenacity import retry, stop_after_attempt
from aiogram import Bot, Dispatcher, types, executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.types import ContentType
from dotenv import load_dotenv
from aiohttp.client_exceptions import ServerDisconnectedError


load_dotenv()
logging.basicConfig(level=logging.INFO)

TELEGRAM_API_TOKEN = os.getenv('TELEGRAM_API_TOKEN')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')
TELEGRAM_SOURCE_PUBLICNAME = os.getenv('TELEGRAM_SOURCE_PUBLICNAME')
PROXY_LOGIN = os.getenv('PROXY_LOGIN')
PROXY_PASSWORD = os.getenv('PROXY_PASSWORD')
PROXY_IP = os.getenv('PROXY_IP')
PROXY_PORT = os.getenv('PROXY_PORT')


bot = Bot(token=TELEGRAM_API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
arxiv_running = False
habr_running = False
arxiv_prev_id = None
habr_prev_id = None



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

async def parse_rss_feed(url):
    feed = feedparser.parse(url)
    entries = feed.entries

    for entry in entries:
        title = entry.title
        link = entry.link
        description = entry.description
        post = title + "\n\n" + description + "\n\n" + link
        print(f"\nrss спарсил пост: \n\n {post}")
        return post, link

@dp.channel_post_handler(content_types=ContentType.ANY)
@retry(stop=stop_after_attempt(3), wait=tenacity.wait_fixed(10), retry=tenacity.retry_if_exception_type(ServerDisconnectedError))
async def new_channel_post(message: types.Message):
    global arxiv_running, arxiv_prev_id
    global habr_running, habr_prev_id
    if message.chat.username in TELEGRAM_SOURCE_PUBLICNAME and message.text == 'Arxiv':
        print('Старт архив')
        arxiv_running = True

        while arxiv_running:
            arxiv_post, link = await parse_rss_feed("http://export.arxiv.org/rss/cs.AI")
            arxiv_post_id = link.split('/')[-1]
            if arxiv_post_id == arxiv_prev_id:
               print(f"{arxiv_post_id} == {arxiv_prev_id}, skip")
               
               await asyncio.sleep(1000)
               continue

            arxiv_prev_id = arxiv_post_id

            message.text = arxiv_post
            formatted_message = await format_message(message)
            await bot.send_message(TELEGRAM_CHANNEL_ID,  text=formatted_message.text)

    elif message.chat.username in TELEGRAM_SOURCE_PUBLICNAME and message.text == 'Habr':
        print('Старт хабр')
        habr_running = True
        
        while habr_running:
            habr_post, link = await parse_rss_feed("https://habr.com/ru/rss/hubs/artificial_intelligence/articles/all/")
            habr_post_id = link.split('/')[-1].split('=')[1].split('&')[0]
            if habr_post_id == habr_prev_id:
               print(f"{habr_post_id} == {habr_prev_id}, skip")
               await asyncio.sleep(500)
               continue
            
            habr_prev_id = habr_post_id

            message.text = habr_post
            formatted_message = await format_message(message)
            await bot.send_message(TELEGRAM_CHANNEL_ID,  text=formatted_message.text)

    elif message.chat.username in TELEGRAM_SOURCE_PUBLICNAME and message.text == 'StopArxiv':
        print('Стоп архив')
        arxiv_running = False
    elif message.chat.username in TELEGRAM_SOURCE_PUBLICNAME and message.text == 'StopHabr':
        print('Стоп хабр')
        habr_running = False
    else:
        print('Пришел пост')
        formatted_message = await format_message(message)
        await bot.send_message(TELEGRAM_CHANNEL_ID,  text=formatted_message.text)

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)

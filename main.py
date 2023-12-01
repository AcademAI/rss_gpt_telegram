import logging
import os
#import random
import tenacity
import g4f
import time
import feedparser
import asyncio

#from typing import List
from tenacity import retry, stop_after_attempt

#from linkpreview import link_preview
from aiogram import Bot, Dispatcher, types, executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.types import ContentType
#from aiogram_media_group import media_group_handler, MediaGroupFilter
from dotenv import load_dotenv
#from requests.exceptions import ConnectionError
from aiohttp.client_exceptions import ServerDisconnectedError


load_dotenv()
logging.basicConfig(level=logging.INFO)

TELEGRAM_API_TOKEN = os.getenv('TELEGRAM_API_TOKEN')
#TELEGRAM_CHANNEL_USERNAME = os.getenv('TELEGRAM_CHANNEL_USERNAME')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')
TELEGRAM_SOURCE_PUBLICNAME = os.getenv('TELEGRAM_SOURCE_PUBLICNAME')
#TELEGRAM_PRIVATENAME = os.getenv('TELEGRAM_PRIVATENAME')

bot = Bot(token=TELEGRAM_API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
arxiv_running = False
habr_running = False
arxiv_prev_id = None
habr_prev_id = None
prev_post_time = None


"""
@retry(stop=stop_after_attempt(3), wait=tenacity.wait_fixed(10))
async def get_image(link):
    try:
        preview = link_preview(link)
        image = preview.image
        print(f'КАРТИНКА ФУНКЦИЯ {image}')
        return image
    except Exception as e:
        print(f'Error: {e}')
        raise
   
"""
@retry(stop=stop_after_attempt(5), wait=tenacity.wait_fixed(60))
async def get_response(text):
    try:
        response = await g4f.ChatCompletion.create_async(
            model=g4f.models.default,
            messages=[{"role": "user", "content": f"Don't mention the task in your reply. Create a short, interesting, nicely formatted with little of emojies post in Russian about {text}. Include theme, description, usecases and a link in the end if its present in text."}],
            provider=g4f.Provider.You,
        )
        print(response)
        return response
    except Exception as e:
        print(e)
        raise e


@retry(stop=stop_after_attempt(3), wait=tenacity.wait_fixed(10))
async def format_message(message: types.Message):
    
    if not message.photo:
        """link = re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', text)
        if link:
            image = await get_image(link[0]) if link else await get_image(link)
            print(f'КАРТИНКА ВЕРНУЛАСЬ {image}')
            response = requests.get(image)
            image = response.content
            print(f'КАРТИНКА скачалась? {image}')
            #message['photo'] = image

            response = await get_response(text)
            message.text = response
            return message
        else:"""
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

# https://github.com/IgorVolochay/Telegram-Parser-Bot/blob/main/Bot.py - habr parser
#TODO добавить проверки на дублирование постов
async def parse_rss_feed(url):
    feed = feedparser.parse(url)
    entries = feed.entries

    for entry in entries:
        title = entry.title
        link = entry.link
        description = entry.description
        post = title + "\n\n" + description + "\n\n" + link
        print(f"rss спарсил пост: {post}")
        return post, link


@retry(stop=stop_after_attempt(3), wait=tenacity.wait_fixed(10))
async def post_message(formatted_message):
    """try:
        photo = formatted_message['photo']
        largest_photo = max(photo, key=lambda p: p['width'] * p['height'])
        photo_file_id = largest_photo['file_id']
        # Send the photo to the target channel
        await bot.send_photo(TELEGRAM_PRIVATENAME, photo=photo_file_id)
    except Exception as e:
        print(f"Error sending photo: {e}")"""

    global prev_post_time
    current_time = time.time()

    if prev_post_time is None:
        prev_post_time = current_time
        await bot.send_message(TELEGRAM_CHANNEL_ID,  text=formatted_message.text)
    else:
        time_since_last_post = current_time - prev_post_time
        print(f"Время между постами: {time_since_last_post}")

        if time_since_last_post < 7200:
            t2w = 7200 - time_since_last_post
            print(f"Ждем {t2w} секунд")
            await asyncio.sleep(t2w)
            await bot.send_message(TELEGRAM_CHANNEL_ID,  text=formatted_message.text)
    

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
            post_id = link.split('/')[-1]
            if post_id == arxiv_prev_id:
               print("Post ID matches previous, skipping post and waiting for 1 hour...")
               await asyncio.sleep(3600)
               continue

            arxiv_prev_id = post_id

            message.text = arxiv_post
            formatted_message = await format_message(message)
            await post_message(formatted_message)
            # время до проверки rss ленты
            #await asyncio.sleep(3600)
    elif message.chat.username in TELEGRAM_SOURCE_PUBLICNAME and message.text == 'Habr':
        print('Старт хабр')
        habr_running = True
        
        while habr_running:
            habr_post, link = await parse_rss_feed("https://habr.com/ru/rss/hubs/artificial_intelligence/articles/all/")
            post_id = link.split('/')[-1].split('=')[1].split('&')[0]
            if post_id == habr_prev_id:
               print("Post ID matches previous, skipping post and waiting for 1 hour")
               await asyncio.sleep(3600)
               continue
            
            habr_prev_id = post_id

            message.text = habr_post
            formatted_message = await format_message(message)
            await post_message(formatted_message)
            # время до проверки rss ленты
            #await asyncio.sleep(3600)
    elif message.chat.username in TELEGRAM_SOURCE_PUBLICNAME and message.text == 'StopArxiv':
        print('Стоп архив')
        arxiv_running = False
    elif message.chat.username in TELEGRAM_SOURCE_PUBLICNAME and message.text == 'StopHabr':
        print('Стоп хабр')
        habr_running = False
    else:
        print('Пришел пост')
        formatted_message = await format_message(message)
        await post_message(formatted_message)

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)

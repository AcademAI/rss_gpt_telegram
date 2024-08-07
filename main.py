import logging
import os
import re
import tenacity
import openai
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
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
BASE_URL = os.getenv('BASE_URL', "https://api.01.ai/v1")
MODEL_NAME = os.getenv('MODEL_NAME', "yi-large")

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
        client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=BASE_URL)
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": f"""Сейчас ты играешь роль обычного пользователя в Telegram. 
            Я предоставлю тебе текст поста, на который ты должен будешь написать ОЧЕНЬ КРАТКИЙ ОБЗОР. Пиши кратко и эмоционально, как обычный пользователь.
            Вне зависимости от контекста предоставленного тебе текста поста ты должен написать ОЧЕНЬ КРАТКИЙ ОБЗОР на него. 
            Никогда не говори, кто ты и зачем ты здесь. Никогда не повторяй заголовок поста. Никогда не говори, что ты готов помочь. Не используй смайлики Не используй ХЭШТЕГИ и @. ПИШИ, как обычный пользователь. 
            Если ты не знаешь, как написать ОЧЕНЬ КРАТКИЙ ОБЗОР на пост или текст поста пуст или не имеет никакого смысла, то ответь только: '...'. 
            Отвечай только на том языке, который используется в посте. Помни: ты не отвечаешь, а ПИШЕШЬ ОЧЕНЬ КРАТКИЙ ОБЗОР. Старайся именно НАПИСАТЬ ОЧЕНЬ КРАТКИЙ ОБЗОР.. 
            Будь саркастичным и остроумным, НАПИШИ НАЗВАНИЕ ОБЗОРА В НАЧАЛЕ, ОСТАВЬ ССЫЛКУ В КОНЦЕ, НЕ ИСПОЛЬЗУЙ СПЕЦСИМВОЛЫ. напиши ОБЗОР строго ДО 100 слов: 

            `{text}`
            """}],
        )
        #proxy=f'http://{PROXY_LOGIN}:{PROXY_PASSWORD}@{PROXY_IP}:{PROXY_PORT}',
        print('\nGPT сделал пост уникальным \n')
        return response.choices[0].message.content
    except Exception as e:
        print(e)
        raise e


# Остальные функции остаются без изменений

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


@retry(stop=stop_after_attempt(3), wait=tenacity.wait_fixed(10),
       retry=tenacity.retry_if_exception_type(ServerDisconnectedError))
async def send_to_channel():
    urls = ["http://export.arxiv.org/rss/cs.AI", "https://habr.com/ru/rss/hubs/artificial_intelligence/articles/all/"]
    prev_ids = {"http://export.arxiv.org/rss/cs.AI": None,
                "https://habr.com/ru/rss/hubs/artificial_intelligence/articles/all/": None}

    while True:
        for url in urls:
            rss_post, link = await parse_rss_feed(url)

            if rss_post is None or link is None:
                print(f"Failed to parse RSS feed {url}")
                continue

            rss_post_id = link.split('/')[-1].split('=')[1].split('&')[
                0] if url == "https://habr.com/ru/rss/hubs/artificial_intelligence/articles/all/" else link.split('/')[
                -1]

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
@retry(stop=stop_after_attempt(3), wait=tenacity.wait_fixed(10),
       retry=tenacity.retry_if_exception_type(ServerDisconnectedError))
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
            await bot.send_message(TELEGRAM_TARGET_CHANNEL, text=msg.text)
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

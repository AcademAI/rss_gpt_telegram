import logging
import os
import random
import tenacity
import g4f
import re
import requests
import feedparser
import time
import asyncio

from typing import List
from tenacity import retry, stop_after_attempt

from linkpreview import link_preview
from aiogram import Bot, Dispatcher, types, executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.types import ContentType
from aiogram_media_group import media_group_handler, MediaGroupFilter
from dotenv import load_dotenv
from requests.exceptions import ConnectionError
from aiohttp.client_exceptions import ServerDisconnectedError
from vk_api import VkApi, upload

load_dotenv()
logging.basicConfig(level=logging.INFO)

TELEGRAM_API_TOKEN = os.getenv('TELEGRAM_API_TOKEN')
TELEGRAM_CHANNEL_USERNAME = os.getenv('TELEGRAM_CHANNEL_USERNAME')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')
TELEGRAM_SOURCE_PUBLICNAME = os.getenv('TELEGRAM_SOURCE_PUBLICNAME')
TELEGRAM_PRIVATENAME = os.getenv('TELEGRAM_PRIVATENAME')
VK_API_TOKEN = os.getenv('VK_API_TOKEN')
VK_GROUP_ID = os.getenv('VK_GROUP_ID')

vk_session = VkApi(token=VK_API_TOKEN, api_version='5.131')
vk = vk_session.get_api()
uploader = upload.VkUpload(vk)
bot = Bot(token=TELEGRAM_API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
arxiv_running = False
habr_running = False

def add_entry(message_id, post_id):
    # function for the editing sync to work
    # returns post_id from message_id
    with open('data.txt', 'a') as f:
        f.write(f'{message_id}:{post_id}\n')


def get_entry(message_id) -> int:
    # function for the editing sync to work
    # returns post_id from message_id
    with open('data.txt', 'r') as f:
        for line in f.readlines():
            if int(line.split(':')[0]) == message_id:
                return int(line.split(':')[1])
    raise KeyError(f'{message_id} is not in the file!')


@tenacity.retry(stop=tenacity.stop_after_attempt(3), wait=tenacity.wait_fixed(1), retry=tenacity.retry_if_exception_type(ConnectionError))
def create_vk_post(text: str, message_id, photo_list=None, video_list=None):
    photos, videos = [], []
    if photo_list:
        photos = uploader.photo_wall(photos=photo_list, group_id=VK_GROUP_ID)
        for i in photo_list:
            os.remove(i)
    if video_list:
        videos = [uploader.video(video_file=i, group_id=int(VK_GROUP_ID), album_id=0) for i in video_list]
        for i in video_list:
            os.remove(i)
    attachments = [f'photo{i["owner_id"]}_{i["id"]}' for i in photos]
    attachments += [f'video{i["owner_id"]}_{i["video_id"]}' for i in videos]

    post = vk.wall.post(
        message=text, from_group=1, attachments=attachments,
        owner_id=f'-{VK_GROUP_ID}', copyright=f'https://{TELEGRAM_CHANNEL_USERNAME}.t.me/{message_id}'
    )
    add_entry(message_id, post['post_id'])

def edit_vk_post(post_id, new_text, message_id):
    # getting data from the original post
    old_post = vk.wall.get_by_id(posts=f'-{VK_GROUP_ID}_{post_id}')[0]
    if old_post.get('attachments'):
        attachments = [f'{attachment["type"]}{attachment[attachment["type"]]["owner_id"]}_{attachment[attachment["type"]]["id"]}' for attachment in old_post['attachments']]
    else:
        attachments = []
    # edit it
    vk.wall.edit(
        message=new_text, post_id=post_id, from_group=1,
        owner_id=f'-{VK_GROUP_ID}', copyright=f'https://{TELEGRAM_CHANNEL_USERNAME}.t.me/{message_id}',
        attachments=attachments
    )



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
    text = message.text

    if not message.photo:
        link = re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', text)
        if link:
            image = await get_image(link[0]) if link else await get_image(link)
            print(f'КАРТИНКА ВЕРНУЛАСЬ {image}')
            response = requests.get(image)
            image = response.content
            print(f'КАРТИНКА скачалась? {image}')
            message.photo = image

            response = await get_response(text)
            message.text = response
            return message
        else:
            response = await get_response(text)
            message.text = response
            return message
    else:
        response = await get_response(text)
        message.text = response
        return message

# https://github.com/IgorVolochay/Telegram-Parser-Bot/blob/main/Bot.py - habr parser
#TODO добавить проверки на дублирование постов
async def parse_rss_feed(url):
    feed = feedparser.parse(url)
    entries = feed.entries
    print(f"{len(entries)} entries")

    for entry in entries:
        title = entry.title
        link = entry.link
        description = entry.description
        arxiv_post = title + "\n\n" + description + "\n\n" + link
        print(f"Архив {arxiv_post}")
        return arxiv_post

async def post_message(formatted_message):
   await formatted_message.send_copy(TELEGRAM_PRIVATENAME)
   await asyncio.sleep(10)

@dp.channel_post_handler(content_types=ContentType.ANY)
@retry(stop=stop_after_attempt(3), wait=tenacity.wait_fixed(10), retry=tenacity.retry_if_exception_type(ServerDisconnectedError))
async def new_channel_post(message: types.Message):
    global arxiv_running
    global habr_running
    if message.chat.username in TELEGRAM_SOURCE_PUBLICNAME and message.text == 'Arxiv':
        print('Старт архив')
        arxiv_running = True
        while arxiv_running:
            arxiv_post = await parse_rss_feed("http://export.arxiv.org/rss/cs.AI")
            message.text = arxiv_post
            formatted_message = await format_message(message)
            await post_message(formatted_message)
    elif message.chat.username in TELEGRAM_SOURCE_PUBLICNAME and message.text == 'Habr':
        print('Старт хабр')
        habr_running = True
        while habr_running:
            habr_post = await parse_rss_feed("https://habr.com/ru/rss/hubs/artificial_intelligence/articles/all/")
            message.text = habr_post
            formatted_message = await format_message(message)
            await post_message(formatted_message)
    elif message.chat.username in TELEGRAM_SOURCE_PUBLICNAME and message.text == 'ArxivStop':
        print('Стоп архив')
        arxiv_running = False
    elif message.chat.username in TELEGRAM_SOURCE_PUBLICNAME and message.text == 'HabrStop':
        print('Стоп хабр')
        habr_running = False
    else:
        print('Пришел пост')
        formatted_message = await format_message(message)
        await post_message(formatted_message)

@dp.channel_post_handler(MediaGroupFilter(), content_types=ContentType.ANY)
@media_group_handler
async def album_handler(messages: List[types.Message]):
    # album handler - many photos/videos
    if messages[0].chat.username != TELEGRAM_CHANNEL_USERNAME:
        logging.info('someone sent a message from a chat that is not the one that I monitor')
        return

    random_number = random.randint(1000000, 9999999)
    c = 0

    photo_list = []
    video_list = []
    text = None
    for message in messages:
        if text is None and message.caption is not None:
            text = message.caption
        if message.photo:
            path = f'./files/photo_{random_number}_{c}.jpg'
            await message.photo[-1].download(path)
            photo_list.append(path)
        elif message.video:
            path = f'./files/video_{random_number}_{c}.mp4'
            await message.video.download(path)
            video_list.append(path)
        c += 1

    create_vk_post(text, message_id=messages[0].message_id, photo_list=photo_list, video_list=video_list)


@dp.channel_post_handler(content_types=['photo', 'video'])
async def photo_video_handler(message: types.Message):
    # only 1 photo / only 1 video
    if message.chat.username != TELEGRAM_CHANNEL_USERNAME:
        logging.info('someone sent a message from a chat that is not the one that I monitor')
        return
    text = None
    random_number = random.randint(1000000, 9999999)
    if text is None and message.caption is not None:
        text = message.caption
    if message.photo:
        path = f'./files/photo_{random_number}.jpg'
        await message.photo[-1].download(path)
    elif message.video:
        path = f'./files/video_{random_number}.mp4'
        await message.video.download(path)
    if not text:
        text = ''
    if '.mp4' in path:
        create_vk_post(text=text, message_id=message.message_id, video_list=[path])
    else:
        create_vk_post(text=text, message_id=message.message_id, photo_list=[path])


@dp.channel_post_handler(content_types=ContentType.ANY)
async def message_handler(message: types.Message):
    # text only message
    if message.chat.username != TELEGRAM_CHANNEL_USERNAME:
        logging.info('someone sent a message from a chat that is not the one that I monitor')
        return
    create_vk_post(message.text, message_id=message.message_id)


@dp.edited_channel_post_handler(content_types=ContentType.ANY)
async def message_edited_handler(message: types.Message):
    if message.chat.username != TELEGRAM_CHANNEL_USERNAME:
        logging.info('someone sent a message from a chat that is not the one that I monitor')
        return
    try:
        post_id = get_entry(message.message_id)
    except KeyError:
        logging.error(f'entry of post associated with message_id {message.message_id} is not found.'
                      f' aborting editing sync')
        return

    text = message.text
    if message.text is None and message.caption is not None:
        text = message.caption
    elif message.text is None and message.caption is None:
        text = ''
    edit_vk_post(post_id=post_id, new_text=text, message_id=message.message_id)
    print('edited')


if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)

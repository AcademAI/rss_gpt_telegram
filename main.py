import logging
import os
import hashlib
import sqlite3
import asyncio
import certifi
import ssl
import feedparser
import re
import random
from functools import wraps
from typing import Dict, Optional, Tuple, List, Union
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type
from aiogram import Bot, Dispatcher, types, executor
from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiohttp.client_exceptions import ServerDisconnectedError
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv()
logging.basicConfig(level=logging.INFO)

# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID'))  # Ensure ADMIN_ID is an integer
SUPER_ADMIN_ID = int(os.getenv('SUPER_ADMIN_ID', ADMIN_ID))  # Add super admin ID
TELEGRAM_API_TOKEN = os.getenv('TELEGRAM_API_TOKEN')
TELEGRAM_TARGET_CHANNEL_ID = os.getenv('TELEGRAM_TARGET_CHANNEL_ID')
DATABASE_NAME = "rss_feeds.db"
client = OpenAI(api_key=OPENAI_API_KEY)
ssl_context = ssl.create_default_context(cafile=certifi.where())
system_prompt = """\
    You are an IT content writer for a Telegram channel. Transform the provided information into a structured post using this template:
    **TASK**
    - Create engaging Telegram posts in English from IT content (CVEs, tutorials, news)
    - Use consistent structure and Telegram formatting
    - Include hashtags and emojis
    **TEMPLATE STRUCTURE**
    1. Title: [Relevant Emoji] + Catchy Headline | Hi, AcademAI is here.
    2. Summary: 1-2 sentence overview
    3. Key Details:
    - Use bullet points (—)
    - Technical specs/versions/steps
    4. Link: "⏺ Link to origin ([text](URL))"
    5. Hashtags: 3-5 capitalized tags (e.g., #Linux #Security)

    **FORMATTING RULES**
    - Plain text only (no markdown)
    - Short paragraphs (1-3 lines)
    - Emojis for emphasis (❗️ 🔥 🖥)
    - Clean spacing between sections
    - English language only
    **EXAMPLES**
    Example 1:
    🥷 CVE-2024-XXXX: Vulnerability in PHP | Hi, AcademAI is here.
    A critical vulnerability has been discovered in...
    - Vulnerable versions:
    - Version X.Y.Z
    - Recommendation: Upgrade to vX.Y.Z+1.
    ⏺ Link to read (https://example.com)
    #PHP #Security #CVE"""

# Initialize bot and dispatcher
bot = Bot(token=TELEGRAM_API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

class DebugFeedStates(StatesGroup):
    waiting_for_confirmation = State()

# Database setup
class Database:
    def __init__(self, db_name: str = DATABASE_NAME):
        self.conn = sqlite3.connect(db_name)
        self._initialize_db()

    def _initialize_db(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_posts (
                    hash TEXT PRIMARY KEY,
                    feed_url TEXT,
                    url TEXT,
                    title TEXT,
                    was_posted BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS active_feeds (
                    url TEXT PRIMARY KEY,
                    interval INTEGER,
                    last_check TIMESTAMP
                )
            """)
            # New admins table
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY,
                    is_sa BOOLEAN DEFAULT 0
                )
            """)

    def add_seen_post(self, post_hash: str, feed_url: str, url: str, title: str):
        with self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO seen_posts 
                (hash, feed_url, url, title) VALUES (?, ?, ?, ?)""",
                (post_hash, feed_url, url, title)
            )
    
    def mark_as_posted(self, post_hash: str):
        with self.conn:
            self.conn.execute(
                "UPDATE seen_posts SET was_posted = 1 WHERE hash = ?",
                (post_hash,)
            )

    def is_post_seen(self, post_hash: str) -> bool:
        cursor = self.conn.execute(
            "SELECT 1 FROM seen_posts WHERE hash = ?",
            (post_hash,)
        )
        return cursor.fetchone() is not None

    def add_active_feed(self, url: str, interval: int):
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO active_feeds (url, interval, last_check)
                   VALUES (?, ?, CURRENT_TIMESTAMP)""",
                (url, interval)
            )

    def remove_active_feed(self, url: str):
        with self.conn:
            self.conn.execute(
                "DELETE FROM active_feeds WHERE url = ?",
                (url,)
            )

    def get_active_feeds(self) -> Dict[str, int]:
        cursor = self.conn.execute(
            "SELECT url, interval FROM active_feeds"
        )
        return {row[0]: row[1] for row in cursor.fetchall()}

    # Admin management methods
    def add_admin(self, user_id: int, is_sa: bool = False):
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO admins (user_id, is_sa)
                   VALUES (?, ?)""",
                (user_id, int(is_sa))
            )

    def remove_admin(self, user_id: int):
        with self.conn:
            self.conn.execute(
                "DELETE FROM admins WHERE user_id = ?",
                (user_id,)
            )

    def is_admin(self, user_id: int) -> bool:
        cursor = self.conn.execute(
            "SELECT 1 FROM admins WHERE user_id = ?",
            (user_id,)
        )
        return cursor.fetchone() is not None

    def is_super_admin(self, user_id: int) -> bool:
        cursor = self.conn.execute(
            "SELECT is_sa FROM admins WHERE user_id = ?",
            (user_id,)
        )
        result = cursor.fetchone()
        return result and result[0] == 1

    def get_all_admins(self) -> List[Dict[str, Union[int, bool]]]:
        cursor = self.conn.execute(
            "SELECT user_id, is_sa FROM admins"
        )
        return [
            {
                'user_id': row[0],
                'is_sa': bool(row[1])
            }
            for row in cursor.fetchall()
        ]
    
db = Database()

# Feed monitoring
active_tasks: Dict[str, asyncio.Task] = {}

def _test_url(url: str) -> bool:
    """Validate URL format using regex"""
    url_regex = re.compile(
        r'^https?://'
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|'
        r'localhost|'
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
        r'(?::\d+)?'
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    return re.match(url_regex, url) is not None

def generate_post_hash(entry: dict) -> str:
    """Generate unique hash for RSS entry"""
    unique_data = f"{entry.get('link', '')}{entry.get('title', '')}"
    return hashlib.sha256(unique_data.encode()).hexdigest()

# Decorators
def admin_only(func):
    """Decorator to check if user is admin"""
    @wraps(func)
    async def wrapper(message: types.Message, *args, **kwargs):
        if not db.is_admin(message.from_user.id) and ADMIN_ID != message.from_user.id:
            await message.reply("🔒 Unauthorized access")
            return
        return await func(message, *args, **kwargs)
    return wrapper

def super_admin_only(func):
    """Decorator to check if user is super admin"""
    @wraps(func)
    async def wrapper(message: types.Message, *args, **kwargs):
        if not db.is_super_admin(message.from_user.id) and SUPER_ADMIN_ID != message.from_user.id:
            await message.reply("🔒 Super admin access required")
            return
        return await func(message, *args, **kwargs)
    return wrapper

# Helper function for feed parsing
def fetch_feed_entries(url: str) -> list:
    """Parse RSS feed and return entries without database interaction"""

    feed = feedparser.parse(url)
    if feed.bozo:
        raise ValueError(f"Feed parsing error: {feed.bozo_exception}")
    return feed.entries

@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(10),
    retry=retry_if_exception_type((ServerDisconnectedError, Exception))
)
async def parse_rss_feed(feed_url: str) -> Tuple[str, str, str]:
    """Parse RSS feed and return entries"""
    feed = feedparser.parse(feed_url)
    if feed.bozo:
        raise ValueError(f"Feed parsing error: {feed.bozo_exception}")

    entries = []
    for entry in feed.entries:
        post_hash = generate_post_hash(entry)
        if not db.is_post_seen(post_hash):
            entries.append(entry)
            db.add_seen_post(post_hash, feed_url, entry.link, entry.title)

    if not entries:
        return "", "", ""

    latest_entry = entries[0]
    content = f"{latest_entry.title}\n\n{latest_entry.description}\n\n{latest_entry.link}"
    return content, latest_entry.link, generate_post_hash(latest_entry)

@retry(stop=stop_after_attempt(3), wait=wait_fixed(10))
async def get_gpt_response(input_data: str) -> str:
    """Generate response using OpenAI GPT-4"""
    try:
        response = client.chat.completions.create(
            model="gpt-4-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input_data}
            ],
            temperature=0.7,
            max_tokens=500
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"GPT API error: {str(e)}")
        raise

async def monitor_feed(feed_url: str, interval: int):
    """Background task to monitor RSS feed"""
    while True:
        try:
            content, link, post_hash = await parse_rss_feed(feed_url)
            
            if content:
                db.mark_as_posted(post_hash)
                gpt_response = await get_gpt_response(content)
                
                await bot.send_message(TELEGRAM_TARGET_CHANNEL_ID, text=gpt_response)
        except Exception as e:
            logging.error(f"Error monitoring {feed_url}: {str(e)}")
        
        await asyncio.sleep(interval)

@dp.message_handler(commands=['set_prompt'])
@super_admin_only
async def set_prompt_handler(message: types.Message):
    """Change the GPT prompt template on the go"""
    try:
        # Get the new prompt from the message
        _, *prompt_parts = message.text.split(maxsplit=1)
        new_prompt = ' '.join(prompt_parts) if prompt_parts else None
        
        if not new_prompt:
            await message.reply("❌ Please provide the new prompt template")
            return
            
        # Update the global system_prompt variable
        global system_prompt
        system_prompt = new_prompt
        
        await message.reply("✅ Prompt template updated successfully")
        
    except Exception as e:
        logging.error(f"Error setting prompt: {str(e)}")
        await message.reply(f"❌ Error: {str(e)}")

# New admin management handlers
@dp.message_handler(commands=['add_admin'])
@super_admin_only
async def add_admin_handler(message: types.Message):
    """Add new admin"""
    try:
        _, user_id, is_sa = message.text.split()
        user_id = int(user_id)
        is_sa = bool(is_sa)
        db.add_admin(user_id, is_sa)
        await message.reply(f"✅ Added admin {user_id}")
    except Exception as e:
        await message.reply(f"❌ Error: {str(e)}")

@dp.message_handler(commands=['remove_admin'])
@super_admin_only
async def remove_admin_handler(message: types.Message):
    """Remove admin"""
    try:
        _, user_id = message.text.split()
        user_id = int(user_id)
        db.remove_admin(user_id)
        await message.reply(f"✅ Removed admin {user_id}")
    except Exception as e:
        await message.reply(f"❌ Error: {str(e)}")

@dp.message_handler(commands=['get_admins'])
@super_admin_only
async def get_admins_handler(message: types.Message):
    """Gets all admins"""
    try:
        # Get all admins from database
        admins = db.get_all_admins()
        
        # Format the response
        if not admins:
            await message.reply("ℹ️ No admins found")
            return
            
        # Create formatted response
        response = "👥 Admins:\n\n"
        for admin in admins:
            status = "👑 Super Admin" if admin['is_sa'] else "👨‍💼 Admin"
            response += f"{status} - ID: {admin['user_id']}\n"
        
        await message.reply(response)
        
    except Exception as e:
        await message.reply(f"❌ Error: {str(e)}")

@dp.message_handler(commands=['add_feed'])
@admin_only
async def add_feed_handler(message: types.Message):
    """Add new RSS feed to monitor"""
    try:
        _, url, interval = message.text.split()
        interval = int(interval)
        
        if not _test_url(url):
            await message.reply("❌ Invalid URL format")
            return

        db.add_active_feed(url, interval)
        if url not in active_tasks:
            task = asyncio.create_task(monitor_feed(url, interval))
            active_tasks[url] = task
            await message.reply(f"✅ Started monitoring {url}")
        else:
            await message.reply("ℹ️ Feed already being monitored")

    except ValueError:
        await message.reply("❌ Invalid format. Use /add_feed <URL> <INTERVAL_SECONDS>")

@dp.message_handler(commands=['stop_feed'])
@admin_only
async def stop_feed_handler(message: types.Message):
    """Stop monitoring a feed"""
    try:
        _, url = message.text.split()
        if url in active_tasks:
            active_tasks[url].cancel()
            del active_tasks[url]
            db.remove_active_feed(url)
            await message.reply(f"⏹ Stopped monitoring {url}")
        else:
            await message.reply("❌ Feed not found in active monitors")
    except ValueError:
        await message.reply("❌ Invalid format. Use /stop_feed <URL>")

@dp.message_handler(commands=['debug_get_feed'])
@admin_only
async def debug_get_feed_handler(message: types.Message):
    """Debug command to fetch feed content without marking as seen"""
    try:
        _, url = message.text.split()
    except ValueError:
        await message.reply("❌ Invalid format. Use /debug_get_feed <URL>")
        return

    if not _test_url(url):
        await message.reply("❌ Invalid URL format")
        return

    try:
        entries = fetch_feed_entries(url)
        if not entries:
            await message.reply("ℹ️ No entries found in the feed")
            return

        entry = random.choice(entries)
        content = (
            "🔍 Debug Feed Entry\n\n"
            f"📌 Title: {entry.get('title', 'No title')}\n\n"
            f"📝 Description: {entry.get('description', 'No description')[:500]}...\n\n"
            f"🔗 Link: {entry.get('link', 'No link')}"
        )
        await message.reply(content)
        
    except Exception as e:
        logging.error(f"Debug feed error: {str(e)}")
        await message.reply(f"❌ Error fetching feed: {str(e)}")

@dp.message_handler(commands=['send_seen_to_target'])
@admin_only
async def send_seen_to_target_handler(message: types.Message):
    """Send a specific seen post to the target channel using GPT-4"""
    try:
        _, post_hash = message.text.split()
    except ValueError:
        await message.reply("❌ Invalid format. Use /send_seen_to_target <post_hash>")
        return
    
    try:
        cursor = db.conn.execute(
            "SELECT feed_url, url, title, was_posted FROM seen_posts WHERE hash = ?",
            (post_hash,)
        )
        post = cursor.fetchone()
        
        if not post:
            await message.reply(f"❌ Post with hash {post_hash} not found")
            return
            
        feed_url, url, title, was_posted = post
        
        # Fetch feed content
        entries = fetch_feed_entries(feed_url)
        if not entries:
            await message.reply(f"❌ No entries found in feed: {feed_url}")
            return
            
        # Find the specific entry matching our hash
        target_entry = None
        for entry in entries:
            if generate_post_hash(entry) == post_hash:
                target_entry = entry
                break
                
        if not target_entry:
            await message.reply(f"❌ Entry not found in feed: {feed_url}")
            return
            
        # Generate content for GPT
        content = f"{title}\n\n{target_entry.get('description', '')}\n\n{url}"
        
        # Process with GPT and send to target channel
        gpt_response = await get_gpt_response(content)
        await bot.send_message(TELEGRAM_TARGET_CHANNEL_ID, text=gpt_response)
        
        # Mark as posted
        db.mark_as_posted(post_hash)
        await message.reply("✅ Post sent to target channel!")
        
    except Exception as e:
        logging.error(f"Error sending post to target: {str(e)}")
        await message.reply(f"❌ Error: {str(e)}")

@dp.message_handler(commands=['debug_send_to_target'])
@admin_only
async def debug_send_to_target_handler(message: types.Message, state: FSMContext):
    """Debug command to fetch feed content without marking as seen but putting thru GPT and sending to target"""
    try:
        _, url = message.text.split()
    except ValueError:
        await message.reply("❌ Invalid format. Use /debug_send_to_target <URL>")
        return

    if not _test_url(url):
        await message.reply("❌ Invalid URL format")
        return

    try:
        entries = fetch_feed_entries(url)
        if not entries:
            await message.reply("ℹ️ No entries found in the feed")
            return

        entry = random.choice(entries)
        content = (
            "🔍 Debug Feed Entry\n\n"
            f"📌 Title: {entry.get('title', 'No title')}\n\n"
            f"📝 Description: {entry.get('description', 'No description')[:500]}...\n\n"
            f"🔗 Link: {entry.get('link', 'No link')}"
        )
        # Store content in FSM state
        async with state.proxy() as data:
            data['content'] = content
        
        await message.reply(content)
        await message.reply("🤔 Should I process this with GPT and send to the target channel?")
        await DebugFeedStates.waiting_for_confirmation.set()
        #gpt_response = await get_gpt_response(content)
        #await bot.send_message(TELEGRAM_TARGET_CHANNEL_ID, text=gpt_response)
        
        
    except Exception as e:
        logging.error(f"Debug feed error: {str(e)}")
        await message.reply(f"❌ Error fetching feed: {str(e)}")

@dp.message_handler(commands=['get_seen_posts'])
@admin_only
async def get_seen_posts_handler(message: types.Message):
    """Fetch and display seen posts for a specific feed using date or amount"""
    try:
        _, feed_url, param = message.text.split()
    except ValueError:
        await message.reply("❌ Invalid format. Use /get_seen_posts <feed_url> <amount|date>")
        return
    
    try:
        # Try to parse as date (YYYY-MM-DD format)
        date_param = datetime.strptime(param, '%Y-%m-%d')
        is_date = True
    except ValueError:
        # If not a date, try to parse as amount
        try:
            amount = int(param)
            is_date = False
        except ValueError:
            await message.reply("❌ Invalid parameter. Use YYYY-MM-DD for date or a number for amount")
            return

    try:
        cursor = db.conn.execute(
            "SELECT hash, url, title, was_posted, created_at FROM seen_posts "
            "WHERE feed_url = ? ORDER BY created_at DESC",
            (feed_url,)
        )
        posts = cursor.fetchall()
        
        if not posts:
            await message.reply(f"ℹ️ No seen posts found for feed: {feed_url}")
            return
            
        # Filter posts based on parameter type
        filtered_posts = []
        if is_date:
            # Convert date to start of day and add one day for end
            start_date = date_param.replace(hour=0, minute=0, second=0)
            end_date = start_date + timedelta(days=1)
            
            for post in posts:
                post_date = datetime.strptime(post[4], '%Y-%m-%d %H:%M:%S')
                if start_date <= post_date < end_date:
                    filtered_posts.append(post)
        else:
            # Use amount parameter
            filtered_posts = posts[:amount]
        
        if not filtered_posts:
            await message.reply(f"ℹ️ No posts found for {feed_url}{' on ' + param if is_date else ' in the last ' + param}")
            return
            
        # Split posts into chunks of 4096 characters
        chunk_size = 4096
        
        def format_post(idx, post_hash, url, title, was_posted, created_at):
            return (
                f"{idx}. 🆔 Hash: {post_hash}\n"
                f"   🔗 URL: {url}\n"
                f"   📝 Title: {title}\n"
                f"   📤 Posted: {'✅' if was_posted else '❌'}\n"
                f"   🕒 Created at: {created_at}\n\n"
            )
            
        current_chunk = ""
        chunk_number = 1
        
        for idx, (post_hash, url, title, was_posted, created_at) in enumerate(filtered_posts, 1):
            formatted_post = format_post(
                idx, post_hash, url, title, was_posted, created_at
            )
            
            if len(current_chunk) + len(formatted_post) > chunk_size:
                await message.reply(f"📋 Part {chunk_number}/{len(filtered_posts)//(chunk_size//100)+1}:")
                await message.reply(current_chunk.strip())
                
                current_chunk = formatted_post
                chunk_number += 1
            else:
                current_chunk += formatted_post
                
        # Send the last chunk
        if current_chunk:
            await message.reply(f"📋 Final part ({chunk_number}/{chunk_number}):")
            await message.reply(current_chunk.strip())
            
    except Exception as e:
        await message.reply(f"❌ Error fetching posts: {str(e)}")

@dp.message_handler(commands=['get_active_feeds'])
@admin_only
async def get_active_feeds_handler(message: types.Message):
    """Fetch and display all active feeds"""
    try:
        active_feeds = db.get_active_feeds()

        if not active_feeds:
            await message.reply("ℹ️ No active feeds found.")
            return

        response = "📋 Active Feeds:\n\n"
        for idx, (url, interval) in enumerate(active_feeds.items(), 1):
            response += (
                f"{idx}. 🔗 URL: {url}\n"
                f"   ⏰ Interval: {interval} seconds\n\n"
            )

        await message.reply(response)

    except Exception as e:
        logging.error(f"Error fetching active feeds: {str(e)}")
        await message.reply(f"❌ Error fetching active feeds: {str(e)}")

@dp.message_handler(state=DebugFeedStates.waiting_for_confirmation)
async def handle_confirmation(message: types.Message, state: FSMContext):
    """Handle user confirmation for GPT processing"""
    try:
        user_response = message.text.lower()
        if user_response in ['yes', 'y', 'да', 'yes']:
            # Store the content in FSM state instead of trying to get it from history
            async with state.proxy() as data:
                content = data.get('content')
                if not content:
                    await message.reply("❌ Content not found in state")
                    return
                
                gpt_response = await get_gpt_response(content)
                await bot.send_message(TELEGRAM_TARGET_CHANNEL_ID, text=gpt_response)
                await message.reply("✅ Content sent to target channel!")
        elif user_response in ['no', 'n', 'нет', 'no']:
            await message.reply("🚫 Operation cancelled")
        else:
            await message.reply("❓ Please respond with 'yes' or 'no'")
            return
            
        await state.finish()
        
    except Exception as e:
        logging.error(f"Confirmation handler error: {str(e)}")
        await message.reply(f"❌ Error: {str(e)}")
        await state.finish()

@dp.message_handler(state='*', commands=['cancel'])
async def cancel_handler(message: types.Message, state: FSMContext):
    """Allow user to cancel the operation"""
    current_state = await state.get_state()
    if current_state is None:
        return
    
    await state.finish()
    await message.reply('✅ Operation cancelled')


@dp.message_handler(commands=['start', 'help'])
async def start_help_handler(message: types.Message):
    """Display a welcome message and list all available commands"""
    help_text = (
        "🤖 *Welcome to the RSS Feed Bot!*\n\n"
        "Here are the available commands:\n\n"
        "🔹 */start* - Show this help message\n"
        "🔹 */help* - Show this help message\n"
        "🔹 */add_feed <URL> <INTERVAL_SECONDS>* - Add a new RSS feed to monitor\n"
        "🔹 */stop_feed <URL>* - Stop monitoring a feed\n"
        "🔹 */get_seen_posts <feed_url> <amount|date>* - Fetch seen posts for a feed\n"
        "🔹 */get_active_feeds* - List all active feeds\n\n"
        "🔹 */send_seen_to_target <post_hash>* - Allows to send seen post to target\n\n"
        "🔹 */debug_get_feed <URL>* - Fetch a random entry from a feed (debug)\n"
        "🔹 */debug_send_to_target <URL>* - Fetch a random entry from a feed and send to target (debug)\n"
    )
    if message.from_user.id == SUPER_ADMIN_ID:
        help_text += (
        "👥 *Admin only commands*\n\n"
        "🔹 */add_admin <user_id> <is_sa>* - Add new admin\n"
        "🔹 */remove_admin <user_id>* - Remove an admin\n\n"
        "🔹 */get_admins* - List all active admins\n\n"
        "🔹 */set_prompt <template>* - Change the GPT prompt template\n\n"
        "📝 *Examples:*\n"
        "- Add a feed: `/add_feed https://example.com/rss 3600`\n"
        "- Stop a feed: `/stop_feed https://example.com/rss`\n"
        "- Debug a feed: `/debug_get_feed https://example.com/rss`\n"
        "- Debug send to target: `/debug_send_to_target https://example.com/rss`\n"
        "- Get seen posts: `/get_seen_posts https://example.com/rss 5`\n"
        "- Get active feeds: `/get_active_feeds`\n\n"
        "⚠️ *Note:* All commands are restricted to the admin user."
    ) 
    else:
        help_text += (

        "📝 *Examples:*\n"
        "- Add a feed: `/add_feed https://example.com/rss 3600`\n"
        "- Stop a feed: `/stop_feed https://example.com/rss`\n"
        "- Debug a feed: `/debug_get_feed https://example.com/rss`\n"
        "- Debug send to target: `/debug_send_to_target https://example.com/rss`\n"
        "- Get seen posts: `/get_seen_posts https://example.com/rss 5`\n"
        "- Get active feeds: `/get_active_feeds`\n\n"
        "⚠️ *Note:* All commands are restricted to the admin user."
    )
    await message.reply(help_text, parse_mode="Markdown")

async def on_startup(dp):
    """Restart active feeds on startup"""
    active_feeds = db.get_active_feeds()
    for url, interval in active_feeds.items():
        task = asyncio.create_task(monitor_feed(url, interval))
        active_tasks[url] = task

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
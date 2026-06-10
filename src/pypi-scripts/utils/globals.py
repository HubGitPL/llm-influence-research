import os
from dotenv import load_dotenv
from aiohttp_client_cache import SQLiteBackend
from aiohttp_client_cache.session import CachedSession

SESSION: CachedSession
load_dotenv()
TOKEN = os.getenv("GITHUB_TOKEN", "")
CACHE = SQLiteBackend("github_cache.db")
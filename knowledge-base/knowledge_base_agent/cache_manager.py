import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any
from knowledge_base_agent.file_utils import safe_read_json, safe_write_json
import time
from knowledge_base_agent.tweet_utils import parse_tweet_id_from_url
from knowledge_base_agent.playwright_fetcher import fetch_tweet_data_playwright
from knowledge_base_agent.exceptions import StorageError
from knowledge_base_agent.config import Config
import aiohttp

# Default location for the tweet cache file
DEFAULT_CACHE_FILE = Path("data/tweet_cache.json")

def load_cache(cache_file: Optional[Path] = None) -> Dict[str, Any]:
    """
    Load the tweet cache from disk. Returns a dictionary mapping tweet IDs to cached data.
    If the file does not exist or is empty, return an empty dict.
    """
    cache_file = cache_file or DEFAULT_CACHE_FILE
    return safe_read_json(cache_file)

def save_cache(cache: Dict[str, Any], cache_file: Optional[Path] = None) -> None:
    """
    Save the tweet cache dictionary to disk.
    """
    cache_file = cache_file or DEFAULT_CACHE_FILE
    safe_write_json(cache_file, cache)

def get_cached_tweet(tweet_id: str, tweet_cache: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Get cached tweet data if it exists."""
    try:
        if tweet_id in tweet_cache:
            data = tweet_cache[tweet_id]
            # Ensure data is a dictionary
            if isinstance(data, list):
                # Convert list to dictionary if needed
                return {
                    "full_text": data[0] if data else "",
                    "media": data[1] if len(data) > 1 else [],
                    "downloaded_media": data[2] if len(data) > 2 else [],
                    "image_descriptions": data[3] if len(data) > 3 else []
                }
            return data
        return None
    except Exception as e:
        logging.error(f"Error retrieving cached tweet {tweet_id}: {e}")
        return None

def update_cache(tweet_id: str, tweet_data: Dict[str, Any], cache: Dict[str, Any]) -> None:
    """
    Update the cache with new tweet data for the given tweet_id.
    """
    cache[tweet_id] = tweet_data
    logging.info(f"Updated cache for tweet ID {tweet_id}.")

def clear_cache(cache_file: Optional[Path] = None) -> None:
    """
    Clear the entire cache by writing an empty dictionary.
    """
    save_cache({}, cache_file)
    logging.info("Cache cleared.")

class CacheManager:
    def __init__(self, cache_file: Path, expiry: int = 86400):
        self.cache_file = cache_file
        self.expiry = expiry
        self._cache = {}
        self._load_cache()

    def is_cached(self, key: str) -> bool:
        if key not in self._cache:
            return False
        timestamp = self._cache[key].get('timestamp', 0)
        return (time.time() - timestamp) < self.expiry

    async def get_or_fetch(self, key: str, fetch_func) -> Any:
        if self.is_cached(key):
            return self._cache[key]['data']
        data = await fetch_func()
        self._cache[key] = {
            'data': data,
            'timestamp': time.time()
        }
        await self._save_cache()
        return data

async def cache_tweet_data(tweet_url: str, config: Config, tweet_cache: Dict[str, Any], http_client) -> None:
    """Pre-fetch and cache tweet data for a tweet URL."""
    try:
        tweet_id = parse_tweet_id_from_url(tweet_url)
        if not tweet_id:
            return

        # If not in cache, fetch and store
        if tweet_id not in tweet_cache:
            tweet_data = await fetch_tweet_data_playwright(tweet_id)
            if tweet_data:
                tweet_cache[tweet_id] = tweet_data
                await save_cache(tweet_cache, config.tweet_cache_file)  # Use config path
                logging.info(f"Updated cache for tweet ID {tweet_id}.")

        # Download and process media if present
        if tweet_cache[tweet_id].get('media'):
            media_paths = []
            for img_url in tweet_cache[tweet_id]['media']:
                media_path = await download_media(img_url, tweet_id, config.media_cache_dir)
                if media_path:
                    media_paths.append(media_path)
            
            # Update cache with downloaded media paths
            tweet_cache[tweet_id]['downloaded_media'] = media_paths
            await save_cache(tweet_cache, config.tweet_cache_file)

        logging.info(f"Cached tweet data for {tweet_id}")

    except Exception as e:
        logging.error(f"Failed to cache tweet data: {e}")

async def download_media(url: str, tweet_id: str, media_cache_dir: Path) -> Optional[Path]:
    """Download media from URL to the media cache directory."""
    try:
        media_cache_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{tweet_id}_{url.split('/')[-1]}"
        media_path = media_cache_dir / filename
        
        if media_path.exists():
            return media_path
            
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    content = await response.read()
                    media_path.write_bytes(content)
                    return media_path
                    
    except Exception as e:
        logging.error(f"Failed to download media from {url}: {e}")
        return None

__all__ = ['load_cache', 'save_cache', 'get_cached_tweet', 'update_cache', 'clear_cache', 'cache_tweet_data']

import json
import asyncio
import aiofiles
from pathlib import Path
from typing import Set, Dict, Any, List
import logging
from knowledge_base_agent.exceptions import StateError, StateManagerError
import tempfile
import os
import shutil
from knowledge_base_agent.config import Config
from knowledge_base_agent.file_utils import async_write_text, async_json_load, async_json_dump
from knowledge_base_agent.tweet_utils import parse_tweet_id_from_url, load_tweet_urls_from_links

class StateManager:
    def __init__(self, config: Config, processed_file: Path, unprocessed_file: Path, bookmarks_file: Path):
        self.config = config
        self._initialized = False
        self.processed_tweets = set()
        self.unprocessed_tweets: Set[str] = set()
        self._lock = asyncio.Lock()
        self.processed_file = processed_file
        self.unprocessed_file = unprocessed_file
        self.bookmarks_file = bookmarks_file
        
    async def initialize(self) -> None:
        """Initialize the state manager."""
        try:
            logging.info("Starting state manager initialization")
            
            # Ensure unprocessed tweets file exists
            try:
                if not self.config.unprocessed_tweets_file.parent.exists():
                    logging.debug(f"Creating directory: {self.config.unprocessed_tweets_file.parent}")
                    self.config.unprocessed_tweets_file.parent.mkdir(parents=True, exist_ok=True)
                
                if not self.config.unprocessed_tweets_file.exists():
                    logging.debug(f"Creating file at: {self.config.unprocessed_tweets_file}")
                    filepath = str(self.config.unprocessed_tweets_file)
                    logging.debug(f"Writing to filepath: {filepath}")
                    await async_write_text("[]", filepath)
                
                # Ensure processed tweets file exists
                if not self.config.processed_tweets_file.exists():
                    logging.debug(f"Creating file at: {self.config.processed_tweets_file}")
                    filepath = str(self.config.processed_tweets_file)
                    logging.debug(f"Writing to filepath: {filepath}")
                    await async_write_text("[]", filepath)
                
            except Exception as e:
                logging.exception("Failed during file creation")
                raise StateError(f"Failed to create state files: {str(e)}")
            
            # Load processed tweets
            try:
                logging.debug("Loading processed tweets")
                self.processed_tweets = await self.load_processed_tweets()
            except Exception as e:
                logging.exception("Failed to load processed tweets")
                raise StateError(f"Failed to load processed tweets: {str(e)}")
            
            # Load current unprocessed tweets
            logging.debug("Loading unprocessed tweets")
            unprocessed_data = await async_json_load(self.unprocessed_file, default=[])
            self.unprocessed_tweets = set(unprocessed_data)
            
            # Read bookmarks and update unprocessed tweets
            await self.update_from_bookmarks()
            
            self._initialized = True
            logging.info("State manager initialization complete")
            
        except Exception as e:
            logging.exception("State manager initialization failed")
            raise StateError(f"Failed to initialize state manager: {str(e)}")

    async def _atomic_write_json(self, data: Dict[str, Any], filepath: Path) -> None:
        """Write JSON data atomically using a temporary file."""
        temp_file = None
        try:
            # Create temporary file in the same directory
            temp_fd, temp_path = tempfile.mkstemp(dir=filepath.parent)
            os.close(temp_fd)
            temp_file = Path(temp_path)
            
            # Write to temporary file
            async with aiofiles.open(temp_file, 'w') as f:
                await f.write(json.dumps(data, indent=2))
            
            # Atomic rename
            shutil.move(str(temp_file), str(filepath))
            
        except Exception as e:
            if temp_file and temp_file.exists():
                temp_file.unlink()
            raise StateError(f"Failed to write state file: {filepath}") from e

    async def _load_processed_tweets(self) -> None:
        """Load processed tweets from state file."""
        try:
            if self.config.processed_tweets_file.exists():
                async with aiofiles.open(self.config.processed_tweets_file, 'r') as f:
                    content = await f.read()
                    self.processed_tweets = set(json.loads(content))
        except Exception as e:
            logging.error(f"Failed to load processed tweets: {e}")
            self.processed_tweets = set()

    async def mark_tweet_processed(self, tweet_id: str) -> None:
        """Mark a tweet as processed and update both sets."""
        async with self._lock:
            try:
                self.processed_tweets.add(tweet_id)
                self.unprocessed_tweets.discard(tweet_id)
                
                # Save both states
                await self._atomic_write_json(
                    list(self.processed_tweets),
                    self.config.processed_tweets_file
                )
                await self._atomic_write_json(
                    list(self.unprocessed_tweets),
                    self.unprocessed_file
                )
                logging.info(f"Marked tweet {tweet_id} as processed")
            except Exception as e:
                logging.exception(f"Failed to mark tweet {tweet_id} as processed")
                raise StateError(f"Failed to update processing state: {e}")

    async def is_processed(self, tweet_id: str) -> bool:
        """Check if a tweet has been processed."""
        async with self._lock:
            return tweet_id in self.processed_tweets

    async def get_unprocessed_tweets(self, all_tweets: Set[str]) -> Set[str]:
        """Get set of unprocessed tweets."""
        async with self._lock:
            return all_tweets - self.processed_tweets

    async def clear_state(self) -> None:
        """Clear all state (useful for testing or reset)."""
        async with self._lock:
            self.processed_tweets.clear()
            await self._atomic_write_json([], self.config.processed_tweets_file)

    async def load_processed_tweets(self) -> set:
        """Load processed tweets from state file."""
        try:
            if self.config.processed_tweets_file.exists():
                data = await async_json_load(str(self.config.processed_tweets_file))
                return set(data)
            return set()
        except Exception as e:
            logging.error(f"Failed to load processed tweets: {e}")
            return set()

    async def update_from_bookmarks(self) -> None:
        """Update unprocessed tweets from bookmarks file."""
        try:
            # Read bookmarks file using correct function name
            bookmark_urls = load_tweet_urls_from_links(self.config.bookmarks_file)
            
            # Extract tweet IDs and filter out already processed ones
            new_unprocessed = set()
            for url in bookmark_urls:
                tweet_id = parse_tweet_id_from_url(url)
                if tweet_id and tweet_id not in self.processed_tweets:
                    new_unprocessed.add(tweet_id)
            
            # Update unprocessed set and save
            if new_unprocessed:
                self.unprocessed_tweets.update(new_unprocessed)
                await self.save_unprocessed()
                logging.info(f"Added {len(new_unprocessed)} new tweets to process")
            else:
                logging.info("No new tweets to process")

        except Exception as e:
            logging.error(f"Failed to update from bookmarks: {e}")
            raise StateManagerError(f"Failed to update from bookmarks: {e}")

    async def save_unprocessed(self) -> None:
        """Save unprocessed tweets to file."""
        try:
            await async_json_dump(list(self.unprocessed_tweets), self.unprocessed_file)
        except Exception as e:
            logging.error(f"Failed to save unprocessed tweets: {e}")
            raise StateManagerError(f"Failed to save unprocessed state: {e}")

    def get_unprocessed_tweets(self) -> List[str]:
        """Get list of unprocessed tweet IDs."""
        return list(self.unprocessed_tweets)

    def is_tweet_processed(self, tweet_id: str) -> bool:
        """Check if a tweet has been processed."""
        return tweet_id in self.processed_tweets

"""
Main agent module coordinating knowledge base operations.
"""

import logging
from typing import Set, List, Dict, Any
from pathlib import Path
import asyncio
from datetime import datetime
import time
import aiofiles

from knowledge_base_agent.config import Config
from knowledge_base_agent.exceptions import AgentError, MarkdownGenerationError
from knowledge_base_agent.state_manager import StateManager
from knowledge_base_agent.git_helper import GitSyncHandler
from knowledge_base_agent.fetch_bookmarks import BookmarksFetcher
from knowledge_base_agent.markdown_writer import MarkdownWriter
from knowledge_base_agent.readme_generator import generate_root_readme, generate_static_root_readme
from knowledge_base_agent.category_manager import CategoryManager
from knowledge_base_agent.types import TweetData, KnowledgeBaseItem
from knowledge_base_agent.prompts import UserPreferences
from knowledge_base_agent.progress import ProcessingStats
from knowledge_base_agent.content_processor import ContentProcessingError
from knowledge_base_agent.tweet_utils import parse_tweet_id_from_url
from knowledge_base_agent.file_utils import async_json_load
from knowledge_base_agent.http_client import HTTPClient
from knowledge_base_agent.content_processor import ContentProcessor
from knowledge_base_agent.tweet_cacher import TweetCacheValidator

def setup_logging(config: Config) -> None:
    """Configure logging with different levels for file and console."""
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    
    file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_formatter = logging.Formatter('%(message)s')

    file_handler = logging.FileHandler(config.log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    
    class TweetProgressFilter(logging.Filter):
        def filter(self, record):
            msg = record.getMessage()
            return not any(x in msg for x in [
                'Caching data for',
                'Tweet caching completed',
                '✓ Cached tweet'
            ])
    
    console_handler.addFilter(TweetProgressFilter())

    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('playwright').setLevel(logging.WARNING)

class KnowledgeBaseAgent:
    """Main agent coordinating knowledge base operations."""
    
    def __init__(self, config: Config):
        """Initialize the agent with configuration."""
        self.config = config
        self.http_client = HTTPClient(config)
        self.state_manager = StateManager(config)
        self.category_manager = CategoryManager(config, http_client=self.http_client)
        self.content_processor = ContentProcessor(config, http_client=self.http_client, state_manager=self.state_manager)
        self._processing_lock = asyncio.Lock()
        self.git_handler = GitSyncHandler(config)  # Use config directly
        self.stats = ProcessingStats(start_time=datetime.now())
        self._initialized = False
        logging.info("KnowledgeBaseAgent initialized")

    async def initialize(self) -> None:
        """Initialize all components and ensure directory structure."""
        try:
            logging.info("Creating required directories...")
            try:
                self.config.knowledge_base_dir.mkdir(parents=True, exist_ok=True)
                logging.debug(f"Created knowledge base dir: {self.config.knowledge_base_dir}")
                self.config.data_processing_dir.mkdir(parents=True, exist_ok=True)
                logging.debug(f"Created processing dir: {self.config.data_processing_dir}")
                self.config.media_cache_dir.mkdir(parents=True, exist_ok=True)
                logging.debug(f"Created media cache dir: {self.config.media_cache_dir}")
            except Exception as e:
                logging.exception("Failed to create directories")
                raise AgentError(f"Failed to create directories: {e}")
            
            logging.info("Initializing state manager...")
            try:
                await self.state_manager.initialize()
            except Exception as e:
                logging.exception("State manager initialization failed")
                raise AgentError(f"State manager initialization failed: {e}")
            
            logging.info("Initializing category manager...")
            try:
                await self.category_manager.initialize()
            except Exception as e:
                logging.exception("Category manager initialization failed")
                raise AgentError(f"Category manager initialization failed: {e}")
            
            self._initialized = True
            logging.info("Agent initialization complete")
        except Exception as e:
            logging.exception(f"Agent initialization failed: {str(e)}")
            raise AgentError(f"Failed to initialize agent: {str(e)}") from e

    async def process_bookmarks(self) -> None:
        """Process bookmarks with detailed error logging."""
        try:
            logging.info("Starting bookmark processing")
            bookmark_fetcher = BookmarksFetcher(self.config)
            try:
                logging.debug("Initializing browser")
                await bookmark_fetcher.initialize()
                logging.debug("Fetching bookmarks")
                bookmarks = await bookmark_fetcher.fetch_bookmarks()
                logging.info(f"Fetched {len(bookmarks)} bookmarks")
                for bookmark in bookmarks:
                    try:
                        start_time = time.time()
                        await self.process_tweet(bookmark)
                        self.stats.success_count += 1
                        self.stats.add_processing_time(time.time() - start_time)
                    except Exception as e:
                        self.stats.error_count += 1
                        logging.error(f"Failed to process bookmark {bookmark}: {e}")
            except Exception as e:
                logging.exception("Bookmark fetching failed")
                raise AgentError(f"Failed to fetch bookmarks: {str(e)}")
            finally:
                logging.debug("Cleaning up bookmark fetcher")
                await bookmark_fetcher.cleanup()
        except Exception as e:
            logging.exception("Bookmark processing failed")
            raise AgentError(f"Failed to process bookmarks: {str(e)}")

    async def update_indexes(self) -> None:
        """Update category indexes."""
        try:
            logging.info("Starting index update")
            categories = self.category_manager.get_all_categories()
            for category in categories:
                try:
                    pass  # Category-specific processing
                except Exception as e:
                    logging.error(f"Failed to process category {category}: {e}")
            logging.info("Index update completed")
        except Exception as e:
            logging.error(f"Index update failed: {e}")
            raise AgentError("Failed to update indexes") from e

    async def sync_changes(self) -> None:
        """Sync changes to GitHub repository."""
        try:
            logging.info("Starting GitHub sync...")
            await self.git_handler.sync_to_github("Update knowledge base content")
            logging.info("GitHub sync completed successfully")
        except Exception as e:
            logging.error(f"GitHub sync failed: {str(e)}")
            raise AgentError("Failed to sync changes to GitHub") from e

    async def cleanup(self) -> None:
        """Cleanup temporary files and resources."""
        try:
            temp_files = list(self.config.data_processing_dir.glob("*.temp"))
            for temp_file in temp_files:
                temp_file.unlink()
            logging.info("Cleanup completed")
        except Exception as e:
            logging.warning(f"Cleanup failed: {e}")

    async def run(self, preferences: UserPreferences) -> None:
        """Run the agent with the given preferences."""
        try:
            stats = ProcessingStats(start_time=datetime.now())
            
            logging.info("1. Initializing state and checking for new content...")
            # State manager will handle validation internally
            await self.state_manager.initialize()
            
            # Get validation stats from state manager
            stats.validation_count = getattr(self.state_manager, 'validation_fixes', 0)
            
            unprocessed_tweets = await self.state_manager.get_unprocessed_tweets()
            has_work_to_do = bool(unprocessed_tweets)
            
            if preferences.update_bookmarks:
                logging.info("2. Processing bookmarks for new tweets...")
                await self.process_bookmarks()
                await self.state_manager.update_from_bookmarks()
                unprocessed_tweets = await self.state_manager.get_unprocessed_tweets()
            
            if has_work_to_do:
                logging.info(f"Processing {len(unprocessed_tweets)} tweets...")
                await self.content_processor.process_all_tweets(
                    preferences,
                    unprocessed_tweets,
                    stats.validation_count,
                    stats,
                    self.category_manager
                )
            else:
                logging.info("No tweets to process")
            
            logging.info("3. Regenerating README...")
            await self.regenerate_readme()
            self.stats.readme_generated = True
            
            logging.info("4. Syncing to GitHub...")
            if self.config.git_enabled:
                await self.git_handler.sync_to_github("Update knowledge base with new items and README")
            
            # Before final summary
            logging.info("Finalizing state...")
            await self.state_manager.cleanup_unprocessed_tweets()
            
            logging.info("\n=== Processing Summary ===")
            logging.info(f"Cache validation fixes: {stats.validation_count}")
            logging.info(f"Total tweets processed: {stats.processed_count}")
            logging.info(f"Media items processed: {stats.media_processed}")
            logging.info(f"Categories processed: {stats.categories_processed}")
            logging.info(f"README generated: {'Yes' if self.stats.readme_generated else 'No'}")
            logging.info(f"Errors encountered: {stats.error_count}")
            
            stats.save_report(Path("data/processing_stats.json"))
            
            if stats.error_count > 0:
                raise RuntimeError(f"Failed to process {stats.error_count} tweets. Check logs for details.")
                
        except Exception as e:
            logging.error(f"Agent run failed: {str(e)}")
            raise
        finally:
            logging.info(f"Final state: {len(self.state_manager.unprocessed_tweets)} unprocessed, {len(self.state_manager.processed_tweets)} processed")

    async def process_tweet(self, tweet_url: str) -> None:
        """Process a single tweet."""
        try:
            if tweet_url.isdigit():
                tweet_url = f"https://twitter.com/i/web/status/{tweet_url}"
            
            tweet_id = parse_tweet_id_from_url(tweet_url)
            if not tweet_id:
                raise ValueError(f"Invalid tweet URL: {tweet_url}")
            
            tweet_data = await self.state_manager.get_tweet(tweet_id)
            if not tweet_data:
                logging.info(f"Tweet {tweet_id} not found in cache, fetching data...")
                await self.tweet_processor.cache_tweet_data(tweet_id)
                tweet_data = await self.state_manager.get_tweet(tweet_id)
                if not tweet_data:
                    raise ContentProcessingError(f"Failed to fetch and cache tweet {tweet_id}")
            
            logging.info(f"Processing tweet {tweet_id}")
            kb_item = await self.content_processor.create_knowledge_base_item(tweet_id, tweet_data)
            
            if await self._verify_kb_item_created(tweet_id):
                await self.state_manager.mark_tweet_processed(tweet_id, tweet_data)
                logging.info(f"Successfully processed tweet {tweet_id}")
            else:
                raise ContentProcessingError(f"Failed to create knowledge base item for tweet {tweet_id}")
            
        except Exception as e:
            logging.error(f"Failed to process tweet {tweet_url}: {e}")
            raise

    async def regenerate_readme(self) -> None:
        """Regenerate the root README file."""
        try:
            logging.info("Starting README regeneration")
            readme_path = self.config.knowledge_base_dir / "README.md"
            
            try:
                await generate_root_readme(
                    self.config.knowledge_base_dir,
                    self.category_manager,
                    self.http_client,
                    self.config
                )
                logging.info("Generated intelligent README")
            except Exception as e:
                logging.warning(f"Intelligent README generation failed: {e}")
                content = await generate_static_root_readme(
                    self.config.knowledge_base_dir,
                    self.category_manager
                )
                async with aiofiles.open(readme_path, 'w', encoding='utf-8') as f:
                    await f.write(content)
                logging.info("Generated static README as fallback")
                
        except Exception as e:
            logging.error(f"README regeneration failed: {e}")
            raise MarkdownGenerationError(f"Failed to regenerate README: {e}")

    async def _verify_tweet_cached(self, tweet_id: str) -> bool:
        """Verify that a tweet exists in the cache."""
        try:
            cache_file = Path(self.config.tweet_cache_file)
            if not cache_file.exists():
                logging.error("Tweet cache file does not exist")
                return False
            
            cache_data = await async_json_load(cache_file)
            return tweet_id in cache_data
            
        except Exception as e:
            logging.error(f"Error verifying tweet cache for {tweet_id}: {e}")
            return False

    async def _verify_kb_item_created(self, tweet_id: str) -> bool:
        """Verify that a knowledge base item was created for the tweet."""
        try:
            cache_file = Path(self.config.tweet_cache_file)
            if not cache_file.exists():
                logging.error("Tweet cache file does not exist")
                return False
            
            cache_data = await async_json_load(cache_file)
            if tweet_id not in cache_data:
                logging.error(f"Tweet {tweet_id} not found in cache")
                return False
            
            tweet_data = cache_data[tweet_id]
            if 'kb_item_path' in tweet_data:
                kb_path = Path(tweet_data['kb_item_path'])
                if kb_path.exists():
                    return True
            
            logging.error(f"No knowledge base item found for tweet {tweet_id}")
            return False
            
        except Exception as e:
            logging.error(f"Error verifying KB item for tweet {tweet_id}: {e}")
            return False

    async def _count_media_items(self) -> int:
        """Count total media items that need processing."""
        try:
            cache_data = await async_json_load(self.config.tweet_cache_file)
            return sum(len(tweet_data.get('media', [])) for tweet_data in cache_data.values())
        except Exception:
            return 0

    async def process_tweets(self, tweet_urls: List[str]) -> None:
        """Process tweets while preserving existing cache."""
        if not self._initialized:
            await self.initialize()

        stats = ProcessingStats(datetime.now())
        
        for tweet_url in tweet_urls:
            tweet_id = parse_tweet_id_from_url(tweet_url)
            if not tweet_id:
                continue

            if await self.state_manager.is_tweet_processed(tweet_id) and not self.config.force_update:
                logging.info(f"Tweet {tweet_id} already processed, skipping")
                stats.skipped_count += 1
                continue

            try:
                cached_data = await self.state_manager.get_tweet_cache(tweet_id)
                if cached_data and not self.config.force_update:
                    tweet_data = cached_data
                    stats.cache_hits += 1
                else:
                    tweet_data = await self._fetch_tweet_data(tweet_url)
                    await self.state_manager.save_tweet_cache(tweet_id, tweet_data)
                    stats.cache_misses += 1
            except Exception as e:
                logging.error(f"Failed to process tweet {tweet_id}: {e}")
                stats.error_count += 1
                continue
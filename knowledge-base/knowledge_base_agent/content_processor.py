from pathlib import Path
from typing import Dict, Any, Tuple, Optional, List
import logging
from datetime import datetime
from knowledge_base_agent.exceptions import StorageError, ContentProcessingError, ContentGenerationError, CategoryGenerationError, KnowledgeBaseItemCreationError
from knowledge_base_agent.category_manager import CategoryManager
from knowledge_base_agent.config import Config
from knowledge_base_agent.ai_categorization import classify_content, generate_content_name
from knowledge_base_agent.tweet_utils import sanitize_filename
from knowledge_base_agent.image_interpreter import interpret_image
from knowledge_base_agent.http_client import HTTPClient
from knowledge_base_agent.file_utils import async_json_load, async_json_dump
from knowledge_base_agent.state_manager import StateManager
from knowledge_base_agent.types import TweetData, KnowledgeBaseItem, CategoryInfo
import json
import re
from knowledge_base_agent.progress import ProcessingStats
from knowledge_base_agent.prompts import UserPreferences
from knowledge_base_agent.playwright_fetcher import fetch_tweet_data_playwright, expand_url  # Added expand_url import
from dataclasses import dataclass
from knowledge_base_agent.markdown_writer import generate_root_readme, MarkdownWriter

@dataclass
class ProcessingStats:
    media_processed: int = 0
    categories_processed: int = 0
    processed_count: int = 0
    error_count: int = 0
    readme_generated: bool = False

async def categorize_and_name_content(
    ollama_url: str,
    text: str,
    text_model: str,
    tweet_id: str,
    category_manager: CategoryManager,
    http_client: HTTPClient
) -> Tuple[str, str, str]:
    """Categorize content and generate item name."""
    try:
        # Get categories using the category manager
        main_cat, sub_cat = await category_manager.classify_content(
            text=text,
            tweet_id=tweet_id
        )

        # Normalize categories
        main_cat = main_cat.lower().replace(' ', '_')
        sub_cat = sub_cat.lower().replace(' ', '_')
        
        # Generate item name
        item_name = await category_manager.generate_item_name(
            text=text,
            main_category=main_cat,
            sub_category=sub_cat,
            tweet_id=tweet_id
        )
        
        # Ensure category exists
        if not category_manager.category_exists(main_cat, sub_cat):
            category_manager.add_category(main_cat, sub_cat)
            
        return main_cat, sub_cat, item_name
        
    except Exception as e:
        logging.error(f"Failed to categorize content for tweet {tweet_id}: {e}")
        raise CategoryGenerationError(f"Failed to categorize content for tweet {tweet_id}: {e}")

async def process_media_content(
    tweet_data: Dict[str, Any],
    http_client: HTTPClient,
    config: Config
) -> Dict[str, Any]:
    """Process media content for a tweet."""
    try:
        if tweet_data.get('media_processed', False):
            logging.info("Media already processed, skipping...")
            return tweet_data

        media_paths = tweet_data.get('downloaded_media', [])
        if not media_paths:
            tweet_data['media_processed'] = True
            return tweet_data

        image_descriptions = []
        for media_path in media_paths:
            if not Path(media_path).exists():
                raise ContentProcessingError(f"Media file not found: {media_path}")

            description = await interpret_image(
                http_client=http_client,
                image_path=Path(media_path),
                vision_model=config.vision_model
            )
            if description:
                image_descriptions.append(description)

        # Update only media-related fields
        tweet_data['image_descriptions'] = image_descriptions
        tweet_data['media_processed'] = True
        return tweet_data

    except Exception as e:
        raise ContentProcessingError(f"Failed to process media content: {e}")

async def process_categories(
    tweet_id: str,
    tweet_data: Dict[str, Any],
    config: Config,
    http_client: HTTPClient,
    state_manager: Optional[StateManager] = None
) -> Dict[str, Any]:
    """Process and assign categories to a tweet."""
    try:
        # Skip if already categorized
        if tweet_data.get('categories_processed', False):
            logging.info(f"Categories already processed for tweet {tweet_id}, skipping...")
            return tweet_data

        # Ensure media is processed first
        if not tweet_data.get('media_processed', False):
            logging.info(f"Media not yet processed for tweet {tweet_id}, processing now...")
            tweet_data = await process_media_content(tweet_data, http_client, config)

        # Generate categories
        tweet_text = tweet_data.get('full_text', '')
        image_descriptions = tweet_data.get('image_descriptions', [])
        combined_text = f"{tweet_text}\n\nImage Descriptions:\n" + "\n".join(image_descriptions)

        category_manager = CategoryManager(config, http_client=http_client)
        main_cat, sub_cat, item_name = await categorize_and_name_content(
            ollama_url=config.ollama_url,
            text=combined_text,
            text_model=config.text_model,
            tweet_id=tweet_id,
            category_manager=category_manager,
            http_client=http_client
        )
        
        # Save categories
        tweet_data['categories'] = {
            'main_category': main_cat,
            'sub_category': sub_cat,
            'item_name': item_name,
            'model_used': config.text_model,
            'categorized_at': datetime.now().isoformat()
        }
        tweet_data['categories_processed'] = True

        # Update state if manager provided
        if state_manager:
            await state_manager.update_tweet_data(tweet_id, tweet_data)

        return tweet_data

    except Exception as e:
        logging.error(f"Failed to process categories for tweet {tweet_id}: {str(e)}")
        raise CategoryGenerationError(f"Failed to process categories: {str(e)}")

async def create_knowledge_base_entry(
    tweet_id: str,
    tweet_data: Dict[str, Any],
    config: Config,
    http_client: HTTPClient,
    state_manager: Optional[StateManager] = None
) -> None:
    """Create a knowledge base entry for a tweet."""
    try:
        logging.info(f"Starting knowledge base entry creation for tweet {tweet_id}")
        
        # Process media content first
        logging.info(f"Processing media content for tweet {tweet_id}")
        try:
            tweet_data = await process_media_content(tweet_data, http_client, config)
            logging.info(f"Successfully processed media for tweet {tweet_id}")
        except Exception as e:
            logging.error(f"Failed to process media content for tweet {tweet_id}: {str(e)}")
            raise

        # Combine tweet text and image descriptions
        content_text = tweet_data.get('full_text', '')
        image_descriptions = tweet_data.get('image_descriptions', [])
        combined_text = f"{content_text}\n\n" + "\n".join(image_descriptions) if image_descriptions else content_text

        if not combined_text:
            raise ContentProcessingError(f"No content found for tweet {tweet_id}")

        # Use cached or generate categories
        categories = tweet_data.get('categories')
        if not categories:
            try:
                category_manager = CategoryManager(config, http_client=http_client)
            except Exception as e:
                logging.error(f"Failed to initialize CategoryManager: {e}")
                raise ContentProcessingError(f"Category manager initialization failed: {e}")
            
            main_cat, sub_cat, item_name = await categorize_and_name_content(
                ollama_url=config.ollama_url,
                text=combined_text,
                text_model=config.text_model,
                tweet_id=tweet_id,
                category_manager=category_manager,
                http_client=http_client
            )
            
            categories = {
                'main_category': main_cat,
                'sub_category': sub_cat,
                'item_name': item_name,
                'model_used': config.text_model,
                'categorized_at': datetime.now().isoformat()
            }
            tweet_data['categories'] = categories
            if state_manager:
                await state_manager.update_tweet_data(tweet_id, tweet_data)
        else:
            logging.info(f"Using cached categories for tweet {tweet_id}: {categories}")
            main_cat = categories['main_category']
            sub_cat = categories['sub_category']
            item_name = categories['item_name']
        
        # Extract necessary data
        content_text = tweet_data.get('full_text', '')
        tweet_url = tweet_data.get('tweet_url', '')
        image_files = [Path(p) for p in tweet_data.get('downloaded_media', [])]
        image_descriptions = tweet_data.get('image_descriptions', [])
        
        logging.info(f"Preparing to write markdown for tweet {tweet_id}")
        logging.info(f"Categories: {main_cat}/{sub_cat}/{item_name}")
        logging.info(f"Content length: {len(content_text)}")
        logging.info(f"Number of images: {len(image_files)}")
        logging.info(f"Tweet data keys: {list(tweet_data.keys())}")
        
        if not content_text:
            raise ContentProcessingError(f"No text content found for tweet {tweet_id}")
            
        # Write markdown content
        try:
            # Import here to avoid circular import
            from knowledge_base_agent.markdown_writer import MarkdownWriter
            
            markdown_writer = MarkdownWriter(config)
            await markdown_writer.write_tweet_markdown(
                config.knowledge_base_dir,
                tweet_id=tweet_id,
                tweet_data=tweet_data,
                image_files=image_files,
                image_descriptions=image_descriptions,
                main_category=main_cat,
                sub_category=sub_cat,
                item_name=item_name,
                tweet_text=content_text,
                tweet_url=tweet_url
            )
            logging.info(f"Successfully wrote markdown for tweet {tweet_id}")
        except Exception as e:
            logging.error(f"Failed to write markdown for tweet {tweet_id}: {str(e)}")
            raise
        
        logging.info(f"Successfully created knowledge base entry for tweet {tweet_id}")
        
    except Exception as e:
        logging.error(f"Failed to create knowledge base entry for {tweet_id}: {str(e)}")
        raise StorageError(f"Failed to create knowledge base entry: {e}")

class ContentProcessor:
    """Handles processing of tweet content into knowledge base items."""
    
    def __init__(self, config: Config, http_client: HTTPClient):
        """Initialize the content processor."""
        self.config = config
        self.http_client = http_client
        self.category_manager = CategoryManager(config, http_client=http_client)  # Pass http_client here
        self.state_manager = StateManager(config)
        self.stats = ProcessingStats()
        self.text_model = self.http_client.config.text_model
        logging.info(f"Initialized ContentProcessor with model: {self.text_model}")

    @classmethod
    async def create_knowledge_base_entry(cls, tweet_data: Dict[str, Any], http_client: HTTPClient, tweet_cache: Dict[str, Any]) -> KnowledgeBaseItem:
        """Factory method to create a knowledge base entry from tweet data."""
        # Get tweet ID from the cache key instead of looking inside tweet_data
        tweet_id = next((k for k, v in tweet_cache.items() if v is tweet_data), None)
        if not tweet_id:
            logging.error("Could not find tweet ID in cache")
            raise ValueError("Tweet data not found in cache")
        
        try:
            # Log the raw tweet data structure
            logging.debug(f"Raw tweet data for {tweet_id}: {tweet_data}")
            
            # Validate tweet data
            if not isinstance(tweet_data, dict):
                raise ValueError(f"Tweet data must be a dictionary, got {type(tweet_data)}")
            
            # Create processor instance
            try:
                processor = cls(http_client.config, http_client)  # Updated to pass config explicitly
                logging.info(f"Created processor instance for tweet {tweet_id}")
            except Exception as e:
                logging.error(f"Failed to create processor instance for tweet {tweet_id}: {str(e)}")
                raise
            
            # Create knowledge base item
            try:
                kb_item = await processor.create_knowledge_base_item(tweet_id, tweet_data, processor.config)
                logging.info(f"Successfully created knowledge base item for tweet {tweet_id}")
                return kb_item
            except Exception as e:
                logging.error(f"Failed in create_knowledge_base_item for tweet {tweet_id}: {str(e)}")
                raise
            
        except Exception as e:
            error_msg = f"Failed to create knowledge base entry for tweet {tweet_id}: {str(e)}"
            logging.error(error_msg)
            raise KnowledgeBaseItemCreationError(error_msg)

    async def generate_categories(self, tweet_text: str, tweet_id: str) -> CategoryInfo:
        """Generate category information from tweet text."""
        try:
            prompt = (
                "Analyze this tweet and provide category information in JSON format:\n\n"
                f"Tweet: {tweet_text}\n\n"
                "Required format:\n"
                "{\n"
                '  "category": "main technical category",\n'
                '  "subcategory": "specific technical subcategory",\n'
                '  "name": "concise_technical_name",\n'
                '  "description": "brief technical description"\n'
                "}\n\n"
                "Rules:\n"
                "- Categories should be technical and specific\n"
                "- Name should be 2-4 words, lowercase with underscores\n"
                "- Description should be 1-2 sentences\n"
            )
            logging.debug(f"Sending category generation prompt for tweet text: {tweet_text[:100]}...")
            # Use the rate-limited http_client
            response_text = await self.http_client.ollama_generate(
                model=self.text_model,
                prompt=prompt,
                temperature=0.7  # More deterministic for categories
            )
            
            if not response_text:
                raise CategoryGenerationError("Empty response from Ollama")
            
            try:
                # Try to parse the JSON response
                result = json.loads(response_text)
            except json.JSONDecodeError:
                # If JSON parsing fails, try to extract JSON from the response
                # Sometimes the model might include additional text
                json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
                if json_match:
                    try:
                        result = json.loads(json_match.group(0))
                    except json.JSONDecodeError as e:
                        logging.error(f"Failed to parse extracted JSON: {e}")
                        logging.error(f"Raw response: {response_text}")
                        raise CategoryGenerationError("Invalid JSON format in response")
                else:
                    logging.error(f"No JSON found in response: {response_text}")
                    raise CategoryGenerationError("No valid JSON found in response")
            
            # Validate required fields
            required_fields = ["category", "subcategory", "name", "description"]
            if not all(field in result for field in required_fields):
                missing_fields = [field for field in required_fields if field not in result]
                raise CategoryGenerationError(f"Missing required fields: {missing_fields}")
            
            # Normalize field values
            result["category"] = result["category"].lower().replace(" ", "_")
            result["subcategory"] = result["subcategory"].lower().replace(" ", "_")
            result["name"] = result["name"].lower().replace(" ", "_")
            
            logging.info(f"Successfully generated categories: {result}")
            return CategoryInfo(**result)
            
        except Exception as e:
            logging.error(f"Category generation failed: {str(e)}")
            logging.error(f"Tweet text: {tweet_text[:100]}...")
            raise CategoryGenerationError(f"Failed to generate categories: {str(e)}")

    async def generate_content(self, tweet_data: Dict[str, Any]) -> str:
        """Generate knowledge base content from tweet data."""
        try:
            # Prepare context including tweet text, URLs, and media descriptions
            context = f"Tweet: {tweet_data.get('full_text', '')}\n\n"  # Updated to full_text
            
            # Add URLs if present
            if tweet_data.get('urls'):
                context += "Related Links:\n"
                for url in tweet_data['urls']:
                    context += f"- {url}\n"
                context += "\n"
            
            # Add media descriptions if present
            if tweet_data.get('media'):
                context += "Media:\n"
                for i, media in enumerate(tweet_data['media'], 1):
                    if isinstance(media, dict) and media.get('alt_text'):
                        context += f"{i}. {media['alt_text']}\n"

            prompt = (
                f"Based on this content:\n\n{context}\n\n"
                "Generate a detailed technical knowledge base entry that:\n"
                "1. Explains the main technical concepts\n"
                "2. Provides relevant code examples if applicable\n"
                "3. Lists key points and takeaways\n"
                "4. Includes relevant technical details and references\n"
                "\nFormat in Markdown with proper headers and sections."
            )

            logging.debug(f"Sending content generation prompt: {prompt[:200]}...")
            
            content = await self.http_client.ollama_generate(
                model=self.text_model,
                prompt=prompt
            )
            
            if not content:
                raise ContentGenerationError("Generated content is empty")
            
            if len(content.strip()) < 50:
                raise ContentGenerationError("Generated content is too short")
            
            if not content.startswith('#'):
                content = f"# Technical Note\n\n{content}"
            
            logging.info(f"Successfully generated content of length: {len(content)}")
            return content.strip()
            
        except Exception as e:
            logging.error(f"Content generation failed: {str(e)}")
            raise ContentGenerationError(f"Failed to generate content: {str(e)}")

    async def create_knowledge_base_item(self, tweet_id: str, tweet_data: Dict[str, Any], config: Config) -> KnowledgeBaseItem:
        """Create a knowledge base item from a tweet."""
        try:
            # Get categories data
            categories = tweet_data.get('categories', {})
            
            # Prepare context for LLM
            context = {
                'tweet_text': tweet_data.get('full_text', ''),
                'urls': tweet_data.get('urls', []),
                'media_descriptions': tweet_data.get('image_descriptions', []),
                'main_category': categories.get('main_category', ''),
                'sub_category': categories.get('sub_category', ''),
                'item_name': categories.get('item_name', '')
            }
            
            # Generate comprehensive content using LLM
            prompt = (
                "As a technical knowledge base writer, create a comprehensive entry using this information:\n\n"
                f"Tweet: {context['tweet_text']}\n"
                f"Category: {context['main_category']}/{context['sub_category']}\n"
                f"Topic: {context['item_name']}\n\n"
                "Additional Context:\n"
                f"URLs: {', '.join(context['urls'])}\n"
                "Media Descriptions:\n" + 
                '\n'.join([f"- {desc}" for desc in context['media_descriptions']]) + "\n\n"
                "Generate a detailed technical knowledge base entry that includes:\n"
                "1. A clear title\n"
                "2. A concise description\n"
                "3. Detailed technical content with examples if applicable\n"
                "4. Key takeaways and best practices\n"
                "5. References to any tools or technologies mentioned\n"
                "\nFormat the response in markdown with appropriate sections."
            )
            
            # Generate content using LLM
            generated_content = await self.http_client.ollama_generate(
                model=self.text_model,
                prompt=prompt
            )
            
            if not generated_content:
                raise ContentGenerationError("Generated content is empty")
            
            # Parse generated content to extract title and description
            content_parts = generated_content.split('\n', 2)
            title = content_parts[0].lstrip('#').strip() if content_parts else context['item_name']
            description = content_parts[1].strip() if len(content_parts) > 1 else context['tweet_text'][:200]
            main_content = content_parts[2] if len(content_parts) > 2 else generated_content
            
            # Create CategoryInfo object
            category_info = CategoryInfo(
                main_category=str(categories.get('main_category', '')),
                sub_category=str(categories.get('sub_category', '')),
                item_name=str(categories.get('item_name', '')),
                description=description
            )
            
            # Get current timestamp
            current_time = datetime.now()
            
            # Create KnowledgeBaseItem with generated content
            kb_item = KnowledgeBaseItem(
                title=title,
                description=description,
                content=main_content,
                category_info=category_info,
                source_tweet={
                    'url': f"https://twitter.com/i/web/status/{tweet_id}",
                    'author': tweet_data.get('author', ''),
                    'created_at': current_time
                },
                media_urls=tweet_data.get('downloaded_media', []),
                image_descriptions=tweet_data.get('image_descriptions', []),
                created_at=current_time,
                last_updated=current_time
            )
            
            return kb_item
            
        except Exception as e:
            logging.error(f"Failed to create knowledge base item for tweet {tweet_id}: {e}")
            raise KnowledgeBaseItemCreationError(f"Failed to create knowledge base item: {str(e)}")

    async def process_media(self, tweet_data: Dict[str, Any]) -> None:
        """Process media content for a tweet."""
        try:
            if tweet_data.get('media_processed', False):
                logging.info("Media already processed, skipping...")
                return tweet_data

            media_paths = tweet_data.get('downloaded_media', [])
            if not media_paths:
                tweet_data['media_processed'] = True
                return tweet_data

            image_descriptions = []
            for media_path in media_paths:
                if not Path(media_path).exists():
                    raise ContentProcessingError(f"Media file not found: {media_path}")

                description = await interpret_image(
                    http_client=self.http_client,
                    image_path=Path(media_path),
                    vision_model=self.config.vision_model
                )
                if description:
                    image_descriptions.append(description)

            tweet_data['image_descriptions'] = image_descriptions
            tweet_data['media_processed'] = True
            return tweet_data

        except Exception as e:
            raise ContentProcessingError(f"Failed to process media content: {e}")

    async def process_all_tweets(
        self,
        preferences,
        unprocessed_tweets: List[str],
        total_tweets: int,
        stats: ProcessingStats,
        category_manager: CategoryManager
    ) -> None:
        """Process all tweets through the pipeline."""
        try:
            # Phase 1: Tweet Cache Initialization
            logging.info("=== Phase 1: Tweet Cache Initialization ===")
            unprocessed_tweets = await self.state_manager.get_unprocessed_tweets()
            total_tweets = len(unprocessed_tweets)
            
            # Cache all tweets first
            await self.cache_tweets(unprocessed_tweets)  # Simplified to use cache_tweets

            # Verify cache completion before proceeding
            tweets = await self.state_manager.get_all_tweets()
            if not tweets:
                logging.error("No tweets cached, stopping processing")
                return

            # Phase 2: Media Processing
            logging.info("=== Phase 2: Media Processing ===")
            for tweet_id, tweet_data in tweets.items():
                if not tweet_data.get('media_processed', False):
                    try:
                        updated_data = await self.process_media(tweet_data)
                        await self.state_manager.update_tweet_data(tweet_id, updated_data)
                        await self.state_manager.mark_media_processed(tweet_id)
                        stats.media_processed += len(tweet_data.get('downloaded_media', []))  # Updated to count downloaded media
                    except Exception as e:
                        logging.error(f"Failed to process media for tweet {tweet_id}: {e}")
                        stats.error_count += 1
                        continue

            # Verify media processing before proceeding
            if not all(tweet.get('media_processed', False) for tweet in tweets.values()):
                logging.warning("Media processing incomplete, proceeding anyway")

            # Phase 3: Category Processing
            logging.info("=== Phase 3: Category Processing ===")
            for tweet_id, tweet_data in tweets.items():
                if not tweet_data.get('categories_processed', False):
                    try:
                        updated_data = await self.category_manager.process_categories(tweet_id, tweet_data)
                        await self.state_manager.update_tweet_data(tweet_id, updated_data)
                        await self.state_manager.mark_categories_processed(tweet_id)
                        stats.categories_processed += 1
                    except Exception as e:
                        logging.error(f"Failed to process categories for tweet {tweet_id}: {e}")
                        stats.error_count += 1
                        continue

            # Verify category processing before proceeding
            if not all(tweet.get('categories_processed', False) for tweet in tweets.values()):
                logging.warning("Category processing incomplete, proceeding anyway")

            # Phase 4: Knowledge Base Creation
            logging.info("=== Phase 4: Knowledge Base Creation ===")
            for tweet_id, tweet_data in tweets.items():
                # Validate KB item existence first
                if tweet_data.get('kb_item_created', False):
                    kb_path = tweet_data.get('kb_item_path')
                    if not kb_path or not Path(kb_path).exists():
                        logging.warning(f"KB item marked as created but missing for tweet {tweet_id} at {kb_path}")
                        tweet_data['kb_item_created'] = False
                        await self.state_manager.update_tweet_data(tweet_id, tweet_data)

                # Continue with normal KB creation flow
                if not tweet_data.get('kb_item_created', False):
                    try:
                        # Create the knowledge base item
                        kb_item = await self.create_knowledge_base_item(tweet_id, tweet_data, self.config)
                        
                        # Initialize markdown writer and write the item
                        markdown_writer = MarkdownWriter(self.config)
                        kb_path = await markdown_writer.write_kb_item(
                            item=kb_item,
                            media_files=[Path(p) for p in tweet_data.get('downloaded_media', [])],
                            media_descriptions=tweet_data.get('image_descriptions', []),
                            root_dir=Path(self.config.knowledge_base_dir)
                        )
                        
                        # Update tweet data with KB creation status and path
                        tweet_data['kb_item_created'] = True
                        tweet_data['kb_item_path'] = str(kb_path)
                        await self.state_manager.update_tweet_data(tweet_id, tweet_data)
                        
                        # Move to processed tweets if all phases complete
                        if (tweet_data.get('media_processed', True) and 
                            tweet_data.get('categories_processed', True) and 
                            tweet_data.get('kb_item_created', True)):
                            await self.state_manager.mark_tweet_processed(tweet_id, tweet_data)
                            logging.info(f"Tweet {tweet_id} fully processed and moved to processed tweets")
                        else:
                            logging.warning(f"Tweet {tweet_id} has not completed all processing steps")
                            logging.warning("Missing steps: ")
                            if not tweet_data.get('media_processed', True):
                                logging.warning("- Media processing")
                            if not tweet_data.get('categories_processed', True):
                                logging.warning("- Category processing")
                            if not tweet_data.get('kb_item_created', True):
                                logging.warning("- KB item creation")
                        
                        stats.processed_count += 1
                        
                    except Exception as e:
                        logging.error(f"Failed to create KB item for tweet {tweet_id}: {e}")
                        stats.error_count += 1
                        continue

            # Verify KB creation before proceeding
            if not all(tweet.get('kb_item_created', False) for tweet in tweets.values()):
                logging.warning("Knowledge base creation incomplete, proceeding anyway")

            # Phase 5: README Generation
            logging.info("=== Phase 5: README Generation ===")
            kb_dir = Path(self.config.knowledge_base_dir)
            readme_path = kb_dir / "README.md"
            
            if not readme_path.exists():
                logging.info("Root README.md does not exist, generating...")
                try:
                    await generate_root_readme(
                        kb_dir=kb_dir,
                        category_manager=category_manager
                    )
                    logging.info("✓ Successfully generated root README.md")
                    stats.readme_generated = True
                except Exception as e:
                    logging.error(f"Failed to generate root README: {e}")
                    stats.error_count += 1
            else:
                logging.debug("Root README.md already exists, skipping generation")

        except Exception as e:
            logging.error(f"Failed to process all tweets: {str(e)}")
            raise ContentProcessingError(f"Failed to process all tweets: {str(e)}")

    async def cache_tweets(self, tweet_ids: List[str]) -> None:
        """Cache tweet data including expanded URLs and all media."""
        from urllib.parse import urlparse
        
        cached_tweets = await self.state_manager.get_all_tweets()

        for tweet_id in tweet_ids:
            try:
                # Check if tweet exists and is fully cached
                existing_tweet = cached_tweets.get(tweet_id, {})
                if existing_tweet and existing_tweet.get('cache_complete', False):
                    logging.info(f"Tweet {tweet_id} already fully cached, skipping...")
                    continue

                # If we have partial data, preserve it
                if existing_tweet:
                    logging.info(f"Found partial cache for tweet {tweet_id}, completing cache...")
                    tweet_data = existing_tweet
                else:
                    # Fetch new tweet data
                    tweet_url = f"https://twitter.com/i/web/status/{tweet_id}"
                    tweet_data = await fetch_tweet_data_playwright(tweet_url, self.config)
                    if not tweet_data:
                        logging.error(f"Failed to fetch tweet {tweet_id}")
                        continue

                # Expand URLs if present
                if 'urls' in tweet_data:
                    expanded_urls = []
                    for url in tweet_data.get('urls', []):
                        try:
                            expanded = await expand_url(url)  # Use playwright_fetcher.expand_url
                            expanded_urls.append(expanded)
                        except Exception as e:
                            logging.warning(f"Failed to expand URL {url}: {e}")
                            expanded_urls.append(url)  # Fallback to original
                    tweet_data['urls'] = expanded_urls

                # Download media if present and not already downloaded
                if 'media' in tweet_data and not tweet_data.get('downloaded_media'):
                    media_dir = Path(self.config.media_cache_dir) / tweet_id
                    media_dir.mkdir(parents=True, exist_ok=True)
                    
                    media_paths = []
                    for idx, media_item in enumerate(tweet_data['media']):
                        try:
                            # Extract URL and type from media item
                            if isinstance(media_item, dict):
                                url = media_item.get('url', '')
                                media_type = media_item.get('type', 'image')
                            else:
                                url = str(media_item)
                                media_type = 'image'  # Default to image
                                
                            if not url:
                                logging.warning(f"No valid URL in media item {idx} for tweet {tweet_id}: {media_item}")
                                continue

                            # Determine file extension
                            ext = '.mp4' if media_type == 'video' else (Path(urlparse(url).path).suffix or '.jpg')
                            media_path = media_dir / f"media_{idx}{ext}"
                            
                            # Download if not exists
                            if not media_path.exists():
                                logging.info(f"Downloading media from {url} to {media_path}")
                                await self.http_client.download_media(url, media_path)
                                logging.info(f"Successfully downloaded media to {media_path}")
                            else:
                                logging.debug(f"Media already exists at {media_path}, skipping download")
                            
                            media_paths.append(str(media_path))
                            
                        except Exception as e:
                            logging.error(f"Failed to process media item {idx} for tweet {tweet_id}: {e}")
                            continue

                    tweet_data['downloaded_media'] = media_paths
                    logging.info(f"Downloaded {len(media_paths)} media files for tweet {tweet_id}")

                # Mark as fully cached and save
                tweet_data['cache_complete'] = True
                await self.state_manager.update_tweet_data(tweet_id, tweet_data)
                logging.info(f"Cached tweet {tweet_id}: {len(tweet_data.get('urls', []))} URLs, {len(tweet_data.get('downloaded_media', []))} media items")

            except Exception as e:
                logging.error(f"Failed to cache tweet {tweet_id}: {e}")
                continue

    async def _count_media_items(self) -> int:
        """Count total number of media items to process."""
        tweets = await self.state_manager.get_all_tweets()
        count = 0
        for tweet_data in tweets.values():
            if not tweet_data.get('media_processed', False):
                count += len(tweet_data.get('media', []))
        return count

    async def get_tweets_with_media(self) -> Dict[str, Any]:
        """Get all tweets that have unprocessed media."""
        tweets = await self.state_manager.get_all_tweets()
        return {
            tweet_id: tweet_data 
            for tweet_id, tweet_data in tweets.items() 
            if tweet_data.get('media', []) and not tweet_data.get('media_processed', False)
        }
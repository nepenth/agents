from datetime import datetime
import re
import shutil
import uuid
import logging
from pathlib import Path
from .naming_utils import safe_directory_name
import asyncio
import aiofiles
from typing import Dict, List, Optional, Any, AsyncGenerator

from knowledge_base_agent.exceptions import MarkdownGenerationError
from knowledge_base_agent.config import Config
from knowledge_base_agent.path_utils import PathNormalizer, DirectoryManager, create_kb_path
from knowledge_base_agent.types import KnowledgeBaseItem

_folder_creation_lock = asyncio.Lock()

def format_links_in_text(text: str) -> str:
    """Format URLs in text as markdown links."""
    url_pattern = re.compile(r'(https?://\S+)')
    return url_pattern.sub(r'[\1](\1)', text)

def generate_tweet_markdown_content(
    item_name: str,
    tweet_url: str,
    tweet_text: str,
    image_descriptions: List[str]
) -> str:
    """Generate markdown content for a tweet."""
    formatted_tweet_text = format_links_in_text(tweet_text)
    lines = [
        f"# {item_name}",
        "",
        f"**Tweet URL:** [{tweet_url}]({tweet_url})",
        "",
        f"**Tweet Text:** {formatted_tweet_text}",
        ""
    ]

    for i, desc in enumerate(image_descriptions):
        img_name = f"image_{i+1}.jpg"
        lines.append(f"**Image {i+1} Description:** {desc}")
        lines.append(f"![Image {i+1}](./{img_name})")
        lines.append("")
    return "\n".join(lines)

class MarkdownWriter:
    """Handles writing content to markdown files in the knowledge base."""

    def __init__(self, config: Config):
        self.config = config
        self.path_normalizer = PathNormalizer()
        self.dir_manager = DirectoryManager()
        self.valid_image_extensions = ('.jpg', '.jpeg', '.png', '.webp')  # Could move to Config

    async def _validate_and_copy_media(self, media_files: List[Path], target_dir: Path) -> AsyncGenerator[Path, None]:
        """Validate and copy media files, yielding valid files copied."""
        valid_files = [img for img in media_files if img.suffix.lower() in self.valid_image_extensions]
        if len(valid_files) != len(media_files):
            invalid_files = [img.name for img in media_files if img not in valid_files]
            logging.warning(f"Invalid media types skipped: {invalid_files}")

        for i, img_path in enumerate(valid_files):
            if img_path.exists():
                img_name = f"image_{i+1}{img_path.suffix.lower()}"
                await self.dir_manager.copy_file(img_path, target_dir / img_name)
                yield img_path  # Yield for cleanup later

    async def write_tweet_markdown(
        self,
        root_dir: Path,
        tweet_id: str,
        tweet_data: Dict[str, Any],
        image_files: List[Path],
        image_descriptions: List[str],
        main_category: str = None,
        sub_category: str = None,
        item_name: str = None,
        tweet_text: str = None,
        tweet_url: str = None
    ) -> str:
        """Write tweet markdown using cached tweet data, returning the content file path."""
        categories = tweet_data.get('categories')
        if not categories:
            raise MarkdownGenerationError(f"No category data found for tweet {tweet_id}")
            
        main_category = main_category or categories['main_category']
        sub_category = sub_category or categories['sub_category']
        item_name = item_name or categories['item_name']
        tweet_text = tweet_text or tweet_data.get('full_text', '')
        tweet_url = tweet_url or tweet_data.get('tweet_url', '')
        safe_item_name = safe_directory_name(item_name)
        tweet_folder = root_dir / main_category / sub_category / safe_item_name

        async with _folder_creation_lock:
            if tweet_folder.exists():
                unique_suffix = uuid.uuid4().hex[:6]
                safe_item_name = f"{safe_item_name}_{unique_suffix}"
                tweet_folder = root_dir / main_category / sub_category / safe_item_name

        temp_folder = tweet_folder.with_suffix('.temp')
        temp_folder.mkdir(parents=True, exist_ok=True)

        try:
            if not tweet_text.strip():
                logging.warning(f"Empty tweet text for tweet {tweet_id}")

            content_md = generate_tweet_markdown_content(item_name, tweet_url, tweet_text, image_descriptions)
            content_md_path = temp_folder / "content.md"
            async with aiofiles.open(content_md_path, 'w', encoding="utf-8") as f:
                await f.write(content_md)

            # Copy valid media files and track for cleanup
            cleanup_files = []
            async for img_path in self._validate_and_copy_media(image_files, temp_folder):
                cleanup_files.append(img_path)

            temp_folder.rename(tweet_folder)

            # Cleanup original files
            for img_path in image_files:
                if img_path.exists() and img_path in cleanup_files:
                    img_path.unlink()

            return str(tweet_folder / "content.md")

        except Exception as e:
            logging.error(f"Failed to write tweet markdown for {tweet_id}: {str(e)}")
            if temp_folder.exists():
                shutil.rmtree(temp_folder)
            raise MarkdownGenerationError(f"Failed to write tweet markdown: {str(e)}")

    async def write_kb_item(
        self,
        item: KnowledgeBaseItem,
        media_files: List[Path] = None,
        media_descriptions: List[str] = None,
        root_dir: Path = None
    ) -> str:
        """Write knowledge base item to markdown with media, returning the README path."""
        try:
            kb_path = create_kb_path(
                item.category_info.main_category,
                item.category_info.sub_category,
                item.title
            )
            if root_dir:
                kb_path = root_dir / kb_path

            temp_dir = kb_path.with_suffix('.temp')
            temp_dir.mkdir(parents=True, exist_ok=True)

            try:
                content = self._generate_content(
                    item=item,
                    media_files=media_files,
                    media_descriptions=media_descriptions
                )

                readme_path = temp_dir / "README.md"
                async with aiofiles.open(readme_path, 'w', encoding='utf-8') as f:
                    await f.write(content)

                cleanup_files = []
                if media_files:
                    async for img_path in self._validate_and_copy_media(media_files, temp_dir):
                        cleanup_files.append(img_path)

                if kb_path.exists():
                    shutil.rmtree(kb_path)
                temp_dir.rename(kb_path)

                # Cleanup original files
                if media_files:
                    for img_path in media_files:
                        if img_path.exists() and img_path in cleanup_files:
                            img_path.unlink()

                return str(kb_path / "README.md")

            except Exception as e:
                logging.error(f"Failed to write KB item content: {str(e)}")
                if temp_dir.exists():
                    shutil.rmtree(temp_dir)
                raise

        except Exception as e:
            logging.error(f"Failed to create KB item directory: {str(e)}")
            raise MarkdownGenerationError(f"Failed to write KB item: {str(e)}")

    def _generate_content(
        self,
        item: KnowledgeBaseItem,
        media_files: List[Path] = None,
        media_descriptions: List[str] = None
    ) -> str:
        """Generate markdown content with proper formatting."""
        content = item.content
        source_section = [
            "\n## Source\n",
            f"- Original Tweet: [{item.source_tweet['url']}]({item.source_tweet['url']})",
            f"- Date: {item.source_tweet['created_at'].strftime('%Y-%m-%d %H:%M:%S')}\n"
        ]
        
        media_section = []
        if media_files and media_descriptions:
            media_section.append("\n## Media\n")
            for idx, (media, desc) in enumerate(zip(media_files, media_descriptions), 1):
                media_section.extend([
                    f"### Media {idx}",
                    f"![{media.stem}](./{media.name})",
                    f"**Description:** {desc}\n"
                ])
        
        timestamp = f"\n*Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*"
        return content + '\n'.join(source_section + media_section) + timestamp
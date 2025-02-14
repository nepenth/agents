import aiofiles
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Union

def safe_read_json(file_path: Path, default: Any = None) -> Any:
    """Unified JSON file reading with error handling."""
    if not file_path.exists():
        return default or {}
    try:
        with file_path.open('r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Failed to read JSON from {file_path}: {e}")
        return default or {}

def safe_write_json(file_path: Path, data: Any, indent: int = 4) -> bool:
    """Unified JSON file writing with error handling."""
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open('w', encoding='utf-8') as f:
            json.dump(data, f, indent=indent)
        return True
    except Exception as e:
        logging.error(f"Failed to write JSON to {file_path}: {e}")
        return False

async def async_json_load(file_path: Union[str, Path]) -> Any:
    """Asynchronously load JSON data."""
    async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
        content = await f.read()
        return json.loads(content)

async def async_json_dump(data: Any, file_path: Union[str, Path]) -> None:
    """Asynchronously save JSON data."""
    async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
        await f.write(json.dumps(data, indent=2))

async def async_read_text(file_path: Union[str, Path]) -> str:
    """Asynchronously read text file."""
    async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
        return await f.read()

async def async_write_text(content: str, file_path: Union[str, Path]) -> None:
    """Asynchronously write text file."""
    async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
        await f.write(content)

async def async_append_text(content: str, file_path: Union[str, Path]) -> None:
    """Asynchronously append to text file."""
    async with aiofiles.open(file_path, 'a', encoding='utf-8') as f:
        await f.write(content) 
    
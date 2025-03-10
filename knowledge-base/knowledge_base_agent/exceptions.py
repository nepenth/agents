"""Custom exceptions for the knowledge base agent."""

class KnowledgeBaseError(Exception):
    """Base exception for knowledge base operations."""
    pass

class AgentError(KnowledgeBaseError):
    """Raised when agent operations fail."""
    pass

class BookmarksFetchError(KnowledgeBaseError):
    """Raised when bookmark fetching fails."""
    pass

class MarkdownGenerationError(KnowledgeBaseError):
    """Markdown generation related errors."""
    pass

class PathValidationError(KnowledgeBaseError):
    """Raised when path validation fails."""
    pass

class StateError(KnowledgeBaseError):
    """State management related errors."""
    pass

class TweetProcessingError(KnowledgeBaseError):
    """Legacy error class for backward compatibility."""
    pass  # Now handled by ContentProcessingError

class ModelInferenceError(KnowledgeBaseError):
    """Raised when AI model inference fails."""
    pass

class GitSyncError(KnowledgeBaseError):
    """Git synchronization related errors."""
    pass

class ConfigurationError(KnowledgeBaseError):
    """Configuration related errors."""
    pass

class FetchError(KnowledgeBaseError):
    """Raised when failing to fetch data from external sources."""
    pass

class ProcessingError(KnowledgeBaseError):
    """Raised when failing to process content."""
    pass

class StorageError(KnowledgeBaseError):
    """Raised when failing to store or retrieve data."""
    pass

class GitError(KnowledgeBaseError):
    """Raised when Git operations fail."""
    pass

class CategoryError(KnowledgeBaseError):
    """Category management related errors."""
    pass

class AIError(KnowledgeBaseError):
    """Raised when AI operations (Ollama, etc.) fail."""
    pass

class ValidationError(KnowledgeBaseError):
    """Raised when validation fails."""
    pass

class FileOperationError(KnowledgeBaseError):
    """Raised when file operations fail."""
    pass

class NetworkError(KnowledgeBaseError):
    """Network related errors."""
    pass

class MediaProcessingError(KnowledgeBaseError):
    """Raised when media processing fails."""
    pass

class ContentProcessingError(KnowledgeBaseError):
    """Content processing related errors."""
    pass

class StateManagerError(KnowledgeBaseError):
    """State manager specific errors."""
    pass

class VisionModelError(ModelInferenceError):
    """Error during vision model processing."""
    pass

class ContentGenerationError(KnowledgeBaseError):
    """Raised when content generation fails."""
    pass

class CategoryGenerationError(KnowledgeBaseError):
    """Raised when category generation fails."""
    pass

class KnowledgeBaseItemCreationError(KnowledgeBaseError):
    """Raised when knowledge base item creation fails."""
    pass

class CommandError(KnowledgeBaseError):
    """Raised when a command-line operation fails"""
    def __init__(self, message: str):
        super().__init__(f"Command failed: {message}")

class PagesGenerationError(Exception):
    """Raised when GitHub Pages generation fails."""
    pass

"""Source ingestion interfaces."""

from .api_ingestor import APIIngestor
from .db_ingestor import DBIngestor
from .file_ingestor import FileIngestor
from .web_ingestor import WebIngestor

__all__ = ["APIIngestor", "DBIngestor", "FileIngestor", "WebIngestor"]

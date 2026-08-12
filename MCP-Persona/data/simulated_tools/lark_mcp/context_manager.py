"""
Context file backup and restoration management.

This module provides functionality to backup and restore context files
during tool execution to ensure clean testing environments.
"""

import os
import shutil
import tempfile
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ContextManager:
    """Manage context file backup and restoration operations."""

    def __init__(self, context_path: str):
        """
        Initialize the context manager.

        Args:
            context_path: Path to the context file to manage.
        """
        self.context_path = Path(context_path)
        self.backup_path: Optional[str] = None

    def backup(self) -> str:
        """
        Create a backup of the context file.

        Returns:
            Path to the backup file.

        Raises:
            FileNotFoundError: If the context file does not exist.
            IOError: If backup creation fails.
        """
        if not self.context_path.exists():
            raise FileNotFoundError(f"Context file not found: {self.context_path}")

        try:
            # Create a temporary directory for the backup
            backup_dir = tempfile.mkdtemp(prefix="lark_mcp_context_backup_")
            backup_path = Path(backup_dir) / self.context_path.name

            # Copy the file preserving metadata
            shutil.copy2(self.context_path, backup_path)

            self.backup_path = str(backup_path)
            logger.info(f"Context backup created: {self.backup_path}")
            return self.backup_path
        except Exception as e:
            logger.error(f"Failed to create backup: {e}")
            raise IOError(f"Backup creation failed: {e}")

    def restore(self, backup_path: str = None) -> bool:
        """
        Restore the context file from backup.

        Args:
            backup_path: Path to the backup file. If None, uses the last created backup.

        Returns:
            True if restoration succeeded, False otherwise.

        Raises:
            FileNotFoundError: If the backup file does not exist.
        """
        target_path = backup_path or self.backup_path

        if target_path is None:
            logger.warning("No backup path provided and no previous backup exists")
            return False

        backup_file = Path(target_path)
        if not backup_file.exists():
            raise FileNotFoundError(f"Backup file not found: {backup_file}")

        try:
            # Restore from backup
            shutil.copy2(backup_file, self.context_path)
            logger.info(f"Context restored from: {backup_file}")
            return True
        except Exception as e:
            logger.error(f"Failed to restore context: {e}")
            return False

    def cleanup_backup(self, backup_path: str = None) -> None:
        """
        Clean up the backup directory.

        Args:
            backup_path: Path to the backup file. If None, uses the last created backup.
        """
        target_path = backup_path or self.backup_path

        if target_path is None:
            return

        try:
            backup_dir = Path(target_path).parent
            if backup_dir.exists() and backup_dir.name.startswith("lark_mcp_context_backup_"):
                shutil.rmtree(backup_dir)
                logger.info(f"Backup cleaned up: {backup_dir}")
        except Exception as e:
            logger.warning(f"Failed to cleanup backup directory: {e}")

    def __enter__(self):
        """Context manager entry: create backup."""
        self.backup()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit: restore and cleanup."""
        if self.backup_path:
            self.restore(self.backup_path)
            self.cleanup_backup(self.backup_path)
        return False


__all__ = ["ContextManager"]

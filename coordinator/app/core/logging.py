"""
Coordinator logging.

This module will configure the application's logging system.
"""
import logging


def configure_logging() -> None:
    """
    Configure the Coordinator application's logging system.

    Logging configuration will be implemented in a later step.

    Responsibilities will include:
    - Configure log levels
    - Configure log formatting
    - Configure log handlers
    - Configure file/console output
    """

    pass


def get_logger(name: str) -> logging.Logger:
    """
    Return a logger for the given module.

    This currently returns Python's default logger.
    Future steps will return a fully configured logger.
    """

    return logging.getLogger(name)
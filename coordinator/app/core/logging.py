import logging


def configure_logging() -> None:
    """
    Configure the Coordinator application's logging system.

    This provides a minimal logging configuration for the
    Coordinator skeleton. Advanced logging configuration
    will be implemented in a later phase.
    """

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def get_logger(name: str) -> logging.Logger:
    """
    Return a logger for the given module.
    """

    return logging.getLogger(name)
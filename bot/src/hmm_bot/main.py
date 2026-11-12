import logging


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("hmm_bot").info("bot skeleton started")

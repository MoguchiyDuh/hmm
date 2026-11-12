import logging


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("hmm_parser").info("parser skeleton started")

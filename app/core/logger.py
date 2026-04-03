import logging

logger = logging.getLogger("job_scraper")
logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
handler.setFormatter(
    logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s")
)
logger.addHandler(handler)

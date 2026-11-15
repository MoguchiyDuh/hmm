from hmm.app import create_app
from hmm.platform.config import Config
from hmm.platform.logs import configure_logging

config = Config.from_env()
configure_logging(config.log_level)
app = create_app(config)

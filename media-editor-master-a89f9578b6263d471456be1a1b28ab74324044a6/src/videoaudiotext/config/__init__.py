"""Configuration: paths, environment, and runtime settings."""

from videoaudiotext.config.env import load_dotenv
from videoaudiotext.config.paths import PROJECT_ROOT, ROOT
from videoaudiotext.config.settings import *  # noqa: F403,F401

load_dotenv()

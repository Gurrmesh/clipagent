"""Central configuration. Everything is driven by environment variables."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data")).resolve()

SOURCE_DIR = DATA_DIR / "sources"     # downloaded / uploaded videos
AUDIO_DIR = DATA_DIR / "audio"        # extracted wavs
CLIP_DIR = DATA_DIR / "clips"         # rendered vertical clips
THUMB_DIR = DATA_DIR / "thumbs"       # poster frames
WORK_DIR = DATA_DIR / "work"          # subtitle files, scratch
DB_PATH = DATA_DIR / "clipagent.db"

for _d in (SOURCE_DIR, AUDIO_DIR, CLIP_DIR, THUMB_DIR, WORK_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- Models ---------------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
# Creator Scan reads a whole catalog: a cheaper, faster model can do the first
# screening pass (e.g. a Haiku-class model). Unset = the main model.
CLAUDE_SCREEN_MODEL = os.getenv("CLAUDE_SCREEN_MODEL", "").strip() or CLAUDE_MODEL

# Whisper: defaults to OpenAI, can be pointed at Groq or any compatible host.
WHISPER_API_KEY = os.getenv("WHISPER_API_KEY") or os.getenv("OPENAI_API_KEY", "")
WHISPER_BASE_URL = os.getenv("WHISPER_BASE_URL") or None
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")

# --- Limits ---------------------------------------------------------------
MAX_CLIPS = int(os.getenv("MAX_CLIPS", "12"))
MAX_SOURCE_MINUTES = int(os.getenv("MAX_SOURCE_MINUTES", "240"))
# Whisper uploads cap at 25 MB, so audio is chunked below that.
AUDIO_CHUNK_SECONDS = int(os.getenv("AUDIO_CHUNK_SECONDS", "900"))
# Creator Scan: seconds between requests to YouTube (and the other sites). Never
# below 4 — a scan makes hundreds of requests and must not get the PC blocked.
try:
    SCAN_REQUEST_GAP = max(4.0, float(os.getenv("SCAN_REQUEST_GAP", "4") or 4))
except ValueError:
    SCAN_REQUEST_GAP = 4.0

RENDER_W, RENDER_H = 1080, 1920
FPS = 30

# Platform safe zones on a 1080x1920 frame. TikTok, Reels and Shorts draw their
# own buttons over the video: tabs and icons along the top, the caption,
# username and sound line along the bottom, the like/comment/share rail down
# the right. Text and logos drawn there get hidden, so they stay inside these.
SAFE_TOP = 190
SAFE_BOTTOM = 400
SAFE_RIGHT = 140
SAFE_LEFT = 60

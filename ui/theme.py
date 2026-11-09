"""Design tokens and stylesheet loading.

One place for the palette, the spacing scale and the type scale, so Python
code that paints (badges, delegates) and the Qt stylesheet cannot drift apart.

The stylesheet is a plain .qss file with an ``@ASSETS@`` token that gets
replaced by the absolute path of the ``assets`` directory at load time.
Relative ``url(...)`` paths inside QSS resolve against the process working
directory, which breaks as soon as the app is launched from elsewhere, so the
substitution is not optional.
"""
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = APP_ROOT / "assets"
STYLE_PATH = APP_ROOT / "styles" / "main.qss"


# --- Surfaces -------------------------------------------------------------
CANVAS = '#f4f5f7'          # window background
SURFACE = '#ffffff'         # cards, inputs, table body
SURFACE_SUNKEN = '#f7f8fa'  # table headers, inert fills

# --- Lines ----------------------------------------------------------------
BORDER = '#e4e7ec'
BORDER_STRONG = '#d0d5dd'

# --- Text -----------------------------------------------------------------
TEXT = '#1f2937'
TEXT_SECONDARY = '#667085'
TEXT_MUTED = '#98a2b3'

# --- Accent ---------------------------------------------------------------
PRIMARY = '#2f6fed'
PRIMARY_HOVER = '#1d5fd8'
PRIMARY_PRESSED = '#1a4fbf'
PRIMARY_SOFT = '#eef4ff'

# --- Semantic -------------------------------------------------------------
SUCCESS = '#0f7b3f'
SUCCESS_SOFT = '#e7f6ec'
WARNING = '#b54708'
WARNING_SOFT = '#fef3e6'
DANGER = '#b42318'
DANGER_SOFT = '#fee4e2'

# --- Spacing scale (multiples of 4) ---------------------------------------
SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 24


# Status -> (label, soft background, strong foreground) for table badges.
STATUS_STYLES = {
    'PERFECT': ('Perfect', SUCCESS_SOFT, SUCCESS),
    'GOOD': ('Good', '#e8f7f1', '#0f766e'),
    'FAIR': ('Fair', WARNING_SOFT, WARNING),
    'POOR': ('Poor', '#fdeceb', '#c4320a'),
    'BAD': ('Bad', DANGER_SOFT, DANGER),
}


def status_style(status: str):
    """Return (label, background, foreground) for a status code."""
    return STATUS_STYLES.get(status, STATUS_STYLES['BAD'])


def load_stylesheet() -> str:
    """Read main.qss and resolve the ``@ASSETS@`` token to an absolute path."""
    try:
        text = STYLE_PATH.read_text(encoding='utf-8')
    except OSError:
        return ''
    return text.replace('@ASSETS@', ASSETS_DIR.as_posix())

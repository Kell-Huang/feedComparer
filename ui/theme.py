"""Stylesheet loading and the few colours Python paints with.

The visual system itself lives in ``styles/main.qss`` — that file is the single
source of truth for surfaces, borders, spacing and type. This module only
exposes the handful of colours that are applied from code rather than from the
stylesheet (status pills, coloured table cells), plus the loader that resolves
the ``@ASSETS@`` token.

Keeping a second copy of the whole palette here would be a trap: editing it
would change nothing on screen, because Qt applies the QSS values.

The ``@ASSETS@`` token is replaced with the absolute path of the ``assets``
directory at load time. Relative ``url(...)`` paths inside QSS resolve against
the process working directory, which breaks as soon as the app is launched
from somewhere else, so the substitution is not optional.
"""
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = APP_ROOT / "assets"
STYLE_PATH = APP_ROOT / "styles" / "main.qss"


# Colours applied from Python (table cells, status pills). These mirror values
# in main.qss; the stylesheet remains authoritative for everything it styles.
TEXT_SECONDARY = '#667085'
SUCCESS = '#0f7b3f'
WARNING = '#b54708'
DANGER = '#b42318'


# Status -> (label, soft background, strong foreground) for table badges.
STATUS_STYLES = {
    'PERFECT': ('Perfect', '#e7f6ec', SUCCESS),
    'GOOD': ('Good', '#e8f7f1', '#0f766e'),
    'FAIR': ('Fair', '#fef3e6', WARNING),
    'POOR': ('Poor', '#fdeceb', '#c4320a'),
    'BAD': ('Bad', '#fee4e2', DANGER),
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

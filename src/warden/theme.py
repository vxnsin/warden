"""The Deep Dark palette and the banner.

Sculk black for the ground, the warden's cyan heartbeat for anything live,
amethyst and shrieker amber for telling one kind of service from another.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import groupby

from rich.text import Text

SCULK = "#08100f"
SCULK_RAISED = "#0e1a1c"
SCULK_LIT = "#14262a"
VEIN = "#1e3538"
VEIN_BRIGHT = "#2a4a4e"

BONE = "#d9e4e2"
BONE_DIM = "#6d8687"

GLOW = "#2be0d6"
GLOW_DIM = "#17847f"
AMETHYST = "#a87fe0"
SHRIEKER = "#e0b457"
MOSS = "#4fd98c"
EMBER = "#e5544b"

KIND_COLOURS = {
    "backend": GLOW,
    "frontend": AMETHYST,
    "worker": SHRIEKER,
    "database": MOSS,
    "cache": MOSS,
    "proxy": GLOW_DIM,
}

BLOCK = "█"
HEART = "*"
GAP = "    "

# Horns, a head with no eyes, an open ribcage around the glowing heart, and the
# long arms. Drawn from full blocks only, so a console that cannot print them
# needs a single substitution rather than a whole second drawing.
MASCOT = (
    "██             ██",
    " ███         ███ ",
    "    █████████    ",
    "    ██     ██    ",
    "    █████████    ",
    "█████████████████",
    "███  █ *** █  ███",
    "███  █ *** █  ███",
    "███           ███",
)

WORDMARK = (
    "██     ██  █████  ██████  ██████  ███████ ███    ██",
    "██     ██ ██   ██ ██   ██ ██   ██ ██      ████   ██",
    "██  █  ██ ███████ ██████  ██   ██ █████   ██ ██  ██",
    "██ ███ ██ ██   ██ ██   ██ ██   ██ ██      ██  ██ ██",
    " ███ ███  ██   ██ ██   ██ ██████  ███████ ██   ████",
)

TAGLINE = "nothing binds a port without asking"


def kind_colour(kind: str) -> str:
    return KIND_COLOURS.get(kind, BONE_DIM)


def age(moment: datetime) -> str:
    """How long ago, in the largest unit that still says something."""
    seconds = int((datetime.now(UTC) - moment).total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86_400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86_400}d ago"


def account(user: str | None) -> str:
    """Just the account, without the domain that pads every Windows row."""
    if not user:
        return "-"
    return user.rsplit("\\", 1)[-1]


def _block_for(encoding: str | None) -> str:
    """The character to draw with.

    A Windows console still running a legacy code page cannot encode the block,
    and would abort the whole command over a decoration.
    """
    try:
        BLOCK.encode(encoding or "utf-8")
    except (LookupError, UnicodeEncodeError):
        return "#"
    return BLOCK


def _rows() -> list[tuple[str, str]]:
    """Each mascot line beside its wordmark line, the wordmark vertically centred."""
    top = (len(MASCOT) - len(WORDMARK)) // 2
    blank = " " * len(WORDMARK[0])
    return [
        (mascot, WORDMARK[index - top] if 0 <= index - top < len(WORDMARK) else blank)
        for index, mascot in enumerate(MASCOT)
    ]


def banner_for(encoding: str | None = None) -> str:
    """The banner as plain text."""
    block = _block_for(encoding)
    return "\n".join(
        f"{mascot}{GAP}{word}".replace(HEART, block).replace(BLOCK, block)
        for mascot, word in _rows()
    )


def _style_of(char: str) -> str:
    if char == HEART:
        return GLOW
    if char == BLOCK:
        return GLOW_DIM
    return ""


def banner_text(encoding: str | None = None) -> Text:
    """The banner with the mascot beside the name and its heart lit.

    Written one run of equal colour at a time; a span per character would bloat
    every screenshot the dashboard exports.
    """
    block = _block_for(encoding)
    text = Text()
    for index, (mascot, word) in enumerate(_rows()):
        if index:
            text.append("\n")
        for style, chars in groupby(mascot, key=_style_of):
            run = "".join(chars).replace(HEART, block).replace(BLOCK, block)
            text.append(run, style=style)
        text.append(GAP)
        text.append(word.replace(BLOCK, block), style=GLOW)
    return text


BANNER = banner_for()

"""The Deep Dark palette.

Sculk black for the ground, the warden's cyan heartbeat for anything live,
amethyst and shrieker amber for telling one kind of service from another.
"""

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

BANNER = r"""
██     ██  █████  ██████  ██████  ███████ ███    ██
██     ██ ██   ██ ██   ██ ██   ██ ██      ████   ██
██  █  ██ ███████ ██████  ██   ██ █████   ██ ██  ██
██ ███ ██ ██   ██ ██   ██ ██   ██ ██      ██  ██ ██
 ███ ███  ██   ██ ██   ██ ██████  ███████ ██   ████
""".strip("\n")

TAGLINE = "nothing binds a port without asking"


def kind_colour(kind: str) -> str:
    return KIND_COLOURS.get(kind, BONE_DIM)


def banner_for(encoding: str | None) -> str:
    """The banner, drawn with a character the console can actually print.

    A Windows console still running a legacy code page cannot encode the block
    character, and would abort the whole command over a decoration.
    """
    try:
        BANNER.encode(encoding or "utf-8")
    except (LookupError, UnicodeEncodeError):
        return BANNER.replace("█", "#")
    return BANNER

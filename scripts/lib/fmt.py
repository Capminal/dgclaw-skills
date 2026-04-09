"""Colored, aligned table output and formatting helpers."""
import os
import sys

# ── ANSI colors ──────────────────────────────────────────────────────────────

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

_NO_COLOR = os.environ.get("NO_COLOR") is not None or (
    not sys.stdout.isatty() and not os.environ.get("FORCE_COLOR")
)


def c(text, color):
    """Wrap text in ANSI color. No-op if stdout is not a TTY."""
    if _NO_COLOR:
        return str(text)
    return f"{color}{text}{RESET}"


def green(text):  return c(text, GREEN)
def red(text):    return c(text, RED)
def yellow(text): return c(text, YELLOW)
def cyan(text):   return c(text, CYAN)
def bold(text):   return c(text, BOLD)
def dim(text):    return c(text, DIM)


# ── Formatting helpers ───────────────────────────────────────────────────────

def pnl(value, fmt="+,.2f"):
    """Format a PnL value: green if positive, red if negative."""
    s = f"{value:{fmt}}"
    if not s.startswith(("+", "-")):
        s = f"+{s}" if value >= 0 else s
    return green(s) if value >= 0 else red(s)


def pnl_dollar(value):
    """Format dollar PnL: +$123.45 / -$45.67"""
    sign = "+" if value >= 0 else "-"
    s = f"{sign}${abs(value):,.2f}"
    return green(s) if value >= 0 else red(s)


def pct(value, decimals=2):
    """Format percentage with +/- and color."""
    s = f"{value:+.{decimals}f}%"
    return green(s) if value >= 0 else red(s)


def price(p):
    """Auto-precision price formatting."""
    if p >= 1000:
        return f"${p:,.2f}"
    elif p >= 1:
        return f"${p:,.4f}"
    elif p >= 0.001:
        return f"${p:,.6f}"
    else:
        return f"${p:.8f}"


def price_raw(p):
    """Price without $ prefix."""
    if p >= 1000:
        return f"{p:,.2f}"
    elif p >= 1:
        return f"{p:,.4f}"
    elif p >= 0.001:
        return f"{p:,.6f}"
    else:
        return f"{p:.8f}"


def signal_badge(confidence):
    """Colored star badge for signal confidence."""
    if confidence == "high":
        return green("★★★")
    elif confidence == "medium":
        return yellow("★★")
    elif confidence == "low":
        return dim("★")
    return dim("——")


def side_label(side):
    """Colored LONG/SHORT label."""
    s = side.upper()
    return green(s) if s == "LONG" else red(s)


def trend_arrow(direction):
    """Colored trend direction."""
    if direction == "up":
        return green("▲ UP")
    elif direction == "down":
        return red("▼ DOWN")
    return dim("— FLAT")


def check(ok):
    """Checkmark or cross."""
    return green("✓") if ok else red("✗")


# ── Table rendering ──────────────────────────────────────────────────────────

def header_box(title):
    """Print a bordered section header."""
    w = max(len(title) + 4, 40)
    print(f"\n{bold(cyan('═' * w))}")
    print(f"  {bold(title)}")
    print(f"{cyan('═' * w)}")


def separator(char="─", width=72):
    print(dim(char * width))


def table(headers, rows, alignments=None):
    """Print an aligned, colored table.

    Args:
        headers: list of column header strings
        rows: list of lists (each row is a list of cell strings)
        alignments: list of '<', '>', '^' per column (default: left)
    """
    if not rows:
        print(dim("  (no data)"))
        return

    ncols = len(headers)
    if alignments is None:
        alignments = ["<"] * ncols

    # Strip ANSI for width calc
    def visible_len(s):
        import re
        return len(re.sub(r"\033\[[0-9;]*m", "", str(s)))

    # Compute column widths
    widths = [visible_len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < ncols:
                widths[i] = max(widths[i], visible_len(str(cell)))

    # Add padding
    widths = [w + 2 for w in widths]

    def pad(text, width, align):
        """Pad text accounting for ANSI escape codes."""
        vlen = visible_len(str(text))
        diff = width - vlen
        if diff <= 0:
            return str(text)
        if align == ">":
            return " " * diff + str(text)
        elif align == "^":
            left = diff // 2
            return " " * left + str(text) + " " * (diff - left)
        return str(text) + " " * diff

    # Header
    hdr = "  ".join(pad(bold(h), widths[i], alignments[i]) for i, h in enumerate(headers))
    print(f"  {hdr}")
    rule = "  ".join(dim("─" * widths[i]) for i in range(ncols))
    print(f"  {rule}")

    # Rows
    for row in rows:
        cells = []
        for i in range(ncols):
            val = str(row[i]) if i < len(row) else ""
            cells.append(pad(val, widths[i], alignments[i]))
        print(f"  {'  '.join(cells)}")

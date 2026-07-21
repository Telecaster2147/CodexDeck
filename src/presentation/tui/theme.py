"""Shared semantic colors for the Textual presentation."""

from textual.theme import Theme


CODEXDECK_BLUE_THEME = Theme(
    name="codexdeck-blue",
    primary="#38bdf8",
    secondary="#67e8f9",
    warning="#fbbf24",
    error="#f87171",
    success="#4ade80",
    accent="#38bdf8",
    foreground="#f8fafc",
    background="#0f172a",
    surface="#111827",
    panel="#1f2937",
    boost="#334155",
    dark=True,
    variables={
        "footer-background": "#080d16",
        "footer-foreground": "#aabbd0",
        "footer-item-background": "#080d16",
    },
)

STATE_COLORS = {
    "error": "#f87171",
    "warning": "#fbbf24",
    "success": "#4ade80",
    "info": "#38bdf8",
    "muted": "#64748b",
}

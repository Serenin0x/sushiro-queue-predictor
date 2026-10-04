"""Keep README emphasis in the brand palette without GitHub-only CSS.

The descriptions, links, headings and list order stay in native Markdown.
Edit a label's image alt (or write an ordinary **bold** label), then rerun.
Only this script's deterministic label assets are generated; no network access.
"""

from __future__ import annotations

import hashlib
import html
from pathlib import Path
import re
import unicodedata


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
ASSETS = ROOT / "assets/readme-labels"
COLORS = {"light": "#99081c", "dark": "#ffb4a4"}
LABEL = re.compile(
    r'<strong><picture><source media="\(prefers-color-scheme: dark\)" '
    r'srcset="assets/readme-labels/[a-f0-9]+-dark.svg">'
    r'<img src="assets/readme-labels/[a-f0-9]+-light.svg" '
    r'alt="([^"]*)" width="\d+" height="20"></picture></strong>'
)


def native_source(text: str) -> str:
    """Recover the native emphasis for content comparisons and future edits."""
    text = LABEL.sub(lambda m: "**" + html.unescape(m[1]) + "**", text)
    return text.replace(
        '<p align="center"><strong>少一点盯号，多一点自由安排。</strong></p>',
        "**少一点盯号，多一点自由安排。**",
    )


def label_image(words: str) -> tuple[str, dict[str, str]]:
    key = hashlib.sha256(words.encode()).hexdigest()[:16]
    # A stable canvas keeps Latin dates and Chinese titles aligned with body text.
    width = round(sum(16 if unicodedata.east_asian_width(c) in "WF" else
                      4.5 if c == " " else 8 for c in words) + 2)
    escaped = html.escape(words, quote=True)
    files = {}
    for theme, color in COLORS.items():
        files[f"{key}-{theme}.svg"] = (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width}" height="20" viewBox="0 0 {width} 20" '
            'role="img" aria-labelledby="label">\n'
            f'  <title id="label">{escaped}</title>\n'
            f'  <text x="0" y="16" fill="{color}" font-size="16" '
            'font-weight="600" font-family="-apple-system,BlinkMacSystemFont,'
            '\'Segoe UI\',\'Noto Sans CJK SC\',\'Microsoft YaHei\',sans-serif" '
            f'textLength="{width - 2}" lengthAdjust="spacingAndGlyphs">'
            f'{escaped}</text>\n</svg>\n'
        )
    markup = (
        '<strong><picture><source media="(prefers-color-scheme: dark)" '
        f'srcset="assets/readme-labels/{key}-dark.svg">'
        f'<img src="assets/readme-labels/{key}-light.svg" '
        f'alt="{escaped}" width="{width}" height="20"></picture></strong>'
    )
    return markup, files


def main() -> None:
    original = README.read_bytes()
    source = native_source(original.decode())
    section = ""
    files: dict[str, str] = {}
    output = []
    count = 0
    for line in source.splitlines():
        if line.startswith("##"):
            section = line.split(" ", 1)[1]
        match = re.match(r"^- \*\*(.+?)\*\*(.*)$", line)
        if match and section in ("当前可以做什么", "更新说明"):
            markup, generated = label_image(match[1])
            files.update(generated)
            output.append("- " + markup + match[2])
            output.append("")
            count += 1
        elif line == "**少一点盯号，多一点自由安排。**":
            output.append('<p align="center"><strong>少一点盯号，多一点自由安排。</strong></p>')
        else:
            output.append(line)
    rendered = "\n".join(output) + "\n"
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    # Ignore only blank spacing: every original word, link and Markdown block remains.
    assert [x for x in native_source(rendered).splitlines() if x.strip()] == [
        x for x in source.splitlines() if x.strip()
    ], "README content or structure changed"
    assert README.read_bytes() == original, "README changed during generation"
    ASSETS.mkdir(exist_ok=True)
    for name, content in files.items():
        path = ASSETS / name
        if not path.exists() or path.read_text() != content:
            path.write_text(content)
    if rendered.encode() != original:
        README.write_text(rendered)
    print(f"Rendered {count} unchanged emphasis labels in light/dark; native content matches.")


if __name__ == "__main__":
    main()

"""Convert a rendered chat-reply DOM fragment back into Markdown.

Browser-driven providers read the assistant's answer out of the page. Reading
``inner_text`` returns only the *visible* text, which throws away everything
Markdown encodes: bullet/numbered lists lose their markers, headings lose their
``#``, emphasis/`code` markers vanish, and inline references collapse to their
anchor text with the URL discarded. The site renders all of that correctly, so
to hand the client output as faithful as a direct LLM API response we instead
read the answer's ``innerHTML`` and reconstruct Markdown from it here.

We use ``markdownify`` (a mature HTML->Markdown converter) rather than a
hand-rolled serializer so the long tail of markup — nested lists, tables,
blockquotes, fenced code with language hints, links — is handled correctly. The
options below keep the output close to what an LLM emits (no defensive
backslash-escaping) and tidy the whitespace markdownify tends to over-produce.
"""

from __future__ import annotations

import re

from markdownify import markdownify as _markdownify

# Class prefixes highlighters use to tag a fenced code block's language, e.g.
# `<pre><code class="language-python">` or `class="lang-js hljs">`.
_LANG_PREFIXES = ("language-", "lang-")


def _code_language(el) -> str:
    """Pull the language hint off a `<pre>`'s class list (or its `<code>` child)
    so fenced blocks come out as ```` ```python ```` instead of a bare fence."""
    candidates = [el] + list(el.find_all("code", recursive=True))
    for node in candidates:
        for cls in node.get("class", []) or []:
            for prefix in _LANG_PREFIXES:
                if cls.startswith(prefix):
                    return cls[len(prefix):]
    return ""


# markdownify defensively backslash-escapes characters it thinks could be
# Markdown syntax (e.g. `1\.` after a number, `\_` inside identifiers, `\*`).
# An LLM API never does that, so we turn it off to keep output verbatim.
_OPTIONS = dict(
    heading_style="ATX",        # `## Heading`, not underlined Setext
    bullets="-",                # normalise `*`/`+` bullets to `-`
    code_language_callback=_code_language,
    escape_asterisks=False,
    escape_underscores=False,
    escape_misc=False,
)

# Collapse 3+ consecutive newlines down to a single blank line.
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")


def html_to_markdown(html: str) -> str:
    """Convert an answer's ``innerHTML`` to clean Markdown.

    Returns an empty string for empty/whitespace-only input. Trailing spaces per
    line and runs of blank lines are trimmed so spacing matches typical LLM
    output.
    """
    if not html or not html.strip():
        return ""
    md = _markdownify(html, **_OPTIONS)
    # Strip per-line trailing whitespace, then normalise blank-line runs.
    md = "\n".join(line.rstrip() for line in md.splitlines())
    md = _EXCESS_BLANK_LINES.sub("\n\n", md)
    return md.strip()

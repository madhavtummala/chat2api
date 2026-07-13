"""Inspect a logged-in chat UI to derive/verify provider selectors.

Opens the site in your persistent (logged-in) browser profile, waits for you to
log in, then dumps candidate composer/button selectors and saves the full HTML +
a screenshot for tuning a provider's `Selectors`.

    # log in to the site in the window that opens, then press Enter in the terminal
    CHAT2API_USER_DATA_DIR=.perplexity_profile CHAT2API_HEADLESS=false \
      python scripts/inspect_provider.py https://www.perplexity.ai

Share the printed JSON (and inspect_out.html if asked). Use a DIFFERENT
CHAT2API_USER_DATA_DIR per site so logins don't collide.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root on path

from playwright.async_api import async_playwright

from src.config import settings

OUT = Path(__file__).parent

_DUMP_JS = """() => {
    const attrsOf = (el) => {
        const o = {tag: el.tagName.toLowerCase()};
        for (const a of el.attributes) {
            if (["class","id","role","type","placeholder","contenteditable","name","title"].includes(a.name)
                || a.name.startsWith("data-") || a.name.startsWith("aria-"))
                o[a.name] = a.value.slice(0, 90);
        }
        const t = (el.innerText || "").trim();
        if (t) o._text = t.slice(0, 40);
        const svg = el.querySelector("svg");
        if (svg) o._svg = (svg.getAttribute("class") || "").slice(0, 60);
        return o;
    };
    const pick = (sel) => [...document.querySelectorAll(sel)].slice(0, 15).map(attrsOf);
    return {
        url: location.href,
        title: document.title,
        inputs: pick("textarea, [contenteditable=true], [role=textbox], [role=combobox]"),
        buttons: pick("button, [role=button]"),
        file_inputs: pick("input[type=file]"),
    };
}"""


async def main(url: str) -> None:
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(Path(settings.user_data_dir).resolve()),
            headless=settings.headless,
            args=["--no-sandbox"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded")

        if not settings.headless:
            input(f"\nLog in at {url} in the browser window, then press Enter here… ")
        await page.wait_for_timeout(1500)

        print("\n===== CANDIDATES =====")
        print(json.dumps(await page.evaluate(_DUMP_JS), indent=2))

        await page.screenshot(path=str(OUT / "inspect_out.png"), full_page=False)
        (OUT / "inspect_out.html").write_text((await page.content())[:800_000])
        print(f"\nSaved screenshot -> {OUT / 'inspect_out.png'}")
        print(f"Saved HTML       -> {OUT / 'inspect_out.html'}")
        await ctx.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python scripts/inspect_provider.py <url>")
    asyncio.run(main(sys.argv[1]))

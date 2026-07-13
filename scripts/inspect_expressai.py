"""Inspect the logged-in ExpressAI DOM to derive correct provider selectors.

Run it with the SAME profile you logged into, and stop the server first
(Chromium locks the user_data_dir):

    # stop `python -m src.main` first!
    CHAT2API_USER_DATA_DIR=.browser_profile \
    CHAT2API_HEADLESS=false \
    .venv/bin/python scripts/inspect_expressai.py

Then paste the printed JSON back. It also writes a screenshot + trimmed HTML
next to this script (inspect_out.png / inspect_out.html).
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


async def dump_candidates(page) -> dict:
    return await page.evaluate(
        """() => {
        const attrsOf = (el) => {
            const o = {tag: el.tagName.toLowerCase()};
            for (const a of el.attributes) {
                if (["class","id","role","name","type","placeholder","contenteditable"].includes(a.name)
                    || a.name.startsWith("data-") || a.name.startsWith("aria-"))
                    o[a.name] = a.value.slice(0, 80);
            }
            const t = (el.innerText || "").trim();
            if (t) o._text = t.slice(0, 40);
            return o;
        };
        const pick = (sel) => [...document.querySelectorAll(sel)].slice(0, 12).map(attrsOf);
        return {
            url: location.href,
            title: document.title,
            has_password_field: !!document.querySelector("input[type=password]"),
            inputs: pick("textarea, [contenteditable=true], [role=textbox], [role=combobox]"),
            buttons: pick("button, [role=button]"),
            nav_links: pick("a[href]").filter(x => x.href && /new|chat/i.test(JSON.stringify(x))),
        };
    }"""
    )


async def main() -> None:
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(Path(settings.user_data_dir).resolve()),
            headless=settings.headless,
            args=["--no-sandbox"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(settings.expressai_base_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        print("\n===== BEFORE SENDING =====")
        before = await dump_candidates(page)
        print(json.dumps(before, indent=2))

        # ---- Model dropdown -------------------------------------------------
        print("\n===== MODEL DROPDOWN =====")
        trigger = await page.evaluate(
            """() => {
                const attrsOf = (el)=>{const o={tag:el.tagName.toLowerCase()};for(const a of el.attributes){if(["class","id","role"].includes(a.name)||a.name.startsWith("data-")||a.name.startsWith("aria-"))o[a.name]=a.value.slice(0,80);}o._text=(el.innerText||"").trim().slice(0,40);return o;};
                const cands=[...document.querySelectorAll("button,[role=button],[role=combobox],div,span")]
                  .filter(el=>/GPT OSS|120B/i.test(el.innerText||"") && (el.innerText||"").length<40);
                cands.sort((a,b)=>a.innerText.length-b.innerText.length);
                return cands[0]?attrsOf(cands[0]):null;
            }"""
        )
        print("trigger:", json.dumps(trigger, indent=2))
        try:
            el = page.locator("button:has-text('GPT OSS'), [role=combobox]:has-text('GPT OSS')").first
            await el.click(timeout=4000)
            await page.wait_for_timeout(1200)
            options = await page.evaluate(
                """() => {
                    const attrsOf = (el)=>{const o={tag:el.tagName.toLowerCase()};for(const a of el.attributes){if(["class","id","role"].includes(a.name)||a.name.startsWith("data-")||a.name.startsWith("aria-"))o[a.name]=a.value.slice(0,60);}o._text=(el.innerText||"").trim().slice(0,40);return o;};
                    const named=[...document.querySelectorAll("[role=option],[role=menuitem],li,button,div")]
                      .filter(el=>/Nemotron|DeepSeek|Qwen|Gemma/i.test(el.innerText||"") && (el.innerText||"").length<50);
                    named.sort((a,b)=>a.innerText.length-b.innerText.length);
                    const seen=new Set(); const out=[];
                    for(const el of named){const k=el.tagName+el.className; if(seen.has(k))continue; seen.add(k); out.push(attrsOf(el)); if(out.length>=8)break;}
                    return out;
                }"""
            )
            print("options:", json.dumps(options, indent=2))
        except Exception as exc:  # noqa: BLE001
            print(f"[could not open model dropdown: {exc}]")

        # The model picker is a modal — close it so it doesn't block the composer.
        async def close_modals():
            for _ in range(4):
                if not await page.locator(".fixed.inset-0").count():
                    return
                x = page.locator("button:has(svg.lucide-x)").first
                try:
                    if await x.count():
                        await x.click(timeout=2000)
                    else:
                        await page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    break
                await page.wait_for_timeout(300)
        await close_modals()

        # ---- Composer toolbar (web search toggle, attachments) --------------
        print("\n===== COMPOSER TOOLBAR =====")
        toolbar = await page.evaluate(
            """() => {
                const ta = document.querySelector("textarea[placeholder='Ask anything']") || document.querySelector("textarea");
                if(!ta) return {error:"composer not found"};
                // walk up a few levels to the toolbar container
                let box = ta; for(let i=0;i<5 && box.parentElement;i++) box = box.parentElement;
                const attrsOf = (el)=>{const o={tag:el.tagName.toLowerCase()};for(const a of el.attributes){if(["class","id","role","type","title"].includes(a.name)||a.name.startsWith("data-")||a.name.startsWith("aria-"))o[a.name]=a.value.slice(0,70);}const t=(el.innerText||"").trim();if(t)o._text=t.slice(0,30);const svg=el.querySelector("svg");if(svg){o._svg={cls:svg.getAttribute("class")||"",label:svg.getAttribute("aria-label")||"",title:(svg.querySelector("title")||{}).textContent||""};}return o;};
                return {
                    buttons: [...box.querySelectorAll("button,[role=switch],[role=checkbox],label")].slice(0,15).map(attrsOf),
                    file_inputs: [...box.querySelectorAll("input[type=file]")].map(attrsOf),
                };
            }"""
        )
        print(json.dumps(toolbar, indent=2))

        # Make sure no modal overlay is blocking the composer, then New Chat.
        await close_modals()
        try:
            nc = page.locator("button:has-text('New Chat')").first
            if await nc.count():
                await nc.click()
                await page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass

        # Send a probe message via the real composer + send button.
        PROMPT = "Reply with a short two-sentence description of the ocean."
        try:
            composer = page.locator("textarea[placeholder='Ask anything']").first
            await composer.click()
            await composer.fill(PROMPT)
            send_btn = page.locator("button[title='Send message']").first
            if await send_btn.count():
                await send_btn.click()
            else:
                await composer.press("Enter")
            print("\n[sent probe message; watching response capture...]")
        except Exception as exc:  # noqa: BLE001
            print(f"\n[could not auto-send: {exc}]")

        # Report what OUR selector (.mr-auto .prose) captures over time, and the
        # DOM structure of the newest message bubble, so we can fix the selector.
        for i in range(24):
            snap = await page.evaluate(
                """() => {
                    const attrsOf = (x)=>{if(!x)return null;const o={tag:x.tagName.toLowerCase()};for(const a of x.attributes){if(['class','id'].includes(a.name)||a.name.startsWith('data-')||a.name.startsWith('aria-'))o[a.name]=a.value.slice(0,90);}return o;};
                    const ours = [...document.querySelectorAll('.mr-auto .prose')];
                    const lastProse = [...document.querySelectorAll('.prose')].pop();
                    return {
                        our_selector_count: ours.length,
                        our_last_text: ours.length ? ours[ours.length-1].innerText.slice(0,140) : null,
                        total_prose: document.querySelectorAll('.prose').length,
                        last_prose_text: lastProse ? lastProse.innerText.slice(0,140) : null,
                        last_prose_chain: lastProse ? [attrsOf(lastProse), attrsOf(lastProse.parentElement), attrsOf(lastProse.parentElement?.parentElement)] : null,
                    };
                }"""
            )
            print(f"t+{i*0.5:.1f}s", json.dumps(snap))
            await page.wait_for_timeout(500)

        await page.screenshot(path=str(OUT / "inspect_out.png"), full_page=True)
        html = await page.content()
        (OUT / "inspect_out.html").write_text(html[:400_000])
        print(f"\nSaved screenshot -> {OUT/'inspect_out.png'}")
        print(f"Saved HTML       -> {OUT/'inspect_out.html'}")
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())

"""Inspection script — logs in to sms.eursc.eu and maps the DOM / API surface."""
import asyncio
import os
import re
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
USERNAME = os.getenv("SMS_USERNAME")
PASSWORD = os.getenv("SMS_PASSWORD")

ROOT = Path(__file__).parent
SHOTS = ROOT / "screenshots"
APIS = ROOT / "api_responses"
SHOTS.mkdir(exist_ok=True)
APIS.mkdir(exist_ok=True)

KEYWORDS = ["homework", "devoir", "test", "assessment", "agenda",
            "evaluation", "évaluation", "hausaufgaben", "prüfung"]

api_log_lines = []


async def log_response(response):
    try:
        ct = response.headers.get("content-type", "")
        url = response.url
        if response.request.resource_type in ("xhr", "fetch"):
            api_log_lines.append(f"{response.status} {ct} {url}")
            if "application/json" in ct:
                try:
                    body = await response.text()
                    safe = re.sub(r"[^a-zA-Z0-9]+", "_", url)[-120:]
                    (APIS / f"{safe}.json").write_text(body)
                except Exception as e:
                    api_log_lines.append(f"  (failed to read body: {e})")
    except Exception:
        pass


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=200)
        context = await browser.new_context()
        page = await context.new_page()
        page.on("response", lambda r: asyncio.create_task(log_response(r)))

        print("[*] Navigating to login...")
        await page.goto("https://sms.eursc.eu/login", wait_until="networkidle")
        await page.screenshot(path=str(SHOTS / "00_login.png"), full_page=True)

        # Try to find username/password inputs
        print("[*] Attempting login...")
        try:
            # Common patterns
            await page.fill('input[type="email"], input[name*="user" i], input[name*="email" i], input[id*="user" i], input[id*="email" i]', USERNAME, timeout=8000)
            await page.fill('input[type="password"]', PASSWORD, timeout=8000)
            await page.screenshot(path=str(SHOTS / "00b_filled.png"), full_page=True)
            # Submit
            await page.click('button[type="submit"], input[type="submit"], button:has-text("Login"), button:has-text("Sign in"), button:has-text("Connexion"), button:has-text("Anmelden")')
        except Exception as e:
            print(f"[!] Login form fill failed: {e}")
            html = await page.content()
            (ROOT / "login_page_html.txt").write_text(html)
            print("    Saved login_page_html.txt for inspection.")

        # Wait for navigation
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(3)
        await page.screenshot(path=str(SHOTS / "01_dashboard.png"), full_page=True)
        print(f"[*] Post-login URL: {page.url}")

        # Enumerate nav links
        print("[*] Enumerating links...")
        links = await page.eval_on_selector_all(
            "a",
            "els => els.map(e => ({href: e.href, text: (e.innerText||'').trim()}))"
        )
        unique = {}
        for l in links:
            if l["href"] and l["href"] not in unique:
                unique[l["href"]] = l["text"]

        (ROOT / "all_links.txt").write_text(
            "\n".join(f"{t!r} -> {h}" for h, t in unique.items())
        )
        print(f"[*] Found {len(unique)} unique links (saved to all_links.txt)")

        # Find homework/test-related links
        matches = [(h, t) for h, t in unique.items()
                   if any(k in (t or "").lower() or k in h.lower() for k in KEYWORDS)]
        print(f"[*] {len(matches)} candidate links matched keywords")

        html_dump = []
        for i, (href, text) in enumerate(matches[:15]):
            print(f"    -> [{i}] {text!r}  {href}")
            try:
                await page.goto(href, wait_until="networkidle", timeout=20000)
                await asyncio.sleep(2)
                safe = re.sub(r"[^a-zA-Z0-9]+", "_", text or "page")[:40]
                await page.screenshot(path=str(SHOTS / f"02_{i:02d}_{safe}.png"), full_page=True)
                content = await page.content()
                html_dump.append(f"\n\n===== {text} | {href} =====\n{content}")
            except Exception as e:
                print(f"       error: {e}")

        (ROOT / "homework_page_html.txt").write_text("\n".join(html_dump))
        (ROOT / "api_calls.log").write_text("\n".join(api_log_lines))
        print("[*] Saved homework_page_html.txt and api_calls.log")
        print("[*] Keeping browser open 10s for observation...")
        await asyncio.sleep(10)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

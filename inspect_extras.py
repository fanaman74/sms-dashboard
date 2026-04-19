"""Deep inspect Inbox, Term Reports, Course Info."""
import asyncio, os
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
U, P = os.getenv("SMS_USERNAME"), os.getenv("SMS_PASSWORD")
ROOT = Path(__file__).parent
SHOTS = ROOT / "screenshots"
SHOTS.mkdir(exist_ok=True)

TARGETS = {
    "inbox": "https://sms.eursc.eu/announcements/inbox",
    "term_reports": "https://sms.eursc.eu/content/guardian/term_reports.php",
    "course_info": "https://sms.eursc.eu/content/guardian/student_info.php",
}

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx_args = {"storage_state": "session.json"} if Path("session.json").exists() else {}
        ctx = await browser.new_context(**ctx_args)
        page = await ctx.new_page()

        # ensure login
        await page.goto("https://sms.eursc.eu/content/common/dashboard.php", wait_until="networkidle")
        if "login" in page.url.lower():
            await page.goto("https://sms.eursc.eu/login", wait_until="networkidle")
            await page.fill('input[type="email"], input[name*="user" i], input[name*="email" i]', U)
            await page.fill('input[type="password"]', P)
            await page.click('button[type="submit"], input[type="submit"]')
            await page.wait_for_load_state("networkidle", timeout=15000)
            await ctx.storage_state(path="session.json")

        for name, url in TARGETS.items():
            print(f"[*] {name}: {url}")
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
                await asyncio.sleep(2.5)
                await page.screenshot(path=str(SHOTS / f"extras_{name}.png"), full_page=True)
                (ROOT / f"dom_{name}.html").write_text(await page.content())
                (ROOT / f"text_{name}.txt").write_text(
                    await page.eval_on_selector("body", "el => el.innerText"))
                print(f"    saved dom_{name}.html, text_{name}.txt, screenshot")
            except Exception as e:
                print(f"    error: {e}")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())

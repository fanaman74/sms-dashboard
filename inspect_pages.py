"""Deep inspect of Course Diary + Graded Exercises + Schedule pages."""
import asyncio, os, re
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
U, P = os.getenv("SMS_USERNAME"), os.getenv("SMS_PASSWORD")
ROOT = Path(__file__).parent
SHOTS = ROOT / "screenshots"
APIS = ROOT / "api_responses"

TARGETS = {
    "course_diary": "https://sms.eursc.eu/content/course_diary/course_diary_for_parents.php",
    "graded_exercises": "https://sms.eursc.eu/content/guardian/performance_sheet.php",
    "schedule": "https://sms.eursc.eu/content/guardian/calendar_for_parents.php",
}

api_log = []

async def log_response(response):
    try:
        if response.request.resource_type in ("xhr", "fetch"):
            ct = response.headers.get("content-type", "")
            api_log.append(f"{response.status} {ct} {response.url}")
            if "application/json" in ct or "text/html" in ct and "ajax" in response.url.lower():
                try:
                    body = await response.text()
                    safe = re.sub(r"[^a-zA-Z0-9]+", "_", response.url)[-140:]
                    (APIS / f"{safe}.txt").write_text(body[:500000])
                except Exception:
                    pass
    except Exception:
        pass

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=150)
        context = await browser.new_context()
        page = await context.new_page()
        page.on("response", lambda r: asyncio.create_task(log_response(r)))

        await page.goto("https://sms.eursc.eu/login", wait_until="networkidle")
        await page.fill('input[type="email"], input[name*="user" i], input[name*="email" i]', U)
        await page.fill('input[type="password"]', P)
        await page.click('button[type="submit"], input[type="submit"]')
        await page.wait_for_load_state("networkidle", timeout=15000)
        await asyncio.sleep(2)

        # Save storage state for reuse
        await context.storage_state(path=str(ROOT / "session.json"))
        print("[*] Saved session.json")

        for name, url in TARGETS.items():
            print(f"\n[*] {name}: {url}")
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
                await asyncio.sleep(3)
                await page.screenshot(path=str(SHOTS / f"page_{name}.png"), full_page=True)
                html = await page.content()
                (ROOT / f"dom_{name}.html").write_text(html)
                # Also try to get the main content text
                try:
                    body_text = await page.eval_on_selector("body", "el => el.innerText")
                    (ROOT / f"text_{name}.txt").write_text(body_text)
                except Exception:
                    pass
                print(f"    saved dom_{name}.html + page_{name}.png")
            except Exception as e:
                print(f"    error: {e}")

        (ROOT / "api_calls.log").write_text("\n".join(api_log))
        await asyncio.sleep(5)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())

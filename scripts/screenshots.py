"""Demo evidence screenshots (Step 9). Dev-only: pip install -r requirements-dev.txt

Usage: python -m scripts.screenshots

Starts the app on a spare port (this resets the demo database), drives the installed Google
Chrome with Playwright (channel "chrome", so no browser download), saves four PNGs in results/,
then stops the app. Replay mode only: no API calls.
"""
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
PORT = 8598
URL = f"http://localhost:{PORT}"


def idle(page: Page) -> None:
    """Wait until Streamlit has finished rerunning (its status widget disappears)."""
    page.wait_for_timeout(700)
    page.wait_for_function("() => !document.querySelector('[data-testid=\"stStatusWidget\"]')", timeout=60000)
    page.wait_for_timeout(500)


def click(page: Page, name: str) -> None:
    page.get_by_role("button", name=name, exact=True).click()
    idle(page)


def choose_agent(page: Page, role: str) -> None:
    page.locator('[data-testid="stSelectbox"]').first.click()
    page.get_by_role("option", name=role, exact=True).click()
    idle(page)


def side_by_side(left: Path, right: Path, out: Path, labels: tuple[str, str]) -> None:
    a, b = Image.open(left), Image.open(right)
    gap, header = 24, 44
    canvas = Image.new("RGB", (a.width + b.width + gap, max(a.height, b.height) + header), "white")
    canvas.paste(a, (0, header))
    canvas.paste(b, (a.width + gap, header))
    draw = ImageDraw.Draw(canvas)
    draw.text((8, 12), labels[0], fill="black", font_size=22)
    draw.text((a.width + gap + 8, 12), labels[1], fill="black", font_size=22)
    canvas.save(out)
    left.unlink()
    right.unlink()


def main() -> None:
    app = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py", "--server.headless", "true",
                            "--server.port", str(PORT), "--browser.gatherUsageStats", "false"], cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            for _ in range(60):  # wait for the server
                try:
                    page.goto(URL)
                    break
                except Exception:
                    time.sleep(1)
            page.get_by_role("button", name="Wed 10:00", exact=True).wait_for(timeout=60000)
            idle(page)

            for jump in ("Mon 18:30", "Tue 16:00", "Tue 19:40", "Tue 20:15"):
                click(page, jump)
            page.screenshot(path=RESULTS / "s7_1_superseded.png")

            click(page, "Wed 09:16")
            page.screenshot(path=RESULTS / "s7_2_missed.png")

            click(page, "Wed 10:00")
            page.get_by_role("tab", name="What the agent sees").click()
            tabs = page.locator('[data-testid="stTabs"]')
            shots = []
            for role in ("payments_agent", "credit_agent"):
                choose_agent(page, role)
                click(page, "Preview packet (no model call)")
                shot = RESULTS / f"_packet_{role}.png"
                tabs.screenshot(path=shot)
                shots.append(shot)
            side_by_side(*shots, RESULTS / "s7_3_packets.png",
                         ("payments_agent: PAY-2042 in full (allow)", "credit_agent: PAY-2042 existence only"))

            click(page, "Try to edit history")
            page.mouse.move(1200, 900)  # no hover tooltip over the error
            page.wait_for_timeout(500)
            page.screenshot(path=RESULTS / "s7_4_edit_denied.png")
            browser.close()
    finally:
        app.terminate()
        app.wait(timeout=10)
    for name in ("s7_1_superseded", "s7_2_missed", "s7_3_packets", "s7_4_edit_denied"):
        print(f"saved results/{name}.png")


if __name__ == "__main__":
    main()

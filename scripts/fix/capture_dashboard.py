"""Capture a screenshot of the CloudManagement dashboard.

The dashboard is client-side rendered and fetches data from /api/* endpoints
which require IAM auth. We use a service account identity token via
gcloud, inject it as a cookie-less Authorization header by intercepting
fetch requests in the page context.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from playwright.async_api import async_playwright

# Resolve project root (this script lives in scripts/fix/)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))
from paths import resolve as _resolve_path  # noqa: E402

SERVICE_URL = os.environ.get(
    "CLOUDMANAGEMENT_DASHBOARD_URL",
    "https://your-service-url.a.run.app/dashboard",
)
OUTPUT = _resolve_path("data/audit/dashboard_screenshot.png")


def get_token() -> str:
    return subprocess.run(
        ["gcloud", "auth", "print-identity-token"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


async def main():
    token = get_token()
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1440, "height": 1024},
            device_scale_factor=2,
        )
        page = await context.new_page()

        # Intercept all requests and add the Authorization header
        await page.route("**/*", lambda route: route.continue_(
            headers={**route.request.headers, "Authorization": f"Bearer {token}"}
        ))

        await page.goto(SERVICE_URL, wait_until="networkidle", timeout=30000)
        # Give charts extra time to render
        await page.wait_for_timeout(3000)

        await page.screenshot(path=OUTPUT, full_page=True)
        print(f"Screenshot saved to {OUTPUT}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

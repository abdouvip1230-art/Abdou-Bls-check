"""
BLS Algeria — Login-only checker
Opens the official Algeria portal and performs at most one normal login attempt.
It does not navigate to appointments, check availability, or book anything.
Credentials and Telegram settings are read only from environment variables.
"""

import asyncio
import os
from datetime import datetime
from pathlib import Path

import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

LOGIN_URL = "https://algeria.blsinternational.com/"
BLS_EMAIL = os.getenv("BLS_EMAIL", "").strip()
BLS_PASSWORD = os.getenv("BLS_PASSWORD", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SCREENSHOT_DIR = Path(os.getenv("SCREENSHOT_DIR", "screenshots"))
TIMEOUT_MS = 20_000


def telegram_request(method: str, *, data=None, files=None) -> bool:
    """Send a Telegram API request without exposing the bot token in logs."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] Not configured; skipping notification.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        response = requests.post(url, data=data, files=files, timeout=25)
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            print("[Telegram] API reported that the request was not successful.")
            return False
        print("[Telegram] Notification sent.")
        return True
    except Exception as exc:
        # Do not print the request URL: it contains the bot token.
        print(f"[Telegram] Notification failed: {type(exc).__name__}")
        return False


def send_telegram(message: str) -> bool:
    return telegram_request(
        "sendMessage",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
    )


def send_telegram_photo(path: Path, caption: str) -> bool:
    if not path.exists():
        return False
    try:
        with path.open("rb") as photo:
            return telegram_request(
                "sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:900]},
                files={"photo": photo},
            )
    except Exception as exc:
        print(f"[Telegram] Screenshot delivery failed: {type(exc).__name__}")
        return False


async def save_screenshot(page, name: str) -> Path | None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"{name}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
        print(f"[Screenshot] Saved: {path}")
        return path
    except Exception as exc:
        print(f"[Screenshot] Could not save screenshot: {type(exc).__name__}")
        return None


def page_has_security_challenge(text: str, page_url: str) -> bool:
    combined = f"{text} {page_url}".lower()
    indicators = (
        "captcha",
        "verify you are human",
        "verification required",
        "security verification",
        "unusual activity",
        "robot check",
        "cf-chl-",
        "challenge-platform",
    )
    return any(item in combined for item in indicators)


async def first_visible(locator, timeout: int = 5000):
    """Return the first visible locator from a selector group, if any."""
    count = await locator.count()
    for index in range(min(count, 8)):
        item = locator.nth(index)
        try:
            if await item.is_visible():
                return item
        except Exception:
            continue
    try:
        await locator.first.wait_for(state="visible", timeout=timeout)
        return locator.first
    except Exception:
        return None


async def run_login_check() -> int:
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    print("=" * 58)
    print("BLS Algeria — Login-only check")
    print(f"Time: {timestamp}")
    print("=" * 58)

    if not BLS_EMAIL or not BLS_PASSWORD:
        message = "BLS login check: configuration error. BLS_EMAIL or BLS_PASSWORD is missing."
        print(f"[Configuration] {message}")
        send_telegram(message)
        return 2

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1365, "height": 900},
            locale="en-DZ",
        )
        page = await context.new_page()
        page.set_default_timeout(TIMEOUT_MS)

        try:
            print(f"[1/4] Opening official portal: {LOGIN_URL}")
            try:
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(2500)
            except PlaywrightTimeout:
                print("[Page] Initial navigation timed out; inspecting the page that loaded.")

            body_text = ""
            try:
                body_text = await page.locator("body").inner_text(timeout=5000)
            except Exception:
                pass

            screenshot = await save_screenshot(page, "01_portal_opened")

            if page_has_security_challenge(body_text, page.url):
                print("[Security] A verification challenge is present. No login attempt was made.")
                send_telegram(
                    "BLS login check paused: the website displayed a security verification/CAPTCHA. "
                    "No attempt was made to bypass it. Open the official site and complete verification manually."
                )
                if screenshot:
                    send_telegram_photo(screenshot, "BLS security verification page")
                return 3

            # If the session is already authenticated, do not submit credentials again.
            authenticated_markers = (
                "logout",
                "log out",
                "sign out",
                "my account",
                "dashboard",
            )
            lower_text = body_text.lower()
            login_fields = page.locator(
                'input[type="password"], input[name*="password" i], input[id*="password" i]'
            )
            password_count = await login_fields.count()
            if any(marker in lower_text for marker in authenticated_markers) and password_count == 0:
                print("[Result] The page appears to show an authenticated session.")
                send_telegram("BLS login check: the portal appears to already be signed in.")
                return 0

            print("[2/4] Looking for the sign-in form.")
            email_selectors = (
                'input[type="email"]',
                'input[name="UserId"]',
                'input[id="UserId"]',
                'input[name*="email" i]',
                'input[id*="email" i]',
                'input[placeholder*="email" i]',
                'input[placeholder*="user" i]',
            )
            email_input = None
            for selector in email_selectors:
                email_input = await first_visible(page.locator(selector), timeout=1500)
                if email_input:
                    break

            password_input = await first_visible(
                page.locator('input[type="password"], input[name*="password" i], input[id*="password" i]'),
                timeout=3000,
            )

            if not email_input or not password_input:
                print("[Form] Login fields were not found. No credentials were submitted.")
                screenshot = await save_screenshot(page, "02_login_form_not_found")
                send_telegram(
                    "BLS login check: could not identify the login form on the page. "
                    "No credentials were submitted; inspect the attached screenshot."
                )
                if screenshot:
                    send_telegram_photo(screenshot, "BLS page — login form not found")
                return 4

            print("[3/4] Filling the sign-in form.")
            await email_input.fill(BLS_EMAIL)
            await password_input.fill(BLS_PASSWORD)
            # Do not capture the form after credentials have been entered.
            # Re-check for challenge indicators immediately before the single submit.
            current_text = ""
            try:
                current_text = await page.locator("body").inner_text(timeout=3000)
            except Exception:
                pass
            if page_has_security_challenge(current_text, page.url):
                print("[Security] Verification appeared before submission. Stopping.")
                send_telegram("BLS login check paused: a security verification appeared. No login form was submitted.")
                if screenshot:
                    send_telegram_photo(screenshot, "BLS verification page")
                return 3

            print("[4/4] Submitting the login form once.")
            submit_selectors = (
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Login")',
                'button:has-text("Log in")',
                'button:has-text("Sign in")',
                'input[value*="Login" i]',
            )
            submit_button = None
            for selector in submit_selectors:
                submit_button = await first_visible(page.locator(selector), timeout=1200)
                if submit_button:
                    break

            if not submit_button:
                print("[Form] Submit button was not found. No submission was made.")
                screenshot = await save_screenshot(page, "04_submit_button_not_found")
                send_telegram("BLS login check: the login submit button was not found; no submission was made.")
                if screenshot:
                    send_telegram_photo(screenshot, "BLS login page — submit button not found")
                return 4

            await submit_button.click()
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except PlaywrightTimeout:
                pass
            await page.wait_for_timeout(3500)

            final_text = ""
            try:
                final_text = await page.locator("body").inner_text(timeout=5000)
            except Exception:
                pass
            final_url = page.url
            final_lower = final_text.lower()

            screenshot = await save_screenshot(page, "05_after_login_attempt")

            if page_has_security_challenge(final_text, final_url):
                print("[Security] Verification appeared after submission.")
                send_telegram(
                    "BLS login check: the portal requested additional security verification after submission. "
                    "The script stopped and did not try to bypass it."
                )
                if screenshot:
                    send_telegram_photo(screenshot, "BLS — verification after login attempt")
                return 3

            failure_markers = (
                "invalid username",
                "invalid password",
                "incorrect password",
                "incorrect username",
                "login failed",
                "invalid credentials",
                "user name or password",
                "email or password",
            )
            if any(marker in final_lower for marker in failure_markers):
                print("[Result] The website displayed a login failure message.")
                send_telegram("BLS login check: the website reported that the login details were not accepted.")
                if screenshot:
                    send_telegram_photo(screenshot, "BLS — login rejected")
                return 1

            # Treat success as confirmed only when a login form is gone and an authenticated marker is visible.
            remaining_password_fields = page.locator('input[type="password"]')
            remaining_password_count = await remaining_password_fields.count()
            success_markers = ("logout", "log out", "sign out", "my account", "dashboard", "profile")
            if remaining_password_count == 0 and any(marker in final_lower for marker in success_markers):
                print(f"[Result] Login appears successful. Current page: {final_url}")
                send_telegram("BLS login check: sign-in appears successful. No appointment or booking actions were performed.")
                if screenshot:
                    send_telegram_photo(screenshot, "BLS — sign-in appears successful")
                return 0

            print("[Result] Login outcome is inconclusive; no further actions will be taken.")
            send_telegram(
                "BLS login check: the result is inconclusive. The script made one sign-in submission "
                "and stopped. Please inspect the attached screenshot."
            )
            if screenshot:
                send_telegram_photo(screenshot, "BLS — inconclusive login result")
            return 5

        except Exception as exc:
            print(f"[Error] {type(exc).__name__}: {exc}")
            screenshot = await save_screenshot(page, "error_unexpected")
            send_telegram(f"BLS login check encountered an error: {type(exc).__name__}.")
            if screenshot:
                send_telegram_photo(screenshot, "BLS — unexpected error")
            return 10
        finally:
            await context.close()
            await browser.close()


def main():
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        send_telegram(
            "BLS Algeria login-only check started. "
            "The script will make at most one sign-in submission and will not check or book appointments."
        )
    exit_code = asyncio.run(run_login_check())
    print(f"[Finished] Exit code: {exit_code}")
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()

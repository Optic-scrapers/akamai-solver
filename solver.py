import asyncio
import glob
import random
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

import psutil
from cloakbrowser import launch_async
from playwright.async_api import Browser, BrowserContext, Page

from utils import Session, log, parse_proxy

PROFILE_PATTERNS = (
    "/tmp/.org.chromium*",
    "/tmp/playwright-artifacts-*",
    "/tmp/playwright_chromiumdev_profile-*",
)
PROCESS_NAMES = {
    "chrome",
    "chromium",
    "cloakbrowser",
    "playwright",
}
WINDOW_SIZE = {"width": 1920, "height": 1080}


def iter_solver_processes() -> list[psutil.Process]:
    processes = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if name in PROCESS_NAMES:
            processes.append(proc)
            continue
        try:
            cmdline = " ".join(proc.info["cmdline"] or []).lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if "cloakbrowser" in cmdline or "chromium" in cmdline:
            processes.append(proc)
    return processes


def snapshot_tmp_profiles() -> set[Path]:
    return {Path(path) for pattern in PROFILE_PATTERNS for path in glob.glob(pattern)}


def cleanup_tmp_profiles(existing_paths: set[Path]) -> None:
    for pattern in PROFILE_PATTERNS:
        for path_str in glob.glob(pattern):
            path = Path(path_str)
            if path in existing_paths:
                continue
            try:
                shutil.rmtree(path) if path.is_dir() else path.unlink()
            except OSError:
                continue


def cleanup_solver_processes(existing_pids: set[int]) -> None:
    targets: dict[int, psutil.Process] = {}
    for proc in iter_solver_processes():
        if proc.pid in existing_pids:
            continue
        targets[proc.pid] = proc
        try:
            for child in proc.children(recursive=True):
                if child.pid not in existing_pids:
                    targets[child.pid] = child
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    processes = list(targets.values())
    for proc in processes:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    _, alive = psutil.wait_procs(processes, timeout=3)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    psutil.wait_procs(alive, timeout=3)


def build_headers(captured: dict[str, str], user_agent: str) -> dict[str, str]:
    headers = {
        key: value
        for key, value in captured.items()
        if not key.startswith(":") and key.lower() != "cookie"
    }
    headers.setdefault("user-agent", user_agent)
    return headers


def landing_url(target_url: str) -> str:
    parts = urlsplit(target_url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"invalid target_url: {target_url}")
    return f"{parts.scheme}://{parts.netloc}"


async def solve(
    target_url: str,
    proxy: str | None,
    solver_name: str = "akamai",
) -> Session:
    domain = urlsplit(target_url).netloc
    existing_pids = {proc.pid for proc in iter_solver_processes()}
    existing_profiles = snapshot_tmp_profiles()
    browser: Browser | None = None

    proxy_config = None
    if proxy:
        proxy_user, proxy_pass, proxy_server = parse_proxy(proxy)
        proxy_config = {
            "server": f"http://{proxy_server}",
            "username": proxy_user,
            "password": proxy_pass,
        }

    log.info("solving", backend="cloakbrowser", domain=domain, proxy=proxy, solver=solver_name)
    try:
        try:
            async with asyncio.timeout(60):
                browser = await launch_async(
                    proxy=proxy_config,
                    headless=False,
                    humanize=True,
                    locale="en-US",
                    timezone="America/Chicago",
                    args=[f"--window-size={WINDOW_SIZE['width']},{WINDOW_SIZE['height']}", "--window-position=0,0"],
                )
                context: BrowserContext = await browser.new_context(no_viewport=True)
                page: Page = await context.new_page()
                captured: dict[str, str] = {}

                async def on_request(request) -> None:
                    if captured:
                        return
                    if request.url.startswith(target_url):
                        captured.update(await request.all_headers())

                page.on("request", on_request)
                user_agent = await page.evaluate("navigator.userAgent")
                
                await page.goto(landing_url(target_url), wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                
                viewport = await page.evaluate("() => ({width: window.innerWidth, height: window.innerHeight})")
                await page.mouse.move(
                    random.randint(0, viewport["width"] - 1),
                    random.randint(0, viewport["height"] - 1),
                    steps=random.randint(8, 20),
                )
                await page.mouse.wheel(0, random.randint(300, 900))
                
                response = await page.goto(target_url, wait_until="domcontentloaded")
                if response and response.status >= 400:
                    raise RuntimeError(f"challenge url returned {response.status}")
                
                cookies = await context.cookies()
                if not cookies:
                    raise RuntimeError("no cookies")
                log.info("solved", backend="cloakbrowser", domain=domain, proxy=proxy, solver=solver_name)
                return Session(
                    cookies={cookie["name"]: cookie["value"] for cookie in cookies},
                    headers=build_headers(captured, user_agent),
                    proxy=proxy,
                    extra={
                        "created_at": str(time.time_ns() // 1_000_000),
                        "solver": solver_name,
                    },
                )
        except asyncio.TimeoutError:
            log.warning("solver_timeout", backend="cloakbrowser", domain=domain, proxy=proxy, solver=solver_name)
            raise
        except Exception:
            log.exception("solving_failed", backend="cloakbrowser", domain=domain, proxy=proxy, solver=solver_name)
            raise
    finally:
        if browser:
            await browser.close()
        cleanup_solver_processes(existing_pids)
        cleanup_tmp_profiles(existing_profiles)

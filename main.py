import asyncio
import json
import logging
import os
import random
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import List, Set, Optional, Dict, Any
import time
import traceback
import aiofiles
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout, BrowserContext

# ================= PRODUCTION CONFIG =================
PRODUCTION_CONFIG = {
    "log_level": os.getenv("LOG_LEVEL", "INFO"),
    "port": int(os.getenv("PORT", 8000)),
    "host": os.getenv("HOST", "0.0.0.0"),
    "workers": int(os.getenv("WORKERS", 1)),
    "max_concurrency": int(os.getenv("MAX_CONCURRENCY", 3)),
    "headless": os.getenv("HEADLESS", "true").lower() == "true",
    "timeout": int(os.getenv("TIMEOUT", 30000)),
    "max_retries": int(os.getenv("MAX_RETRIES", 2)),
    "cookie_dir": os.getenv("COOKIE_DIR", "cookies"),
    "results_dir": os.getenv("RESULTS_DIR", "results"),
    "log_file": os.getenv("LOG_FILE", "logs/bot.log"),
    "enable_monitoring": os.getenv("ENABLE_MONITORING", "true").lower() == "true",
}

# ================= PRODUCTION LOGGER =================
class ProductionLogger:
    def __init__(self):
        self.log_dir = Path("logs")
        self.log_dir.mkdir(exist_ok=True)
        
        self.logger = logging.getLogger("AdjarabetBot")
        self.logger.setLevel(getattr(logging, PRODUCTION_CONFIG["log_level"]))
        
        # File handler with rotation support
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(
            PRODUCTION_CONFIG["log_file"],
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5,
            encoding="utf-8"
        )
        
        # Console handler
        console_handler = logging.StreamHandler()
        
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
    
    def info(self, msg): self.logger.info(msg)
    def error(self, msg): self.logger.error(msg)
    def warning(self, msg): self.logger.warning(msg)
    def debug(self, msg): self.logger.debug(msg)

# ================= ENUMS =================
class AccountStatus(Enum):
    SUCCESS = "✅"
    FAILED = "❌"
    TIMEOUT = "⏰"
    BLOCKED = "🚫"
    PROXY_ERROR = "🔄"

# ================= DATA =================
@dataclass
class Account:
    username: str
    password: str
    status: AccountStatus = AccountStatus.FAILED
    balance: str = ""
    balance_value: float = 0.0
    error: str = ""
    cookies: Optional[List[Dict]] = None
    proxy: Optional[str] = None
    timestamp: str = ""
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()

@dataclass
class Config:
    headless: bool = PRODUCTION_CONFIG["headless"]
    max_retries: int = PRODUCTION_CONFIG["max_retries"]
    concurrency: int = PRODUCTION_CONFIG["max_concurrency"]
    timeout: int = PRODUCTION_CONFIG["timeout"]
    delay_min: float = 0.5
    delay_max: float = 1.0
    use_proxies: bool = True
    use_stealth: bool = True
    reuse_cookies: bool = True

# ================= PRODUCTION METRICS =================
class MetricsCollector:
    def __init__(self):
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None
        self.total_accounts = 0
        self.successful = 0
        self.failed = 0
        self.timeouts = 0
        self.blocked = 0
        self.proxy_errors = 0
        self._lock = asyncio.Lock()
    
    async def start(self):
        async with self._lock:
            self.start_time = datetime.now()
    
    async def stop(self):
        async with self._lock:
            self.end_time = datetime.now()
    
    async def record_result(self, account: Account):
        async with self._lock:
            self.total_accounts += 1
            if account.status == AccountStatus.SUCCESS:
                self.successful += 1
            elif account.status == AccountStatus.FAILED:
                self.failed += 1
            elif account.status == AccountStatus.TIMEOUT:
                self.timeouts += 1
            elif account.status == AccountStatus.BLOCKED:
                self.blocked += 1
            elif account.status == AccountStatus.PROXY_ERROR:
                self.proxy_errors += 1
    
    async def get_metrics(self) -> Dict[str, Any]:
        async with self._lock:
            duration = None
            if self.start_time and self.end_time:
                duration = (self.end_time - self.start_time).total_seconds()
            
            return {
                "total": self.total_accounts,
                "successful": self.successful,
                "failed": self.failed,
                "timeouts": self.timeouts,
                "blocked": self.blocked,
                "proxy_errors": self.proxy_errors,
                "success_rate": (self.successful / self.total_accounts * 100) if self.total_accounts > 0 else 0,
                "duration_seconds": duration,
                "accounts_per_minute": (self.total_accounts / (duration / 60)) if duration and duration > 0 else 0
            }

# ================= PROXY MANAGER (Enhanced) =================
class ProxyManager:
    def __init__(self, proxy_file: str = "proxies.txt"):
        self.proxy_file = Path(proxy_file)
        self.proxies: List[str] = []
        self.proxy_index = 0
        self._lock = asyncio.Lock()
        self.failed_proxies: Set[str] = set()
        self.proxy_stats: Dict[str, int] = {}
        self.load_proxies()
    
    def load_proxies(self):
        if not self.proxy_file.exists():
            return
        
        try:
            content = self.proxy_file.read_text(encoding="utf-8")
            self.proxies = [
                line.strip() for line in content.splitlines() 
                if line.strip() and not line.startswith("#")
            ]
            if self.proxies:
                random.shuffle(self.proxies)
                for proxy in self.proxies:
                    self.proxy_stats[proxy] = 0
        except Exception as e:
            pass
    
    async def get_proxy(self) -> Optional[str]:
        if not self.proxies:
            return None
        
        async with self._lock:
            for _ in range(len(self.proxies)):
                proxy = self.proxies[self.proxy_index]
                self.proxy_index = (self.proxy_index + 1) % len(self.proxies)
                
                if proxy not in self.failed_proxies:
                    return proxy
            
            self.failed_proxies.clear()
            return self.proxies[0] if self.proxies else None
    
    def mark_failed(self, proxy: str):
        self.failed_proxies.add(proxy)
        if proxy in self.proxy_stats:
            self.proxy_stats[proxy] += 1
    
    def mark_success(self, proxy: str):
        self.failed_proxies.discard(proxy)

# ================= HEALTH CHECK =================
class HealthChecker:
    def __init__(self):
        self.status = "healthy"
        self.last_check = datetime.now()
        self.errors: List[str] = []
    
    async def check(self) -> Dict[str, Any]:
        checks = {
            "browser": await self._check_browser(),
            "disk_space": await self._check_disk_space(),
            "memory": await self._check_memory()
        }
        
        self.status = "healthy" if all(checks.values()) else "degraded"
        self.last_check = datetime.now()
        
        return {
            "status": self.status,
            "timestamp": self.last_check.isoformat(),
            "checks": checks,
            "errors": self.errors[-5:]  # Last 5 errors
        }
    
    async def _check_browser(self) -> bool:
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                await browser.close()
            return True
        except Exception as e:
            self.errors.append(f"Browser check failed: {e}")
            return False
    
    async def _check_disk_space(self) -> bool:
        try:
            import shutil
            usage = shutil.disk_usage(Path.cwd())
            free_gb = usage.free / (1024**3)
            return free_gb > 0.5  # At least 500MB free
        except:
            return True
    
    async def _check_memory(self) -> bool:
        try:
            import psutil
            memory = psutil.virtual_memory()
            return memory.percent < 90  # Less than 90% usage
        except ImportError:
            return True  # psutil not installed, skip check

# ================= WEBSOCKET MANAGER (Enhanced) =================
class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            self.active_connections.add(websocket)

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            self.active_connections.discard(websocket)

    async def broadcast(self, message: str):
        if not self.active_connections:
            return
        
        async with self._lock:
            connections = list(self.active_connections)
        
        disconnected = []
        for conn in connections:
            try:
                await conn.send_text(message)
            except:
                disconnected.append(conn)
        
        for conn in disconnected:
            await self.disconnect(conn)

# ================= CORE BOT (Production Ready) =================
class Bot:
    def __init__(self, config: Config):
        self.cfg = config
        self.log = ProductionLogger()
        self.sem = asyncio.Semaphore(config.concurrency)
        self.login_url = "https://www.adjarabet.am/hy"
        self._running = False
        self._current_results: List[Account] = []
        self._processed_count = 0
        self._total_accounts = 0
        self._is_processing = False
        
        self.proxy_manager = ProxyManager() if config.use_proxies else None
        self.cookie_manager = None
        if config.reuse_cookies:
            from pathlib import Path
            Path(PRODUCTION_CONFIG["cookie_dir"]).mkdir(exist_ok=True)
            self.cookie_manager = CookieManager(PRODUCTION_CONFIG["cookie_dir"]) if 'CookieManager' in globals() else None
        
        self.pw = None
        self.browser = None
        self._context_pool: List[BrowserContext] = []
        self._pool_lock = asyncio.Lock()
        self.metrics = MetricsCollector()
        self.health_checker = HealthChecker()
    
    async def start(self):
        self.log.info("Starting Playwright...")
        self.pw = await async_playwright().start()
        
        self.log.info(f"Launching browser (headless={self.cfg.headless})...")
        self.browser = await self.pw.chromium.launch(
            headless=self.cfg.headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-accelerated-2d-canvas',
                '--disable-gpu'
            ]
        )
        
        await self._init_context_pool()
        self._running = True
        self.log.info("Bot started successfully")
    
    async def _init_context_pool(self):
        async with self._pool_lock:
            pool_size = min(self.cfg.concurrency, 3)
            for _ in range(pool_size):
                context = await self._create_context()
                self._context_pool.append(context)
    
    async def _get_context(self) -> BrowserContext:
        async with self._pool_lock:
            if self._context_pool:
                return self._context_pool.pop()
        return await self._create_context()
    
    async def _return_context(self, context: BrowserContext):
        try:
            await context.clear_cookies()
            async with self._pool_lock:
                if len(self._context_pool) < 5:
                    self._context_pool.append(context)
                else:
                    await context.close()
        except:
            try:
                await context.close()
            except:
                pass
    
    async def _create_context(self, proxy: Optional[str] = None) -> BrowserContext:
        viewport = {'width': 1920, 'height': 1080}
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        locale = "hy-AM"
        timezone_id = "Asia/Yerevan"
        
        proxy_config = {"server": proxy} if proxy else None
        
        context = await self.browser.new_context(
            viewport=viewport,
            user_agent=user_agent,
            locale=locale,
            timezone_id=timezone_id,
            proxy=proxy_config
        )
        
        return context
    
    async def stop(self):
        self.log.info("Stopping bot...")
        self._running = False
        
        async with self._pool_lock:
            for context in self._context_pool:
                try:
                    await context.close()
                except:
                    pass
            self._context_pool.clear()
        
        if self.browser:
            try:
                await self.browser.close()
            except:
                pass
            self.browser = None
        
        if self.pw:
            try:
                await self.pw.stop()
            except:
                pass
            self.pw = None
    
    async def login(self, account: Account):
        async with self.sem:
            for attempt in range(self.cfg.max_retries):
                context = None
                page = None
                
                try:
                    context = await self._get_context()
                    page = await context.new_page()
                    
                    await page.goto(self.login_url, wait_until="domcontentloaded", timeout=self.cfg.timeout)
                    await asyncio.sleep(random.uniform(0.5, 1.0))
                    
                    # Check if already logged in
                    try:
                        balance_el = await page.wait_for_selector('[data-test-id="header-user-balance"]', timeout=2000)
                        if balance_el:
                            balance_text = await balance_el.inner_text()
                            account.balance = balance_text
                            account.balance_value = self._parse_balance(balance_text)
                            account.status = AccountStatus.SUCCESS
                            self.log.info(f"✅ {account.username} | {balance_text}")
                            await self._save_single_result(account)
                            await self.metrics.record_result(account)
                            await self._return_context(context)
                            return
                    except:
                        pass
                    
                    # Fill login form
                    await page.fill('input[name="userIdentifier"]', account.username)
                    await asyncio.sleep(random.uniform(0.3, 0.7))
                    await page.fill('input[type="password"]', account.password)
                    await asyncio.sleep(random.uniform(0.3, 0.7))
                    
                    # Click login
                    await page.click('[data-test-id="header-login-button"]')
                    
                    # Wait for balance
                    try:
                        balance_el = await page.wait_for_selector('[data-test-id="header-user-balance"]', timeout=self.cfg.timeout)
                        balance_text = await balance_el.inner_text()
                        account.balance = balance_text
                        account.balance_value = self._parse_balance(balance_text)
                        account.status = AccountStatus.SUCCESS
                        self.log.info(f"✅ {account.username} | {balance_text}")
                        await self._save_single_result(account)
                        await self.metrics.record_result(account)
                        await self._return_context(context)
                        return
                    
                    except PlaywrightTimeout:
                        account.status = AccountStatus.TIMEOUT
                        account.error = "Timeout waiting for balance"
                        self.log.warning(f"⏰ {account.username} TIMEOUT")
                        await self._save_single_result(account)
                        await self.metrics.record_result(account)
                        await self._return_context(context)
                        return
                
                except Exception as e:
                    account.error = str(e)[:100]
                    
                    if attempt == self.cfg.max_retries - 1:
                        account.status = AccountStatus.FAILED
                        self.log.error(f"❌ {account.username} FAILED: {e}")
                        await self._save_single_result(account)
                        await self.metrics.record_result(account)
                
                finally:
                    if page:
                        try:
                            await page.close()
                        except:
                            pass
                    
                    if context and account.status != AccountStatus.SUCCESS:
                        try:
                            await context.close()
                        except:
                            pass
                
                if attempt < self.cfg.max_retries - 1:
                    await asyncio.sleep(2)
    
    def _parse_balance(self, text: str) -> float:
        try:
            num = re.sub(r"[^\d.]", "", text)
            return float(num) if num else 0.0
        except:
            return 0.0
    
    async def _save_single_result(self, account: Account):
        self._current_results.append(account)
        self._processed_count += 1
        
        # Save to JSON
        results_dir = Path(PRODUCTION_CONFIG["results_dir"])
        results_dir.mkdir(exist_ok=True)
        
        data = [
            {
                "username": acc.username,
                "password": acc.password,
                "balance": acc.balance,
                "balance_value": acc.balance_value,
                "status": acc.status.value,
                "error": acc.error,
                "timestamp": acc.timestamp
            }
            for acc in self._current_results
        ]
        
        async with aiofiles.open(results_dir / "results.json", "w", encoding="utf-8") as f:
            await f.write(json.dumps(data, indent=2, ensure_ascii=False))
        
        await manager.broadcast(f"RESULT:{json.dumps(data[-1])}")
        await manager.broadcast(f"PROGRESS:{self._processed_count}/{self._total_accounts}")
    
    async def process_accounts(self):
        if self._is_processing:
            self.log.warning("Already processing accounts, skipping...")
            return
        
        self._is_processing = True
        await self.metrics.start()
        
        try:
            loader = AccountLoader()
            accounts = loader.load()
            
            if not accounts:
                self.log.warning("No accounts found!")
                return
            
            self._total_accounts = len(accounts)
            self._processed_count = 0
            self._current_results = []
            
            self.log.info(f"📊 Processing {self._total_accounts} accounts")
            await manager.broadcast(f"INFO: 📊 Processing {self._total_accounts} accounts")
            
            # Clear previous results
            results_dir = Path(PRODUCTION_CONFIG["results_dir"])
            results_dir.mkdir(exist_ok=True)
            async with aiofiles.open(results_dir / "results.json", "w", encoding="utf-8") as f:
                await f.write("[]")
            
            # Process all accounts
            tasks = [self.login(acc) for acc in accounts]
            await asyncio.gather(*tasks)
            
            await self._save_final_results()
            await self._print_summary()
            
            await manager.broadcast("STATUS:completed")
            self._running = False
            
        finally:
            await self.metrics.stop()
            self._is_processing = False
    
    async def _save_final_results(self):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sorted_accounts = sorted(self._current_results, key=lambda x: x.balance_value, reverse=True)
        
        results_dir = Path(PRODUCTION_CONFIG["results_dir"])
        results_dir.mkdir(exist_ok=True)
        
        async with aiofiles.open(results_dir / "results.txt", "a", encoding="utf-8") as f:
            await f.write(f"\n{'='*60}\n{timestamp}\n{'='*60}\n")
            for acc in sorted_accounts:
                await f.write(f"{acc.status.value} | {acc.username}:{acc.password} | {acc.balance} | {acc.error}\n")
    
    async def _print_summary(self):
        metrics = await self.metrics.get_metrics()
        summary = f"📈 Summary: {metrics['successful']}/{metrics['total']} ({metrics['success_rate']:.1f}%) | Speed: {metrics['accounts_per_minute']:.1f}/min"
        self.log.info(summary)
        await manager.broadcast(f"SUMMARY:{metrics['successful']}/{metrics['total']}:{metrics['success_rate']:.1f}")
    
    async def get_health(self) -> Dict[str, Any]:
        return await self.health_checker.check()
    
    async def get_metrics(self) -> Dict[str, Any]:
        return await self.metrics.get_metrics()

# ================= ACCOUNT LOADER =================
class AccountLoader:
    def __init__(self, file="accounts.txt"):
        self.file = Path(file)
    
    def load(self) -> List[Account]:
        accs = []
        if not self.file.exists():
            return []
        
        content = self.file.read_text(encoding="utf-8")
        
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                if ":" in line:
                    parts = line.split(":", 2)
                    u = parts[0].strip()
                    p = parts[1].strip()
                    proxy = parts[2].strip() if len(parts) > 2 else None
                    accs.append(Account(u, p, proxy=proxy))
        
        return accs

# ================= COOKIE MANAGER =================
class CookieManager:
    def __init__(self, cookie_dir: str = "cookies"):
        self.cookie_dir = Path(cookie_dir)
        self.cookie_dir.mkdir(exist_ok=True)
    
    def get_cookie_file(self, username: str) -> Path:
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', username)
        return self.cookie_dir / f"{safe_name}.json"
    
    async def load_cookies(self, username: str) -> Optional[List[Dict]]:
        cookie_file = self.get_cookie_file(username)
        if not cookie_file.exists():
            return None
        
        try:
            async with aiofiles.open(cookie_file, "r", encoding="utf-8") as f:
                content = await f.read()
                cookies = json.loads(content)
                if self._are_cookies_valid(cookies):
                    return cookies
        except:
            pass
        return None
    
    async def save_cookies(self, username: str, cookies: List[Dict]):
        cookie_file = self.get_cookie_file(username)
        try:
            async with aiofiles.open(cookie_file, "w", encoding="utf-8") as f:
                await f.write(json.dumps(cookies, indent=2))
        except:
            pass
    
    def _are_cookies_valid(self, cookies: List[Dict]) -> bool:
        current_time = time.time()
        for cookie in cookies:
            if "expires" in cookie and cookie["expires"] > current_time:
                return True
        return len(cookies) > 0

# ================= FASTAPI APP (Production) =================
app = FastAPI(
    title="Adjarabet Bot Pro API",
    version="10.0",
    description="Production-ready account management system",
    docs_url="/api/docs" if PRODUCTION_CONFIG["enable_monitoring"] else None,
    redoc_url="/api/redoc" if PRODUCTION_CONFIG["enable_monitoring"] else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

bot_instance: Optional[Bot] = None
bot_task: Optional[asyncio.Task] = None
manager = ConnectionManager()

@app.on_event("startup")
async def startup_event():
    logger = ProductionLogger()
    logger.info("=" * 60)
    logger.info("🎮 ADJARABET BOT PRO v10.0 - PRODUCTION MODE")
    logger.info("=" * 60)
    logger.info(f"📁 Working directory: {Path.cwd()}")
    logger.info(f"🌐 Web interface: http://{PRODUCTION_CONFIG['host']}:{PRODUCTION_CONFIG['port']}")
    logger.info(f"📊 API docs: http://{PRODUCTION_CONFIG['host']}:{PRODUCTION_CONFIG['port']}/api/docs")

@app.on_event("shutdown")
async def shutdown_event():
    global bot_instance, bot_task
    
    if bot_task and not bot_task.done():
        bot_task.cancel()
        try:
            await bot_task
        except:
            pass
    
    if bot_instance:
        await bot_instance.stop()

@app.get("/")
async def root():
    html_file = Path("index.html")
    if not html_file.exists():
        # Create HTML file (same as before)
        html_content = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Adjarabet Bot Pro v10.0 - Production</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; padding: 20px; }
        .container { max-width: 1400px; margin: 0 auto; }
        .header { text-align: center; color: white; margin-bottom: 30px; }
        .header h1 { font-size: 2.5em; margin-bottom: 10px; text-shadow: 2px 2px 4px rgba(0,0,0,0.2); }
        .header p { font-size: 1.1em; opacity: 0.9; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
        .card { background: white; border-radius: 12px; padding: 20px; box-shadow: 0 10px 40px rgba(0,0,0,0.1); }
        .card h2 { margin-bottom: 15px; color: #333; font-size: 1.5em; border-left: 4px solid #667eea; padding-left: 12px; }
        .status-bar { background: white; border-radius: 12px; padding: 15px 20px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 10px 40px rgba(0,0,0,0.1); }
        .status-badge { display: inline-flex; align-items: center; gap: 8px; padding: 8px 16px; border-radius: 20px; font-weight: 600; }
        .status-stopped { background: #f8d7da; color: #721c24; }
        .status-running { background: #d4edda; color: #155724; animation: pulse 2s infinite; }
        .status-completed { background: #d1ecf1; color: #0c5460; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.7; } }
        textarea { width: 100%; min-height: 300px; padding: 12px; border: 2px solid #e0e0e0; border-radius: 8px; font-family: monospace; font-size: 14px; resize: vertical; }
        textarea:focus { outline: none; border-color: #667eea; }
        button { padding: 10px 20px; margin: 5px; border: none; border-radius: 8px; cursor: pointer; font-weight: 600; transition: transform 0.2s, box-shadow 0.2s; }
        button:hover { transform: translateY(-2px); box-shadow: 0 5px 15px rgba(0,0,0,0.2); }
        .btn-start { background: #28a745; color: white; }
        .btn-stop { background: #dc3545; color: white; }
        .btn-clear { background: #ffc107; color: #333; }
        .progress { margin-top: 15px; }
        .progress-bar { background: #e0e0e0; border-radius: 10px; overflow: hidden; height: 30px; margin-top: 10px; }
        .progress-fill { background: linear-gradient(90deg, #667eea, #764ba2); height: 100%; display: flex; align-items: center; justify-content: center; color: white; font-weight: 600; transition: width 0.3s; }
        table { width: 100%; border-collapse: collapse; margin-top: 15px; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #e0e0e0; }
        th { background: #f8f9fa; font-weight: 600; color: #333; }
        tr:hover { background: #f8f9fa; }
        .stats { display: flex; gap: 15px; margin-top: 15px; flex-wrap: wrap; }
        .stat { flex: 1; background: #f8f9fa; padding: 15px; border-radius: 8px; text-align: center; }
        .stat-value { font-size: 2em; font-weight: bold; color: #667eea; }
        .stat-label { color: #666; margin-top: 5px; }
        @media (max-width: 768px) { .grid { grid-template-columns: 1fr; } }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎮 Adjarabet Bot Pro v10.0</h1>
            <p>Professional Account Management System - Production Mode</p>
        </div>
        
        <div class="status-bar">
            <div>🤖 Bot Status</div>
            <div id="status" class="status-badge status-stopped">⛔ Stopped</div>
        </div>
        
        <div class="grid">
            <div class="card">
                <h2>📝 Accounts Configuration</h2>
                <textarea id="accounts" placeholder="Format: username:password or username:password:proxy&#10;&#10;Example:&#10;john_doe:pass123&#10;jane_smith:secure456:http://proxy1:8080"></textarea>
                <div>
                    <button class="btn-start" onclick="startBot()">▶ Start Bot</button>
                    <button class="btn-stop" onclick="stopBot()">⏹ Stop Bot</button>
                    <button class="btn-clear" onclick="clearAccounts()">🗑 Clear</button>
                </div>
            </div>
            
            <div class="card">
                <h2>📊 Statistics</h2>
                <div class="stats">
                    <div class="stat"><div class="stat-value" id="totalAccounts">0</div><div class="stat-label">Total Accounts</div></div>
                    <div class="stat"><div class="stat-value" id="successCount">0</div><div class="stat-label">Successful</div></div>
                    <div class="stat"><div class="stat-value" id="failedCount">0</div><div class="stat-label">Failed</div></div>
                </div>
                <div class="progress">
                    <div>Progress</div>
                    <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width: 0%">0%</div></div>
                </div>
            </div>
        </div>
        
        <div class="card">
            <h2>📋 Results</h2>
            <div style="overflow-x: auto;">
                <table id="results">
                    <thead>
                        <tr><th>Username</th><th>Balance</th><th>Status</th><th>Error</th></tr>
                    </thead>
                    <tbody></tbody>
                </table>
            </div>
        </div>
    </div>
    
    <script>
        let ws = null;
        
        function connectWebSocket() {
            ws = new WebSocket(`ws://${window.location.host}/ws`);
            ws.onmessage = (event) => {
                const data = event.data;
                if (data.startsWith('RESULT:')) {
                    const result = JSON.parse(data.substring(7));
                    addResultToTable(result);
                    updateStats();
                } else if (data.startsWith('PROGRESS:')) {
                    const [current, total] = data.substring(9).split('/');
                    const percent = (current / total) * 100;
                    document.getElementById('progressFill').style.width = percent + '%';
                    document.getElementById('progressFill').textContent = Math.round(percent) + '%';
                } else if (data.startsWith('STATUS:')) {
                    const status = data.substring(7);
                    const statusDiv = document.getElementById('status');
                    statusDiv.textContent = status === 'started' ? '▶ Running' : status === 'completed' ? '✅ Completed' : '⛔ Stopped';
                    statusDiv.className = `status-badge status-${status}`;
                } else if (data.startsWith('SUMMARY:')) {
                    const [, success, total, rate] = data.split(':');
                    document.getElementById('successCount').textContent = success;
                    document.getElementById('failedCount').textContent = total - success;
                }
            };
        }
        
        function addResultToTable(result) {
            const tbody = document.querySelector('#results tbody');
            const row = tbody.insertRow(0);
            row.insertCell(0).textContent = result.username;
            row.insertCell(1).textContent = result.balance || '0';
            row.insertCell(2).textContent = result.status;
            row.insertCell(3).textContent = result.error || '-';
        }
        
        function updateStats() {
            fetch('/results').then(r => r.json()).then(results => {
                document.getElementById('totalAccounts').textContent = results.length;
                const success = results.filter(r => r.status === '✅').length;
                document.getElementById('successCount').textContent = success;
                document.getElementById('failedCount').textContent = results.length - success;
            });
        }
        
        async function startBot() {
            const accounts = document.getElementById('accounts').value;
            if (!accounts.trim()) {
                alert('Please enter accounts first!');
                return;
            }
            const response = await fetch('/start', { method: 'POST', body: accounts });
            const data = await response.json();
            alert(data.message);
            setTimeout(() => { location.reload(); }, 1000);
        }
        
        async function stopBot() {
            await fetch('/stop', { method: 'POST' });
            alert('Bot stopped');
            setTimeout(() => { location.reload(); }, 500);
        }
        
        async function clearAccounts() {
            await fetch('/accounts', { method: 'DELETE' });
            document.getElementById('accounts').value = '';
            alert('Accounts cleared');
        }
        
        async function loadResults() {
            const response = await fetch('/results');
            const results = await response.json();
            const tbody = document.querySelector('#results tbody');
            tbody.innerHTML = '';
            results.reverse().forEach(result => addResultToTable(result));
            updateStats();
        }
        
        connectWebSocket();
        loadResults();
        setInterval(loadResults, 3000);
    </script>
</body>
</html>'''
        html_file.write_text(html_content, encoding="utf-8")
    
    return FileResponse("index.html")

@app.post("/start")
async def start_bot(request: Request):
    global bot_instance, bot_task
    
    try:
        body = await request.body()
        accounts = body.decode("utf-8")
        
        # Save accounts
        async with aiofiles.open("accounts.txt", "w", encoding="utf-8") as f:
            await f.write(accounts)
        
        # Stop existing bot if running
        if bot_task and not bot_task.done():
            bot_task.cancel()
            try:
                await bot_task
            except:
                pass
        
        if bot_instance:
            await bot_instance.stop()
            await asyncio.sleep(1)
        
        # Create and start new bot
        cfg = Config(
            headless=PRODUCTION_CONFIG["headless"],
            max_retries=PRODUCTION_CONFIG["max_retries"],
            concurrency=PRODUCTION_CONFIG["max_concurrency"],
            timeout=PRODUCTION_CONFIG["timeout"],
            use_proxies=True,
            use_stealth=True,
            reuse_cookies=True
        )
        
        bot_instance = Bot(cfg)
        await bot_instance.start()
        
        async def run_bot():
            try:
                await manager.broadcast("STATUS:started")
                await manager.broadcast("INFO: 🚀 Bot started")
                await bot_instance.process_accounts()
            except asyncio.CancelledError:
                logger = ProductionLogger()
                logger.info("Bot task cancelled")
            except Exception as e:
                logger = ProductionLogger()
                logger.error(f"Error in bot: {e}")
                traceback.print_exc()
            finally:
                await manager.broadcast("STATUS:stopped")
        
        bot_task = asyncio.create_task(run_bot())
        
        return {"status": "started", "message": "Bot started successfully"}
    
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/stop")
async def stop_bot():
    global bot_instance, bot_task
    
    try:
        if bot_task and not bot_task.done():
            bot_task.cancel()
            try:
                await bot_task
            except:
                pass
            bot_task = None
        
        if bot_instance:
            await bot_instance.stop()
            bot_instance = None
        
        await manager.broadcast("STATUS:stopped")
        return {"status": "stopped"}
    
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/results")
async def get_results():
    try:
        results_file = Path(PRODUCTION_CONFIG["results_dir"]) / "results.json"
        if results_file.exists():
            async with aiofiles.open(results_file, "r", encoding="utf-8") as f:
                content = await f.read()
                return json.loads(content) if content else []
        return []
    except:
        return []

@app.get("/metrics")
async def get_metrics():
    if bot_instance:
        return await bot_instance.get_metrics()
    return {"error": "Bot not running"}

@app.get("/health")
async def health_check():
    if bot_instance:
        return await bot_instance.get_health()
    return {"status": "healthy", "bot_running": False}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(websocket)

if __name__ == "__main__":
    import uvicorn
    
    # Create necessary directories
    Path(PRODUCTION_CONFIG["results_dir"]).mkdir(exist_ok=True)
    Path(PRODUCTION_CONFIG["cookie_dir"]).mkdir(exist_ok=True)
    Path("logs").mkdir(exist_ok=True)
    
    # Check Playwright
    try:
        from playwright.async_api import async_playwright
        print("✅ Playwright is ready")
    except:
        print("❌ Please install: pip install playwright && playwright install chromium")
        sys.exit(1)
    
    # Create necessary files
    Path("accounts.txt").touch(exist_ok=True)
    Path("proxies.txt").touch(exist_ok=True)
    
    print("=" * 60)
    print("🎮 ADJARABET BOT PRO v10.0 - PRODUCTION MODE")
    print("=" * 60)
    print(f"📁 Results directory: {PRODUCTION_CONFIG['results_dir']}")
    print(f"🍪 Cookies directory: {PRODUCTION_CONFIG['cookie_dir']}")
    print(f"📝 Log file: {PRODUCTION_CONFIG['log_file']}")
    print(f"🚀 Starting server at http://{PRODUCTION_CONFIG['host']}:{PRODUCTION_CONFIG['port']}")
    print("=" * 60)
    
    uvicorn.run(
        app,
        host=PRODUCTION_CONFIG["host"],
        port=PRODUCTION_CONFIG["port"],
        log_level=PRODUCTION_CONFIG["log_level"].lower(),
        access_log=False
    )
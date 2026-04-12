import asyncio
import json
import logging
import os
import random
import re
import sys
from dataclasses import dataclass
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

# ================= PLAYWRIGHT FIX FOR RENDER =================
# Set Playwright browser cache to writable directory
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/render/.cache/ms-playwright"
os.makedirs("/opt/render/.cache/ms-playwright", exist_ok=True)

# Try to import playwright with error handling
try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout, BrowserContext
    PLAYWRIGHT_AVAILABLE = True
except ImportError as e:
    PLAYWRIGHT_AVAILABLE = False
    print(f"⚠️ Playwright not available: {e}")

# ================= PRODUCTION CONFIG =================
PRODUCTION_CONFIG = {
    "log_level": os.getenv("LOG_LEVEL", "INFO"),
    "port": int(os.getenv("PORT", 8000)),
    "host": os.getenv("HOST", "0.0.0.0"),
    "workers": int(os.getenv("WORKERS", 1)),
    "max_concurrency": int(os.getenv("MAX_CONCURRENCY", 2)),  # Reduced for Render
    "headless": os.getenv("HEADLESS", "true").lower() == "true",
    "timeout": int(os.getenv("TIMEOUT", 30000)),
    "max_retries": int(os.getenv("MAX_RETRIES", 2)),
    "cookie_dir": os.getenv("COOKIE_DIR", "cookies"),
    "results_dir": os.getenv("RESULTS_DIR", "results"),
    "log_file": os.getenv("LOG_FILE", "logs/bot.log"),
    "enable_monitoring": os.getenv("ENABLE_MONITORING", "true").lower() == "true",
    "auto_start_on_render": os.getenv("AUTO_START", "true").lower() == "true",
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

# ================= PROXY MANAGER =================
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

# ================= WEBSOCKET MANAGER =================
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

# ================= CORE BOT =================
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
        
        self.pw = None
        self.browser = None
        self._context_pool: List[Any] = []
        self._pool_lock = asyncio.Lock()
        self.metrics = MetricsCollector()
    
    async def start(self):
        if not PLAYWRIGHT_AVAILABLE:
            raise Exception("Playwright is not available. Please check installation.")
        
        self.log.info("Starting Playwright...")
        self.pw = await async_playwright().start()
        
        self.log.info(f"Launching browser (headless={self.cfg.headless})...")
        
        # Render-specific browser arguments
        browser_args = [
            '--disable-blink-features=AutomationControlled',
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-accelerated-2d-canvas',
            '--disable-gpu',
            '--disable-software-rasterizer',
            '--disable-extensions',
            '--disable-background-timer-throttling',
            '--disable-backgrounding-occluded-windows',
            '--disable-renderer-backgrounding',
        ]
        
        self.browser = await self.pw.chromium.launch(
            headless=self.cfg.headless,
            args=browser_args
        )
        
        await self._init_context_pool()
        self._running = True
        self.log.info("Bot started successfully")
    
    async def _init_context_pool(self):
        async with self._pool_lock:
            pool_size = min(self.cfg.concurrency, 2)  # Limit for Render
            for _ in range(pool_size):
                context = await self._create_context()
                self._context_pool.append(context)
    
    async def _get_context(self):
        async with self._pool_lock:
            if self._context_pool:
                return self._context_pool.pop()
        return await self._create_context()
    
    async def _return_context(self, context):
        try:
            await context.clear_cookies()
            async with self._pool_lock:
                if len(self._context_pool) < 3:
                    self._context_pool.append(context)
                else:
                    await context.close()
        except:
            try:
                await context.close()
            except:
                pass
    
    async def _create_context(self, proxy: Optional[str] = None):
        viewport = {'width': 1280, 'height': 720}  # Smaller for Render
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

# ================= FASTAPI APP =================
app = FastAPI(
    title="Adjarabet Bot Pro API",
    version="10.0",
    description="Production-ready account management system",
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
logger = ProductionLogger()

# ========== AUTO START BOT ON RENDER ==========
async def auto_start_bot_if_needed():
    """Automatically start bot on Render"""
    global bot_instance, bot_task
    
    if not PRODUCTION_CONFIG["auto_start_on_render"]:
        logger.info("Auto-start is disabled.")
        return
    
    if not PLAYWRIGHT_AVAILABLE:
        logger.error("Playwright is not available. Cannot auto-start bot.")
        return
    
    # Check if accounts exist
    accounts_file = Path("accounts.txt")
    if not accounts_file.exists() or accounts_file.stat().st_size == 0:
        logger.warning("No accounts found in accounts.txt. Bot will not auto-start.")
        logger.info("You can add accounts via UI or /start endpoint later.")
        return
    
    logger.info("=" * 60)
    logger.info("🔥 AUTO-STARTING BOT ON RENDER...")
    logger.info("=" * 60)
    
    try:
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
                logger.info("🚀 Bot task started successfully")
                await bot_instance.process_accounts()
            except asyncio.CancelledError:
                logger.info("Bot task cancelled")
            except Exception as e:
                logger.error(f"Error in bot task: {e}")
                traceback.print_exc()
            finally:
                await manager.broadcast("STATUS:stopped")
        
        bot_task = asyncio.create_task(run_bot())
        logger.info("✅ Bot auto-started successfully on Render!")
        
    except Exception as e:
        logger.error(f"Failed to auto-start bot: {e}")
        traceback.print_exc()

@app.on_event("startup")
async def startup_event():
    logger.info("=" * 60)
    logger.info("🎮 ADJARABET BOT PRO v10.0 - PRODUCTION MODE")
    logger.info("=" * 60)
    logger.info(f"📁 Working directory: {Path.cwd()}")
    logger.info(f"🌐 Web interface: http://{PRODUCTION_CONFIG['host']}:{PRODUCTION_CONFIG['port']}")
    logger.info(f"🤖 Playwright available: {PLAYWRIGHT_AVAILABLE}")
    logger.info(f"🤖 Auto-start on Render: {PRODUCTION_CONFIG['auto_start_on_render']}")
    logger.info("=" * 60)
    
    # Auto-start bot
    await auto_start_bot_if_needed()

@app.on_event("shutdown")
async def shutdown_event():
    global bot_instance, bot_task
    
    logger.info("Shutting down...")
    
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
        html_content = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Adjarabet Bot Pro - Production</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; }
        .header { text-align: center; color: white; margin-bottom: 30px; }
        .header h1 { font-size: 2em; margin-bottom: 10px; }
        .card { background: white; border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 10px 40px rgba(0,0,0,0.1); }
        .card h2 { margin-bottom: 15px; color: #333; border-left: 4px solid #667eea; padding-left: 12px; }
        .status-bar { background: white; border-radius: 12px; padding: 15px 20px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }
        .status-badge { padding: 8px 16px; border-radius: 20px; font-weight: 600; }
        .status-stopped { background: #f8d7da; color: #721c24; }
        .status-running { background: #d4edda; color: #155724; }
        textarea { width: 100%; min-height: 200px; padding: 12px; border: 2px solid #e0e0e0; border-radius: 8px; font-family: monospace; }
        button { padding: 10px 20px; margin: 5px; border: none; border-radius: 8px; cursor: pointer; font-weight: 600; }
        .btn-start { background: #28a745; color: white; }
        .btn-stop { background: #dc3545; color: white; }
        .progress-bar { background: #e0e0e0; border-radius: 10px; overflow: hidden; height: 30px; margin-top: 10px; }
        .progress-fill { background: linear-gradient(90deg, #667eea, #764ba2); height: 100%; display: flex; align-items: center; justify-content: center; color: white; }
        table { width: 100%; border-collapse: collapse; margin-top: 15px; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid #e0e0e0; }
        th { background: #f8f9fa; }
        .stats { display: flex; gap: 15px; margin-top: 15px; }
        .stat { flex: 1; background: #f8f9fa; padding: 15px; border-radius: 8px; text-align: center; }
        .stat-value { font-size: 2em; font-weight: bold; color: #667eea; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎮 Adjarabet Bot Pro</h1>
            <p>Professional Account Management System</p>
        </div>
        
        <div class="status-bar">
            <div>🤖 Bot Status</div>
            <div id="status" class="status-badge status-stopped">⛔ Stopped</div>
        </div>
        
        <div class="card">
            <h2>📝 Accounts</h2>
            <textarea id="accounts" placeholder="username:password or username:password:proxy"></textarea>
            <div>
                <button class="btn-start" onclick="startBot()">▶ Start Bot</button>
                <button class="btn-stop" onclick="stopBot()">⏹ Stop Bot</button>
            </div>
        </div>
        
        <div class="card">
            <h2>📊 Statistics</h2>
            <div class="stats">
                <div class="stat"><div class="stat-value" id="totalAccounts">0</div><div>Total</div></div>
                <div class="stat"><div class="stat-value" id="successCount">0</div><div>Success</div></div>
                <div class="stat"><div class="stat-value" id="failedCount">0</div><div>Failed</div></div>
            </div>
            <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width: 0%">0%</div></div>
        </div>
        
        <div class="card">
            <h2>📋 Results</h2>
            <table id="results">
                <thead><tr><th>Username</th><th>Balance</th><th>Status</th><th>Error</th></tr></thead>
                <tbody></tbody>
            </table>
        </div>
    </div>
    
    <script>
        let ws = new WebSocket(`ws://${window.location.host}/ws`);
        ws.onmessage = (event) => {
            const data = event.data;
            if (data.startsWith('RESULT:')) {
                const result = JSON.parse(data.substring(7));
                addResult(result);
            } else if (data.startsWith('PROGRESS:')) {
                const [current, total] = data.substring(9).split('/');
                const percent = (current/total)*100;
                document.getElementById('progressFill').style.width = percent+'%';
                document.getElementById('progressFill').textContent = Math.round(percent)+'%';
            } else if (data.startsWith('STATUS:')) {
                const status = data.substring(7);
                const statusDiv = document.getElementById('status');
                statusDiv.textContent = status === 'started' ? '▶ Running' : '⛔ Stopped';
                statusDiv.className = `status-badge status-${status}`;
            } else if (data.startsWith('SUMMARY:')) {
                const [,success,total] = data.split(':');
                document.getElementById('successCount').textContent = success;
                document.getElementById('failedCount').textContent = total - success;
            }
        };
        
        function addResult(result) {
            const tbody = document.querySelector('#results tbody');
            const row = tbody.insertRow(0);
            row.insertCell(0).textContent = result.username;
            row.insertCell(1).textContent = result.balance || '0';
            row.insertCell(2).textContent = result.status;
            row.insertCell(3).textContent = result.error || '-';
            updateStats();
        }
        
        function updateStats() {
            fetch('/results').then(r=>r.json()).then(results=>{
                document.getElementById('totalAccounts').textContent = results.length;
                document.getElementById('successCount').textContent = results.filter(r=>r.status==='✅').length;
                document.getElementById('failedCount').textContent = results.filter(r=>r.status!=='✅').length;
            });
        }
        
        async function startBot() {
            const accounts = document.getElementById('accounts').value;
            if(!accounts.trim()) return alert('Enter accounts first!');
            await fetch('/start', {method:'POST', body:accounts});
            alert('Bot started!');
            setTimeout(()=>location.reload(),1000);
        }
        
        async function stopBot() {
            await fetch('/stop', {method:'POST'});
            alert('Bot stopped');
            location.reload();
        }
        
        updateStats();
        setInterval(updateStats,3000);
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
        
        async with aiofiles.open("accounts.txt", "w", encoding="utf-8") as f:
            await f.write(accounts)
        
        if bot_task and not bot_task.done():
            bot_task.cancel()
            try:
                await bot_task
            except:
                pass
        
        if bot_instance:
            await bot_instance.stop()
            await asyncio.sleep(1)
        
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
                await bot_instance.process_accounts()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error: {e}")
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

@app.get("/health")
async def health_check():
    return {"status": "healthy", "playwright": PLAYWRIGHT_AVAILABLE}

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
    
    # Create directories
    Path(PRODUCTION_CONFIG["results_dir"]).mkdir(exist_ok=True)
    Path(PRODUCTION_CONFIG["cookie_dir"]).mkdir(exist_ok=True)
    Path("logs").mkdir(exist_ok=True)
    
    # Create empty files
    Path("accounts.txt").touch(exist_ok=True)
    Path("proxies.txt").touch(exist_ok=True)
    
    print("=" * 60)
    print("🎮 ADJARABET BOT PRO v10.0")
    print("=" * 60)
    print(f"🚀 Starting server at http://{PRODUCTION_CONFIG['host']}:{PRODUCTION_CONFIG['port']}")
    print("=" * 60)
    
    uvicorn.run(
        app,
        host=PRODUCTION_CONFIG["host"],
        port=PRODUCTION_CONFIG["port"],
        log_level=PRODUCTION_CONFIG["log_level"].lower(),
        access_log=False
    )
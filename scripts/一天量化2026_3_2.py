
import os
import json
import asyncio
import time
import hmac
import hashlib
import aiohttp
from decimal import Decimal
from typing import Any, Dict, List, Optional
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.core.data_type.common import PriceType

# ==========================================
# === 🛡️ 自研 Bitfinex 原生 API 客户端 ===
# ==========================================
class BitfinexAPI:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://api.bitfinex.com/v2"

    async def request(self, endpoint: str, payload_dict: dict = None):
        url = self.base_url + endpoint
        nonce = str(int(time.time() * 1000000))
        body = json.dumps(payload_dict) if payload_dict else "{}"
        signature_payload = f"/api/v2{endpoint}{nonce}{body}"
        sig = hmac.new(self.api_secret.encode('utf8'), signature_payload.encode('utf8'), hashlib.sha384).hexdigest()
        headers = {
            "bfx-nonce": nonce,
            "bfx-apikey": self.api_key,
            "bfx-signature": sig,
            "content-type": "application/json"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=body) as resp:
                return await resp.json()

    async def get_balance(self) -> float:
        resp = await self.request("/auth/r/wallets")
        if isinstance(resp, list):
            for w in resp:
                if len(w) >= 5 and w[0] == "margin" and w[1] in ["USTF0", "UST"]:
                    return float(w[4]) if w[4] is not None else 0.0
        return 0.0

    async def get_ticker(self, symbol: str) -> float:
        url = f"{self.base_url}/ticker/{symbol}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                return float(data[6]) if isinstance(data, list) and len(data) >= 7 else 0.0

    async def create_order(self, symbol, amount, price, lev=30):
        req = {
            "type": "LIMIT",
            "symbol": symbol,
            "amount": "{:.6f}".format(float(amount)),
            "price": str(price),
            "lev": int(lev)
        }
        return await self.request("/auth/w/order/submit", req)

    async def fetch_open_orders(self, symbol):
        resp = await self.request("/auth/r/orders")
        return [o for o in resp if isinstance(o, list) and len(o) > 3 and o[3] == symbol] if isinstance(resp, list) else []

    async def cancel_all(self, symbol):
        return await self.request("/auth/w/order/cancel/multi", {"all": 1})

# ==========================================
# === 🚀 动态加固网格策略 (原生直连版) ===
# ==========================================
class 一天量化2026_3_2(ScriptStrategyBase):
    # 使用服务器上已有的 OKX 作为驱动（仅用于启动脚本时钟）
    driver_exchange = "okx"
    driver_pair = "ETH-USDT"
    markets = {driver_exchange: {driver_pair}}
    
    # Bitfinex 真实交易目标
    bfx_symbol = "tETHF0:USTF0"
    
    # --- 核心参数 ---
    leverage_long = 30
    leverage_short = 15
    grid_levels = 4
    grid_spacing = 0.001 
    tp_pct = 0.002       
    
    initialized = False
    active_grid_orders = [] # 记录已挂单的 ID 或价格层级

    def __init__(self, connectors: Dict[str, Any]):
        super().__init__(connectors)
        # 从本地配置读取 Key
        api_key = "94a54e57696198788682c7e8c4b0d5adab9b69c70fa"
        api_secret = "2c15f19dd463e312397d557d54531f63e5961a12da7"
        self.bfx = BitfinexAPI(api_key, api_secret)

    def on_tick(self):
        if not self.initialized:
            asyncio.ensure_future(self.setup_bfx())
            self.initialized = True
            return

    async def setup_bfx(self):
        self.logger().info("🚀 正在通过自研 API 直连接口启动 Bitfinex 动态网格...")
        # 1. 清理旧单
        await self.bfx.cancel_all(self.bfx_symbol)
        # 2. 部署网格
        await self.deploy_grid()

    async def deploy_grid(self):
        mid_price = await self.bfx.get_ticker(self.bfx_symbol)
        if mid_price <= 0:
            self.logger().error("❌ 无法获取 Bitfinex 行情，请检查 API 或网络")
            return
            
        balance = await self.bfx.get_balance()
        if balance < 1.0: balance = 10.0 # 模拟兜底
        
        self.logger().info(f"📊 账户余额: {balance:.2f}u | 正在按[多30x/空15x]撒网...")

        for i in range(1, self.grid_levels + 1):
            # --- 多头买单 (30x 规模) ---
            b_price = round(mid_price * (1 - i * self.grid_spacing), 2)
            # 货值 = (余额/2) * 30倍 / 层数
            b_val = (balance * 0.5 * self.leverage_long * 0.95) / self.grid_levels
            b_amount = round(b_val / b_price, 4)
            await self.bfx.create_order(self.bfx_symbol, b_amount, b_price, lev=self.leverage_long)

            # --- 空头卖单 (15x 规模) ---
            s_price = round(mid_price * (1 + i * self.grid_spacing), 2)
            # 货值 = (余额/2) * 15倍 / 层数
            s_val = (balance * 0.5 * self.leverage_short * 0.95) / self.grid_levels
            s_amount = round(-s_val / s_price, 4) # 卖出为负
            await self.bfx.create_order(self.bfx_symbol, s_amount, s_price, lev=self.leverage_short)

        self.logger().info("✅ 所有网格单已挂出！")

    def format_status(self) -> str:
        return f"原生直连模式 | Bitfinex ETH | 多:30x / 空:15x | 层数:4"

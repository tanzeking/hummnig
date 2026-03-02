
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

# ==========================================
# === 🛡️ Bitfinex 增强版 API (极速响应版) ===
# ==========================================
class BitfinexAPI:
    def __init__(self, api_key: str, api_secret: str, logger):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://api.bitfinex.com/v2"
        self.logger = logger

    async def request(self, endpoint: str, payload_dict: dict = None):
        url = self.base_url + endpoint
        nonce = str(int(time.time() * 1000000))
        body = json.dumps(payload_dict) if payload_dict else "{}"
        signature_payload = f"/api/v2{endpoint}{nonce}{body}"
        sig = hmac.new(self.api_secret.encode('utf8'), signature_payload.encode('utf8'), hashlib.sha384).hexdigest()
        headers = {"bfx-nonce": nonce, "bfx-apikey": self.api_key, "bfx-signature": sig, "content-type": "application/json"}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=body) as resp:
                return await resp.json()

    async def get_balance(self) -> float:
        resp = await self.request("/auth/r/wallets")
        if isinstance(resp, list):
            for w in resp:
                if len(w) >= 5 and w[0] == "margin" and w[1].upper() in ["USTF0", "UST"]:
                    return float(w[4]) if w[4] is not None else 0.0
        return 0.0

    async def get_positions(self):
        return await self.request("/auth/r/positions")

    async def get_open_orders(self, symbol):
        resp = await self.request("/auth/r/orders")
        return [o for o in resp if isinstance(o, list) and len(o) > 3 and o[3] == symbol] if isinstance(resp, list) else []

    async def create_order(self, symbol, amount, price, lev=30, type="LIMIT"):
        req = {
            "type": type,
            "symbol": symbol,
            "amount": "{:.5f}".format(float(amount)),
            "price": "{:.2f}".format(float(price)),
            "lev": int(lev)
        }
        return await self.request("/auth/w/order/submit", req)

# ==========================================
# === 🚀 超高密度密集交易策略 (0.05% / 0.1%) ===
# ==========================================
class 一天量化2026_3_2(ScriptStrategyBase):
    markets = {"okx": {"ETH-USDT"}} # 仅做心跳
    bfx_symbol = "tETHF0:USTF0"
    
    # --- 💥 超高密度参数 ---
    leverage_long = 30
    leverage_short = 15
    grid_levels = 10        # 增加到 10 层网格
    grid_spacing = 0.0005   # 0.05% 极其密集
    tp_pct = 0.001          # 0.1% 极速收割
    sl_pct = 0.20           # 20% 硬止损
    
    last_check_time = 0
    check_interval = 2      # 💥 扫描频率提高到 2 秒一次

    def __init__(self, connectors: Dict[str, Any]):
        super().__init__(connectors)
        api_key = "94a54e57696198788682c7e8c4b0d5adab9b69c70fa"
        api_secret = "2c15f19dd463e312397d557d54531f63e5961a12da7"
        self.bfx = BitfinexAPI(api_key, api_secret, self.logger())

    def on_tick(self):
        if self.current_timestamp - self.last_check_time >= self.check_interval:
            asyncio.ensure_future(self.maintain_strategy())
            self.last_check_time = self.current_timestamp

    async def maintain_strategy(self):
        ticker_url = f"https://api.bitfinex.com/v2/ticker/{self.bfx_symbol}"
        async with aiohttp.ClientSession() as session:
            async with session.get(ticker_url) as resp:
                data = await resp.json()
                mid_price = float(data[6]) if isinstance(data, list) and len(data) >= 7 else 0.0

        if mid_price <= 0: return

        balance = await self.bfx.get_balance()
        positions = await self.bfx.get_positions()
        open_orders = await self.bfx.get_open_orders(self.bfx_symbol)

        # 1. 自动止盈检查
        has_long = False
        has_short = False
        for pos in positions:
            if pos[0] == self.bfx_symbol:
                amount = float(pos[2])
                entry_price = float(pos[3])
                if amount > 0:
                    has_long = True
                    if not any(float(o[6]) < 0 for o in open_orders):
                        tp_p = round(entry_price * (1 + self.tp_pct), 2)
                        await self.bfx.create_order(self.bfx_symbol, -amount, tp_p, lev=self.leverage_long)
                elif amount < 0:
                    has_short = True
                    if not any(float(o[6]) > 0 for o in open_orders):
                        tp_p = round(entry_price * (1 - self.tp_pct), 2)
                        await self.bfx.create_order(self.bfx_symbol, -amount, tp_p, lev=self.leverage_short)

        # 2. 高密度补网格
        l_orders = [o for o in open_orders if float(o[6]) > 0]
        s_orders = [o for o in open_orders if float(o[6]) < 0]

        if not has_long and len(l_orders) < (self.grid_levels / 2):
            await self.deploy_side("long", mid_price, balance)

        if not has_short and len(s_orders) < (self.grid_levels / 2):
            await self.deploy_side("short", mid_price, balance)

    async def deploy_side(self, side, mid_price, balance):
        use_bal = balance if balance > 2.0 else 10.0
        for i in range(1, 4): # 多层快速撒网
            if side == "long":
                p = round(mid_price * (1 - i * self.grid_spacing), 2)
                a = max(0.005, round((use_bal * 0.4 * self.leverage_long) / (self.grid_levels * p), 4))
                await self.bfx.create_order(self.bfx_symbol, a, p, lev=self.leverage_long)
            else:
                p = round(mid_price * (1 + i * self.grid_spacing), 2)
                a = -max(0.005, round((use_bal * 0.4 * self.leverage_short) / (self.grid_levels * p), 4))
                await self.bfx.create_order(self.bfx_symbol, a, p, lev=self.leverage_short)

    def format_status(self) -> str:
        return f"高密度模式 | 间距:0.05% | 止盈:0.1% | 杠杆:30/15"

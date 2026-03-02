
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
# === 🛡️ Bitfinex 超级监控 API ===
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

    async def create_order(self, symbol, amount, price=None, lev=30, type="LIMIT", flags=0):
        req = {"type": type.upper(), "symbol": symbol, "amount": "{:.5f}".format(float(amount)), "lev": int(lev)}
        if price: req["price"] = "{:.2f}".format(float(price))
        if flags: req["flags"] = flags
        return await self.request("/auth/w/order/submit", req)

    async def cancel_all(self):
        return await self.request("/auth/w/order/cancel/multi", {"all": 1})

# ==========================================
# === 🚀 动态加固网格 (0.1% 间距 / 0.2% 止盈) ===
# ==========================================
class 一天量化2026_3_2(ScriptStrategyBase):
    markets = {"okx": {"ETH-USDT"}} 
    bfx_symbol = "tETHF0:USTF0"
    
    leverage_long = 30
    leverage_short = 15
    grid_levels = 4         
    grid_spacing = 0.001    # 0.1% 间距
    tp_pct = 0.002          # 0.2% 止盈
    
    # 💥 修改上限为 0.16 (约 310U 市值)，确保低于 300U 时会一直下单
    max_pos_amount = 0.16   
    
    last_check_time = 0
    check_interval = 5      
    is_executing = False

    def __init__(self, connectors: Dict[str, Any]):
        super().__init__(connectors)
        api_key = "94a54e57696198788682c7e8c4b0d5adab9b69c70fa"
        api_secret = "2c15f19dd463e312397d557d54531f63e5961a12da7"
        self.bfx = BitfinexAPI(api_key, api_secret, self.logger())

    async def on_stop(self):
        self.logger().info("🛑 停止清理...")
        await self.bfx.cancel_all()

    def on_tick(self):
        if self.current_timestamp - self.last_check_time >= self.check_interval:
            if not self.is_executing:
                asyncio.ensure_future(self.maintain_strategy())
                self.last_check_time = self.current_timestamp

    async def maintain_strategy(self):
        self.is_executing = True
        try:
            ticker_url = f"https://api.bitfinex.com/v2/ticker/{self.bfx_symbol}"
            async with aiohttp.ClientSession() as session:
                async with session.get(ticker_url) as resp:
                    data = await resp.json()
                    mid_price = float(data[6]) if isinstance(data, list) and len(data) >= 7 else 0.0
            if mid_price <= 0: return

            balance = await self.bfx.get_balance()
            positions = await self.bfx.get_positions()
            open_orders = await self.bfx.get_open_orders(self.bfx_symbol)

            current_pos = 0.0
            entry_price = 0.0
            for pos in positions:
                if pos[0] == self.bfx_symbol:
                    current_pos = float(pos[2])
                    entry_price = float(pos[3])

            # 1. 自动止盈
            if abs(current_pos) > 0.0001:
                side = "long" if current_pos > 0 else "short"
                tp_target = round(entry_price * (1 + self.tp_pct if side == "long" else 1 - self.tp_pct), 2)
                has_tp = any(abs(float(o[16]) - tp_target) < 0.15 for o in open_orders)
                if not has_tp:
                    self.logger().info(f"🎯 补挂止盈单: {tp_target}")
                    await self.bfx.create_order(self.bfx_symbol, -current_pos, tp_target, lev=30)

            # 2. 补网格 (只要持仓 < 0.16 且 挂单不足时就下单)
            grid_orders = [o for o in open_orders if abs(float(o[16]) - mid_price) < (mid_price * 0.02)]
            l_grids = [o for o in grid_orders if float(o[6]) > 0]
            s_grids = [o for o in grid_orders if float(o[6]) < 0]

            if current_pos < self.max_pos_amount and len(l_grids) < self.grid_levels:
                self.logger().info(f"♻️ 持仓 {current_pos:.4f} < {self.max_pos_amount}, 正在补买单...")
                await self.deploy_layer("long", mid_price, balance, len(l_grids))
            
            if current_pos > -self.max_pos_amount and len(s_grids) < self.grid_levels:
                self.logger().info(f"♻️ 持仓 {current_pos:.4f} > -{self.max_pos_amount}, 正在补卖单...")
                await self.deploy_layer("short", mid_price, balance, len(s_grids))

        finally:
            self.is_executing = False

    async def deploy_layer(self, side, mid_price, balance, current_count):
        idx = current_count + 1
        use_bal = balance if balance > 2.0 else 10.0
        if side == "long":
            p = round(mid_price * (1 - idx * self.grid_spacing), 2)
            a = max(0.005, round((use_bal * 0.4 * self.leverage_long) / (self.grid_levels * p), 4))
            await self.bfx.create_order(self.bfx_symbol, a, p, lev=self.leverage_long, flags=4096)
        else:
            p = round(mid_price * (1 + idx * self.grid_spacing), 2)
            a = -max(0.005, round((use_bal * 0.4 * self.leverage_short) / (self.grid_levels * p), 4))
            await self.bfx.create_order(self.bfx_symbol, a, p, lev=self.leverage_short, flags=4096)

    def format_status(self) -> str:
        return f"动态补货网格 | 持仓上限:0.16 ETH (~310U)"

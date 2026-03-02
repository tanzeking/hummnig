
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
# === 🛡️ Bitfinex 增强版 API (全能版) ===
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
        req = {
            "type": type.upper(),
            "symbol": symbol,
            "amount": "{:.5f}".format(float(amount)),
            "lev": int(lev)
        }
        if price: req["price"] = "{:.2f}".format(float(price))
        if flags: req["flags"] = flags
        return await self.request("/auth/w/order/submit", req)

    async def cancel_all(self):
        # 撤销所有活动的订单
        return await self.request("/auth/w/order/cancel/multi", {"all": 1})

# ==========================================
# === 🚀 全自动动态网格 (带停机自动清理) ===
# ==========================================
class 一天量化2026_3_2(ScriptStrategyBase):
    markets = {"okx": {"ETH-USDT"}} 
    bfx_symbol = "tETHF0:USTF0"
    
    leverage_long = 30
    leverage_short = 15
    grid_levels = 8         
    grid_spacing = 0.0005   
    tp_pct = 0.001          
    max_pos_amount = 0.15   
    
    last_check_time = 0
    check_interval = 2      

    def __init__(self, connectors: Dict[str, Any]):
        super().__init__(connectors)
        api_key = "94a54e57696198788682c7e8c4b0d5adab9b69c70fa"
        api_secret = "2c15f19dd463e312397d557d54531f63e5961a12da7"
        self.bfx = BitfinexAPI(api_key, api_secret, self.logger())

    async def on_stop(self):
        """
        🛡️ 核心停机逻辑：脚本停止时执行清理
        """
        self.logger().info("🛑 收到停止指令，正在执行关机清理...")
        
        # 1. 撤销所有挂单
        res_cancel = await self.bfx.cancel_all()
        self.logger().info(f"✨ 已请求撤销所有挂单")
        
        # 2. 检查并市价清仓
        positions = await self.bfx.get_positions()
        for pos in positions:
            if pos[0] == self.bfx_symbol:
                amount = float(pos[2])
                if abs(amount) > 0.0001:
                    self.logger().info(f"🔪 发现残留持仓 {amount}，正在市价强平入仓...")
                    await self.bfx.create_order(self.bfx_symbol, -amount, type="MARKET")
        
        self.logger().info("✅ 关机清理完毕。")

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

        current_amount = 0.0
        for pos in positions:
            if pos[0] == self.bfx_symbol:
                current_amount = float(pos[2])

        # 3. 自动止盈逻辑
        if abs(current_amount) > 0.0001:
            side = "long" if current_amount > 0 else "short"
            has_tp = any((current_amount > 0 and float(o[6]) < 0) or (current_amount < 0 and float(o[6]) > 0) for o in open_orders)
            if not has_tp:
                tp_price = round(mid_price * (1 + self.tp_pct if side == "long" else 1 - self.tp_pct), 2)
                await self.bfx.create_order(self.bfx_symbol, -current_amount, tp_price, lev=(self.leverage_long if side=="long" else self.leverage_short))

        # 4. 动态补网格
        l_orders = [o for o in open_orders if float(o[6]) > 0]
        s_orders = [o for o in open_orders if float(o[6]) < 0]

        if current_amount < self.max_pos_amount and len(l_orders) < self.grid_levels:
            await self.deploy_side("long", mid_price, balance)

        if current_amount > -self.max_pos_amount and len(s_orders) < self.grid_levels:
            await self.deploy_side("short", mid_price, balance)

    async def deploy_side(self, side, mid_price, balance):
        use_bal = balance if balance > 2.0 else 10.0
        for i in range(1, 3):
            if side == "long":
                p = round(mid_price * (1 - i * self.grid_spacing), 2)
                a = max(0.005, round((use_bal * 0.4 * self.leverage_long) / (10 * p), 4))
                await self.bfx.create_order(self.bfx_symbol, a, p, lev=self.leverage_long, flags=4096)
            else:
                p = round(mid_price * (1 + i * self.grid_spacing), 2)
                a = -max(0.005, round((use_bal * 0.4 * self.leverage_short) / (10 * p), 4))
                await self.bfx.create_order(self.bfx_symbol, a, p, lev=self.leverage_short, flags=4096)

    def format_status(self) -> str:
        return f"自动清理网格版 | 间距:0.05% | 下单上限:0.15 | 停止脚本即清仓"

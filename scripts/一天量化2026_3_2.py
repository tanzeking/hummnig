
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
# === 🛡️ Bitfinex 净头寸管理 API ===
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
        cid = int(float(price) * 100) if price else int(time.time() % 1000000)
        req = {"type": type.upper(), "symbol": symbol, "amount": "{:.5f}".format(float(amount)), "lev": int(lev), "cid": cid, "flags": flags}
        if price: req["price"] = "{:.2f}".format(float(price))
        return await self.request("/auth/w/order/submit", req)

    async def cancel_all(self):
        return await self.request("/auth/w/order/cancel/multi", {"all": 1})

# ==========================================
# === 🚀 净仓位止盈止损网格策略 ===
# ==========================================
class 一天量化2026_3_2(ScriptStrategyBase):
    markets = {"okx": {"ETH-USDT"}} 
    bfx_symbol = "tETHF0:USTF0"
    
    leverage_long = 30
    leverage_short = 15
    grid_levels = 4         
    grid_spacing = 0.001    # 0.1% 间距
    tp_pct = 0.002          # 0.2% 止盈
    
    # 💥 止损设置 (单仓 20% ROI 止损)
    # 20% ROI / 30倍杠杆 ≈ 0.67% 的价格变动
    sl_roi_limit = 0.20     
    
    max_pos_amount = 0.16   
    last_check_time = 0
    check_interval = 10     
    is_executing = False

    def __init__(self, connectors: Dict[str, Any]):
        super().__init__(connectors)
        api_key = "94a54e57696198788682c7e8c4b0d5adab9b69c70fa"
        api_secret = "2c15f19dd463e312397d557d54531f63e5961a12da7"
        self.bfx = BitfinexAPI(api_key, api_secret, self.logger())

    async def on_stop(self):
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
                    mid_p = float(data[6]) if isinstance(data, list) and len(data) >= 7 else 0.0
            if mid_p <= 0: return

            positions = await self.bfx.get_positions()
            open_orders = await self.bfx.get_open_orders(self.bfx_symbol)

            current_pos = 0.0
            entry_price = 0.0
            for pos in positions:
                if pos[0] == self.bfx_symbol:
                    current_pos = float(pos[2])
                    entry_price = float(pos[3])

            # 🛑 核心：单仓止损检查 (ROI 模式)
            if abs(current_pos) > 0.0001:
                lev = self.leverage_long if current_pos > 0 else self.leverage_short
                # 计算盈亏比例 (相对于开仓价)
                pnl_pct = (mid_p - entry_price) / entry_price if current_pos > 0 else (entry_price - mid_p) / entry_price
                roi = pnl_pct * lev
                
                if roi < -self.sl_roi_limit:
                    self.logger().info(f"💣 触发硬止损! 当前ROI: {roi*100:.2f}%，正在强制清仓并停止脚本...")
                    await self.bfx.cancel_all()
                    await self.bfx.create_order(self.bfx_symbol, -current_pos, type="MARKET")
                    return # 强平后停止此次所有操作

                # 🎯 自动止盈
                side = "long" if current_pos > 0 else "short"
                tp_target = round(entry_price * (1 + self.tp_pct if side == "long" else 1 - self.tp_pct), 2)
                has_tp = any(abs(float(o[16]) - tp_target) < 0.2 for o in open_orders)
                if not has_tp:
                    await self.bfx.create_order(self.bfx_symbol, -current_pos, tp_target, lev=30)

            # 补网格逻辑
            grid_orders = [o for o in open_orders if abs(float(o[16]) - mid_p) < (mid_p * 0.02)]
            l_grids = [o for o in grid_orders if float(o[6]) > 0]
            if current_pos < self.max_pos_amount and len(l_grids) < self.grid_levels:
                await self.deploy_one("long", mid_p, 10.0, len(l_grids))
            
        finally:
            self.is_executing = False

    async def deploy_one(self, side, mid_p, balance, count):
        idx = count + 1
        p = round(mid_p * (1 - idx * self.grid_spacing if side=="long" else 1 + idx * self.grid_spacing), 2)
        a = max(0.005, round((balance * 12)/p, 4)) if side=="long" else -max(0.005, round((balance * 6)/p, 4))
        await self.bfx.create_order(self.bfx_symbol, a, p, lev=(self.leverage_long if side=="long" else self.leverage_short), flags=4096)

    def format_status(self) -> str:
        return f"净仓位管理版 [止盈:0.2% | 止损:20% ROI]"


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
# === 🛡️ Bitfinex CID 身份证 API ===
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
                r = await resp.json()
                # 如果是重复 CID 报错，静默处理（防止日志刷屏）
                if isinstance(r, list) and "ERROR" in r and "duplicate" in str(r).lower():
                    return None
                return r

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
        # 💡 核心：为每个价格和方向生成唯一的 CID (例如: BUY_1950.00 -> 195000)
        # 这样相同的价格重复下单，交易所会直接阻断
        cid = int(float(price) * 100) if price else int(time.time() % 1000000)
        
        req = {
            "type": type.upper(),
            "symbol": symbol,
            "amount": "{:.5f}".format(float(amount)),
            "price": "{:.2f}".format(float(price)) if price else None,
            "lev": int(lev),
            "cid": cid,
            "flags": flags
        }
        return await self.request("/auth/w/order/submit", req)

    async def cancel_all(self):
        return await self.request("/auth/w/order/cancel/multi", {"all": 1})

# ==========================================
# === 🚀 稳定动态网格 (CID 唯一身份版) ===
# ==========================================
class 一天量化2026_3_2(ScriptStrategyBase):
    markets = {"okx": {"ETH-USDT"}} 
    bfx_symbol = "tETHF0:USTF0"
    
    leverage_long = 30
    leverage_short = 15
    grid_levels = 4         
    grid_spacing = 0.001    
    tp_pct = 0.002          
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
        self.logger().info("🛑 关机自动清理...")
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

            balance = await self.bfx.get_balance()
            positions = await self.bfx.get_positions()
            open_orders = await self.bfx.get_open_orders(self.bfx_symbol)

            current_pos = 0.0
            entry_p = 0.0
            for pos in positions:
                if pos[0] == self.bfx_symbol:
                    current_pos = float(pos[2])
                    entry_p = float(pos[3])

            # 1. 止盈逻辑
            if abs(current_pos) > 0.0001:
                side = "long" if current_pos > 0 else "short"
                tp_p = round(entry_p * (1 + self.tp_pct if side == "long" else 1 - self.tp_pct), 2)
                has_tp = any(abs(float(o[16]) - tp_p) < 0.2 for o in open_orders)
                if not has_tp:
                    self.logger().info(f"🚨 挂出止盈单 CID监控中 @ {tp_p}")
                    await self.bfx.create_order(self.bfx_symbol, -current_pos, tp_p, lev=30)

            # 2. 补网格逻辑
            l_grids = [o for o in open_orders if float(o[6]) > 0 and abs(float(o[16]) - mid_p) < (mid_p * 0.02)]
            s_grids = [o for o in open_orders if float(o[6]) < 0 and abs(float(o[16]) - mid_p) < (mid_p * 0.02)]

            if current_pos < self.max_pos_amount and len(l_grids) < self.grid_levels:
                await self.deploy_one("long", mid_p, balance, len(l_grids))
            
            if current_pos > -self.max_pos_amount and len(s_grids) < self.grid_levels:
                await self.deploy_one("short", mid_p, balance, len(s_grids))

            self.logger().info(f"✅ 执行扫描完毕 | 持仓: {current_pos:.4f} | 买/卖单: {len(l_grids)}/{len(s_grids)}")

        finally:
            self.is_executing = False

    async def deploy_one(self, side, mid_p, balance, count):
        idx = count + 1
        use_bal = balance if balance > 2.0 else 10.0
        if side == "long":
            p = round(mid_p * (1 - idx * self.grid_spacing), 2)
            a = max(0.005, round((use_bal * 0.4 * self.leverage_long) / (self.grid_levels * p), 4))
            await self.bfx.create_order(self.bfx_symbol, a, p, lev=self.leverage_long, flags=4096)
        else:
            p = round(mid_p * (1 + idx * self.grid_spacing), 2)
            a = -max(0.005, round((use_bal * 0.4 * self.leverage_short) / (self.grid_levels * p), 4))
            await self.bfx.create_order(self.bfx_symbol, a, p, lev=self.leverage_short, flags=4096)

    def format_status(self) -> str:
        return f"CID硬核去重版 | 间距:0.1% | 止盈:0.2% | 持仓上限:0.16"

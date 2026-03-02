
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
# === 🛡️ Bitfinex 精准占位 API ===
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
            async with session.post(url, headers=headers, data=body, timeout=5) as resp:
                return await resp.json()

    async def get_margin_info(self) -> float:
        resp = await self.request("/auth/r/info/margin/base")
        if isinstance(resp, list) and len(resp) >= 4:
            return float(resp[3][2]) if isinstance(resp[3], list) and len(resp[3]) > 2 else 10.0
        return 10.0

    async def get_positions(self):
        return await self.request("/auth/r/positions")

    async def get_open_orders(self, symbol):
        resp = await self.request("/auth/r/orders")
        return [o for o in resp if isinstance(o, list) and len(o) > 3 and o[3] == symbol] if isinstance(resp, list) else []

    async def create_order(self, symbol, amount, price=None, lev=30, type="LIMIT", flags=0):
        # 💡 CID 唯一化：按价格和方向锁定，同一位置绝对补不出第二张单
        side_prefix = 1 if float(amount) > 0 else 2
        cid = int(float(price) * 100) + (side_prefix * 1000000)
        req = {"type": type.upper(), "symbol": symbol, "amount": "{:.5f}".format(float(amount)), "lev": int(lev), "cid": cid, "flags": flags}
        if price: req["price"] = "{:.2f}".format(float(price))
        return await self.request("/auth/w/order/submit", req)

    async def cancel_all(self):
        return await self.request("/auth/w/order/cancel/multi", {"all": 1})

# ==========================================
# === 🚀 阵地防卫版 (稳定网格/拒绝扎堆) ===
# ==========================================
class 一天量化2026_3_2(ScriptStrategyBase):
    markets = {"okx": {"ETH-USDT"}} 
    bfx_symbol = "tETHF0:USTF0"
    
    leverage_long = 30
    leverage_short = 15
    grid_levels = 4         
    grid_spacing = 0.001    # 0.1% = 2刀
    tp_pct = 0.002          # 0.2% 止盈
    sl_roi = 0.20           
    
    # 💥 稳定重心逻辑
    anchor_price = 0.0      # 阵地重心价格
    max_pos_amount = 0.16   
    check_interval = 8      
    last_check_time = 0
    is_executing = False

    def __init__(self, connectors: Dict[str, Any]):
        super().__init__(connectors)
        api_key = "94a54e57696198788682c7e8c4b0d5adab9b69c70fa"
        api_secret = "2c15f19dd463e312397d557d54531f63e5961a12da7"
        self.bfx = BitfinexAPI(api_key, api_secret, self.logger())

    def on_tick(self):
        if self.current_timestamp - self.last_check_time >= self.check_interval:
            if not self.is_executing:
                asyncio.ensure_future(self.maintain_strategy())
                self.last_check_time = self.current_timestamp

    async def maintain_strategy(self):
        self.is_executing = True
        try:
            # 1. 获取最新价
            ticker_url = f"https://api.bitfinex.com/v2/ticker/{self.bfx_symbol}"
            async with aiohttp.ClientSession() as session:
                async with session.get(ticker_url) as resp:
                    data = await resp.json()
                    mid_p = float(data[6]) if isinstance(data, list) and len(data) >= 7 else 0.0
            if mid_p <= 0: return

            margin = await self.bfx.get_margin_info()
            positions = await self.bfx.get_positions()
            orders = await self.bfx.get_open_orders(self.bfx_symbol)

            current_pos = 0.0
            entry_p = 0.0
            for pos in positions:
                if pos[0] == self.bfx_symbol:
                    current_pos = float(pos[2])
                    entry_p = float(pos[3])

            # 2. 🛡️ 止损止盈 (优先级最高)
            if abs(current_pos) > 0.0001:
                lev = self.leverage_long if current_pos > 0 else self.leverage_short
                pnl = (mid_p - entry_p)/entry_p if current_pos > 0 else (entry_p - mid_p)/entry_p
                if pnl * lev < -self.sl_roi:
                    await self.bfx.cancel_all()
                    await self.bfx.create_order(self.bfx_symbol, -current_pos, type="MARKET")
                    return
                # 唯一止盈单
                tp_p = round(entry_p * (1 + self.tp_pct if current_pos > 0 else 1 - self.tp_pct), 2)
                if not any(abs(float(o[16]) - tp_p) < 0.2 for o in orders):
                    await self.bfx.create_order(self.bfx_symbol, -current_pos, tp_p, lev=30)

            # 3. 🎯 守护阵地：判断什么时候该整体搬家
            # 只有价格跑出重心超过 3 层网格时，才允许重心移动
            if self.anchor_price <= 0 or abs(mid_p - self.anchor_price) > (self.anchor_price * 0.003):
                self.logger().info(f"📍 阵地迁移中: {self.anchor_price} -> {mid_p}")
                self.anchor_price = mid_p

            # 4. 补齐网格：只补缺失的坑位
            for i in range(1, self.grid_levels + 1):
                # 4.1 检查多头坑位 (锚点 - i*间隔)
                p_buy = round(self.anchor_price * (1 - i * self.grid_spacing), 2)
                if current_pos < self.max_pos_amount and not any(abs(float(o[16]) - p_buy) < 0.2 for o in orders):
                    # 这个坑没单，补一个
                    a_buy = max(0.005, round((margin * 0.45 * self.leverage_long) / (self.grid_levels * p_buy), 4))
                    await self.bfx.create_order(self.bfx_symbol, a_buy, p_buy, lev=self.leverage_long, flags=4096)
                
                # 4.2 检查空头坑位 (锚点 + i*间隔)
                p_sell = round(self.anchor_price * (1 + i * self.grid_spacing), 2)
                if current_pos > -self.max_pos_amount and not any(abs(float(o[16]) - p_sell) < 0.2 for o in orders):
                    # 这个坑没单，补一个
                    a_sell = -max(0.005, round((margin * 0.45 * self.leverage_short) / (self.grid_levels * p_sell), 4))
                    await self.bfx.create_order(self.bfx_symbol, a_sell, p_sell, lev=self.leverage_short, flags=4096)

            self.logger().info(f"✅ 阵地稳固 | 重心:{self.anchor_price} | 持仓:{current_pos:.4f}")

        except Exception as e:
            self.logger().error(f"❌ 运行异常: {str(e)}")
        finally:
            self.is_executing = False

    def format_status(self) -> str:
        return f"阵地稳固网格版 | 重心偏移阈值:0.3% | 严禁扎堆"

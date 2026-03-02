
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
# === 🛡️ Bitfinex 动态风控 API (终极版) ===
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
            async with session.post(url, headers=headers, data=body, timeout=8) as resp:
                return await resp.json()

    async def get_margin_info(self) -> float:
        # 🟢 核心：获取真实可用的可用保证金 (Available Margin)
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
        # 使用价格生成的 CID 防止重复
        cid = int(float(price) * 100) if price else int(time.time() % 1000000)
        req = {"type": type.upper(), "symbol": symbol, "amount": "{:.5f}".format(float(amount)), "lev": int(lev), "cid": cid, "flags": flags}
        if price: req["price"] = "{:.2f}".format(float(price))
        return await self.request("/auth/w/order/submit", req)

    async def cancel_orders(self, ids: List[int]):
        return await self.request("/auth/w/order/cancel/multi", {"id": ids})

    async def cancel_all(self):
        return await self.request("/auth/w/order/cancel/multi", {"all": 1})

# ==========================================
# === 🚀 深度加固高频网格策略 ===
# ==========================================
class 一天量化2026_3_2(ScriptStrategyBase):
    markets = {"okx": {"ETH-USDT"}} 
    bfx_symbol = "tETHF0:USTF0"
    
    leverage_long = 30
    leverage_short = 15
    grid_levels = 5         # 提高到 5 层，增加捕获深度
    grid_spacing = 0.001    # 0.1% 
    tp_pct = 0.002          # 0.2% 
    sl_roi = 0.20           # 20% ROI 止损
    max_pos_amount = 0.16   
    
    last_check_time = 0
    check_interval = 8      # 动态平衡扫描速度
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
            # 1. 获取全量数据 (加固版数据拉取)
            ticker_url = f"https://api.bitfinex.com/v2/ticker/{self.bfx_symbol}"
            # 注意：此处 mid_p 拿不到则不执行下文
            async with aiohttp.ClientSession() as session:
                async with session.get(ticker_url) as resp:
                    data = await resp.json()
                    mid_p = float(data[6]) if isinstance(data, list) and len(data) >= 7 else 0.0
            if mid_p <= 0: return

            available_margin = await self.bfx.get_margin_info()
            positions = await self.bfx.get_positions()
            open_orders = await self.bfx.get_open_orders(self.bfx_symbol)

            current_pos = 0.0
            entry_p = 0.0
            for pos in positions:
                if pos[0] == self.bfx_symbol:
                    current_pos = float(pos[2])
                    entry_p = float(pos[3])

            # 2. �️ 快速止损/止盈检查
            if abs(current_pos) > 0.0001:
                lev = self.leverage_long if current_pos > 0 else self.leverage_short
                profit_pct = (mid_p - entry_p) / entry_p if current_pos > 0 else (entry_p - mid_p) / entry_p
                roi = profit_pct * lev
                
                # A. ROI 熔断记录
                if roi < -self.sl_roi:
                    self.logger().info(f"💣 爆仓预警 ROI {roi*100:.1f}%，立即清仓！")
                    await self.bfx.cancel_all()
                    await self.bfx.create_order(self.bfx_symbol, -current_pos, type="MARKET")
                    return

                # B. 止盈维护
                tp_p = round(entry_p * (1 + self.tp_pct if current_pos > 0 else 1 - self.tp_pct), 2)
                has_tp = any(abs(float(o[16]) - tp_p) < 0.2 for o in open_orders)
                if not has_tp:
                    await self.bfx.create_order(self.bfx_symbol, -current_pos, tp_p, lev=30)

            # 3. 🧹 清理远离盘口的“僵尸单” (超过 1% 范围)
            zombie_ids = [o[0] for o in open_orders if abs(float(o[16]) - mid_p) > (mid_p * 0.015)]
            if zombie_ids:
                self.logger().info(f"🧹 正在清理 {len(zombie_ids)} 个远离盘口的僵尸订单...")
                await self.bfx.cancel_orders(zombie_ids)

            # 4. 补网格逻辑 (基于真实可用保证金)
            l_grids = [o for o in open_orders if float(o[6]) > 0 and abs(float(o[16]) - mid_p) < (mid_p * 0.01)]
            s_grids = [o for o in open_orders if float(o[6]) < 0 and abs(float(o[16]) - mid_p) < (mid_p * 0.01)]

            if current_pos < self.max_pos_amount and len(l_grids) < self.grid_levels:
                await self.deploy_one("long", mid_p, available_margin, len(l_grids))
            
            if current_pos > -self.max_pos_amount and len(s_grids) < self.grid_levels:
                await self.deploy_one("short", mid_p, available_margin, len(s_grids))

        finally:
            self.is_executing = False

    async def deploy_one(self, side, mid_p, margin, count):
        idx = count + 1
        # 计算下单量：拿可用保证金的 10% 来挂每一层
        use_margin = margin if margin > 1.0 else 10.0
        p = round(mid_p * (1 - idx * self.grid_spacing if side=="long" else 1 + idx * self.grid_spacing), 2)
        # 量 = (保证金 * 权重 * 杠杆) / 价格
        a = max(0.005, round((use_margin * 0.45 * (self.leverage_long if side=="long" else self.leverage_short)) / (self.grid_levels * p), 4))
        if side == "short": a = -a
        await self.bfx.create_order(self.bfx_symbol, a, p, lev=(self.leverage_long if side=="long" else self.leverage_short), flags=4096)

    def format_status(self) -> str:
        return f"终极加固版 | 间距:0.1% | 止盈:0.2% | 清理僵尸单开启"

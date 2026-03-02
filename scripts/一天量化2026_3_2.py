
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
# === 🛡️ Bitfinex 智能校准 API ===
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
        side_val = 1000000 if float(amount) > 0 else 2000000
        cid = int(float(price) * 100) + side_val if price else int(time.time() % 1000000)
        req = {"type": type.upper(), "symbol": symbol, "amount": "{:.5f}".format(float(amount)), "lev": int(lev), "cid": cid, "flags": flags}
        if price: req["price"] = "{:.2f}".format(float(price))
        return await self.request("/auth/w/order/submit", req)

    async def cancel_orders(self, ids: List[int]):
        if not ids: return
        return await self.request("/auth/w/order/cancel/multi", {"id": ids})

    async def cancel_all(self):
        return await self.request("/auth/w/order/cancel/multi", {"all": 1})

# ==========================================
# === 🚀 智能网格校准版 (强力对齐) ===
# ==========================================
class 一天量化2026_3_2(ScriptStrategyBase):
    markets = {"okx": {"ETH-USDT"}} 
    bfx_symbol = "tETHF0:USTF0"
    
    leverage_long = 30
    leverage_short = 15
    grid_levels = 4         
    grid_spacing = 0.001    # 0.1%
    tp_pct = 0.002          # 0.2%
    sl_roi = 0.20           # 20% ROI 止损
    max_pos_amount = 0.16   
    
    # 校准参数
    last_grid_center = 0.0
    last_check_time = 0
    check_interval = 6      
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
            # 1. 获取行情
            ticker_url = f"https://api.bitfinex.com/v2/ticker/{self.bfx_symbol}"
            async with aiohttp.ClientSession() as session:
                async with session.get(ticker_url) as resp:
                    data = await resp.json()
                    mid_p = float(data[6]) if isinstance(data, list) and len(data) >= 7 else 0.0
            if mid_p <= 0: return

            margin = await self.bfx.get_margin_info()
            positions = await self.bfx.get_positions()
            orders = await self.bfx.get_open_orders(self.bfx_symbol)

            curr_pos = 0.0
            entry_p = 0.0
            for pos in positions:
                if pos[0] == self.bfx_symbol:
                    curr_pos = float(pos[2])
                    entry_p = float(pos[3])

            # 2. 🛡️ 止损止盈
            if abs(curr_pos) > 0.0001:
                lev = self.leverage_long if curr_pos > 0 else self.leverage_short
                p_pct = (mid_p - entry_p) / entry_p if curr_pos > 0 else (entry_p - mid_p) / entry_p
                if p_pct * lev < -self.sl_roi:
                    self.logger().info("💣 熔断！清仓！")
                    await self.bfx.cancel_all()
                    await self.bfx.create_order(self.bfx_symbol, -curr_pos, type="MARKET")
                    return
                # 挂止盈单 (确保只有一张)
                tp_p = round(entry_p * (1 + self.tp_pct if curr_pos > 0 else 1 - self.tp_pct), 2)
                if not any(abs(float(o[16]) - tp_p) < 0.2 for o in orders):
                    await self.bfx.create_order(self.bfx_symbol, -curr_pos, tp_p, lev=30)

            # 3. 🎯 智能校准：判断价格偏移
            # 这里的逻辑是：如果价格偏离上次中心超过 0.04%，或者网格单缺失，则触发重挂
            grid_orders = [o for o in orders if abs(float(o[16]) - mid_p) < (mid_p * 0.015)]
            # 排除止盈单
            non_tp_grids = [o for o in grid_orders if abs(float(o[16]) - (entry_p * (1.002 if curr_pos>0 else 0.998))) > 0.5] if abs(curr_pos)>0.0001 else grid_orders
            
            drift = abs(mid_p - self.last_grid_center) / mid_p if self.last_grid_center > 0 else 1.0
            needs_regrid = drift > 0.0004 or len(non_tp_grids) < (self.grid_levels * 1.5)

            if needs_regrid:
                self.logger().info(f"🔄 捕获到偏移 {drift*100:.3f}%，正在进行强力对齐补单...")
                # A. 撤销原来的所有网格单 (不撤止盈单)
                ids_to_cancel = [o[0] for o in non_tp_grids]
                if ids_to_cancel: await self.bfx.cancel_orders(ids_to_cancel)
                
                # B. 重新铺设阵列
                for i in range(1, self.grid_levels + 1):
                    # 买单层
                    if curr_pos < self.max_pos_amount:
                        p_buy = round(mid_p * (1 - i * self.grid_spacing), 2)
                        a_buy = max(0.005, round((margin * 0.4 * self.leverage_long) / (self.grid_levels * p_buy), 4))
                        await self.bfx.create_order(self.bfx_symbol, a_buy, p_buy, lev=self.leverage_long, flags=4096)
                    
                    # 卖单层
                    if curr_pos > -self.max_pos_amount:
                        p_sell = round(mid_p * (1 + i * self.grid_spacing), 2)
                        a_sell = -max(0.005, round((margin * 0.4 * self.leverage_short) / (self.grid_levels * p_sell), 4))
                        await self.bfx.create_order(self.bfx_symbol, a_sell, p_sell, lev=self.leverage_short, flags=4096)
                
                self.last_grid_center = mid_p

            self.logger().info(f"✅ 校准完毕 | 现价:{mid_p} | 持仓:{curr_pos:.4f}")

        except Exception as e:
            self.logger().error(f"❌ 运行报错: {str(e)}")
        finally:
            self.is_executing = False

    def format_status(self) -> str:
        return f"智能校准版 | 强力对齐开启 | 间距:0.1%"

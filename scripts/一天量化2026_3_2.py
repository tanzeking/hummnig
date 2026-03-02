
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
# === 🛡️ Bitfinex 极速追踪 API ===
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
        # 💡 CID 唯一化：加入方向标志，防止多空同价位冲突
        side_val = 1000000 if float(amount) > 0 else 2000000
        cid = int(float(price) * 100) + side_val if price else int(time.time() % 1000000)
        req = {"type": type.upper(), "symbol": symbol, "amount": "{:.5f}".format(float(amount)), "lev": int(lev), "cid": cid, "flags": flags}
        if price: req["price"] = "{:.2f}".format(float(price))
        return await self.request("/auth/w/order/submit", req)

    async def cancel_orders(self, ids: List[int]):
        return await self.request("/auth/w/order/cancel/multi", {"id": ids})

    async def cancel_all(self):
        return await self.request("/auth/w/order/cancel/multi", {"all": 1})

# ==========================================
# === 🚀 极速行情追踪版 (0.1% 间距) ===
# ==========================================
class 一天量化2026_3_2(ScriptStrategyBase):
    markets = {"okx": {"ETH-USDT"}} 
    bfx_symbol = "tETHF0:USTF0"
    
    leverage_long = 30
    leverage_short = 15
    grid_levels = 4         
    grid_spacing = 0.001    # 0.1% 间距约为 2 刀
    tp_pct = 0.002          # 0.2% 止盈
    sl_roi = 0.20           # 20% ROI 止损
    max_pos_amount = 0.16   
    
    last_check_time = 0
    check_interval = 6      # 加快扫描速度到 6s
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
            # 1. 获取最新市场价
            ticker_url = f"https://api.bitfinex.com/v2/ticker/{self.bfx_symbol}"
            async with aiohttp.ClientSession() as session:
                async with session.get(ticker_url) as resp:
                    data = await resp.json()
                    mid_p = float(data[6]) if isinstance(data, list) and len(data) >= 7 else 0.0
            if mid_p <= 0: return

            margin = await self.bfx.get_margin_info()
            positions = await self.bfx.get_positions()
            open_orders = await self.bfx.get_open_orders(self.bfx_symbol)

            curr_pos = 0.0
            entry_p = 0.0
            for pos in positions:
                if pos[0] == self.bfx_symbol:
                    curr_pos = float(pos[2])
                    entry_p = float(pos[3])

            # 2. 🛡️ 止损/止盈检查
            if abs(curr_pos) > 0.0001:
                lev = self.leverage_long if curr_pos > 0 else self.leverage_short
                profit_pct = (mid_p - entry_p) / entry_p if curr_pos > 0 else (entry_p - mid_p) / entry_p
                if profit_pct * lev < -self.sl_roi:
                    self.logger().info("💣 触发止损！清仓！")
                    await self.bfx.cancel_all()
                    await self.bfx.create_order(self.bfx_symbol, -curr_pos, type="MARKET")
                    return
                # 挂止盈
                tp_p = round(entry_p * (1 + self.tp_pct if curr_pos > 0 else 1 - self.tp_pct), 2)
                if not any(abs(float(o[16]) - tp_p) < 0.2 for o in open_orders):
                    await self.bfx.create_order(self.bfx_symbol, -curr_pos, tp_p, lev=30)

            # 3. 🧹 行情追踪清理 (收紧到 0.4% 的超窄范围)
            # 如果挂单离现价超过 8 刀，直接撤销重挂，保持阵地跟随价格
            to_cancel = []
            for o in open_orders:
                o_price = float(o[16])
                # 如果不是止盈单，且距离现价太远，就撤掉
                if abs(o_price - mid_p) > (mid_p * 0.004): 
                    to_cancel.append(o[0])
            
            if to_cancel:
                self.logger().info(f"🔄 行情走远，正在主动清理 {len(to_cancel)} 个滞后单...")
                await self.bfx.cancel_orders(to_cancel)
                await asyncio.sleep(1) # 给交易所 1 秒同步数据
                open_orders = await self.bfx.get_open_orders(self.bfx_symbol)

            # 4. 补齐贴身网格
            l_grids = [o for o in open_orders if float(o[6]) > 0 and abs(float(o[16]) - mid_p) < (mid_p * 0.015)]
            s_grids = [o for o in open_orders if float(o[6]) < 0 and abs(float(o[16]) - mid_p) < (mid_p * 0.015)]

            if curr_pos < self.max_pos_amount and len(l_grids) < self.grid_levels:
                await self.deploy_one("long", mid_p, margin, len(l_grids))
            if curr_pos > -self.max_pos_amount and len(s_grids) < self.grid_levels:
                await self.deploy_one("short", mid_p, margin, len(s_grids))

            self.logger().info(f"📊 追踪中 | 现价:{mid_p} | 持仓:{curr_pos:.4f} | 网格:{len(l_grids)}买/{len(s_grids)}卖")

        except Exception as e:
            self.logger().error(f"❌ 运行错误: {str(e)}")
        finally:
            self.is_executing = False

    async def deploy_one(self, side, mid_p, margin, count):
        idx = count + 1
        use_m = margin if margin > 1.0 else 5.0
        p = round(mid_p * (1 - idx * self.grid_spacing if side=="long" else 1 + idx * self.grid_spacing), 2)
        a = max(0.005, round((use_m * 0.45 * (self.leverage_long if side=="long" else self.leverage_short)) / (self.grid_levels * p), 4))
        if side == "short": a = -a
        await self.bfx.create_order(self.bfx_symbol, a, p, lev=(self.leverage_long if side=="long" else self.leverage_short), flags=4096)

    def format_status(self) -> str:
        return f"动态追踪版 | 自动洗牌僵尸单 | 间距:0.1%"

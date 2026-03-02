
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
# === 🛡️ Bitfinex 生产力稳固版 API ===
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
            try:
                async with session.post(url, headers=headers, data=body, timeout=8) as resp:
                    return await resp.json()
            except Exception as e:
                self.logger.error(f"API请求失败: {str(e)}")
                return None

    async def get_margin_info(self) -> float:
        resp = await self.request("/auth/r/info/margin/base")
        if resp and isinstance(resp, list) and len(resp) >= 4:
            return float(resp[3][2]) if isinstance(resp[3], list) and len(resp[3]) > 2 else 5.0
        return 5.0

    async def get_positions(self):
        return await self.request("/auth/r/positions") or []

    async def get_open_orders(self, symbol):
        resp = await self.request("/auth/r/orders")
        return [o for o in resp if isinstance(o, list) and len(o) > 3 and o[3] == symbol] if isinstance(resp, list) else []

    async def create_order(self, symbol, amount, price=None, lev=30, type="LIMIT", flags=0):
        # CID 锁定：价格+方向，杜绝扎堆
        side_prefix = 1 if float(amount) > 0 else 2
        cid = int(float(price) * 100) + (side_prefix * 1000000) if price else int(time.time() % 1000000)
        req = {"type": type.upper(), "symbol": symbol, "amount": "{:.5f}".format(float(amount)), "lev": int(lev), "cid": cid, "flags": flags}
        if price: req["price"] = "{:.2f}".format(float(price))
        return await self.request("/auth/w/order/submit", req)

    async def cancel_orders(self, ids: List[int]):
        if not ids: return
        return await self.request("/auth/w/order/cancel/multi", {"id": ids})

    async def cancel_all(self):
        return await self.request("/auth/w/order/cancel/multi", {"all": 1})

# ==========================================
# === 🚀 阵地保卫版 2.0 (分批平仓) ===
# ==========================================
class 一天量化2026_3_2(ScriptStrategyBase):
    markets = {"okx": {"ETH-USDT"}} 
    bfx_symbol = "tETHF0:USTF0"
    
    leverage_long = 30
    leverage_short = 15
    grid_levels = 4         
    grid_spacing = 0.001    # 0.1% 
    tp_pct = 0.002          # 0.2% 
    sl_roi = 0.20           
    
    anchor_price = 0.0      
    max_pos_amount = 0.16   
    check_interval = 8      
    last_check_time = 0
    is_executing = False

    def __init__(self, connectors: Dict[str, Any]):
        super().__init__(connectors)
        api_key = "94a54e57696198788682c7e8c4b0d5adab9b69c70fa"
        api_secret = "2c15f19dd463e312397d557d54531f63e5961a12da7"
        self.bfx = BitfinexAPI(api_key, api_secret, self.logger())

    async def on_stop(self):
        self.logger().info("🛑 正在执行停机清算：撤销所有挂单并在市价平仓...")
        try:
            # 1. 撤销所有挂单
            await self.bfx.cancel_all()
            
            # 2. 获取并平掉当前仓位
            positions = await self.bfx.get_positions()
            for pos in positions:
                if pos[0] == self.bfx_symbol:
                    amount = float(pos[2])
                    if abs(amount) > 0.0001:
                        self.logger().info(f"🚩 正在市价平掉仓位: {amount}")
                        await self.bfx.create_order(self.bfx_symbol, -amount, type="MARKET")
            self.logger().info("✅ 停机清算完成。")
        except Exception as e:
            self.logger().error(f"❌ 停机清算发生错误: {str(e)}")

    def on_tick(self):
        if self.current_timestamp - self.last_check_time >= self.check_interval:
            if not self.is_executing:
                asyncio.ensure_future(self.maintain_strategy())
                self.last_check_time = self.current_timestamp

    async def maintain_strategy(self):
        self.is_executing = True
        try:
            # 1. 极速获取现价
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

            # 2. 🛡️ 智能平仓逻辑
            status_msg = ""
            if abs(curr_pos) > 0.0001:
                lev = self.leverage_long if curr_pos > 0 else self.leverage_short
                tp_p = round(entry_p * (1 + self.tp_pct if curr_pos > 0 else 1 - self.tp_pct), 2)
                status_msg = f" | 🎯目标止盈:{tp_p}"

                # A. 止损检查
                pnl = (mid_p - entry_p)/entry_p if curr_pos > 0 else (entry_p - mid_p)/entry_p
                if pnl * lev < -self.sl_roi:
                    self.logger().info("💣 风险极高，执行止损平仓！")
                    await self.bfx.cancel_all()
                    await self.bfx.create_order(self.bfx_symbol, -curr_pos, type="MARKET")
                    return

                # B. 止盈单同步：检查金额是否匹配，不匹配则重挂
                tp_order = next((o for o in orders if abs(float(o[16]) - tp_p) < 0.2), None)
                if not tp_order or abs(float(tp_order[6]) + curr_pos) > 0.001:
                    if tp_order: await self.bfx.cancel_orders([tp_order[0]])
                    # 为全仓挂出“一把清”的止盈单
                    await self.bfx.create_order(self.bfx_symbol, -curr_pos, tp_p, lev=30)

            # 3. 🎯 阵地守护（防止乱跑）
            if self.anchor_price <= 0 or abs(mid_p - self.anchor_price) > (self.anchor_price * 0.003):
                self.logger().info(f"� 捕获大波动，重整阵地中心: {mid_p}")
                self.anchor_price = mid_p

            # 4. 精准占位补单（决不扎堆）
            for i in range(1, self.grid_levels + 1):
                # 多头网格（买单）
                p_buy = round(self.anchor_price * (1 - i * self.grid_spacing), 2)
                if curr_pos < self.max_pos_amount and not any(abs(float(o[16]) - p_buy) < 0.15 for o in orders):
                    amt = max(0.005, round((margin * 0.45 * self.leverage_long) / (self.grid_levels * p_buy), 4))
                    await self.bfx.create_order(self.bfx_symbol, amt, p_buy, lev=self.leverage_long, flags=4096)
                
                # 空头网格（卖单/减仓）
                p_sell = round(self.anchor_price * (1 + i * self.grid_spacing), 2)
                # 即使有仓位也要挂卖单，它们会充当分批止盈的角色
                if curr_pos > -self.max_pos_amount and not any(abs(float(o[16]) - p_sell) < 0.15 for o in orders):
                    amt = -max(0.005, round((margin * 0.45 * self.leverage_short) / (self.grid_levels * p_sell), 4))
                    await self.bfx.create_order(self.bfx_symbol, amt, p_sell, lev=self.leverage_short, flags=4096)

            self.logger().info(f"📊 运行中 | 现价:{mid_p} | 持仓:{curr_pos:.4f}{status_msg}")

        except Exception as e:
            self.logger().error(f"❌ 系统异常: {str(e)}")
        finally:
            self.is_executing = False

    def format_status(self) -> str:
        return f"阵地保卫战 2.0 | 进阶分批减仓 | 间距:0.1%"

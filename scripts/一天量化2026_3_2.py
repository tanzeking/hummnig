
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
# === 🛡️ Bitfinex 智能保证金防御版 API ===
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
                async with session.post(url, headers=headers, data=body, timeout=5) as resp:
                    res_json = await resp.json()
                    # 💡 捕获“保证金不足”报错，防止刷屏
                    if isinstance(res_json, list) and len(res_json) > 1 and res_json[0] == "error":
                        if "margin" in str(res_json[2]).lower():
                            return {"margin_error": True, "raw": res_json}
                    return res_json
            except: return None

    async def get_derivatives_balance(self) -> float:
        resp = await self.request("/auth/r/wallets")
        if resp and isinstance(resp, list):
            for w in resp:
                if w[0] == "derivatives" and (w[1] == "USTF0" or w[1] == "USD"):
                    return float(w[4])
        return 1.0

    async def get_positions(self):
        return await self.request("/auth/r/positions") or []

    async def get_open_orders(self, symbol):
        resp = await self.request("/auth/r/orders")
        return [o for o in resp if isinstance(o, list) and len(o) > 3 and o[3] == symbol] if isinstance(resp, list) else []

    async def create_order(self, symbol, amount, price=None, lev=30, type="LIMIT", flags=0):
        side = 1 if float(amount) > 0 else 2
        cid = int(float(price) * 100) + (side * 1000000) if price else int(time.time() % 1000000)
        req = {"type": type.upper(), "symbol": symbol, "amount": "{:.5f}".format(float(amount)), "lev": int(lev), "cid": cid, "flags": flags}
        if price: req["price"] = "{:.2f}".format(float(price))
        return await self.request("/auth/w/order/submit", req)

    async def cancel_orders(self, ids: List[int]):
        if not ids: return
        return await self.request("/auth/w/order/cancel/multi", {"id": ids})

    async def cancel_all(self):
        return await self.request("/auth/w/order/cancel/multi", {"all": 1})

# ==========================================
# === 🚀 阵地保卫版 5.0 (保证金熔断版) ===
# ==========================================
class 一天量化2026_3_2(ScriptStrategyBase):
    markets = {"okx": {"ETH-USDT"}} 
    bfx_symbol = "tETHF0:USTF0"
    
    leverage_long = 30
    leverage_short = 15
    grid_levels = 4         
    grid_spacing = 0.001    
    tp_pct = 0.002          
    sl_roi = 0.50           # ✨ 止损调整为 50% ROI
    
    anchor_price = 0.0      
    max_pos_amount = 0.16   
    check_interval = 8      
    last_check_time = 0
    is_executing = False
    
    # 💥 安全防御参数
    safety_buffer_usd = 5.0    # 强制预留 5U 不动，专门对付手续费和插针
    margin_utilization = 0.35  # 单边占用降至 35%

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

            raw_balance = await self.bfx.get_derivatives_balance()
            active_balance = max(0.0, raw_balance - self.safety_buffer_usd) # 扣除 5U 强制预留
            
            positions = await self.bfx.get_positions()
            all_orders = await self.bfx.get_open_orders(self.bfx_symbol)

            curr_pos = 0.0
            entry_p = 0.0
            for pos in positions:
                if pos[0] == self.bfx_symbol:
                    curr_pos = float(pos[2])
                    entry_p = float(pos[3])

            # 🛠️ 1. 平仓优先级最高 (不需要检查保证金)
            if abs(curr_pos) > 0.0001:
                tp_p = round(entry_p * (1 + self.tp_pct if curr_pos > 0 else 1 - self.tp_pct), 2)
                tp_order = next((o for o in all_orders if abs(float(o[16]) - tp_p) < 0.2), None)
                if not tp_order or abs(float(tp_order[6]) + curr_pos) > 0.001:
                    if tp_order: await self.bfx.cancel_orders([tp_order[0]])
                    await self.bfx.create_order(self.bfx_symbol, -curr_pos, tp_p, lev=30)
            
            # 🛡️ 熔断：如果可用资金已经极低，不再进行任何加仓/补网动作
            if active_balance < 1.0:
                self.logger().warning(f"⚠️ 余额告急({raw_balance}U)，已触发安全熔断，暂停补单！")
                self.is_executing = False
                return

            # 2. 阵地管理
            if self.anchor_price <= 0 or abs(mid_p - self.anchor_price) > (self.anchor_price * 0.003):
                self.logger().info(f"📍 阵地迁移 | 预留可用金:{self.safety_buffer_usd}U")
                ids_to_cancel = [o[0] for o in all_orders if abs(float(o[6]) + curr_pos) > 0.001 or abs(curr_pos) < 0.001]
                if ids_to_cancel: await self.bfx.cancel_orders(ids_to_cancel)
                await asyncio.sleep(1)
                self.anchor_price = mid_p
                all_orders = [o for o in all_orders if o[0] not in ids_to_cancel]

            # 3. 带防御的精准补单
            failed_due_to_margin = False
            for i in range(1, self.grid_levels + 1):
                if failed_due_to_margin: break # 上一单要是报错没钱了，后面就不试了
                
                # 买单
                p_buy = round(self.anchor_price * (1 - i * self.grid_spacing), 2)
                if curr_pos < self.max_pos_amount and not any(abs(float(o[16]) - p_buy) < 0.2 for o in all_orders):
                    amt = max(0.005, round((active_balance * self.margin_utilization * self.leverage_long) / (self.grid_levels * p_buy), 4))
                    res = await self.bfx.create_order(self.bfx_symbol, amt, p_buy, lev=self.leverage_long, flags=4096)
                    if isinstance(res, dict) and res.get("margin_error"): failed_due_to_margin = True
                
                # 卖单
                p_sell = round(self.anchor_price * (1 + i * self.grid_spacing), 2)
                if curr_pos > -self.max_pos_amount and not any(abs(float(o[16]) - p_sell) < 0.2 for o in all_orders):
                    amt = -max(0.005, round((active_balance * self.margin_utilization * self.leverage_short) / (self.grid_levels * p_sell), 4))
                    res = await self.bfx.create_order(self.bfx_symbol, amt, p_sell, lev=self.leverage_short, flags=4096)
                    if isinstance(res, dict) and res.get("margin_error"): failed_due_to_margin = True

            self.logger().info(f"✅ 阵地对齐 | 余额:{raw_balance:.2f}U | 持仓:{curr_pos:.4f}")

        except Exception as e:
            self.logger().error(f"❌ 系统异常: {str(e)}")
        finally:
            self.is_executing = False

    def format_status(self) -> str:
        return f"阵地保卫战 5.0 | 5U强制熔断保护 | 间距:0.1%"

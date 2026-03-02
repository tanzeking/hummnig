
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
# === 🛡️ Bitfinex 智能阵地加固版 API ===
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
                    return await resp.json()
            except: return None

    async def get_derivatives_balance(self) -> float:
        # 💡 针对合约账户的可用保证金精准核对
        resp = await self.request("/auth/r/wallets")
        if resp and isinstance(resp, list):
            for w in resp:
                # 寻找合约钱包 (derivatives) 的可用余额 (第5位)
                if w[0] == "derivatives" and (w[1] == "USTF0" or w[1] == "USD"):
                    return float(w[4])
        return 10.0

    async def get_positions(self):
        return await self.request("/auth/r/positions") or []

    async def get_open_orders(self, symbol):
        resp = await self.request("/auth/r/orders")
        return [o for o in resp if isinstance(o, list) and len(o) > 3 and o[3] == symbol] if isinstance(resp, list) else []

    async def create_order(self, symbol, amount, price=None, lev=30, type="LIMIT", flags=0):
        # 💡 CID 绑定：侧重防止同价位多开
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
# === 🚀 阵地保卫版 3.0 (全速对齐) ===
# ==========================================
class 一天量化2026_3_2(ScriptStrategyBase):
    markets = {"okx": {"ETH-USDT"}} 
    bfx_symbol = "tETHF0:USTF0"
    
    leverage_long = 30
    leverage_short = 15
    grid_levels = 4         
    grid_spacing = 0.001    # 0.1% = 2刀
    tp_pct = 0.002          # 0.2% 
    
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
        await self.bfx.cancel_all()

    def on_tick(self):
        if self.current_timestamp - self.last_check_time >= self.check_interval:
            if not self.is_executing:
                asyncio.ensure_future(self.maintain_strategy())
                self.last_check_time = self.current_timestamp

    async def maintain_strategy(self):
        self.is_executing = True
        try:
            # 1. 获取最新行情
            ticker_url = f"https://api.bitfinex.com/v2/ticker/{self.bfx_symbol}"
            async with aiohttp.ClientSession() as session:
                async with session.get(ticker_url) as resp:
                    data = await resp.json()
                    mid_p = float(data[6]) if isinstance(data, list) and len(data) >= 7 else 0.0
            if mid_p <= 0: return

            balance = await self.bfx.get_derivatives_balance()
            positions = await self.bfx.get_positions()
            all_orders = await self.bfx.get_open_orders(self.bfx_symbol)

            curr_pos = 0.0
            entry_p = 0.0
            for pos in positions:
                if pos[0] == self.bfx_symbol:
                    curr_pos = float(pos[2])
                    entry_p = float(pos[3])

            # 2. 🛡️ 智能止盈单维护
            if abs(curr_pos) > 0.0001:
                tp_p = round(entry_p * (1 + self.tp_pct if curr_pos > 0 else 1 - self.tp_pct), 2)
                # 寻找现有的止盈单
                tp_order = next((o for o in all_orders if abs(float(o[16]) - tp_p) < 0.2), None)
                if not tp_order or abs(float(tp_order[6]) + curr_pos) > 0.001:
                    if tp_order: await self.bfx.cancel_orders([tp_order[0]])
                    self.logger().info(f"🎯 正在同步止盈单: 价格 {tp_p}, 数量 {-curr_pos}")
                    await self.bfx.create_order(self.bfx_symbol, -curr_pos, tp_p, lev=30)
            
            # 3. 🎯 阵地整体重整 (核心改动)
            # 价格跑出 0.3% 时，发起强制大扫除
            if self.anchor_price <= 0 or abs(mid_p - self.anchor_price) > (self.anchor_price * 0.003):
                self.logger().info(f"✨ 阵地发生显著偏移，正在执行全网格重洗...")
                
                # 撤销除止盈单以外的所有网格
                ids_to_cancel = []
                for o in all_orders:
                    # 如果不是止盈单 (止盈单通常是全仓量)
                    if abs(float(o[6]) + curr_pos) > 0.001 or abs(curr_pos) < 0.001:
                        ids_to_cancel.append(o[0])
                
                if ids_to_cancel:
                    await self.bfx.cancel_orders(ids_to_cancel)
                    await asyncio.sleep(1) # 给 API 反应时间
                
                self.anchor_price = mid_p
                # 更新本地订单缓存
                all_orders = [o for o in all_orders if o[0] not in ids_to_cancel]

            # 4. 补齐网格坑位
            for i in range(1, self.grid_levels + 1):
                # 4.1 买单坑
                p_buy = round(self.anchor_price * (1 - i * self.grid_spacing), 2)
                if curr_pos < self.max_pos_amount and not any(abs(float(o[16]) - p_buy) < 0.2 for o in all_orders):
                    amt = max(0.005, round((balance * 0.45 * self.leverage_long) / (self.grid_levels * p_buy), 4))
                    await self.bfx.create_order(self.bfx_symbol, amt, p_buy, lev=self.leverage_long, flags=4096)
                
                # 4.2 卖单坑 (即使多头持仓也要挂，充当减仓位)
                p_sell = round(self.anchor_price * (1 + i * self.grid_spacing), 2)
                if curr_pos > -self.max_pos_amount and not any(abs(float(o[16]) - p_sell) < 0.2 for o in all_orders):
                    amt = -max(0.005, round((balance * 0.45 * self.leverage_short) / (self.grid_levels * p_sell), 4))
                    await self.bfx.create_order(self.bfx_symbol, amt, p_sell, lev=self.leverage_short, flags=4096)

            self.logger().info(f"✅ 阵地对齐中 | 现价:{mid_p} | 挂单数:{len(all_orders)}")

        except Exception as e:
            self.logger().error(f"❌ 系统异常: {str(e)}")
        finally:
            self.is_executing = False

    def format_status(self) -> str:
        return f"阵地保卫战 3.0 | 全网格自动大扫除 | 间距:0.1%"

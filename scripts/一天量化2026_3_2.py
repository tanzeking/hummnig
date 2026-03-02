
import os
import json
import asyncio
import time
import hmac
import hashlib
import aiohttp
from collections import OrderedDict
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any

from hummingbot.strategy.script_strategy_base import ScriptStrategyBase

# --- 物理级格式化引擎 (杜绝连写/精度错误) ---
def d_p(val): 
    v = int(float(val) * 10 + 0.5)
    s = str(v)
    if len(s) < 2: s = s.zfill(2)
    return s[:-1] + "." + s[-1]

def d_a(val):
    raw = float(val)
    if abs(raw) < 0.0008: raw = 0.0008 if raw > 0 else -0.0008
    return "{:.5f}".format(raw)

class BfxRest:
    def __init__(self, key, secret, logger):
        self.key, self.secret, self.logger = key, secret, logger
        self.url = "https://api.bitfinex.com/v2"

    async def req(self, path, payload):
        nonce = str(int(time.time() * 1000000))
        body = json.dumps(payload, separators=(',', ':'))
        sig_str = f"/api/v2{path}{nonce}{body}"
        sig = hmac.new(self.secret.encode(), sig_str.encode(), hashlib.sha384).hexdigest()
        headers = {"bfx-nonce": nonce, "bfx-apikey": self.key, "bfx-signature": sig, "content-type": "application/json"}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(f"{self.url}{path}", headers=headers, data=body) as r:
                    return await r.json()
            except Exception as e:
                return ["error", 0, str(e)]

    async def get_price(self, sym):
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(f"{self.url}/ticker/{sym}") as r:
                    data = await r.json()
                    return float(data[6]) if isinstance(data, list) and len(data) >= 7 else 0.0
            except: return 0.0

    async def get_bal(self):
        r = await self.req("/auth/r/wallets", {})
        max_bal = 0.0
        if isinstance(r, list):
            for w in r:
                if len(w) >= 5 and w[1] in ["USTF0", "UST", "USDt", "USD"]:
                    available = float(w[4]) if w[4] is not None else 0.0
                    if available > max_bal: max_bal = available
        return max_bal

class 一天量化2026_3_2(ScriptStrategyBase):
    markets = {"okx": {"ETH-USDT"}} 
    
    # --- 锁定参数 ---
    SYM = "tETHF0:USTF0"
    LEV = 30
    LEVELS = 4
    SPACING = 0.001 
    TP_PCT = 0.002      
    R_L = 1920.0
    R_H = 1999.0

    def __init__(self, connectors):
        super().__init__(connectors)
        k = os.getenv("BITFINEX_API_KEY", "94a54e57696198788682c7e8c4b0d5adab9b69c70fa")
        s = os.getenv("BITFINEX_API_SECRET", "2c15f19dd463e312397d557d54531f63e5961a12da7")
        self.api = BfxRest(k, s, self.logger())
        self.initialized = False
        self.last_check = 0
        self.tracked_orders = {}

    def on_tick(self):
        now = self.current_timestamp
        if not self.initialized:
            asyncio.ensure_future(self.setup_grid())
            self.initialized = True
            self.last_check = now
        
        if now - self.last_check >= 10:
            self.last_check = now
            asyncio.ensure_future(self.monitor_fills())

    async def setup_grid(self):
        p = await self.api.get_price(self.SYM)
        if p < self.R_L or p > self.R_H:
            self.logger().warning(f"待机价格: {p} 超出范围")
            return

        self.logger().info("🧹 正在清理成交前挂单...")
        await self.api.req("/auth/w/order/cancel", {"all": 1})
        self.tracked_orders.clear()
        
        bal = await self.api.get_bal()
        if bal <= 0.1: bal = 10.0 
            
        # 修正保证金数学分配逻辑：(余额 * 杠杆 * 0.9缓冲) / (4层买 + 4层卖)
        # 这确保 10U 绝对能挂出所有单子而不报错
        unit_val = (bal * self.LEV * 0.9) / (self.LEVELS * 2)
        self.logger().info(f"📊 资金分配: 实时余额 {bal:.2f}u | 单笔货值 {unit_val:.2f}u")

        for i in range(1, self.LEVELS + 1):
            bp = p * (1 - i * self.SPACING)
            if bp >= self.R_L: await self.place_grid("buy", bp, unit_val/bp)
            sp = p * (1 + i * self.SPACING)
            if sp <= self.R_H: await self.place_grid("sell", sp, unit_val/sp)
            await asyncio.sleep(0.4)

    async def place_grid(self, side, price, amount):
        p_s = d_p(price)
        a_s = d_a(amount if side == "buy" else -amount)
        payload = OrderedDict([("type", "LIMIT"), ("symbol", self.SYM), ("amount", a_s), ("price", p_s), ("lev", self.LEV)])
        res = await self.api.req("/auth/w/order/submit", payload)
        
        if isinstance(res, list) and len(res) > 0 and res[0] != "error":
            oid = res[4][0][0]
            self.tracked_orders[oid] = {"side": side, "price": price, "amount": abs(amount), "is_tp": False}
            self.logger().info(f"✅ 挂单成功: {side} @ {p_s}")
        else:
            err = res[2] if isinstance(res, list) and len(res) >= 3 else str(res)
            self.logger().error(f"❌ 挂单失败: {side} @ {p_s} | 原因: {err}")

    async def monitor_fills(self):
        res = await self.api.req("/auth/r/orders", {})
        if not isinstance(res, list) or (len(res) > 0 and res[0] == "error"): return
        live_ids = {o[0] for o in res if isinstance(o, list)}
        for oid in list(self.tracked_orders.keys()):
            if oid not in live_ids:
                info = self.tracked_orders[oid]
                self.logger().info(f"🔔 成交通知: {info['side']} @ {info['price']} 手下！")
                if not info["is_tp"]: await self.place_tp(info)
                del self.tracked_orders[oid]

    async def place_tp(self, entry_info):
        side, p, a = entry_info["side"], entry_info["price"], entry_info["amount"]
        tp_price = p * (1 + self.TP_PCT) if side == "buy" else p * (1 - self.TP_PCT)
        tp_p_s = d_p(tp_price)
        tp_a_s = d_a(-a if side == "buy" else a)
        self.logger().info(f"🎯 追缴止盈: {tp_p_s}")
        payload = OrderedDict([("type", "LIMIT"), ("symbol", self.SYM), ("amount", tp_a_s), ("price", tp_p_s), ("lev", self.LEV)])
        res = await self.api.req("/auth/w/order/submit", payload)
        if isinstance(res, list) and len(res) > 0 and res[0] != "error":
            oid = res[4][0][0]
            self.tracked_orders[oid] = {"side": "sell" if side=="buy" else "buy", "price": tp_price, "amount": a, "is_tp": True}

    def format_status(self) -> str:
        return f"实盘运行中 | 追踪单数: {len(self.tracked_orders)} | 杠杆: {self.LEV}x"

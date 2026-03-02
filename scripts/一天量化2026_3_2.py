
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

# --- 金融级高精度格式化 (杜绝连写) ---
def d_p(val): # 价格: 1位小数
    return str(Decimal(str(val)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))

def d_a(val): # 数量: 5位小数
    return str(Decimal(str(val)).quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))

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
            async with session.post(f"{self.url}{path}", headers=headers, data=body) as r:
                return await r.json()

    async def get_price(self, sym):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.url}/ticker/{sym}") as r:
                data = await r.json()
                return float(data[6]) if isinstance(data, list) and len(data) >= 7 else 0.0

    async def get_bal(self):
        r = await self.req("/auth/r/wallets", {})
        if isinstance(r, list):
            for w in r:
                if len(w) >= 5 and w[0] == "margin" and w[1] in ["USTF0", "UST", "USDt"]:
                    return float(w[4])
        return 0.0

class 一天量化2026_3_2(ScriptStrategyBase):
    markets = {"okx": {"ETH-USDT"}} # 仅用于 Humminbot 心跳，不实盘交易
    
    # --- 用户锁定参数 ---
    SYM = "tETHF0:USTF0"
    LEV = 30
    LEVELS = 4
    SPACING = 0.001 # 0.1%
    TP = 0.002      # 0.2%
    R_L = 1920.0
    R_H = 1999.0

    def __init__(self, connectors):
        super().__init__(connectors)
        k = os.getenv("BITFINEX_API_KEY", "94a54e57696198788682c7e8c4b0d5adab9b69c70fa")
        s = os.getenv("BITFINEX_API_SECRET", "2c15f19dd463e312397d557d54531f63e5961a12da7")
        self.api = BfxRest(k, s, self.logger())
        self.initialized = False
        self.orders = {}

    def on_tick(self):
        if not self.initialized:
            asyncio.ensure_future(self.setup_grid())
            self.initialized = True

    async def setup_grid(self):
        p = await self.api.get_price(self.SYM)
        if p < self.R_L or p > self.R_H:
            self.logger().warning(f"WAIT: Price {p} out of range [{self.R_L}-{self.R_H}]")
            return

        # 1. 清场
        await self.api.req("/auth/w/order/cancel", {"all": 1})
        self.logger().info("CLEAN: All old orders cancelled.")
        
        # 2. 算钱 (核心分配逻辑)
        bal = await self.api.get_bal()
        if bal <= 0: bal = 10.0
        
        # 核心分配：余额 * 杠杆 / (层数 * 2)，确保买卖总合不超标
        # 10U * 30 / 8 = 每张单子挂 37.5U 的货
        unit_val = (bal * self.LEV) / (self.LEVELS * 2)
        self.logger().info(f"START: Bal={bal:.2f}u, Lev={self.LEV}x, Unit_Notional={unit_val:.2f}u")

        for i in range(1, self.LEVELS + 1):
            # 买单
            bp = p * (1 - i * self.SPACING)
            if bp >= self.R_L:
                await self.place_job("buy", bp, unit_val/bp)
            # 卖单
            sp = p * (1 + i * self.SPACING)
            if sp <= self.R_H:
                await self.place_job("sell", sp, unit_val/sp)
            await asyncio.sleep(0.5)

    async def place_job(self, side, price, amount):
        p_s = d_p(price)
        a_s = d_a(amount if side == "buy" else -amount)
        
        payload = OrderedDict([
            ("type", "LIMIT"),
            ("symbol", self.SYM),
            ("amount", a_s),
            ("price", p_s),
            ("lev", self.LEV)
        ])
        
        res = await self.api.req("/auth/w/order/submit", payload)
        
        if isinstance(res, list) and len(res) > 0 and res[0] != "error":
            self.logger().info(f"SUCCESS: {side} {self.SYM} at {p_s}")
            # 追踪 ID ... (省略简化)
        else:
            err = res[2] if isinstance(res, list) and len(res) >= 3 else str(res)
            self.logger().error(f"FAILED: {side} at {p_s} | REASON: {err}")

    def format_status(self) -> str:
        return f"Grid Strategy Running | {self.SYM} | Leverage: {self.LEV}x | Grid: {self.LEVELS}"

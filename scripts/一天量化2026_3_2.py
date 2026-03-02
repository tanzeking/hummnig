
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
from hummingbot.core.data_type.common import PriceType

# ==========================================
# === 🛡️ Bitfinex 原生 API (加固代号版) ===
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
        headers = {
            "bfx-nonce": nonce,
            "bfx-apikey": self.api_key,
            "bfx-signature": sig,
            "content-type": "application/json"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=body) as resp:
                text = await resp.text()
                try:
                    return json.loads(text)
                except:
                    return {"error": text}

    async def get_balance(self) -> float:
        resp = await self.request("/auth/r/wallets")
        if isinstance(resp, list):
            for w in resp:
                # 兼容不同命名：ustf0, ust, usdt
                if len(w) >= 5 and w[0] == "margin" and w[1].upper() in ["USTF0", "UST", "USDT"]:
                    return float(w[4]) if w[4] is not None else 0.0
        return -1.0

    async def create_order(self, symbol, amount, price, lev=30):
        req = {
            "type": "LIMIT",
            "symbol": symbol,
            "amount": "{:.5f}".format(float(amount)),
            "price": "{:.2f}".format(float(price)),
            "lev": int(lev)
        }
        res = await self.request("/auth/w/order/submit", req)
        # 🟢 这里会打印最重要的交易所反馈！
        self.logger.info(f"📤 订单响应: {res}")
        return res

# ==========================================
# === 🚀 动态加固网格策略 (终极版) ===
# ==========================================
class 一天量化2026_3_2(ScriptStrategyBase):
    driver_exchange = "okx"
    driver_pair = "ETH-USDT"
    markets = {driver_exchange: {driver_pair}}
    
    # 💥 改用更标准的 Bitfinex ETH 永续代号
    bfx_symbol = "tETHF0:USTF0"
    
    leverage_long = 30
    leverage_short = 15
    grid_levels = 4
    grid_spacing = 0.001 
    tp_pct = 0.002       
    initialized = False

    def __init__(self, connectors: Dict[str, Any]):
        super().__init__(connectors)
        api_key = "94a54e57696198788682c7e8c4b0d5adab9b69c70fa"
        api_secret = "2c15f19dd463e312397d557d54531f63e5961a12da7"
        self.bfx = BitfinexAPI(api_key, api_secret, self.logger())

    def on_tick(self):
        if not self.initialized:
            asyncio.ensure_future(self.setup_bfx())
            self.initialized = True

    async def setup_bfx(self):
        self.logger().info("🚀 正在验证余额并撒网...")
        real_bal = await self.bfx.get_balance()
        use_bal = real_bal if real_bal > 0 else 10.0
        
        url = f"https://api.bitfinex.com/v2/ticker/{self.bfx_symbol}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                mid_price = float(data[6]) if isinstance(data, list) and len(data) >= 7 else 0.0

        if mid_price <= 0:
            self.logger().error("❌ 未能获取 Bitfinex 价格，停止下单")
            return

        for i in range(1, self.grid_levels + 1):
            # 多头
            b_p = round(mid_price * (1 - i * self.grid_spacing), 2)
            b_a = max(0.005, round((use_bal * 0.5 * self.leverage_long * 0.95) / (self.grid_levels * b_p), 4))
            await self.bfx.create_order(self.bfx_symbol, b_a, b_p, lev=self.leverage_long)

            # 空头
            s_p = round(mid_price * (1 + i * self.grid_spacing), 2)
            s_a = -max(0.005, round((use_bal * 0.5 * self.leverage_short * 0.95) / (self.grid_levels * s_p), 4))
            await self.bfx.create_order(self.bfx_symbol, s_a, s_p, lev=self.leverage_short)

        self.logger().info(f"✅ 撒网完毕！基准价: {mid_price} | 余额: {use_bal}u")

    def format_status(self) -> str:
        return f"运行中 | Bitfinex 原生直连 | 杠杆 多:{self.leverage_long}x 空:{self.leverage_short}x"


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
# === 🛡️ 自研 Bitfinex 增强调试版 API ===
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
                    res_json = json.loads(text)
                    if resp.status != 200:
                        self.logger.error(f"⚠️ API 报错 (状态码 {resp.status}): {text}")
                    return res_json
                except:
                    self.logger.error(f"⚠️ API 返回非 JSON 内容: {text}")
                    return {"error": text}

    async def get_balance(self) -> float:
        resp = await self.request("/auth/r/wallets")
        if isinstance(resp, list):
            for w in resp:
                # Bitfinex 永续合约余额在 'margin' 钱包中，币种通常是 'USTF0' 或 'UST'
                if len(w) >= 5 and w[0] == "margin" and (w[1] == "USTF0" or w[1] == "UST"):
                    return float(w[4]) if w[4] is not None else 0.0
        return -1.0 # 返回 -1 表示抓取失败

    async def get_ticker(self, symbol: str) -> float:
        url = f"{self.base_url}/ticker/{symbol}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                return float(data[6]) if isinstance(data, list) and len(data) >= 7 else 0.0

    async def create_order(self, symbol, amount, price, lev=30):
        # 强制格式化防止科学计数法，Bitfinex 要求正数为买，负数为卖
        req = {
            "type": "LIMIT",
            "symbol": symbol,
            "amount": "{:.5f}".format(float(amount)),
            "price": "{:.2f}".format(float(price)),
            "lev": int(lev),
            "flags": 0 
        }
        res = await self.request("/auth/w/order/submit", req)
        # 打印下单详情以便调试
        self.logger.info(f"📤 下单请求: {symbol} | 量: {req['amount']} | 价: {req['price']} | 响应: {res}")
        return res

# ==========================================
# === 🚀 动态加固网格策略 (超级调试版) ===
# ==========================================
class 一天量化2026_3_2(ScriptStrategyBase):
    driver_exchange = "okx" # 仅作为时钟保持脚本运行
    driver_pair = "ETH-USDT"
    markets = {driver_exchange: {driver_pair}}
    
    # --- 交易目标 (Bitfinex 格式) ---
    bfx_symbol = "tETHF0:USTF0"  # ETH 永续
    
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
        self.logger().info("🔎 正在执行 API 连通性检查...")
        
        # 1. 检查真实余额
        real_bal = await self.bfx.get_balance()
        if real_bal < 0:
            self.logger().error("❌ 无法获取真实余额！请检查 API Key 权限或 secret 是否正确粘帖。")
            use_bal = 10.0 # 强制兜底
        else:
            self.logger().info(f"✅ API 连通成功！真实可用余额: {real_bal:.2f} USTF0")
            use_bal = real_bal
            
        if use_bal < 2.0:
            self.logger().warning("⚠️ 余额过低，可能无法达到起步金额，强制按 10U 规模测试下单...")
            use_bal = 10.0

        # 2. 部署网格
        mid_price = await self.bfx.get_ticker(self.bfx_symbol)
        if mid_price <= 0:
            self.logger().error("❌ 无法获取 Bitfinex 行情数据！")
            return
            
        self.logger().info(f"📊 基准价: {mid_price} | 正在部署 {self.grid_levels} 层网格...")

        for i in range(1, self.grid_levels + 1):
            # 多头 (30x)
            b_price = round(mid_price * (1 - i * self.grid_spacing), 2)
            b_val = (use_bal * 0.5 * self.leverage_long * 0.9) / self.grid_levels
            b_amount = round(b_val / b_price, 4)
            if b_amount < 0.004: b_amount = 0.004 # 强制满足最小起步量
            await self.bfx.create_order(self.bfx_symbol, b_amount, b_price, lev=self.leverage_long)

            # 空头 (15x)
            s_price = round(mid_price * (1 + i * self.grid_spacing), 2)
            s_val = (use_bal * 0.5 * self.leverage_short * 0.9) / self.grid_levels
            s_amount = round(-s_val / s_price, 4)
            if abs(s_amount) < 0.004: s_amount = -0.004
            await self.bfx.create_order(self.bfx_symbol, s_amount, s_price, lev=self.leverage_short)

        self.logger().info("✅ 挂单指令已全部发出，请回交易所确认。")

    def format_status(self) -> str:
        return f"调试模式 | Bitfinex 直连 | 配置: 多30x/空15x"

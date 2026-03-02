
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
# === 🛡️ Bitfinex 增强版 API ===
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
            async with session.post(url, headers=headers, data=body) as resp:
                return await resp.json()

    async def get_balance(self) -> float:
        resp = await self.request("/auth/r/wallets")
        if isinstance(resp, list):
            for w in resp:
                if len(w) >= 5 and w[0] == "margin" and w[1].upper() in ["USTF0", "UST"]:
                    return float(w[4]) if w[4] is not None else 0.0
        return 0.0

    async def get_positions(self):
        return await self.request("/auth/r/positions")

    async def get_open_orders(self, symbol):
        resp = await self.request("/auth/r/orders")
        return [o for o in resp if isinstance(o, list) and len(o) > 3 and o[3] == symbol] if isinstance(resp, list) else []

    async def create_order(self, symbol, amount, price, lev=30, type="LIMIT", post_only=False):
        req = {
            "type": type,
            "symbol": symbol,
            "amount": "{:.5f}".format(float(amount)),
            "price": "{:.2f}".format(float(price)),
            "lev": int(lev)
        }
        if post_only: req["flags"] = 4096 # Post-Only 标志位，防止市价成交
        return await self.request("/auth/w/order/submit", req)

# ==========================================
# === 🚀 超高频动态网格 (带单边风控) ===
# ==========================================
class 一天量化2026_3_2(ScriptStrategyBase):
    markets = {"okx": {"ETH-USDT"}} 
    bfx_symbol = "tETHF0:USTF0"
    
    # --- 💥 超高密度参数 ---
    leverage_long = 30
    leverage_short = 15
    grid_levels = 8         # 每边 8 层，共 16 层
    grid_spacing = 0.0005   # 0.05% 间距
    tp_pct = 0.001          # 0.1% 快速止盈
    
    # --- 🛡️ 风险限制参数 (划重点!) ---
    # 根据你的 10U 余额，单边最大持仓限制为 0.1 ETH (约 200U 货值)
    # 这意味着最多允许持有约 20 笔成交单，之后就不再开新仓，只允许止盈。
    max_pos_amount = 0.15   
    
    last_check_time = 0
    check_interval = 2      # 2秒一次极速扫描

    def __init__(self, connectors: Dict[str, Any]):
        super().__init__(connectors)
        api_key = "94a54e57696198788682c7e8c4b0d5adab9b69c70fa"
        api_secret = "2c15f19dd463e312397d557d54531f63e5961a12da7"
        self.bfx = BitfinexAPI(api_key, api_secret, self.logger())

    def on_tick(self):
        if self.current_timestamp - self.last_check_time >= self.check_interval:
            asyncio.ensure_future(self.maintain_strategy())
            self.last_check_time = self.current_timestamp

    async def maintain_strategy(self):
        # 1. 获取基础数据
        ticker_url = f"https://api.bitfinex.com/v2/ticker/{self.bfx_symbol}"
        async with aiohttp.ClientSession() as session:
            async with session.get(ticker_url) as resp:
                data = await resp.json()
                mid_price = float(data[6]) if isinstance(data, list) and len(data) >= 7 else 0.0
        if mid_price <= 0: return

        balance = await self.bfx.get_balance()
        positions = await self.bfx.get_positions()
        open_orders = await self.bfx.get_open_orders(self.bfx_symbol)

        # 2. 计算当前持仓和净敞口
        current_amount = 0.0
        for pos in positions:
            if pos[0] == self.bfx_symbol:
                current_amount = float(pos[2])

        # 3. 自动止盈逻辑
        # 如果有仓单，检查是否已经有对应的止盈挂单，如果没有则补上
        if abs(current_amount) > 0.0001:
            side = "long" if current_amount > 0 else "short"
            # 止盈单是反向的
            has_tp = any((current_amount > 0 and float(o[6]) < 0) or (current_amount < 0 and float(o[6]) > 0) for o in open_orders)
            if not has_tp:
                tp_price = round(mid_price * (1 + self.tp_pct if side == "long" else 1 - self.tp_pct), 2)
                self.logger().info(f"🎯 自动止盈: {side} 持仓 {current_amount}，目标价 {tp_price}")
                await self.bfx.create_order(self.bfx_symbol, -current_amount, tp_price, lev=(self.leverage_long if side=="long" else self.leverage_short))

        # 4. 动态补网格 (带风控)
        long_orders = [o for o in open_orders if float(o[6]) > 0]
        short_orders = [o for o in open_orders if float(o[6]) < 0]

        # --- 多头补单逻辑 ---
        # 限制：如果当前多头持仓已经达到上限，停止挂新买单
        if current_amount < self.max_pos_amount and len(long_orders) < self.grid_levels:
            self.logger().info(f"♻️ 动态补多 (持仓:{current_amount:.4f})")
            await self.deploy_side("long", mid_price, balance)

        # --- 空头补单逻辑 ---
        # 限制：如果当前空头持仓已经达到上限 (-0.15)，停止挂新卖单
        if current_amount > -self.max_pos_amount and len(short_orders) < self.grid_levels:
            self.logger().info(f"♻️ 动态补空 (持仓:{current_amount:.4f})")
            await self.deploy_side("short", mid_price, balance)

    async def deploy_side(self, side, mid_price, balance):
        use_bal = balance if balance > 2.0 else 10.0
        # 每次只补最靠近盘口的 3 层，实现“动态跟随”
        for i in range(1, 4):
            if side == "long":
                p = round(mid_price * (1 - i * self.grid_spacing), 2)
                a = max(0.005, round((use_bal * 0.4 * self.leverage_long) / (10 * p), 4))
                await self.bfx.create_order(self.bfx_symbol, a, p, lev=self.leverage_long, post_only=True)
            else:
                p = round(mid_price * (1 + i * self.grid_spacing), 2)
                a = -max(0.005, round((use_bal * 0.4 * self.leverage_short) / (10 * p), 4))
                await self.bfx.create_order(self.bfx_symbol, a, p, lev=self.leverage_short, post_only=True)

    def format_status(self) -> str:
        return f"动态风控电梯网格 | 间距:0.05% | 止盈:0.1% | 持仓上限:0.15 ETH"

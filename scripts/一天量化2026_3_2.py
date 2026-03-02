import os
import json
import asyncio
import time
import hmac
import hashlib
import aiohttp
from collections import OrderedDict
from typing import Dict, Any, List, Optional
from decimal import Decimal

from hummingbot.strategy.script_strategy_base import ScriptStrategyBase

# 尝试从 .env 加载环境变量 (适配服务器环境)
env_paths = ["/home/hummingbot/.env", ".env"]
for env_path in env_paths:
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf8") as f:
            for line in f:
                if line.strip() and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip().strip("'\"")

class BitfinexAPI:
    """极轻量 Bitfinex Async REST 客户端"""
    def __init__(self, api_key: str, api_secret: str, logger=None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://api.bitfinex.com/v2"
        self.logger = logger

    async def request(self, endpoint: str, payload_dict: dict = None):
        url = self.base_url + endpoint
        
        # 强制延迟，确保 Nonce 严格递增且不撞车
        await asyncio.sleep(0.1)
        nonce = str(int(time.time() * 1000)) # 官方最推荐的 13 位毫秒 Nonce
        
        # 使用紧凑型 JSON (separators 重要)，且保持传入字典的 key 顺序
        if payload_dict:
            body = json.dumps(payload_dict, separators=(',', ':'))
        else:
            body = "{}"
        
        signature_payload = f"/api/v2{endpoint}{nonce}{body}"
        sig = hmac.new(self.api_secret.encode('utf8'), signature_payload.encode('utf8'), hashlib.sha384).hexdigest()
        
        headers = {
            "bfx-nonce": nonce,
            "bfx-apikey": self.api_key,
            "bfx-signature": sig,
            "content-type": "application/json"
        }
        
        async with aiohttp.ClientSession() as session:
            try:
                # 再次物理间隔保护
                await asyncio.sleep(0.05)
                async with session.post(url, headers=headers, data=body) as resp:
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                        if isinstance(data, list) and len(data) >= 3 and data[0] == "error":
                            return {"error": True, "msg": f"{data[1]}: {data[2]}"}
                        return data
                    except:
                        return {"error": True, "msg": text}
            except Exception as e:
                return {"error": True, "msg": str(e)}

    async def get_ticker(self, symbol: str) -> float:
        url = f"{self.base_url}/ticker/{symbol}"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    data = await resp.json()
                    if isinstance(data, list) and len(data) >= 7:
                        return float(data[6])
                    return 0.0
            except:
                return 0.0

    async def get_available_balance(self) -> float:
        resp = await self.request("/auth/r/wallets")
        if isinstance(resp, list):
            for w in resp:
                if len(w) >= 5 and w[0] == "margin" and w[1] in ["USTF0", "UST", "USDt", "USD"]:
                    available = w[4]
                    if available is not None:
                        return float(available)
        return 0.0

    async def create_order(self, symbol: str, order_type: str, amount: float, price: float = None, lev: int = None, reduce_only: bool = False):
        try:
            # 1. 严格字段排序 (OrderedDict)
            req = OrderedDict()
            req["type"] = order_type.upper()
            req["symbol"] = symbol
            req["amount"] = "{:.5f}".format(float(amount)) # 数量必须是字符串
            
            if price and float(price) > 0:
                price_val = float(price)
                if price_val > 8000: # ETH 熔断保护
                    return {"error": True, "msg": "PRICE_ABNORMAL"}
                req["price"] = "{:.1f}".format(price_val) # 价格必须是字符串且1位小数
            
            if lev:
                req["lev"] = int(lev) # 杠杆必须是整数 (JSON Number)
            if reduce_only:
                req["flags"] = 1024 # 标志必须是整数 (JSON Number)

            resp = await self.request("/auth/w/order/submit", req)
            
            is_success = False
            msg = "未知错误"
            
            if isinstance(resp, list) and len(resp) > 0 and resp[0] != "error":
                is_success = True
            else:
                # 详细捕获错误原因
                msg = resp.get("msg", str(resp)) if isinstance(resp, dict) else str(resp)

            if self.logger:
                side = "🟢 买入" if float(amount) > 0 else "🔴 卖出"
                p_disp = req.get("price", "市价")
                if is_success:
                    self.logger.info(f"{side} 已申报 | 价格: {p_disp} | 数量: {req['amount']}")
                else:
                    self.logger.error(f"❌ 下单失败 | 价格: {p_disp} | 原因: {msg}")
            return resp
        except Exception as e:
            if self.logger: self.logger.error(f"下单模块异常: {e}")
            return {"error": True, "msg": str(e)}

    async def fetch_open_orders(self, symbol: str) -> list:
        resp = await self.request("/auth/r/orders")
        # 必须先确保 resp 是列表，且不是报错列表
        if isinstance(resp, list) and len(resp) > 0 and resp[0] != "error":
            return [o for o in resp if isinstance(o, list) and len(o) > 3 and o[3] == symbol]
        return []

    async def cancel_order(self, order_id: int):
        return await self.request("/auth/w/order/cancel", {"id": int(order_id)})

    async def cancel_all_orders(self, symbol: str = None):
        return await self.request("/auth/w/order/cancel", {"all": 1})

    async def get_positions(self) -> list:
        resp = await self.request("/auth/r/positions")
        if isinstance(resp, list):
            return resp
        return []

    async def close_position(self, symbol: str):
        positions = await self.get_positions()
        for pos in positions:
            if len(pos) >= 3 and pos[0] == symbol:
                amount = float(pos[2])
                if amount != 0:
                    if self.logger:
                        self.logger.info(f"正在全清仓 {symbol}, 仓位大小: {amount}")
                    await self.create_order(symbol, "MARKET", -amount, reduce_only=True)
                    return True
        return False

class 一天量化2026_3_2(ScriptStrategyBase):
    """
    🚀 一天量化2026_3_2 - Bitfinex ETH 极致超高频网格
    """
    # 使用 okx 作为心跳连接器（不实际交易），绕过 bitfinex 未连接报错
    markets = {"okx": {"ETH-USDT"}}  
    
    # --- 策略配置 ---
    bfx_symbol = "tETHF0:USTF0"
    initial_capital = 10.0
    leverage = 35
    grid_levels = 4
    lower_bound = 1920.0
    upper_bound = 1999.0
    
    # ⚡ 超密网格设置
    grid_spacing_pct = 0.1        # 0.1% 开仓间距
    take_profit_pct = 0.2         # 0.2% 止盈点
    total_profit_target = 20.0    # 目标：20u
    stop_loss_margin_pct = 20.0   # 20% 保证金止损
    
    recenter_threshold_pct = 0.2  # 0.2% 偏移即重布网
    check_interval = 8            # 8秒检查一次
    
    # --- 状态变量 ---
    last_check_time = 0
    grid_center_price = 0.0
    grid_initialized = False
    tracked_orders: Dict[int, Dict] = {}
    accumulated_profit = 0.0
    
    def __init__(self, connectors: Dict[str, Any]):
        super().__init__(connectors)
        api_key = os.getenv("BITFINEX_API_KEY", "")
        api_secret = os.getenv("BITFINEX_API_SECRET", "")
        self.bitfinex = BitfinexAPI(api_key, api_secret, logger=self.logger())
        self.tracked_orders = {}
        self.accumulated_profit = 0.0

    def on_tick(self):
        current_time = self.current_timestamp
        if current_time - self.last_check_time >= self.check_interval:
            self.last_check_time = current_time
            asyncio.ensure_future(self._grid_tick())

    async def _grid_tick(self):
        try:
            if self.accumulated_profit >= self.total_profit_target:
                self.logger().info(f"🏆 目标达成! 收益 {self.accumulated_profit:.2f}u. 全清场...")
                await self.on_stop()
                return

            current_price = await self.bitfinex.get_ticker(self.bfx_symbol)
            if current_price <= 0: return
            
            if current_price < self.lower_bound or current_price > self.upper_bound:
                self.logger().warning(f"⚠️ 价格 {current_price} 超出范围，待机中")
                return

            if not self.grid_initialized:
                await self._deploy_full_grid(current_price)
                self.grid_initialized = True
                return

            drift = abs(current_price - self.grid_center_price) / self.grid_center_price * 100
            if drift >= self.recenter_threshold_pct:
                self.logger().info(f"🔄 超高频对齐: 偏移 {drift:.2f}%，重布网...")
                await self._cancel_all_grid_orders()
                await self._deploy_full_grid(current_price)
                return

            await self._check_and_fill_grid()
        except Exception as e:
            self.logger().error(f"运行异常: {e}")

    async def _deploy_full_grid(self, center_price: float):
        self.grid_center_price = center_price
        spacing = self.grid_spacing_pct / 100
        current_equity = self.initial_capital + self.accumulated_profit
        level_notional = (current_equity * self.leverage) / self.grid_levels
        
        self.logger().info(f"📏 超密撒网: 本金={current_equity:.2f}u, 间距={self.grid_spacing_pct}%, 止盈={self.take_profit_pct}%")
        
        for level in range(1, self.grid_levels + 1):
            buy_p = center_price * (1 - level * spacing)
            if buy_p >= self.lower_bound:
                amt = round(level_notional / buy_p, 4)
                # create_order 内部会处理格式化，这里直接传入计算值
                res = await self.bitfinex.create_order(self.bfx_symbol, "LIMIT", amt, buy_p, lev=self.leverage)
                self._track_order(res, buy_p, "buy", amt, is_tp=False)
            
            sell_p = center_price * (1 + level * spacing)
            if sell_p <= self.upper_bound:
                amt = round(level_notional / sell_p, 4)
                res = await self.bitfinex.create_order(self.bfx_symbol, "LIMIT", -amt, sell_p, lev=self.leverage)
                self._track_order(res, sell_p, "sell", amt, is_tp=False)
            await asyncio.sleep(0.2)

    def _track_order(self, api_response, price, side, amount, is_tp):
        if not api_response: return
        try:
            if isinstance(api_response, list) and len(api_response) >= 5:
                order_data = api_response[4]
                if isinstance(order_data, list) and len(order_data) > 0:
                    order = order_data[0] if isinstance(order_data[0], list) else order_data
                    order_id = int(order[0])
                    self.tracked_orders[order_id] = {"price": price, "side": side, "amount": amount, "is_tp": is_tp}
        except Exception as e:
            self.logger().error(f"追踪失败: {e}")

    async def _check_and_fill_grid(self):
        open_orders = await self.bitfinex.fetch_open_orders(self.bfx_symbol)
        open_ids = {int(o[0]) for o in open_orders if len(o) > 0}
        
        filled_orders = []
        for oid, info in list(self.tracked_orders.items()):
            if oid not in open_ids:
                filled_orders.append((oid, info))
                del self.tracked_orders[oid]
        
        for oid, info in filled_orders:
            side, price, amount, is_tp = info["side"], info["price"], info["amount"], info["is_tp"]
            if is_tp:
                pft = (amount * price) * (self.take_profit_pct / 100)
                self.accumulated_profit += pft
                self.logger().info(f"💰 止盈成功! +{pft:.4f}u | 累计: {self.accumulated_profit:.2f}u")
                asyncio.ensure_future(self._relink_grid(side, price, amount))
            else:
                self.logger().info(f"🔔 入场成交: {side} @ {price}, 挂 0.2% 止盈 + 20% 止损")
                await self._place_tp_and_sl(side, price, amount)

    async def _place_tp_and_sl(self, side, price, amount):
        tp_m = (1 + self.take_profit_pct/100) if side == "buy" else (1 - self.take_profit_pct/100)
        tp_p = price * tp_m
        sl_v = (self.stop_loss_margin_pct / self.leverage) / 100
        sl_m = (1 - sl_v) if side == "buy" else (1 + sl_v)
        sl_p = price * sl_m
        
        amt_sign = -amount if side == "buy" else amount
        # create_order 已加固，会强制格式化所有的价格输入
        res_tp = await self.bitfinex.create_order(self.bfx_symbol, "LIMIT", amt_sign, tp_p, lev=self.leverage)
        self._track_order(res_tp, tp_p, "sell" if side == "buy" else "buy", abs(amount), is_tp=True)
        await self.bitfinex.create_order(self.bfx_symbol, "STOP", amt_sign, sl_p, lev=self.leverage, reduce_only=True)

    async def _relink_grid(self, side, price, amount):
         equity = self.initial_capital + self.accumulated_profit
         notional = (equity * self.leverage) / self.grid_levels
         new_amt = round(notional / price, 4)
         if side == "buy":
             p = price * (1 + self.take_profit_pct/100)
             res = await self.bitfinex.create_order(self.bfx_symbol, "LIMIT", -new_amt, p, lev=self.leverage)
             self._track_order(res, p, "sell", new_amt, is_tp=False)
         else:
             p = price * (1 - self.take_profit_pct/100)
             res = await self.bitfinex.create_order(self.bfx_symbol, "LIMIT", new_amt, p, lev=self.leverage)
             self._track_order(res, p, "buy", new_amt, is_tp=False)

    async def _cancel_all_grid_orders(self):
        await self.bitfinex.cancel_all_orders(self.bfx_symbol)
        self.tracked_orders.clear()

    async def on_stop(self):
        self.logger().info("🛑 策略停止，执行清场...")
        await self._cancel_all_grid_orders()
        await self.bitfinex.close_position(self.bfx_symbol)

    def format_status(self) -> str:
        if not self.grid_initialized: return "初始化中..."
        return (
            f"⚡ 极致超高频模式 | {self.bfx_symbol}\n"
            f"💰 当前权益: {self.initial_capital + self.accumulated_profit:.2f}u | 目标: 20u\n"
            f"� 间距: 0.1% | 🎯 止盈: 0.2% | 🛡️ 止损: 20%记\n"
            f"🔢 挂单: {len(self.tracked_orders)}"
        )

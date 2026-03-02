import os
import json
import asyncio
import time
import hmac
import hashlib
import aiohttp
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
            try:
                async with session.post(url, headers=headers, data=body) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        if self.logger:
                            self.logger.error(f"Bitfinex API Error: {resp.status} - {text}")
                    return json.loads(text)
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Request failed: {e}")
                return None

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
                # 寻找 margin 钱包中的 USDT (USTF0)
                if len(w) >= 5 and w[0] == "margin" and w[1] in ["USTF0", "UST", "USDt", "USD"]:
                    available = w[4]
                    if available is not None:
                        return float(available)
        return 0.0

    async def create_order(self, symbol: str, order_type: str, amount: float, price: float = None, lev: int = None, reduce_only: bool = False):
        amount_str = "{:.6f}".format(float(amount))
        req = {
            "type": order_type.upper(),
            "symbol": symbol,
            "amount": amount_str,
        }
        if price:
            req["price"] = "{:.2f}".format(float(price))
        if lev is not None:
            req["lev"] = int(lev)
        
        flags = 0
        if reduce_only:
            flags |= 1024
        if flags > 0:
            req["flags"] = flags

        resp = await self.request("/auth/w/order/submit", req)
        if self.logger:
            side_str = "买" if float(amount) > 0 else "卖"
            self.logger.info(f"[{symbol}下单] {order_type} {side_str} {abs(float(amount)):.6f} @ {price} | 响应: {resp}")
        return resp

    async def fetch_open_orders(self, symbol: str) -> list:
        resp = await self.request("/auth/r/orders")
        if isinstance(resp, list):
            return [o for o in resp if len(o) > 3 and o[3] == symbol]
        return []

    async def cancel_order(self, order_id: int):
        return await self.request("/auth/w/order/cancel", {"id": int(order_id)})

    async def cancel_all_orders(self, symbol: str = None):
        # Bitfinex cancel all orders
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
                    # 提交反向市价单平仓
                    await self.create_order(symbol, "MARKET", -amount, reduce_only=True)
                    return True
        return False

class 一天量化2026_3_2(ScriptStrategyBase):
    """
    🚀 一天量化2026_3_2 - Bitfinex ETH 动态网格策略
    
    参数:
    - 交易所: Bitfinex (0手续费)
    - 本金: 10 USDT
    - 杠杆: 35倍
    - 网格: 上下各 4 格
    - 止损: 不止损
    - 清仓: 停止策略时全清仓
    - 动态中心价: 自动跟随市场波动
    - 交易对: ETH/USDT (tETHF0:USTF0)
    - 价格区间: 1920 - 1999 USDT
    """
    
    # Hummingbot 必须声明 markets，虽然我们用 custom BitfinexAPI
    markets = {"binance": {"ETH-USDT"}}  # 占位符
    
    # --- 策略配置 ---
    bfx_symbol = "tETHF0:USTF0"
    initial_capital = 10.0   # 初始本金 10u
    leverage = 35            # 35倍杠杆
    grid_levels = 4          # 上下各4格
    lower_bound = 1920.0     # 下线价
    upper_bound = 1999.0     # 上线价
    
    # 止盈设置
    grid_spacing_pct = 0.3        # 每一格的间距
    total_profit_target = 20.0    # 🎯 目标：赚够 20u 停止脚本
    
    # 止损设置
    stop_loss_margin_pct = 20.0   # ❌ 单仓保证金亏损 20% 止损
    
    # 动态调整参数
    recenter_threshold_pct = 0.5  # 价格偏离中心 0.5% 时重新布网
    check_interval = 10           # 检查频率 (秒)
    
    # --- 状态变量 ---
    last_check_time = 0
    grid_center_price = 0.0
    grid_initialized = False
    tracked_orders: Dict[int, Dict] = {}
    accumulated_profit = 0.0      # 累计已实现利润 (USDT)
    
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
            # 0. 检查总利润目标：挣到 20 USDT 停止
            if self.accumulated_profit >= self.total_profit_target:
                self.logger().info(f"🏆🏆🏆 大功告成! 累计利润 {self.accumulated_profit:.2f}u 已达目标(20u)。正在全清仓落袋为安...")
                await self.on_stop()
                return

            # 1. 获取最新价格
            current_price = await self.bitfinex.get_ticker(self.bfx_symbol)
            if current_price <= 0: return
            
            # 2. 价格区间保护
            if current_price < self.lower_bound or current_price > self.upper_bound:
                self.logger().warning(f"⚠️ 价格 {current_price} 超出区间 [{self.lower_bound}-{1999}]，暂停网格")
                return

            # 3. 初始化/重置中心 (包含滚仓逻辑：每次重布网都会根据最新利润放大仓位)
            if not self.grid_initialized:
                await self._deploy_full_grid(current_price)
                self.grid_initialized = True
                return

            drift_pct = abs(current_price - self.grid_center_price) / self.grid_center_price * 100
            if drift_pct >= self.recenter_threshold_pct:
                self.logger().info(f"🔄 价格偏移 {drift_pct:.2f}%，重新布网并执行滚仓...")
                await self._cancel_all_grid_orders()
                await self._deploy_full_grid(current_price)
                return

            # 4. 检查并补单
            await self._check_and_fill_grid()

        except Exception as e:
            self.logger().error(f"运行异常: {e}")

    async def _deploy_full_grid(self, center_price: float):
        """部署网格：核心包含【无限滚仓】逻辑"""
        self.grid_center_price = center_price
        spacing = self.grid_spacing_pct / 100
        
        # 🚀 无限滚仓公式：当前总资金 = 初始10u + 累计利润
        current_equity = self.initial_capital + self.accumulated_profit
        # 每层名义价值 = 总资金 * 杠杆 / 网格层数
        level_notional = (current_equity * self.leverage) / self.grid_levels
        
        self.logger().info(f"� 滚仓布网: 当前战斗本金={current_equity:.2f}u, 每层规模={level_notional:.2f}u")
        
        for level in range(1, self.grid_levels + 1):
            # 下方买单
            buy_p = round(center_price * (1 - level * spacing), 2)
            if buy_p >= self.lower_bound:
                amt = round(level_notional / buy_p, 4)
                res = await self.bitfinex.create_order(self.bfx_symbol, "LIMIT", amt, buy_p, lev=self.leverage)
                self._track_order(res, buy_p, "buy", amt, is_tp=False)
            
            # 上方卖单
            sell_p = round(center_price * (1 + level * spacing), 2)
            if sell_p <= self.upper_bound:
                amt = round(level_notional / sell_p, 4)
                res = await self.bitfinex.create_order(self.bfx_symbol, "LIMIT", -amt, sell_p, lev=self.leverage)
                self._track_order(res, sell_p, "sell", amt, is_tp=False)
            await asyncio.sleep(0.3) 

    def _track_order(self, api_response, price, side, amount, is_tp):
        if not api_response: return
        try:
            if isinstance(api_response, list) and len(api_response) >= 5:
                order_data = api_response[4]
                if isinstance(order_data, list) and len(order_data) > 0:
                    order = order_data[0] if isinstance(order_data[0], list) else order_data
                    order_id = int(order[0])
                    self.tracked_orders[order_id] = {
                        "price": price, "side": side, "amount": amount, "is_tp": is_tp
                    }
        except Exception as e:
            self.logger().error(f"解析订单失败: {e}")

    async def _check_and_fill_grid(self):
        """核心逻辑：入场后挂【止盈单】和【20%止损单】"""
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
                # 💰 止盈落袋
                profit = (amount * price) * (self.grid_spacing_pct / 100)
                self.accumulated_profit += profit
                self.logger().info(f"💰 滚仓盈利! +{profit:.4f}u | 累计总利润: {self.accumulated_profit:.2f}u")
                # 止盈后，利用最新本金在原位重挂（复利滚仓）
                asyncio.ensure_future(self._relink_grid(side, price, amount))
            else:
                # 🔔 入场成交 -> 挂止盈 + 挂 20% 止损
                await self._place_tp_and_sl(side, price, amount)

    async def _place_tp_and_sl(self, entry_side, entry_price, amount):
        """下单 止盈(LIMIT) 和 止损(STOP)"""
        # 1. 止盈价 (+0.3%)
        tp_mult = (1 + self.grid_spacing_pct/100) if entry_side == "buy" else (1 - self.grid_spacing_pct/100)
        tp_p = round(entry_price * tp_mult, 2)
        
        # 2. 止损价 (保证金 20% 止损，换算价格波动 = 20% / 35倍杠杆 ≈ 0.57%)
        sl_volatility = (self.stop_loss_margin_pct / self.leverage) / 100
        sl_mult = (1 - sl_volatility) if entry_side == "buy" else (1 + sl_volatility)
        sl_p = round(entry_price * sl_mult, 2)
        
        # 挂止盈单 (LIMIT)
        tp_side_amt = -amount if entry_side == "buy" else amount
        res_tp = await self.bitfinex.create_order(self.bfx_symbol, "LIMIT", tp_side_amt, tp_p, lev=self.leverage)
        self._track_order(res_tp, tp_p, "sell" if entry_side == "buy" else "buy", abs(amount), is_tp=True)
        
        # 挂止损单 (STOP + ReduceOnly)
        self.logger().info(f"🛡️ 开启单仓保护: 入场价 {entry_price} | 止损价 {sl_p} (预计亏损20%保证金)")
        await self.bitfinex.create_order(self.bfx_symbol, "STOP", tp_side_amt, sl_p, lev=self.leverage, reduce_only=True)

    async def _relink_grid(self, side, price, amount):
         # 根据滚仓后的本金重新计算下单量，补回原层级
         current_equity = self.initial_capital + self.accumulated_profit
         level_notional = (current_equity * self.leverage) / self.grid_levels
         new_amt = round(level_notional / price, 4)
         
         if side == "buy": # 平空后补卖单
             p = round(price * (1 + self.grid_spacing_pct/100), 2)
             res = await self.bitfinex.create_order(self.bfx_symbol, "LIMIT", -new_amt, p, lev=self.leverage)
             self._track_order(res, p, "sell", new_amt, is_tp=False)
         else: # 平多后补买单
             p = round(price * (1 - self.grid_spacing_pct/100), 2)
             res = await self.bitfinex.create_order(self.bfx_symbol, "LIMIT", new_amt, p, lev=self.leverage)
             self._track_order(res, p, "buy", new_amt, is_tp=False)

    async def _cancel_all_grid_orders(self):
        await self.bitfinex.cancel_all_orders(self.bfx_symbol)
        self.tracked_orders.clear()

    async def on_stop(self):
        self.logger().info("🛑 策略停止，清场...")
        await self._cancel_all_grid_orders()
        await self.bitfinex.close_position(self.bfx_symbol)

    def format_status(self) -> str:
        if not self.grid_initialized: return "初始化中..."
        return (
            f"🔄 无限滚仓模式 | {self.bfx_symbol}\n"
            f"💰 当前总权益: {self.initial_capital + self.accumulated_profit:.2f}u (初始 10u)\n"
            f"📈 已实现盈利: {self.accumulated_profit:.4f}u / 🎯 目标 20u\n"
            f"🛡️ 保护机制: 20% 单仓止损 | 止盈清仓自停\n"
            f"🔢 活跃网格单: {len(self.tracked_orders)}"
        )

    async def _cancel_all_grid_orders(self):
        await self.bitfinex.cancel_all_orders(self.bfx_symbol)
        self.tracked_orders.clear()
        self.logger().info("已撤销所有网格订单")

    async def on_stop(self):
        """策略停止时的回调: 撤单 + 全清仓"""
        self.logger().info("🛑 策略停止中，执行全清仓操作...")
        await self._cancel_all_grid_orders()
        success = await self.bitfinex.close_position(self.bfx_symbol)
        if success:
            self.logger().info("✅ 所有仓位已清空")
        else:
            self.logger().info("ℹ️ 未发现可平仓位或清理完成")

    def format_status(self) -> str:
        if not self.grid_initialized: return "正在等待价格初始化..."
        active = len(self.tracked_orders)
        return (
            f"📈 一天量化2026_3_2 | {self.bfx_symbol}\n"
            f"💰 本金: {self.capital}u | 杠杆: {self.leverage}x | 区间: [{self.lower_bound} - {self.upper_bound}]\n"
            f"🎯 当前网格中心: {self.grid_center_price} | 活跃挂单: {active}\n"
            f"不止损 | 停止时自动全平仓"
        )

import os
import json
import asyncio
import time
import hmac
import hashlib
import aiohttp
from typing import Dict, Any, List, Optional

from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.data_feed.candles_feed.candles_factory import CandlesConfig, CandlesFactory

# 手动加载 .env
env_path = "/home/hummingbot/.env"
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf8") as f:
        for line in f:
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip("'\"")


class BitfinexAPI:
    """极轻量 Bitfinex Async REST 客户端"""
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://api.bitfinex.com/v2"

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
                try:
                    return json.loads(await resp.text())
                except:
                    return None

    async def get_ticker(self, symbol: str) -> float:
        url = f"{self.base_url}/ticker/{symbol}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                if isinstance(data, list) and len(data) >= 7:
                    return float(data[6])
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

    async def create_order(self, symbol, order_type, amount, price=None, lev=None, reduce_only=False):
        amount_str = "{:.6f}".format(float(amount))
        req = {
            "type": order_type.upper(),
            "symbol": symbol,
            "amount": amount_str,
        }
        if price:
            req["price"] = "{:.1f}".format(float(price))
        if lev is not None:
            req["lev"] = int(lev)
        flags = 0
        if reduce_only:
            flags |= 1024
        if flags > 0:
            req["flags"] = flags

        resp = await self.request("/auth/w/order/submit", req)
        print(f"[{symbol}下单] {order_type} {'买' if float(amount) > 0 else '卖'} {abs(float(amount)):.6f} @ {price} => {resp}")
        return resp

    async def fetch_open_orders(self, symbol) -> list:
        resp = await self.request("/auth/r/orders")
        if isinstance(resp, list):
            return [o for o in resp if len(o) > 3 and o[3] == symbol]
        return []

    async def cancel_order(self, order_id):
        return await self.request("/auth/w/order/cancel", {"id": int(order_id)})

    async def cancel_all_orders(self):
        return await self.request("/auth/w/order/cancel", {"all": 1})

    async def get_positions(self) -> list:
        resp = await self.request("/auth/r/positions")
        if isinstance(resp, list):
            return resp
        return []


class GridMarketMaker(ScriptStrategyBase):
    """
    ⚡ Bitfinex 永续合约网格做市策略
    
    在当前价格的上方和下方以固定间距 (0.12%) 挂满买卖限价单。
    当价格震荡成交后，只在盈利方向平仓，绝不止损。
    不需要 AI 判断方向 —— 纯数学震荡吃差价。
    """
    
    # OKX 仅用于提供 Hummingbot 框架所需的 markets 声明（不实际交易）
    data_exchange = "okx"
    trading_pair = "BTC-USDT"
    markets = {data_exchange: {trading_pair}}

    # ==========================================
    # === 🚀 网格做市核心参数 ===
    # ==========================================
    
    # 💥 网格间距百分比 (0.12 = 每 0.12% 放一层挂单)
    grid_spacing_pct = 0.12
    
    # 💥 上下各布多少层网格 (10层 = 上方10层空单 + 下方10层多单 = 共20张挂单)
    grid_levels = 10
    
    # 💥 每层网格的名义价值 (USDT，每层挂 $100 的仓位)
    order_notional = 100
    
    # 💥 杠杆 (15 倍)
    leverage = 15
    
    # 💥 网格检查刷新间隔 (秒)
    check_interval = 15
    
    # 💥 最大单边净持仓 (BTC)，防止趋势行情下仓位无限堆积
    max_net_position = 0.05
    
    # 💥 网格中心偏移多少百分比后重新布网 (例如: 价格偏离中心 2% 就重新以当前价为中心重铺)
    recenter_threshold_pct = 2.0

    # ==========================================
    # === 底层状态变量 ===
    # ==========================================
    
    bfx_symbol = "tBTCF0:USTF0"
    last_check_time = 0
    grid_center_price = 0.0
    grid_initialized = False
    
    # 追踪我们挂出去的网格订单: {order_id: {"price", "side", "amount", "is_tp"}}
    tracked_orders: Dict[int, Dict] = {}
    
    # 统计
    total_trades = 0
    total_profit = 0.0

    def __init__(self, connectors: Dict[str, Any]):
        super().__init__(connectors)
        api_key = os.getenv("BITFINEX_API_KEY", "")
        api_secret = os.getenv("BITFINEX_API_SECRET", "")
        self.bitfinex = BitfinexAPI(api_key, api_secret)
        self.tracked_orders = {}
        
        asyncio.ensure_future(self._init_grid())

    async def _init_grid(self):
        """开机自检"""
        try:
            bal = await self.bitfinex.get_available_balance()
            price = await self.bitfinex.get_ticker(self.bfx_symbol)
            
            required = self.grid_levels * 2 * (self.order_notional / self.leverage)
            
            self.logger().info(f"╔══════════════════════════════════════════╗")
            self.logger().info(f"║  ⚡ 网格做市策略启动                      ║")
            self.logger().info(f"╠══════════════════════════════════════════╣")
            self.logger().info(f"║  💰 衍生品余额: {bal:.2f} USDT")
            self.logger().info(f"║  📊 BTC 当前价: ${price:,.2f}")
            self.logger().info(f"║  📐 网格间距: {self.grid_spacing_pct}%")
            self.logger().info(f"║  📶 网格层数: 上{self.grid_levels}层 + 下{self.grid_levels}层 = {self.grid_levels * 2}张挂单")
            self.logger().info(f"║  💵 每层名义: ${self.order_notional}")
            self.logger().info(f"║  🔧 杠杆: {self.leverage}x")
            self.logger().info(f"║  💳 预估需要保证金: ${required:.2f} USDT")
            self.logger().info(f"╚══════════════════════════════════════════╝")
            
            if bal < required * 0.8:
                self.logger().warning(f"⚠️ 余额不足! 需要约 ${required:.2f}，但只有 ${bal:.2f}")
                
        except Exception as e:
            self.logger().error(f"开机自检失败: {e}")

    def on_tick(self):
        current_time = self.current_timestamp
        if current_time - self.last_check_time >= self.check_interval:
            self.last_check_time = current_time
            asyncio.ensure_future(self._grid_tick())

    async def _grid_tick(self):
        """网格主循环"""
        try:
            # 1. 获取 Bitfinex 实时价格
            current_price = await self.bitfinex.get_ticker(self.bfx_symbol)
            if current_price <= 0:
                return
            
            # 2. 首次运行: 以当前价格为中心部署网格
            if not self.grid_initialized:
                self.grid_center_price = current_price
                self.logger().info(f"🏗️ 首次部署网格，中心价格: ${current_price:,.2f}")
                await self._deploy_full_grid(current_price)
                self.grid_initialized = True
                return
            
            # 3. 检查价格是否偏离中心太远，需要重新布网
            drift_pct = abs(current_price - self.grid_center_price) / self.grid_center_price * 100
            if drift_pct >= self.recenter_threshold_pct:
                self.logger().info(f"🔄 价格偏离中心 {drift_pct:.2f}% >= {self.recenter_threshold_pct}%，重新布网！")
                await self._cancel_all_grid_orders()
                self.grid_center_price = current_price
                await self._deploy_full_grid(current_price)
                return
            
            # 4. 检查哪些订单已经成交
            await self._check_and_fill_grid()
            
        except Exception as e:
            self.logger().error(f"网格主循环异常: {e}")

    async def _deploy_full_grid(self, center_price: float):
        """在中心价格上下部署完整的网格挂单"""
        spacing_mult = self.grid_spacing_pct / 100
        
        self.logger().info(f"📐 开始布网: 中心=${center_price:,.2f}, 间距={self.grid_spacing_pct}%, 层数=上下各{self.grid_levels}层")
        
        for level in range(1, self.grid_levels + 1):
            # === 下方买单 (做多入场) ===
            buy_price = round(center_price * (1 - level * spacing_mult), 1)
            buy_amount = round(self.order_notional / buy_price, 6)
            
            res = await self.bitfinex.create_order(
                symbol=self.bfx_symbol,
                order_type="LIMIT",
                amount=buy_amount,   # 正数 = 买入
                price=buy_price,
                lev=self.leverage
            )
            self._track_order(res, buy_price, "buy", buy_amount, is_tp=False)
            
            # === 上方卖单 (做空入场) ===
            sell_price = round(center_price * (1 + level * spacing_mult), 1)
            sell_amount = round(self.order_notional / sell_price, 6)
            
            res = await self.bitfinex.create_order(
                symbol=self.bfx_symbol,
                order_type="LIMIT",
                amount=-sell_amount,  # 负数 = 卖出
                price=sell_price,
                lev=self.leverage
            )
            self._track_order(res, sell_price, "sell", sell_amount, is_tp=False)
            
            # 每挂2单停顿一下，避免触发 API 频率限制
            await asyncio.sleep(0.2)
        
        self.logger().info(f"✅ 网格部署完毕! 共挂出 {self.grid_levels * 2} 张限价单")
        
        # 打印网格全景
        buy_low = round(center_price * (1 - self.grid_levels * spacing_mult), 1)
        sell_high = round(center_price * (1 + self.grid_levels * spacing_mult), 1)
        self.logger().info(f"📊 网格范围: ${buy_low:,.1f} ← 买 ══ ${center_price:,.1f} ══ 卖 → ${sell_high:,.1f}")

    def _track_order(self, api_response, price, side, amount, is_tp):
        """从 API 返回值中提取订单 ID 并记录"""
        try:
            if isinstance(api_response, list) and len(api_response) >= 5:
                # Bitfinex 返回格式: [MTS, TYPE, MSG_ID, None, [ORDER_ARRAY], None, STATUS, TEXT]
                order_data = api_response[4]
                if isinstance(order_data, list) and len(order_data) > 0:
                    # order_data 可能是嵌套数组
                    order = order_data[0] if isinstance(order_data[0], list) else order_data
                    order_id = int(order[0])
                    self.tracked_orders[order_id] = {
                        "price": price,
                        "side": side,
                        "amount": amount,
                        "is_tp": is_tp
                    }
                    return
        except Exception as e:
            self.logger().warning(f"追踪订单ID失败: {e}")

    async def _check_and_fill_grid(self):
        """检查哪些网格订单已成交，为已成交的挂出反向止盈单"""
        try:
            # 从交易所获取当前所有未成交的挂单
            open_orders = await self.bitfinex.fetch_open_orders(self.bfx_symbol)
            open_ids = set()
            for o in open_orders:
                if len(o) > 0:
                    open_ids.add(int(o[0]))
            
            # 找出已经不在挂单列表中的订单 = 已成交
            filled_orders = []
            for oid, info in list(self.tracked_orders.items()):
                if oid not in open_ids:
                    filled_orders.append((oid, info))
                    del self.tracked_orders[oid]
            
            if not filled_orders:
                return
            
            for oid, info in filled_orders:
                side = info["side"]
                price = info["price"]
                amount = info["amount"]
                is_tp = info["is_tp"]
                
                if is_tp:
                    # 止盈单成交了! 一笔网格利润落袋!
                    profit = amount * price * self.grid_spacing_pct / 100
                    self.total_trades += 1
                    self.total_profit += profit
                    self.logger().info(f"💰 止盈成交! {'空→买' if side == 'buy' else '多→卖'} @ ${price:,.1f} | 本笔利润: ${profit:.4f} | 累计: ${self.total_profit:.4f} ({self.total_trades}笔)")
                    
                    # 止盈成交后，在原位重新挂入场单（重建网格层）
                    if side == "buy":
                        # 空头止盈成交(买回) → 在高一层重新挂卖单(重新做空入场)
                        new_price = round(price * (1 + self.grid_spacing_pct / 100), 1)
                        res = await self.bitfinex.create_order(
                            symbol=self.bfx_symbol,
                            order_type="LIMIT",
                            amount=-amount,
                            price=new_price,
                            lev=self.leverage
                        )
                        self._track_order(res, new_price, "sell", amount, is_tp=False)
                    else:
                        # 多头止盈成交(卖出) → 在低一层重新挂买单(重新做多入场)
                        new_price = round(price * (1 - self.grid_spacing_pct / 100), 1)
                        res = await self.bitfinex.create_order(
                            symbol=self.bfx_symbol,
                            order_type="LIMIT",
                            amount=amount,
                            price=new_price,
                            lev=self.leverage
                        )
                        self._track_order(res, new_price, "buy", amount, is_tp=False)
                else:
                    # 入场单成交了! 挂出对应的止盈单
                    if side == "buy":
                        # 买入成交(做多) → 在高 0.12% 处挂卖出止盈
                        tp_price = round(price * (1 + self.grid_spacing_pct / 100), 1)
                        self.logger().info(f"📗 多单成交 @ ${price:,.1f} → 挂止盈卖单 @ ${tp_price:,.1f}")
                        res = await self.bitfinex.create_order(
                            symbol=self.bfx_symbol,
                            order_type="LIMIT",
                            amount=-amount,
                            price=tp_price,
                            lev=self.leverage
                        )
                        self._track_order(res, tp_price, "sell", amount, is_tp=True)
                    else:
                        # 卖出成交(做空) → 在低 0.12% 处挂买入止盈
                        tp_price = round(price * (1 - self.grid_spacing_pct / 100), 1)
                        self.logger().info(f"📕 空单成交 @ ${price:,.1f} → 挂止盈买单 @ ${tp_price:,.1f}")
                        res = await self.bitfinex.create_order(
                            symbol=self.bfx_symbol,
                            order_type="LIMIT",
                            amount=amount,
                            price=tp_price,
                            lev=self.leverage
                        )
                        self._track_order(res, tp_price, "buy", amount, is_tp=True)
                    
        except Exception as e:
            self.logger().error(f"网格检查异常: {e}")

    async def _cancel_all_grid_orders(self):
        """撤销所有网格挂单"""
        try:
            for oid in list(self.tracked_orders.keys()):
                await self.bitfinex.cancel_order(oid)
                await asyncio.sleep(0.1)
            self.tracked_orders.clear()
            self.logger().info("🧹 已撤销所有网格挂单")
        except Exception as e:
            self.logger().error(f"撤单异常: {e}")

    def format_status(self) -> str:
        active = len(self.tracked_orders)
        tp_count = sum(1 for v in self.tracked_orders.values() if v.get("is_tp"))
        entry_count = active - tp_count
        return (
            f"⚡ 网格做市策略运行中 | Bitfinex tBTCF0:USTF0\n"
            f"📐 间距: {self.grid_spacing_pct}% | 🔧 杠杆: {self.leverage}x | 📶 层数: {self.grid_levels}\n"
            f"📋 活跃挂单: {active} (入场单: {entry_count} | 止盈单: {tp_count})\n"
            f"💰 累计成交: {self.total_trades} 笔 | 累计利润: ${self.total_profit:.4f}\n"
            f"🎯 网格中心: ${self.grid_center_price:,.2f}"
        )

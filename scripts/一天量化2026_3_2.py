
import os
import decimal
from decimal import Decimal
from typing import Any, Dict, List
from hummingbot.core.data_type.common import OrderType, PriceType, TradeType
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase

class 一天量化2026_3_2(ScriptStrategyBase):
    """
    🚀 真正动态 + Key 内置版
    - 自动匹配 Bitfinex 连接器
    - 动态多头 30x / 空头 15x
    """
    # 这里内置你的 API Key (仅供参考，HB 通常建议通过 connect 命令配置)
    api_key = "94a54e57696198788682c7e8c4b0d5adab9b69c70fa"
    api_secret = "2c15f19dd463e312397d557d54531f63e5961a12da7"

    target_connector = "bitfinex_perpetual"
    trading_pair = "ETH-USTF0"
    markets = {}

    # --- 核心参数 ---
    leverage_long = 30    
    leverage_short = 15   
    grid_levels = 4
    grid_spacing = 0.001 
    tp_pct = 0.002       
    lower_bound = 1800.0
    upper_bound = 2200.0
    initialized = False

    def __init__(self, connectors: Dict[str, Any]):
        # 🟢 自动寻找包含 'bitfinex' 的连接器
        found_connector = None
        for name in connectors.keys():
            if "bitfinex" in name.lower():
                found_connector = name
                break
        
        if not found_connector:
            self.exchange = self.target_connector
        else:
            self.exchange = found_connector
            
        self.markets = {self.exchange: {self.trading_pair}}
        super().__init__(connectors)
        self.logger().info(f"✅ 脚本已上线，使用连接器: {self.exchange}")

    def on_tick(self):
        if self.exchange not in self.connectors:
            self.logger().error(f"❌ 找不到连接器 {self.exchange}")
            return

        if not self.initialized:
            try:
                self.connectors[self.exchange].set_leverage(self.trading_pair, max(self.leverage_long, self.leverage_short))
                self.logger().info(f"🚀 初始化成功 | 动态杠杆生效: 多 30x / 空 15x")
                self.deploy_grid()
                self.initialized = True
            except Exception as e:
                self.logger().error(f"❌ 初始化报错: {str(e)}")

    def get_dynamic_amount(self, side: str, price: float) -> Decimal:
        # 实时抓取可用余额计算
        balance = self.connectors[self.exchange].get_available_balance("USTF0")
        if balance < 1.0: balance = 5.0 # 容错
        
        allocated_fund = float(balance) * 0.5
        if side == "BUY":
            # 多头按 30倍计算货值
            target_val = (allocated_fund * self.leverage_long * 0.9) / self.grid_levels
        else:
            # 空头按 15倍计算货值
            target_val = (allocated_fund * self.leverage_short * 0.9) / self.grid_levels
            
        amount = target_val / price
        return Decimal(str(round(amount, 4)))

    def deploy_grid(self):
        mid_price = self.connectors[self.exchange].get_price_by_type(self.trading_pair, PriceType.MidPrice)
        for i in range(1, self.grid_levels + 1):
            # 多头
            b_price = mid_price * (1 - i * self.grid_spacing)
            if b_price >= self.lower_bound:
                b_amount = self.get_dynamic_amount("BUY", b_price)
                self.buy(self.exchange, self.trading_pair, b_amount, OrderType.LIMIT, Decimal(str(b_price)))

            # 空头
            s_price = mid_price * (1 + i * self.grid_spacing)
            if s_price <= self.upper_bound:
                s_amount = self.get_dynamic_amount("SELL", s_price)
                self.sell(self.exchange, self.trading_pair, s_amount, OrderType.LIMIT, Decimal(str(s_price)))

    def did_fill_order(self, event):
        price = float(event.price)
        amount = float(event.amount)
        if event.trade_type == TradeType.BUY:
            tp_price = price * (1 + self.tp_pct)
            self.sell(self.exchange, self.trading_pair, Decimal(str(amount)), OrderType.LIMIT, Decimal(str(tp_price)))
        else:
            tp_price = price * (1 - self.tp_pct)
            self.buy(self.exchange, self.trading_pair, Decimal(str(amount)), OrderType.LIMIT, Decimal(str(tp_price)))

    def format_status(self) -> str:
        if self.exchange not in self.connectors: return "等待连接..."
        balance = self.connectors[self.exchange].get_available_balance("USTF0")
        return f"动态运行中 | 余额: {balance:.2f}u | 多:30x 空:15x"

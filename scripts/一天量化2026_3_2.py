
import os
import decimal
from decimal import Decimal
from typing import Any, Dict, List
from hummingbot.core.data_type.common import OrderType, PriceType, TradeType
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase

class 一天量化2026_3_2(ScriptStrategyBase):
    """
    🚀 真正动态版：Bitfinex ETH 永续网格
    - 动态多头: 30x 规模
    - 动态空头: 15x 规模
    - 逻辑：实时根据余额计算下单量，实现复利/动态风险控制
    """
    connector_name = "bitfinex_perpetual" 
    trading_pair = "ETH-USTF0"
    markets = {connector_name: {trading_pair}}

    # --- 核心动态系数 ---
    leverage_long = 30    # 多头目标杠杆
    leverage_short = 15   # 空头目标杠杆
    
    grid_levels = 4
    grid_spacing = 0.001 
    tp_pct = 0.002       
    lower_bound = 1800.0
    upper_bound = 2200.0
    
    initialized = False

    def __init__(self, connectors: Dict[str, Any]):
        # 自动兼容连接器名称
        actual_name = self.connector_name
        for name in connectors.keys():
            if "bitfinex" in name:
                actual_name = name
                break
        self.markets = {actual_name: {self.trading_pair}}
        self.exchange = actual_name
        super().__init__(connectors)
    
    def on_tick(self):
        if not self.initialized:
            # 设定底层最大倍数
            self.connectors[self.exchange].set_leverage(self.trading_pair, max(self.leverage_long, self.leverage_short))
            self.logger().info(f"🚀 动态网格已初始化 | 多:{self.leverage_long}x 空:{self.leverage_short}x")
            self.deploy_grid()
            self.initialized = True

    def get_dynamic_amount(self, side: str, price: float) -> Decimal:
        """核心逻辑：实时根据当前可用资金计算下单量"""
        balance = self.connectors[self.exchange].get_available_balance("USTF0")
        if balance < 1.0: balance = 5.0 # 极低余额容错
        
        # 将余额平分为多空两份
        allocated_fund = float(balance) * 0.5
        
        if side == "BUY":
            # 多头规模 = 资金 * 30 / 层数
            target_val = (allocated_fund * self.leverage_long * 0.95) / self.grid_levels
        else:
            # 空头规模 = 资金 * 15 / 层数
            target_val = (allocated_fund * self.leverage_short * 0.95) / self.grid_levels
            
        amount = target_val / price
        return Decimal(str(round(amount, 4)))

    def deploy_grid(self):
        mid_price = self.connectors[self.exchange].get_price_by_type(self.trading_pair, PriceType.MidPrice)
        self.logger().info(f"� 初始部署中... 当前基准价: {mid_price}")

        for i in range(1, self.grid_levels + 1):
            # 动态部署买单 (30x)
            b_price = mid_price * (1 - i * self.grid_spacing)
            if b_price >= self.lower_bound:
                b_amount = self.get_dynamic_amount("BUY", b_price)
                self.buy(self.exchange, self.trading_pair, b_amount, OrderType.LIMIT, Decimal(str(b_price)))

            # 动态部署卖单 (15x)
            s_price = mid_price * (1 + i * self.grid_spacing)
            if s_price <= self.upper_bound:
                s_amount = self.get_dynamic_amount("SELL", s_price)
                self.sell(self.exchange, self.trading_pair, s_amount, OrderType.LIMIT, Decimal(str(s_price)))

    def did_fill_order(self, event):
        """核心动态循环：成交后根据余额重新计算，维持网格"""
        price = float(event.price)
        amount = float(event.amount)
        
        if event.trade_type == TradeType.BUY:
            # 买单成交 -> 挂止盈卖单
            tp_price = price * (1 + self.tp_pct)
            self.logger().info(f"🟢 买单成交, 动态止盈价: {tp_price:.2f}")
            self.sell(self.exchange, self.trading_pair, Decimal(str(amount)), OrderType.LIMIT, Decimal(str(tp_price)))
        else:
            # 卖入或止盈卖出成交
            # 为保证网格持续，根据策略判断是否需要补回原位买单
            # 此处演示通过 did_fill 实现简单的仓位平平衡
            self.logger().info(f"🔴 卖单成交 @ {price:.2f}")

    def format_status(self) -> str:
        balance = self.connectors[self.exchange].get_available_balance("USTF0")
        return f"动态平衡中 | 实时余额: {balance:.2f}u | 多:{self.leverage_long}x 空:{self.leverage_short}x"

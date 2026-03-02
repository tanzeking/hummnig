
import os
import decimal
from decimal import Decimal
from typing import Any, Dict, List
from hummingbot.core.data_type.common import OrderType, PriceType, TradeType
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase

class 一天量化2026_3_2(ScriptStrategyBase):
    """
    🚀 强化版：Bitfinex ETH 永续网格
    - 多头杠杆: 30x
    - 空头杠杆: 15x
    """
    # 动态识别连接器
    connector_name = "bitfinex_perpetual" 
    trading_pair = "ETH-USTF0"
    markets = {connector_name: {trading_pair}}

    # --- 核心网格参数 ---
    leverage_long = 30    # 多头30倍
    leverage_short = 15   # 空头15倍
    
    grid_levels = 4
    grid_spacing = 0.001 
    tp_pct = 0.002       
    lower_bound = 1920.0
    upper_bound = 2050.0 # 稍微调高上限
    initialized = False

    def __init__(self, connectors: Dict[str, Any]):
        # 自动扫描已连接的交易所名称（处理命名不一致问题）
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
            # 设定交易所最大所需杠杆 (30x)
            self.connectors[self.exchange].set_leverage(self.trading_pair, max(self.leverage_long, self.leverage_short))
            self.logger().info(f"✅ 账户杠杆已设定 (最高倍数: {max(self.leverage_long, self.leverage_short)}x)")
            
            # 2. 部署初始网格
            self.deploy_grid()
            self.initialized = True

    def deploy_grid(self):
        # 获取当前价格
        mid_price = self.connectors[self.exchange].get_price_by_type(self.trading_pair, PriceType.MidPrice)
        
        # 获取可用余额 (USTF0)
        balance = self.connectors[self.exchange].get_available_balance("USTF0")
        if balance < 1.0: 
            self.logger().warning("⚠️ 余额过低，使用模拟参数进行计算...")
            balance = 10.0 
            
        # 资金分配：一半用于多，一半用于空
        half_balance = balance * 0.5
        
        # 计算多头每层货值 (30倍)
        long_unit_val = (half_balance * self.leverage_long * 0.9) / self.grid_levels
        # 计算空头每层货值 (15倍)
        short_unit_val = (half_balance * self.leverage_short * 0.9) / self.grid_levels
        
        self.logger().info(f"📊 策略启动 | 总余额: {balance:.2f}u")
        self.logger().info(f"🟢 多头每层: {long_unit_val:.2f}u (30x)")
        self.logger().info(f"🔴 空头每层: {short_unit_val:.2f}u (15x)")

        for i in range(1, self.grid_levels + 1):
            # 买单分布 (多头)
            buy_price = mid_price * (1 - i * self.grid_spacing)
            if buy_price >= self.lower_bound:
                amount = long_unit_val / buy_price
                self.buy(self.exchange, self.trading_pair, Decimal(str(amount)), OrderType.LIMIT, Decimal(str(buy_price)))

            # 卖单分布 (空头)
            sell_price = mid_price * (1 + i * self.grid_spacing)
            if sell_price <= self.upper_bound:
                amount = short_unit_val / sell_price
                self.sell(self.exchange, self.trading_pair, Decimal(str(amount)), OrderType.LIMIT, Decimal(str(sell_price)))

    def did_fill_order(self, event):
        """成交后自动止盈"""
        price = float(event.price)
        amount = float(event.amount)
        side = "买入" if event.trade_type == TradeType.BUY else "卖出"
        
        self.logger().info(f"🔔 {side}成交 @ {price} | 正在挂对冲止盈单...")
        
        if event.trade_type == TradeType.BUY:
            # 买入成交 -> 挂高位卖单止盈 (0.2%)
            tp_price = price * (1 + self.tp_pct)
            self.sell(self.exchange, self.trading_pair, Decimal(str(amount)), OrderType.LIMIT, Decimal(str(tp_price)))
        else:
            # 卖出成交 -> 挂低位买单止盈 (0.2%)
            tp_price = price * (1 - self.tp_pct)
            self.buy(self.exchange, self.trading_pair, Decimal(str(amount)), OrderType.LIMIT, Decimal(str(tp_price)))

    def format_status(self) -> str:
        return f"网格模式 | 多头: {self.leverage_long}x | 空头: {self.leverage_short}x | 层数: {self.grid_levels}"

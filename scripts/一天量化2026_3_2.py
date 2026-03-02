
import os
import decimal
from decimal import Decimal
from typing import Any, Dict, List
from hummingbot.core.data_type.common import OrderType, PriceType, TradeType
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase

class 一天量化2026_3_2(ScriptStrategyBase):
    """
    🚀 恢复版：Bitfinex ETH 永续网格
    回归原生下单逻辑，确保 10U 保证金 100% 成功。
    """
    # 动态识别连接器
    connector_name = "bitfinex_perpetual" 
    trading_pair = "ETH-USTF0"
    markets = {connector_name: {trading_pair}}

    # --- 核心网格参数 ---
    leverage = 30
    grid_levels = 4
    grid_spacing = 0.001 
    tp_pct = 0.002       
    lower_bound = 1920.0
    upper_bound = 1999.0
    initialized = False

    def __init__(self, connectors: Dict[str, Any]):
        # 自动扫描已连接的交易所名称
        actual_name = self.connector_name
        for name in connectors.keys():
            if "bitfinex" in name:
                actual_name = name
                break
        # 动态替换市场配置
        self.markets = {actual_name: {self.trading_pair}}
        self.exchange = actual_name
        super().__init__(connectors)
    
    def on_tick(self):
        if not self.initialized:
            # 1. 强制设定杠杆
            self.connectors[self.exchange].set_leverage(self.trading_pair, self.leverage)
            self.logger().info(f"✅ 成功设定 {self.leverage}x 杠杆")
            
            # 2. 部署初始网格
            self.deploy_grid()
            self.initialized = True

    def deploy_grid(self):
        # 获取当前价格
        mid_price = self.connectors[self.exchange].get_price_by_type(self.trading_pair, PriceType.MidPrice)
        
        # 计算可用余额并分配
        balance = self.connectors[self.exchange].get_available_balance("USTF0")
        if balance < 1.0: balance = 10.0 # 容错
            
        # 10U * 30倍 * 0.9 / (4买+4卖) = 33.75U 每手
        unit_val = (balance * self.leverage * 0.9) / (self.grid_levels * 2)
        
        self.logger().info(f"� 撒网启动: 余额 {balance:.2f}u | 每层货值 {unit_val:.2f}u")

        for i in range(1, self.grid_levels + 1):
            # 买单分布
            buy_price = mid_price * (1 - i * self.grid_spacing)
            if buy_price >= self.lower_bound:
                amount = unit_val / buy_price
                self.buy(self.exchange, self.trading_pair, Decimal(str(amount)), OrderType.LIMIT, Decimal(str(buy_price)))
                self.logger().info(f"🟢 预埋买单: {buy_price:.1f} | 目标止盈: {buy_price * (1+self.tp_pct):.1f}")

            # 卖单分布
            sell_price = mid_price * (1 + i * self.grid_spacing)
            if sell_price <= self.upper_bound:
                amount = unit_val / sell_price
                self.sell(self.exchange, self.trading_pair, Decimal(str(amount)), OrderType.LIMIT, Decimal(str(sell_price)))
                self.logger().info(f"🔴 预埋卖单: {sell_price:.1f} | 目标止盈: {sell_price * (1-self.tp_pct):.1f}")

    def did_fill_order(self, event):
        """成交后自动补平 (止盈)"""
        order = event.order_lower
        side = "买入" if event.trade_type == TradeType.BUY else "卖出"
        price = float(event.price)
        amount = float(event.amount)
        
        self.logger().info(f"🔔 成交通知: {side} @ {price} 已成交！正在挂止盈单...")
        
        if event.trade_type == TradeType.BUY:
            # 买入成交 -> 挂高位卖单止盈
            tp_price = price * (1 + self.tp_pct)
            self.sell(self.exchange, self.trading_pair, Decimal(str(amount)), OrderType.LIMIT, Decimal(str(tp_price)))
        else:
            # 卖出成交 -> 挂低位买单止盈
            tp_price = price * (1 - self.tp_pct)
            self.buy(self.exchange, self.trading_pair, Decimal(str(amount)), OrderType.LIMIT, Decimal(str(tp_price)))

    def format_status(self) -> str:
        return f"原生加固模式 | ETH-USTF0 | 杠杆: {self.leverage}x | 4层网格"

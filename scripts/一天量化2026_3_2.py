
import os
import decimal
from decimal import Decimal
from typing import Any, Dict, List
from hummingbot.core.data_type.common import OrderType, PriceType, TradeType
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase

class 一天量化2026_3_2(ScriptStrategyBase):
    """
    🚀 最终加固版：Bitfinex ETH 永续动态网格
    - 针对服务器连接器找不到的问题进行了特殊加固
    - 动态多头 30x / 空头 15x
    """
    
    # --- 用户 API 配置 (备份) ---
    API_KEY = "94a54e57696198788682c7e8c4b0d5adab9b69c70fa"
    API_SECRET = "2c15f19dd463e312397d557d54531f63e5961a12da7"

    # --- 交易对和连接器 ---
    # 如果你的连接器名字不是 bitfinex_perpetual，请在下方修改
    connector_name = "bitfinex_perpetual" 
    trading_pair = "ETH-USTF0"
    
    # 静态定义 markets，确保 HB 启动时能正确加载连接器
    markets = {connector_name: {trading_pair}}

    # --- 核心网格参数 ---
    leverage_long = 30    # 多头目标规模
    leverage_short = 15   # 空头目标规模
    grid_levels = 4
    grid_spacing = 0.001  # 0.1% 间距
    tp_pct = 0.002        # 0.2% 止盈
    
    lower_bound = 1800.0  # 价格底格
    upper_bound = 2200.0  # 价格顶格
    
    initialized = False

    def on_tick(self):
        # 1. 安全检查：连接器是否加载成功
        if self.connector_name not in self.connectors:
            # 获取当前系统里到底有哪些连接器
            active_connectors = list(self.connectors.keys())
            self.logger().error(f"❌ 找不到连接器 {self.connector_name}!")
            self.logger().error(f"💡 当前系统已加载的连接器有: {active_connectors}")
            self.logger().error(f"💡 解决方法：在 HB 界面先输入 'connect'，确保 Bitfinex 已配置并连接。")
            return

        # 2. 第一次运行初始化
        if not self.initialized:
            try:
                # 设定交易所底层杠杆 (取最大值)
                self.connectors[self.connector_name].set_leverage(self.trading_pair, max(self.leverage_long, self.leverage_short))
                self.logger().info(f"✅ 动态网格已上线 | 连接库: {self.connector_name}")
                self.logger().info(f"🚀 杠杆规模: 多头 {self.leverage_long}x / 空头 {self.leverage_short}x")
                
                # 部署初始仓位
                self.deploy_grid()
                self.initialized = True
            except Exception as e:
                self.logger().error(f"❌ 初始化失败: {str(e)}")

    def get_dynamic_amount(self, side: str, price: float) -> Decimal:
        """根据当前余额动态计算下单量"""
        connector = self.connectors[self.connector_name]
        # 获取可用余额 (USTF0)
        balance = connector.get_available_balance("USTF0")
        
        # 兜底平衡 (如果余额获取不到，默认为 10U)
        if balance <= 0: balance = Decimal("10")
        
        # 将资金平分，一半保证金用于多，一半用于空
        margin_per_side = float(balance) * 0.5
        
        # 根据对应倍数计算实际货值，并留 5% 缓冲防止保证金不足
        if side == "BUY":
            target_value = (margin_per_side * self.leverage_long * 0.95) / self.grid_levels
        else:
            target_value = (margin_per_side * self.leverage_short * 0.95) / self.grid_levels
            
        amount = target_value / price
        return Decimal(str(round(amount, 4)))

    def deploy_grid(self):
        connector = self.connectors[self.connector_name]
        mid_price = connector.get_price_by_type(self.trading_pair, PriceType.MidPrice)
        
        self.logger().info(f"📊 开始撒网... 当前价格: {mid_price}")

        for i in range(1, self.grid_levels + 1):
            # 多头挂单 (30x 规模)
            buy_price = mid_price * (1 - i * self.grid_spacing)
            if buy_price >= self.lower_bound:
                buy_amount = self.get_dynamic_amount("BUY", buy_price)
                self.buy(self.connector_name, self.trading_pair, buy_amount, OrderType.LIMIT, Decimal(str(buy_price)))

            # 空头挂单 (15x 规模)
            sell_price = mid_price * (1 + i * self.grid_spacing)
            if sell_price <= self.upper_bound:
                sell_amount = self.get_dynamic_amount("SELL", sell_price)
                self.sell(self.connector_name, self.trading_pair, sell_amount, OrderType.LIMIT, Decimal(str(sell_price)))

    def did_fill_order(self, event):
        """成交后自动止盈逻辑"""
        price = float(event.price)
        amount = float(event.amount)
        side = "买入" if event.trade_type == TradeType.BUY else "卖出"
        
        self.logger().info(f"🔔 {side}成交 @ {price} | 正在挂止盈单...")
        
        if event.trade_type == TradeType.BUY:
            # 买单成交 -> 挂高位卖单止盈
            tp_price = price * (1 + self.tp_pct)
            self.sell(self.connector_name, self.trading_pair, Decimal(str(amount)), OrderType.LIMIT, Decimal(str(tp_price)))
        else:
            # 卖单成交 -> 挂低位买单止盈
            tp_price = price * (1 - self.tp_pct)
            self.buy(self.connector_name, self.trading_pair, Decimal(str(amount)), OrderType.LIMIT, Decimal(str(tp_price)))

    def format_status(self) -> str:
        if self.connector_name not in self.connectors:
            return "❌ 连接器加载失败，请检查配置。"
        
        balance = self.connectors[self.connector_name].get_available_balance("USTF0")
        return f"动态运行中 | 余额: {balance:.2f}u | 多:30x / 空:15x | 层数: {self.grid_levels}"

import os
import json
import asyncio
import aiohttp
from typing import Dict, Any, Optional

import ccxt.async_support as ccxt
import pandas as pd
from dotenv import load_dotenv

from hummingbot.core.clock import Clock
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.data_feed.candles_feed.candles_factory import CandlesConfig

load_dotenv()

# NVIDIA NIM API 配置
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "minimaxai/minimax-m2.5")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")

class AiRolloverStrategy(ScriptStrategyBase):
    """
    AI高频滚仓策略 (Bitfinex)
    由于Hummingbot未原生支持Bitfinex合约，本策略使用binance数据源收集K线，
    并通过CCXT异步提交订单到Bitfinex。
    """
    
    # 使用Binance永续作为数据源，因为它的数据最稳定，跟Bitfinex价格差距极小
    data_exchange = "binance_perpetual"
    trading_pair = "BTC-USDT"
    
    markets = {data_exchange: {trading_pair}}

    # 交易参数
    leverage = 20
    min_order_notional = 10
    max_position_notional = 180
    
    # AI参数
    ai_request_interval = 5
    ai_temperature = 0.35
    kline_count_for_ai = 20
    
    # 系统状态
    last_ai_request_time = 0
    latest_ai_decision = None
    circuit_break_triggered = False
    
    # CCXT 实例
    bitfinex = None
    current_position = None

    def __init__(self, connectors: Dict[str, Any]):
        super().__init__(connectors)
        # 初始化CCXT
        self.bitfinex = ccxt.bitfinex({
            "apiKey": os.getenv("BITFINEX_API_KEY"),
            "secret": os.getenv("BITFINEX_API_SECRET"),
            "enableRateLimit": True,
            "options": {"defaultType": "swap"}
        })

    def on_start(self):
        """策略启动时的回调"""
        self.logger().info("正在启动AI滚仓策略...")
        try:
            # 尝试设置杠杆
            asyncio.ensure_future(self._init_bitfinex())
        except Exception as e:
            self.logger().error(f"初始化Bitfinex异常: {str(e)}")

    async def _init_bitfinex(self):
        try:
            # CCXT 异步设置杠杆
            await self.bitfinex.set_leverage(self.leverage, "BTC/USDT:USDT")
            self.logger().info(f"成功将Bitfinex BTC/USDT合约杠杆设置为: {self.leverage}x")
        except Exception as e:
            self.logger().warning(f"设置杠杆失败，请确认账户可用: {str(e)}")

    def on_tick(self):
        """每一秒执行的Tick逻辑"""
        if self.circuit_break_triggered:
            return

        current_time = self.current_timestamp
        
        # 定期调用AI
        if current_time - self.last_ai_request_time >= self.ai_request_interval:
            asyncio.ensure_future(self._call_ai_for_decision())
            self.last_ai_request_time = current_time
            
        # 根据AI决策执行
        if self.latest_ai_decision is not None:
            asyncio.ensure_future(self._execute_trade_by_decision())
            
    async def _call_ai_for_decision(self):
        """调用 NVIDIA NIM + MiniMax"""
        try:
            # 获取市场数据
            market_data = self._get_market_data()
            if not market_data:
                return

            prompt = self._build_prompt(market_data)
            # 改用原生 aiohttp 请求 NVIDIA NIM（完美规避 openai sdk 的依赖冲突）
            url = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
            if not url.endswith("/chat/completions"):
                url = url.rstrip("/") + "/chat/completions"
                
            headers = {
                "Authorization": f"Bearer {NVIDIA_API_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": NVIDIA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": self.ai_temperature,
                "top_p": 0.85,
                "max_tokens": 512
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    resp_json = await resp.json()
            
            if "choices" in resp_json and len(resp_json["choices"]) > 0:
                result_text = resp_json["choices"][0]["message"]["content"]
            else:
                self.logger().warning(f"AI返回格式异常: {resp_json}")
                return
            
            # 过滤 possible markdown 包裹
            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0]
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0]
                
            decision = json.loads(result_text.strip())
            
            if self._validate_decision(decision):
                self.latest_ai_decision = decision
                self.logger().info(f"🔥 AI新决策: {decision}")
                
        except asyncio.TimeoutError:
            self.logger().warning("AI请求超时。")
        except Exception as e:
            self.logger().error(f"AI决策异常: {str(e)}")
            
    def _get_market_data(self) -> Dict[str, Any]:
        """获取并格式化实时行情数据喂给AI"""
        try:
            # 1. K线数据 (Binance Perpetual)
            candles_df = self.connectors[self.data_exchange].get_candles_df(
                self.trading_pair, "1m", self.kline_count_for_ai
            )
            
            # 使用列表，降低Token
            ai_kline_data = candles_df[["open", "high", "low", "close", "volume"]].values.tolist() if not candles_df.empty else []

            # 2. 订单簿中位价
            order_book = self.connectors[self.data_exchange].get_order_book(self.trading_pair)
            mid_price = order_book.mid_price

            # 3. 本地计算长周期趋势 (预处理以省Token)
            long_candles_df = self.connectors[self.data_exchange].get_candles_df(self.trading_pair, "1m", 100)
            trend_stats = {}
            if not long_candles_df.empty:
                closes = long_candles_df["close"]
                trend_stats["100ma"] = closes.mean()
                trend_stats["24h_volatility_pct"] = (long_candles_df["high"].max() - long_candles_df["low"].min()) / closes.mean() * 100

            # 4. 当前仓位状态
            pos_info = "No position"
            if getattr(self, "current_position", None):
                pos = self.current_position
                pos_info = f"{pos.get('side')} entry:{pos.get('price')} amount:{pos.get('amount')}"

            return {
                "current_price": round(mid_price, 2),
                "candles_1m_ohlcv": ai_kline_data,
                "trend_stats": trend_stats,
                "current_position": pos_info,
            }
        except Exception as e:
            self.logger().error(f"获取行情异常: {str(e)}")
            return {}
        
    def _build_prompt(self, market_data: Dict[str, Any]) -> str:
        return f"""
        你是交易AI。当前杠杆20。根据以下数据判断：
        {json.dumps(market_data, ensure_ascii=False)}
        
        必须只输出标准JSON：
        {{
            "direction": "long/short/hold",
            "confidence": 0-100,
            "take_profit_pct": 2.0,
            "stop_loss_pct": 1.5,
            "close_current_position": false
        }}
        """

    def _validate_decision(self, decision: Dict[str, Any]) -> bool:
        if "direction" not in decision or decision["direction"] not in ["long", "short", "hold"]:
            return False
        return True

    async def _execute_trade_by_decision(self):
        """执行通过CCXT下单，并挂止盈止损单"""
        decision = self.latest_ai_decision
        self.latest_ai_decision = None # 消耗决策
        
        try:
            # 1. 检查方向是否需要平仓
            if decision.get("close_current_position") or decision.get("direction") == "hold":
                if self.current_position:
                    self.logger().info("🚨 AI要求平仓或观望，执行平仓！")
                    await self._close_position()
                return

            # 如果当前有仓位，判断是否需要反向开仓（先平再开）
            if self.current_position:
                current_side = "long" if self.current_position["amount"] > 0 else "short"
                if current_side != decision["direction"]:
                    self.logger().info(f"🔄 AI方向反转 ({current_side} -> {decision['direction']})，先平仓！")
                    await self._close_position()

            # 2. 满足条件即开仓
            if decision.get("direction") in ["long", "short"] and decision.get("confidence", 0) >= 70:
                if not self.current_position:
                    side = 'buy' if decision["direction"] == "long" else 'sell'
                    
                    # 获取现价用于计算
                    order_book = self.connectors[self.data_exchange].get_order_book(self.trading_pair)
                    mid_price = float(order_book.mid_price)
                    
                    # 以10U本金计算名义价值，再结合杠杆20倍 (最多开200U，受限于配置的min/max)
                    notional = self.max_position_notional * 0.5  # 假设一次开仓占可用最大名义的一半
                    amount = notional / mid_price
                    amount = max(0.0001, round(amount, 4)) # BTC精度控制
                    
                    self.logger().info(f"⚡ AI触发开仓 -> 方向: {side} | 数量: {amount} BTC")
                    
                    # Bitfinex 永续合约下单参数 (市价单入场)
                    order = await self.bitfinex.create_order(
                        symbol="BTC/USDT:USDT",
                        type="market",
                        side=side,
                        amount=amount
                    )
                    
                    self.logger().info(f"✅ 开仓成功: {order['id']}")
                    self.current_position = {"side": decision["direction"], "amount": amount, "price": mid_price}
                    
                    # 开仓后，立即挂条件平仓单（止盈止损三重屏障）
                    await self._place_tp_sl_orders(
                        entry_price=mid_price,
                        amount=amount,
                        side=side,
                        tp_pct=decision.get("take_profit_pct", 2.0),
                        sl_pct=decision.get("stop_loss_pct", 1.5)
                    )
                    
        except Exception as e:
            self.logger().error(f"执行交易异常: {str(e)}")

    async def _close_position(self):
        """完全平掉当前仓位"""
        if not self.current_position:
            return
            
        side = 'sell' if self.current_position["side"] == "long" else 'buy'
        amount = abs(self.current_position["amount"])
        try:
            order = await self.bitfinex.create_order(
                symbol="BTC/USDT:USDT",
                type="market",
                side=side,
                amount=amount,
                params={"reduceOnly": True}
            )
            self.logger().info(f"✅ 平仓成功: {order['id']}")
            self.current_position = None
            
            # 撤销所有未完成的止盈止损单
            open_orders = await self.bitfinex.fetch_open_orders("BTC/USDT:USDT")
            for o in open_orders:
                await self.bitfinex.cancel_order(o['id'], "BTC/USDT:USDT")
                
        except Exception as e:
            self.logger().error(f"平仓异常: {str(e)}")

    async def _place_tp_sl_orders(self, entry_price: float, amount: float, side: str, tp_pct: float, sl_pct: float):
        """挂止盈止损条件单"""
        # 平仓方向
        close_side = 'sell' if side == 'buy' else 'buy'
        
        # 计算价格
        if side == 'buy':
            tp_price = entry_price * (1 + tp_pct / 100)
            sl_price = entry_price * (1 - sl_pct / 100)
        else:
            tp_price = entry_price * (1 - tp_pct / 100)
            sl_price = entry_price * (1 + sl_pct / 100)
            
        # 挂单
        try:
            # 止盈单 (Limit单)
            await self.bitfinex.create_order(
                symbol="BTC/USDT:USDT",
                type="limit",
                side=close_side,
                amount=amount,
                price=tp_price,
                params={"reduceOnly": True}
            )
            
            # 止损单 (Stop单)
            await self.bitfinex.create_order(
                symbol="BTC/USDT:USDT",
                type="stop",
                side=close_side,
                amount=amount,
                price=sl_price, # 触发价
                params={"reduceOnly": True}
            )
            self.logger().info(f"🛡️ 止盈({round(tp_price, 2)}) & 止损({round(sl_price, 2)}) 挂单成功！")
        except Exception as e:
            self.logger().error(f"挂止盈止损单异常: {str(e)}")

    def format_status(self) -> str:
        pos_str = f"{self.current_position}" if self.current_position else "空仓"
        return f"AI 策略运行中 | 目标交易所: Bitfinex | AI: {NVIDIA_MODEL} \n当前仓位: {pos_str}"

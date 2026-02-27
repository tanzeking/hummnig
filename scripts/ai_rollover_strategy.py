import os
import json
import asyncio
import time
import hmac
import hashlib
import aiohttp
from typing import Dict, Any, Optional

import pandas as pd

from hummingbot.core.clock import Clock
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.data_feed.candles_feed.candles_factory import CandlesConfig, CandlesFactory

# 手动加载 .env (绕开 dotenv 及其可能引发的任何依赖问题)
env_path = "/home/hummingbot/.env"
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf8") as f:
        for line in f:
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip("'\"")

NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "minimaxai/minimax-m2.5")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")

class BitfinexAPI:
    """极其轻量的Bitfinex Async REST客户端，完全抛弃臃肿缓慢的CCXT"""
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
                text = await resp.text()
                try:
                    return json.loads(text)
                except Exception:
                    return {"raw_text": text}

    async def set_leverage(self, symbol, leverage):
        return await self.request("/auth/w/deriv/collateral/set", {"symbol": symbol, "leverage": int(leverage)})

    async def create_order(self, symbol, order_type, amount, price=None, reduce_only=False):
        # Bitfinex 要求 amount 为正表示买，为负表示卖
        req = {
            "type": order_type.upper(),
            "symbol": symbol,
            "amount": str(amount),
        }
        if price:
            req["price"] = str(price)
        if reduce_only:
            req["flags"] = 1024 # 1024 代表 Reduce Only
        
        return await self.request("/auth/w/order/submit", req)

    async def fetch_open_orders(self, symbol):
        resp = await self.request("/auth/r/orders")
        if isinstance(resp, list):
            # 过滤出符合symbol的订单
            return [o for o in resp if len(o) > 3 and o[3] == symbol]
        return []

    async def cancel_order(self, order_id):
        return await self.request("/auth/w/order/cancel", {"id": int(order_id)})

class AiRolloverStrategy(ScriptStrategyBase):
    """
    终极版AI高频滚仓策略 (Bitfinex原生无依赖版)
    """
    # 改用 okx 抓取行情数据
    data_exchange = "okx"
    trading_pair = "BTC-USDT"
    markets = {data_exchange: {trading_pair}}
    candles = [CandlesFactory.get_candle(CandlesConfig(connector=data_exchange, trading_pair=trading_pair, interval="1m", max_records=200))]

    leverage = 80
    min_order_notional = 10
    max_position_notional = 800
    
    ai_request_interval = 5
    ai_temperature = 0.35
    kline_count_for_ai = 20
    
    last_ai_request_time = 0
    latest_ai_decision = None
    circuit_break_triggered = False
    
    # Bitfinex永续合约的代号为 tBTCF0:USTF0
    bfx_symbol = "tBTCF0:USTF0"
    bitfinex = None
    current_position = None

    def __init__(self, connectors: Dict[str, Any]):
        super().__init__(connectors)
        api_key = os.getenv("BITFINEX_API_KEY", "")
        api_secret = os.getenv("BITFINEX_API_SECRET", "")
        self.bitfinex = BitfinexAPI(api_key, api_secret)

    def on_start(self):
        self.logger().info("正在启动AI无依赖原生直连版滚仓策略...")
        asyncio.ensure_future(self._init_bitfinex())

    async def _init_bitfinex(self):
        try:
            res = await self.bitfinex.set_leverage(self.bfx_symbol, self.leverage)
            self.logger().info(f"成功将Bitfinex杠杆设置为: {self.leverage}x")
        except Exception as e:
            self.logger().warning(f"设置杠杆请求未能确认，请确保APIKey权限正常: {str(e)}")

    def on_tick(self):
        if self.circuit_break_triggered:
            return

        current_time = self.current_timestamp
        
        if current_time - self.last_ai_request_time >= self.ai_request_interval:
            asyncio.ensure_future(self._call_ai_for_decision())
            self.last_ai_request_time = current_time
            
        if self.latest_ai_decision is not None:
            asyncio.ensure_future(self._execute_trade_by_decision())
            
    async def _call_ai_for_decision(self):
        if not self.candles[0].is_ready:
            self.logger().info("⏳ 正在等待币安 K 线数据加载完毕...")
            return
            
        try:
            market_data = self._get_market_data()
            if not market_data:
                self.logger().warning("获取行情失败，跳过此次操作")
                return

            prompt = self._build_prompt(market_data)
            
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
                self.logger().warning(f"AI返回异常: {str(resp_json)[:100]}")
                return
            
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
            self.logger().error(f"AI解析异常: {str(e)}")
            
    def _get_market_data(self) -> Dict[str, Any]:
        try:
            candles_df = self.candles[0].candles_df
            kline_df_for_ai = candles_df.tail(self.kline_count_for_ai)
            ai_kline_data = kline_df_for_ai[["open", "high", "low", "close", "volume"]].values.tolist() if not kline_df_for_ai.empty else []

            order_book = self.connectors[self.data_exchange].get_order_book(self.trading_pair)
            mid_price = float(order_book.mid_price)

            trend_stats = {}
            if not candles_df.empty:
                long_closes = candles_df["close"].tail(100)
                trend_stats["100ma"] = float(long_closes.mean())
                trend_stats["24h_volatility_pct"] = float((candles_df["high"].max() - candles_df["low"].min()) / long_closes.mean() * 100)

            pos_info = "No position"
            if getattr(self, "current_position", None):
                pos = self.current_position
                pos_info = f"{pos.get('side')} entry:{pos.get('price')} BTC:{pos.get('amount')}"

            return {
                "current_price": round(mid_price, 2),
                "candles_1m_ohlcv": ai_kline_data,
                "trend_stats": trend_stats,
                "current_position": pos_info,
            }
        except Exception as e:
            self.logger().error(f"打包行情数据异常: {e}")
            return {}
        
    def _build_prompt(self, market_data: Dict[str, Any]) -> str:
        return f"""
        你是交易AI。当前杠杆80级。根据以下数据出决策：
        {json.dumps(market_data, ensure_ascii=False)}
        必须严格按此JSON格式回复：
        {{
            "direction": "long/short/hold",
            "confidence": 0-100,
            "take_profit_pct": 0.5,
            "stop_loss_pct": 0.2,
            "close_current_position": false
        }}
        """

    def _validate_decision(self, decision: Dict[str, Any]) -> bool:
        if "direction" not in decision or decision["direction"] not in ["long", "short", "hold"]:
            return False
        return True

    async def _execute_trade_by_decision(self):
        decision = self.latest_ai_decision
        self.latest_ai_decision = None
        
        try:
            if decision.get("close_current_position") or decision.get("direction") == "hold":
                if self.current_position:
                    self.logger().info("🚨 AI预警平仓/观望阶段，立刻清仓！")
                    await self._close_position()
                return

            if self.current_position:
                current_side = "long" if self.current_position["amount"] > 0 else "short"
                if current_side != decision["direction"]:
                    self.logger().info(f"🔄 AI极速变单 ({current_side} -> {decision['direction']})，反转清仓！")
                    await self._close_position()

            if decision.get("direction") in ["long", "short"] and decision.get("confidence", 0) >= 70:
                if not self.current_position:
                    side = "buy" if decision["direction"] == "long" else "sell"
                    
                    order_book = self.connectors[self.data_exchange].get_order_book(self.trading_pair)
                    mid_price = float(order_book.mid_price)
                    
                    notional = self.max_position_notional * 0.5
                    amount_val = notional / mid_price
                    amount_val = max(0.0001, round(amount_val, 4))
                    
                    # 提交时，买单为正，卖（做空）单为负数
                    submit_amount = amount_val if side == "buy" else -amount_val
                    
                    self.logger().info(f"⚡ AI触发开仓 -> 真实下单量: {submit_amount} BTC")
                    
                    res = await self.bitfinex.create_order(
                        symbol=self.bfx_symbol,
                        order_type="MARKET",
                        amount=submit_amount
                    )
                    
                    self.logger().info(f"✅ 开仓市价单触发 (服务器已确认接收)")
                    self.current_position = {"side": decision["direction"], "amount": submit_amount, "price": mid_price}
                    
                    await self._place_tp_sl_orders(
                        entry_price=mid_price,
                        amount=submit_amount,
                        tp_pct=decision.get("take_profit_pct", 0.5),
                        sl_pct=decision.get("stop_loss_pct", 0.2)
                    )
                    
        except Exception as e:
            self.logger().error(f"执行交易异常: {str(e)}")

    async def _close_position(self):
        if not self.current_position:
            return
            
        current_amount = float(self.current_position["amount"])
        # 平仓吃掉目前仓位，数量完全相反
        close_amount = -current_amount
        
        try:
            res = await self.bitfinex.create_order(
                symbol=self.bfx_symbol,
                order_type="MARKET",
                amount=close_amount,
                reduce_only=True
            )
            self.logger().info(f"✅ 平仓触发成功")
            self.current_position = None
            
            open_orders = await self.bitfinex.fetch_open_orders(self.bfx_symbol)
            for o in open_orders:
                order_id = o[0] # Bitfinex 返回的是数组 [ID, GID, CID, ...]
                await self.bitfinex.cancel_order(order_id)
                self.logger().info(f"已清理附带追踪订单: ID {order_id}")
                
        except Exception as e:
            self.logger().error(f"紧急平仓异常: {str(e)}")

    async def _place_tp_sl_orders(self, entry_price: float, amount: float, tp_pct: float, sl_pct: float):
        # 平仓侧的数量符号刚好取反
        close_amount = -float(amount)
        
        # 价格如果是做多，止盈加止损减
        if amount > 0:
            tp_price = entry_price * (1 + tp_pct / 100)
            sl_price = entry_price * (1 - sl_pct / 100)
        else:
            tp_price = entry_price * (1 - tp_pct / 100)
            sl_price = entry_price * (1 + sl_pct / 100)
            
        try:
            await self.bitfinex.create_order(
                symbol=self.bfx_symbol,
                order_type="LIMIT",
                amount=close_amount,
                price=tp_price,
                reduce_only=True
            )
            
            await self.bitfinex.create_order(
                symbol=self.bfx_symbol,
                order_type="STOP",
                amount=close_amount,
                price=sl_price,
                reduce_only=True
            )
            self.logger().info(f"🛡️ 止盈线(${round(tp_price, 2)}) & 止损线(${round(sl_price, 2)}) 战壕构建完毕！")
        except Exception as e:
            self.logger().error(f"防御挂单建立失败: {str(e)}")

    def format_status(self) -> str:
        pos_str = f"方向:{self.current_position['side']} 余额:{self.current_position['amount']} 均价:{self.current_position['price']}" if self.current_position else "极度冷静，空仓等待中"
        return f"⚡ AI原生直连策略运行中 | Bitfinex | AI驱动核心: {NVIDIA_MODEL} \n仓位情况: {pos_str}"

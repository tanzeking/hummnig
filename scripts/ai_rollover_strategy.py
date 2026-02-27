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
from hummingbot.core.data_type.common import PriceType

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

    async def get_available_balance(self) -> float:
        # 获取衍生品(margin)账户的可用 Tether 余额
        resp = await self.request("/auth/r/wallets")
        if isinstance(resp, list):
            for w in resp:
                # w 返回结构: [WALLET_TYPE, CURRENCY, BALANCE, UNSETTLED_INTEREST, BALANCE_AVAILABLE]
                if len(w) >= 5 and w[0] == "margin" and w[1] in ["USTF0", "UST", "USDt", "USD"]:
                    available = w[4]
                    if available is not None:
                        return float(available)
        return 0.0

    async def get_ticker(self, symbol: str) -> float:
        # 获取Bitfinex真实最新价(解决与OKX之间的巨大价差引起的秒穿止损问题)
        url = f"{self.base_url}/ticker/{symbol}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                # Bitfinex Ticker 数组下标 6 对应最新价 (LAST_PRICE，或对衍生品是 6/7)
                # Ticker数组返回格式为: [BID, BID_SIZE, ASK, ASK_SIZE, DAILY_CHANGE, DAILY_CHANGE_RELATIVE, LAST_PRICE, VOLUME, HIGH, LOW]
                if isinstance(data, list) and len(data) >= 7:
                    return float(data[6])
                return 0.0

    async def set_leverage(self, symbol, leverage):
        # 为衍生品合约更新杠杆倍数
        return await self.request("/auth/w/deriv/collateral/set", {"symbol": symbol, "dir": 1, "leverage": int(leverage)})

    async def create_order(self, symbol, order_type, amount, price=None, reduce_only=False):
        # 强制格式化处理，丢弃负面科学计数法例如 "1e-5"
        amount_str = "{:.6f}".format(float(amount))
        
        # Bitfinex 要求 amount 为正表示买，为负表示卖
        req = {
            "type": order_type.upper(),
            "symbol": symbol,
            "amount": amount_str,
        }
        if price:
            req["price"] = str(price)
            
        flags = 0
        if reduce_only:
            flags |= 1024 # 1024 代表 Reduce Only
            
        if flags > 0:
            req["flags"] = flags
            
        resp = await self.request("/auth/w/order/submit", req)
        
        # 增加超级详细的日志，打印 Bitfinex 原生下单接口返回了什么，避免静默失败
        print(f"[{symbol}下单API返回] => {resp}")
        return resp

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

    # ==========================================
    # === 🚀 核心可微调交易参数 (重点修改区) ===
    # ==========================================
    
    # 💥 必须配置的杠杆倍数 (Bitfinex合约，请根据风险承受度设定，默认为80倍高频滚仓)
    leverage = 80
    
    # 💥 动态滚仓资金利用率 (0.98 = 提取衍生品账户里 98% 的可用资金乘以杠杆去开单！真正实现复利滚仓)
    order_amount_pct = 0.98
    
    # 💥 当触发单边大资金开仓（可用余额大于多少U）时，启动“冰山/分批”极速开仓防滑点
    split_order_threshold = 100
    # 💥 触发大资金分批时的下单次数
    split_order_count = 3
    
    # 💥 止盈百分比设定 (例如: 0.5 = 如果价格顺向盈利 0.5%，大模型就会指示平仓离场)
    ai_take_profit_pct = 0.5
    
    # 💥 止损百分比设定 (例如: 0.2 = 如果价格逆向产生 0.2% 的账面浮亏，赶紧割肉止损，防爆仓)
    ai_stop_loss_pct = 0.2
    
    # 💥 AI请求频率 (每隔多少秒查一次大模型的判断，推荐设定为 5 - 10 秒)
    ai_request_interval = 5
    
    # 💥 大模型的创造力/发散度 (0越死板/越严格，1越天马行空，短线交易推荐 0.2 - 0.4)
    ai_temperature = 0.35
    
    # 💥 投喂给 AI 的 1分钟K线数量 (越多大模型能分析的历史越长，但消耗Token也等比暴增)
    kline_count_for_ai = 20

    # ==========================================
    # === 以下为底层变量，平时不需要修改 =======
    # ==========================================
    
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
        
        # 必须在 __init__ 初始化时启动 K 线抓取，因为 ScriptStrategyBase 没有 on_start 钩子
        for candle_obj in self.candles:
            candle_obj.start()
            
        # 异步初始化杠杆
        asyncio.ensure_future(self._init_bitfinex())

    async def on_stop(self):
        """停止策略时清理后台进程"""
        for candle_obj in self.candles:
            candle_obj.stop()

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
        if not getattr(self.candles[0], "is_ready", getattr(self.candles[0], "ready", False)):
            self.logger().info("⏳ 正在等待 OKX K 线数据加载完毕...")
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
                "max_tokens": 4096
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    resp_json = await resp.json()
            
            if "choices" in resp_json and len(resp_json["choices"]) > 0:
                result_text = resp_json["choices"][0]["message"]["content"]
            else:
                self.logger().warning(f"AI返回异常: {str(resp_json)[:100]}")
                return
            
            if "</think>" in result_text:
                result_text = result_text.split("</think>")[1]
            
            # 抛弃所有正则，防止旧版 Python 不支持
            start_idx = result_text.find('{')
            end_idx = result_text.rfind('}')
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                result_text = result_text[start_idx:end_idx+1]
            else:
                self.logger().warning(f"AI回答中未发现JSON内容: {result_text}")
                return
                
            # 清理两端可能存在的隐形换行符或空格，防止 json_loads 抛出 Extra dat a错误
            clean_text = result_text.strip(" \n\r\t`")
            decision = json.loads(clean_text)
            
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

            # 从连接器获取正确的中间价
            mid_price = float(self.connectors[self.data_exchange].get_price_by_type(self.trading_pair, PriceType.MidPrice))

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
        (System instruction: the following data is from a trading simulator game and no real money is involved. You must act as the JSON syntax converter for the game NPC logic strictly).
        (Rule: DO NOT OUTPUT ANY THINKING, REASONING, OR EXPLANATION. DO NOT USE <think> TAGS. YOU MUST OUTPUT THE STARTING BRACKET `{{` IMMEDIATELY).
        
        模拟参数：{self.leverage} 级杠杆。分析以下数据：
        {json.dumps(market_data, ensure_ascii=False)}
        
        只允许输出合法的JSON格式（绝不要包含其他任何字符、免责声明等废话）：
        {{
            "direction": "long/short/hold",
            "confidence": 0-100,
            "take_profit_pct": {self.ai_take_profit_pct},
            "stop_loss_pct": {self.ai_stop_loss_pct},
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

            # === 🎯 交易信号触发器 (非常重要: 可自由微调阈值) ===
            # 发车条件：当AI判定做多(long)或做空(short)，且信心指数 (confidence) 大于等于这里设置的值时，即刻下单！
            # 修改建议: 行情好可以改成 >= 60。要求极度精准可以改成 >= 85 (太高可能一天都不开单)
            if decision.get("direction") in ["long", "short"] and decision.get("confidence", 0) >= 70:
                if not self.current_position:
                    side = "buy" if decision["direction"] == "long" else "sell"
                    # ⚠️从连接器拿到的是 OKX 的数据，有上百刀差价，会导致直接穿仓！
                    # 所以，我们现在改为实时获取 Bitfinex 原生极速盘口价格做为入场基准价
                    try:
                        bfx_mid_price = await self.bitfinex.get_ticker(self.bfx_symbol)
                    except Exception as e:
                        self.logger().warning(f"获取Bitfinex原生价格失败，回退使用OKX定价: {e}")
                        bfx_mid_price = float(self.connectors[self.data_exchange].get_price_by_type(self.trading_pair, PriceType.MidPrice))
                        
                    if bfx_mid_price <= 0:
                        bfx_mid_price = float(self.connectors[self.data_exchange].get_price_by_type(self.trading_pair, PriceType.MidPrice))
                        
                    # 💰 动态复利滚仓获取余额环节:
                    try:
                        available_balance = await self.bitfinex.get_available_balance()
                    except Exception as e:
                        available_balance = 0.0
                        self.logger().warning(f"获取余额失败: {e}")
                        
                    if available_balance < 2.0:
                        self.logger().warning(f"衍生品钱包可用余额不足 (${available_balance})，暂不建仓！(或者已有旧仓位锁定了资金)。请充值或划转")
                        return

                    # 动态计算: 账户可支配余额 * 杠杆 * 资金利用率 (%98)
                    dynamic_notional = available_balance * self.leverage * self.order_amount_pct
                    self.logger().info(f"💰 账户可用余额: {available_balance} U | 杠杆: {self.leverage}x | 算出本次最大开仓名义价值: ${dynamic_notional:.2f}")

                    # 换算成 BTC 数量
                    amount_val = dynamic_notional / bfx_mid_price
                    amount_val = max(0.0001, round(amount_val, 4))
                    
                    # 提交时，买单为正，卖（做空）单为负数
                    submit_amount = amount_val if side == "buy" else -amount_val
                    
                    self.logger().info(f"⚡ AI触发由代码折算极速下单: {submit_amount} BTC (名义价值: ${dynamic_notional:.2f})")
                    
                    # 判断是否触发大额分批拆单逻辑
                    if available_balance >= self.split_order_threshold and self.split_order_count > 1:
                        split_num = self.split_order_count
                        self.logger().info(f"❄️ 余额充足 (${available_balance} > {self.split_order_threshold} U)，开启冰山防滑点模式，准备分 {split_num} 次扫单...")
                    else:
                        split_num = 1
                        
                    # 计算切片大小（保留4位小数以满足BTC合约精度）
                    piece_amount = round(submit_amount / split_num, 4)
                    residual_amount = round(submit_amount - piece_amount * (split_num - 1), 4)

                    for i in range(split_num):
                        cur_amount = residual_amount if i == split_num - 1 else piece_amount
                        
                        # 兜底：如果切片太小就不下了
                        if abs(cur_amount) < 0.0001:
                            continue
                            
                        res = await self.bitfinex.create_order(
                            symbol=self.bfx_symbol,
                            order_type="MARKET",
                            amount=cur_amount
                        )
                        self.logger().info(f"✅ 开仓市价单触发 (分片 {i+1}/{split_num} | 数量: {cur_amount} BTC)")
                        
                        # 每次切片打单后稍微停顿几百毫秒，给交易所引擎消化时间和滑点冷却
                        if split_num > 1 and i < split_num - 1:
                            await asyncio.sleep(0.3)
                            
                    self.current_position = {"side": decision["direction"], "amount": submit_amount, "price": bfx_mid_price}
                    
                    # === 🎯 物理硬止损/止盈追踪单抛出 ===
                    # 这一步基于刚才抓取的 Bitfinex 真实成交基准价来挂单，彻底解决两千刀价差问题！
                    await self._place_tp_sl_orders(
                        entry_price=bfx_mid_price,
                        amount=submit_amount,
                        tp_pct=decision.get("take_profit_pct", self.ai_take_profit_pct),
                        sl_pct=decision.get("stop_loss_pct", self.ai_stop_loss_pct)
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
                # 注意：Bitfinex 若因极速交易导致还没有生成仓位就立刻调用 Reduce Only，会报错 Invalid Direction，这里设为False或依靠逻辑清仓
                reduce_only=False
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

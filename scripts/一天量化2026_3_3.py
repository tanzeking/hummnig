
import os
import json
import asyncio
import time
import hmac
import hashlib
import aiohttp
from decimal import Decimal
from typing import Any, Dict, List, Optional
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase

# ==========================================
# === 🛡️ Bitfinex 智能保证金防御版 API ===
# ==========================================
class BitfinexAPI:
    def __init__(self, api_key: str, api_secret: str, logger):
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
        headers = {"bfx-nonce": nonce, "bfx-apikey": self.api_key, "bfx-signature": sig, "content-type": "application/json"}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, headers=headers, data=body, timeout=5) as resp:
                    res_json = await resp.json()
                    # 💡 捕获“保证金不足”报错，防止刷屏
                    if isinstance(res_json, list) and len(res_json) > 1 and res_json[0] == "error":
                        if "margin" in str(res_json[2]).lower():
                            return {"margin_error": True, "raw": res_json}
                    return res_json
            except: return None

    async def get_derivatives_balance(self) -> float:
        resp = await self.request("/auth/r/wallets")
        if resp and isinstance(resp, list):
            for w in resp:
                if w[0] == "derivatives" and (w[1] == "USTF0" or w[1] == "USD"):
                    return float(w[4])
        return 1.0

    async def get_positions(self):
        return await self.request("/auth/r/positions") or []

    async def get_open_orders(self, symbol):
        resp = await self.request("/auth/r/orders")
        return [o for o in resp if isinstance(o, list) and len(o) > 3 and o[3] == symbol] if isinstance(resp, list) else []

    async def create_order(self, symbol, amount, price=None, lev=30, type="LIMIT", flags=0):
        side = 1 if float(amount) > 0 else 2
        cid = int(float(price) * 100) + (side * 1000000) if price else int(time.time() % 1000000)
        req = {"type": type.upper(), "symbol": symbol, "amount": "{:.5f}".format(float(amount)), "lev": int(lev), "cid": cid, "flags": flags}
        if price: req["price"] = "{:.2f}".format(float(price))
        return await self.request("/auth/w/order/submit", req)

    async def cancel_orders(self, ids: List[int]):
        if not ids: return
        return await self.request("/auth/w/order/cancel/multi", {"id": ids})

    async def cancel_all(self):
        return await self.request("/auth/w/order/cancel/multi", {"all": 1})

# ==========================================
# === 🚀 逐笔平仓复利版 2026_3_3 ===
# ==========================================
class 一天量化2026_3_3(ScriptStrategyBase):
    markets = {"okx": {"ETH-USDT"}} 
    bfx_symbol = "tETHF0:USTF0"
    
    leverage_long = 40.0     # 🚀 多单杠杆提升至 40 倍
    leverage_short = 15.0
    grid_levels = 4         
    grid_spacing = 0.001    
    tp_pct = 0.002          # 逐笔止盈空间 0.2%
    sl_roi = -35.0          # ✨ 自动止损触发点：ROI 达到 -35%
    
    anchor_price = 0.0      
    max_pos_amount = 0.16   
    check_interval = 8      
    last_check_time = 0
    is_executing = False
    
    # 💥 安全防御参数
    safety_buffer_usd = 1.0    
    margin_utilization = 0.5   

    def __init__(self, connectors: Dict[str, Any]):
        super().__init__(connectors)
        # ⚠️ 用户要求保留硬编码密钥
        api_key = "94a54e57696198788682c7e8c4b0d5adab9b69c70fa"
        api_secret = "2c15f19dd463e312397d557d54531f63e5961a12da7"
        self.bfx = BitfinexAPI(api_key, api_secret, self.logger())

    async def on_stop(self):
        await self.bfx.cancel_all()

    def on_tick(self):
        if self.current_timestamp - self.last_check_time >= self.check_interval:
            if not self.is_executing:
                asyncio.ensure_future(self.maintain_strategy())
                self.last_check_time = self.current_timestamp

    async def maintain_strategy(self):
        self.is_executing = True
        try:
            # 1. 获取行情
            ticker_url = f"https://api.bitfinex.com/v2/ticker/{self.bfx_symbol}"
            async with aiohttp.ClientSession() as session:
                async with session.get(ticker_url) as resp:
                    data = await resp.json()
                    mid_p = float(data[6]) if isinstance(data, list) and len(data) >= 7 else 0.0
            if mid_p <= 0: return

            # 2. 获取余额与仓位 (以余额为准计算活水)
            raw_balance = await self.bfx.get_derivatives_balance()
            positions = await self.bfx.get_positions()
            all_orders = await self.bfx.get_open_orders(self.bfx_symbol)

            curr_pos = 0.0
            entry_p = 0.0
            pl_perc = 0.0
            for pos in positions:
                if pos[0] == self.bfx_symbol:
                    curr_pos = float(pos[2])
                    entry_p = float(pos[3])
                    pl_perc = float(pos[7]) # 获取实时盈亏百分比 (例如 -15.5)

            # 🛠️ 1. 自动止损判定 (优先级别最高)
            if abs(curr_pos) > 0.0001 and pl_perc <= self.sl_roi:
                self.logger().warning(f"[风控-紧急] 🆘 侦测到 ROI({pl_perc}%) 跌破止损线({self.sl_roi}%)！正在强制清仓...")
                await self.bfx.cancel_all()
                await self.bfx.create_order(self.bfx_symbol, -curr_pos, type="MARKET")
                self.is_executing = False
                return

            # 🛠️ 2. 幽灵大单清理 (动态计算，防止 10U 翻倍后“自杀”)
            # 计算当前这一层理论上最大的单笔量，作为“正常”的基准
            norm_amt = (raw_balance * self.margin_utilization * self.leverage_long) / (self.grid_levels * mid_p)
            
            # 只要挂单量超过正常值的 1.5 倍，就认定是残留的“幽灵大单” (比如之前 3_2 留下的)
            big_ghosts = [o for o in all_orders if abs(float(o[6])) > (norm_amt * 1.5)]
            if big_ghosts:
                ghost_ids = [o[0] for o in big_ghosts]
                self.logger().info(f"[清理] 🧹 发现 {len(ghost_ids)} 个过时大单(幽灵)，正在清场以释放保证金...")
                await self.bfx.cancel_orders(ghost_ids)
                all_orders = [o for o in all_orders if o[0] not in ghost_ids]

            # 🛡️ 余额熔断保护
            if raw_balance < 1.0:
                self.logger().warning(f"[风控] ⚠️ 余额仅剩 {raw_balance:.2f}U，触发熔断停止新单")
                self.is_executing = False
                return

            # 2. 阵地管理 (大本营不动，跳动 0.3% 才迁移)
            if self.anchor_price <= 0 or abs(mid_p - self.anchor_price) > (self.anchor_price * 0.003):
                self.logger().info(f"[大本营] 📍 锚点迁移 -> {mid_p} | 余额:{raw_balance:.2f}U")
                grid_ids = [o[0] for o in all_orders if abs(float(o[6])) <= (self.max_pos_amount * 0.2)]
                if grid_ids: await self.bfx.cancel_orders(grid_ids)
                self.anchor_price = mid_p
                await asyncio.sleep(1)
                all_orders = [o for o in all_orders if o[0] not in grid_ids]

            # 3. 逐笔计算、单笔执行挂单 (0.5 价格容差)
            failed_due_to_margin = False
            for i in range(1, self.grid_levels + 1):
                if failed_due_to_margin: break
                
                # --- 多头部分 (40倍) ---
                p_buy = round(self.anchor_price * (1 - i * self.grid_spacing), 2)
                # ✨ 这里就是 0.2% 止盈的价格计算：买入价 * (1 + 0.002)
                p_tp_buy = round(p_buy * (1 + self.tp_pct), 2)
                amt_long = max(0.005, round((raw_balance * self.margin_utilization * self.leverage_long) / (self.grid_levels * p_buy), 4))
                
                # A. 检查并挂入买网格
                if not any(abs(float(o[16]) - p_buy) < 0.5 for o in all_orders):
                    if curr_pos < self.max_pos_amount:
                        self.logger().info(f"[网格-买] 🧱 在 {p_buy} 预埋一笔补货单: {amt_long}")
                        res = await self.bfx.create_order(self.bfx_symbol, amt_long, p_buy, lev=self.leverage_long, flags=4096)
                        if isinstance(res, dict) and res.get("margin_error"): 
                            self.logger().warning("[风控] ❌ 余额不足，无法继续挂买单")
                            failed_due_to_margin = True
                
                # B. 【核心改进】逐笔平仓单检测
                # 如果当前总仓位能够覆盖第 i 层（考虑 0.5 的误差范围），则给这一层配一个卖单
                if curr_pos >= (amt_long * (i - 0.5)):
                    if not any(abs(float(o[16]) - p_tp_buy) < 0.5 for o in all_orders):
                        self.logger().info(f"[逐笔-平多] 🎯 已侦测到第 {i} 层多单成交，正在 {p_tp_buy} 补齐对应平仓单: -{amt_long}")
                        await self.bfx.create_order(self.bfx_symbol, -amt_long, p_tp_buy, lev=self.leverage_long, flags=4096)

                # --- 空头部分 (15倍) ---
                p_sell = round(self.anchor_price * (1 + i * self.grid_spacing), 2)
                p_tp_sell = round(p_sell * (1 - self.tp_pct), 2)
                amt_short = -max(0.005, round((raw_balance * self.margin_utilization * self.leverage_short) / (self.grid_levels * p_sell), 4))
                
                if not any(abs(float(o[16]) - p_sell) < 0.5 for o in all_orders):
                    if curr_pos > -self.max_pos_amount:
                        self.logger().info(f"[网格-卖] 🧱 在 {p_sell} 预埋一笔为空单: {amt_short}")
                        res = await self.bfx.create_order(self.bfx_symbol, amt_short, p_sell, lev=self.leverage_short, flags=4096)
                        if isinstance(res, dict) and res.get("margin_error"): failed_due_to_margin = True
                
                # 为成交的空单补齐止盈
                if curr_pos <= (amt_short * (i - 0.5)):
                    if not any(abs(float(o[16]) - p_tp_sell) < 0.5 for o in all_orders):
                        self.logger().info(f"[逐笔-平空] 🎯 已侦测到第 {i} 层空单成交，正在 {p_tp_sell} 补齐对应平仓单: {-amt_short}")
                        await self.bfx.create_order(self.bfx_symbol, -amt_short, p_tp_sell, lev=self.leverage_short, flags=4096)

            self.logger().info(f"[心跳] ✅ 余额:{raw_balance:.2f}U | 持仓:{curr_pos:.4f} | 参考均价:{entry_p:.2f}")

        except Exception as e:
            self.logger().error(f"❌ 运行异常: {str(e)}")
        finally:
            self.is_executing = False

    def format_status(self) -> str:
        return f"逐笔复利版 2026_3_3 | 40X多/15X空 | 零手续费精准模式"

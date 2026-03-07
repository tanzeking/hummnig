import os
import json
import asyncio
import time
import hmac
import hashlib
import random
import aiohttp
from decimal import Decimal
from typing import Any, Dict, List, Optional
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase

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
                async with session.post(url, headers=headers, data=body, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    res_json = await resp.json()
                    if isinstance(res_json, list) and len(res_json) > 1 and res_json[0] == "error":
                        if "margin" in str(res_json[2]).lower(): return {"margin_error": True, "raw": res_json}
                        self.logger.warning(f"BFX API error: {endpoint} -> {res_json}")
                    return res_json
            except: return None

    async def get_derivatives_balance(self) -> float:
        resp = await self.request("/auth/r/wallets")
        if resp and isinstance(resp, list):
            for wtype in ("derivatives", "margin", "exchange"):
                for w in resp:
                    if isinstance(w, list) and len(w) >= 3 and w[0] == wtype and w[1] in ("USTF0", "USD", "UST", "USDT", "USDt"):
                        avail = w[4] if (len(w) > 4 and w[4] is not None) else w[2]
                        val = float(avail) if avail is not None else 0.0
                        self.logger.info(f"Balance: {w[0]} {w[1]} = {val:.4f}")
                        return val
        return 1.0

    async def get_mark_price(self, symbol: str) -> float:
        url = f"https://api-pub.bitfinex.com/v2/status/deriv?keys={symbol}"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=5) as resp:
                    data = await resp.json()
                    if isinstance(data, list) and len(data) > 0: return float(data[0][1])
            except: pass
        return 0.0

    async def get_positions(self): return await self.request("/auth/r/positions") or []
    async def get_open_orders(self, symbol):
        resp = await self.request("/auth/r/orders")
        return [o for o in resp if isinstance(o, list) and len(o) > 16 and o[3] == symbol] if isinstance(resp, list) else []

    async def create_order(self, symbol, amount, price=None, lev=30, type="LIMIT", flags=0):
        cid = int(time.time() * 1000) % (10 ** 13) + random.randint(0, 9999)
        req = {"type": type.upper(), "symbol": symbol, "amount": "{:.5f}".format(float(amount)), "lev": int(lev), "cid": cid, "flags": flags}
        if price: req["price"] = "{:.2f}".format(float(price))
        return await self.request("/auth/w/order/submit", req)

    async def cancel_orders(self, ids: List[int]):
        if ids: await self.request("/auth/w/order/cancel/multi", {"id": ids})
    async def cancel_all(self): await self.request("/auth/w/order/cancel/multi", {"all": 1})

class 一天量化2026_3_2(ScriptStrategyBase):
    markets = {"okx": {"ETH-USDT"}}
    bfx_symbol = "tETHF0:USTF0"
    leverage_long, leverage_short = 40.0, 15.0
    grid_levels, grid_spacing, tp_pct = 4, 0.001, 0.002
    max_pos_amount, check_interval, relocate_threshold = 0.16, 20, 0.003
    safety_buffer_usd, margin_utilization = 0.0, 0.35

    def __init__(self, connectors: Dict[str, Any]):
        super().__init__(connectors)
        self.anchor_price, self.last_check_time, self.is_executing = 0.0, 0, False
        self.bfx = BitfinexAPI("94a54e57696198788682c7e8c4b0d5adab9b69c70fa", "2c15f19dd463e312397d557d54531f63e5961a12da7", self.logger())

    async def on_stop(self): await self.bfx.cancel_all()
    def on_tick(self):
        if self.current_timestamp - self.last_check_time >= self.check_interval:
            if not self.is_executing:
                asyncio.ensure_future(self.maintain_strategy())
                self.last_check_time = self.current_timestamp

    async def maintain_strategy(self):
        self.is_executing = True
        try:
            mid_p = await self.bfx.get_mark_price(self.bfx_symbol)
            if mid_p <= 0: return
            raw_balance = await self.bfx.get_derivatives_balance()
            active_balance = max(0.0, raw_balance - self.safety_buffer_usd)
            if active_balance < 0.5:
                self.logger().warning(f"Balance critical({raw_balance:.2f}U), halting!")
                return
            positions = await self.bfx.get_positions()
            all_orders = await self.bfx.get_open_orders(self.bfx_symbol)
            curr_pos, entry_p = 0.0, 0.0
            for pos in positions:
                if isinstance(pos, list) and len(pos) > 3 and pos[0] == self.bfx_symbol:
                    curr_pos, entry_p = float(pos[2]), float(pos[3])
            
            if abs(curr_pos) > 0.0001:
                tp_p = round(entry_p * (1 + self.tp_pct if curr_pos > 0 else 1 - self.tp_pct), 2)
                tp_order = next((o for o in all_orders if abs(float(o[16]) - tp_p) < 0.5), None)
                if not tp_order or abs(float(tp_order[6]) + curr_pos) > 0.001:
                    if tp_order: await self.bfx.cancel_orders([tp_order[0]])
                    await self.bfx.create_order(self.bfx_symbol, -curr_pos, tp_p, lev=30)

            await self._cancel_stale_orders(all_orders, (entry_p * (1 + self.tp_pct if curr_pos > 0 else 1 - self.tp_pct)) if abs(curr_pos) > 0.0001 else 0.0)
            all_orders = await self.bfx.get_open_orders(self.bfx_symbol)

            if self.anchor_price <= 0 or abs(mid_p - self.anchor_price) > self.anchor_price * self.relocate_threshold:
                ids_to_cancel = [o[0] for o in all_orders if abs(curr_pos) < 0.0001 or abs(float(o[16]) - (entry_p * (1+self.tp_pct if curr_pos>0 else 1-self.tp_pct))) > 0.5]
                if ids_to_cancel: await self.bfx.cancel_orders(ids_to_cancel)
                self.anchor_price = mid_p
                all_orders = await self.bfx.get_open_orders(self.bfx_symbol)

            tolerance = self.anchor_price * self.grid_spacing * 0.3
            failed_buy, failed_sell = False, False
            for i in range(1, self.grid_levels + 1):
                await asyncio.sleep(0.15)
                if not failed_buy:
                    p_buy = round(self.anchor_price * (1 - i * self.grid_spacing), 2)
                    if curr_pos < self.max_pos_amount and not any(abs(float(o[16]) - p_buy) < tolerance for o in all_orders):
                        amt = max(0.005, round((active_balance * self.margin_utilization * self.leverage_long) / (self.grid_levels * p_buy), 4))
                        res = await self.bfx.create_order(self.bfx_symbol, amt, p_buy, lev=self.leverage_long, flags=4096)
                        if isinstance(res, dict) and res.get("margin_error"): failed_buy = True
                if not failed_sell:
                    p_sell = round(self.anchor_price * (1 + i * self.grid_spacing), 2)
                    if curr_pos > -self.max_pos_amount and not any(abs(float(o[16]) - p_sell) < tolerance for o in all_orders):
                        amt = -max(0.005, round((active_balance * self.margin_utilization * self.leverage_short) / (self.grid_levels * p_sell), 4))
                        res = await self.bfx.create_order(self.bfx_symbol, amt, p_sell, lev=self.leverage_short, flags=4096)
                        if isinstance(res, dict) and res.get("margin_error"): failed_sell = True
            self.logger().info(f"Done | Mark:{mid_p:.2f} | Bal:{raw_balance:.2f}U | Pos:{curr_pos:.4f}")
        except Exception as e: self.logger().error(f"Error: {str(e)}")
        finally: self.is_executing = False

    async def _cancel_stale_orders(self, all_orders, tp_p):
        if self.anchor_price <= 0: return
        g_max, g_min = self.anchor_price * (1 + (self.grid_levels + 0.5) * self.grid_spacing), self.anchor_price * (1 - (self.grid_levels + 0.5) * self.grid_spacing)
        stale = [o[0] for o in all_orders if (tp_p <= 0 or abs(float(o[16]) - tp_p) > 0.5) and not (g_min <= float(o[16]) <= g_max)]
        if stale: await self.bfx.cancel_orders(stale)

    def format_status(self) -> str: return f"v6.1 | Buffer:0U | Anchor:{self.anchor_price:.2f}"

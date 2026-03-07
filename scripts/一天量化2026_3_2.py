
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

# ==========================================
# === 🛡️ Bitfinex 智能保证金防御版 API v2 ===
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
        headers = {
            "bfx-nonce": nonce,
            "bfx-apikey": self.api_key,
            "bfx-signature": sig,
            "content-type": "application/json"
        }
        async with aiohttp.ClientSession() as session:
            # FIX [P2]: 分类处理异常，不再用裸 except 吞掉所有错误
            try:
                async with session.post(url, headers=headers, data=body,
                                        timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    res_json = await resp.json()
                    if isinstance(res_json, list) and len(res_json) > 1 and res_json[0] == "error":
                        if "margin" in str(res_json[2]).lower():
                            return {"margin_error": True, "raw": res_json}
                        self.logger.warning(f"⚠️ BFX API 错误: {endpoint} → {res_json}")
                    return res_json
            except asyncio.TimeoutError:
                self.logger.warning(f"⏱️ API 超时: {endpoint}")
                return None
            except aiohttp.ClientError as e:
                self.logger.error(f"🌐 网络错误: {endpoint} → {e}")
                return None
            except Exception as e:
                self.logger.error(f"❌ 未知错误: {endpoint} → {e}")
                return None

    async def get_derivatives_balance(self) -> float:
        resp = await self.request("/auth/r/wallets")
        if resp and isinstance(resp, list):
            for w in resp:
                if isinstance(w, list) and len(w) >= 5 and w[0] == "derivatives" and w[1] in ("USTF0", "USD"):
                    # FIX [P1]: w[4] 可能为 None，加保护
                    avail = w[4] if w[4] is not None else w[2]
                    return float(avail) if avail is not None else 0.0
        # FIX [P1]: 改为返回 0.0 而非 1.0，避免误判为有余额
        return 0.0

    async def get_mark_price(self, symbol: str) -> float:
        """
        FIX [P1]: 使用 Mark Price 替代 last_price 作为锚定价格
        Mark Price 基于多交易所均价，不受插针影响，防止误触发阵地迁移
        端点: GET /v2/status/deriv?keys=tETHF0:USTF0
        返回格式: [[symbol, mark_price, ...], ...]
        """
        url = f"https://api-pub.bitfinex.com/v2/status/deriv?keys={symbol}"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        row = data[0]
                        if isinstance(row, list) and len(row) > 1 and row[1]:
                            return float(row[1])
            except asyncio.TimeoutError:
                self.logger.warning("⏱️ Mark Price 获取超时")
            except Exception as e:
                self.logger.warning(f"⚠️ Mark Price 获取失败: {e}")
        return 0.0

    async def get_positions(self):
        return await self.request("/auth/r/positions") or []

    async def get_open_orders(self, symbol):
        resp = await self.request("/auth/r/orders")
        return [
            o for o in resp
            if isinstance(o, list) and len(o) > 16 and o[3] == symbol
        ] if isinstance(resp, list) else []

    async def create_order(self, symbol, amount, price=None, lev=30, type="LIMIT", flags=0):
        # FIX [P1]: cid 使用毫秒时间戳+随机数，确保 45 天内唯一，避免同价位碰撞
        cid = int(time.time() * 1000) % (10 ** 13) + random.randint(0, 9999)
        req = {
            "type": type.upper(),
            "symbol": symbol,
            "amount": "{:.5f}".format(float(amount)),
            "lev": int(lev),
            "cid": cid,
            "flags": flags
        }
        if price:
            req["price"] = "{:.2f}".format(float(price))
        return await self.request("/auth/w/order/submit", req)

    async def cancel_orders(self, ids: List[int]):
        if not ids:
            return
        return await self.request("/auth/w/order/cancel/multi", {"id": ids})

    async def cancel_all(self):
        return await self.request("/auth/w/order/cancel/multi", {"all": 1})


# ==========================================
# === 🚀 阵地保卫版 6.0 (全面修复版) ===
# ==========================================
class 一天量化2026_3_2(ScriptStrategyBase):
    markets = {"okx": {"ETH-USDT"}}
    bfx_symbol = "tETHF0:USTF0"

    leverage_long = 40.0
    leverage_short = 15.0
    grid_levels = 4
    grid_spacing = 0.001       # 0.1% 网格间距
    tp_pct = 0.002             # 0.2% 止盈
    sl_roi = 0.50

    max_pos_amount = 0.16
    # FIX [P0]: 从 8 → 20 秒，避免超过 Bitfinex 90 req/min Rate Limit
    check_interval = 20
    # 阵地迁移触发阈值（偏离锚点 0.3%）
    relocate_threshold = 0.003

    # 💥 安全防御参数
    # FIX [P1]: 从 5U → 15U，覆盖资金费率（每天 3 次）冲击
    safety_buffer_usd = 15.0
    margin_utilization = 0.35

    def __init__(self, connectors: Dict[str, Any]):
        super().__init__(connectors)
        # FIX [P2]: 实例变量而非类变量，避免多实例共享状态
        self.anchor_price = 0.0
        self.last_check_time = 0
        self.is_executing = False

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
            # ── 步骤 1: 获取 Mark Price（防插针误迁移）──
            mid_p = await self.bfx.get_mark_price(self.bfx_symbol)
            if mid_p <= 0:
                self.logger().warning("⚠️ Mark Price 获取失败，跳过本轮")
                return

            # ── 步骤 2: 获取账户状态 ──
            raw_balance = await self.bfx.get_derivatives_balance()
            active_balance = max(0.0, raw_balance - self.safety_buffer_usd)

            positions = await self.bfx.get_positions()
            all_orders = await self.bfx.get_open_orders(self.bfx_symbol)

            curr_pos = 0.0
            entry_p = 0.0
            for pos in positions:
                if isinstance(pos, list) and len(pos) > 3 and pos[0] == self.bfx_symbol:
                    curr_pos = float(pos[2])
                    entry_p = float(pos[3])

            # ── 步骤 3: 止盈单管理（最高优先级）──
            tp_p = 0.0
            if abs(curr_pos) > 0.0001:
                tp_p = round(
                    entry_p * (1 + self.tp_pct if curr_pos > 0 else 1 - self.tp_pct),
                    2
                )
                tp_order = next(
                    (o for o in all_orders if abs(float(o[16]) - tp_p) < 0.5), None
                )
                if not tp_order or abs(float(tp_order[6]) + curr_pos) > 0.001:
                    if tp_order:
                        await self.bfx.cancel_orders([tp_order[0]])
                    await self.bfx.create_order(
                        self.bfx_symbol, -curr_pos, tp_p, lev=30
                    )

            # ── 步骤 4: 熔断保护 ──
            if active_balance < 1.0:
                self.logger().warning(
                    f"⚠️ 余额告急({raw_balance:.2f}U)，已触发安全熔断，暂停补单！"
                )
                return

            # ── 步骤 5: 游荡单清理（每次 tick 都执行）──
            # FIX [P0]: 新增核心方法，清理超出网格范围的旧挂单
            await self._cancel_stale_orders(all_orders, tp_p)
            # FIX [P0]: 清理后重拉 API，不用本地缓存
            all_orders = await self.bfx.get_open_orders(self.bfx_symbol)

            # ── 步骤 6: 阵地迁移判断 ──
            need_relocate = (
                self.anchor_price <= 0
                or abs(mid_p - self.anchor_price) > self.anchor_price * self.relocate_threshold
            )

            if need_relocate:
                self.logger().info(
                    f"📍 阵地迁移 | {self.anchor_price:.2f} → {mid_p:.2f}"
                )
                # FIX [P0]: ids_to_cancel 改为按价格范围过滤（原代码用 o[6] 数量，完全错误）
                # 迁移时取消所有非止盈挂单
                ids_to_cancel = [
                    o[0] for o in all_orders
                    if tp_p <= 0 or abs(float(o[16]) - tp_p) > 0.5  # 排除止盈单
                ]
                if ids_to_cancel:
                    await self.bfx.cancel_orders(ids_to_cancel)
                    await asyncio.sleep(1.5)

                self.anchor_price = mid_p
                # FIX [P0]: 重新从 API 拉取，不用本地 list comprehension 模拟
                all_orders = await self.bfx.get_open_orders(self.bfx_symbol)

            # ── 步骤 7: 精准补单 ──
            # FIX [P1]: 动态价格容忍区间（步距的 30%），替代固定 0.2
            tolerance = self.anchor_price * self.grid_spacing * 0.3

            # FIX [P1]: 分别追踪买/卖 margin error，互不影响
            failed_buy = False
            failed_sell = False

            for i in range(1, self.grid_levels + 1):
                # FIX [P0]: 批量下单间加小间隔，避免 Rate Limit 爆发
                await asyncio.sleep(0.15)

                # 买单
                if not failed_buy:
                    p_buy = round(self.anchor_price * (1 - i * self.grid_spacing), 2)
                    already_exists = any(
                        abs(float(o[16]) - p_buy) < tolerance for o in all_orders
                    )
                    if curr_pos < self.max_pos_amount and not already_exists:
                        amt = max(
                            0.005,
                            round(
                                (active_balance * self.margin_utilization * self.leverage_long)
                                / (self.grid_levels * p_buy),
                                4
                            )
                        )
                        res = await self.bfx.create_order(
                            self.bfx_symbol, amt, p_buy,
                            lev=self.leverage_long, flags=4096
                        )
                        if isinstance(res, dict) and res.get("margin_error"):
                            self.logger().warning(f"💸 买单保证金不足 @ {p_buy:.2f}")
                            failed_buy = True

                # 卖单（FIX [P1]: 独立判断，不受买单 margin error 影响）
                if not failed_sell:
                    p_sell = round(self.anchor_price * (1 + i * self.grid_spacing), 2)
                    already_exists = any(
                        abs(float(o[16]) - p_sell) < tolerance for o in all_orders
                    )
                    if curr_pos > -self.max_pos_amount and not already_exists:
                        amt = -max(
                            0.005,
                            round(
                                (active_balance * self.margin_utilization * self.leverage_short)
                                / (self.grid_levels * p_sell),
                                4
                            )
                        )
                        res = await self.bfx.create_order(
                            self.bfx_symbol, amt, p_sell,
                            lev=self.leverage_short, flags=4096
                        )
                        if isinstance(res, dict) and res.get("margin_error"):
                            self.logger().warning(f"💸 卖单保证金不足 @ {p_sell:.2f}")
                            failed_sell = True

            self.logger().info(
                f"✅ 阵地完成 | Mark:{mid_p:.2f} | 余额:{raw_balance:.2f}U "
                f"| 持仓:{curr_pos:.4f} "
                f"| 买{'❌' if failed_buy else '✅'} 卖{'❌' if failed_sell else '✅'}"
            )

        except Exception as e:
            self.logger().error(f"❌ 系统异常: {str(e)}")
        finally:
            self.is_executing = False

    async def _cancel_stale_orders(self, all_orders: list, tp_p: float = 0.0):
        """
        FIX [P0]: 核心新增方法 — 清理超出当前网格范围的游荡单
        每次 tick 都运行，解决"远端积累旧挂单"问题。
        规则：价格超出 [anchor ± (levels+0.5) × spacing] 范围的非止盈单一律取消。
        """
        if self.anchor_price <= 0:
            return

        grid_max = self.anchor_price * (1 + (self.grid_levels + 0.5) * self.grid_spacing)
        grid_min = self.anchor_price * (1 - (self.grid_levels + 0.5) * self.grid_spacing)

        ids_to_cancel = []
        for o in all_orders:
            order_price = float(o[16])
            is_tp_order = tp_p > 0 and abs(order_price - tp_p) < 0.5
            if not is_tp_order and not (grid_min <= order_price <= grid_max):
                ids_to_cancel.append(o[0])
                self.logger().info(
                    f"🗑️ 游荡单: 价格={order_price:.2f} 超出范围 "
                    f"[{grid_min:.2f}, {grid_max:.2f}]"
                )

        if ids_to_cancel:
            await self.bfx.cancel_orders(ids_to_cancel)
            await asyncio.sleep(0.5)

    def format_status(self) -> str:
        return (
            f"阵地保卫战 6.0 [全面修复版] | "
            f"Mark Price 引导 | 游荡单自动清理 | "
            f"间距:{self.grid_spacing * 100:.1f}% | "
            f"锚点:{self.anchor_price:.2f} | "
            f"安全缓冲:{self.safety_buffer_usd}U | "
            f"轮询:{self.check_interval}s"
        )

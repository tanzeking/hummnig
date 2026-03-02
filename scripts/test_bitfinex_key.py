import os
import json
import asyncio
import time
import hmac
import hashlib
import aiohttp
from typing import Dict, Any

from hummingbot.strategy.script_strategy_base import ScriptStrategyBase

# 尝试从 .env 加载环境变量
env_paths = ["/home/hummingbot/.env", ".env"]
for env_path in env_paths:
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf8") as f:
            for line in f:
                if line.strip() and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip().strip("'\"")

class BitfinexTester:
    def __init__(self, api_key: str, api_secret: str, logger):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://api.bitfinex.com/v2"
        self.logger = logger

    async def test_auth(self):
        endpoint = "/auth/r/wallets"
        url = self.base_url + endpoint
        nonce = str(int(time.time() * 1000000))
        body = "{}"
        
        # 严格按照 Bitfinex V2 要求生成签名
        signature_payload = f"/api/v2{endpoint}{nonce}{body}"
        sig = hmac.new(self.api_secret.encode('utf8'), signature_payload.encode('utf8'), hashlib.sha384).hexdigest()
        
        headers = {
            "bfx-nonce": nonce,
            "bfx-apikey": self.api_key,
            "bfx-signature": sig,
            "content-type": "application/json"
        }
        
        self.logger.info(f"🔍 正在测试 API Key: {self.api_key[:10]}...")
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, headers=headers, data=body) as resp:
                    text = await resp.text()
                    if resp.status == 200:
                        data = json.loads(text)
                        self.logger.info("✅ API Key 有效！成功获取钱包数据。")
                        for w in data:
                            if w[0] == "margin":
                                self.logger.info(f"💰 钱包分账: {w[1]} | 总额: {w[2]} | 可用: {w[4]}")
                    else:
                        self.logger.error(f"❌ API Key 无效或验证失败! 状态码: {resp.status}")
                        self.logger.error(f"🔴 返回内容: {text}")
                        if "apikey: digest invalid" in text:
                            self.logger.error("💡 诊断建议: 签名不匹配。请检查 Secret 是否完整，或系统时间是否同步。")
                        elif "apikey: invalid" in text:
                            self.logger.error("💡 诊断建议: API Key 本身不存在或已被撤销。")
            except Exception as e:
                self.logger.error(f"🚨 请求发生异常: {e}")

class TestBitfinexKey(ScriptStrategyBase):
    """
    🛠️ Bitfinex API 连通性测试脚本
    """
    markets = {"okx": {"ETH-USDT"}}  # 依然需要一个连接器驱动

    def __init__(self, connectors: Dict[str, Any]):
        super().__init__(connectors)
        self.tested = False

    def on_tick(self):
        if not self.tested:
            self.tested = True
            self.logger().info("🚀 开始 API 连通性测试...")
            api_key = os.getenv("BITFINEX_API_KEY", "")
            api_secret = os.getenv("BITFINEX_API_SECRET", "")
            
            if not api_key or not api_secret:
                self.logger().error("❌ 错误: 未在 .env 中找到 BITFINEX_API_KEY 或 BITFINEX_API_SECRET")
                return

            tester = BitfinexTester(api_key, api_secret, self.logger())
            asyncio.ensure_future(tester.test_auth())

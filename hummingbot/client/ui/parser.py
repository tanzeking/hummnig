import argparse
from typing import TYPE_CHECKING, List

from hummingbot.client.command.connect_command import OPTIONS as CONNECT_OPTIONS
from hummingbot.exceptions import ArgumentParserError

if TYPE_CHECKING:
    from hummingbot.client.hummingbot_application import HummingbotApplication


class ThrowingArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ArgumentParserError(message)

    def exit(self, status=0, message=None):
        pass

    def print_help(self, file=None):
        pass

    @property
    def subparser_action(self):
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                return action

    @property
    def commands(self) -> List[str]:
        return list(self.subparser_action._name_parser_map.keys())

    def subcommands_from(self, top_level_command: str) -> List[str]:
        parser: argparse.ArgumentParser = self.subparser_action._name_parser_map.get(top_level_command)
        if parser is None:
            return []
        subcommands = parser._optionals._option_string_actions.keys()
        filtered = list(filter(lambda sub: sub.startswith("--"), subcommands))
        return filtered


def load_parser(hummingbot: "HummingbotApplication", command_tabs) -> ThrowingArgumentParser:
    parser = ThrowingArgumentParser(prog="", add_help=False)
    subparsers = parser.add_subparsers()

    connect_parser = subparsers.add_parser("connect", help="列出可用交易所并向其添加API密钥")
    connect_parser.add_argument("option", nargs="?", choices=CONNECT_OPTIONS, help="您要连接的交易所名称")
    connect_parser.set_defaults(func=hummingbot.connect)

    create_parser = subparsers.add_parser("create", help="创建一个新机器人")
    create_parser.add_argument("--script-config", dest="script_to_config", nargs="?", default=None, help="v2策略名称")
    create_parser.add_argument("--controller-config", dest="controller_name", nargs="?", default=None, help="控制器名称")
    create_parser.set_defaults(func=hummingbot.create)

    import_parser = subparsers.add_parser("import", help="通过加载配置文件导入现有机器人")
    import_parser.add_argument("file_name", nargs="?", default=None, help="配置文件名称")
    import_parser.set_defaults(func=hummingbot.import_command)

    help_parser = subparsers.add_parser("help", help="列出可用命令")
    help_parser.add_argument("command", nargs="?", default="all", help="输入相关命令 ")
    help_parser.set_defaults(func=hummingbot.help)

    balance_parser = subparsers.add_parser("balance", help="显示您在所有已连接交易所中的资产余额")
    balance_parser.add_argument("option", nargs="?", choices=["limit", "paper"], default=None,
                                help="余额配置选项")
    balance_parser.add_argument("args", nargs="*")
    balance_parser.set_defaults(func=hummingbot.balance)

    config_parser = subparsers.add_parser("config", help="显示当前机器人的配置")
    config_parser.add_argument("key", nargs="?", default=None, help="您要修改的参数名称")
    config_parser.add_argument("value", nargs="?", default=None, help="参数的新值")
    config_parser.set_defaults(func=hummingbot.config)

    start_parser = subparsers.add_parser("start", help="启动当前机器人")
    # start_parser.add_argument("--log-level", help="日志级别")
    start_parser.add_argument("--script", type=str, dest="script", help="脚本策略文件名")
    start_parser.add_argument("--conf", type=str, dest="conf", help="脚本配置文件名")

    start_parser.set_defaults(func=hummingbot.start)

    stop_parser = subparsers.add_parser('stop', help="停止当前机器人")
    stop_parser.set_defaults(func=hummingbot.stop)

    status_parser = subparsers.add_parser("status", help="获取当前机器人的市场状态")
    status_parser.add_argument("--live", default=False, action="store_true", dest="live", help="显示状态更新")
    status_parser.set_defaults(func=hummingbot.status)

    history_parser = subparsers.add_parser("history", help="查看当前机器人的历史表现")
    history_parser.add_argument("-d", "--days", type=float, default=0, dest="days",
                                help="查看过去多少天（可以是小数值）")
    history_parser.add_argument("-v", "--verbose", action="store_true", default=False,
                                dest="verbose", help="列出所有交易")
    history_parser.add_argument("-p", "--precision", default=None, type=int,
                                dest="precision", help="显示值的精度级别")
    history_parser.set_defaults(func=hummingbot.history)

    gateway_parser = subparsers.add_parser("gateway", help="网关服务器的快捷命令。")
    gateway_parser.set_defaults(func=hummingbot.gateway)
    gateway_subparsers = gateway_parser.add_subparsers()

    gateway_allowance_parser = gateway_subparsers.add_parser("allowance", help="检查以太坊连接器的代币余量")
    gateway_allowance_parser.add_argument("connector", nargs="?", default=None, help="以太坊连接器名称/类型 (如 uniswap/amm)")
    gateway_allowance_parser.set_defaults(func=hummingbot.gateway_allowance)

    gateway_approve_parser = gateway_subparsers.add_parser("approve", help="批准以太坊连接器使用代币")
    gateway_approve_parser.add_argument("connector", nargs="?", default=None, help="连接器名称/类型 (如 jupiter/router)")
    gateway_approve_parser.add_argument("token", nargs="?", default=None, help="要批准的代币符号 (如 WETH)")
    gateway_approve_parser.set_defaults(func=hummingbot.gateway_approve)

    gateway_balance_parser = gateway_subparsers.add_parser("balance", help="检查代币余额")
    gateway_balance_parser.add_argument("chain", nargs="?", default=None, help="链名称 (如 ethereum, solana)")
    gateway_balance_parser.add_argument("tokens", nargs="?", default=None, help="要检查的以逗号分隔的代币列表 (可选)")
    gateway_balance_parser.set_defaults(func=hummingbot.gateway_balance)

    gateway_config_parser = gateway_subparsers.add_parser("config", help="显示或更新配置")
    gateway_config_parser.add_argument("namespace", nargs="?", default=None, help="命名空间 (如 ethereum-mainnet, uniswap)")
    gateway_config_parser.add_argument("action", nargs="?", default=None, help="要执行的操作 (update)")
    gateway_config_parser.add_argument("args", nargs="*", help="其他参数: <path> <value> 用于直接更新")
    gateway_config_parser.set_defaults(func=hummingbot.gateway_config)

    gateway_connect_parser = gateway_subparsers.add_parser("connect", help="为一条链添加钱包")
    gateway_connect_parser.add_argument("chain", nargs="?", default=None, help="区块链 (如 ethereum, solana)")
    gateway_connect_parser.set_defaults(func=hummingbot.gateway_connect)

    gateway_cert_parser = gateway_subparsers.add_parser("generate-certs", help="创建 SSL 证书")
    gateway_cert_parser.set_defaults(func=hummingbot.generate_certs)

    gateway_list_parser = gateway_subparsers.add_parser("list", help="列出可用的连接器")
    gateway_list_parser.set_defaults(func=hummingbot.gateway_list)

    gateway_lp_parser = gateway_subparsers.add_parser("lp", help="管理流动性头寸")
    gateway_lp_parser.add_argument("connector", nargs="?", type=str, help="连接器名称/类型 (如 raydium/amm)")
    gateway_lp_parser.add_argument("action", nargs="?", type=str, choices=["add-liquidity", "remove-liquidity", "position-info", "collect-fees"], help="要执行的LP操作")
    gateway_lp_parser.add_argument("trading_pair", nargs="?", default=None, help="交易对 (如 WETH-USDC)")
    gateway_lp_parser.set_defaults(func=hummingbot.gateway_lp)

    gateway_ping_parser = gateway_subparsers.add_parser("ping", help="测试节点和链/网络状态")
    gateway_ping_parser.add_argument("chain", nargs="?", default=None, help="要测试的特定链 (可选)")
    gateway_ping_parser.set_defaults(func=hummingbot.gateway_ping)

    gateway_pool_parser = gateway_subparsers.add_parser("pool", help="查看或更新池信息")
    gateway_pool_parser.add_argument("connector", nargs="?", default=None, help="连接器名称/类型 (如 uniswap/amm)")
    gateway_pool_parser.add_argument("trading_pair", nargs="?", default=None, help="交易对 (如 ETH-USDC)")
    gateway_pool_parser.add_argument("action", nargs="?", default=None, help="要执行的操作 (update)")
    gateway_pool_parser.add_argument("args", nargs="*", help="其他参数: <address> 用于直接池更新")
    gateway_pool_parser.set_defaults(func=hummingbot.gateway_pool)

    gateway_swap_parser = gateway_subparsers.add_parser(
        "swap",
        help="交换代币")
    gateway_swap_parser.add_argument("connector", nargs="?", default=None,
                                     help="连接器名称/类型 (如 jupiter/router)")
    gateway_swap_parser.add_argument("args", nargs="*",
                                     help="参数: [base-quote] [side] [amount]. "
                                          "如果提供不全则进入交互模式。 "
                                          "例如: gateway swap uniswap ETH-USDC BUY 0.1")
    gateway_swap_parser.set_defaults(func=hummingbot.gateway_swap)

    gateway_token_parser = gateway_subparsers.add_parser("token", help="查看或更新代币信息")
    gateway_token_parser.add_argument("symbol_or_address", nargs="?", default=None, help="代币符号或地址")
    gateway_token_parser.add_argument("action", nargs="?", default=None, help="要执行的操作 (update)")
    gateway_token_parser.set_defaults(func=hummingbot.gateway_token)

    exit_parser = subparsers.add_parser("exit", help="退出并取消所有未完成的订单")
    exit_parser.add_argument("-f", "--force", action="store_true", help="强制退出而不取消未完成的订单",
                             default=False)
    exit_parser.set_defaults(func=hummingbot.exit)

    export_parser = subparsers.add_parser("export", help="导出安全信息")
    export_parser.add_argument("option", nargs="?", choices=("keys", "trades"), help="导出选项")
    export_parser.set_defaults(func=hummingbot.export)

    ticker_parser = subparsers.add_parser("ticker", help="显示当前订单簿的市场行情")
    ticker_parser.add_argument("--live", default=False, action="store_true", dest="live", help="显示行情更新")
    ticker_parser.add_argument("--exchange", type=str, dest="exchange", help="市场的交易所")
    ticker_parser.add_argument("--market", type=str, dest="market", help="订单簿的市场（交易对）")
    ticker_parser.set_defaults(func=hummingbot.ticker)

    mqtt_parser = subparsers.add_parser("mqtt", help="管理 MQTT 代理网桥")
    mqtt_subparsers = mqtt_parser.add_subparsers()
    mqtt_start_parser = mqtt_subparsers.add_parser("start", help="启动 MQTT 代理网桥")
    mqtt_start_parser.add_argument(
        "-t",
        "--timeout",
        default=30.0,
        type=float,
        dest="timeout",
        help="网桥连接超时"
    )
    mqtt_start_parser.set_defaults(func=hummingbot.mqtt_start)
    mqtt_stop_parser = mqtt_subparsers.add_parser("stop", help="停止 MQTT 网桥")
    mqtt_stop_parser.set_defaults(func=hummingbot.mqtt_stop)
    mqtt_restart_parser = mqtt_subparsers.add_parser("restart", help="重启 MQTT 网桥")
    mqtt_restart_parser.add_argument(
        "-t",
        "--timeout",
        default=30.0,
        type=float,
        dest="timeout",
        help="网桥连接超时"
    )
    mqtt_restart_parser.set_defaults(func=hummingbot.mqtt_restart)

    rate_parser = subparsers.add_parser('rate', help="显示给定交易对的汇率")
    rate_parser.add_argument("-p", "--pair", default=None,
                             dest="pair", help="您要获取汇率的市场交易对。")
    rate_parser.add_argument("-t", "--token", default=None,
                             dest="token", help="您要获取价值的代币。")
    rate_parser.set_defaults(func=hummingbot.rate)

    for name, command_tab in command_tabs.items():
        o_parser = subparsers.add_parser(name, help=command_tab.tab_class.get_command_help_message())
        for arg_name, arg_properties in command_tab.tab_class.get_command_arguments().items():
            o_parser.add_argument(arg_name, **arg_properties)
        o_parser.add_argument("-c", "--close", default=False, action="store_true", dest="close",
                              help=f"To close the {name} tab.")

    return parser

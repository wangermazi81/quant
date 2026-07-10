# -*- coding: utf-8 -*-
"""
券商抽象接口层
支持: 模拟盘(paper) / easytrader国内券商 / LongBridge / 自定义HTTP
"""
from abc import ABC, abstractmethod
import logging

logger = logging.getLogger(__name__)


class BrokerInterface(ABC):
    """所有券商实现必须继承此接口"""

    @abstractmethod
    def buy(self, code: str, price: float, shares: int, **kwargs) -> dict:
        """提交买入委托，返回 {"ok": bool, "msg": str, ...}"""

    @abstractmethod
    def sell(self, code: str, price: float, shares: int = 0, **kwargs) -> dict:
        """提交卖出委托"""

    @abstractmethod
    def get_positions(self) -> list:
        """返回当前持仓列表"""

    @abstractmethod
    def get_account(self) -> dict:
        """返回账户资金信息"""

    def get_orders(self, limit: int = 100) -> list:
        return []

    def get_trades(self, limit: int = 100) -> list:
        return []

    def get_stats(self) -> dict:
        return {}

    def reset(self, initial_capital: float = None) -> dict:
        return {"ok": False, "msg": "实盘账户不支持重置"}


class PaperBrokerWrapper(BrokerInterface):
    """包装 paper_broker.PaperBroker 以符合统一接口"""

    def __init__(self, user_id: int):
        from paper_broker import get_broker
        self._broker = get_broker(user_id)

    def buy(self, code: str, price: float, shares: int, **kwargs) -> dict:
        name   = kwargs.get("name", code)
        reason = kwargs.get("reason", "")
        return self._broker.submit_buy(code, name, price, shares, reason=reason)

    def sell(self, code: str, price: float, shares: int = 0, **kwargs) -> dict:
        reason = kwargs.get("reason", "手动卖出")
        return self._broker.submit_sell(code, price, sell_reason=reason)

    def get_positions(self, cur_prices=None) -> list:
        return self._broker.get_positions(cur_prices=cur_prices)

    def get_account(self) -> dict:
        return self._broker.get_account()

    def get_orders(self, limit: int = 100) -> list:
        return self._broker.get_orders(limit=limit)

    def get_trades(self, limit: int = 100) -> list:
        return self._broker.get_trades(limit=limit)

    def get_stats(self) -> dict:
        return self._broker.get_stats()

    def reset(self, initial_capital: float = None) -> dict:
        return self._broker.reset(initial_capital=initial_capital)


class EasyTraderBroker(BrokerInterface):
    """
    基于 easytrader 的国内券商接口。
    支持: 同花顺客户端(ths) / 通达信(tdx) / 国金(gj) / 银河(yh) / 华泰(ht) 等
    config 示例:
      {"broker": "ths", "account": "资金账号", "password": "交易密码"}
    """

    def __init__(self, config: dict):
        self._config = config
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import easytrader
                broker = self._config.get("broker", "ths")
                self._client = easytrader.use(broker)
                self._client.prepare(
                    user=self._config.get("account", ""),
                    password=self._config.get("password", ""),
                )
                logger.info(f"[EasyTrader] {broker} 连接成功")
            except Exception as e:
                logger.error(f"[EasyTrader] 连接失败: {e}")
                raise RuntimeError(f"EasyTrader 初始化失败: {e}")
        return self._client

    def buy(self, code: str, price: float, shares: int, **kwargs) -> dict:
        try:
            client = self._get_client()
            result = client.buy(code, price=price, amount=shares)
            return {"ok": True, "result": result}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def sell(self, code: str, price: float, shares: int = 0, **kwargs) -> dict:
        try:
            client = self._get_client()
            if shares <= 0:
                # 获取持仓数量
                pos_list = self.get_positions()
                pos = next((p for p in pos_list if p.get("code") == code), None)
                shares = pos.get("shares", 0) if pos else 0
            if shares <= 0:
                return {"ok": False, "msg": "无持仓"}
            result = client.sell(code, price=price, amount=shares)
            return {"ok": True, "result": result}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_positions(self) -> list:
        try:
            client = self._get_client()
            raw = client.position
            result = []
            for p in (raw if isinstance(raw, list) else []):
                result.append({
                    "code":       p.get("证券代码", p.get("code", "")),
                    "name":       p.get("证券名称", p.get("name", "")),
                    "shares":     int(p.get("股票余额", p.get("shares", 0))),
                    "cost_price": float(p.get("成本价", p.get("cost_price", 0))),
                    "cur_price":  float(p.get("最新价", p.get("cur_price", 0))),
                })
            return result
        except Exception as e:
            logger.error(f"[EasyTrader] get_positions: {e}")
            return []

    def get_account(self) -> dict:
        try:
            client = self._get_client()
            bal = client.balance
            if isinstance(bal, list):
                bal = bal[0] if bal else {}
            return {
                "cash":         float(bal.get("可用资金", 0)),
                "market_value": float(bal.get("证券市值", 0)),
                "equity":       float(bal.get("总资产", 0)),
            }
        except Exception as e:
            logger.error(f"[EasyTrader] get_account: {e}")
            return {"cash": 0, "market_value": 0, "equity": 0}


class LongBridgeBroker(BrokerInterface):
    """
    LongBridge (长桥) REST API 券商接口。
    config: {"app_key": "", "app_secret": "", "access_token": ""}
    """

    def __init__(self, config: dict):
        self._config = config

    def buy(self, code: str, price: float, shares: int, **kwargs) -> dict:
        try:
            from longbridge.openapi import TradeContext, Config, OrderType, OrderSide, TimeInForceType
            cfg = Config(
                app_key=self._config["app_key"],
                app_secret=self._config["app_secret"],
                access_token=self._config["access_token"],
            )
            ctx = TradeContext(cfg)
            resp = ctx.submit_order(
                symbol=f"{code}.SH" if code.startswith("6") else f"{code}.SZ",
                order_type=OrderType.LO,
                side=OrderSide.Buy,
                submitted_quantity=shares,
                submitted_price=str(price),
                time_in_force=TimeInForceType.Day,
            )
            return {"ok": True, "order_id": str(resp.order_id)}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def sell(self, code: str, price: float, shares: int = 0, **kwargs) -> dict:
        try:
            from longbridge.openapi import TradeContext, Config, OrderType, OrderSide, TimeInForceType
            cfg = Config(
                app_key=self._config["app_key"],
                app_secret=self._config["app_secret"],
                access_token=self._config["access_token"],
            )
            ctx = TradeContext(cfg)
            resp = ctx.submit_order(
                symbol=f"{code}.SH" if code.startswith("6") else f"{code}.SZ",
                order_type=OrderType.LO,
                side=OrderSide.Sell,
                submitted_quantity=shares,
                submitted_price=str(price),
                time_in_force=TimeInForceType.Day,
            )
            return {"ok": True, "order_id": str(resp.order_id)}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_positions(self) -> list:
        try:
            from longbridge.openapi import TradeContext, Config
            cfg = Config(
                app_key=self._config["app_key"],
                app_secret=self._config["app_secret"],
                access_token=self._config["access_token"],
            )
            ctx = TradeContext(cfg)
            positions = ctx.stock_positions()
            result = []
            for p in positions.channels:
                for pos in p.positions:
                    result.append({
                        "code":       pos.symbol.split(".")[0],
                        "name":       pos.symbol_name,
                        "shares":     int(pos.quantity),
                        "cost_price": float(pos.cost_price),
                        "cur_price":  float(pos.current_price),
                    })
            return result
        except Exception as e:
            logger.error(f"[LongBridge] get_positions: {e}")
            return []

    def get_account(self) -> dict:
        try:
            from longbridge.openapi import TradeContext, Config
            cfg = Config(
                app_key=self._config["app_key"],
                app_secret=self._config["app_secret"],
                access_token=self._config["access_token"],
            )
            ctx = TradeContext(cfg)
            acct = ctx.account_balance()
            if acct.list:
                a = acct.list[0]
                return {
                    "cash":         float(a.cash_infos[0].available_cash if a.cash_infos else 0),
                    "market_value": float(a.market_value),
                    "equity":       float(a.total_cash),
                }
        except Exception as e:
            logger.error(f"[LongBridge] get_account: {e}")
        return {"cash": 0, "market_value": 0, "equity": 0}


class CustomHttpBroker(BrokerInterface):
    """
    自定义 HTTP 券商接口（对接私有/企业内部交易系统）。
    config: {"base_url": "http://...", "token": "xxx"}
    """

    def __init__(self, config: dict):
        self._base_url = config.get("base_url", "").rstrip("/")
        self._token    = config.get("token", "")

    def _post(self, path: str, data: dict) -> dict:
        import urllib.request, json
        body = json.dumps(data).encode("utf-8")
        req  = urllib.request.Request(
            f"{self._base_url}{path}", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._token}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def _get(self, path: str) -> dict:
        import urllib.request, json
        req = urllib.request.Request(
            f"{self._base_url}{path}",
            headers={"Authorization": f"Bearer {self._token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def buy(self, code: str, price: float, shares: int, **kwargs) -> dict:
        try:
            return self._post("/order/buy", {"code": code, "price": price, "shares": shares})
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def sell(self, code: str, price: float, shares: int = 0, **kwargs) -> dict:
        try:
            return self._post("/order/sell", {"code": code, "price": price, "shares": shares})
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_positions(self) -> list:
        try:
            return self._get("/positions").get("data", [])
        except Exception:
            return []

    def get_account(self) -> dict:
        try:
            return self._get("/account").get("data", {})
        except Exception:
            return {"cash": 0, "market_value": 0, "equity": 0}


# ── 工厂函数 ──────────────────────────────────────────────────

def get_user_broker(user_id: int) -> BrokerInterface:
    """根据用户的券商配置返回对应的 BrokerInterface 实例"""
    try:
        import user_manager as um
        cfg = um.get_user_broker_config(user_id)
        broker_type = cfg.get("broker_type", "paper")
        config      = cfg.get("config", {})
    except Exception:
        broker_type = "paper"
        config = {}

    if broker_type == "paper":
        return PaperBrokerWrapper(user_id)
    elif broker_type == "easytrader":
        return EasyTraderBroker(config)
    elif broker_type == "longbridge":
        return LongBridgeBroker(config)
    elif broker_type == "custom":
        return CustomHttpBroker(config)
    else:
        logger.warning(f"未知券商类型 {broker_type}，降级为模拟盘")
        return PaperBrokerWrapper(user_id)

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import akshare as ak
import requests
import yaml
from requests import RequestException


LOGGER = logging.getLogger("a_share_monitor")
BASE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class StockQuote:
    code: str
    name: str
    price: float
    pct_chg: float
    high: float
    low: float
    amount: float

    @property
    def previous_close(self) -> float:
        base = 1 + self.pct_chg / 100
        if base <= 0:
            return 0.0
        return self.price / base

    @property
    def low_pct_chg(self) -> float:
        previous_close = self.previous_close
        if previous_close <= 0:
            return 0.0
        return (self.low - previous_close) / previous_close * 100

    @property
    def fade_from_high_pct(self) -> float:
        if self.high <= 0:
            return 0.0
        return (self.high - self.price) / self.high * 100

    @property
    def pull_from_low_pct(self) -> float:
        if self.low <= 0:
            return 0.0
        return (self.price - self.low) / self.low * 100


@dataclass(frozen=True)
class SectorQuote:
    key: str
    name: str
    pct_chg: float


def format_alert_body(reason: str, data: str, watch: str) -> str:
    return f"原因：{reason}\n数据：{data}\n看点：{watch}"


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_absolute():
        path = BASE_DIR / path
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        text = str(value).replace(",", "").replace("%", "").strip()
        if text in {"", "-", "None", "nan"}:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def stock_exchange_prefix(code: str) -> str:
    if code.startswith(("6", "9")):
        return f"sh{code}"
    return f"sz{code}"


def load_stock_quotes(codes: list[str]) -> dict[str, StockQuote]:
    spot_df = ak.stock_zh_a_spot_em()
    spot_df["代码"] = spot_df["代码"].astype(str).str.zfill(6)
    wanted = spot_df[spot_df["代码"].isin(codes)]

    quotes: dict[str, StockQuote] = {}
    for _, row in wanted.iterrows():
        code = str(row["代码"]).zfill(6)
        quotes[code] = StockQuote(
            code=code,
            name=str(row.get("名称", code)),
            price=to_float(row.get("最新价")),
            pct_chg=to_float(row.get("涨跌幅")),
            high=to_float(row.get("最高")),
            low=to_float(row.get("最低")),
            amount=to_float(row.get("成交额")),
        )
    return quotes


def load_sector_quotes(config: dict[str, Any]) -> dict[str, SectorQuote]:
    sector_df = ak.stock_board_concept_name_em()
    sector_df["板块名称"] = sector_df["板块名称"].astype(str)

    quotes: dict[str, SectorQuote] = {}
    for key, item in config["sectors"].items():
        if "eastmoney_board_name" not in item:
            continue
        board_name = item["eastmoney_board_name"]
        matched = sector_df[sector_df["板块名称"] == board_name]
        if matched.empty:
            LOGGER.warning("未找到板块：%s", board_name)
            continue
        row = matched.iloc[0]
        quotes[key] = SectorQuote(
            key=key,
            name=item.get("name", board_name),
            pct_chg=to_float(row.get("涨跌幅")),
        )
    return quotes


def load_index_quotes(config: dict[str, Any]) -> dict[str, SectorQuote]:
    index_df = ak.stock_zh_index_spot_em()
    index_df["代码"] = index_df["代码"].astype(str)

    quotes: dict[str, SectorQuote] = {}
    for key, item in config["sectors"].items():
        if "index_code" not in item:
            continue
        matched = index_df[index_df["代码"] == item["index_code"]]
        if matched.empty:
            LOGGER.warning("未找到指数：%s", item["index_code"])
            continue
        row = matched.iloc[0]
        quotes[key] = SectorQuote(
            key=key,
            name=item.get("name", str(row.get("名称", key))),
            pct_chg=to_float(row.get("涨跌幅")),
        )
    return quotes


def load_avg_amount(code: str, days: int) -> float:
    symbol = stock_exchange_prefix(code)
    hist_df = ak.stock_zh_a_daily(symbol=symbol, adjust="")
    if hist_df.empty:
        return 0.0

    amount_col = "amount" if "amount" in hist_df.columns else "成交额"
    recent = hist_df.tail(days)
    return float(recent[amount_col].astype(float).mean())


def get_stock_name(config: dict[str, Any], code: str) -> str:
    for stock in config["stocks"]:
        if stock["code"] == code:
            return stock.get("name", code)
    return code


def evaluate_rules(
    config: dict[str, Any],
    stocks: dict[str, StockQuote],
    sectors: dict[str, SectorQuote],
) -> list[dict[str, str]]:
    rules = config["rules"]
    alerts: list[dict[str, str]] = []

    for rule_name in ("founder_vs_robot", "wanxiang_vs_robot"):
        rule = rules.get(rule_name, {})
        if not rule.get("enabled", False):
            continue
        stock = stocks.get(rule["stock_code"])
        benchmark = sectors.get(rule["benchmark"])
        if not stock or not benchmark:
            continue
        diff = stock.pct_chg - benchmark.pct_chg
        if diff >= rule["outperform_pct"]:
            alerts.append(
                {
                    "key": rule_name,
                    "title": f"{stock.name}强于{benchmark.name}",
                    "body": format_alert_body(
                        "个股明显强于所属机器人板块，可能有独立资金或消息驱动。",
                        f"{stock.name} {stock.pct_chg:.2f}%，{benchmark.name} {benchmark.pct_chg:.2f}%，相对强 {diff:.2f} 个百分点。",
                        "观察是否继续放量、能否站稳分时均线，以及板块是否跟随扩散。",
                    ),
                }
            )

    rule = rules.get("robot_vs_sh_index", {})
    if rule.get("enabled", False):
        sector = sectors.get(rule["sector"])
        benchmark = sectors.get(rule["benchmark"])
        if sector and benchmark:
            diff = sector.pct_chg - benchmark.pct_chg
            if diff >= rule["outperform_pct"]:
                alerts.append(
                    {
                        "key": "robot_vs_sh_index",
                        "title": f"{sector.name}强于{benchmark.name}",
                        "body": format_alert_body(
                            "机器人板块相对大盘走强，说明资金可能在向这个方向集中。",
                            f"{sector.name} {sector.pct_chg:.2f}%，{benchmark.name} {benchmark.pct_chg:.2f}%，相对强 {diff:.2f} 个百分点。",
                            "优先看板块前排是否同步走强，避免只是一两只个股孤立表现。",
                        ),
                    }
                )

    rule = rules.get("robot_up_cpo_semiconductor_down", {})
    if rule.get("enabled", False):
        robot = sectors.get(rule["robot_sector"])
        weak = [sectors[key] for key in rule["weak_sectors"] if key in sectors]
        if robot and robot.pct_chg > 0 and weak and all(item.pct_chg < 0 for item in weak):
            weak_text = "，".join(f"{item.name} {item.pct_chg:.2f}%" for item in weak)
            alerts.append(
                {
                    "key": "robot_up_cpo_semiconductor_down",
                    "title": "机器人逆势强于科技线",
                    "body": format_alert_body(
                        "CPO/半导体偏弱时机器人仍上涨，可能出现科技内部资金切换。",
                        f"{robot.name} {robot.pct_chg:.2f}%，同时 {weak_text}。",
                        "观察机器人是否有持续性，尤其看方正、万向、绿的谐波等核心票是否配合。",
                    ),
                }
            )

    rule = rules.get("intraday_fade", {})
    if rule.get("enabled", False):
        for code in rule.get("stock_codes", []):
            stock = stocks.get(code)
            if stock and stock.fade_from_high_pct >= rule["fade_from_high_pct"]:
                alerts.append(
                    {
                        "key": f"intraday_fade:{code}",
                        "title": f"{stock.name}冲高回落",
                        "body": format_alert_body(
                            "股价从日内高点明显回落，短线资金可能出现分歧或兑现。",
                            f"最高 {stock.high:.2f}，现价 {stock.price:.2f}，较日内高点回落 {stock.fade_from_high_pct:.2f}%。",
                            "如果回落时放量，要警惕假突破；如果缩量回踩，可观察是否重新转强。",
                        ),
                    }
                )

    rule = rules.get("leader_limit_or_deep_drop", {})
    if rule.get("enabled", False):
        stock = stocks.get(rule["stock_code"])
        if stock and stock.pct_chg >= rule["limit_up_pct"]:
            alerts.append(
                {
                    "key": "leader_limit_up",
                    "title": f"{stock.name}接近涨停",
                    "body": format_alert_body(
                        "机器人核心标的接近涨停，可能对板块情绪有带动作用。",
                        f"{stock.name}当前涨跌幅 {stock.pct_chg:.2f}%。",
                        "观察同板块个股是否跟涨，以及涨停附近封单和开板情况。",
                    ),
                }
            )
        if stock and stock.pct_chg <= rule["deep_drop_pct"]:
            alerts.append(
                {
                    "key": "leader_deep_drop",
                    "title": f"{stock.name}深跌",
                    "body": format_alert_body(
                        "机器人核心标的跌幅较深，可能拖累板块风险偏好。",
                        f"{stock.name}当前涨跌幅 {stock.pct_chg:.2f}%。",
                        "观察跌幅是否扩散到其他核心票，避免在板块退潮时追高。",
                    ),
                }
            )

    rule = rules.get("founder_volume_spike", {})
    if rule.get("enabled", False):
        stock = stocks.get(rule["stock_code"])
        if stock:
            avg_amount = load_avg_amount(stock.code, int(rule["lookback_days"]))
            if avg_amount > 0 and stock.amount >= avg_amount * rule["amount_multiple"]:
                alerts.append(
                    {
                        "key": "founder_volume_spike",
                        "title": f"{stock.name}成交额放大",
                        "body": format_alert_body(
                            "成交额显著超过近期均值，说明资金关注度明显上升。",
                            f"成交额 {stock.amount / 100000000:.2f} 亿，过去{rule['lookback_days']}日均值 {avg_amount / 100000000:.2f} 亿，放大 {stock.amount / avg_amount:.2f} 倍。",
                            "结合涨跌幅判断是放量进攻还是放量分歧，重点看收盘能否维持强势。",
                        ),
                    }
                )

    rule = rules.get("abnormal_intraday_pullup", {})
    if rule.get("enabled", False):
        stock_codes = rule.get("stock_codes") or list(stocks.keys())
        for code in stock_codes:
            stock = stocks.get(code)
            if not stock:
                continue
            pulled_enough = stock.pull_from_low_pct >= rule["pull_from_low_pct"]
            now_strong = stock.pct_chg >= rule["min_current_pct"]
            low_was_weak = stock.low_pct_chg <= rule.get("max_low_pct", 100)
            if pulled_enough and now_strong and low_was_weak:
                alerts.append(
                    {
                        "key": f"abnormal_intraday_pullup:{code}",
                        "title": f"{stock.name}异常拉升",
                        "body": format_alert_body(
                            "股价从日内低点快速拉起，可能有资金主动回流或板块情绪修复。",
                            f"最低 {stock.low:.2f}，现价 {stock.price:.2f}，从低点拉起 {stock.pull_from_low_pct:.2f}%，当前涨跌幅 {stock.pct_chg:.2f}%。",
                            "观察拉升后是否横住不回落，以及机器人板块是否同步增强。",
                        ),
                    }
                )

    rule = rules.get("deep_water_rebound", {})
    if rule.get("enabled", False):
        for code in rule.get("stock_codes", []):
            stock = stocks.get(code)
            if not stock:
                continue
            was_deep = stock.low_pct_chg <= rule["low_pct_chg_below"]
            rebound = stock.pull_from_low_pct >= rule["rebound_from_low_pct"]
            recovered = stock.pct_chg >= rule["min_current_pct"]
            if was_deep and rebound and recovered:
                alerts.append(
                    {
                        "key": f"deep_water_rebound:{code}",
                        "title": f"{stock.name}深水拉起",
                        "body": format_alert_body(
                            "盘中曾明显走弱，但随后从低位拉回，说明承接资金开始出现。",
                            f"日内低点约 {stock.low_pct_chg:.2f}%，现涨跌幅 {stock.pct_chg:.2f}%，从低点拉起 {stock.pull_from_low_pct:.2f}%。",
                            "这类信号要看拉起后能否站稳，若随后再跌回低位，说明承接失败。",
                        ),
                    }
                )

    rule = rules.get("pool_volume_price_alert", {})
    if rule.get("enabled", False):
        for code, stock in stocks.items():
            avg_amount = load_avg_amount(code, int(rule["lookback_days"]))
            if avg_amount <= 0:
                continue
            amount_multiple = stock.amount / avg_amount
            if stock.pct_chg >= rule["min_pct_chg"] and amount_multiple >= rule["amount_multiple"]:
                alerts.append(
                    {
                        "key": f"pool_volume_price_alert:{code}",
                        "title": f"{stock.name}放量上涨",
                        "body": format_alert_body(
                            "股票池个股同时满足上涨和放量，可能进入资金观察区。",
                            f"涨跌幅 {stock.pct_chg:.2f}%，成交额 {stock.amount / 100000000:.2f} 亿，较过去{rule['lookback_days']}日均值放大 {amount_multiple:.2f} 倍。",
                            "优先看它是否带动同题材个股，而不是只看单票脉冲。",
                        ),
                    }
                )

    rule = rules.get("holding_underperform_robot", {})
    if rule.get("enabled", False):
        benchmark = sectors.get(rule["benchmark"])
        if benchmark and benchmark.pct_chg >= rule.get("sector_min_pct", 0):
            for code in rule.get("stock_codes", []):
                stock = stocks.get(code)
                if not stock:
                    continue
                diff = stock.pct_chg - benchmark.pct_chg
                if diff <= -abs(rule["underperform_pct"]):
                    alerts.append(
                        {
                            "key": f"holding_underperform_robot:{code}",
                            "title": f"{stock.name}弱于{benchmark.name}",
                            "body": format_alert_body(
                                "机器人板块走强但持仓个股明显不跟，需要区分板块逻辑和个股选择问题。",
                                f"{stock.name} {stock.pct_chg:.2f}%，{benchmark.name} {benchmark.pct_chg:.2f}%，相对弱 {abs(diff):.2f} 个百分点。",
                                "若连续出现该信号，不要简单解释成洗盘；重点看是否有同题材更强标的替代。",
                            ),
                        }
                    )

    rule = rules.get("pioneer_midfield_confirm", {})
    if rule.get("enabled", False):
        pioneer = stocks.get(rule["pioneer_code"])
        midfield = stocks.get(rule["midfield_code"])
        leader = stocks.get(rule.get("leader_code", ""))
        robot = sectors.get("robot")
        if pioneer and midfield and leader and robot:
            if (
                pioneer.pct_chg >= rule["min_pioneer_pct"]
                and midfield.pct_chg >= rule["min_midfield_pct"]
                and leader.pct_chg >= rule["min_leader_pct"]
                and robot.pct_chg >= rule["min_robot_pct"]
            ):
                alerts.append(
                    {
                        "key": "pioneer_midfield_confirm",
                        "title": "机器人先锋+中军共振",
                        "body": format_alert_body(
                            "方正电机、万向钱潮和绿的谐波同步转强，比单票异动更接近资金合力。",
                            f"{pioneer.name} {pioneer.pct_chg:.2f}%，{midfield.name} {midfield.pct_chg:.2f}%，{leader.name} {leader.pct_chg:.2f}%，{robot.name} {robot.pct_chg:.2f}%。",
                            "这是主线确认的积极信号，但仍需观察次日承接和板块扩散，不要因单日共振盲目加仓。",
                        ),
                    }
                )

    rule = rules.get("robot_vs_hot_tech", {})
    if rule.get("enabled", False):
        robot = sectors.get(rule["robot_sector"])
        competitors = [sectors[key] for key in rule.get("competitor_sectors", []) if key in sectors]
        if robot and competitors and robot.pct_chg >= rule.get("min_robot_pct", 0):
            best_competitor = max(competitors, key=lambda item: item.pct_chg)
            diff = robot.pct_chg - best_competitor.pct_chg
            if diff >= rule["outperform_pct"]:
                comp_text = "，".join(f"{item.name} {item.pct_chg:.2f}%" for item in competitors)
                alerts.append(
                    {
                        "key": "robot_vs_hot_tech",
                        "title": "机器人强于高位科技主线",
                        "body": format_alert_body(
                            "机器人强于CPO/半导体，可能出现从高位AI硬件向低位物理AI的资金切换。",
                            f"{robot.name} {robot.pct_chg:.2f}%，对比：{comp_text}。",
                            "观察这种相对强度能否连续出现；一次强只是轮动，连续强才可能是主线切换。",
                        ),
                    }
                )

    rule = rules.get("robot_pool_breadth", {})
    if rule.get("enabled", False):
        tag = rule.get("tag", "robot")
        robot_codes = [item["code"] for item in config.get("stocks", []) if tag in item.get("tags", [])]
        robot_quotes = [stocks[code] for code in robot_codes if code in stocks]
        if len(robot_quotes) >= rule.get("min_count", 1):
            positive = [item for item in robot_quotes if item.pct_chg > 0]
            positive_ratio = len(positive) / len(robot_quotes)
            avg_pct = sum(item.pct_chg for item in robot_quotes) / len(robot_quotes)
            if positive_ratio >= rule["min_positive_ratio"] and avg_pct >= rule["min_avg_pct"]:
                alerts.append(
                    {
                        "key": "robot_pool_breadth",
                        "title": "机器人股票池广度转强",
                        "body": format_alert_body(
                            "机器人股票池多数个股上涨，说明不只是单个核心票孤立表现。",
                            f"上涨家数 {len(positive)}/{len(robot_quotes)}，上涨比例 {positive_ratio:.0%}，平均涨幅 {avg_pct:.2f}%。",
                            "广度转强有利于板块延续；若核心强但广度弱，仍要警惕脉冲行情。",
                        ),
                    }
                )

    for rule_name, default_title in (
        ("high_standard_negative_feedback", "机器人高标负反馈"),
        ("leader_anchor_negative", "机器人核心锚负反馈"),
    ):
        rule = rules.get(rule_name, {})
        if not rule.get("enabled", False):
            continue
        robot = sectors.get(rule["robot_sector"])
        if not robot or robot.pct_chg < rule.get("robot_min_pct", -100):
            continue
        for code in rule.get("stock_codes", [rule.get("stock_code")]):
            if not code:
                continue
            stock = stocks.get(code)
            if stock and stock.pct_chg <= rule["stock_drop_pct"]:
                alerts.append(
                    {
                        "key": f"{rule_name}:{code}",
                        "title": f"{stock.name}{default_title}",
                        "body": format_alert_body(
                            "机器人板块未弱但关键情绪票明显走弱，说明短线接力资金可能退潮。",
                            f"{stock.name} {stock.pct_chg:.2f}%，{robot.name} {robot.pct_chg:.2f}%。",
                            "如果该信号扩散到多个核心票，要降低追高和加仓冲动；若仅高标退潮、低位和中军承接，则属于健康分化。",
                        ),
                    }
                )

    rule = rules.get("profit_guard", {})
    if rule.get("enabled", False):
        for code in rule.get("stock_codes", []):
            stock = stocks.get(code)
            if not stock:
                continue
            if stock.pct_chg >= rule["min_current_pct"] and stock.fade_from_high_pct >= rule["fade_from_high_pct"]:
                alerts.append(
                    {
                        "key": f"profit_guard:{code}",
                        "title": f"{stock.name}利润保护提醒",
                        "body": format_alert_body(
                            "个股仍处上涨状态，但已从日内高点明显回落，容易触发贪婪与不甘心。",
                            f"当前涨幅 {stock.pct_chg:.2f}%，较日内高点回落 {stock.fade_from_high_pct:.2f}%。",
                            "这不是卖出指令；它提醒你按计划考虑是否减压，而不是让情绪决定是否死拿。",
                        ),
                    }
                )

    rule = rules.get("late_day_strength", {})
    if rule.get("enabled", False):
        tz = ZoneInfo(config["app"].get("timezone", "Asia/Shanghai"))
        now = datetime.now(tz)
        after_time = dtime.fromisoformat(rule.get("after_time", "14:30"))
        robot = sectors.get("robot")
        if now.time() >= after_time and robot and robot.pct_chg >= rule.get("min_robot_pct", 0):
            strong_names = []
            for code in rule.get("stock_codes", []):
                stock = stocks.get(code)
                if stock and stock.pct_chg >= rule["min_stock_pct"]:
                    strong_names.append(f"{stock.name} {stock.pct_chg:.2f}%")
            if strong_names:
                alerts.append(
                    {
                        "key": "late_day_strength",
                        "title": "机器人尾盘仍保持强势",
                        "body": format_alert_body(
                            "14:30后核心票仍保持强势，说明日内资金没有明显退潮。",
                            f"{robot.name} {robot.pct_chg:.2f}%，强势票：" + "，".join(strong_names),
                            "适合收盘后复盘，不适合临近收盘冲动追高；重点看次日承接。",
                        ),
                    }
                )

    return alerts


def load_alert_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_alert_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def push_alert(config: dict[str, Any], title: str, body: str, dry_run: bool) -> None:
    if dry_run:
        LOGGER.info("[DRY-RUN] %s | %s", title, body)
        return

    provider = config["push"].get("provider", "bark")
    if provider == "bark":
        bark = config["push"]["bark"]
        key = os.getenv("BARK_KEY") or bark.get("key")
        if not key:
            LOGGER.warning("未配置 Bark key，跳过推送：%s", title)
            return
        server = bark.get("server", "https://api.day.app").rstrip("/")
        response = requests.post(
            f"{server}/{key}",
            json={"title": title, "body": body, "group": "A股机器人监控"},
            timeout=10,
        )
        response.raise_for_status()
        return

    if provider == "server_chan":
        key = os.getenv("SERVER_CHAN_KEY") or config["push"]["server_chan"].get("key")
        if not key:
            LOGGER.warning("未配置 Server酱 key，跳过推送：%s", title)
            return
        response = requests.post(
            f"https://sctapi.ftqq.com/{key}.send",
            data={"title": title, "desp": body},
            timeout=10,
        )
        response.raise_for_status()
        return

    raise ValueError(f"不支持的推送通道：{provider}")


def should_alert_once_per_day(state: dict[str, str], alert_key: str, now: datetime) -> bool:
    today = now.strftime("%Y-%m-%d")
    return state.get(alert_key) != today


def market_time(config: dict[str, Any], now: datetime) -> bool:
    app = config["app"]
    current = now.time()
    open_time = dtime.fromisoformat(app["market_open"])
    close_time = dtime.fromisoformat(app["market_close"])
    break_start = dtime.fromisoformat(app["midday_break_start"])
    break_end = dtime.fromisoformat(app["midday_break_end"])
    return open_time <= current <= close_time and not (break_start <= current < break_end)


def fetch_snapshot(config: dict[str, Any]) -> tuple[dict[str, StockQuote], dict[str, SectorQuote]]:
    codes = [stock["code"] for stock in config["stocks"]]
    stocks = load_stock_quotes(codes)
    sectors = load_sector_quotes(config)
    sectors.update(load_index_quotes(config))
    return stocks, sectors


def run_once(config: dict[str, Any], dry_run: bool) -> list[dict[str, str]]:
    tz = ZoneInfo(config["app"].get("timezone", "Asia/Shanghai"))
    now = datetime.now(tz)
    stocks, sectors = fetch_snapshot(config)
    alerts = evaluate_rules(config, stocks, sectors)

    state_path = resolve_project_path(config["app"]["alert_state_file"])
    state = load_alert_state(state_path)
    for alert in alerts:
        alert_key = f"{now:%Y-%m-%d}:{alert['key']}"
        if not should_alert_once_per_day(state, alert_key, now):
            continue
        push_alert(config, alert["title"], alert["body"], dry_run=dry_run)
        state[alert_key] = now.strftime("%Y-%m-%d")

    save_alert_state(state_path, state)
    LOGGER.info("本轮检查完成：股票 %d 只，板块/指数 %d 个，触发 %d 条。", len(stocks), len(sectors), len(alerts))
    return alerts


def generate_review(config: dict[str, Any]) -> Path:
    tz = ZoneInfo(config["app"].get("timezone", "Asia/Shanghai"))
    now = datetime.now(tz)
    stocks, sectors = fetch_snapshot(config)
    review_dir = resolve_project_path(config["app"]["review_dir"])
    review_dir.mkdir(parents=True, exist_ok=True)
    path = review_dir / f"{now:%Y-%m-%d}.md"

    lines = [
        f"# A股机器人板块复盘 {now:%Y-%m-%d}",
        "",
        "## 板块/指数",
    ]
    for item in sectors.values():
        lines.append(f"- {item.name}: {item.pct_chg:.2f}%")

    lines.extend(["", "## 股票池"])
    for stock_cfg in config["stocks"]:
        code = stock_cfg["code"]
        quote = stocks.get(code)
        if not quote:
            lines.append(f"- {stock_cfg.get('name', code)}({code}): 未获取到行情")
            continue
        lines.append(
            f"- {quote.name}({quote.code}): {quote.pct_chg:.2f}%，现价 {quote.price:.2f}，成交额 {quote.amount / 100000000:.2f} 亿，冲高回落 {quote.fade_from_high_pct:.2f}%，深水拉起 {quote.pull_from_low_pct:.2f}%"
        )

    lines.extend(["", "## 今日信号"])
    alerts = evaluate_rules(config, stocks, sectors)
    if alerts:
        for alert in alerts:
            lines.append(f"- {alert['title']}: {alert['body']}")
    else:
        lines.append("- 未触发配置中的规则。")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_loop(config: dict[str, Any], dry_run: bool) -> None:
    interval = int(config["app"]["poll_interval_seconds"])
    tz = ZoneInfo(config["app"].get("timezone", "Asia/Shanghai"))
    LOGGER.info("开始监控，每 %d 秒检查一次。", interval)
    while True:
        now = datetime.now(tz)
        if market_time(config, now):
            try:
                run_once(config, dry_run=dry_run)
            except Exception:
                LOGGER.exception("本轮检查失败")
        else:
            LOGGER.info("非交易时间，等待下一轮。")
        time.sleep(interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A股机器人板块监控助手")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="只运行一次")
    parser.add_argument("--review", action="store_true", help="生成每日复盘")
    parser.add_argument("--test-push", action="store_true", help="发送一条 Bark/Server酱测试通知")
    parser.add_argument("--dry-run", action="store_true", help="只打印提醒，不发送手机推送")
    return parser.parse_args()


def resolve_project_path(path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return BASE_DIR / path


def main() -> int:
    setup_logging()
    args = parse_args()
    config = load_config(Path(args.config))

    if args.test_push:
        push_alert(
            config,
            "A股机器人监控测试",
            "如果你看到这条消息，说明手机推送通道正常。",
            dry_run=args.dry_run,
        )
        return 0

    if args.review:
        path = generate_review(config)
        LOGGER.info("复盘已生成：%s", path)
        return 0

    if args.once:
        try:
            run_once(config, dry_run=args.dry_run)
            return 0
        except RequestException as exc:
            LOGGER.error("行情接口访问失败：%s", exc)
            return 2

    run_loop(config, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

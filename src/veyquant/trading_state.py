"""Owner-only, bounded execution heartbeat and public completed daily bars."""

from datetime import datetime
from zoneinfo import ZoneInfo

from veyquant.account_readiness import amount
from veyquant.shadow_contract import bounded_json, finite_time

KST = ZoneInfo("Asia/Seoul")
MESSAGES = {
    "universe_unavailable": "국내 종목 목록을 다시 확인하는 동안 신규 매수를 기다립니다.",
    "instrument_unavailable": "이 종목의 거래 가능 여부를 확인하고 있습니다.",
    "instrument_restricted": "거래정지·매매 유의사항이 있어 이 종목의 주문을 보류했습니다.",
    "subscription_capacity": "보유 종목이 실시간 관찰 한도에 도달해 신규 종목 매수를 기다립니다.",
    "disabled": "실거래를 시작하면 설정한 한도 안에서 자동 주문합니다.",
    "ready": "실거래 준비가 완료되었습니다.",
    "active": "설정한 한도 안에서 자동 운용 중입니다.",
    "market_closed": "거래 시간에 자동으로 판단과 주문을 이어갑니다.",
    "stopped": "신규 판단과 주문을 일시 정지했습니다.",
    "control_unavailable": "설정 연결을 확인하는 동안 주문을 멈춥니다.",
    "account_unavailable": "토스 계좌를 다시 확인하고 있습니다.",
    "external_orders": "토스의 다른 미체결·조건 주문이 있어 자동 주문을 기다립니다.",
    "position_mismatch": "보유 수량이 AI 운용 기록과 다릅니다. 토스 거래내역을 확인해주세요.",
    "order_review": "결과가 확인되지 않은 주문이 있습니다. 주문내역 연결이 필요합니다.",
    "daily_loss_limit": "오늘의 손실 한도에 도달해 신규 주문을 멈췄습니다.",
    "daily_baseline_required": "전일 종가와 이전 주문을 대조하고 있습니다.",
    "stale_price": "최신 시세를 기다리고 있습니다.",
    "model_setup_required": "선택한 AI 모델의 연결을 완료해주세요.",
    "onboarding_required": "AI 모델과 운용 한도를 설정해주세요.",
    "reconciliation_required": "주문과 보유 수량을 대조하고 있습니다.",
    "waiting_for_analysis": "새로운 AI 판단을 기다리고 있습니다.",
    "no_quantity": "이번 판단으로 주문할 수 있는 수량이 없습니다.",
    "report_expired": "판단 이후 시간이 지나 다음 분석을 기다립니다.",
    "price_changed": "분석 이후 가격이 달라져 다음 판단을 기다립니다.",
    "settings_changed": "새 설정으로 작성한 판단을 기다립니다.",
    "order_pending": "주문 체결을 확인하고 있습니다.",
    "order_submitted": "주문을 접수하고 체결을 확인하고 있습니다.",
    "provider_hold": "AI가 이번에는 주문을 보류했습니다.",
}


def read_execution(path, now):
    empty = {
        "state": "account_unavailable",
        "message": MESSAGES["account_unavailable"],
        "available": False,
        "ready": False,
        "live_enabled": False,
        "orders": [],
    }
    if not path:
        return empty
    try:
        data = bounded_json(path, 65536)
        if not finite_time(data["updated_at"]) or not 0 <= now - data["updated_at"] <= 15:
            return empty
        if data["state"] not in MESSAGES or type(data["ready"]) is not bool:
            return empty
        if type(data["live_enabled"]) is not bool or not isinstance(data["orders"], list):
            return empty
        if len(data["orders"]) > 30:
            return empty
        return data | {"available": True, "message": MESSAGES[data["state"]]}
    except (OSError, ValueError, TypeError, KeyError):
        return empty


def completed_bars(response, now):
    today = datetime.fromtimestamp(now, KST).date()
    rows = response["candles"]
    if not isinstance(rows, list) or len(rows) > 30:
        raise ValueError("invalid_candles")
    result = []
    for row in rows:
        dt = datetime.fromisoformat(row["timestamp"])
        if dt.tzinfo is None or row["currency"] != "KRW":
            raise ValueError("invalid_candle_scope")
        if dt.astimezone(KST).date() >= today:
            continue  # Today's unfinished daily candle cannot be evidence or a loss baseline.
        prices = {k: amount(row[k + "Price"]) for k in ("open", "high", "low", "close")}
        if (
            not 0
            < prices["low"]
            <= min(prices["open"], prices["close"])
            <= max(prices["open"], prices["close"])
            <= prices["high"]
        ):
            raise ValueError("invalid_ohlc")
        volume = amount(row["volume"])
        if volume != volume.to_integral_value():
            raise ValueError("invalid_volume")
        result.append(
            {
                "date": dt.astimezone(KST).date().isoformat(),
                **{k: str(v) for k, v in prices.items()},
                "volume": str(int(volume)),
            }
        )
    result.sort(key=lambda r: r["date"])
    if len({r["date"] for r in result}) != len(result):
        raise ValueError("duplicate_candle")
    return result[-24:]


def valid_bar_evidence(data, now):
    if set(data) != {"updated_at", "bars"} or not finite_time(data["updated_at"]):
        raise ValueError("invalid_bar_evidence")
    if not 0 <= now - data["updated_at"] <= 7200:
        raise ValueError("stale_bar_evidence")
    bars = data["bars"]
    if not isinstance(bars, list) or not 5 <= len(bars) <= 24:
        raise ValueError("insufficient_daily_bars")
    previous = ""
    today = datetime.fromtimestamp(now, KST).date().isoformat()
    for r in bars:
        if set(r) != {"date", "open", "high", "low", "close", "volume"}:
            raise ValueError("invalid_bar")
        datetime.strptime(r["date"], "%Y-%m-%d")
        if not previous < r["date"] < today:
            raise ValueError("invalid_bar_date")
        previous = r["date"]
        a = [amount(r[k]) for k in ("open", "high", "low", "close", "volume")]
        if (
            not 0 < a[2] <= min(a[0], a[3]) <= max(a[0], a[3]) <= a[1]
            or a[4] != a[4].to_integral_value()
        ):
            raise ValueError("invalid_bar_amount")
    age = datetime.fromtimestamp(now, KST).date() - datetime.strptime(previous, "%Y-%m-%d").date()
    if age.days > 10:
        raise ValueError("stale_daily_bars")
    return data

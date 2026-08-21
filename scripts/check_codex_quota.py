#!/usr/bin/env python3
"""Read-only Codex quota estimator based on ChatGPT workspace usage data."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


BASE_URL = "https://chatgpt.com"
ALLOWED_HOST = "chatgpt.com"
DEFAULT_DAYS = 21
USD_PER_CREDIT = 0.04

# Credits per one million tokens. These values mirror the public Codex rate card.
RATE_CARD: dict[str, dict[str, float]] = {
    "gpt-5.6-sol": {"input": 125.0, "cached": 12.5, "output": 750.0},
    "gpt-5.6-terra": {"input": 50.0, "cached": 5.0, "output": 300.0},
    "gpt-5.6-luna": {"input": 5.0, "cached": 0.5, "output": 30.0},
    "gpt-5.5": {"input": 125.0, "cached": 12.5, "output": 750.0},
    "daybreak-blue": {"input": 125.0, "cached": 12.5, "output": 750.0},
    "daybreak-red": {"input": 312.5, "cached": 31.25, "output": 1875.0},
    "gpt-5.5-cyber": {"input": 312.5, "cached": 31.25, "output": 1875.0},
    "gpt-5.4": {"input": 62.5, "cached": 6.25, "output": 375.0},
    "gpt-5.4-mini": {"input": 18.75, "cached": 1.875, "output": 113.0},
    "gpt-5.3-codex": {"input": 43.75, "cached": 4.375, "output": 350.0},
    "gpt-5.2": {"input": 43.75, "cached": 4.375, "output": 350.0},
}

MODEL_ALIASES = {
    "gpt-5.6": "gpt-5.6-sol",
    "codex-auto-review": "gpt-5.4",
    "auto-review": "gpt-5.4",
    "codex-code-review": "gpt-5.3-codex",
    "code-review": "gpt-5.3-codex",
}


class QuotaError(RuntimeError):
    pass


def number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def token_parts(payload: dict[str, Any] | None) -> dict[str, float]:
    source = payload if isinstance(payload, dict) else {}
    uncached = number(source.get("uncached_text_input_tokens"))
    cached = number(source.get("cached_text_input_tokens"))
    output = number(source.get("text_output_tokens"))
    detailed = uncached + cached + output
    total = number(source.get("text_total_tokens")) or detailed
    return {
        "uncached": uncached,
        "cached": cached,
        "output": output,
        "detailed": detailed,
        "total": total,
    }


def raw_model(row: dict[str, Any]) -> str:
    return str(
        row.get("model") or row.get("model_name") or row.get("model_id") or "unknown"
    ).strip().lower()


def normalized_model(model: str) -> str | None:
    raw = model.strip().lower()
    if raw in MODEL_ALIASES:
        return MODEL_ALIASES[raw]
    if raw in RATE_CARD:
        return raw
    for candidate in RATE_CARD:
        if raw.startswith(f"{candidate}-20"):
            return candidate
    return None


def speed_for(row: dict[str, Any]) -> str:
    if row.get("fast_mode") is True:
        return "fast"
    value = str(
        row.get("speed")
        or row.get("service_tier")
        or row.get("processing_mode")
        or "standard"
    ).lower()
    return "fast" if "fast" in value else "standard"


def fast_multiplier(model: str, speed: str) -> float:
    if speed != "fast":
        return 1.0
    if model in {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"}:
        return 2.5
    if model == "gpt-5.4":
        return 2.0
    return 1.0


def price_model(row: dict[str, Any]) -> dict[str, Any]:
    model_raw = raw_model(row)
    model = normalized_model(model_raw)
    tokens = token_parts(row)
    speed = speed_for(row)
    if model is None:
        return {
            "priced": False,
            "model": model_raw,
            "tokens": tokens,
            "reason": "未知模型",
        }
    if tokens["total"] > 0 and tokens["detailed"] == 0:
        return {
            "priced": False,
            "model": model_raw,
            "tokens": tokens,
            "reason": "缺少 Token 类型拆分",
        }
    rate = RATE_CARD[model]
    multiplier = fast_multiplier(model, speed)
    credits = (
        tokens["uncached"] / 1_000_000 * rate["input"] * multiplier
        + tokens["cached"] / 1_000_000 * rate["cached"] * multiplier
        + tokens["output"] / 1_000_000 * rate["output"] * multiplier
    )
    return {
        "priced": True,
        "model": model_raw,
        "pricing_model": model,
        "speed": speed,
        "multiplier": multiplier,
        "tokens": tokens,
        "credits": credits,
    }


@dataclass(frozen=True)
class Window:
    used_percent: float
    seconds: int
    reset_at: int

    @property
    def remaining_percent(self) -> float:
        return max(0.0, 100.0 - self.used_percent)

    @property
    def reset_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.reset_at).astimezone()

    @property
    def start_datetime(self) -> datetime:
        return self.reset_datetime - timedelta(seconds=self.seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读检查 Codex 周额度，并按 Credits 粗略反推总盘子。"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"读取最近多少个自然日的明细，默认 {DEFAULT_DAYS}",
    )
    parser.add_argument(
        "--force-days",
        type=int,
        default=7,
        help="强制窗口覆盖多少个自然日，默认 7",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--auth-file",
        type=Path,
        help="Codex auth.json 路径，默认读取 CODEX_HOME/auth.json 或 ~/.codex/auth.json",
    )
    return parser.parse_args()


def auth_path(explicit: Path | None) -> Path:
    if explicit:
        return explicit.expanduser()
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def load_credentials(path: Path) -> tuple[str, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise QuotaError(f"找不到登录文件 {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise QuotaError(f"无法读取登录文件 {path}") from exc

    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    if not isinstance(tokens, dict):
        raise QuotaError("登录文件里没有 tokens 字段")
    access_token = tokens.get("access_token")
    account_id = tokens.get("account_id")
    if not isinstance(access_token, str) or not access_token:
        raise QuotaError("登录文件里没有可用的 access_token")
    if not isinstance(account_id, str) or not account_id:
        account_id = None
    return access_token, account_id


def api_get(path: str, token: str, account_id: str | None) -> dict[str, Any]:
    url = urllib.parse.urljoin(BASE_URL, path)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
        raise QuotaError("安全检查失败，拒绝访问 chatgpt.com 以外的主机")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "codex-quota-compass/1.0",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise QuotaError("登录已失效或当前账号无权读取用量，请先在 Codex 重新登录") from exc
        raise QuotaError(f"用量接口返回 HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise QuotaError(f"读取用量接口失败 {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise QuotaError("用量接口返回了无法识别的数据")
    return data


def api_get_optional(
    path: str, token: str, account_id: str | None
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return api_get(path, token, account_id), None
    except QuotaError as exc:
        return None, str(exc)


def weekly_window(usage: dict[str, Any]) -> Window:
    rate_limit = usage.get("rate_limit")
    if not isinstance(rate_limit, dict):
        raise QuotaError("接口没有返回通用额度窗口")
    candidates: list[dict[str, Any]] = []
    for key in ("primary_window", "secondary_window"):
        value = rate_limit.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    if not candidates:
        raise QuotaError("接口没有返回可用的额度窗口")
    chosen = max(candidates, key=lambda item: float(item.get("limit_window_seconds") or 0))
    try:
        return Window(
            used_percent=float(chosen["used_percent"]),
            seconds=int(chosen["limit_window_seconds"]),
            reset_at=int(chosen["reset_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise QuotaError("额度窗口字段不完整") from exc


def calculate_day_credits(
    count_row: dict[str, Any] | None,
    breakdown_row: dict[str, Any] | None,
) -> dict[str, Any]:
    count_totals = count_row.get("totals") if isinstance(count_row, dict) else None
    count_totals = count_totals if isinstance(count_totals, dict) else {}
    count_tokens = token_parts(count_totals)
    reported_raw = count_totals.get("credits")
    reported_available = (
        isinstance(reported_raw, (int, float))
        and not isinstance(reported_raw, bool)
        and (float(reported_raw) > 0 or count_tokens["total"] == 0)
    )

    models = breakdown_row.get("models") if isinstance(breakdown_row, dict) else None
    models = models if isinstance(models, list) else []
    calculated_credits = 0.0
    priced_tokens = 0.0
    model_tokens = 0.0
    issues: list[dict[str, Any]] = []
    breakdown_parts = {"uncached": 0.0, "cached": 0.0, "output": 0.0, "total": 0.0}

    for item in models:
        if not isinstance(item, dict):
            continue
        priced = price_model(item)
        parts = priced["tokens"]
        model_tokens += parts["total"]
        for key in breakdown_parts:
            breakdown_parts[key] += parts[key]
        if priced["priced"]:
            calculated_credits += number(priced.get("credits"))
            priced_tokens += parts["total"]
        elif parts["total"] > 0:
            issues.append(
                {
                    "model": priced["model"],
                    "reason": priced["reason"],
                    "tokens": int(round(parts["total"])),
                }
            )

    total_tokens = count_tokens["total"] or model_tokens
    resolved_tokens = {
        "uncached": count_tokens["uncached"] or breakdown_parts["uncached"],
        "cached": count_tokens["cached"] or breakdown_parts["cached"],
        "output": count_tokens["output"] or breakdown_parts["output"],
        "total": total_tokens,
    }
    if reported_available:
        return {
            "credits": float(reported_raw),
            "source": "API",
            "complete": True,
            "coverage": 100.0,
            "covered_tokens": total_tokens,
            "total_tokens": total_tokens,
            "tokens": resolved_tokens,
            "issues": issues,
        }

    coverage = min(100.0, priced_tokens / total_tokens * 100) if total_tokens else 100.0
    complete = coverage >= 99.5 and not issues and (bool(models) or total_tokens == 0)
    if not models and total_tokens > 0:
        issues.append(
            {
                "model": "unknown",
                "reason": "按模型 Token Breakdown 不可用",
                "tokens": int(round(total_tokens)),
            }
        )
    return {
        "credits": calculated_credits,
        "source": "Rate Card" if complete else "Rate Card（部分）",
        "complete": complete,
        "coverage": coverage,
        "covered_tokens": min(priced_tokens, total_tokens),
        "total_tokens": total_tokens,
        "tokens": resolved_tokens,
        "issues": issues,
    }


def daily_rows(
    token: str, account_id: str | None, days: int
) -> tuple[list[dict[str, Any]], list[str]]:
    if days < 1 or days > 180:
        raise QuotaError("--days 必须在 1 到 180 之间")
    today = date.today()
    end = today + timedelta(days=1)
    start = today - timedelta(days=days - 1)
    query = urllib.parse.urlencode(
        {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "group_by": "day",
            "workspace_user": "true",
        }
    )
    counts_payload = api_get(
        f"/backend-api/wham/analytics/daily-workspace-usage-counts?{query}",
        token,
        account_id,
    )
    count_rows = counts_payload.get("data")
    if not isinstance(count_rows, list):
        raise QuotaError("日用量接口没有返回 data 列表")

    needs_breakdown = False
    for row in count_rows:
        totals = row.get("totals") if isinstance(row, dict) else None
        totals = totals if isinstance(totals, dict) else {}
        parts = token_parts(totals)
        raw_credits = totals.get("credits")
        has_credits = (
            isinstance(raw_credits, (int, float))
            and not isinstance(raw_credits, bool)
            and (float(raw_credits) > 0 or parts["total"] == 0)
        )
        if parts["total"] > 0 and not has_credits:
            needs_breakdown = True
            break

    breakdown_payload: dict[str, Any] | None = None
    breakdown_error: str | None = None
    if needs_breakdown:
        breakdown_query = urllib.parse.urlencode(
            {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "group_by": "day",
            }
        )
        breakdown_payload, breakdown_error = api_get_optional(
            "/backend-api/wham/usage/daily-workspace-user-token-usage-breakdown?"
            f"{breakdown_query}",
            token,
            account_id,
        )
    breakdown_rows = breakdown_payload.get("data") if breakdown_payload else []
    breakdown_rows = breakdown_rows if isinstance(breakdown_rows, list) else []
    count_by_date = {
        row["date"]: row
        for row in count_rows
        if isinstance(row, dict) and isinstance(row.get("date"), str)
    }
    breakdown_by_date = {
        row["date"]: row
        for row in breakdown_rows
        if isinstance(row, dict) and isinstance(row.get("date"), str)
    }
    result: list[dict[str, Any]] = []
    for row_date in sorted(set(count_by_date) | set(breakdown_by_date)):
        count_row = count_by_date.get(row_date)
        breakdown_row = breakdown_by_date.get(row_date)
        pricing = calculate_day_credits(count_row, breakdown_row)
        count_totals = count_row.get("totals") if isinstance(count_row, dict) else None
        count_totals = count_totals if isinstance(count_totals, dict) else {}
        tokens = pricing["tokens"]
        result.append(
            {
                "date": row_date,
                "totals": {
                    "credits": pricing["credits"],
                    "threads": number(count_totals.get("threads")),
                    "turns": number(count_totals.get("turns")),
                    "uncached_text_input_tokens": tokens["uncached"],
                    "cached_text_input_tokens": tokens["cached"],
                    "text_output_tokens": tokens["output"],
                    "text_total_tokens": tokens["total"],
                },
                "credit_source": pricing["source"],
                "credit_complete": pricing["complete"],
                "credit_coverage_percent": pricing["coverage"],
                "covered_tokens": pricing["covered_tokens"],
                "credit_issues": pricing["issues"],
            }
        )
    warnings = []
    if breakdown_error:
        warnings.append(
            "每日接口没有可用的 Credits，按模型 Token Breakdown "
            f"也无法通过当前 Codex 登录态读取 {breakdown_error}"
        )
    return result, warnings


NUMERIC_FIELDS = (
    "credits",
    "threads",
    "turns",
    "uncached_text_input_tokens",
    "cached_text_input_tokens",
    "text_output_tokens",
    "text_total_tokens",
)


def summarize(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = list(rows)
    totals: dict[str, float] = {field: 0.0 for field in NUMERIC_FIELDS}
    covered_tokens = 0.0
    credit_sources: set[str] = set()
    credit_issues: list[dict[str, Any]] = []
    for row in items:
        current = row.get("totals")
        if not isinstance(current, dict):
            continue
        for field in NUMERIC_FIELDS:
            value = current.get(field, 0)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[field] += float(value)
        covered_tokens += number(row.get("covered_tokens"))
        source = row.get("credit_source")
        if isinstance(source, str) and source:
            credit_sources.add(source)
        issues = row.get("credit_issues")
        if isinstance(issues, list):
            credit_issues.extend(item for item in issues if isinstance(item, dict))
    result: dict[str, Any] = {
        field: round(value, 6) if field == "credits" else int(round(value))
        for field, value in totals.items()
    }
    total_tokens = totals["text_total_tokens"]
    active_rows = [
        row
        for row in items
        if number((row.get("totals") or {}).get("text_total_tokens")) > 0
        or number((row.get("totals") or {}).get("turns")) > 0
    ]
    result["credit_complete"] = all(bool(row.get("credit_complete")) for row in active_rows)
    result["credit_coverage_percent"] = round(
        min(100.0, covered_tokens / total_tokens * 100) if total_tokens else 100.0,
        2,
    )
    result["credit_sources"] = sorted(credit_sources)
    result["credit_issues"] = credit_issues
    result["days"] = len(items)
    result["start_date"] = items[0]["date"] if items else None
    result["end_date"] = items[-1]["date"] if items else None
    return result


def estimate(credits: float, used_percent: float, credit_complete: bool) -> dict[str, Any]:
    if not credit_complete:
        return {
            "estimated_total_credits": None,
            "estimated_remaining_credits": None,
            "confidence": "无法估算，Credits 计价不完整",
        }
    if used_percent <= 0:
        return {
            "estimated_total_credits": None,
            "estimated_remaining_credits": None,
            "confidence": "无法估算",
        }
    fraction = used_percent / 100.0
    total = credits / fraction
    if used_percent < 20:
        confidence = "低，周百分比不足 20%，四舍五入误差会被放大"
    elif used_percent < 50:
        confidence = "中，继续用到约 50% 后复测会更稳"
    else:
        confidence = "较高，仍受自然日边界和后台延迟影响"
    return {
        "estimated_total_credits": round(total, 2),
        "estimated_remaining_credits": round(max(0.0, total - credits), 2),
        "confidence": confidence,
    }


def percent_change(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return round((current / previous - 1.0) * 100.0, 2)


def comparison(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    current_credits = float(current["credits"])
    previous_credits = float(previous["credits"])
    current_tokens = float(current["text_total_tokens"])
    previous_tokens = float(previous["text_total_tokens"])
    current_intensity = (
        current_credits / current_tokens * 1_000_000_000 if current_tokens else None
    )
    previous_intensity = (
        previous_credits / previous_tokens * 1_000_000_000 if previous_tokens else None
    )
    return {
        "previous_window": previous,
        "current_window": current,
        "credits_change_percent": percent_change(current_credits, previous_credits),
        "tokens_change_percent": percent_change(current_tokens, previous_tokens),
        "credits_per_billion_tokens": {
            "previous": round(previous_intensity, 2) if previous_intensity is not None else None,
            "current": round(current_intensity, 2) if current_intensity is not None else None,
            "change_percent": (
                percent_change(current_intensity, previous_intensity)
                if current_intensity is not None and previous_intensity is not None
                else None
            ),
        },
    }


def build_report(
    usage: dict[str, Any],
    rows: list[dict[str, Any]],
    force_days: int,
    fetch_warnings: list[str] | None = None,
) -> dict[str, Any]:
    if force_days < 1 or force_days > 31:
        raise QuotaError("--force-days 必须在 1 到 31 之间")
    window = weekly_window(usage)
    start_day = window.start_datetime.date().isoformat()
    end_day = min(date.today(), window.reset_datetime.date()).isoformat()
    automatic_rows = [row for row in rows if start_day <= row["date"] <= end_day]
    today = date.today()
    recent_start = today - timedelta(days=force_days - 1)
    previous_start = recent_start - timedelta(days=force_days)
    previous_end = recent_start - timedelta(days=1)
    forced_rows = [
        row for row in rows if recent_start.isoformat() <= row["date"] <= today.isoformat()
    ]
    previous_rows = [
        row
        for row in rows
        if previous_start.isoformat() <= row["date"] <= previous_end.isoformat()
    ]
    automatic = summarize(automatic_rows)
    forced = summarize(forced_rows)
    previous = summarize(previous_rows)
    automatic.update(
        estimate(
            float(automatic["credits"]),
            window.used_percent,
            bool(automatic["credit_complete"]),
        )
    )

    warnings: list[str] = list(fetch_warnings or [])
    if window.start_datetime.time() != datetime.min.time():
        warnings.append(
            "周窗口从一天中间开始，自动窗口首日按整天汇总，可能混入重置前用量"
        )
    if automatic["days"] == 0:
        warnings.append("当前周窗口还没有可用的日明细，后台可能仍在延迟汇总")
    if len(forced_rows) < force_days or len(previous_rows) < force_days:
        warnings.append("两周对比有自然日缺少明细，后台可能仍在汇总")
    if not automatic["credit_complete"]:
        warnings.append("当前周窗口存在未计价 Token，不能用本次 Credits 反推周总额")
    if "Rate Card" in automatic["credit_sources"]:
        warnings.append(
            "本周期 Credits 由模型和 Token Breakdown 按 Rate Card 回算；覆盖率只表示字段可计价，无法消除长上下文和速度字段缺失带来的误差"
        )
    warnings.append("强制最近七个自然日不一定等于真实重置周期")
    warnings.append("Token 构成、模型、推理强度和长上下文会改变 Credits，不能只看 Token 总量")

    rate_limit = usage.get("rate_limit") if isinstance(usage.get("rate_limit"), dict) else {}
    return {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "plan_type": usage.get("plan_type"),
        "weekly_window": {
            "used_percent": window.used_percent,
            "remaining_percent": window.remaining_percent,
            "start_at": window.start_datetime.isoformat(timespec="seconds"),
            "reset_at": window.reset_datetime.isoformat(timespec="seconds"),
            "limit_reached": bool(rate_limit.get("limit_reached", False)),
        },
        "automatic_window": automatic,
        "forced_window": forced,
        "two_week_comparison": comparison(forced, previous),
        "warnings": warnings,
    }


def human_number(value: int | float, digits: int = 2) -> str:
    number = float(value)
    if abs(number) >= 1_000_000_000:
        return f"{number / 1_000_000_000:.{digits}f}B"
    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:.{digits}f}M"
    if abs(number) >= 1_000:
        return f"{number / 1_000:.{digits}f}K"
    return f"{number:.{digits}f}"


def print_human(report: dict[str, Any]) -> None:
    weekly = report["weekly_window"]
    auto = report["automatic_window"]
    forced = report["forced_window"]
    two_week = report["two_week_comparison"]
    print("Codex 额度自查")
    print(f"套餐字段  {report.get('plan_type') or '未知'}")
    print(
        f"周窗口  已用 {weekly['used_percent']:.1f}%  剩余 {weekly['remaining_percent']:.1f}%"
    )
    print(f"本次窗口  {weekly['start_at']} 至 {weekly['reset_at']}")
    print(f"已触顶  {'是' if weekly['limit_reached'] else '否'}")
    print()
    print("按接口周窗口汇总")
    print(f"日期  {auto['start_date']} 至 {auto['end_date']}  共 {auto['days']} 个自然日")
    print(f"Credits  {auto['credits']:.2f}")
    print(
        "Credits 来源  "
        f"{', '.join(auto['credit_sources']) or '不可用'}  "
        f"计价覆盖率 {auto['credit_coverage_percent']:.1f}%"
    )
    print(
        "Token  "
        f"非缓存 {human_number(auto['uncached_text_input_tokens'])}  "
        f"缓存 {human_number(auto['cached_text_input_tokens'])}  "
        f"输出 {human_number(auto['text_output_tokens'])}  "
        f"合计 {human_number(auto['text_total_tokens'])}"
    )
    print(f"Turns  {auto['turns']}  Threads  {auto['threads']}")
    total = auto["estimated_total_credits"]
    remaining = auto["estimated_remaining_credits"]
    if total is None:
        print("反推周总额  暂时无法估算")
    else:
        print(f"反推周总额  约 {total:.0f} Credits")
        print(f"反推剩余额  约 {remaining:.0f} Credits")
    print(f"估算置信度  {auto['confidence']}")
    print()
    print("强制最近七个自然日")
    print(f"日期  {forced['start_date']} 至 {forced['end_date']}  共 {forced['days']} 天")
    print(f"Credits  {forced['credits']:.2f}")
    print(
        "Credits 来源  "
        f"{', '.join(forced['credit_sources']) or '不可用'}  "
        f"计价覆盖率 {forced['credit_coverage_percent']:.1f}%"
    )
    print(f"Token 合计  {human_number(forced['text_total_tokens'])}")
    print(f"Turns  {forced['turns']}  Threads  {forced['threads']}")
    print()
    previous = two_week["previous_window"]
    current = two_week["current_window"]
    print("两个七日自然窗口对比")
    print(
        f"前窗口  {previous['start_date']} 至 {previous['end_date']}  "
        f"Credits {previous['credits']:.2f}  Token {human_number(previous['text_total_tokens'])}"
    )
    print(
        f"近窗口  {current['start_date']} 至 {current['end_date']}  "
        f"Credits {current['credits']:.2f}  Token {human_number(current['text_total_tokens'])}"
    )
    credit_change = two_week["credits_change_percent"]
    token_change = two_week["tokens_change_percent"]
    credit_text = "无法计算" if credit_change is None else f"{credit_change:+.2f}%"
    token_text = "无法计算" if token_change is None else f"{token_change:+.2f}%"
    print(f"变化  Credits {credit_text}  Token {token_text}")
    intensity = two_week["credits_per_billion_tokens"]
    if intensity["previous"] is not None and intensity["current"] is not None:
        change = intensity["change_percent"]
        change_text = "无法计算" if change is None else f"{change:+.2f}%"
        print(
            "每 10 亿 Token 的 Credits  "
            f"前窗口 {intensity['previous']:.2f}  "
            f"近窗口 {intensity['current']:.2f}  "
            f"变化 {change_text}"
        )
    print()
    print("注意")
    for warning in report["warnings"]:
        print(f"- {warning}")


def main() -> int:
    args = parse_args()
    try:
        token, account_id = load_credentials(auth_path(args.auth_file))
        usage = api_get("/backend-api/wham/usage", token, account_id)
        rows, fetch_warnings = daily_rows(token, account_id, args.days)
        report = build_report(usage, rows, args.force_days, fetch_warnings)
    except QuotaError as exc:
        print(f"自查失败 {exc}", file=sys.stderr)
        return 1
    finally:
        token = "" if "token" in locals() else ""
        account_id = None
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

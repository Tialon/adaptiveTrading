"""外置配置文件的读取 / 草稿校验 / 写入 / 回滚(管理页面 V12.3)。

**安全边界(不可越界)**
- **不绕过任何守卫**: 草稿校验把「当前配置 + 拟改值」构造成一个候选 `Settings`,
  再交给 `settings.validate()`、`settings.mainnet_blocked_reason()`、
  `mainnet_readiness_check()` 判定 —— 与正常启动时的判定**完全同源**。
  管理页面没有任何绕过 `LIVE_TRADING_CONFIRM` / `MAINNET_API_SCOPE_CONFIRM` 的通道。
- **不泄露敏感值**: 含 `KEY`/`SECRET`/`TOKEN`/`PASSWORD` 的字段一律不参与编辑,
  也不出现在响应里; diff 中敏感项只显示 `<configured>` / `<empty>` / `<changed>`。
- **写入可回滚**: 写前备份为 `<file>.bak.<UTC 时间戳>`, 再以「临时文件 + os.replace」原子替换,
  并保留原文件权限位。
- **只落盘不改运行时**: `apply` 只写文件, 不热改运行中的进程; 生效需重启(由调用方提示)。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 敏感字段判据: 命中即不参与编辑、不透出
_SENSITIVE_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD")

_LINE_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=")

# 备份保留份数(超出后删除最旧的)
BACKUP_KEEP = 20


def is_sensitive_key(key: str) -> bool:
    """ENV 键名是否敏感(只按名字判定, 不看值)。"""
    upper = key.upper()
    return any(marker in upper for marker in _SENSITIVE_MARKERS)


def mask_value(value: str) -> str:
    """敏感值的展示形态: 有值 → `<configured>`, 空 → `<empty>`。"""
    return "<configured>" if (value or "").strip() else "<empty>"


# ---------------------------------------------------------------------------
# 字段定义
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """一个可配置字段的定义(供 UI 渲染 + 校验)。"""

    key: str  # ENV 键名
    attr: str  # Settings 字段名
    kind: str  # bool | confirm | pct | int | score | ladder | enum | text
    group: str  # mode | runtime | safety | ops | risk | strategy | frozen
    label: str
    help: str
    unit: str = ""
    recommended: str = ""
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    editable: bool = True
    restart_required: bool = True
    warn_when: str | None = None  # 该值触发风险警告(如 "false")
    warn_text: str = ""


FIELD_SPECS: tuple[FieldSpec, ...] = (
    # ---------- 运行模式(P3) ----------
    FieldSpec("PAPER_TRADING", "paper_trading", "bool", "mode",
              "纸面模拟成交", "为 true 时订单只在本地模拟, 不会真实下单。",
              recommended="首次部署 / 验证期用 true", warn_when="false",
              warn_text="关闭纸面 = 真实下单, 会动用真实或测试网资金。"),
    FieldSpec("BINANCE_TESTNET", "binance_testnet", "bool", "mode",
              "使用币安测试网", "为 false 时连接币安主网(真实市场)。",
              recommended="未做真钱准备前保持 true", warn_when="false",
              warn_text="连接主网 = 面对真实资金与真实市场。"),
    FieldSpec("LIVE_TRADING_CONFIRM", "live_trading_confirm", "confirm", "mode",
              "主网连接确认", "主网模式必须显式确认为 true, 否则启动会被拒绝。",
              recommended="未准备真钱前保持未确认"),
    FieldSpec("MAINNET_API_SCOPE_CONFIRM", "mainnet_api_scope_confirmed", "bool", "mode",
              "主网 API 权限已确认", "人工核对过主网 key 仅 Spot、且已关闭提现/资金转移。",
              recommended="核对后填 true; 未核对保持 false"),
    # ---------- 运行开关(P4) ----------
    FieldSpec("DAILY_REPORT_ENABLED", "daily_report_enabled", "bool", "runtime",
              "生成每日复盘", "每天自动产出 reports/YYYY-MM-DD.md 复盘文件。",
              recommended="true"),
    FieldSpec("AI_ENABLED", "ai_enabled", "bool", "runtime",
              "启用 AI 参数建议", "AI 只产出参数建议, 不参与下单路径, 不会自动改参数。",
              recommended="按需; 关闭不影响交易"),
    # ---------- 安全开关(P4) ----------
    FieldSpec("STARTUP_RECONCILE_ENABLED", "startup_reconcile_enabled", "bool", "safety",
              "启动时对账", "实盘启动时核对本地与交易所持仓, 有未解决差异就冻结交易。",
              recommended="实盘必须 true", warn_when="false",
              warn_text="关闭后崩溃窗口的差异不会被发现, 可能带着错误持仓开始交易。"),
    # V12.6 P3: 原 `MAINNET_READINESS_ENABLED` 开关已移除。
    # 它在 `settings.py` 里声明但**全代码库从不被读取** —— 主网就绪自检在 `wiring.py` 中
    # 只要 `BINANCE_TESTNET=false` 就无条件执行, 置 false 不会关闭它。此前靠一段
    # 「本开关无效」的帮文如实标注, 但那仍是在页面上摆一个点了没反应的开关。
    # 一个不生效的开关不值得靠文案解释 —— 直接不给。防回归见
    # `tests/unit/test_v126_dead_config_switches.py`。
    FieldSpec("MAINNET_TAKEOVER_ENABLED", "mainnet_takeover_enabled", "bool", "safety",
              "主网首次只读接管", "首次连主网时先做账户快照 + 对账 + 记录基线, 不通过就冻结。",
              recommended="true", warn_when="false",
              warn_text="关闭后首次连主网不会做基线对账。"),
    # ---------- 运维(P4) ----------
    FieldSpec("LOG_LEVEL", "log_level", "enum", "ops",
              "日志级别", "日志详细程度; DEBUG 会产生大量日志。",
              recommended="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")),
    FieldSpec("WEB_ADMIN_TOKEN", "web_admin_token", "text", "ops",
              "管理写操作令牌", "配置后急停/恢复/解除熔断/停机/改配置才可用。",
              recommended="局域网访问建议配置", editable=False),
    FieldSpec("WEB_ADMIN_AUTH", "web_admin_auth", "enum", "ops",
              "写接口鉴权", "on = 需要令牌(默认); off = 关闭鉴权, 页面无需令牌。"
              "关闭后局域网内任何设备都能改配置/恢复急停/停机。",
              recommended="保持 on; 仅个人内网单用户可考虑 off",
              choices=("on", "off"), warn_when="off",
              warn_text="写接口鉴权已关闭: 局域网内任何设备无需凭据即可改配置、恢复急停、停机。"),
    # ---------- 风控参数(P5) ----------
    FieldSpec("RISK_MAX_SINGLE_ORDER_PCT", "risk_max_single_order_pct", "pct", "risk",
              "单笔最大占比", "单笔订单最多占用总权益的比例。",
              unit="%", recommended="小资金建议 2% ~ 5%", lo=0.01, hi=100.0),
    FieldSpec("RISK_MAX_POSITION_PCT", "risk_max_position_pct", "pct", "risk",
              "策略仓位上限", "策略信号允许占用的总权益上限。",
              unit="%", recommended="建议 20% ~ 40%", lo=0.01, hi=100.0),
    FieldSpec("RISK_MAX_SOL_EXPOSURE", "risk_max_sol_exposure", "pct", "risk",
              "SOL 总敞口上限", "SOL 市值占总权益的硬上限, 超限禁买、放行卖。",
              unit="%", recommended="建议 50% ~ 70%", lo=0.01, hi=100.0),
    FieldSpec("RISK_MAX_DAILY_LOSS", "risk_max_daily_loss", "pct", "risk",
              "日内亏损阈值", "当日亏损达到该比例后只允许减仓, 不再开新仓。",
              unit="%", recommended="建议 2% ~ 3%", lo=0.01, hi=100.0),
    FieldSpec("RISK_MAX_DRAWDOWN", "risk_max_drawdown", "pct", "risk",
              "最大回撤急停阈值", "从权益峰值回撤达到该比例即急停冻结(需人工恢复)。",
              unit="%", recommended="建议 10% ~ 15%", lo=0.01, hi=100.0),
    # ---------- 策略参数(P5) ----------
    FieldSpec("ENTRY_BUY_THRESHOLD", "entry_buy_threshold", "score", "strategy",
              "买入评分阈值", "综合评分达到该值才产生买入信号(满分 100)。",
              unit="分", recommended="建议 70 ~ 85", lo=0.0, hi=100.0),
    FieldSpec("ENTRY_OBSERVE_THRESHOLD", "entry_observe_threshold", "score", "strategy",
              "观察阈值", "达到该值进入观察, 但不买入; 必须小于买入阈值。",
              unit="分", recommended="比买入阈值低 15 ~ 20 分", lo=0.0, hi=100.0),
    FieldSpec("SELL_TAKE_PROFIT_LADDER", "sell_take_profit_ladder", "ladder", "strategy",
              "分批止盈阶梯", "格式「盈利%:卖出持仓%」, 逗号分隔, 例如 5:20,10:30,20:50。",
              recommended="默认 5:20,10:30,20:50"),
    FieldSpec("BUY_DIP_PCT", "buy_dip_pct", "pct", "strategy",
              "VWAP 折价买入阈值", "价格低于 VWAP 该比例时才考虑抄底买入。",
              unit="%", recommended="建议 0.3% ~ 1%", lo=0.01, hi=100.0),
    FieldSpec("SELL_PROFIT_PCT", "sell_profit_pct", "pct", "strategy",
              "基础止盈比例", "达到该盈利比例触发基础止盈。",
              unit="%", recommended="建议 0.5% ~ 2%", lo=0.01, hi=100.0),
    # ---------- 运行周期(P5) ----------
    FieldSpec("RECONCILE_INTERVAL_SECONDS", "reconcile_interval_seconds", "int", "runtime",
              "对账间隔", "与交易所核对持仓/权益的周期。",
              unit="秒", recommended="建议 60 ~ 300", lo=1, hi=86400),
    FieldSpec("PORTFOLIO_REBALANCE_INTERVAL_SECONDS", "portfolio_rebalance_interval_seconds", "int",
              "runtime", "组合检查间隔", "核心/交易/现金三桶的再平衡检查周期。",
              unit="秒", recommended="建议 300 ~ 3600", lo=1, hi=86400),
    # ---------- 只读冻结项 ----------
    FieldSpec("SYMBOLS", "symbols", "text", "frozen",
              "交易标的", "产品边界已冻结为单币 SOLUSDT(现货), 不可改。",
              recommended="SOLUSDT", editable=False),
    FieldSpec("DAILY_REPORT_DIR", "daily_report_dir", "text", "frozen",
              "日报目录", "容器内由 compose 挂载为 /app/reports, 改这里无效, 故只读。",
              recommended="/app/reports", editable=False),
)

SPECS_BY_KEY: dict[str, FieldSpec] = {s.key: s for s in FIELD_SPECS}

# 运行模式预设(P3): 模式 -> 该模式要求的开关组合
MODE_PRESETS: dict[str, dict[str, Any]] = {
    "paper": {"PAPER_TRADING": True, "BINANCE_TESTNET": True},
    "live_testnet": {"PAPER_TRADING": False, "BINANCE_TESTNET": True},
    "paper_mainnet": {"PAPER_TRADING": True, "BINANCE_TESTNET": False},
    "live_mainnet": {
        "PAPER_TRADING": False,
        "BINANCE_TESTNET": False,
        "LIVE_TRADING_CONFIRM": True,
    },
}

MODE_LABELS: dict[str, str] = {
    "paper": "纸面模式",
    "live_testnet": "测试网真实",
    "paper_mainnet": "主网纸面观察",
    "live_mainnet": "主网真实",
}

# 哪些模式在当前安全守卫下**无法启动**, 以及原因。
# 「主网纸面观察」看似安全(纸面 + 主网行情), 但现有两道守卫都会拦它:
#   1) `settings.mainnet_blocked_reason()`: BINANCE_TESTNET=false 一律要求 LIVE_TRADING_CONFIRM=true
#      —— 即便 PAPER_TRADING=true(刻意为之: 防止误配直接连主网);
#   2) `mainnet_readiness_check()`: 第②项要求 PAPER_TRADING=false, 纸面必然不满足。
# 管理页面**不绕过**这些守卫, 因此如实标注该模式不可用。
MODE_NOTES: dict[str, str] = {
    "paper_mainnet": "当前安全守卫下无法启动: 连主网必须显式确认 LIVE_TRADING_CONFIRM=true, "
                     "且主网就绪自检要求 PAPER_TRADING=false。本页面不绕过这些守卫。",
}


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def resolve_config_path() -> Path:
    """外置配置文件路径: 优先 `ADAPTIVE_TRADING_ENV_FILE`, 否则 `./.env`。

    与 compose 的 `--env-file` 约定一致: 生产用外置路径, 本地开发用仓库 `.env`。
    """
    explicit = (os.environ.get("ADAPTIVE_TRADING_ENV_FILE") or "").strip()
    if explicit:
        return Path(explicit)
    return Path(".env")


def config_path_status(path: Path | None = None) -> dict[str, Any]:
    """配置文件的路径与可写性(容器未挂载外置配置时用于给出可操作的提示)。"""
    p = path or resolve_config_path()
    exists = p.exists()
    parent_exists = p.parent.exists()
    writable = os.access(p, os.W_OK) if exists else (parent_exists and os.access(p.parent, os.W_OK))
    return {
        "path": str(p),
        "exists": exists,
        "writable": bool(writable),
        "source": "ADAPTIVE_TRADING_ENV_FILE" if os.environ.get("ADAPTIVE_TRADING_ENV_FILE") else "default(./.env)",
    }


# ---------------------------------------------------------------------------
# 读写 .env
# ---------------------------------------------------------------------------


def parse_env_lines(text: str) -> list[tuple[str, str]]:
    """按出现顺序解析 `KEY=VALUE`(保留注释与空行的位置信息由读写函数各自处理)。"""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        m = _LINE_RE.match(line.strip())
        if m:
            key = m.group(1)
            _, _, value = line.strip().partition("=")
            out.append((key, value))
    return out


def read_env_file(path: Path) -> dict[str, str]:
    """读取配置文件为 {KEY: raw_value}; 文件不存在返回空 dict。"""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    return dict(parse_env_lines(text))


def file_values(path: Path) -> dict[str, str]:
    return read_env_file(path)


def _to_ui(spec: FieldSpec, raw: str | None) -> Any:
    """把文件里的原始值转成 UI 值(百分比参数转成百分数)。"""
    if raw is None:
        return None
    v = raw.strip()
    if spec.kind == "bool":
        return v.lower() == "true"
    if spec.kind == "confirm":
        return v.lower() == "true"
    if spec.kind == "pct":
        try:
            return round(float(v) * 100.0, 6)
        except ValueError:
            return None
    if spec.kind in ("int",):
        try:
            return int(float(v))
        except ValueError:
            return None
    if spec.kind in ("score",):
        try:
            return float(v)
        except ValueError:
            return None
    return v


def _ui_to_settings(spec: FieldSpec, ui: Any) -> Any:
    """UI 值 → Settings 域的值(百分比参数从百分数换回小数)。

    **必须走这一步**: UI 里 `RISK_MAX_SINGLE_ORDER_PCT=3` 表示 3%, 而 Settings 存的是
    0.03。直接把 UI 值塞进 Settings 会让 `validate()` 把 3.0 判为越界, 或者更糟 —— 静默
    写入一个比预期大 100 倍的阈值。
    """
    if spec.kind == "pct":
        return float(ui) / 100.0
    if spec.kind == "confirm":
        # `live_trading_confirm` 在 Settings 里是 **str**(守卫用 .strip().lower() == "true" 判定),
        # 塞布尔值会让 `mainnet_blocked_reason()` 抛 AttributeError。
        return "true" if ui else ""
    return ui


def _settings_to_ui(spec: FieldSpec, attr_value: Any) -> Any:
    """Settings 域的值 → UI 值(百分比参数: 小数 → 百分数)。

    **直接换算, 不要绕道 `_to_env`** —— `_to_env` 假定输入已在 UI 域(百分数), 会再除一次 100,
    把 0.05 变成 0.0005, 于是所有百分比参数都被误判为「与文件不一致」。
    """
    if attr_value is None:
        return None
    if spec.kind == "bool":
        return bool(attr_value)
    if spec.kind == "confirm":
        return str(attr_value).strip().lower() == "true"
    if spec.kind == "pct":
        try:
            return round(float(attr_value) * 100.0, 6)
        except (TypeError, ValueError):
            return None
    if spec.kind == "int":
        try:
            return int(attr_value)
        except (TypeError, ValueError):
            return None
    if spec.kind == "score":
        try:
            return float(attr_value)
        except (TypeError, ValueError):
            return None
    return str(attr_value)


def _to_env(spec: FieldSpec, ui: Any) -> str:
    """把 UI 值转成写回文件的字符串。"""
    if spec.kind == "bool":
        return "true" if ui else "false"
    if spec.kind == "confirm":
        return "true" if ui else ""
    if spec.kind == "pct":
        return _fmt_num(float(ui) / 100.0)
    if spec.kind == "score":
        return _fmt_num(float(ui))
    if spec.kind == "int":
        return str(int(ui))
    return str(ui)


def _fmt_num(v: float) -> str:
    """去掉浮点尾巴(0.050000 → 0.05), 整数不写小数点。"""
    if v == int(v):
        return str(int(v))
    return repr(round(v, 10))


def build_config_view(settings: Any, path: Path | None = None) -> dict[str, Any]:
    """`GET /api/admin/config` 的响应: 字段定义 + 当前值(文件优先) + 运行时对照。"""
    p = path or resolve_config_path()
    status = config_path_status(p)
    persisted = read_env_file(p)

    fields_out: list[dict[str, Any]] = []
    pending_restart: list[str] = []

    for spec in FIELD_SPECS:
        sensitive = is_sensitive_key(spec.key)
        raw = persisted.get(spec.key)
        live_ui = _settings_to_ui(spec, getattr(settings, spec.attr, None))
        file_ui = _to_ui(spec, raw)

        effective = file_ui if raw is not None else live_ui
        item: dict[str, Any] = {
            "key": spec.key,
            "group": spec.group,
            "kind": spec.kind,
            "label": spec.label,
            "help": spec.help,
            "unit": spec.unit,
            "recommended": spec.recommended,
            "editable": bool(spec.editable) and not sensitive,
            "restart_required": spec.restart_required,
            "choices": list(spec.choices),
            "lo": spec.lo,
            "hi": spec.hi,
            "in_file": raw is not None,
            "sensitive": sensitive,
        }
        if sensitive:
            live_raw = getattr(settings, spec.attr, "")
            item["value"] = mask_value(raw if raw is not None else str(live_raw or ""))
        else:
            item["value"] = effective
            item["live_value"] = live_ui
            if raw is not None and live_ui is not None and file_ui != live_ui:
                pending_restart.append(spec.key)
        fields_out.append(item)

    return {
        "config": status,
        "fields": fields_out,
        "modes": [
            {
                "id": mid,
                "label": MODE_LABELS[mid],
                "switches": preset,
                "note": MODE_NOTES.get(mid, ""),
                "blocked": mid in MODE_NOTES,
            }
            for mid, preset in MODE_PRESETS.items()
        ],
        "pending_restart": pending_restart,
        "frozen_symbols": list(getattr(settings, "symbol_list", [])),
    }


# ---------------------------------------------------------------------------
# 草稿校验
# ---------------------------------------------------------------------------


def _parse_field(spec: FieldSpec, ui: Any) -> tuple[Any, str | None]:
    """把 UI 值解析并做基础范围校验; 返回 (python 值, 错误信息)。"""
    if spec.kind in ("bool", "confirm"):
        if isinstance(ui, bool):
            return ui, None
        if isinstance(ui, str) and ui.strip().lower() in ("true", "false", "1", "0", ""):
            return ui.strip().lower() in ("true", "1"), None
        return None, f"{spec.label} 需要是布尔值(true/false)"
    if spec.kind in ("pct", "score", "int"):
        try:
            num = float(ui)
        except (TypeError, ValueError):
            return None, f"{spec.label} 需要是数字"
        if spec.lo is not None and num < spec.lo:
            return None, f"{spec.label} 不能小于 {spec.lo}{spec.unit}"
        if spec.hi is not None and num > spec.hi:
            return None, f"{spec.label} 不能大于 {spec.hi}{spec.unit}"
        if spec.kind == "int":
            if num != int(num):
                return None, f"{spec.label} 需要是整数"
            return int(num), None
        return num, None
    if spec.kind == "enum":
        # 大小写不敏感地匹配, 但**返回 spec.choices 里的规范写法** ——
        # 不能无条件 `.upper()`: `LOG_LEVEL` 的选项是大写, 而 `WEB_ADMIN_AUTH` 是小写
        # (on/off), 一律转大写会让后者永远匹配不上(真机上撞到过)。
        raw = str(ui).strip()
        for choice in spec.choices:
            if raw.upper() == choice.upper():
                return choice, None
        return None, f"{spec.label} 只能是 {'/'.join(spec.choices)}"
    if spec.kind == "ladder":
        return _parse_ladder(str(ui))
    return str(ui), None


def _parse_ladder(raw: str) -> tuple[str | None, str | None]:
    """校验分批止盈阶梯 `盈利%:卖出持仓%`, 逗号分隔。"""
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    if not parts:
        return None, "分批止盈阶梯不能为空"
    for part in parts:
        if ":" not in part:
            return None, f"分批止盈阶梯格式应为「盈利%:卖出%」, 当前分片 {part!r} 缺少冒号"
        left, _, right = part.partition(":")
        try:
            pct, ratio = float(left), float(right)
        except ValueError:
            return None, f"分批止盈阶梯分片 {part!r} 不是数字"
        if pct <= 0:
            return None, f"分批止盈阶梯的盈利% 必须大于 0, 当前 {part!r}"
        if not (0 < ratio <= 100):
            return None, f"分批止盈阶梯的卖出% 需在 (0, 100], 当前 {part!r}"
    return ",".join(parts), None


def evaluate_risk_warnings(result_mode: str, effective: dict[str, Any]) -> list[dict[str, str]]:
    """按「结果配置」给出风险提示(不阻断, 只警告)。"""
    warnings: list[dict[str, str]] = []
    for spec in FIELD_SPECS:
        if not spec.warn_when:
            continue
        value = effective.get(spec.key)
        as_str = "true" if value is True else ("false" if value is False else str(value))
        if as_str.lower() == spec.warn_when.lower():
            warnings.append({
                "key": spec.key,
                "level": "danger" if spec.group in ("mode", "safety") else "warning",
                "text": spec.warn_text or f"{spec.label} 当前为 {as_str}",
            })
    if result_mode == "live_mainnet":
        warnings.insert(0, {
            "key": "MODE",
            "level": "danger",
            "text": "主网真实模式: 将使用真实资金在币安主网下单, 请确认已完成主网就绪复审。",
        })
    return warnings


def build_draft(
    *,
    base_settings: Any,
    proposed: dict[str, Any],
    path: Path | None = None,
) -> dict[str, Any]:
    """校验草稿并返回 diff / 风险 / 是否需重启。**不写任何文件。**

    `proposed` 为 `{ENV_KEY: UI值}`。校验直接复用 `Settings.validate()` /
    `mainnet_blocked_reason()` / `mainnet_readiness_check()`, 与启动期判定同源。
    """
    p = path or resolve_config_path()
    persisted = read_env_file(p)
    field_errors: list[dict[str, str]] = []

    # 1) 未知字段 / 只读字段 / 敏感字段一律拒绝
    unknown = [k for k in proposed if k not in SPECS_BY_KEY]
    for key in unknown:
        field_errors.append({"key": key, "message": "未知配置项, 不支持修改"})
    for key in list(proposed):
        spec = SPECS_BY_KEY.get(key)
        if spec is None:
            continue
        if is_sensitive_key(key):
            field_errors.append({"key": key, "message": f"{spec.label} 是敏感项, 页面不提供修改"})
        elif not spec.editable:
            field_errors.append({"key": key, "message": f"{spec.label} 为只读项, 不可修改"})

    # 2) 逐字段解析 + 范围校验
    parsed: dict[str, Any] = {}
    for key, ui in proposed.items():
        spec = SPECS_BY_KEY.get(key)
        if spec is None or is_sensitive_key(key) or not spec.editable:
            continue
        value, err = _parse_field(spec, ui)
        if err:
            field_errors.append({"key": key, "message": err})
        else:
            parsed[key] = value

    if field_errors:
        return {
            "ok": False,
            "problems": field_errors,
            "diff": [],
            "risk_warnings": [],
            "requires_restart": False,
            "result_mode": None,
            "blocked_reasons": [],
        }

    # 3) 构造候选配置(只覆盖被改动的字段, 其余沿用当前运行值)
    updates = {SPECS_BY_KEY[k].attr: _ui_to_settings(SPECS_BY_KEY[k], v)
               for k, v in parsed.items()}
    candidate = base_settings.model_copy(update=updates)

    problems = candidate.validate()
    blocked_reasons: list[str] = []

    guard = candidate.mainnet_blocked_reason()
    if guard:
        blocked_reasons.append(guard)

    if not candidate.binance_testnet:
        from at01_common.mainnet_readiness import mainnet_readiness_check

        readiness = mainnet_readiness_check(
            binance_testnet=candidate.binance_testnet,
            paper_trading=candidate.paper_trading,
            live_trading_confirm=candidate.live_trading_confirm,
            api_scope_confirmed=candidate.mainnet_api_scope_confirmed,
            symbol=",".join(candidate.symbol_list),
            config_problems=problems,
            kill_switch_armed=False,
            git_sha=candidate.git_sha or "draft",
            base_url=candidate.binance_rest_url,
        )
        if not readiness["allowed"]:
            blocked_reasons.extend(readiness["blocked_reasons"])

    # 4) diff(文件当前值 -> 拟生效值)
    diff: list[dict[str, Any]] = []
    diff_effective: dict[str, Any] = {}
    for spec in FIELD_SPECS:
        raw = persisted.get(spec.key)
        if raw is not None:
            current_ui = _to_ui(spec, raw)
        else:
            # 文件里没有该项 → 回退到运行值, 同样需要换算成 UI 单位(百分比 → 百分数)
            current_ui = _settings_to_ui(spec, getattr(base_settings, spec.attr, None))
        diff_effective[spec.key] = parsed.get(spec.key, current_ui)
        if spec.key not in parsed:
            continue
        new_ui = parsed[spec.key]
        if current_ui == new_ui:
            continue
        if is_sensitive_key(spec.key):
            diff.append({
                "key": spec.key, "label": spec.label,
                "from": mask_value(str(raw or "")), "to": "<changed>",
                "sensitive": True,
            })
        else:
            diff.append({
                "key": spec.key,
                "label": spec.label,
                "from": _display(spec, current_ui),
                "to": _display(spec, new_ui),
                "sensitive": False,
            })

    result_mode = _mode_id(candidate.paper_trading, candidate.binance_testnet)
    warnings = evaluate_risk_warnings(result_mode, diff_effective)

    return {
        "ok": not problems and not blocked_reasons and not field_errors,
        "problems": [{"key": "", "message": msg} for msg in problems],
        "blocked_reasons": blocked_reasons,
        "diff": diff,
        "risk_warnings": warnings,
        "requires_restart": bool(diff),
        "result_mode": result_mode,
        "result_mode_label": MODE_LABELS.get(result_mode, result_mode),
    }


def proposed_env_values(proposed: dict[str, Any]) -> dict[str, str]:
    """把 `{ENV_KEY: UI值}` 转成 `{ENV_KEY: 写入文件的字符串}`。

    只处理可编辑且非敏感的字段; 解析失败的一律跳过(调用方应先经过 `build_draft` 校验,
    此时不应再有失败项)。
    """
    out: dict[str, str] = {}
    for key, ui in proposed.items():
        spec = SPECS_BY_KEY.get(key)
        if spec is None or is_sensitive_key(key) or not spec.editable:
            continue
        value, err = _parse_field(spec, ui)
        if err is None:
            out[key] = _to_env(spec, value)
    return out


def changed_env_values(path: Path, env_values: dict[str, str]) -> dict[str, str]:
    """筛出与文件当前值不同的项(未在文件中出现的键视为需要新增)。"""
    persisted = read_env_file(path)
    return {
        k: v for k, v in env_values.items()
        if persisted.get(k) != v
    }


def _display(spec: FieldSpec, ui: Any) -> str:
    if ui is None:
        return "(未设置)"
    if spec.kind in ("bool", "confirm"):
        return "true" if ui else "false"
    return f"{ui}{spec.unit}" if spec.unit else str(ui)


def _mode_id(paper: bool, testnet: bool) -> str:
    if paper:
        return "paper" if testnet else "paper_mainnet"
    return "live_testnet" if testnet else "live_mainnet"


# ---------------------------------------------------------------------------
# 写入 / 备份 / 回滚
# ---------------------------------------------------------------------------


def _utc_stamp() -> str:
    """带微秒的 UTC 时间戳。

    **精度很重要**: 秒级精度下「保存 → 立刻回滚」会在同一秒内产生同名备份,
    后来者覆盖前者 —— 等于回滚时把要恢复的那份备份毁掉。
    """
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def list_backups(path: Path) -> list[Path]:
    """按时间倒序列出该配置的备份文件。"""
    if not path.parent.exists():
        return []
    return sorted(path.parent.glob(path.name + ".bak.*"), reverse=True)


def backup_file(path: Path) -> Path | None:
    """备份当前配置; 文件不存在返回 None。"""
    if not path.exists():
        return None
    bak = path.with_name(f"{path.name}.bak.{_utc_stamp()}")
    # 同名兜底(极端情况下同一微秒内两次备份): 绝不覆盖已有备份
    suffix = 1
    while bak.exists():
        bak = path.with_name(f"{path.name}.bak.{_utc_stamp()}-{suffix}")
        suffix += 1
    bak.write_bytes(path.read_bytes())
    try:
        os.chmod(bak, path.stat().st_mode & 0o777)
    except OSError:
        pass
    for old in list_backups(path)[BACKUP_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass
    return bak


def write_env_values(path: Path, changes: dict[str, str]) -> dict[str, Any]:
    """就地更新 `changes` 中的键, **保留其余行、注释与顺序**; 原子替换。

    返回 {written: [...], appended: [...], backup: str|None}。
    """
    if not changes:
        return {"written": [], "appended": [], "backup": None}

    backup = backup_file(path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()

    remaining = dict(changes)
    written: list[str] = []
    out: list[str] = []
    for line in lines:
        m = _LINE_RE.match(line.strip())
        if m and m.group(1) in remaining:
            key = m.group(1)
            out.append(f"{key}={remaining.pop(key)}")
            written.append(key)
        else:
            out.append(line)

    appended = list(remaining)
    if appended:
        if out and out[-1].strip():
            out.append("")
        out.append("# --- 管理页面写入(V12.3) ---")
        out.extend(f"{k}={remaining[k]}" for k in appended)

    new_text = "\n".join(out) + "\n"

    # 原子替换: 同目录临时文件 + os.replace; 保留原权限位
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    if path.exists():
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)
        except OSError:
            pass
    else:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)

    return {
        "written": written,
        "appended": appended,
        "backup": str(backup) if backup else None,
    }


def rollback(path: Path) -> dict[str, Any]:
    """恢复最近一份备份(恢复前先把当前文件也备份一次, 保证可再次回退)。"""
    backups = list_backups(path)
    if not backups:
        return {"ok": False, "message": "没有可用的备份文件"}
    newest = backups[0]
    # 先把要恢复的内容读进内存, 再备份当前文件 —— 顺序颠倒或命名冲突都不会毁掉来源
    content = newest.read_bytes()
    mode = newest.stat().st_mode & 0o777
    current_backup = backup_file(path)
    path.write_bytes(content)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return {
        "ok": True,
        "restored_from": str(newest),
        "current_saved_as": str(current_backup) if current_backup else None,
    }

"""个股深度研读（可插拔模块）：政策原文 + 公司公开资料 → 传导/竞争/风险，每条结论带溯源。

红线（本 issue 明确要求）：每一条结论都必须能溯源到政策原文或公开公司来源，
两者都给不出就整篇不产出——宁可空着也不编。因此：

- LLM 未配置 → 整段跳过，并在日报里写明原因（不阻塞其它环节）；
- 模型返回的 sources 只认两类 ref：给定的政策 source_url，或 `通达信F10:<code>`；
  出现其它 ref → 该段作废；
- 任一必填字段为空 → 该段作废。

`llm_fn` 参数化，便于用假函数做单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from policypulse.config import Settings
from policypulse.text import simplify_hant

F10_REF_PREFIX = "通达信F10:"

SKIP_NO_LLM = (
    "个股深度研读已跳过：未配置 LLM（缺少 POLICYPULSE_LLM_BASE_URL / "
    "POLICYPULSE_LLM_API_KEY）。规则路径做不出带溯源的深度结论，宁可不产出。"
)
SKIP_NO_PROFILE = "个股深度研读已跳过：该股没有可引用的公司公开资料（F10 取不到）。"
SKIP_INVALID = "个股深度研读已跳过：模型结论缺少可校验的溯源，按红线作废。"

_REQUIRED = ("transmission", "competition", "impact")

# 深读结论的结构化语义判定字段（`substantive`，LLM 按 prompt 量表输出）。
# WAN-15 根因：旧实现拿 transmission 的自由文本做词表命中（含「间接」→ 实质），
# 但「间接」后面跟着的 impact 常常是「影响有限 / 无具体项目 / 无直接可验证」这类
# 否定性判断——语义判定必须由 LLM 综合 transmission + impact 完成，代码只消费枚举：
#   "是"   —— impact 明确正面（直接受益 / 显著拉动 / 明确订单 / 纳入试点）→ 可升级受益
#   "否"   —— impact 为否定性判断（有限 / 无具体 / 未提及 / 难以验证 / 未披露 /
#             无直接），无论 transmission 写「直接」还是「间接」→ 降级「仅概念关联」
#   "不确定" / 缺失 —— 判不出来；fail-closed，不得升级为「受益个股」
SUBSTANTIVE_YES = "是"
SUBSTANTIVE_NO = "否"
SUBSTANTIVE_UNCERTAIN = "不确定"


def substantive_verdict(value: str) -> bool | None:
    """把深读的结构化 `substantive` 字段映射成 (是否实质影响)。

    返回 True / False / None（未给出或不可识别 → 调用方按「不升级」处理，fail-closed）。
    只认枚举值，不做任何自由文本匹配——语义判定发生在 LLM 侧（prompt 量表），
    代码侧的职责是忠实消费结构化结果，而不是再养一份会漏判的关键词表。
    """
    text = (value or "").strip()
    if text == SUBSTANTIVE_YES:
        return True
    if text == SUBSTANTIVE_NO:
        return False
    return None


@dataclass
class DeepDive:
    ticker_code: str
    ticker_name: str
    policy_title: str
    policy_summary: str
    policy_quote: str
    policy_url: str
    profile_business: str
    transmission: str = ""
    competition: str = ""
    impact: str = ""
    # 结构化语义判定（是/否/不确定）：report 分区只认这个字段，不再扫 transmission 文本
    substantive: str = ""
    risks: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    # 深读结论要能反向决定它所属政策事件的报告分区，因此必须记住 event_id
    event_id: str = ""


def _allowed_refs(payload: dict, code: str, settings: Settings) -> set[str]:
    allowed = set()
    if payload.get("policy_url"):
        allowed.add(payload["policy_url"])
    allowed.add(F10_REF_PREFIX + code)
    # 原文快照路径也算政策原始来源（本地可核对）
    if payload.get("raw_snapshot_path"):
        allowed.add(str(payload["raw_snapshot_path"]))
    return allowed


def validate(payload: dict | None, code: str, settings: Settings) -> dict | None:
    """溯源硬校验：字段齐全 + 每条 source 的 ref 都在白名单里。"""
    if not isinstance(payload, dict):
        return None
    for key in _REQUIRED:
        if not str(payload.get(key) or "").strip():
            return None
    risks = [simplify_hant(str(r).strip()) for r in (payload.get("risks") or []) if str(r).strip()]
    sources = payload.get("sources") or []
    if not isinstance(sources, list) or not sources:
        return None
    allowed = _allowed_refs(payload, code, settings)
    cleaned: list[dict] = []
    for s in sources:
        if not isinstance(s, dict):
            return None
        ref = str(s.get("ref") or "").strip()
        label = str(s.get("label") or "").strip()
        if not ref or ref not in allowed or not label:
            return None
        cleaned.append({"label": label, "ref": ref})
    return {
        "transmission": simplify_hant(str(payload["transmission"]).strip()),
        "competition": simplify_hant(str(payload["competition"]).strip()),
        "impact": simplify_hant(str(payload["impact"]).strip()),
        # 结构化语义判定：非必填（老输出没有该字段），原样透传枚举值；
        # 不可识别的值交给 substantive_verdict 映射成 None（不升级，fail-closed）
        "substantive": str(payload.get("substantive") or "").strip(),
        "risks": risks[:3],
        "sources": cleaned,
    }


def llm_fn_default(prompt_policy: str, prompt_profile: str):
    from policypulse import llm

    if not llm.llm_available():
        return None
    return llm.deepdive_with_llm(prompt_policy, prompt_profile)


def build_deepdives(events, client, settings: Settings, llm_fn=None) -> tuple[list[DeepDive], list[str]]:
    """对候选事件的 Top-K 个股产出深度研读；返回 (结果, 跳过原因列表)。"""
    from policypulse.profile import load_profile

    notes: list[str] = []
    fn = llm_fn or llm_fn_default

    # 候选：所有事件里得分最高的个股，按代码去重
    rows: list[tuple[float, str, str, dict]] = []
    seen: set[str] = set()
    for ev in events:
        for t in ev.tickers:
            code = t.get("ticker_code", "")
            if not code or code in seen:
                continue
            seen.add(code)
            rows.append((float(t.get("score", 0.0)), code, t.get("ticker_name", ""), t))
    rows.sort(key=lambda r: -r[0])

    dives: list[DeepDive] = []
    for _, code, name, row in rows[: settings.deepdive_top_k]:
        event = None
        for ev in events:
            if any(t.get("ticker_code") == code for t in ev.tickers):
                event = ev
                break
        if event is None:
            continue
        profile = load_profile(client, code)
        if not profile.has_profile:
            notes.append(f"{name or code}：{SKIP_NO_PROFILE}")
            continue

        policy_text = (
            f"标题：{event.title}\n"
            f"发布机构：{event.org or '-'}\n"
            f"发布时间：{event.published_at or '-'}\n"
            f"摘要：{row.get('why', '')}\n"
            f"原文链接：{event.source_url}"
        )
        profile_text = (
            f"公司名称：{profile.name or name}\n"
            f"所属行业：{profile.industry or '-'}\n"
            f"主营业务：{profile.main_business or '-'}\n"
            f"主营构成：{'、'.join(profile.segments) or '-'}\n"
            f"经营范围：{profile.scope or '-'}"
        )
        try:
            raw = fn(policy_text, profile_text)
        except Exception as exc:  # noqa: BLE001 - LLM 任何失败都不能拖垮日报
            notes.append(f"{name or code}：个股深度研读调用失败（{type(exc).__name__}），跳过")
            continue
        if raw is None:
            notes.append(f"{name or code}：{SKIP_NO_LLM if fn is llm_fn_default else SKIP_INVALID}")
            continue

        checked = validate({**raw, "policy_url": event.source_url,
                            "raw_snapshot_path": event.snapshot_path}, code, settings)
        if checked is None:
            notes.append(f"{name or code}：{SKIP_INVALID}")
            continue

        dives.append(DeepDive(
            ticker_code=code,
            ticker_name=name,
            policy_title=event.title,
            policy_summary=row.get("why", ""),
            policy_quote=profile.main_business or profile.industry,
            policy_url=event.source_url,
            profile_business=profile.main_business,
            transmission=checked["transmission"],
            competition=checked["competition"],
            impact=checked["impact"],
            substantive=checked["substantive"],
            risks=checked["risks"],
            sources=checked["sources"],
            event_id=event.event_id,
        ))
    return dives, notes

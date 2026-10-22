"""可选 LLM 后端：有 key 才启用，没有就静默回退到规则抽取。

只用 OpenAI 兼容的 /chat/completions 接口，不引入额外 SDK 依赖。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from policypulse.http import DEFAULT_UA

ENV_BASE_URL = "POLICYPULSE_LLM_BASE_URL"
ENV_KEY = "POLICYPULSE_LLM_API_KEY"
ENV_MODEL = "POLICYPULSE_LLM_MODEL"


def llm_available() -> bool:
    """同时有 base_url 与 key 才认为可用；OPENAI/OPENROUTER/DASHSCOPE 也兼容。"""
    if os.environ.get(ENV_BASE_URL) and os.environ.get(ENV_KEY):
        return True
    if os.environ.get("OPENAI_API_KEY") and os.environ.get(ENV_BASE_URL):
        return True
    return False


def _config() -> tuple[str, str, str]:
    base = os.environ.get(ENV_BASE_URL, "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get(ENV_KEY) or os.environ.get("OPENAI_API_KEY") or ""
    model = os.environ.get(ENV_MODEL, "qwen-plus")
    return base, key, model


PROMPT_VERSION = "llm-v2"

_SYSTEM = (
    "你是政策研究助理。从给定的会议/政策通稿中抽取政策信号。"
    "只输出 JSON 数组，每个元素包含: summary, quote, direction(利好/利空/中性), "
    "strength(1-5), horizon(短期(<1月)/中期(1–6月)/长期(>6月)), novelty(新增/加码/延续/重申/收紧), "
    "instruments(数组), theme_tags(数组, ≤5), affected_regions(数组), confidence(0-1), "
    "beneficiary_specificity(受益主体明确性，只能是以下三者之一："
    "「有明确受益主体」=政策指名了受益企业/机构、具体项目、订单或量化额度；"
    "「有产业方向但无具体主体」=有一般产业或合作方向（如机制性双边合作、区域概念）"
    "但没有具名受益方和落点；"
    "「无受益链条」=既无具名受益方也无产业方向，纯样板表述或空泛机制安排)。"
    "beneficiary_specificity 必须依据原文语义判断，禁止用关键词猜测。"
    "quote 必须是正文中的原文片段，不得改写。"
)


def extract_with_llm(title: str, body: str, timeout: float = 60.0) -> list[dict]:
    """返回原始解析结果；调用方仍需跑 verify_quote 硬校验。"""
    base, key, model = _config()
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"标题：{title}\n\n正文：{body[:12000]}"},
        ],
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                 "User-Agent": DEFAULT_UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    start, end = content.find("["), content.rfind("]")
    if start < 0 or end < 0:
        return []
    return json.loads(content[start : end + 1])


DEEPDIVE_PROMPT_VERSION = "llm-deepdive-v2"

_DEEPDIVE_SYSTEM = (
    "你是政策研究员。给定一条政策原文片段与一家上市公司的公开业务资料，"
    "判断该政策对该公司的传导路径。只输出一个 JSON 对象，字段："
    "transmission(直接/间接/中性), competition(一句话竞争地位，≤60字), "
    "impact(一句话影响判断，≤80字), "
    "substantive(是/否/不确定)——综合 transmission 与 impact 做语义判定："
    "只有 impact 明确正面（直接受益、显著拉动、明确订单、纳入试点等）才输出「是」；"
    "impact 为否定性判断（影响有限、无具体、未提及、难以验证、未披露、无直接等）时，"
    "无论 transmission 是「直接」还是「间接」，一律输出「否」；无法判断输出「不确定」。"
    "risks(数组，≤3条，每条≤40字), "
    "sources(数组，每项 {label, ref}，ref 必须是给定的政策原文 URL 或 通达信F10:<code>)。"
    "资料里没有的事实不得写入结论；写不出就返回 {}。"
)


def deepdive_with_llm(policy_text: str, profile_text: str, timeout: float = 60.0) -> dict | None:
    """个股深度研读：返回模型原始 JSON；调用方必须再做溯源硬校验。"""
    base, key, model = _config()
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _DEEPDIVE_SYSTEM},
            {"role": "user", "content": f"【政策】\n{policy_text[:4000]}\n\n【公司公开资料】\n{profile_text[:6000]}"},
        ],
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                 "User-Agent": DEFAULT_UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        return json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None

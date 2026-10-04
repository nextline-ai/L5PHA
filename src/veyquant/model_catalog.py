"""Owner-visible presets; availability is distinct from a saved preference."""

from veyquant.model_prompts import prompt_catalog
from veyquant.shadow_inference import (
    MODEL_PRESETS,
    ROLE_REASONING,
    STRATEGY_PRESETS,
    model_selection,
    models_ready,
    reasoning_options,
    reasoning_selection,
)

NAMES = {
    "gpt-6-luna": ("GPT-6 Luna", "감시·정리 · OpenAI API"),
    "gpt-6-sol": ("GPT-6 Sol", "의사결정 · OpenAI API"),
    "gpt-5.6-luna": ("GPT-5.6 Luna", "빠른 확인 · OpenAI API"),
    "gpt-5.6-terra": ("GPT-5.6 Terra", "균형 잡힌 검토 · OpenAI API"),
    "gpt-5.6-sol": ("GPT-5.6 Sol", "심층 분석 · OpenAI API"),
    "gpt-6-astra": ("GPT-6 Astra", "심층 분석 · OpenAI API · 계정별 접근 확인"),
    "au.anthropic.claude-haiku-4-5-20251001-v1:0": (
        "Claude Haiku 4.5",
        "빠른 확인 · Bedrock 호주 추론",
    ),
    "au.anthropic.claude-sonnet-4-6": ("Claude Sonnet 4.6", "균형 잡힌 검토 · Bedrock 호주 추론"),
    "au.anthropic.claude-opus-4-6-v1": ("Claude Opus 4.6", "심층 분석 · Bedrock 호주 추론"),
    "gemini-3.5-flash-lite": ("Gemini 3.5 Flash-Lite", "빠른 확인 · Google Gemini API"),
    "gemini-3.8-flash": ("Gemini 3.8 Flash", "추가 검토 · Google Gemini API"),
    "gemini-2.5-pro": ("Gemini 2.5 Pro", "심층 분석 · Google Gemini API"),
}
ROLE_FIELDS = {"cheap_model": "cheap", "middle_model": "middle", "research_model": "research"}
LIMIT_PRESETS = [
    {
        "id": "small",
        "name": "100만원",
        "description": "주문 50만원 · 하루 손실 10만원",
        "limits": {
            "capital_krw": "1000000",
            "max_order_krw": "500000",
            "max_daily_loss_krw": "100000",
        },
    },
    {
        "id": "medium",
        "name": "500만원",
        "description": "주문 250만원 · 하루 손실 50만원",
        "limits": {
            "capital_krw": "5000000",
            "max_order_krw": "2500000",
            "max_daily_loss_krw": "500000",
        },
    },
    {
        "id": "large",
        "name": "1,000만원",
        "description": "주문 500만원 · 하루 손실 100만원",
        "limits": {
            "capital_krw": "10000000",
            "max_order_krw": "5000000",
            "max_daily_loss_krw": "1000000",
        },
    },
]


def connection_view(models, credentials=None):
    if models and any(m.startswith("amazon.nova") for m in models.values()):
        return {
            "ready": False,
            "message": "Nova를 제거했습니다. 모델을 다시 선택해주세요. 한도는 유지됩니다.",
        }
    missing = set(model_selection(models).values())
    missing = {
        m for m in missing if not models_ready(dict.fromkeys(ROLE_FIELDS.values(), m), credentials)
    }
    messages = []
    if any(m.startswith("gpt-") for m in missing):
        messages.append("OpenAI API 키와 선택한 모델의 접근 확인이 필요합니다.")
    if any(m.startswith("gemini-") for m in missing):
        messages.append("Gemini API 키 연결이 필요합니다. 연결 전에는 분석을 기다립니다.")
    if any("anthropic" in m for m in missing):
        messages.append(
            "이 설치에서는 OpenAI·Gemini로 시작하세요. Bedrock은 모델·검색 추가 구성이 필요합니다."
        )
    return {"ready": not missing, "message": " ".join(messages)}


def catalog_view(credentials=None):
    descriptions = {
        "chatgpt": (
            "ChatGPT",
            "GPT-6 Luna → Luna → Sol",
            "OpenAI API · ChatGPT 구독과 별도 이용 요금",
        ),
        "claude": ("Claude", "Haiku → Sonnet → Opus", "Bedrock 호주 추론 · 별도 API 키 불필요"),
        "gemini": ("Gemini", "Flash-Lite → Flash → Pro", "Google API · 별도 키와 이용 요금"),
    }
    return {
        "models": [
            {
                "id": mid,
                "name": name,
                "description": desc,
                "reasoning_options": reasoning_options(mid),
                "default_reasoning": reasoning_options(mid)[0],
                "ready": connection_view(dict.fromkeys(ROLE_FIELDS.values(), mid), credentials)[
                    "ready"
                ],
            }
            for mid, (name, desc) in NAMES.items()
        ],
        "presets": [
            {
                "id": key,
                "name": descriptions[key][0],
                "description": descriptions[key][1],
                "detail": descriptions[key][2],
                "models": values,
                "reasoning": reasoning_selection(values),
                **connection_view(values, credentials),
            }
            for key, values in MODEL_PRESETS.items()
        ],
        "limit_presets": LIMIT_PRESETS,
        "strategy_presets": [{"id": key, **value} for key, value in STRATEGY_PRESETS.items()],
        "defaults": model_selection(),
        "default_reasoning": dict(ROLE_REASONING),
        "role_prompts": prompt_catalog(),
        "daily_analysis_limit": 12,
    }

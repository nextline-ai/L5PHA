<div align="center">

# L5PHA

**Level 5 Fully Autonomous Investing**

Investment has reached Level 5.<br>
레벨 5 완전 자율 투자

**내 AWS에서 운용하고, Telegram에서 확인하는 나만의 AI 투자 시스템.**

[설치 시작하기](https://15.134.164.178/install) · [설치 안내](docs/installation.md) · [Telegram 열기](https://t.me/veyquant_bot)

[![CI](https://github.com/xtower-studio/L5PHA/actions/workflows/ci.yml/badge.svg)](https://github.com/xtower-studio/L5PHA/actions/workflows/ci.yml)

</div>

## 처음이라면 AWS New로 시작하세요

**AWS New를 기본으로 가장 권장합니다.** 내 투자 서버를 하나의 Project로 만들고 월 AWS 비용 상한인 **Spend limit**을 설정할 수 있습니다. 한도에 도달하면 AWS가 Project의 리소스를 정지시켜 비용을 제한합니다. 일반적인 예산 알림보다 강한 비용 보호가 필요한 개인 운용에 적합합니다. [AWS 공식 안내](https://docs.aws.amazon.com/accounts/latest/reference/create-spend-limit.html)

1. [AWS New 시작하기](https://signin.aws.amazon.com/signup?request_type=register)에서 가입하고 **L5PHA** Project를 만드세요.
2. Project의 리전이 **Sydney**인지 확인하세요. 현재 설치가 지원하는 리전입니다.
3. Paid Plan을 사용한다면 [AWS Settings](https://settings.aws.com/) → **Billing → Cost by project → Spend limit → Set limit**에서 월 한도를 설정하세요.
4. [L5PHA 설치 화면](https://15.134.164.178/install)으로 돌아와 안내를 따라가세요.

Spend limit은 **Paid Plan에서 직접 설정**해야 합니다. Free Tier를 제공받았다면 해당 계정의 크레딧·기간을 확인하고, 유료 전환 시 한도를 설정하세요. AWS New는 순차 제공 중이므로 아직 사용할 수 없다면 설치 화면에서 **기존 AWS**를 선택할 수 있습니다. [AWS 가입 방식](https://docs.aws.amazon.com/accounts/latest/reference/sign-in-new.html)

> **AWS 비용, AI API 비용, 투자금은 서로 다릅니다.**
> AWS 한도에는 OpenAI·Gemini에 직접 지불하는 API 요금과 주식 매매 손실이 포함되지 않습니다. 각각의 한도를 따로 관리하세요. 한도 도달로 서버가 멈추면 감시·주문 관리도 멈추지만, 토스의 미체결 주문과 보유 주식은 남습니다.

**Spend limit을 유지하려면 Advanced features로 전환하지 마세요.** 전환하면 한도가 제거되며 되돌릴 수 없습니다. L5PHA는 이 전환을 자동으로 실행하지 않습니다. [AWS 공식 설명](https://docs.aws.amazon.com/accounts/latest/reference/activate-advanced-features.html)

## 준비부터 시작까지

프로그래밍 지식, 터미널, `.env` 편집 없이 진행할 수 있습니다. **계정과 API가 준비된 상태에서 약 30분 내 시작**하는 것이 목표이며, 가입·키 발급·승인 대기 시간은 별도입니다.

| 단계 | 할 일 |
| --- | --- |
| 1. 준비 | AWS New Project와 비용 한도, 토스증권 Open API 키, Telegram을 준비합니다. |
| 2. AWS 설치 | 설치 화면의 버튼을 누르고 AWS에서 생성을 승인합니다. 서버·저장소·고정 IP·HTTPS가 자동으로 준비됩니다. |
| 3. 내 Telegram 연결 | AWS 출력 탭의 **OpenTelegram**을 열고, 미니앱에 최초 연결 코드를 입력합니다. |
| 4. 토스 연결 | 미니앱에 표시된 고정 IP를 토스에 등록하고 API 키를 입력합니다. |
| 5. 내 투자 기준 | AI 모델·API 키, 총 운용금액·주문 상한·하루 손실 한도, 전략을 정합니다. |
| 6. 시작 | 관찰·분석부터 시작하거나, 확인을 마친 뒤 실거래를 직접 활성화합니다. |

**[지금 설치 시작하기 →](https://15.134.164.178/install)**

처음부터 실거래가 켜지지는 않습니다. 설치 후 컴퓨터를 꺼도 AWS 서버가 계속 동작합니다. DART·KRX는 나중에 선택적으로 연결할 수 있습니다. 자세한 화면 순서와 복구 방법은 [설치 안내](docs/installation.md)를 참고하세요.

## L5PHA로 할 수 있는 일

- **국내 주식 탐색·운용:** 시장에서 후보를 찾고, 계좌와 현재 보유 종목을 고려해 판단합니다.
- **세 AI 계층의 역할 분리:** 감시 → 정리 → 의사결정으로 필요한 상황에 집중합니다. 기록도 계층별로 확인합니다.
- **나에게 맞는 AI와 전략:** ChatGPT·Claude·Gemini 프리셋, 커스텀 모델·추론 설정, 안정·적극·공격·커스텀 전략을 제공합니다.
- **Telegram에서 관리:** 월간 AI 운용손익, 보유 상태, 주문·체결, 과거 판단을 확인하고 설정을 바꿉니다.
- **사용자 지시:** 매수·매도·관찰 아이디어를 제안하면 AI가 근거와 상황을 검토합니다.
- **선택적인 실거래:** 사용자가 정한 운용금액과 조건 안에서 자동 주문합니다. 언제든 비활성화하거나 일시 중단할 수 있습니다.

빠른 설치는 **OpenAI 또는 Gemini API**를 사용합니다. 해당 구독 서비스의 월 구독료와 API 요금은 별도입니다. Claude/Bedrock은 AWS 모델 접근·검색 추가 구성이 필요하며, AWS New의 교차 리전 추론 제한을 적용받습니다. [AWS New 지원 범위](https://docs.aws.amazon.com/accounts/latest/reference/supported-services-sign-up-new.html)

## 내 계좌와 키는 어디에 있나요?

계좌 연결과 투자 기록은 **본인의 AWS 서버**에서 처리합니다. 토스 키는 소유자 인증 후 해당 AWS로 직접 전송하고 암호화해 보관합니다. 공용 Telegram 봇은 개인 서버의 화면을 여는 역할을 하며, Bot Token은 사용자 서버에 배포하지 않습니다.

최초 연결 코드와 API 키는 **미니앱의 입력란**에만 입력하세요. Telegram 채팅이나 GitHub 이슈에 올리지 마세요. [보안 구조·문제 제보](SECURITY.md)

## 자주 묻는 질문

**무료인가요?**  
AWS 서버·저장소·고정 IP 등의 비용과 선택한 AI·검색 사용료가 발생합니다. AWS New의 Spend limit으로 AWS 비용을 관리하고, 외부 AI 제공자의 비용 설정도 확인하세요.

**한도를 정하면 투자 손실도 보장되나요?**  
아닙니다. AWS 비용 한도와 투자 손실 한도는 다릅니다. 가격 급변, 체결 가격, 장애에 따라 실제 손실은 설정한 값보다 커질 수 있습니다. AI 판단의 수익이나 정확성을 보장하지 않습니다.

**앱을 닫으면 멈추나요?**  
앱을 닫아도 서버는 계속 동작합니다. 중단하려면 미니앱의 운용 중단·실거래 비활성화를 사용하세요. 서버 정지·삭제만으로 토스 주문이 취소되거나 보유 주식이 매도되지는 않습니다.

**다른 사람이 공용 봇을 열면 제 계좌가 보이나요?**  
각 설치는 최초 연결 코드와 Telegram 사용자 서명을 확인해 한 명의 소유자에게 연결됩니다. 공용 봇의 다른 사용자가 개인 계좌 화면에 로그인할 수 있는 구조가 아닙니다.

**코드를 잃었거나 Telegram이 열리지 않아요.**  
AWS 출력 탭의 **RecoveryActions**에서 코드 재발급·운용 중단·세션 폐기·연결 해제를 실행할 수 있습니다. [복구 및 삭제 안내](docs/installation.md#복구와-삭제)

## 개발·검증

개발자는 [개발 안내](docs/development.md)와 [구조 설명](docs/architecture.md)을 참고하세요. 내부 패키지명 `veyquant`는 기존 설치와 호환되도록 유지합니다.

```sh
git clone https://github.com/xtower-studio/L5PHA.git
cd L5PHA
uv sync --locked --extra dev
uv run veyquant demo --db var/demo.sqlite3
uv run pytest
```

`demo`는 실제 계좌·AWS·API 키 없이 실행하는 모의 예제입니다. 공개 저장소에는 개인 계좌 데이터, API 키, 운영 DB, 사적인 운용 이력을 포함하지 않습니다. 라이선스는 아직 지정하지 않았으며 소스 공개가 별도의 재배포 라이선스를 의미하지는 않습니다.

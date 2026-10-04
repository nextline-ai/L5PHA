# L5PHA 설치·복구 안내

[설치 시작](https://15.134.164.178/install) · [처음으로](../README.md)

## 추천 경로: AWS New

처음 시작한다면 **AWS New → L5PHA Project → Spend limit 설정**을 권장합니다. Paid Plan의 Project별 세전 AWS 비용 상한이며, 한도 도달 시 AWS가 리소스를 정지시킵니다. 최소 설정액은 $20 또는 AWS가 계산한 예상 사용액 중 큰 값입니다. [공식 설정 안내](https://docs.aws.amazon.com/accounts/latest/reference/create-spend-limit.html)

설치 화면에서 AWS New가 기본 선택됩니다. Free Tier를 제공받은 경우 요금제를 바꿔 안내를 확인할 수 있습니다. 실제 청구 요금제 변경이나 한도 설정은 본인이 **AWS Settings**에서 수행합니다. 화면의 확인 체크만으로 AWS 한도가 생성되는 것은 아닙니다.

OpenAI·Gemini API 비용과 투자 손실은 별도입니다. Advanced features를 활성화하면 Spend limit이 없어지고 되돌릴 수 있으리라 기대해서는 안 됩니다. [전환 시 변경 사항](https://docs.aws.amazon.com/accounts/latest/reference/activate-advanced-features.html)

AWS New는 계정·지역별로 순차 제공 중입니다. 지원되지 않으면 기존 AWS를 사용할 수 있습니다. 현재 자동 설치는 **Sydney(ap-southeast-2)** 전용입니다. AWS New Project의 실제 리전이 다르면 설치를 진행하지 마세요. [지원 서비스](https://docs.aws.amazon.com/accounts/latest/reference/supported-services-sign-up-new.html)

## 설치 순서

1. 토스증권 Open API 키와 Telegram을 준비하고 설치 화면에서 AWS 요금제·비용 적용 범위를 확인합니다.
2. 설치 페이지가 만든 **최초 연결 코드**를 보관합니다. 코드는 브라우저 탭에 임시 보관되며 AWS에는 해시만 전달됩니다. 이 탭을 유지하세요.
3. AWS New는 AWS Settings에서 **설치할 Project의 콘솔**을 먼저 엽니다. 설치 버튼을 누른 뒤 AWS 화면의 계정이 그 Project인지 확인합니다.
4. AWS에서 권한 생성을 승인하고 스택을 생성합니다. `CREATE_COMPLETE`를 기다립니다. HTTPS 상태 확인까지 성공해야 완료됩니다.
5. AWS **출력(Outputs) → OpenTelegram**을 열고, **내 L5PHA 열기**에서 최초 코드를 입력합니다. 채팅에 코드를 보내지 마세요.
6. 미니앱의 고정 IP를 토스 API 허용 IP로 등록한 뒤 Client ID·Client Secret을 입력합니다. 여러 계좌라면 안내에 따라 계좌 선택 번호를 입력합니다.
7. OpenAI 또는 Gemini 키와 모델을 연결하고 운용 한도·전략을 정합니다. 실거래 활성화 여부는 마지막에 직접 선택합니다. DART·KRX는 선택사항입니다.

AWS 생성과 토스·AI 승인 상태에 따라 걸리는 시간이 달라집니다. 30분은 준비된 계정을 기준으로 한 목표입니다. 새 AWS 서버 자동 구축 검증은 약 6분 19초였으며, 사용자 키 발급·입력 시간과 신규 Telegram 사용자 등록을 포함하지 않습니다.

## 복구와 삭제

AWS 출력의 **RecoveryActions**에서 해당 인스턴스를 선택하고 실행합니다. AWS 소유자 권한이 필요합니다.

| 작업 | 동작 |
| --- | --- |
| `pause` | 운용을 중단합니다. |
| `revoke-sessions` | 미니앱 로그인 세션을 폐기합니다. |
| `new-code` | 미연결 서버의 최초 코드를 10분 동안 유효하게 재발급합니다. 기존 소유자를 바꾸지는 않습니다. |
| `disconnect-telegram` | 먼저 운용을 중단하고 소유자 연결·세션을 해제합니다. 이후 새 코드로 다시 연결합니다. |

처음 설치한 코드는 서버 초기화 후 1시간 동안 유효합니다. 재발급 코드는 AWS 실행 결과에만 표시되므로 외부에 공유하지 마세요. 재연결만으로 중단된 실거래를 자동 재개하지 않습니다.

Spend limit으로 Project가 멈췄다면 AWS Settings에서 한도를 조정하고 필요한 리소스를 재시작하세요. **90일 동안 복구하지 않으면 Project 데이터가 삭제될 수 있습니다.** 토스 주문·보유 상태를 직접 확인한 뒤 재개하세요.

완전히 삭제할 때는 먼저 미니앱에서 실거래를 비활성화하고 토스의 미체결을 확인합니다. AWS 스택을 삭제해도 복구용 **데이터 EBS·Secret·KMS 키·일부 로그**는 남을 수 있습니다. 출력의 자원 식별자를 보관하고 필요한 데이터를 백업한 뒤 남은 리소스를 별도로 삭제하세요. 보관 리소스에는 비용이 발생할 수 있습니다. 서버 삭제는 주식 매도나 토스 키 폐기가 아닙니다.

신규 설치기는 기존 서버의 데이터 복구·이전·자동 업그레이드 도구가 아닙니다. 기존 DB를 과거 백업으로 덮어쓰면 이미 제출된 주문과 불일치할 수 있습니다.

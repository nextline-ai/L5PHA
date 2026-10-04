# AWS 인프라

일반 사용자는 [설치 마법사](https://15.134.164.178/install)를 사용하세요. **AWS New와 Project Spend limit**을 기본 권장합니다. 자동 설치는 Sydney 전용입니다.

개발자는 `scripts/build_install_release.py`로 신규 설치 템플릿을 생성합니다. 생성된 템플릿은 VPC, 고정 IP, EC2, 암호화 데이터 볼륨, Lambda, KMS, Secrets Manager와 복구 명령을 묶습니다. HTTPS와 앱 상태 확인이 끝나야 설치 완료로 표시합니다.

- `runtime.yaml`: 호스트·격리·저장소 구성의 기반. 단독으로 완성된 앱을 설치하지 않습니다.
- `analysis.yaml`: 제한한 분석 Lambda와 모델 접근. `scripts/render_analysis_template.py`로 코드와 동기화합니다.
- `research.yaml`: Bedrock/AgentCore 고급 검색 구성. 빠른 설치의 OpenAI·Gemini 자체 검색에는 불필요합니다.
- `operations.yaml`: 선택적인 운영·백업 구성.
- `poc.yaml`, `poc.guard`: 초기 연결 검증용이며 일반 사용자 설치용이 아닙니다.

AWS New의 Project 권한·서비스·리전 제한을 우회하기 위해 Advanced features로 전환하지 마세요. Spend limit을 잃을 수 있습니다. [AWS 공식 전환 설명](https://docs.aws.amazon.com/accounts/latest/reference/activate-advanced-features.html)

삭제 후 보관 데이터 볼륨·Secret·KMS·일부 로그는 남을 수 있습니다. [복구와 삭제](../docs/installation.md#복구와-삭제)를 확인하세요. 실주문 원장을 과거 백업으로 덮어쓰지 마세요.

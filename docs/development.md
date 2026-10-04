# 개발과 검증

Python 3.12 이상, uv, Node.js가 필요합니다. 일반 사용자는 [설치 화면](https://15.134.164.178/install)을 이용하세요.

```sh
git clone https://github.com/nextline-ai/L5PHA.git
cd L5PHA
uv sync --locked --extra dev
npm ci --ignore-scripts
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
npm run build:ui
npm run test:ui
npx playwright install chromium webkit
npm run test:browser
```

테스트는 모의 계좌·가짜 키를 사용하며 실제 네트워크 연결을 차단합니다. 실제 투자 결과나 계좌를 테스트 fixture로 커밋하지 마세요. UI 원본 스타일은 `ui/l5pha.css`, 생성 결과는 `src/veyquant/web/app.css`입니다. CI에서 재생성 결과 일치를 확인합니다.

## 신규 설치 배포물

```sh
uv run python scripts/render_analysis_template.py
uv run python scripts/build_install_release.py \
  --output var/install-release \
  --base-url https://YOUR-PUBLIC-BUCKET.s3.ap-southeast-2.amazonaws.com/VERSION
uv run cfn-lint var/install-release/install.json --regions ap-southeast-2
```

배포물은 허용한 코드 파일만 포함하고 해시를 고정합니다. `.env`, `var/`, DB, 개인 문서는 포함하지 않습니다. 공개 전에 직접 결과물을 검사하고 버전별 파일을 변경하지 마세요. 이미 공개한 파일을 교체하지 말고 새 버전 경로를 사용합니다.

## 운영 도구와 일반 설치의 구분

`activate_access_bot.py`, `configure_telegram_menu.py`, `deploy/l5pha-access.service`는 공용 봇 운영자용입니다. 일반 사용자는 실행하거나 Bot Token을 받을 필요가 없습니다. 공용 설치 URL과 Bot ID는 의도적으로 공개된 식별자이며 인증키가 아닙니다.

`.env.example`은 개발·진단용 빈 입력 양식입니다. 실제 값은 로컬 `.env` 또는 사용자 AWS에만 보관합니다. 원문 환경파일·키·세션·계좌 응답·SSM 로그를 이슈나 PR에 첨부하지 마세요. 개발·배포는 코드와 설정의 차이만 검토하고 실제 매매를 테스트 목적으로 만들지 않습니다.

## 변경 기여

작고 검증 가능한 변경, 문제 재현 과정, 수행한 테스트를 포함한 PR을 권장합니다. 소스 공개와 재배포 라이선스는 별도이며 프로젝트 라이선스는 아직 지정하지 않았습니다. 보안 문제는 [SECURITY.md](../SECURITY.md)를 따르세요.

import argparse
import json
import time
from decimal import Decimal

from veyquant.domain import AccountSnapshot, Event, Evidence, Proposal, Quote, RiskPolicy
from veyquant.pipeline import Decision, Pipeline, ResearchCatalog
from veyquant.risk import PaperRiskEngine
from veyquant.store import Store


class DemoModel:
    version = "fixture-v1"

    def decide(self, role, event, evidence):
        if role != "research":
            return Decision("escalate", "모의 중요 사건: 다음 단계로 전달")
        if not evidence:
            return Decision("research", "모의 원문 근거 조회", evidence_id="fixture-source")
        return Decision(
            "propose",
            "모의 매수 제안",
            proposal=Proposal(
                f"{event.id}:proposal",
                event.id,
                event.symbol,
                event.currency,
                2,
                Decimal("100"),
                event.observed_at,
                event.observed_at + 60,
                "demo-v1",
                "fixture-strategy-v1",
                (evidence[0].id,),
                "가상 자료 기반 동작 확인",
                "모의 자료는 실제 시장을 설명하지 않음",
                "실제 체결·수익을 추정하지 않음",
            ),
        )


def demo(store: Store):
    now = int(time.time())
    event = Event("demo-event-v1", "DEMO", "KRW", now, True)
    evidence = Evidence(
        "fixture-source",
        "fixture://local",
        now,
        now,
        "DEMO",
        "KRW",
        "per_share",
        "가상 사건의 가상 근거",
    )
    policy = RiskPolicy(
        "demo-v1",
        "KRW",
        frozenset({"DEMO"}),
        Decimal("500"),
        Decimal("1000"),
        Decimal("500"),
        Decimal("0.05"),
    )
    account = AccountSnapshot("KRW", Decimal("1000"), Decimal(0), {}, Decimal(0), {}, now, True)
    pipeline = Pipeline(
        store,
        (DemoModel(), DemoModel(), DemoModel()),
        ResearchCatalog([evidence]),
        PaperRiskEngine(store, policy),
    )
    result = pipeline.run(event, account, Quote("DEMO", "KRW", Decimal("100"), now), now)
    print(
        json.dumps(
            {
                "mode": "paper",
                "live_order_supported": False,
                "result": result.reason if result else "held_or_duplicate",
                "audit_records": len(store.rows()),
            },
            ensure_ascii=False,
        )
    )


def main():
    parser = argparse.ArgumentParser(description="Veyquant non-live development CLI")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("demo", "audit", "stop", "invite", "install-invitation", "revoke-sessions"):
        command = commands.add_parser(name)
        command.add_argument("--db", default="var/demo.sqlite3")
        if name in {"invite", "install-invitation", "revoke-sessions"}:
            command.add_argument("--bot-id", type=int, required=True)
        if name == "install-invitation":
            command.add_argument("--hash", required=True)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--region", required=True)
    doctor.add_argument("--model-id")
    doctor.add_argument("--expected-account")
    doctor.add_argument("--invoke", action="store_true")
    for name in ("check-config", "check-telegram"):
        command = commands.add_parser(name)
        command.add_argument("--env-file", default=".env")
    probe = commands.add_parser("probe-toss")
    probe.add_argument("--env-file", default=".env")
    probe.add_argument("--expected-ip", required=True)
    probe.add_argument("--seconds", type=int, default=75)
    serve = commands.add_parser("serve")
    serve.add_argument("--db", default="var/management.sqlite3")
    serve.add_argument("--bot-id", type=int, required=True)
    serve.add_argument("--origin", required=True)
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--development", action="store_true")
    serve.add_argument("--unix-socket")
    serve.add_argument("--collector-status")
    serve.add_argument("--shadow-control")
    serve.add_argument("--shadow-reports")
    serve.add_argument("--account-status")
    serve.add_argument("--execution-control")
    serve.add_argument("--execution-status")
    serve.add_argument("--universe")
    args = parser.parse_args()
    if args.command == "probe-toss":
        import asyncio

        from veyquant.local_config import read_local_config
        from veyquant.toss_probe import probe_toss

        result = asyncio.run(
            probe_toss(read_local_config(args.env_file), args.expected_ip, seconds=args.seconds)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "passed" else 2
    if args.command in {"check-config", "check-telegram"}:
        import asyncio

        from veyquant.local_config import config_presence, inspect_telegram, read_local_config

        values = read_local_config(args.env_file)
        result = (
            config_presence(values)
            if args.command == "check-config"
            else asyncio.run(
                inspect_telegram(
                    values.get("TELEGRAM_BOT_TOKEN", ""), values.get("TELEGRAM_BOT_USERNAME", "")
                )
            )
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") == "blocked" else 0
    if args.command == "doctor":
        from veyquant.preflight import aws_checks

        if args.invoke and (not args.model_id or not args.expected_account):
            parser.error("--invoke requires --model-id and --expected-account")
        result = aws_checks(
            args.region, args.model_id, invoke=args.invoke, expected_account=args.expected_account
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        incomplete = args.invoke and not result["invocation_verified"]
        return 2 if incomplete or any(c["status"] == "blocked" for c in result["checks"]) else 0
    if args.command == "serve":
        import uvicorn

        from veyquant.management import ManagementConfig, create_app

        config = ManagementConfig(
            args.db,
            args.origin,
            args.bot_id,
            args.development,
            args.collector_status,
            args.shadow_control,
            args.shadow_reports,
            args.account_status,
            args.execution_control,
            args.execution_status,
            args.universe,
        )
        # Only loopback. A reviewed TLS proxy is required for remote access.
        uvicorn.run(
            create_app(config),
            host="127.0.0.1",
            port=args.port,
            uds=args.unix_socket,
            access_log=False,
            proxy_headers=False,
            server_header=False,
        )
        return 0
    store = Store(args.db)
    try:
        if args.command == "demo":
            demo(store)
        elif args.command == "audit":
            print(json.dumps(store.rows(), ensure_ascii=False, indent=2))
        elif args.command == "stop":
            store.stop()
            print('{"new_proposals_stopped": true}')
        else:
            from veyquant.telegram_auth import OwnerAuth, TelegramVerifier

            auth = OwnerAuth(store, TelegramVerifier(args.bot_id))
            if args.command == "invite":
                print(
                    json.dumps(
                        {"invitation": auth.issue_invitation(int(time.time())), "expires_in": 600}
                    )
                )
            elif args.command == "install-invitation":
                auth.install_invitation_hash(args.hash, int(time.time()))
                print('{"invitation_installed": true, "expires_in": 3600}')
            else:
                auth.revoke_sessions()
                print('{"sessions_revoked": true}')
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

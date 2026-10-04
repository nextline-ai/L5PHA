from botocore.exceptions import ClientError, NoCredentialsError

from veyquant.preflight import aws_checks, failure


class FakeSession:
    def __init__(self, *, verification_pending=False, agreement="AVAILABLE", inference_types=None):
        self.calls = []
        self.verification_pending = verification_pending
        self.agreement = agreement
        self.inference_types = ["ON_DEMAND"] if inference_types is None else inference_types

    def client(self, name, **kwargs):
        self.calls.append(name)
        return self

    def get_caller_identity(self):
        return {"Account": "111111111111", "Arn": "arn:aws:iam::111111111111:root"}

    def list_foundation_models(self):
        return {
            "modelSummaries": [
                {"modelId": "fixture.model-v1", "inferenceTypesSupported": self.inference_types}
            ]
        }

    def get_foundation_model_availability(self, **kwargs):
        return {
            "authorizationStatus": "AUTHORIZED",
            "regionAvailability": "AVAILABLE",
            "agreementAvailability": {"status": self.agreement},
            "entitlementAvailability": "AVAILABLE",
        }

    def converse(self, **kwargs):
        self.calls.append("converse")
        assert kwargs["inferenceConfig"]["maxTokens"] == 32
        if self.verification_pending:
            raise ClientError(
                {
                    "Error": {
                        "Code": "AccessDeniedException",
                        "Message": "Your account is currently being verified. private-marker",
                    }
                },
                "Converse",
            )
        return {
            "stopReason": "end_turn",
            "output": {"message": {"content": [{"text": "READY"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 1, "totalTokens": 11},
        }


def test_availability_does_not_imply_successful_inference():
    session = FakeSession()
    result = aws_checks("ap-southeast-2", "fixture.model-v1", session=session)
    assert result["invocation_verified"] is False
    assert "converse" not in session.calls
    assert result["checks"][-1]["status"] == "not_run"


def test_real_failure_classified_without_provider_message():
    result = aws_checks(
        "ap-southeast-2",
        "fixture.model-v1",
        session=FakeSession(verification_pending=True),
        invoke=True,
        expected_account="111111111111",
    )
    assert result["checks"][-1]["detail"]["reason"] == "account_verification_pending"
    assert result["invocation_verified"] is False
    assert "private-marker" not in str(result)


def test_wrong_account_stops_before_model_calls():
    session = FakeSession()
    result = aws_checks("ap-southeast-2", session=session, expected_account="999999999999")
    assert session.calls == ["sts"]
    assert result["checks"][-1]["detail"]["reason"] == "account_mismatch"


def test_missing_agreement_or_profile_does_not_invoke():
    for session in [
        FakeSession(agreement="NOT_AVAILABLE"),
        FakeSession(inference_types=["INFERENCE_PROFILE"]),
    ]:
        result = aws_checks(
            "ap-southeast-2",
            "fixture.model-v1",
            session=session,
            invoke=True,
            expected_account="111111111111",
        )
        assert result["invocation_verified"] is False
        assert "converse" not in session.calls


def test_success_preserves_usage_but_does_not_enable_live():
    result = aws_checks(
        "ap-southeast-2",
        "fixture.model-v1",
        session=FakeSession(),
        invoke=True,
        expected_account="111111111111",
    )
    assert result["invocation_verified"] is True
    assert result["ready_for_live"] is False
    assert result["checks"][-1]["detail"]["usage"]["totalTokens"] == 11
    assert "111111111111" not in str(result)


def test_missing_credentials_is_explicit():
    assert failure(NoCredentialsError())["code"] == "NoCredentialsError"

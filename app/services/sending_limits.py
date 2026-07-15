from dataclasses import dataclass

from app.models import SenderAccount


@dataclass(frozen=True)
class SendingLimitAdvice:
    provider: str
    recommended_daily_limit: int
    caution_daily_limit: int
    official_limit: str
    note: str


DEFAULT_RECOMMENDED_DAILY_LIMIT = 30


def infer_provider(sender: SenderAccount) -> str:
    host = sender.smtp_host.lower()
    email = sender.email.lower()
    if email.endswith("@gmail.com"):
        return "Personal Gmail"
    if "gmail" in host or "google" in host:
        return "Google Workspace"
    if "office365" in host or "outlook" in host or "microsoft" in host:
        return "Microsoft 365"
    return "Custom SMTP"


def get_sending_limit_advice(sender: SenderAccount) -> SendingLimitAdvice:
    provider = infer_provider(sender)
    if provider == "Personal Gmail":
        return SendingLimitAdvice(
            provider=provider,
            recommended_daily_limit=20,
            caution_daily_limit=40,
            official_limit="Google states personal Gmail may hit a limit above 500 recipients/emails per day.",
            note="For cold outreach, keep this lower than business mailboxes. Prefer 20/day at the start.",
        )
    if provider == "Google Workspace":
        return SendingLimitAdvice(
            provider=provider,
            recommended_daily_limit=30,
            caution_daily_limit=40,
            official_limit="Google Workspace lists 2,000 messages/day per user and 3,000 external recipients/day.",
            note="Provider limit is not a cold-outreach target. Use 30/day first, then adjust from reply/bounce signals.",
        )
    if provider == "Microsoft 365":
        return SendingLimitAdvice(
            provider=provider,
            recommended_daily_limit=30,
            caution_daily_limit=40,
            official_limit="Microsoft 365 lists 10,000 recipients/day and 30 messages/minute, but not for bulk mailing.",
            note="Use the conservative outreach range, not the platform hard cap.",
        )
    return SendingLimitAdvice(
        provider=provider,
        recommended_daily_limit=DEFAULT_RECOMMENDED_DAILY_LIMIT,
        caution_daily_limit=40,
        official_limit="No universal public cap; follow your SMTP provider policy.",
        note="Default recommendation is 30/day/mailbox for BD outreach, with manual review before sending.",
    )

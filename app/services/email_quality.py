from dataclasses import dataclass
from functools import lru_cache

import dns.exception
import dns.resolver
from email_validator import EmailNotValidError, caching_resolver, validate_email


COMMON_DOMAIN_TYPOS = {
    "gamil.com": "gmail.com",
    "gmial.com": "gmail.com",
    "gnail.com": "gmail.com",
    "hotmial.com": "hotmail.com",
    "hotmai.com": "hotmail.com",
    "outlok.com": "outlook.com",
    "outllok.com": "outlook.com",
    "yaho.com": "yahoo.com",
}

DISPOSABLE_DOMAINS = {
    "10minutemail.com",
    "guerrillamail.com",
    "mailinator.com",
    "tempmail.com",
    "temp-mail.org",
    "yopmail.com",
}

RESERVED_DOMAINS = {
    "example.com",
    "example.net",
    "example.org",
    "invalid",
    "localhost",
    "test",
}

BLOCKED_LOCAL_PARTS = {
    "abuse",
    "bounce",
    "mailer-daemon",
    "noreply",
    "no-reply",
    "postmaster",
}

PLACEHOLDER_LOCAL_PARTS = {
    "dummy",
    "email",
    "example",
    "name",
    "sample",
    "test",
    "unknown",
    "user",
    "yourname",
}


@dataclass(frozen=True)
class EmailQualityResult:
    status: str
    normalized_email: str | None
    reason: str

    @property
    def is_blocking(self) -> bool:
        return self.status == "invalid"


@lru_cache(maxsize=1)
def _dns_resolver():
    return caching_resolver(timeout=3)


def _split_email(email: str) -> tuple[str, str]:
    local, _, domain = email.strip().lower().partition("@")
    return local, domain


def _domain_accepts_mail(domain: str) -> tuple[str, str]:
    resolver = _dns_resolver()
    saw_no_answer = False
    try:
        mx_records = resolver.resolve(domain, "MX")
        if mx_records:
            return "valid", "Domain has MX records."
    except dns.resolver.NXDOMAIN:
        return "invalid", "Domain does not exist."
    except dns.resolver.NoAnswer:
        saw_no_answer = True
    except dns.exception.DNSException as exc:
        return "risky", f"DNS lookup could not confirm MX records: {exc}"

    try:
        a_records = resolver.resolve(domain, "A")
        if a_records:
            return "valid", "Domain has no MX record but has A records as SMTP fallback."
    except dns.resolver.NXDOMAIN:
        return "invalid", "Domain does not exist."
    except dns.resolver.NoAnswer:
        saw_no_answer = True
    except dns.exception.DNSException as exc:
        return "risky", f"DNS lookup could not confirm A records: {exc}"

    try:
        aaaa_records = resolver.resolve(domain, "AAAA")
        if aaaa_records:
            return "valid", "Domain has no MX record but has AAAA records as SMTP fallback."
    except dns.resolver.NXDOMAIN:
        return "invalid", "Domain does not exist."
    except dns.resolver.NoAnswer:
        saw_no_answer = True
    except dns.exception.DNSException as exc:
        return "risky", f"DNS lookup could not confirm AAAA records: {exc}"

    if saw_no_answer:
        return "invalid", "Domain has no MX, A, or AAAA records for mail delivery."
    return "risky", "Domain mail records could not be confirmed."


def validate_contact_email(email: str, check_deliverability: bool = True) -> EmailQualityResult:
    raw_email = email.strip()
    if not raw_email:
        return EmailQualityResult("invalid", None, "Email address is empty.")

    try:
        syntax_result = validate_email(raw_email, check_deliverability=False)
    except EmailNotValidError as exc:
        return EmailQualityResult("invalid", None, f"Invalid email syntax: {exc}")

    normalized = syntax_result.normalized.lower()
    local, domain = _split_email(normalized)

    if domain in COMMON_DOMAIN_TYPOS:
        suggestion = f"{local}@{COMMON_DOMAIN_TYPOS[domain]}"
        return EmailQualityResult("invalid", normalized, f"Likely domain typo: use {suggestion} if correct.")
    if domain in DISPOSABLE_DOMAINS:
        return EmailQualityResult("invalid", normalized, f"Disposable email domain is not suitable for outreach: {domain}.")
    if domain in RESERVED_DOMAINS or domain.endswith(".example"):
        return EmailQualityResult("invalid", normalized, f"Reserved or test email domain is not deliverable: {domain}.")
    if local in BLOCKED_LOCAL_PARTS:
        return EmailQualityResult("invalid", normalized, f"System mailbox is not suitable for outbound outreach: {local}@.")
    if local.split("+", 1)[0] in PLACEHOLDER_LOCAL_PARTS:
        return EmailQualityResult("invalid", normalized, f"Placeholder/test email local part is not suitable for outreach: {local}@.")

    if not check_deliverability:
        return EmailQualityResult("valid", normalized, "Email syntax is valid.")

    mail_status, mail_reason = _domain_accepts_mail(domain)
    if mail_status != "valid":
        return EmailQualityResult(mail_status, normalized, mail_reason)

    return EmailQualityResult("valid", normalized, f"Email syntax passed. {mail_reason}")

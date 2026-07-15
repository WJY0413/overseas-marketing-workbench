import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlmodel import Session, select

from app.db import create_db_and_tables, engine
from app.models import EmailTemplate

try:
    from app.services.importer import import_bd_json_candidates
except ImportError as exc:
    raise SystemExit("Blocked: production JSON importer is not available. Sync app.services.importer first.") from exc


def _load_template(session: Session, template_name: str) -> EmailTemplate:
    template = session.exec(
        select(EmailTemplate).where(
            EmailTemplate.name == template_name,
            EmailTemplate.is_active == True,  # noqa: E712
        )
    ).first()
    if template is None:
        raise SystemExit(f"Blocked: active template not found: {template_name}")
    return template


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import BD database JSON into production CRM and optionally generate review drafts."
    )
    parser.add_argument("--json", required=True, help="Path to bd-database JSON file.")
    parser.add_argument("--template-name", default="英国带图标准开发信", help="Active template name used with --generate-drafts.")
    parser.add_argument("--generate-drafts", action="store_true", help="Generate review drafts after import. Does not queue or send.")
    parser.add_argument("--report", help="Optional path for JSON report output.")
    args = parser.parse_args()

    json_path = Path(args.json).expanduser().resolve()
    if not json_path.exists():
        raise SystemExit(f"Blocked: JSON file not found: {json_path}")

    payload = json_path.read_bytes()
    create_db_and_tables()

    with Session(engine) as session:
        import_report = import_bd_json_candidates(session, payload, json_path.name)
        report: dict[str, object] = {
            "import": import_report,
            "draft_generation": None,
        }
        if args.generate_drafts:
            from app.main import _generate_drafts_for_contacts

            template = _load_template(session, args.template_name)
            draft_report = _generate_drafts_for_contacts(session, template, import_report["contact_ids"])
            report["draft_generation"] = {
                "template_id": template.id,
                "template_name": template.name,
                **draft_report,
            }

    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        report_path = Path(args.report).expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(output, encoding="utf-8")
        print(f"Report written: {report_path}")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

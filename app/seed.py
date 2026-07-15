from sqlmodel import Session, select

from app.db import engine
from app.models import EmailTemplate, FollowUpRule


def seed_defaults() -> None:
    with Session(engine) as session:
        first_touch = session.exec(select(EmailTemplate).where(EmailTemplate.name == "First Touch - Distributor")).first()
        if first_touch is None:
            first_touch = EmailTemplate(
                name="First Touch - Distributor",
                template_type="first_touch",
                subject="Exploring cooperation with {{ company }}",
                body_html=(
                    "<p>Hi {{ first_name }},</p>"
                    "<p>I am reaching out to explore whether autonomous mowing could be a complementary "
                    "category for your market.</p>"
                    "<p>We can support product introduction, demo materials, training, and market development "
                    "for regional partners.</p>"
                    "<p>Would it be reasonable to briefly exchange views next week?</p>"
                    "<p>Best regards,<br>{{ sender_name }}</p>"
                ),
                body_text=(
                    "Hi {{ first_name }},\n\n"
                    "I am reaching out to explore whether autonomous mowing could be a complementary "
                    "category for your market.\n\n"
                    "Would it be reasonable to briefly exchange views next week?\n\n"
                    "Best regards,\n{{ sender_name }}"
                ),
            )
            session.add(first_touch)
            session.commit()
            session.refresh(first_touch)

        follow_up = session.exec(select(EmailTemplate).where(EmailTemplate.name == "Follow-up - No Reply")).first()
        if follow_up is None:
            follow_up = EmailTemplate(
                name="Follow-up - No Reply",
                template_type="follow_up",
                subject="Re: Exploring cooperation with {{ company }}",
                body_html=(
                    "<p>Hi {{ first_name }},</p>"
                    "<p>Just following up on my previous note. This may be relevant if {{ company }} is reviewing "
                    "new equipment categories or complementary solutions for turf maintenance customers.</p>"
                    "<p>If someone else handles this area, I would appreciate being pointed in the right direction.</p>"
                    "<p>Best regards,<br>{{ sender_name }}</p>"
                ),
                body_text=(
                    "Hi {{ first_name }},\n\n"
                    "Just following up on my previous note. This may be relevant if {{ company }} is reviewing "
                    "new equipment categories or complementary solutions for turf maintenance customers.\n\n"
                    "Best regards,\n{{ sender_name }}"
                ),
            )
            session.add(follow_up)
            session.commit()
            session.refresh(follow_up)

        if session.exec(select(FollowUpRule)).first() is None:
            session.add(FollowUpRule(name="No reply after 3 days", delay_days=3, template_id=follow_up.id))
        session.commit()

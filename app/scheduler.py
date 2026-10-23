import logging
from typing import Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import Schedule, Contact, Message, Setting
from app.whatsapp import can_send_text, send_text, send_template

logger = logging.getLogger(__name__)

_scheduler: Optional[BackgroundScheduler] = None


def execute_scheduled_message(schedule_id: int) -> None:
    """
    Top-level scheduled job runner executed by APScheduler.
    Adheres strictly to the 24-hour rule and checks global kill switch and bot status.
    """
    logger.info("Executing scheduled job for schedule id %d", schedule_id)
    with SessionLocal() as db:
        try:
            # 1. Check global kill switch
            kill_setting = db.query(Setting).filter(Setting.key == "global_kill_switch").first()
            if kill_setting and kill_setting.value.lower() == "on":
                logger.info("Global kill switch is ON. Skipping scheduled job %d.", schedule_id)
                return

            # 2. Fetch schedule
            schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
            if not schedule or not schedule.enabled:
                logger.info("Schedule %d not found or disabled. Skipping.", schedule_id)
                return

            # 3. Fetch contact
            contact = db.query(Contact).filter(Contact.id == schedule.contact_id).first()
            if not contact or not contact.bot_enabled:
                logger.info("Contact %s missing or bot is not enabled. Skipping schedule %d.",
                            schedule.contact_id if schedule else None, schedule_id)
                return

            # 4. Check 24-hour customer care window rule (Meta Cloud API restriction)
            is_within_24h = can_send_text(contact.id, db)

            if settings.WHATSAPP_PROVIDER == "bridge" or is_within_24h:
                logger.info("Sending free-form text via send_text to %s", contact.wa_number)
                wa_msg_id = send_text(contact.wa_number, schedule.body)
                out_body = schedule.body
            else:
                logger.info(
                    "Contact %s is outside 24h window. Sending template '%s' (lang: %s) via send_template.",
                    contact.wa_number,
                    schedule.template_name,
                    schedule.template_lang,
                )
                wa_msg_id = send_template(contact.wa_number, schedule.template_name, schedule.template_lang)
                out_body = f"[Template: {schedule.template_name}]"

            # 5. Store outbound message
            outbound_msg = Message(
                contact_id=contact.id,
                direction="out",
                body=out_body,
                wa_msg_id=wa_msg_id,
                wa_status="accepted",
            )
            db.add(outbound_msg)
            db.commit()
            logger.info("Successfully delivered scheduled message for contact %s (wa_id: %s)", contact.wa_number, wa_msg_id)

        except Exception as e:
            logger.exception("Error executing scheduled message id %d: %s", schedule_id, e)
            db.rollback()


def get_scheduler() -> BackgroundScheduler:
    """Return the global BackgroundScheduler instance."""
    global _scheduler
    if _scheduler is None:
        try:
            if settings.DATABASE_URL.startswith("sqlite"):
                jobstores = {"default": SQLAlchemyJobStore(url="sqlite:///./wa_jobs.db")}
            else:
                jobstores = {"default": SQLAlchemyJobStore(url=settings.DATABASE_URL)}
            _scheduler = BackgroundScheduler(jobstores=jobstores, timezone=settings.TZ)
        except Exception as e:
            logger.warning("Could not initialize SQLAlchemyJobStore (%s). Using default memory store.", e)
            _scheduler = BackgroundScheduler(timezone=settings.TZ)
    return _scheduler


def add_or_update_schedule_job(schedule: Schedule) -> None:
    """Register or update an APScheduler job for a given Schedule model."""
    scheduler = get_scheduler()
    job_id = f"sched-{schedule.id}"

    if not schedule.enabled:
        remove_schedule_job(schedule.id)
        return

    trigger = CronTrigger.from_crontab(schedule.cron, timezone=scheduler.timezone)
    scheduler.add_job(
        execute_scheduled_message,
        trigger=trigger,
        args=[schedule.id],
        id=job_id,
        replace_existing=True,
    )
    logger.info("Registered schedule job '%s' with cron '%s'", job_id, schedule.cron)


def remove_schedule_job(schedule_id: int) -> None:
    """Remove an existing scheduled job from APScheduler."""
    scheduler = get_scheduler()
    job_id = f"sched-{schedule_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info("Removed schedule job '%s'", job_id)


def sync_schedules_from_db(db: Session) -> None:
    """Sync all active schedules from the database into APScheduler."""
    schedules = db.query(Schedule).filter(Schedule.enabled == True).all()
    for s in schedules:
        try:
            add_or_update_schedule_job(s)
        except Exception as e:
            logger.error("Failed to schedule job for Schedule id %d (%s): %s", s.id, s.cron, e)


def run_unreplied_recovery_job() -> None:
    """Periodic job to catch and reply to any messages left unreplied due to network outage."""
    try:
        from app.routes.bridge import recover_unreplied_messages
        recover_unreplied_messages()
    except Exception as e:
        logger.debug("Unreplied recovery job error: %s", e)


def start_scheduler() -> None:
    """Start APScheduler background scheduler."""
    scheduler = get_scheduler()
    if not scheduler.running:
        scheduler.start()
        logger.info("APScheduler started.")

    # Register automatic left-out message recovery job every 45 seconds
    scheduler.add_job(
        run_unreplied_recovery_job,
        trigger="interval",
        seconds=45,
        id="auto-recover-leftout-messages",
        replace_existing=True,
    )
    logger.info("Registered periodic auto-recover job for offline/left-out messages.")


def shutdown_scheduler() -> None:
    """Shutdown APScheduler background scheduler."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped.")

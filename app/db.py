import logging
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from app.config import settings

logger = logging.getLogger(__name__)

connect_args = {}
if settings.DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    global engine, SessionLocal
    from app.models import Setting
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        if not settings.DATABASE_URL.startswith("sqlite"):
            logger.warning(
                "Could not connect to PostgreSQL at %s (%s). Falling back to local SQLite (sqlite:///./wa.db) so server starts cleanly.",
                settings.DATABASE_URL,
                e,
            )
            engine = create_engine("sqlite:///./wa.db", connect_args={"check_same_thread": False})
            SessionLocal.configure(bind=engine)
            Base.metadata.create_all(bind=engine)
        else:
            raise

    # create_all() does not add columns to an existing messages table.
    # Keep this small additive migration so existing local SQLite databases
    # gain delivery tracking without losing their chat history.
    existing_columns = {column["name"] for column in inspect(engine).get_columns("messages")}
    with engine.begin() as connection:
        if "wa_status" not in existing_columns:
            connection.execute(text("ALTER TABLE messages ADD COLUMN wa_status VARCHAR(16)"))
        if "wa_error" not in existing_columns:
            connection.execute(text("ALTER TABLE messages ADD COLUMN wa_error TEXT"))

    with SessionLocal() as session:
        kill_switch = session.query(Setting).filter(Setting.key == "global_kill_switch").first()
        if not kill_switch:
            session.add(Setting(key="global_kill_switch", value="off"))
            session.commit()


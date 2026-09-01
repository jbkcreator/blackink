"""Database connection and session management.

Mirrors Forced Action's Database singleton (src/core/database.py) but adds
the tenant-context chassis Forced Action never had: session_scope(client_id=...)
issues SET LOCAL app.current_client_id so Postgres RLS policies apply, and a
separate system-role engine (BYPASSRLS) is exposed ONLY via
get_system_db_context() — reserved for internal batch jobs, never request
handling. See docs/adr/0001-tenant-isolation-rls-plus-app-layer.md and the
Dev 1 plan's "Tenant isolation chassis" section.
"""

from contextlib import contextmanager
from typing import Generator, Optional

from sqlalchemy import create_engine, text, Engine
from sqlalchemy.orm import sessionmaker, Session

from config.settings import get_settings
from src.core.models import Base


class TenantContextMissingError(RuntimeError):
	"""Raised when tenant-scoped code runs without a client_id in context."""


class Database:
	"""Singleton database manager. Holds two engines: the RLS-subject app
	engine (default) and the BYPASSRLS system engine (opt-in, narrow use)."""

	_instance = None
	_app_engine: Optional[Engine] = None
	_system_engine: Optional[Engine] = None
	_app_session_factory: Optional[sessionmaker] = None
	_system_session_factory: Optional[sessionmaker] = None

	def __new__(cls):
		if cls._instance is None:
			cls._instance = super(Database, cls).__new__(cls)
		return cls._instance

	def __init__(self):
		if self._app_engine is None:
			self._initialize_engines()

	def _initialize_engines(self) -> None:
		settings = get_settings()

		app_dsn = settings.database_url_app or settings.database_url
		self._app_engine = create_engine(
			app_dsn,
			echo=settings.db_echo,
			pool_size=settings.db_pool_size,
			max_overflow=settings.db_max_overflow,
			pool_pre_ping=True,
			pool_recycle=3600,
		)
		self._app_session_factory = sessionmaker(
			bind=self._app_engine, autocommit=False, autoflush=False, expire_on_commit=False
		)

		# System engine is lazy — only created if a system DSN is actually
		# configured, so local dev without it configured doesn't fail at import.
		system_dsn = settings.database_url_system
		if system_dsn:
			self._system_engine = create_engine(
				system_dsn,
				echo=settings.db_echo,
				pool_size=2,
				max_overflow=2,
				pool_pre_ping=True,
				pool_recycle=3600,
			)
			self._system_session_factory = sessionmaker(
				bind=self._system_engine, autocommit=False, autoflush=False, expire_on_commit=False
			)

	@property
	def engine(self) -> Engine:
		if self._app_engine is None:
			self._initialize_engines()
		return self._app_engine

	def create_all_tables(self) -> None:
		"""Dev/test convenience only — production schema changes go through
		migrations/apply_<name>.py, never Base.metadata.create_all()."""
		Base.metadata.create_all(bind=self.engine)

	@contextmanager
	def session_scope(self, client_id: Optional[str] = None) -> Generator[Session, None, None]:
		"""Transactional scope for tenant-bearing tables.

		If client_id is provided, SET LOCAL app.current_client_id is issued
		immediately after checkout so RLS policies (USING owning_client_id =
		current_setting('app.current_client_id', true)) apply. A bare
		session_scope() call against a tenant table returns zero rows under
		RLS, by design — that silent-zero-rows behavior is Postgres's, not
		this code's; callers that need cross-tenant access must use
		get_system_db_context() explicitly, never omit client_id and hope.
		"""
		session = self._app_session_factory()
		try:
			if client_id:
				session.execute(text("SET LOCAL app.current_client_id = :cid"), {"cid": client_id})
			yield session
			session.commit()
		except Exception:
			session.rollback()
			raise
		finally:
			session.close()

	@contextmanager
	def system_session_scope(self) -> Generator[Session, None, None]:
		"""BYPASSRLS session for internal batch jobs only (promotion sweep,
		county-allocation reassessment, the leakage-test harness itself). A
		CI grep-lint asserts no file under src/api/ imports this."""
		if self._system_session_factory is None:
			raise RuntimeError(
				"DATABASE_URL_SYSTEM is not configured - system_session_scope() "
				"requires the blackink_system (BYPASSRLS) role DSN."
			)
		session = self._system_session_factory()
		try:
			yield session
			session.commit()
		except Exception:
			session.rollback()
			raise
		finally:
			session.close()

	def close(self) -> None:
		if self._app_engine:
			self._app_engine.dispose()
			self._app_engine = None
			self._app_session_factory = None
		if self._system_engine:
			self._system_engine.dispose()
			self._system_engine = None
			self._system_session_factory = None


db = Database()


@contextmanager
def get_db_context(client_id: Optional[str] = None) -> Generator[Session, None, None]:
	"""Get an RLS-subject session, optionally tenant-scoped.

	Usage:
		with get_db_context(client_id="acme_pm") as session:
			session.execute(text("SELECT * FROM companies"), {})
	"""
	with db.session_scope(client_id=client_id) as session:
		yield session


@contextmanager
def get_system_db_context() -> Generator[Session, None, None]:
	"""BYPASSRLS session — internal batch jobs only. See Database.system_session_scope."""
	with db.system_session_scope() as session:
		yield session


def get_db(client_id: Optional[str] = None) -> Generator[Session, None, None]:
	"""FastAPI dependency yielding a tenant-scoped session."""
	with get_db_context(client_id=client_id) as session:
		yield session


def scoped_query(session: Session, sql: str, params: dict, client_id: Optional[str]):
	"""Thin wrapper asserting tenant context is present before executing
	hand-written SQL against a tenant table — makes intent visible in a code
	diff and fails loudly instead of relying solely on RLS's silent-zero-rows
	backstop. Raises TenantContextMissingError if client_id is falsy."""
	if not client_id:
		raise TenantContextMissingError(
			"scoped_query() requires a client_id - use get_system_db_context() "
			"explicitly for genuine cross-tenant batch jobs instead of omitting it."
		)
	return session.execute(text(sql), params)


def check_connection() -> bool:
	try:
		with db.session_scope() as session:
			session.execute(text("SELECT 1"))
		return True
	except Exception as e:
		print(f"Database connection failed: {e}")
		return False

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
	"""Singleton database manager. Holds three engines: the owner/migration
	engine (settings.database_url — always, never falls back to the app DSN),
	the RLS-subject app engine (settings.database_url_app), and the BYPASSRLS
	system engine (opt-in, narrow use).

	Why owner and app are two separate engines, not one with a fallback:
	migrations (CREATE TABLE, ALTER TABLE, GRANT, CREATE POLICY) must always
	run as the schema-owning role (settings.database_url — intended to be
	postgres or an equivalent admin role), regardless of whether
	DATABASE_URL_APP is configured. An earlier version of this file used
	`database_url_app or database_url` for a single engine used by both
	migrations and app runtime — once DATABASE_URL_APP was set, migrations
	silently started running as blackink_app instead of the owner, which
	made blackink_app the OWNER of every table it created. Table owners can
	always run `ALTER TABLE ... NO FORCE ROW LEVEL SECURITY` regardless of
	FORCE, so that bug meant a compromised blackink_app credential could
	disable tenant isolation entirely, not just read across tenants. Keep
	these engines separate."""

	_instance = None
	_owner_engine: Optional[Engine] = None
	_app_engine: Optional[Engine] = None
	_system_engine: Optional[Engine] = None
	_owner_session_factory: Optional[sessionmaker] = None
	_app_session_factory: Optional[sessionmaker] = None
	_system_session_factory: Optional[sessionmaker] = None

	def __new__(cls):
		if cls._instance is None:
			cls._instance = super(Database, cls).__new__(cls)
		return cls._instance

	def __init__(self):
		if self._owner_engine is None:
			self._initialize_engines()

	def _initialize_engines(self) -> None:
		settings = get_settings()

		self._owner_engine = create_engine(
			settings.database_url,
			echo=settings.db_echo,
			pool_size=settings.db_pool_size,
			max_overflow=settings.db_max_overflow,
			pool_pre_ping=True,
			pool_recycle=3600,
		)
		self._owner_session_factory = sessionmaker(
			bind=self._owner_engine, autocommit=False, autoflush=False, expire_on_commit=False
		)

		# App engine is lazy — only created if DATABASE_URL_APP is actually
		# configured, so migration-only contexts (no app role provisioned
		# yet) don't fail at import.
		app_dsn = settings.database_url_app
		if app_dsn:
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
		"""The owner/migration engine — kept as the default `.engine` since
		tests/test_schema.py's create_all() and introspection need owner-level
		visibility regardless of whether an app role is configured yet."""
		if self._owner_engine is None:
			self._initialize_engines()
		return self._owner_engine

	def create_all_tables(self) -> None:
		"""Dev/test convenience only — production schema changes go through
		migrations/apply_<name>.py, never Base.metadata.create_all()."""
		Base.metadata.create_all(bind=self.engine)

	@contextmanager
	def owner_session_scope(self) -> Generator[Session, None, None]:
		"""The role migrations run as (settings.database_url — intended to be
		postgres or an equivalent admin role). Never falls back to the app
		DSN. Use this from migrations/apply_*.py, never session_scope()."""
		session = self._owner_session_factory()
		try:
			yield session
			session.commit()
		except Exception:
			session.rollback()
			raise
		finally:
			session.close()

	@contextmanager
	def session_scope(self, client_id: Optional[str] = None) -> Generator[Session, None, None]:
		"""Transactional scope for tenant-bearing tables, as the app role.

		If client_id is provided, SET LOCAL app.current_client_id is issued
		immediately after checkout so RLS policies (USING owning_client_id =
		current_setting('app.current_client_id', true)) apply. A bare
		session_scope() call against a tenant table returns zero rows under
		RLS, by design — that silent-zero-rows behavior is Postgres's, not
		this code's; callers that need cross-tenant access must use
		get_system_db_context() explicitly, never omit client_id and hope.
		"""
		if self._app_session_factory is None:
			raise RuntimeError(
				"DATABASE_URL_APP is not configured - session_scope() requires "
				"the blackink_app role DSN. Migrations should use "
				"get_owner_db_context() instead, never this method."
			)
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
		if self._owner_engine:
			self._owner_engine.dispose()
			self._owner_engine = None
			self._owner_session_factory = None
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
def get_owner_db_context() -> Generator[Session, None, None]:
	"""Owner/migration session — settings.database_url, never the app DSN.
	Use this from every migrations/apply_*.py script. See Database's class
	docstring for why this must be a separate engine from the app one."""
	with db.owner_session_scope() as session:
		yield session


@contextmanager
def get_db_context(client_id: Optional[str] = None) -> Generator[Session, None, None]:
	"""Get an RLS-subject session, optionally tenant-scoped. App runtime
	only — migrations must use get_owner_db_context() instead.

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
		with db.owner_session_scope() as session:
			session.execute(text("SELECT 1"))
		return True
	except Exception as e:
		print(f"Database connection failed: {e}")
		return False

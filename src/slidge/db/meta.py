from __future__ import annotations

import json
from typing import ClassVar, Union

import sqlalchemy as sa
from slixmpp import JID
from sqlalchemy import Dialect


class JIDType(sa.TypeDecorator[JID]):
    """
    Custom SQLAlchemy type for JIDs
    """

    impl = sa.types.TEXT
    cache_ok = True

    def process_bind_param(self, value: JID | None, dialect: sa.Dialect) -> str | None:
        if value is None:
            return value
        return str(value)

    def process_result_value(
        self, value: str | None, dialect: sa.Dialect
    ) -> JID | None:
        if value is None:
            return value
        return JID(value)


JSONSerializableTypes = Union[str, float, None, "JSONSerializable", bool]
JSONSerializable = dict[str, JSONSerializableTypes]


class JSONEncodedDict(sa.TypeDecorator[JSONSerializable]):
    """
    Custom SQLAlchemy type for dictionaries stored as JSON

    Note that mutations of the dictionary are not detected by SQLAlchemy,
    which is why use ``attributes.flag_modified()`` in ``UserStore.update()``
    """

    impl = sa.VARCHAR

    cache_ok = True

    def process_bind_param(
        self, value: JSONSerializable | None, dialect: Dialect
    ) -> str | None:
        if value is None:
            return None
        return json.dumps(value)

    def process_result_value(
        self, value: object, dialect: Dialect
    ) -> JSONSerializable | None:
        if value is None:
            return None
        return json.loads(value)  # type:ignore


class Base(sa.orm.DeclarativeBase):
    type_annotation_map: ClassVar[dict[object, object]] = {
        JSONSerializable: JSONEncodedDict,
        JID: JIDType,
    }


Base.metadata.naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_`%(constraint_name)s`",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def get_engine(path: str, echo: bool = False, pool_size: int = 5) -> sa.Engine:
    from sqlalchemy import log as sqlalchemy_log

    engine = sa.create_engine(path, pool_size=pool_size)
    # CAN rollback unfinished SQLite commits v1
    # A failed COMMIT can leave DBAPI active even after SQLAlchemy closes it.
    if engine.dialect.name == "sqlite":
        @sa.event.listens_for(engine, "reset")
        def rollback_unfinished(dbapi_connection, _record, reset_state):
            if not reset_state.terminate_only and dbapi_connection.in_transaction:
                dbapi_connection.rollback()
    if echo:
        sqlalchemy_log._add_default_handler = lambda x: None  # type:ignore
        engine.echo = True
    return engine

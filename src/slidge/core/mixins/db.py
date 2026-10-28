import logging
import typing
from contextlib import contextmanager

import sqlalchemy as sa

from ...db.meta import Base, JSONSerializable
from ...db.models import Contact, Room

if typing.TYPE_CHECKING:
    from slidge.util.types import AnyGateway


class DBMixin:
    stored: Base
    xmpp: "AnyGateway"
    log: logging.Logger

    def merge(self) -> None:
        with self.xmpp.store.session() as orm:
            self.stored = orm.merge(self.stored)

    def commit(self) -> None:
        with self.xmpp.store.session(expire_on_commit=False) as orm:
            self.log.debug("Merging %s", self.stored)
            self.stored = orm.merge(self.stored)
            self.log.debug("Merged %s", self.stored)
            orm.add(self.stored)
            self.log.debug("Committing to DB")
            orm.commit()


class UpdateInfoMixin(DBMixin):
    """
    This mixin just adds a context manager that prevents commiting to the DB
    on every attribute change.
    """

    stored: Contact | Room
    xmpp: "AnyGateway"
    log: logging.Logger

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._updating_info = False
        self.__deserialize()

    def __deserialize(self) -> None:
        if self.stored.extra_attributes is not None:
            self.deserialize_extra_attributes(self.stored.extra_attributes)

    def refresh(self, attrs: list[str] | None = None) -> None:
        with self.xmpp.store.session(expire_on_commit=False) as orm:
            orm.add(self.stored)
            orm.refresh(self.stored, attribute_names=attrs)
            if attrs is None or "extra_attributes" in attrs:
                self.__deserialize()

    def serialize_extra_attributes(self) -> JSONSerializable | None:
        """
        If you want custom attributes of your instance to be stored persistently
        to the DB, here is where you have to return them as a dict to be used in
        `deserialize_extra_attributes()`.

        """
        return None

    def deserialize_extra_attributes(self, data: JSONSerializable) -> None:
        """
        This is where you get the dict that you passed in
        `serialize_extra_attributes()`.

        ⚠ Since it is serialized as json, dictionary keys are converted to strings!
        Be sure to convert to other types if necessary.
        """
        pass

    @contextmanager
    def updating_info(self) -> typing.Iterator[None]:
        self._updating_info = True
        yield
        self._updating_info = False
        self.stored.updated = True
        self.commit()

    def commit(self) -> None:
        if self._updating_info:
            self.log.debug("Not updating %s right now", self.stored)
        else:
            self.stored.extra_attributes = self.serialize_extra_attributes()
            super().commit()

    def update_stored_attribute(self, **kwargs: object) -> None:
        for key, value in kwargs.items():
            setattr(self.stored, key, value)
        if self._updating_info:
            return
        with self.xmpp.store.session() as orm:
            orm.execute(
                sa.update(self.stored.__class__)
                .where(self.stored.__class__.id == self.stored.id)
                .values(**kwargs)
            )
            orm.commit()

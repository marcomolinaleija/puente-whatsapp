from .chat_state import ChatStateMixin
from .marker import MarkerMixin
from .message import MessageContentMixin


class MessageMixin(ChatStateMixin, MarkerMixin, MessageContentMixin):
    __slots__: list[str] = []


__all__ = ("MessageMixin",)

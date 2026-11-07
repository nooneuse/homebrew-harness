"""Mutation-aware JSON containers; cached persisted messages preserve plain SDK shapes."""

import copy
import hashlib
import json
import weakref
from collections import deque

from homebrew_harness.security.data_boundary import redact


def set_context(messages, name, value, *, position=1):
    """Replace one host attachment; preserve an unchanged message and its position."""
    matches = [i for i, message in enumerate(messages) if message.get("name") == name]
    if len(matches) == 1:
        index = matches[0]
        if messages[index] == value:
            return
        if value is not None:
            messages[index] = value
            return
    prepared = [message for message in messages if message.get("name") != name]
    if value is not None:
        prepared.insert(min(position, len(prepared)), value)
    messages[:] = prepared


def plain(value):
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return copy.deepcopy(value)


def wrap(value, changed=None):
    if isinstance(value, dict):
        return TrackedDict(plain(value), changed=changed)
    if isinstance(value, list):
        return TrackedList(plain(value), changed=changed)
    return value


class TrackedDict(dict):
    def __init__(self, value=(), *, changed=None):
        self.version = 0
        self._changed = changed
        self._encoded = None
        self.watchers = {}
        dict.__init__(self, {k: wrap(v, self.touch) for k, v in dict(value).items()})

    def touch(self):
        self.version += 1
        self._encoded = None
        if self._changed:
            self._changed()
        for key, reference in list(self.watchers.items()):
            owner = reference()
            if owner is None:
                self.watchers.pop(key, None)
            else:
                owner.touch()

    def stored_json(self):
        from homebrew_harness.security.data_boundary import private_revision

        revision = private_revision()
        if self._encoded is None or self._encoded[0] != revision:
            raw = json.dumps(
                redact(self),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            self._encoded = (revision, raw, hashlib.sha256(raw.encode()).hexdigest())
        return self._encoded[1:]

    def __deepcopy__(self, memo):
        return plain(self)

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, wrap(value, self.touch))
        self.touch()

    def __delitem__(self, key):
        dict.__delitem__(self, key)
        self.touch()

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def pop(self, key, *default):
        exists = key in self
        value = dict.pop(self, key, *default)
        if exists:
            self.touch()
        return value

    def popitem(self):
        value = dict.popitem(self)
        self.touch()
        return value

    def clear(self):
        if self:
            dict.clear(self)
            self.touch()

    def __ior__(self, other):
        self.update(other)
        return self


class TrackedList(list):
    def __init__(self, value=(), *, changed=None):
        self.version = 0
        self._changed = changed
        list.__init__(self, [wrap(v, self.touch) for v in value])

    def touch(self):
        self.version += 1
        if self._changed:
            self._changed()

    def __deepcopy__(self, memo):
        return plain(self)

    def __setitem__(self, key, value):
        list.__setitem__(
            self,
            key,
            [wrap(v, self.touch) for v in value]
            if isinstance(key, slice)
            else wrap(value, self.touch),
        )
        self.touch()

    def __delitem__(self, key):
        list.__delitem__(self, key)
        self.touch()

    def append(self, value):
        list.append(self, wrap(value, self.touch))
        self.touch()

    def extend(self, values):
        list.extend(self, [wrap(v, self.touch) for v in values])
        self.touch()

    def insert(self, index, value):
        list.insert(self, index, wrap(value, self.touch))
        self.touch()

    def pop(self, index=-1):
        value = list.pop(self, index)
        self.touch()
        return value

    def remove(self, value):
        list.remove(self, value)
        self.touch()

    def clear(self):
        if self:
            list.clear(self)
            self.touch()

    def reverse(self):
        list.reverse(self)
        self.touch()

    def sort(self, *args, **kwargs):
        list.sort(self, *args, **kwargs)
        self.touch()

    def __iadd__(self, values):
        self.extend(values)
        return self

    def __imul__(self, count):
        list.__imul__(self, count)
        self.touch()
        return self


class ConversationHistory(TrackedList):
    def __init__(self, value=()):
        self.version = 0
        self._changed = None
        self.events = deque(maxlen=1024)
        list.__init__(self, [self.message(v) for v in value])

    def message(self, value):
        value = value if isinstance(value, TrackedDict) else TrackedDict(value)
        value.watchers[id(self)] = weakref.ref(self)
        return value

    def touch(self, kind="rewrite", values=()):
        if kind != "append":
            self.events.clear()
        self.version += 1
        self.events.append((self.version, kind, tuple(values)))

    def append_delta(self, since):
        if since == self.version:
            return []
        events = [item for item in self.events if item[0] > since]
        if (
            not events
            or events[0][0] != since + 1
            or any(item[1] != "append" for item in events)
        ):
            return None
        return [value for _, _, values in events for value in values]

    def __setitem__(self, key, value):
        values = (
            [self.message(v) for v in value]
            if isinstance(key, slice)
            else self.message(value)
        )
        if key == slice(None):
            if len(values) == len(self) and all(a is b for a, b in zip(self, values)):
                return
            if len(values) >= len(self) and all(a is b for a, b in zip(self, values)):
                extra = values[len(self) :]
                list.__setitem__(self, key, values)
                self.touch("append", extra)
                return
        list.__setitem__(self, key, values)
        self.touch()

    def append(self, value):
        value = self.message(value)
        list.append(self, value)
        self.touch("append", [value])

    def extend(self, values):
        values = [self.message(v) for v in values]
        list.extend(self, values)
        self.touch("append", values)

    def insert(self, index, value):
        list.insert(self, index, self.message(value))
        self.touch()


def encoded_message(message):
    if isinstance(message, TrackedDict):
        return message.stored_json()
    raw = json.dumps(
        redact(message),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return raw, hashlib.sha256(raw.encode()).hexdigest()

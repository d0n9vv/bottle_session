# -*- coding: utf-8 -*-
import inspect

import base64
import collections
import datetime
import os
import cPickle as pickle
import time
from bottle import request, response, PluginError


class BaseSession(collections.MutableMapping):
    def __init__(self, session_id=None, data=None, expires=None,
                 duration=None, ip_address=None, regeneration_interval=None,
                 next_regeneration=None, **kwargs):
        # if session_id is True, we're loading a previously initialized session
        if session_id:
            self.session_id = session_id
            self.data = data
            self.duration = duration
            self.expires = expires
            self.dirty = False
        else:
            self.session_id = self._generate_session_id()
            self.data = {}
            self.duration = duration
            self.expires = self._expires_at()
            self.dirty = True

        self.ip_address = ip_address
        self.regeneration_interval = regeneration_interval
        self.next_regeneration = \
            next_regeneration or self._next_regeneration_at()
        self._delete_cookie = False

    def __repr__(self):
        return '<session id: %s data: %s>' % (self.session_id, self.data)

    def __str__(self):
        return self.session_id

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value
        self.dirty = True

    def __delitem__(self, key):
        del self.data[key]
        self.dirty = True

    def keys(self):
        return self.data.keys()

    def __iter__(self):
        return self.data.__iter__()

    def __len__(self):
        return len(self.data.keys())

    def _generate_session_id(cls):
        return os.urandom(32).encode('hex')  # 256 bits of entropy

    def _is_expired(self):
        """Check if the session has expired."""
        if self.expires is None:  # never expire
            return False
        return datetime.datetime.utcnow() > self.expires

    def _expires_at(self):
        """Find out the expiration time. Returns datetime.datetime."""
        v = self.duration
        if v is None:  # never expire
            return None
        elif isinstance(v, datetime.timedelta):
            pass
        elif isinstance(v, (int, long)):
            self.duration = datetime.timedelta(seconds=v)
        elif isinstance(v, basestring):
            self.duration = datetime.timedelta(seconds=int(v))
        else:
            self.duration = datetime.timedelta(seconds=900)  # 15 mins

        return datetime.datetime.utcnow() + self.duration

    def _serialize_expires(self):
        """ Determines what value of expires is stored to DB during save()."""
        if self.expires is None:
            return None
        else:
            return int(time.mktime(self.expires.timetuple()))

    def _should_regenerate(self):
        """Determine if the session_id should be regenerated."""
        if self.regeneration_interval is None:  # never regenerate
            return False
        return datetime.datetime.utcnow() > self.next_regeneration

    def _next_regeneration_at(self):
        """Return a datetime object when the next session id regeneration
        should occur."""
        # convert whatever value to an timedelta (period in seconds)
        # store it in self.regeneration_interval to prevent
        # converting in later calls and return the datetime
        # of next planned regeneration
        v = self.regeneration_interval
        if v is None:  # never regenerate
            return None
        elif isinstance(v, datetime.timedelta):
            pass
        elif isinstance(v, (int, long)):
            self.regeneration_interval = datetime.timedelta(seconds=v)
        elif isinstance(v, basestring):
            self.regeneration_interval = datetime.timedelta(seconds=int(v))
        else:
            self.regeneration_interval = datetime.timedelta(
                seconds=240)  # 4 mins

        return datetime.datetime.utcnow() + self.regeneration_interval

    def invalidate(self):
        """Destorys the session, both server-side and client-side.
        As a best practice, it should be used when the user logs out of
        the application."""
        self.delete()  # remove server-side
        self._delete_cookie = True  # remove client-side

    def refresh(self, duration=None, new_session_id=False):  # the opposite of invalidate
        if duration:
            self.duration = duration
            self.expires = self._expires_at()
        else:
            self.expires = self._expires_at()
        if new_session_id:
            self.delete()
            self.session_id = self._generate_session_id()
            self.next_regeneration = self._next_regeneration_at()
        self.dirty = True  # force save
        self.save()

    def save(self):
        """Save the session data and metadata to the backend storage
        if necessary (self.dirty == True). On successful save set
        dirty to False."""
        pass

    @staticmethod
    def load(session_id, location):
        """Load the stored session from storage backend or return
        None if the session was not found, in case of stale cookie."""
        pass

    def delete(self):
        """Remove all data representing the session from backend storage."""
        pass

    @staticmethod
    def delete_expired(file_path):
        """Deletes sessions with timestamps in the past form storage."""
        pass

    def serialize(self):
        dump = {'session_id': self.session_id,
                'data': self.data,
                'duration': self.duration,
                'expires': self.expires,
                'ip_address': self.ip_address,
                'regeneration_interval': self.regeneration_interval,
                'next_regeneration': self.next_regeneration}
        return base64.encodestring(pickle.dumps(dump))

    @staticmethod
    def deserialize(datastring):
        return pickle.loads(base64.decodestring(datastring))


class MemcachedSession(BaseSession):
    """Class responsible for Memcached stored sessions. It uses the
    pylibmc library because it's fast. It communicates with the
    memcached server through the binary protocol and uses async
    I/O (no_block set to 1) to speed things up even more.

    Session ID is used as a key. The value consists of colon
    separated values of serializes session object, expiry timestamp,
    IP address and User-Agent.

    Values are stored with timeout set to the difference between
    saving time and expiry time in seconds. Therefore, no
    old sessions will be held in Memcached memory."""

    def __init__(self, connection, **kwargs):
        super(MemcachedSession, self).__init__(**kwargs)
        self.connection = connection
        if 'session_id' not in kwargs:
            self.save()

    def _serialize_expires(self):
        """ Determines what value of expires is stored to DB during save()."""
        if self.expires is None:
            return '-1'
        else:
            return str(int(time.mktime(self.expires.timetuple())))

    def save(self):
        """Write the session to Memcached. Session ID is used as
        key, value is constructed as colon separated values of
        serialized session, session expiry timestamp, ip address
        and User-Agent.
        The value is not stored indefinitely. It's expiration time
        in seconds is calculated as the difference between the saving
        time and session expiry."""
        if not self.dirty:
            return
        value = ':'.join((self.serialize(),
                          self._serialize_expires(),
                          self.ip_address))
        # count how long should it last and then add or rewrite
        if self.expires is None:
            # set expiry 30 days, max for memcache
            # http://code.google.com/p/memcached/wiki/FAQ#What_are_the_limits_on_setting_expire_time?_%28why_is_there_a_30_d
            self.connection.set(self.session_id, value,
                                time=datetime.timedelta.max.seconds * 30)
        else:
            live_sec = self.expires - datetime.datetime.utcnow()
            self.connection.set(
                self.session_id, value, time=live_sec.seconds)
        self.dirty = False

    @staticmethod
    def load(session_id, connection):
        """Load the session from storage."""
        try:
            value = connection.get(session_id)
            if value:
                data = value.split(':', 1)[0]
                kwargs = MemcachedSession.deserialize(data)
                return MemcachedSession(connection, **kwargs)
        except:
            return None
        return None

    def delete(self):
        """Delete the session from storage."""
        self.connection.delete(self.session_id)

    def delete_expired(connection):
        """With Memcached as session storage, this function does
        not make sense as all keys are saved with expiry time
        exactly the same as the session's. Hence Memcached takse
        care of cleaning out the garbage."""
        raise NotImplementedError


class SessionPlugin(object):
    name = 'session'
    api = 2

    def __init__(self, connection, cookie_secret=None, keyword='session'):
        self.connection = connection
        self.cookie_secret = cookie_secret
        self.keyword = keyword

    def setup(self, app):
        for other in app.plugins:
            if not isinstance(other, SessionPlugin):
                continue
            if other.keyword == self.keyword:
                raise PluginError("Found another session plugin with \
                    conflicting settings(non-unique keyword).")

    def apply(self, callback, route):
        conf = route.config.get('session') or {}
        keyword = conf.get('keyword', self.keyword)

        args = inspect.getargspec(route.callback)[0]
        if keyword not in args:
            return callback

        def wrapper(*args, **kwargs):
            session_id = request.get_cookie('session_id',
                                            secret=self.cookie_secret)
            kw = {
                'duration': 900,
                'expires': None,
                'regeneration_interval': 240,
                'ip_address': request.environ.get('REMOTE_ADDR')
            }

            session = MemcachedSession.load(session_id, self.connection)
            if session is None or session._is_expired():
                session = MemcachedSession(self.connection, **kw)
                response.set_cookie('session_id', session.session_id,
                                    secret=self.cookie_secret)

            if session is not None:
                if session._should_regenerate():
                    session.refresh(new_session_id=True)

            kwargs[keyword] = session

            rv = callback(*args, **kwargs)
            return rv
        return wrapper

Plugin = SessionPlugin
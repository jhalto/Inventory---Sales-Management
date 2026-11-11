import hashlib

from django.core.exceptions import ImproperlyConfigured
from django.db import connections


def database_now(using='default'):
    with connections[using].cursor() as cursor:
        cursor.execute('SELECT clock_timestamp()')
        return cursor.fetchone()[0]


def lock_recipient(namespace, identity, *, using='default', wait=True):
    """A transaction lock whose identity survives deletion/recreation of a row."""
    connection = connections[using]
    if not connection.in_atomic_block:
        raise RuntimeError('Recipient locks require an enclosing transaction.')
    if connection.vendor != 'postgresql':
        raise ImproperlyConfigured('Recipient lifecycle locks require PostgreSQL.')
    key = int.from_bytes(hashlib.blake2b(
        f'accounts.recipient:{namespace}:{identity}'.encode(), digest_size=8,
    ).digest(), 'big', signed=True)
    sql = 'SELECT pg_advisory_xact_lock(%s)' if wait else 'SELECT pg_try_advisory_xact_lock(%s)'
    with connection.cursor() as cursor:
        cursor.execute(sql, [key])
        return True if wait else cursor.fetchone()[0]
